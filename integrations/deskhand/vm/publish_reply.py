#!/usr/bin/env python3
"""Publish one complete reply from stdin without overwriting another writer."""
import json
import os
import sys
import tempfile
import uuid
from pathlib import Path


def publish(replies, payload):
    replies = Path(replies)
    turn_id = payload.get('turn_id') if isinstance(payload, dict) else None
    if not isinstance(turn_id, str) or not turn_id or Path(turn_id).name != turn_id or turn_id in ('.', '..'):
        raise ValueError('invalid turn id')
    if not any(isinstance(payload.get(k), str) and payload[k] for k in ('reply', 'error')):
        raise ValueError('missing reply or error')
    replies.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.' + turn_id + '.', suffix='.tmp', dir=replies)
    temp = Path(name)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        target = replies / (turn_id + '.json')
        try:
            os.link(temp, target)
        except FileExistsError:
            conflicts = replies.parent / 'replies-conflicts' / uuid.uuid4().hex
            conflicts.mkdir(parents=True)
            _sync(replies.parent)
            os.link(temp, conflicts / target.name)
            _sync(conflicts)
            _sync(conflicts.parent)
            outcome = 'conflict'
        else:
            _sync(replies)
            outcome = 'published'
        return outcome
    finally:
        temp.unlink(missing_ok=True)
        _sync(replies)


def _sync(path):
    if os.name == 'nt':
        return
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


if __name__ == '__main__':
    if len(sys.argv) != 2:
        sys.exit('usage: publish_reply.py REPLIES_DIR < reply.json')
    outcome = publish(sys.argv[1], json.load(sys.stdin))
    print(outcome)
    # A conflict is durable, but is not proof of delivery. Keep the
    # claimed turn so an operator can reconcile it with the first reply.
    if outcome == 'conflict':
        sys.exit(3)
