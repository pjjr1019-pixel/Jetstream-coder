"""One workflow: browser prompts, correlated file answers and durable local history."""
from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from local_images import image_prompt

POLL_SECONDS = .1
MAX_LINE = 1024 * 1024


def normalize(row):
    if not isinstance(row, dict) or row.get("from") not in ("jett", "phil", "offline", "system"):
        raise ValueError("Invalid message author")
    ts, text = row.get("ts"), row.get("text")
    if isinstance(ts, bool) or not isinstance(ts, (int, float)) or not math.isfinite(ts):
        raise ValueError("Invalid timestamp")
    if not isinstance(text, str) or not text.strip() or len(text) > 240000:
        raise ValueError("Invalid message text")
    identity = row.get("id")
    if identity is not None and (not isinstance(identity, str) or not 1 <= len(identity) <= 160):
        raise ValueError("Invalid message identity")
    if not identity:
        content = json.dumps([ts, row["from"], text], ensure_ascii=True, separators=(",", ":"))
        identity = "file:" + hashlib.sha256(content.encode()).hexdigest()
    return {"id": identity, "ts": ts, "from": row["from"], "text": text}


def append_line(path, row):
    """A single append, never truncating a user's history or an unfinished line."""
    encoded = (json.dumps(row, ensure_ascii=True, separators=(",", ":")) + "\n").encode()
    with Path(path).open("a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell():
            stream.seek(-1, os.SEEK_END)
            if stream.read(1) != b"\n":
                raise BlockingIOError("Waiting for a complete JSONL line")
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())


