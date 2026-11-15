"""Staged Jett assistant-to-executor handoff; no model or listener lives here."""
from __future__ import annotations
import hashlib,json,sqlite3,time,uuid,threading
from pathlib import Path
from contextlib import closing,contextmanager
from executor.protocol import validate_task
from musebridge.lifecycle import local_request

class UncertainAction(RuntimeError):pass
class ControlBlocked(ValueError):pass

class AssistantExecutor:
    def __init__(self,home:Path,ledger:Path,request=local_request,stop=None,progress=None,status_request=None):
        self.home=Path(home);self.ledger=Path(ledger);self.request=request;self.stop=stop;self.progress=progress;self._dispatch_lock=threading.RLock()
        self.status_request=status_request if status_request is not None else (local_request if request is local_request else None)
        self.ledger.parent.mkdir(parents=True,exist_ok=True)
        with closing(sqlite3.connect(self.ledger,timeout=5)) as db:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('CREATE TABLE IF NOT EXISTS calls (turn_id TEXT NOT NULL, call_id TEXT NOT NULL, request_id TEXT NOT NULL, job_id TEXT NOT NULL, fingerprint TEXT NOT NULL, state TEXT NOT NULL, PRIMARY KEY(turn_id,call_id))')
            db.commit()
    def _progress(self,job_id,receipt):
        if self.progress is not None:
            try:self.progress(job_id,receipt)
            except Exception:pass  # Display failures must not repeat or block actions.
    def _call(self,action,payload):
        reply=self.request(self.home,action,payload,timeout=8)
        if not isinstance(reply,dict) or not isinstance(reply.get('result'),dict):raise UncertainAction('Executor returned an invalid receipt; check job status before retrying.')
        return reply['result']
    @staticmethod
    def _ids(turn_id,call_id):
        if str(uuid.UUID(turn_id))!=turn_id:raise ValueError('Invalid turn ID')
        if not isinstance(call_id,str) or not 1<=len(call_id)<=200:raise ValueError('Invalid tool call ID')
        rid=str(uuid.uuid5(uuid.UUID(turn_id),'jett-assistant-tool:'+call_id))
        return rid,str(uuid.uuid5(uuid.NAMESPACE_URL,'jett-executor:'+rid))
    def submit(self,turn_id,call_id,task):
        with self._dispatch_lock:
            if self.stop is not None and self.stop.is_set():raise ValueError('Action cancelled before submission')
            return self._submit(turn_id,call_id,task)
    def _submit(self,turn_id,call_id,task):
        task,fingerprint=validate_task(task);rid,jid=self._ids(turn_id,call_id)
        # A definite, read-only STOP observation blocks before journaling or
        # dispatch. The executor still enforces all permissions atomically.
        state=None
        if self.status_request is not None:
            try:state=self.status_request(self.home,timeout=2)
            except Exception:state=None
        # Journal before dispatch. A crash or lost receipt is never retried.
        with closing(sqlite3.connect(self.ledger,timeout=5)) as db:
            db.execute('PRAGMA synchronous=FULL');db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT request_id,job_id,fingerprint,state FROM calls WHERE turn_id=? AND call_id=?',(turn_id,call_id)).fetchone()
            if row:
                if row[2]!=fingerprint:raise ValueError('Tool call ID reused for a different task')
                db.commit();return self.status(turn_id,call_id)
            duplicate=db.execute('SELECT call_id FROM calls WHERE turn_id=? AND fingerprint=?',(turn_id,fingerprint)).fetchone()
            if duplicate:raise ValueError('This task was already submitted in this turn. Check its existing status; do not repeat it.')
            if isinstance(state,dict) and state.get('stop_latched') is True:
                raise ControlBlocked('STOP is active. Clear STOP locally before starting PC actions.')
            db.execute('INSERT INTO calls VALUES (?,?,?,?,?,?)',(turn_id,call_id,rid,jid,fingerprint,'dispatching'));db.commit()
        self._progress(jid,None)
        try:
            receipt=self._call('executor_submit_local',{'request_id':rid,'task':task})
        except Exception as error:
            raise UncertainAction('Action outcome unconfirmed. Check its status; do not resubmit automatically.') from error
        if receipt.get('id')!=jid:raise UncertainAction('Executor receipt did not match the job; check its status.')
        with closing(sqlite3.connect(self.ledger,timeout=5)) as db:
            db.execute('UPDATE calls SET state=? WHERE turn_id=? AND call_id=?',('accepted',turn_id,call_id));db.commit()
        self._progress(jid,receipt)
        return receipt
    def status(self,turn_id,call_id):
        rid,jid=self._ids(turn_id,call_id)
        with closing(sqlite3.connect(self.ledger,timeout=5)) as db:row=db.execute('SELECT request_id,job_id FROM calls WHERE turn_id=? AND call_id=?',(turn_id,call_id)).fetchone()
        if row!=(rid,jid):raise ValueError('Unknown action for this conversation turn')
        try:
            result=self._call('executor_status',{'id':jid});self._progress(jid,result);return result
        except Exception as error:
            self._progress(jid,None)
            raise UncertainAction('Action status unavailable. Do not repeat the action automatically.') from error
    def cancel(self,turn_id,call_id):
        rid,jid=self._ids(turn_id,call_id)
        with closing(sqlite3.connect(self.ledger,timeout=5)) as db:row=db.execute('SELECT request_id,job_id FROM calls WHERE turn_id=? AND call_id=?',(turn_id,call_id)).fetchone()
        if row!=(rid,jid):raise ValueError('Unknown action for this conversation turn')
        try:
            result=self._call('executor_cancel',{'id':jid});self._progress(jid,result);return result
        except Exception:
            self._progress(jid,None);raise

    def windows(self):
        if self.stop is not None and self.stop.is_set():raise ValueError('Window listing cancelled')
        return self._call('assistant_window_list_local',{})

    def capture(self,turn_id,call_id,step):
        if self.stop is not None and self.stop.is_set():raise ValueError('Capture cancelled')
        rid,jid=self._ids(turn_id,call_id)
        with closing(sqlite3.connect(self.ledger,timeout=5)) as db:row=db.execute('SELECT request_id,job_id FROM calls WHERE turn_id=? AND call_id=?',(turn_id,call_id)).fetchone()
        if row!=(rid,jid):raise ValueError('Unknown action for this conversation turn')
        return self._call('executor_capture_local',{'id':jid,'step':step})

    def cancel_turn(self,turn_id):
        # Serialize with submission so cancellation cannot race between the
        # durable ledger entry and dispatch. Every known ID is tried once;
        # uncertain cancellation is surfaced to the caller, never retried as a new job.
        if str(uuid.UUID(turn_id))!=turn_id:raise ValueError('Invalid turn ID')
        with self._dispatch_lock:
            with closing(sqlite3.connect(self.ledger,timeout=5)) as db:
                ids=[row[0] for row in db.execute('SELECT call_id FROM calls WHERE turn_id=?',(turn_id,))]
            failures=[]
            for call_id in ids:
                try:self.cancel(turn_id,call_id)
                except Exception:failures.append(call_id)
            return failures

    @contextmanager
    def watch_cancellation(self,turn_id):
        done=threading.Event()
        failures=[]
        def watch():
            while not done.wait(.1):
                if self.stop is not None and self.stop.is_set():
                    thread.handled=True
                    try:failures.extend(self.cancel_turn(turn_id))
                    except Exception:failures.append('unconfirmed')
                    return
        thread=threading.Thread(target=watch,daemon=True);thread.start()
        try:
            yield
        finally:
            # Catch a stop coincident with completion, even if the watcher
            # has not awakened yet. The lock prevents a duplicate cancellation.
            done.set();thread.join(timeout=20)
            if self.stop is not None and self.stop.is_set():
                # Cancellation is idempotent at the coordinator, but avoid
                # duplicate requests by recording the watcher's result.
                if not getattr(thread,'handled',False):
                    try:failures.extend(self.cancel_turn(turn_id))
                    except Exception:failures.append('unconfirmed')
            if thread.is_alive() or failures:raise UncertainAction('Cancellation could not be confirmed; check the existing job status.')
