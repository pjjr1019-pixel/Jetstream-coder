"""Bind an HTTPS lease to an actual local JettPad bridge connection.

This helper is not yet exposed by the gateway. The owner must handle
unsolicited bridge events and keep renewing the lease while work is active.
"""
from .https_bridge_client import LocalBridgeClient


class HttpsAdapterSession:
    def __init__(self, bridge, on_unsolicited):
        self.bridge = bridge
        self.on_unsolicited = on_unsolicited
        self.client = None
        self.token = None

    async def open(self):
        if self.client is not None or self.token is not None:
            raise ValueError('Adapter already open')
        lease = self.bridge.https_session
        server = self.bridge.local_adapter_server
        if lease is None or server is None or not server.sockets:
            raise RuntimeError('Local bridge adapter unavailable')
        token = await lease.open()
        self.token = token
        port = server.sockets[0].getsockname()[1]
        client = LocalBridgeClient(f'ws://127.0.0.1:{port}/v1/bridge', self.bridge.token,
                                   self.on_unsolicited, session_token=token)
        try:
            await client.open()
            if not await lease.valid(token):
                raise RuntimeError('Adapter lease expired while connecting')
            self.client = client
            return token
        except BaseException:
            await client.close()
            self.token = None
            await lease.close()
            raise

    async def close(self):
        token, client = self.token, self.client
        self.token, self.client = None, None
        try:
            if token is not None:
                await self.bridge.https_session.close(token)
        finally:
            if client is not None:
                await client.close()