class PadStore:
    def __init__(self, folder):
        Path(folder).mkdir(parents=True, exist_ok=True)
        self.folder=Path(folder)
        self.lock = threading.RLock()
        self.routing_lock = threading.RLock()
        self.route_provider = lambda: 'browser'
        self.queue_changed = threading.Event()
        self.db = sqlite3.connect(Path(folder) / "pad-state.sqlite3", check_same_thread=False)
        # This source has several writers. The installed SQLite 3.49.1 predates
        # the WAL-reset corruption fix; use durable rollback journaling here.
        # Search keeps its separate, rebuildable, single-writer cache.
        self.db.execute("PRAGMA journal_mode=DELETE")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY, ts REAL, sender TEXT, text TEXT,
                status TEXT, logged INTEGER NOT NULL DEFAULT 0);
            -- rowid breaks timestamp ties implicitly, in both scan directions.
            CREATE INDEX IF NOT EXISTS messages_visible_ts
                ON messages(ts) WHERE status!='deferred_cloud';
            CREATE TABLE IF NOT EXISTS outbox (
                id TEXT PRIMARY KEY, state TEXT, bridge_id TEXT, replies_logged INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS render_receipts (
                id TEXT, status TEXT, first_at REAL, last_at REAL, chars INTEGER,
                updates INTEGER, PRIMARY KEY(id,status));
        """)
        if "requested_model" not in {r[1] for r in self.db.execute("PRAGMA table_info(outbox)")}:
            self.db.execute("ALTER TABLE outbox ADD COLUMN requested_model TEXT")
        if "codex_model" not in {r[1] for r in self.db.execute("PRAGMA table_info(outbox)")}:
            self.db.execute("ALTER TABLE outbox ADD COLUMN codex_model TEXT")
        if "codex_effort" not in {r[1] for r in self.db.execute("PRAGMA table_info(outbox)")}:
            self.db.execute("ALTER TABLE outbox ADD COLUMN codex_effort TEXT")
        if "retry_of" not in {r[1] for r in self.db.execute("PRAGMA table_info(outbox)")}:
            self.db.execute("ALTER TABLE outbox ADD COLUMN retry_of TEXT")
        if "route" not in {r[1] for r in self.db.execute("PRAGMA table_info(outbox)")}:
            self.db.execute("ALTER TABLE outbox ADD COLUMN route TEXT NOT NULL DEFAULT 'bridge'")
        # Triggers also cover direct SQL writers (offline routing/recovery).
        # One coalesced revision per identity, not an unbounded event journal.
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS chat_revision (singleton INTEGER PRIMARY KEY CHECK(singleton=1), revision INTEGER NOT NULL);
            INSERT OR IGNORE INTO chat_revision VALUES (1,0);
            CREATE TABLE IF NOT EXISTS chat_changes (id TEXT PRIMARY KEY, revision INTEGER NOT NULL);
            CREATE INDEX IF NOT EXISTS chat_changes_revision ON chat_changes(revision);
        """)
        for table, events in (
                ('messages', [('INSERT',''), ('DELETE',''),
                    ('UPDATE OF ts,sender,text,status', 'WHEN OLD.ts IS NOT NEW.ts OR OLD.sender IS NOT NEW.sender OR OLD.text IS NOT NEW.text OR OLD.status IS NOT NEW.status')]),
                ('outbox', [('INSERT',''), ('DELETE',''),
                    ('UPDATE OF state', 'WHEN OLD.state IS NOT NEW.state')])):
            for event, condition in events:
                name = table+'_'+event.split()[0].lower()
                identity = 'OLD.id' if event == 'DELETE' else 'NEW.id'
                self.db.executescript(f"""
                    CREATE TRIGGER IF NOT EXISTS chat_{name} AFTER {event} ON {table} {condition}
                    BEGIN
                        UPDATE chat_revision SET revision=revision+1 WHERE singleton=1;
                        INSERT OR REPLACE INTO chat_changes(id,revision)
                            SELECT {identity},revision FROM chat_revision WHERE singleton=1;
                    END;
                """)
        from audit_log import initialize as initialize_audit
        initialize_audit(self.db)
        from workspace_store import initialize
        initialize(self.db)
        self.db.execute("INSERT OR IGNORE INTO settings VALUES ('workspace_uuid',?)",(str(uuid.uuid4()),))
        # An HTTP send interrupted by a crash has an unknown outcome; never replay it.
        self.db.execute("UPDATE outbox SET state='uncertain' WHERE state='sending'")
        self.db.commit()

    def put(self, row, status="complete", logged=False):
        row = normalize(row)
        with self.lock, self.db:
            old = self.db.execute("SELECT * FROM messages WHERE id=?", (row["id"],)).fetchone()
            if old:
                if logged and not old["logged"]:
                    self.db.execute("UPDATE messages SET logged=1 WHERE id=?", (row["id"],))
                return False
            self.db.execute("INSERT INTO messages VALUES (?,?,?,?,?,?)",
                            (row["id"], row["ts"], row["from"], row["text"], status, int(logged)))
            return True

    def submit(self, text):
        text = text.strip()
        if not text or len(text) > 24000:
            raise ValueError("Enter a message of 1 to 24,000 characters.")
        row = {"id": str(uuid.uuid4()), "ts": time.time(), "from": "phil", "text": text}
        with self.routing_lock, self.lock, self.db:
            self.db.execute("INSERT INTO messages VALUES (?,?,?,?,?,0)",
                            (row["id"], row["ts"], row["from"], row["text"], "complete"))
            self.db.execute("INSERT INTO outbox(id,state,route) VALUES (?,'queued',?)", (row["id"], "image" if image_prompt(text) else self.route_provider()))
        self.queue_changed.set()
        return row["id"]

    def submit_once(self, identifier, text, model=None, codex_model=None, codex_effort=None):
        if str(uuid.UUID(identifier)) != identifier: raise ValueError('Invalid message ID')
        text=text.strip()
        if not 1 <= len(text) <= 24000: raise ValueError('Enter 1 to 24000 characters')
        from selected_models import catalog
        if model in (None,'auto'):model=None
        elif model in ('muse','codex'):pass
        elif isinstance(model,str) and model.startswith('codex:'):
            from codex_accounts import list_accounts
            if not any(x['id']==model[6:] for x in list_accounts()):raise ValueError('Codex account is unavailable')
        elif model=='image:SSD-1B':
            if not (Path(__file__).resolve().parent/'image-model/model_index.json').is_file():raise ValueError('Local image model is unavailable')
        elif not isinstance(model,str) or not any(r['name']==model and r['chat'] for r in catalog()):raise ValueError('Selected model is not available for chat')
        if codex_model is not None:
            import re
            if not isinstance(codex_model,str) or not re.fullmatch(r'[A-Za-z0-9_.:/-]{1,120}',codex_model) or not (model=='codex' or isinstance(model,str) and model.startswith('codex:')):raise ValueError('Invalid Codex model')
        if codex_effort is not None:
            import re
            if not codex_model or not isinstance(codex_effort,str) or not re.fullmatch(r'[a-z]{1,16}',codex_effort):raise ValueError('Invalid Codex effort')
        with self.routing_lock, self.lock, self.db:
            old=self.db.execute('SELECT m.sender,m.text,o.codex_effort,o.codex_model,o.requested_model FROM messages m LEFT JOIN outbox o ON o.id=m.id WHERE m.id=?',(identifier,)).fetchone()
            if old:
                if old['sender']!='phil' or old['text']!=text or (old['requested_model']!=model or old['codex_model']!=codex_model or old['codex_effort']!=codex_effort): raise ValueError('Message ID reused with different content or model')
                return identifier
            self.db.execute('INSERT INTO messages VALUES (?,?,?,?,?,0)',(identifier,time.time(),'phil',text,'complete'))
            route='image' if model=='image:SSD-1B' or (model is None and image_prompt(text)) else ('browser' if model=='muse' else 'codex' if model=='codex' or (isinstance(model,str) and model.startswith('codex:')) else 'selected_local' if model else self.route_provider())
            self.db.execute("INSERT INTO outbox(id,state,route,requested_model,codex_model,codex_effort) VALUES (?,'queued',?,?,?,?)",(identifier,route,model,codex_model,codex_effort))
        self.queue_changed.set()
        return identifier

    def latest_local_failure(self):
        with self.lock:
            row=self.db.execute("SELECT m.id,m.text,o.route,o.requested_model FROM messages m JOIN outbox o ON o.id=m.id WHERE (o.route='selected_local' AND o.state='selected_failed') OR (o.route='offline' AND o.state='offline_failed') ORDER BY m.ts DESC,m.rowid DESC LIMIT 1").fetchone()
            return dict(row) if row else None

    def retry_local_failure(self,identity,offline_active=False):
        # Check the durable job ledger before creating a new message identity.
        # Any recorded action may already have run, even when the reply failed.
        from action_status import query_turn,format_status
        try:actions=query_turn(self.folder,identity)
        except Exception:raise ValueError('Could not check existing PC jobs. Review their status before retrying.') from None
        if actions:raise ValueError('This message has existing PC jobs. Review their status before requesting another action. '+format_status(actions))
        with self.routing_lock,self.lock,self.db:
            row=self.db.execute("SELECT m.text,o.route,o.state,o.requested_model FROM messages m JOIN outbox o ON o.id=m.id WHERE m.id=?",(identity,)).fetchone()
            if not row or (row['route'],row['state']) not in (('selected_local','selected_failed'),('offline','offline_failed')):raise ValueError('This request is no longer eligible for local retry.')
            prior=self.db.execute('SELECT id,state FROM outbox WHERE retry_of=?',(identity,)).fetchone()
            if prior:
                if prior['state'] in ('selected_complete','offline_complete'):raise ValueError('This request was already retried; review the existing reply.')
                return prior['id']
            if row['route']=='offline' and not offline_active:raise ValueError('Switch to Offline mode first to retry with Offline Jett.')
            if row['route']=='offline':
                from musebridge.local_actions import parse_launch
                from musebridge.local_reads import parse_read
                from offline_model import ACTION
                if parse_launch(row['text']) is not None or parse_read(row['text']) is not None or ACTION.search(row['text']):raise ValueError('Review this PC action before trying again; it cannot be retried automatically.')
            if row['route']=='selected_local':
                from selected_models import catalog
                if not any(x['name']==row['requested_model'] and x['chat'] for x in catalog()):raise ValueError('The original local model is not installed.')
            new=str(uuid.uuid4());self.db.execute('INSERT INTO messages VALUES (?,?,?,?,?,0)',(new,time.time(),'phil',row['text'],'complete'))
            self.db.execute("INSERT INTO outbox(id,state,route,requested_model,retry_of) VALUES (?,'queued',?,?,?)",(new,row['route'],row['requested_model'],identity))
        self.queue_changed.set();return new

    def import_history(self, entries):
        """Restore one journal transaction; preserve immutable IDs and logged flags."""
        def values():
            for row,logged in entries:
                row=normalize(row)
                yield row['id'],row['ts'],row['from'],row['text'],'complete',int(logged)
        with self.lock,self.db:
            self.db.executemany("INSERT INTO messages VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET logged=excluded.logged WHERE messages.logged<excluded.logged",values())

    def recent_messages(self, limit=200):
        if type(limit) is not int or not 1<=limit<=5000:raise ValueError('Invalid message window')
        from workspace_store import PROJECTION
        with self.lock:
            return [dict(row) for row in self.db.execute(PROJECTION+"WHERE m.status!='deferred_cloud' ORDER BY m.ts DESC,m.rowid DESC LIMIT ?",(limit,))][::-1]

    def message_window(self, since=None, limit=200):
        with self.lock,self.db:
            if not self.db.in_transaction:self.db.execute('BEGIN')
            revision=self.db.execute('SELECT revision FROM chat_revision WHERE singleton=1').fetchone()[0]
            if revision==since:return revision,None
            return revision,{row['id']:row for row in self.recent_messages(limit)}

    def get_message(self, identity):
        from workspace_store import PROJECTION
        with self.lock:
            row=self.db.execute(PROJECTION+"WHERE m.id=? AND m.status!='deferred_cloud'",(identity,)).fetchone()
            return dict(row) if row else None

    def live_state(self):
        with self.lock:
            counts={r[0]:r[1] for r in self.db.execute("SELECT state,COUNT(*) FROM outbox WHERE state IN ('queued','browser_queued','browser_sending','uncertain','offline_running','offline_failed','offline_interrupted','image_running','image_failed','image_interrupted','selected_running','selected_failed','selected_interrupted','codex_running','codex_failed','codex_interrupted') GROUP BY state")}
        return {'waiting':sum(counts.get(k,0) for k in ('queued','browser_queued','browser_sending')),
                'active_local':counts.get('offline_running',0)+counts.get('image_running',0)+counts.get('selected_running',0)+counts.get('codex_running',0),
                'attention':sum(counts.get(k,0) for k in ('uncertain','offline_failed','offline_interrupted','image_failed','image_interrupted','selected_failed','selected_interrupted','codex_failed','codex_interrupted'))}

    def messages(self):
        with self.lock:
            return [dict(r) for r in self.db.execute("SELECT m.*,o.state AS delivery FROM messages m "
                    "LEFT JOIN outbox o ON m.id=o.id WHERE m.status!='deferred_cloud' ORDER BY m.ts,m.rowid")]

    def message_updates(self, since=None):
        """Atomic snapshot/delta; no message scan on an unchanged UI tick.

        Hidden/deleted identities map to None. ts/rowid preserve journal order.
        Logged flags, drafts and render receipts do not advance this revision.
        """
        with self.lock, self.db:
            # Explicit read transaction makes external-connection writes atomic
            # with the revision, including while fetching multiple delta rows.
            if not self.db.in_transaction:
                self.db.execute('BEGIN')
            revision = self.db.execute('SELECT revision FROM chat_revision WHERE singleton=1').fetchone()[0]
            if since == revision:
                return revision, {}
            projection = "SELECT m.*,m.rowid AS position,o.state AS delivery FROM messages m LEFT JOIN outbox o ON m.id=o.id "
            if since is None or since > revision:
                rows = self.db.execute(projection+"WHERE m.status!='deferred_cloud' ORDER BY m.ts,m.rowid")
                return revision, {r['id']: dict(r) for r in rows}
            changes = {}
            for identity, in self.db.execute('SELECT id FROM chat_changes WHERE revision>?', (since,)).fetchall():
                row = self.db.execute(projection+"WHERE m.id=? AND m.status!='deferred_cloud'", (identity,)).fetchone()
                changes[identity] = dict(row) if row else None
            return revision, changes

    def delivery(self, identity, state):
        with self.lock, self.db:
            self.db.execute("UPDATE outbox SET state=? WHERE id=?", (state, identity))

    def draft(self, value=None):
        with self.lock:
            if value is not None:
                with self.db:
                    self.db.execute("INSERT OR REPLACE INTO settings VALUES ('draft',?)", (value,))
            row = self.db.execute("SELECT value FROM settings WHERE key='draft'").fetchone()
            return row[0] if row else ""

    def record_renders(self, changes, rendered_at):
        """Local timing metadata only; no extra message bodies or API state."""
        with self.lock, self.db:
            self.db.executemany("INSERT INTO render_receipts VALUES (?,?,?,?,?,1) "
                "ON CONFLICT(id,status) DO UPDATE SET last_at=excluded.last_at,"
                "chars=excluded.chars,updates=render_receipts.updates+1",
                [(identity, status, rendered_at, rendered_at, chars)
                 for identity, status, chars in changes])


