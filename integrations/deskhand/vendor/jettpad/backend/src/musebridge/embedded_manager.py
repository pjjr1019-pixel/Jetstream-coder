"""Staged wiring for the existing companion and guarded built-in streaming.

The local application must supply its authenticated operator, exact pinned
Sunshine controller factory, private-storage verifier and local setup UI.
Nothing in this module starts automatically or changes permissions.
"""
from pathlib import Path
import asyncio

from companion_data import paired_certificates, settings
from owned_paired_transport import OwnedPairedTransport
from paired_sunshine_session import PairedSunshineSession
from embedded_stream_backend import EmbeddedStreamBackend
from peer_binding import validate
from .companion import CompanionChannel
from .runtime_paths import pad_data


class EmbeddedManager:
    def __init__(self,bridge,binding,private_dir,node_factory,controller_factory,
                 verify_private,check_ready,on_sign_in,on_address):
        if not all(callable(item) for item in
                   (node_factory,controller_factory,verify_private,check_ready,on_sign_in,on_address)):
            raise ValueError('Local connection setup unavailable')
        self.bridge=bridge;self.binding=validate(binding);self.private=Path(private_dir)
        if verify_private(self.private) is not True:
            raise PermissionError('Private connection storage unavailable')
        self._controller_factory=controller_factory;self._verify_private=verify_private
        self._check_ready=check_ready
        self._close_task=None
        self.transport=None
        self.backend=EmbeddedStreamBackend(self.private/'embedded-stream-receipts.sqlite3',
                                           self._new_session,self._allow_start)
        channel=CompanionChannel(bridge,expected_phone=self.binding['address'],stream_backend=self.backend)
        self.transport=OwnedPairedTransport(node_factory,channel.handle,on_sign_in,
                                            lambda:paired_certificates(),on_address)

    def _new_session(self):
        if self.transport is None or not self.transport.authorized():
            raise ValueError('Paired phone is not connected')
        def still_ready(controller):
            try:
                from viewing_access import authorized,enabled
                fingerprint=self.binding['fingerprint']
                if (not settings()['remote_start'] or fingerprint not in paired_certificates()
                        or not self.transport.authorized() or
                        (enabled() and not authorized(fingerprint))):
                    return False
                return self._check_ready(controller) is True
            except Exception as error:
                import logging
                logging.getLogger('musebridge.embedded').warning('stream preflight failed: %s: %s',type(error).__name__,str(error)[:200])
                return False
        return PairedSunshineSession(self.transport,self._controller_factory(),still_ready)

    def _allow_start(self,fingerprint,epoch):
        try:
            from viewing_access import authorized,blocked,enabled
            state=self.bridge.stream_stop.status()
            return (settings()['remote_start'] is True and fingerprint in paired_certificates()
                    and self.transport is not None and self.transport.authorized()
                    and state.get('available') is True and type(epoch) is int and state.get('epoch')==epoch
                    and not blocked({'stop_latched':False},pad_data())
                    and (not enabled() or authorized(fingerprint)))
        except Exception:return False

    async def start(self,*,locally_approved):
        if locally_approved is not True or self._verify_private(self.private) is not True:
            raise PermissionError('Local setup and private storage approval required')
        node_dir=self.private/'embedded-node'
        if node_dir.is_symlink():raise PermissionError('Private connection storage unavailable')
        node_dir.mkdir(exist_ok=True,mode=0o700)
        if (not node_dir.is_dir() or node_dir.resolve(strict=True)!=node_dir.absolute()
                or self._verify_private(self.private) is not True):
            raise PermissionError('Private connection storage unavailable')
        await self.transport.start(self.binding,node_dir,private_acl_verified=True,locally_approved=True)

    async def close(self):
        if self._close_task is None:
            async def cleanup():
                try:await self.backend.close()
                finally:await self.transport.close()
            self._close_task=asyncio.create_task(cleanup())
        await asyncio.shield(self._close_task)
