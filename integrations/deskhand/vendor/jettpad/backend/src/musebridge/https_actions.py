"""Durable admission adapter for the copied executor; not publicly exposed."""
import asyncio
import uuid


class HttpsActions:
    def __init__(self, journal, client):
        self.journal = journal
        self.client = client

    def _claim_for_send(self, identifier, payload, kind='executor.submit'):
        claim = self.journal.claim(identifier, kind, payload)
        if not claim['new']:
            return claim
        if not self.journal.transition(identifier, 'accepted', 'running'):
            return {'new': False, 'state': 'uncertain', 'receipt': None}
        return {'new': True, 'state': 'running', 'receipt': None}

    async def submit(self, identifier, payload):
        try:
            parsed = uuid.UUID(identifier)
            if parsed.version != 4 or str(parsed) != identifier or not isinstance(payload, dict):
                raise ValueError('Invalid action request')
        except (ValueError, TypeError, AttributeError):
            raise ValueError('Invalid action request') from None
        # Do disk I/O off the event loop so STOP remains responsive. Shield the
        # claim and reconcile it on cancellation before returning control.
        claiming = asyncio.create_task(asyncio.to_thread(self._claim_for_send, identifier, payload))
        try:
            claim = await asyncio.shield(claiming)
        except asyncio.CancelledError:
            claim = await claiming
            if claim['new']:
                await asyncio.shield(asyncio.to_thread(self.journal.transition, identifier, 'running', 'uncertain'))
            raise
        if not claim['new']:
            return {'id': identifier, **claim}
        try:
            reply = await self.client.request('executor.submit', payload, identifier)
            if not isinstance(reply, dict) or reply.get('in_reply_to') != identifier:
                raise ValueError('Uncorrelated bridge reply')
            kind, data = reply.get('type'), reply.get('payload')
            expected_job = str(uuid.uuid5(uuid.NAMESPACE_URL, 'jett-executor:' + identifier))
            job_status = data.get('status') if isinstance(data, dict) else None
            if (kind == 'executor.result' and isinstance(data, dict) and data.get('id') == expected_job
                    and job_status in {'queued', 'running', 'done', 'failed', 'cancelled', 'uncertain'}):
                state = {'queued': 'running', 'done': 'complete'}.get(job_status, job_status)
                receipt = {'job_id': data['id'], 'response_type': kind}
                changed = await asyncio.to_thread(self.journal.transition, identifier, 'running', state, receipt)
                return {'id': identifier, 'new': True, 'state': state if changed else 'uncertain',
                        'receipt': receipt if changed else None}
            if kind == 'error' and isinstance(data, dict):
                receipt = {'code': str(data.get('code', 'rejected'))[:100], 'response_type': 'error'}
                # A generic protocol error does not prove admission did not
                # occur; retain the identity and require reconciliation.
                changed = await asyncio.to_thread(self.journal.transition, identifier, 'running', 'uncertain', receipt)
                return {'id': identifier, 'new': True, 'state': 'uncertain',
                        'receipt': receipt if changed else None}
        except asyncio.CancelledError:
            await asyncio.shield(asyncio.to_thread(self.journal.transition, identifier, 'running', 'uncertain'))
            raise
        except Exception:
            pass
        await asyncio.to_thread(self.journal.transition, identifier, 'running', 'uncertain')
        return {'id': identifier, 'new': True, 'state': 'uncertain', 'receipt': None}

    async def send_chat(self, identifier, target, kind, payload):
        fingerprint_payload = {'in_reply_to': target, 'payload': payload}
        claiming = asyncio.create_task(asyncio.to_thread(self._claim_for_send, identifier, fingerprint_payload, kind))
        try:
            claim = await asyncio.shield(claiming)
        except asyncio.CancelledError:
            claim = await claiming
            if claim['new']:
                await asyncio.shield(asyncio.to_thread(self.journal.transition, identifier, 'running', 'uncertain'))
            raise
        if not claim['new']:
            return
        try:
            await self.client.send_chat_reply(kind, identifier, target, payload)
        finally:
            await asyncio.shield(asyncio.to_thread(self.journal.transition, identifier, 'running', 'uncertain'))
