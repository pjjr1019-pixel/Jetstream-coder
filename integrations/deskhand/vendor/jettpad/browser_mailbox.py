"""Local-only durable mailbox for the Muse browser relay. No network or credentials."""
from __future__ import annotations
import sqlite3
import json
import os
import threading
import time
import uuid
from pathlib import Path

STATUS = {"ready": "ready", "draft": "waiting for your Muse draft",
          "busy": "waiting for Muse to finish", "needs_pair": "connect the Muse tab",
          "wrong_chat": "chat changed; reconnect the Muse tab", "layout": "Muse layout needs checking",
          "offline": "open Muse and connect its extension", "uncertain": "check the last message in Muse"}


class BrowserMailbox:
    def __init__(self, data):
        self.folder=Path(data)
        Path(data).mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(Path(data) / "browser-relay.sqlite3", check_same_thread=False, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self._last_heartbeat = None
        self.db.executescript("""
            -- Several app connections write this mailbox. Keep rollback mode
            -- on the installed pre-WAL-reset-fix SQLite runtime.
            PRAGMA journal_mode=DELETE;
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, ts REAL NOT NULL, text TEXT NOT NULL,
                reply_id TEXT UNIQUE NOT NULL,
                state TEXT NOT NULL, attempt TEXT, destination TEXT,
                updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS status (id INTEGER PRIMARY KEY CHECK(id=1), code TEXT, seen REAL);
            CREATE TABLE IF NOT EXISTS timing_events (
                job_id TEXT NOT NULL, event TEXT NOT NULL, at REAL NOT NULL,
                PRIMARY KEY(job_id,event));
            CREATE TABLE IF NOT EXISTS relay_control (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS local_claims (id TEXT PRIMARY KEY, epoch INTEGER, state TEXT);
            CREATE TABLE IF NOT EXISTS https_replies (reply_id TEXT PRIMARY KEY, text TEXT NOT NULL, created REAL NOT NULL);
        """)
        for name,kind in [('claimed_by','TEXT'),('claim_epoch','INTEGER'),('held_epoch','INTEGER')]:
            if name not in {r[1] for r in self.db.execute('PRAGMA table_info(jobs)')}:
                self.db.execute('ALTER TABLE jobs ADD COLUMN '+name+' '+kind)
        self.db.commit()

    def close(self):
        self.db.close()

    def headless_selected(self):
        try: return json.loads((self.folder/'browser-mode.json').read_text())!={'mode':'legacy'}
        except FileNotFoundError: return True  # The approved private browser is the default, never the retired extension.
        except (OSError,ValueError): return True  # Never regain visible-browser delivery on a damaged mode file.

    def enqueue(self, row):
        identity = str(uuid.UUID(row["id"]))
        if not isinstance(row["text"], str) or not 1 <= len(row["text"].strip()) <= 24000:
            raise ValueError("Invalid browser message")
        with self.lock, self.db:
            old = self.db.execute("SELECT text FROM jobs WHERE id=?", (identity,)).fetchone()
            if old and old["text"] != row["text"]:
                raise ValueError("Message identity changed")
            self.db.execute("INSERT OR IGNORE INTO jobs(id,ts,text,reply_id,state,updated) VALUES (?,?,?,?,'queued',?)",
                            (identity, row["ts"], row["text"], str(uuid.uuid4()), time.time()))
        return self.get(identity)

    def get(self, identity):
        with self.lock:
            row = self.db.execute("SELECT * FROM jobs WHERE id=?", (identity,)).fetchone()
            return dict(row) if row else None

    def https_reply(self, reply_id):
        with self.lock:
            row = self.db.execute('SELECT text FROM https_replies WHERE reply_id=?', (reply_id,)).fetchone()
            return row['text'] if row else None

    def cancel_queued(self, identity):
        """Abandon a turn only if no browser has claimed or touched it."""
        with self.lock, self.db:
            return self.db.execute(
                "UPDATE jobs SET state='abandoned',updated=? WHERE id=? AND state='queued' AND claimed_by IS NULL",
                (time.time(), identity)).rowcount == 1

    def abandon_queued_after_restart(self):
        """Do not send old work after Deskhand's dispatcher has recovered it."""
        with self.lock, self.db:
            return self.db.execute(
                "UPDATE jobs SET state='abandoned',updated=? WHERE state='queued' AND claimed_by IS NULL",
                (time.time(),)).rowcount

    def active(self):
        with self.lock:
            row = self.db.execute("SELECT * FROM jobs WHERE held_epoch IS NULL AND state IN ('sending','sent','uncertain') "
                                  "ORDER BY ts,id LIMIT 1").fetchone()
            return dict(row) if row else None

    def retire_interrupted_after_restart(self):
        """Exclusive Deskhand owner only: retain unknown work without replay or blocking new jobs."""
        with self.lock, self.db:
            stamp = time.time()
            self.db.execute("INSERT OR IGNORE INTO timing_events SELECT id,'retired_unknown',? FROM jobs WHERE state IN ('sending','sent','uncertain')", (stamp,))
            return self.db.execute("UPDATE jobs SET state='retired_unknown',updated=? WHERE state IN ('sending','sent','uncertain')", (stamp,)).rowcount

    def retire_uncertain(self, identity, attempt):
        """The waiting owner ended this attempt without a confirmed result; never retry it."""
        with self.lock, self.db:
            stamp = time.time()
            changed = self.db.execute("UPDATE jobs SET state='retired_unknown',updated=? WHERE id=? AND attempt=? AND state='uncertain'", (stamp, identity, attempt)).rowcount
            if changed:
                self.db.execute("INSERT OR IGNORE INTO timing_events VALUES (?,'retired_unknown',?)", (identity, stamp))
            return changed == 1

    def set_offline_hold(self, enabled):
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO relay_control VALUES ('offline_hold',?)", ('1' if enabled else '0',))

    def floor_state(self):
        with self.lock:
            values = {r[0]:r[1] for r in self.db.execute('SELECT key,value FROM relay_control')}
            return {'epoch':int(values.get('floor_epoch','0')), 'mode':values.get('floor_mode','cloud')}

    def advance_floor(self, local, handoff_epoch=None):
        with self.lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                current = self.floor_state()
                if current['mode']==('local' if local else 'cloud'):
                    self.db.commit();return current['epoch']
                epoch = current['epoch']+1
                if not local and handoff_epoch != epoch:
                    raise ValueError('Cloud floor requires the durable handoff for the next epoch')
                self.db.executemany('INSERT OR REPLACE INTO relay_control VALUES (?,?)',
                    [('floor_epoch',str(epoch)),('floor_mode','local' if local else 'cloud'),
                     ('offline_hold','1' if local else '0')])
                if local:
                    self.db.execute("UPDATE jobs SET state='uncertain',held_epoch=? WHERE state IN ('sending','sent','uncertain') AND held_epoch IS NULL",(epoch,))
                if not local:self.db.execute("INSERT OR REPLACE INTO relay_control VALUES ('handoff_epoch',?)",(str(epoch),))
                self.db.commit();return epoch
            except BaseException:
                self.db.rollback();raise

    def send_guarded(self, job, call):
        """Serialize send authorization and browser submission against floor changes."""
        with self.lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                current=self.floor_state();row=self.get(job['id'])
                if (self.offline_held() or current['mode']!='cloud' or current['epoch']!=job['claim_epoch']
                        or not row or row['attempt']!=job['attempt'] or row['state']!='sending'):
                    self.db.rollback();return {'state':'blocked'}
                if os.environ.get('DESKHAND_JETT_RUNTIME'):
                    from musebridge.host_stop import ensure_host_allows
                    try:
                        ensure_host_allows()
                    except ValueError:
                        self.db.rollback();return {'state':'blocked'}
                result=call({'op':'send','job':job})
                self.db.commit();return result
            except BaseException:
                self.db.rollback();raise

    def claim_local(self, identity):
        with self.lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                current=self.floor_state()
                if current['mode']!='local' or not self.offline_held():
                    self.db.rollback();return None
                self.db.execute('INSERT OR IGNORE INTO local_claims VALUES (?,?,?)',(identity,current['epoch'],'running'))
                row=self.db.execute('SELECT epoch,state FROM local_claims WHERE id=?',(identity,)).fetchone()
                self.db.commit()
                return current['epoch'] if row[0]==current['epoch'] and row[1]=='running' else None
            except BaseException:
                self.db.rollback();raise

    def local_result_allowed(self, identity, epoch):
        with self.lock:
            current=self.floor_state()
            row=self.db.execute('SELECT epoch,state FROM local_claims WHERE id=?',(identity,)).fetchone()
            return bool(row and row[0]==epoch and row[1]=='running' and current=={'epoch':epoch,'mode':'local'})

    def finish_local(self, identity):
        with self.lock,self.db:
            self.db.execute("UPDATE local_claims SET state='done' WHERE id=?",(identity,))

    def local_handoff_claims(self):
        with self.lock:
            epoch=self.floor_state()['epoch']
            return [dict(row) for row in self.db.execute('SELECT id,state FROM local_claims WHERE epoch=? ORDER BY id',(epoch,))]

    def held_cloud_ids(self):
        with self.lock:
            return [r[0] for r in self.db.execute('SELECT id FROM jobs WHERE held_epoch=? ORDER BY ts',(self.floor_state()['epoch'],))]

    def interrupt_local(self, identity):
        with self.lock,self.db:
            self.db.execute("UPDATE local_claims SET state='interrupted' WHERE id=?",(identity,))

    def offline_held(self):
        with self.lock:
            row = self.db.execute("SELECT value FROM relay_control WHERE key='offline_hold'").fetchone()
            return bool(row and row[0]=='1')

    def claim(self, destination):
        destination = str(uuid.UUID(destination))
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                if self.offline_held() or self.active():
                    self.db.commit()
                    return None
                row = self.db.execute("SELECT * FROM jobs WHERE state='queued' ORDER BY ts,id LIMIT 1").fetchone()
                if row:
                    self.db.execute("UPDATE jobs SET state='sending',attempt=?,destination=?,updated=?,claimed_by='browser',claim_epoch=? WHERE id=? AND state='queued' AND claimed_by IS NULL",
                                    (str(uuid.uuid4()), destination, time.time(), self.floor_state()['epoch'],row["id"]))
                    self.db.execute("INSERT OR IGNORE INTO timing_events VALUES (?,?,?)",
                                    (row['id'], 'claimed', time.time()))
                self.db.commit()
                return self.get(row["id"]) if row else None
            except BaseException:
                self.db.rollback()
                raise

    def transition(self, identity, attempt, event):
        if event not in ("sent", "blocked", "uncertain"):
            raise ValueError("Invalid relay event")
        with self.lock, self.db:
            row = self.get(identity)
            if not row or row["attempt"] != attempt or row["state"] in ("complete", "abandoned", "retired_unknown"):
                return False
            if event == "blocked":
                # Only a browser's explicit pre-edit rejection permits retry.
                if row["state"] != "sending":
                    return False
                self.db.execute("UPDATE jobs SET state='queued',attempt=NULL,destination=NULL,claimed_by=NULL,claim_epoch=NULL,updated=? WHERE id=?",
                                (time.time(), identity))
            else:
                if event == "sent" and row["state"] not in ("sending", "sent"):
                    return False
                self.db.execute("UPDATE jobs SET state=?,updated=? WHERE id=?", (event, time.time(), identity))
            self.db.execute("INSERT OR IGNORE INTO timing_events VALUES (?,?,?)", (identity,event,time.time()))
            return True

    def complete(self, identity, attempt):
        """Called only after a matching receipt is saved in Pad history."""
        with self.lock, self.db:
            row = self.get(identity)
            if not row or row['attempt'] != attempt or row['state'] not in ('sending','sent','uncertain'):
                return False
            stamp = time.time()
            self.db.execute("UPDATE jobs SET state='complete',updated=? WHERE id=?",(stamp,identity))
            self.db.execute('INSERT OR IGNORE INTO timing_events VALUES (?,?,?)',(identity,'complete',stamp))
            return True

    def abandon_uncertain(self):
        with self.lock, self.db:
            self.db.execute("UPDATE jobs SET state='abandoned',updated=? WHERE state='uncertain'", (time.time(),))

    def heartbeat(self, code):
        if code not in STATUS:
            raise ValueError("Invalid browser status")
        with self.lock, self.db:
            now = time.monotonic()
            if self._last_heartbeat is None or self._last_heartbeat[0] != code or now-self._last_heartbeat[1] >= .5:
                self.db.execute("INSERT OR REPLACE INTO status VALUES (1,?,?)", (code, time.time()))
                self._last_heartbeat = (code, now)
            # Store only the first occurrence of each wait reason for each queued
            # request. No message content, browsing history or unbounded heartbeat log.
            if code != 'ready':
                self.db.execute("INSERT OR IGNORE INTO timing_events SELECT id,?,? FROM jobs WHERE state='queued'",
                                ('wait_'+code,time.time()))

    def status(self):
        with self.lock:
            row = self.db.execute("SELECT * FROM status WHERE id=1").fetchone()
            return STATUS[row["code"]] if row and time.time() - row["seen"] < 12 else STATUS["offline"]

    def host_alive(self):
        with self.lock:
            row=self.db.execute('SELECT seen FROM status WHERE id=1').fetchone()
            return bool(row and time.time()-row['seen']<12)

    def queued_count(self):
        with self.lock:
            return self.db.execute("SELECT count(*) FROM jobs WHERE state='queued'").fetchone()[0]
