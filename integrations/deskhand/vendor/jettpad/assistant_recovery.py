"""Read recorded executor jobs without replaying an interrupted assistant turn."""
from pathlib import Path
from contextlib import closing
import sqlite3

def describe_turn(data,turn_id,broker_factory=None):
    from action_status import query_turn,format_status
    rows=query_turn(data,turn_id,broker_factory)
    return format_status(rows) or None

def publish_interrupted(store,data,turn_ids):
    import time
    for turn_id in turn_ids:
        try:
            from action_status import query_turn,format_status,publish
            rows=query_turn(data,turn_id);summary=format_status(rows) or None
            if rows:publish(store,turn_id,rows)
        except Exception:summary='PC task status could not be checked. The action was not resent; check it before trying again.'
        if not summary:continue
        with store.lock,store.db:
            store.db.execute("INSERT OR IGNORE INTO messages(id,ts,sender,text,status,logged) VALUES (?,?,'system',?,'complete',0)",('assistant-recovery:'+turn_id,time.time(),summary))
        store.queue_changed.set()
