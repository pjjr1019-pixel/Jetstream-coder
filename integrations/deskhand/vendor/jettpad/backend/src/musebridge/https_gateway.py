"""Opt-in, loopback-only read-only gateway for a TLS-terminating proxy.

The caller owns its lifecycle and proxy configuration. No control operations
are exposed here; an HTTP probe is not a MuseBridge WebSocket session.
"""
import hmac
import ipaddress
import asyncio
from collections import deque
import math
import time
import uuid

from aiohttp import web


class ReadOnlyGateway:
    def __init__(self, bridge, *, session_enabled=False, actions_enabled=False, adapter_factory=None):
        if actions_enabled and not session_enabled:
            raise ValueError('Action admission requires session ownership')
        self.bridge = bridge
        self.session_enabled = session_enabled
        self.actions_enabled = actions_enabled
        self._adapter_factory = adapter_factory
        self._adapter = None
        self._session_gate = asyncio.Lock()
        self._events = None
        self._event_lock = asyncio.Lock()
        self._pending_event = None
        self._acked_events = deque(maxlen=64)
        self.runner = None
        self.port = None

    async def start(self):
        if self.runner is not None:
            raise RuntimeError('Gateway already running')
        app = web.Application(client_max_size=264192)
        for prefix in ('', '/deskhand-prototype'):
            app.router.add_get(prefix + '/status', self._status, allow_head=False)
            app.router.add_get(prefix + '/capabilities', self._capabilities, allow_head=False)
            app.router.add_post(prefix + '/request', self._request)
            app.router.add_get(prefix + '/requests/{identifier}', self._request_status, allow_head=False)
            if self.session_enabled:
                app.router.add_post(prefix + '/session/open', self._session_open)
                app.router.add_post(prefix + '/session/renew', self._session_renew)
                app.router.add_post(prefix + '/session/close', self._session_close)
                app.router.add_get(prefix + '/events', self._poll_event, allow_head=False)
                app.router.add_post(prefix + '/events/ack', self._ack_event)
                app.router.add_get(prefix + '/chats/{identifier}', self._chat_status, allow_head=False)
                app.router.add_post(prefix + '/pad-replies', self._pad_reply)
                app.router.add_get(prefix + '/pad-replies/{identifier}', self._pad_reply_status, allow_head=False)
            if self.actions_enabled:
                app.router.add_get(prefix + '/requests/{identifier}/captures/{step}', self._capture, allow_head=False)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        try:
            site = web.TCPSite(runner, '127.0.0.1', 0)
            await site.start()
            self.port = site._server.sockets[0].getsockname()[1]
            self.runner = runner
        except BaseException:
            await runner.cleanup()
            raise
        return self.port

    async def close(self):
        runner, self.runner, self.port = self.runner, None, None
        if runner is not None:
            await runner.cleanup()
        async with self._session_gate:
            adapter, self._adapter = self._adapter, None
            self._events = None
            self._pending_event = None
            self._acked_events.clear()
            if adapter is not None:
                await adapter.close()

    async def _bridge_event(self, message):
        queue = self._events
        if queue is None:
            raise RuntimeError('HTTPS event session unavailable')
        # A full queue closes the transport and revokes the lease. Uncertain
        # events are reconciled from the bridge's durable sources on reconnect.
        queue.put_nowait(message)

    def _authorized(self, request):
        try:
            peer = request.transport.get_extra_info('peername')
            if not peer or not ipaddress.ip_address(peer[0]).is_loopback:
                return False
            headers = request.headers.getall('Authorization', [])
            if len(headers) != 1 or not headers[0].startswith('Bearer '):
                return False
            supplied = headers[0][7:].encode('utf-8')
            expected = self.bridge.token.encode('utf-8')
            return hmac.compare_digest(supplied, expected)
        except (ValueError, AttributeError, UnicodeError):
            return False

    def _response(self, request, builder):
        headers = {'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'}
        if not self._authorized(request):
            return web.json_response({'error': 'unauthorized'}, status=401, headers=headers)
        if self.bridge.server is None:
            return web.json_response({'error': 'bridge_unavailable'}, status=503, headers=headers)
        return web.json_response(builder(), headers=headers)

    async def _status(self, request):
        return self._response(request, self.bridge.status)

    async def _capabilities(self, request):
        return self._response(request, lambda: {
            'transport': 'deskhand-https',
            'available_routes': ['GET /status', 'GET /capabilities', 'POST /request', 'GET /requests/{id}']
                + (['POST /session/open', 'POST /session/renew', 'POST /session/close', 'GET /events', 'POST /events/ack', 'GET /chats/{id}', 'POST /pad-replies', 'GET /pad-replies/{id}'] if self.session_enabled else [])
                + (['GET /requests/{id}/captures/{step}'] if self.actions_enabled else []),
            'session_routes_available': self.session_enabled,
            'action_routes_available': self.actions_enabled,
            'websocket_session_connected': bool(self.bridge.connected),
            'bridge_capabilities': self.bridge.capabilities(),
        })

    async def _request(self, request):
        headers = {'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'}
        if not self._authorized(request):
            return web.json_response({'error': 'unauthorized'}, status=401, headers=headers)
        if self.bridge.server is None:
            return web.json_response({'error': 'bridge_unavailable'}, status=503, headers=headers)
        try:
            if request.content_type != 'application/json':
                raise ValueError('JSON required')
            message = await request.json()
            if not isinstance(message, dict) or set(message) != {'id', 'type', 'ts', 'in_reply_to', 'payload'}:
                raise ValueError('Invalid envelope')
            identifier = message['id']
            parsed = uuid.UUID(identifier)
            if parsed.version != 4 or str(parsed) != identifier:
                raise ValueError('Invalid request id')
            if type(message['ts']) not in (int, float) or not math.isfinite(message['ts']):
                raise ValueError('Invalid timestamp')
            if not isinstance(message['payload'], dict):
                raise ValueError('Invalid request payload')
            kind = message['type']
            if kind not in {'chat_accepted', 'chat_reply', 'chat_failed'} and message['in_reply_to'] is not None:
                raise ValueError('Invalid reply correlation')
            if kind == 'get_status' and not message['payload']:
                response_kind, payload = 'status', self.bridge.status()
            elif kind == 'get_capabilities' and not message['payload']:
                response_kind, payload = 'capabilities', {
                    'transport': 'deskhand-https', 'action_routes_available': self.actions_enabled,
                    'session_routes_available': self.session_enabled,
                    'bridge_capabilities': self.bridge.capabilities(),
                }
            elif kind == 'executor.submit' and self.actions_enabled:
                tokens = request.headers.getall('X-Deskhand-Session', [])
                adapter = self._adapter
                if (len(tokens) != 1 or adapter is None or adapter.client is None or adapter.client.socket is None or
                        not isinstance(adapter.token, str) or
                        not hmac.compare_digest(tokens[0].encode('utf-8'), adapter.token.encode('utf-8')) or
                        not await self.bridge.https_session.valid(tokens[0])):
                    return web.json_response({'error': 'session_unavailable'}, status=409, headers=headers)
                from .https_actions import HttpsActions
                payload = await HttpsActions(self.bridge.https_journal, adapter.client).submit(identifier, message['payload'])
                response_kind = 'executor.admission'
            elif kind in {'chat_accepted', 'chat_reply', 'chat_failed'} and self.session_enabled:
                target = message['in_reply_to']
                parsed_target = uuid.UUID(target)
                if parsed_target.version != 4 or str(parsed_target) != target:
                    raise ValueError('Invalid chat correlation')
                tokens = request.headers.getall('X-Deskhand-Session', [])
                adapter = self._adapter
                if (len(tokens) != 1 or adapter is None or adapter.client is None or adapter.client.socket is None
                        or not isinstance(adapter.token, str)
                        or not hmac.compare_digest(tokens[0].encode(), adapter.token.encode())
                        or not await self.bridge.https_session.valid(tokens[0])):
                    return web.json_response({'error': 'session_unavailable'}, status=409, headers=headers)
                try:
                    from .https_actions import HttpsActions
                    await HttpsActions(self.bridge.https_journal, adapter.client).send_chat(
                        identifier, target, kind, message['payload'])
                except ValueError:
                    raise
                except Exception:
                    return web.json_response({'error': 'delivery_unconfirmed'}, status=503, headers=headers)
                # WebSocket send confirms transport acceptance only, not a
                # durable bridge or DeskHand chat receipt.
                response_kind, payload = 'chat.delivery', {'delivery': 'unconfirmed'}
            else:
                raise ValueError('Operation not available on this gateway')
            return web.json_response({'id': str(uuid.uuid4()), 'type': response_kind,
                'ts': time.time(), 'in_reply_to': identifier, 'payload': payload}, headers=headers)
        except (ValueError, TypeError, AttributeError):
            return web.json_response({'error': 'invalid_request'}, status=400, headers=headers)

    async def _request_status(self, request):
        headers = {'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'}
        if not self._authorized(request):
            return web.json_response({'error': 'unauthorized'}, status=401, headers=headers)
        if self.bridge.server is None:
            return web.json_response({'error': 'bridge_unavailable'}, status=503, headers=headers)
        identifier = request.match_info['identifier']
        try:
            value = uuid.UUID(identifier)
            if value.version != 4 or str(value) != identifier:
                raise ValueError('Invalid request id')
        except (ValueError, AttributeError):
            return web.json_response({'error': 'invalid_request'}, status=400, headers=headers)
        journal = self.bridge.https_journal
        result = journal.get(identifier) if journal is not None else None
        if result is None:
            return web.json_response({'error': 'not_found'}, status=404, headers=headers)
        receipt = result.get('receipt') or {}
        if result['kind'] == 'executor.submit' and result['state'] in ('running', 'complete', 'failed', 'cancelled', 'uncertain'):
            expected = str(uuid.uuid5(uuid.NAMESPACE_URL, 'jett-executor:' + identifier))
            if receipt.get('job_id') is not None and receipt.get('job_id') != expected:
                return web.json_response({'error': 'receipt_unconfirmed'}, status=503, headers=headers)
            try:
                job = await asyncio.to_thread(self.bridge.executor.get, expected)
            except (OSError, ValueError):
                job = None
            if job is None:
                result['execution'] = {'status': 'unknown'}
            else:
                result['execution'] = {key: job.get(key) for key in
                    ('id', 'status', 'created_ts', 'started_ts', 'finished_ts', 'reason', 'steps')}
                terminal_state = {'done': 'complete', 'failed': 'failed',
                                  'cancelled': 'cancelled', 'uncertain': 'uncertain'}.get(job.get('status'))
                if result['state'] == 'running' and terminal_state is not None:
                    updated = await asyncio.to_thread(journal.transition, identifier, 'running', terminal_state,
                        {'job_id': expected, 'response_type': 'executor.status'})
                    if updated:
                        result.update(journal.get(identifier))
        return web.json_response(result, headers=headers)

    async def _session_open(self, request):
        headers = {'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'}
        if not self._authorized(request):
            return web.json_response({'error': 'unauthorized'}, status=401, headers=headers)
        if self.bridge.https_session is None or self.bridge.server is None:
            return web.json_response({'error': 'bridge_unavailable'}, status=503, headers=headers)
        if request.can_read_body:
            return web.json_response({'error': 'invalid_request'}, status=400, headers=headers)
        async with self._session_gate:
            if self._adapter is not None:
                if await self.bridge.https_session.valid(self._adapter.token):
                    return web.json_response({'error': 'ownership_unavailable'}, status=409, headers=headers)
                stale, self._adapter = self._adapter, None
                await stale.close()
            queue = asyncio.Queue(maxsize=64)
            self._events = queue
            self._pending_event = None
            self._acked_events.clear()
            async def receive_event(message):
                if self._events is not queue:
                    raise RuntimeError('HTTPS event session replaced')
                await self._bridge_event(message)
            if self._adapter_factory is None:
                from .https_adapter_session import HttpsAdapterSession
                adapter = HttpsAdapterSession(self.bridge, receive_event)
            else:
                adapter = self._adapter_factory(self.bridge, receive_event)
            try:
                token = await adapter.open()
            except (ValueError, RuntimeError, OSError):
                self._events = None
                return web.json_response({'error': 'ownership_unavailable'}, status=409, headers=headers)
            self._adapter = adapter
        return web.json_response({'session': token, 'lease_seconds': self.bridge.https_session.lease_seconds}, headers=headers)

    async def _pad_reply(self, request):
        """Accept one correlated chat receipt, never arbitrary file writes."""
        headers = {'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'}
        if not self._authorized(request):
            return web.json_response({'error': 'unauthorized'}, status=401, headers=headers)
        tokens = request.headers.getall('X-Deskhand-Session', [])
        adapter = self._adapter
        if (self.bridge.server is None or len(tokens) != 1 or adapter is None or adapter.client is None
                or adapter.client.socket is None or not isinstance(adapter.token, str)
                or not hmac.compare_digest(tokens[0].encode(), adapter.token.encode())
                or not await self.bridge.https_session.valid(tokens[0])):
            return web.json_response({'error': 'session_unavailable'}, status=409, headers=headers)
        try:
            if request.content_type != 'application/json':
                raise ValueError('JSON required')
            body = await request.json()
            if not isinstance(body, dict) or set(body) != {'reply_id', 'text'}:
                raise ValueError('Invalid reply')
            from .host_stop import ensure_host_allows
            from .https_pad_reply import store_reply
            async with self._session_gate:
                if self._adapter is not adapter or not await self.bridge.https_session.valid(tokens[0]):
                    return web.json_response({'error': 'session_unavailable'}, status=409, headers=headers)
                ensure_host_allows()
                # STOP latch removed: no stop_latched gate here any more.
                created = await asyncio.to_thread(store_reply, body['reply_id'], body['text'])
            return web.json_response({'reply_id': body['reply_id'], 'stored': True, 'new': created}, headers=headers)
        except (ValueError, TypeError, AttributeError):
            return web.json_response({'error': 'reply_unavailable'}, status=400, headers=headers)
        except Exception:
            return web.json_response({'error': 'delivery_unconfirmed'}, status=503, headers=headers)

    async def _pad_reply_status(self, request):
        headers = {'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'}
        if not self._authorized(request):
            return web.json_response({'error': 'unauthorized'}, status=401, headers=headers)
        tokens = request.headers.getall('X-Deskhand-Session', [])
        adapter = self._adapter
        if (self.bridge.server is None or len(tokens) != 1 or adapter is None or adapter.client is None
                or adapter.client.socket is None or not isinstance(adapter.token, str)
                or not hmac.compare_digest(tokens[0].encode(), adapter.token.encode())
                or not await self.bridge.https_session.valid(tokens[0])):
            return web.json_response({'error': 'session_unavailable'}, status=409, headers=headers)
        identifier = request.match_info['identifier']
        try:
            from .https_pad_reply import reply_stored
            stored = await asyncio.to_thread(reply_stored, identifier)
            if self._adapter is not adapter or not await self.bridge.https_session.valid(tokens[0]):
                return web.json_response({'error': 'session_unavailable'}, status=409, headers=headers)
            return web.json_response({'reply_id': identifier, 'stored': stored}, headers=headers)
        except (ValueError, TypeError, AttributeError):
            return web.json_response({'error': 'invalid_request'}, status=400, headers=headers)
        except Exception:
            return web.json_response({'error': 'status_unavailable'}, status=503, headers=headers)

    async def _capture(self, request):
        headers = {'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'}
        if not self._authorized(request):
            return web.json_response({'error': 'unauthorized'}, status=401, headers=headers)
        adapter = self._adapter
        tokens = request.headers.getall('X-Deskhand-Session', [])
        if (self.bridge.server is None or len(tokens) != 1 or adapter is None or adapter.client is None
                or adapter.client.socket is None or not isinstance(adapter.token, str)
                or not hmac.compare_digest(tokens[0].encode('utf-8'), adapter.token.encode('utf-8'))
                or not await self.bridge.https_session.valid(tokens[0])):
            return web.json_response({'error': 'session_unavailable'}, status=409, headers=headers)
        try:
            identifier = request.match_info['identifier']
            parsed = uuid.UUID(identifier)
            if parsed.version != 4 or str(parsed) != identifier:
                raise ValueError('Invalid request id')
            step_text = request.match_info['step']
            step = int(step_text)
            if str(step) != step_text or not 1 <= step <= 128:
                raise ValueError('Invalid step')
            record = await asyncio.to_thread(self.bridge.https_journal.get, identifier)
            expected = str(uuid.uuid5(uuid.NAMESPACE_URL, 'jett-executor:' + identifier))
            if (not record or record['kind'] != 'executor.submit' or record['state'] != 'complete'
                    or (record.get('receipt') or {}).get('job_id') != expected):
                return web.json_response({'error': 'capture_unavailable'}, status=404, headers=headers)
            from .executor_capture_local import read_capture
            capture = await asyncio.to_thread(read_capture, self.bridge, expected, step)
            if self._adapter is not adapter or not await self.bridge.https_session.valid(tokens[0]):
                return web.json_response({'error': 'session_unavailable'}, status=409, headers=headers)
            return web.json_response(capture, headers=headers)
        except (ValueError, TypeError, OSError):
            return web.json_response({'error': 'capture_unavailable'}, status=404, headers=headers)

    async def _session_mutate(self, request, action):
        headers = {'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'}
        if not self._authorized(request):
            return web.json_response({'error': 'unauthorized'}, status=401, headers=headers)
        if self.bridge.https_session is None or self.bridge.server is None:
            return web.json_response({'error': 'bridge_unavailable'}, status=503, headers=headers)
        tokens = request.headers.getall('X-Deskhand-Session', [])
        if request.can_read_body or len(tokens) != 1:
            return web.json_response({'error': 'invalid_request'}, status=400, headers=headers)
        async with self._session_gate:
            try:
                success = await getattr(self.bridge.https_session, action)(tokens[0])
                if success and action == 'close':
                    adapter, self._adapter = self._adapter, None
                    self._events = None
                    self._pending_event = None
                    self._acked_events.clear()
                    if adapter is not None:
                        await adapter.close()
            except Exception:
                return web.json_response({'error': 'revocation_unconfirmed'}, status=503, headers=headers)
        if not success:
            return web.json_response({'error': 'session_unavailable'}, status=409, headers=headers)
        return web.json_response({'ok': True}, headers=headers)

    async def _session_renew(self, request):
        return await self._session_mutate(request, 'renew')

    async def _session_close(self, request):
        return await self._session_mutate(request, 'close')

    async def _poll_event(self, request):
        headers = {'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'}
        if not self._authorized(request):
            return web.json_response({'error': 'unauthorized'}, status=401, headers=headers)
        adapter, queue = self._adapter, self._events
        tokens = request.headers.getall('X-Deskhand-Session', [])
        if (self.bridge.server is None or adapter is None or queue is None or len(tokens) != 1
                or adapter.client is None or adapter.client.socket is None
                or not isinstance(adapter.token, str)
                or not hmac.compare_digest(tokens[0].encode(), adapter.token.encode())
                or not await self.bridge.https_session.valid(tokens[0])):
            return web.json_response({'error': 'session_unavailable'}, status=409, headers=headers)
        async with self._event_lock:
            if self._adapter is not adapter or self._events is not queue or not await self.bridge.https_session.valid(tokens[0]):
                return web.json_response({'error': 'session_unavailable'}, status=409, headers=headers)
            if self._pending_event is None:
                try:
                    event = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return web.json_response({'event': None}, headers=headers)
                self._pending_event = {'delivery_id': str(uuid.uuid4()), 'message': event}
            return web.json_response({'event': self._pending_event}, headers=headers)

    async def _ack_event(self, request):
        headers = {'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'}
        if not self._authorized(request):
            return web.json_response({'error': 'unauthorized'}, status=401, headers=headers)
        adapter = self._adapter
        tokens = request.headers.getall('X-Deskhand-Session', [])
        if (self.bridge.server is None or adapter is None or len(tokens) != 1
                or adapter.client is None or adapter.client.socket is None
                or not isinstance(adapter.token, str)
                or not hmac.compare_digest(tokens[0].encode(), adapter.token.encode())
                or not await self.bridge.https_session.valid(tokens[0])):
            return web.json_response({'error': 'session_unavailable'}, status=409, headers=headers)
        try:
            body = await request.json()
            if not isinstance(body, dict) or set(body) != {'delivery_id'}:
                raise ValueError('Invalid event acknowledgment')
            async with self._event_lock:
                if self._adapter is not adapter or not await self.bridge.https_session.valid(tokens[0]):
                    return web.json_response({'error': 'session_unavailable'}, status=409, headers=headers)
                pending = self._pending_event
                if body['delivery_id'] in self._acked_events:
                    return web.json_response({'ok': True}, headers=headers)
                if pending is None or body['delivery_id'] != pending['delivery_id']:
                    return web.json_response({'error': 'event_unavailable'}, status=409, headers=headers)
                self._acked_events.append(body['delivery_id'])
                self._pending_event = None
            return web.json_response({'ok': True}, headers=headers)
        except (ValueError, TypeError):
            return web.json_response({'error': 'invalid_request'}, status=400, headers=headers)

    async def _chat_status(self, request):
        headers = {'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'}
        if not self._authorized(request):
            return web.json_response({'error': 'unauthorized'}, status=401, headers=headers)
        if self.bridge.server is None:
            return web.json_response({'error': 'bridge_unavailable'}, status=503, headers=headers)
        identifier = request.match_info['identifier']
        try:
            parsed = uuid.UUID(identifier)
            if parsed.version != 4 or str(parsed) != identifier:
                raise ValueError('Invalid chat ID')
        except (ValueError, AttributeError):
            return web.json_response({'error': 'invalid_request'}, status=400, headers=headers)
        record = self.bridge._chat_pending.get(identifier)
        if record is None:
            return web.json_response({'error': 'not_found'}, status=404, headers=headers)
        # Recovery metadata only: do not disclose message or reply content.
        return web.json_response({'id': identifier, 'status': record.get('status', 'unknown'),
            'terminal': bool(record.get('terminal')), 'accepted': bool(record.get('accepted')),
            'delivery': record.get('delivery', 'unknown'), 'recovered': bool(record.get('recovered'))}, headers=headers)
