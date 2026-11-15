"""Local JettPad protocol client for an HTTPS adapter, not a control endpoint.

Uses an actual authenticated WebSocket and never changes bridge authority.
Unsolicited messages are passed to an explicit handler, never silently dropped.
"""
import asyncio
import json
import ipaddress
import time
import uuid
from urllib.parse import urlsplit

from websockets.asyncio.client import connect


class LocalBridgeClient:
    def __init__(self, uri, credential, on_unsolicited, *, connect_timeout=5, session_token=None):
        parsed = urlsplit(uri)
        try:
            loopback = ipaddress.ip_address(parsed.hostname or '').is_loopback
        except ValueError:
            loopback = False
        if (parsed.scheme != 'ws' or not loopback or parsed.username is not None or
                parsed.password is not None or parsed.path != '/v1/bridge' or parsed.query or parsed.fragment):
            raise ValueError('Bridge client requires a local loopback endpoint')
        self.uri = uri
        self.credential = credential
        self.on_unsolicited = on_unsolicited
        self.connect_timeout = connect_timeout
        self.session_token = session_token
        self.socket = None
        self.reader = None
        self.pending = {}
        self.send_lock = asyncio.Lock()

    async def open(self):
        if self.socket is not None:
            raise RuntimeError('Bridge connection already open')
        headers = {'X-Deskhand-Session': self.session_token} if self.session_token else None
        socket = await asyncio.wait_for(connect(self.uri, max_size=1024 * 1024, additional_headers=headers), self.connect_timeout)
        try:
            await socket.send(json.dumps({'type': 'auth', 'token': self.credential}))
            reply = json.loads(await asyncio.wait_for(socket.recv(), self.connect_timeout))
            if not isinstance(reply, dict) or reply.get('type') != 'auth_ok' or reply.get('protocol') != 1:
                raise ValueError('Bridge authentication failed')
        except BaseException:
            await socket.close()
            raise
        self.socket = socket
        self.reader = asyncio.create_task(self._read(socket))

    async def request(self, kind, payload, identifier, *, timeout=10):
        parsed = uuid.UUID(identifier)
        if parsed.version != 4 or str(parsed) != identifier or not isinstance(payload, dict):
            raise ValueError('Invalid request')
        socket = self.socket
        if socket is None:
            raise ConnectionError('Bridge connection is closed')
        if identifier in self.pending:
            raise ValueError('Request already in flight')
        future = asyncio.get_running_loop().create_future()
        self.pending[identifier] = future
        try:
            async with self.send_lock:
                if self.socket is not socket:
                    raise ConnectionError('Bridge connection changed')
                await socket.send(json.dumps({'id': identifier, 'type': kind, 'ts': time.time(),
                                              'in_reply_to': None, 'payload': payload}, allow_nan=False))
            return await asyncio.wait_for(future, timeout)
        finally:
            self.pending.pop(identifier, None)

    async def send_chat_reply(self, kind, identifier, in_reply_to, payload):
        for value in (identifier, in_reply_to):
            parsed = uuid.UUID(value)
            if parsed.version != 4 or str(parsed) != value:
                raise ValueError('Invalid chat identity')
        if kind not in {'chat_accepted', 'chat_reply', 'chat_failed'} or not isinstance(payload, dict):
            raise ValueError('Invalid chat reply')
        socket = self.socket
        if socket is None:
            raise ConnectionError('Bridge connection is closed')
        async with self.send_lock:
            if self.socket is not socket:
                raise ConnectionError('Bridge connection changed')
            await socket.send(json.dumps({'id': identifier, 'type': kind, 'ts': time.time(),
                'in_reply_to': in_reply_to, 'payload': payload}, allow_nan=False))

    async def _read(self, socket):
        try:
            async for raw in socket:
                message = json.loads(raw)
                if not isinstance(message, dict):
                    raise ValueError('Invalid bridge response')
                future = self.pending.get(message.get('in_reply_to'))
                if future is not None and not future.done():
                    future.set_result(message)
                else:
                    await self.on_unsolicited(message)
        finally:
            if self.socket is socket:
                self.socket = None
            await socket.close()
            for future in list(self.pending.values()):
                if not future.done():
                    future.set_exception(ConnectionError('Bridge connection lost; outcome may be uncertain'))

    async def close(self):
        socket, reader = self.socket, self.reader
        self.socket, self.reader = None, None
        if socket is not None:
            await socket.close()
        if reader is not None:
            await asyncio.gather(reader, return_exceptions=True)