def read_history(path):
    """Restore existing complete journal entries once; never watch chat feeds."""
    with path.open('rb') as stream:
        while line := stream.readline(MAX_LINE + 1):
            if len(line) > MAX_LINE:
                while line and not line.endswith(b'\n'):
                    line = stream.readline(MAX_LINE + 1)
                continue
            if not line.endswith(b'\n'):
                break
            try:
                yield normalize(json.loads(line.decode('utf-8-sig')))
            except (ValueError, UnicodeError, RecursionError):
                continue


class PadEngine:
    def __init__(self, documents, data, *, offline_enabled=True):
        from browser_mailbox import BrowserMailbox
        self.documents, self.data = Path(documents), Path(data)
        self.documents.mkdir(parents=True, exist_ok=True)
        self.inbox = self.documents / 'jett-pad-inbox'
        self.inbox.mkdir(exist_ok=True)
        self.feed = self.documents / 'jett-pad-feed.jsonl'
        self.replies = self.documents / 'jett-pad-replies.jsonl'
        self.feed.touch(exist_ok=True)
        self.replies.touch(exist_ok=True)
        self.store, self.mailbox = PadStore(data), BrowserMailbox(data)
        self.routing_lock = self.store.routing_lock
        self.feed_ids, self.reply_ids = set(), set()
        # Existing SQLite rows and journals remain readable. Old route metadata
        # is preserved, but only new browser jobs may ever be sent.
        for path, identities in ((self.feed,self.feed_ids),(self.replies,self.reply_ids)):
            def entries():
                for row in read_history(path):
                    identities.add(row['id'])
                    yield row,row['id'] in self.feed_ids
            self.store.import_history(entries())
        self.inbox_seen = {}
        self.file_error = False
        self.browser_status = 'connecting to Muse'
        self.browser_controller = None
        self.offline = None
        if offline_enabled:
            from offline_mode import OfflineCoordinator
            self.offline = OfflineCoordinator(self)
            self.store.route_provider = lambda: 'offline' if self.offline.floor.offline or self.offline.manual else 'browser'

    def poll_files(self):
        from companion_data import consume_chat
        consume_chat(self.store)
        for path in self.inbox.glob('*.json'):
            try:
                identity = str(uuid.UUID(path.stem))
                if path.stem != identity or path.is_symlink() or path.resolve().parent != self.inbox.resolve():
                    continue
                stat = path.stat()
                signature = (stat.st_mtime_ns,stat.st_size)
                if stat.st_nlink != 1 or stat.st_size > MAX_LINE or self.inbox_seen.get(identity) == signature:
                    continue
                # Only files for a prompt actually sent from this Pad are replies.
                with self.mailbox.lock:
                    job = self.mailbox.db.execute('SELECT id,state FROM jobs WHERE reply_id=?',(identity,)).fetchone()
                if not job or job['state'] not in ('sending','sent','uncertain','complete','abandoned'):
                    continue
                row = normalize(json.loads(path.read_text(encoding='utf-8-sig')))
                if row['from'] != 'jett' or row['id'] != identity:
                    continue
                with self.store.lock,self.store.db:
                    from audit_log import link_reply
                    link_reply(self.store.db,job['id'],identity)
                with self.routing_lock:
                    status = 'deferred_cloud' if self.offline and self.offline.floor.offline else 'complete'
                    if self.store.put(row,status=status): self.store.queue_changed.set()
                self.inbox_seen[identity] = signature
            except (OSError,ValueError,UnicodeError,RecursionError):
                continue
        with self.store.lock:
            outgoing = list(self.store.db.execute('SELECT m.* FROM messages m JOIN outbox o ON m.id=o.id WHERE o.replies_logged=0'))
        for entry in outgoing:
            row = {'id':entry['id'],'ts':entry['ts'],'from':'phil','text':entry['text']}
            if row['id'] not in self.reply_ids:
                append_line(self.replies,row)
                self.reply_ids.add(row['id'])
            with self.store.lock,self.store.db:
                self.store.db.execute('UPDATE outbox SET replies_logged=1 WHERE id=?',(row['id'],))
        with self.store.lock:
            unlogged = list(self.store.db.execute("SELECT * FROM messages WHERE logged=0 AND status='complete'"))
        for entry in unlogged:
            row = {'id':entry['id'],'ts':entry['ts'],'from':entry['sender'],'text':entry['text']}
            if row['id'] not in self.feed_ids:
                append_line(self.feed,row)
                self.feed_ids.add(row['id'])
            with self.store.lock,self.store.db:
                self.store.db.execute('UPDATE messages SET logged=1 WHERE id=?',(row['id'],))

    def close(self):
        if self.offline: self.offline.close()
        self.mailbox.close()
        self.store.db.close()


