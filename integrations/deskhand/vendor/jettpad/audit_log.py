"""Durable local event trail; records evidence, never judges task success."""


AUDIT_MAX_ROWS=10000  # newest rows kept; the insert trigger below enforces this bound.


def prune(db, keep=AUDIT_MAX_ROWS):
    """Drop audit rows older than the newest `keep` rows. The insert trigger
    enforces the same bound on every write; this is for explicit/manual use."""
    db.execute("DELETE FROM audit_events WHERE seq < (SELECT seq FROM audit_events ORDER BY seq DESC LIMIT 1 OFFSET ?)", (keep-1,))


def initialize(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS audit_events (
        seq INTEGER PRIMARY KEY AUTOINCREMENT, recorded_at REAL NOT NULL,
        kind TEXT NOT NULL, message_id TEXT NOT NULL, message_ts REAL,
        sender TEXT, content TEXT, message_status TEXT, delivery TEXT,
        route TEXT, selected_model TEXT, codex_model TEXT, effort TEXT);
    CREATE INDEX IF NOT EXISTS audit_events_message ON audit_events(message_id,seq);
    CREATE TABLE IF NOT EXISTS audit_reply_links (
        reply_id TEXT PRIMARY KEY, prompt_id TEXT NOT NULL, source TEXT NOT NULL);
    CREATE TRIGGER IF NOT EXISTS audit_messages_insert AFTER INSERT ON messages BEGIN
      INSERT INTO audit_events(recorded_at,kind,message_id,message_ts,sender,content,message_status)
      VALUES ((julianday('now')-2440587.5)*86400.0,'message',NEW.id,NEW.ts,NEW.sender,NEW.text,NEW.status);
    END;
    CREATE TRIGGER IF NOT EXISTS audit_messages_update AFTER UPDATE OF text,status ON messages
    WHEN OLD.text IS NOT NEW.text OR OLD.status IS NOT NEW.status BEGIN
      INSERT INTO audit_events(recorded_at,kind,message_id,message_ts,sender,content,message_status)
      VALUES ((julianday('now')-2440587.5)*86400.0,'message_update',NEW.id,NEW.ts,NEW.sender,NEW.text,NEW.status);
    END;
    CREATE TRIGGER IF NOT EXISTS audit_outbox_insert AFTER INSERT ON outbox BEGIN
      INSERT INTO audit_events(recorded_at,kind,message_id,delivery,route,selected_model,codex_model,effort)
      VALUES ((julianday('now')-2440587.5)*86400.0,'delivery',NEW.id,NEW.state,NEW.route,NEW.requested_model,NEW.codex_model,NEW.codex_effort);
    END;
    CREATE TRIGGER IF NOT EXISTS audit_outbox_update AFTER UPDATE OF state,route,requested_model,codex_model,codex_effort ON outbox
    WHEN OLD.state IS NOT NEW.state OR OLD.route IS NOT NEW.route OR OLD.requested_model IS NOT NEW.requested_model OR OLD.codex_model IS NOT NEW.codex_model OR OLD.codex_effort IS NOT NEW.codex_effort BEGIN
      INSERT INTO audit_events(recorded_at,kind,message_id,delivery,route,selected_model,codex_model,effort)
      VALUES ((julianday('now')-2440587.5)*86400.0,'delivery',NEW.id,NEW.state,NEW.route,NEW.requested_model,NEW.codex_model,NEW.codex_effort);
    END;
    CREATE TRIGGER IF NOT EXISTS audit_events_prune AFTER INSERT ON audit_events BEGIN
      DELETE FROM audit_events WHERE seq < (SELECT seq FROM audit_events ORDER BY seq DESC LIMIT 1 OFFSET 9999);
    END;
    ''')


def link_reply(db, prompt_id, reply_id):
    prior = db.execute('SELECT prompt_id FROM audit_reply_links WHERE reply_id=?', (reply_id,)).fetchone()
    if prior and prior[0] != prompt_id:
        raise ValueError('Reply belongs to a different request')
    db.execute("INSERT OR IGNORE INTO audit_reply_links VALUES (?,?, 'muse')", (reply_id, prompt_id))


def record_codex_model(db, turn_id, model):
    """Record the app-server resolved model, not a guessed account default."""
    import re
    if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,120}", model):
        raise ValueError("Invalid resolved model")
    db.execute("INSERT INTO audit_events(recorded_at,kind,message_id,codex_model) VALUES ((julianday('now')-2440587.5)*86400.0,'resolved_model',?,?)", (turn_id,model))


def record_failure(db, turn_id, category):
    allowed={'uncertain_action','stopped_or_cancelled','permission_refused','timeout','model_unavailable','service_busy','authentication','service_error','http_error','service_unavailable','insufficient_memory','unknown'}
    if category not in allowed:raise ValueError('Invalid failure category')
    db.execute("INSERT INTO audit_events(recorded_at,kind,message_id,content) VALUES ((julianday('now')-2440587.5)*86400.0,'failure',?,?)",(turn_id,category))


def record_capability(db,turn_id,summary):
    if not isinstance(summary,str) or len(summary)>2000:raise ValueError('Invalid capability summary')
    db.execute("INSERT INTO audit_events(recorded_at,kind,message_id,content) VALUES ((julianday('now')-2440587.5)*86400.0,'capability',?,?)",(turn_id,summary))
