"""Short-lived HTTP ownership lease; no implicit desktop permissions.

This component is not exposed by the read-only gateway. Integrators must
explicitly bind its expiry callback to bridge revocation before admitting work.
"""
import asyncio
import hmac
import secrets
import time


class HttpsSession:
    def __init__(self, on_expire, *, lease_seconds=30, monotonic=time.monotonic, ownership_lock=None, can_acquire=lambda: True):
        if not 1 <= lease_seconds <= 60:
            raise ValueError('Invalid session lifetime')
        self._on_expire = on_expire
        self._lease_seconds = lease_seconds
        self._clock = monotonic
        self._can_acquire = can_acquire
        self._lock = ownership_lock if ownership_lock is not None else asyncio.Lock()
        self._token = None
        self._deadline = None
        self._generation = 0
        self._watcher = None
        self._faulted = False

    def occupied_under_lock(self):
        """Called only while the shared bridge ownership lock is held."""
        return self._token is not None or self._faulted

    @property
    def lease_seconds(self):
        return self._lease_seconds

    def matches_under_lock(self, token):
        """Validate an adapter capability while the ownership lock is held."""
        return self._valid(token)

    def live_generation(self):
        """Read-only snapshot for executor admission and worker checks."""
        generation = self._generation
        if (self._token is None or self._faulted or self._deadline is None
                or self._clock() >= self._deadline):
            return None
        return generation

    def generation_is_live(self, generation):
        return generation is not None and self.live_generation() == generation

    async def open(self):
        async with self._lock:
            if self._faulted:
                raise RuntimeError('Session revocation was not confirmed')
            if self._token is not None:
                raise ValueError('An HTTPS session already owns this bridge')
            if not self._can_acquire():
                raise ValueError('Bridge ownership is unavailable')
            self._token = secrets.token_urlsafe(32)
            self._deadline = self._clock() + self._lease_seconds
            self._generation += 1
            generation = self._generation
            self._watcher = asyncio.create_task(self._watch(generation))
            return self._token

    async def renew(self, token):
        async with self._lock:
            if not self._valid(token):
                return False
            self._deadline = self._clock() + self._lease_seconds
            return True

    async def valid(self, token):
        async with self._lock:
            return self._valid(token)

    def _valid(self, token):
        return (isinstance(token, str) and self._token is not None
                and self._deadline is not None and self._clock() < self._deadline
                and hmac.compare_digest(token.encode('utf-8'), self._token.encode('utf-8')))

    async def close(self, token=None):
        async with self._lock:
            if self._token is None:
                return False
            if token is not None and not self._valid(token):
                return False
            self._token = None
            self._deadline = None
            self._generation += 1
            watcher, self._watcher = self._watcher, None
            if watcher is not None and watcher is not asyncio.current_task():
                watcher.cancel()
        # B2-E (B-7): release the ownership lock before the revocation
        # callback. It awaits the adapter WS close handshake, and gateway
        # auth (valid/renew/open) must not stall behind it. The lease state
        # above is already cleared, so no replacement owner can open on the
        # old token; a new open still has to pass can_acquire().
        try:
            await self._on_expire()
        except BaseException:
            async with self._lock:
                self._faulted = True
            raise
        return True

    async def _watch(self, generation):
        try:
            while True:
                async with self._lock:
                    if generation != self._generation or self._deadline is None:
                        return
                    delay = self._deadline - self._clock()
                if delay <= 0:
                    async with self._lock:
                        if generation != self._generation or self._deadline is None:
                            return
                        if self._clock() < self._deadline:
                            continue
                        self._token = None
                        self._deadline = None
                        self._generation += 1
                        self._watcher = None
                    # B2-E (B-7): same lock release as close(): the expiry
                    # callback awaits the adapter WS close handshake.
                    try:
                        await self._on_expire()
                    except Exception:
                        async with self._lock:
                            self._faulted = True
                        return
                    return
                await asyncio.sleep(min(delay, 0.25))
        except asyncio.CancelledError:
            return