class PadWorkers:
    def __init__(self, engine, browser_launcher=None, browser_controller=None):
        self.engine, self.browser_launcher = engine, browser_launcher
        self.browser_controller=browser_controller
        engine.browser_controller = browser_controller
        self.stop = threading.Event()
        self.threads = []

    def start(self):
        if self.engine.offline: self.engine.offline.start()
        from local_images import ImageWorker
        self.image_worker = ImageWorker(self.engine, self.stop)
        from codex_model import CodexWorker
        self.codex_worker=CodexWorker(self.engine,self.stop)
        from selected_models import SelectedModelWorker
        self.selected_worker=SelectedModelWorker(self.engine,self.stop)
        for function in (self.files,self.browser,self.image_worker.run,self.selected_worker.run,self.codex_worker.run):
            thread = threading.Thread(target=function,daemon=True,name='jett-pad-'+function.__name__)
            thread.start()
            self.threads.append(thread)

    def close(self):
        if self.engine.offline: self.engine.offline.close()
        self.stop.set()
        self.engine.store.queue_changed.set()
        for thread in self.threads:
            thread.join(3)

    def files(self):
        while not self.stop.is_set():
            try:
                self.engine.poll_files()
                self.engine.file_error = False
            except (OSError,ValueError,sqlite3.Error):
                self.engine.file_error = True
            self.stop.wait(POLL_SECONDS)

    def browser_once(self, observed_messages):
        mailbox, store = self.engine.mailbox, self.engine.store
        with self.engine.routing_lock:
            with store.lock:
                rows = list(store.db.execute("SELECT m.* FROM messages m JOIN outbox o ON m.id=o.id "
                    "WHERE o.route IN ('browser','cloud_notice') AND o.state NOT IN ('browser_complete','browser_abandoned') ORDER BY m.ts"))
            for row in rows:
                job = mailbox.enqueue(dict(row))
                if job['state']=='local_only':
                    with store.lock,store.db:
                        store.db.execute("UPDATE outbox SET route='offline',state='queued' WHERE id=?",(row['id'],))
                    continue
                with store.lock:
                    answer = store.db.execute("SELECT 1 FROM messages WHERE id=? AND sender='jett' AND status='complete'",(job['reply_id'],)).fetchone()
                if answer and job['attempt'] and job['state'] in ('sending','sent','uncertain'):
                    mailbox.complete(job['id'],job['attempt']);job = mailbox.get(job['id'])
                state = {'queued':'browser_queued','sending':'browser_sending','sent':'browser_sending',
                         'uncertain':'uncertain','complete':'browser_complete','abandoned':'browser_abandoned',
                         'retired_unknown':'browser_abandoned'}.get(job['state'],'browser_abandoned')
                store.delivery(row['id'],state)
            if self.browser_launcher and not mailbox.offline_held():
                fresh = any(row['id'] not in observed_messages for row in rows)
                observed_messages.update(row['id'] for row in rows)
                self.browser_launcher.ensure(connected=mailbox.host_alive(),pending=mailbox.queued_count()>0,new_message=fresh)
            self.engine.browser_status = self.browser_controller.status() if self.browser_controller else mailbox.status()
            from companion_data import atomic
            atomic(self.engine.data/'browser-status.json',{'ts':time.time(),'status':self.engine.browser_status})

    def browser(self):
        observed_messages = set()
        while not self.stop.is_set():
            self.engine.store.queue_changed.clear()
            try:
                self.browser_once(observed_messages)
            except (OSError,ValueError,sqlite3.Error,KeyError):
                self.engine.browser_status = 'browser relay unavailable'
            self.engine.store.queue_changed.wait(.2)


class InstanceGuard:
    def __init__(self, folder):
        self.path = Path(folder) / "instance.lock"
        self.stream = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                if not self.path.stat().st_size:
                    self.stream.write(b"0")
                    self.stream.flush()
                self.stream.seek(0)
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.stream.close()
            return False
        return True

    def close(self):
        if self.stream:
            self.stream.close()
