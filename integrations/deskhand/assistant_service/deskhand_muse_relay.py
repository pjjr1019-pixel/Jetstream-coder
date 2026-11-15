"""Correlate Deskhand turns with the staged JettPad browser mailbox.

This adapter has no authority to start the browser or bridge. The owner must
provide an isolated mailbox and inbox, and must connect desktop STOP separately.
"""
import json
import os
import threading
import time
import uuid
from pathlib import Path

from deskhand_dispatch import NotDispatched, Reply, StopConfirmed


class MuseRelay:
    def __init__(self, mailbox, inbox, timeout=600, ensure_ready=None):
        self.mailbox = mailbox
        self.inbox = Path(inbox)
        self.timeout = timeout
        self.ensure_ready = ensure_ready
        self.stopping = threading.Event()
        self.pending_receipt = None

    def prepare_turn(self):
        self.stopping.clear()
        self.pending_receipt = None

    def request_stop(self):
        # Partial cancel (100.025 S4): stops only the wait for the browser
        # to claim the job; a request already sent to Muse keeps running.
        # Bridge STOP must be requested separately; an already sent Muse
        # request has an uncertain outcome.
        self.stopping.set()

    def _retire_unknown(self, job):
        retire = getattr(self.mailbox, 'retire_uncertain', None)
        if job and job.get('attempt') and callable(retire):
            self.mailbox.transition(job['id'], job['attempt'], 'uncertain')
            retire(job['id'], job['attempt'])

    def __call__(self, request):
        if self.stopping.is_set():
            raise StopConfirmed('Muse turn was stopped before it was queued')
        if self.ensure_ready is not None:
            try:
                self.ensure_ready()
            except Exception as error:
                raise NotDispatched('Message was not sent: ' + str(error)[:400]) from None
        job = self.mailbox.enqueue({'id': request.turn_id, 'text': request.text, 'ts': time.time()})
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            job = self.mailbox.get(request.turn_id)
            if not job or job['state'] in ('abandoned', 'local_only', 'retired_unknown'):
                raise RuntimeError('Muse delivery was interrupted; check the conversation before retrying')
            try:
                identity = str(uuid.UUID(job['reply_id']))
                if identity != job['reply_id']:
                    raise ValueError()
            except (ValueError, TypeError, KeyError, AttributeError):
                raise ValueError('Invalid Muse reply identity') from None
            https_reply = self.mailbox.https_reply(identity)
            if job['state'] in ('sending', 'sent', 'uncertain', 'complete') and isinstance(https_reply, str) and https_reply.strip():
                self.pending_receipt = (request.turn_id, job.get('attempt'))
                return Reply(request.turn_id, request.route, https_reply)
            receipt = self.inbox / (identity + '.json')
            if job['state'] in ('sending', 'sent', 'uncertain', 'complete') and receipt.exists():
                if receipt.is_symlink() or receipt.resolve().parent != self.inbox.resolve():
                    raise ValueError('Invalid Muse reply receipt')
                with receipt.open('rb') as stream:
                    stat = os.fstat(stream.fileno())
                    if stat.st_nlink != 1 or stat.st_size > 1024 * 1024:
                        raise ValueError('Invalid Muse reply receipt')
                    row = json.loads(stream.read(1024 * 1024 + 1).decode('utf-8-sig'))
                if row.get('id') != job['reply_id'] or row.get('from') != 'jett' or not isinstance(row.get('text'), str) or not row['text'].strip():
                    raise ValueError('Muse reply identity did not match the request')
                self.pending_receipt = (request.turn_id, job.get('attempt'))
                return Reply(request.turn_id, request.route, row['text'])
            if job['state'] == 'uncertain':
                self._retire_unknown(job)
                raise RuntimeError('Muse delivery is unconfirmed; check the conversation before retrying')
            if self.ensure_ready is not None:
                try:
                    self.ensure_ready()
                except Exception:
                    if self.mailbox.cancel_queued(request.turn_id):
                        raise StopConfirmed('Muse disconnected before the browser claimed the turn') from None
                    self._retire_unknown(job)
                    raise RuntimeError('Muse disconnected after delivery may have started; check the conversation before retrying') from None
            if self.stopping.wait(.1):
                if self.mailbox.cancel_queued(request.turn_id):
                    raise StopConfirmed('Muse turn was stopped before the browser claimed it')
                self._retire_unknown(job)
                raise RuntimeError('Muse delivery may have started; check the conversation before retrying')
        if not self.mailbox.cancel_queued(request.turn_id):
            self._retire_unknown(self.mailbox.get(request.turn_id))
        raise TimeoutError('Muse did not return a reply; check the conversation before retrying')

    def reply_saved(self, request):
        # Called only after Deskhand's mailbox commits the correlated reply.
        if self.pending_receipt and self.pending_receipt[0] == request.turn_id:
            identity, attempt = self.pending_receipt
            if attempt:
                self.mailbox.complete(identity, attempt)
            self.pending_receipt = None
