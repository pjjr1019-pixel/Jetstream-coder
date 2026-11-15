#!/usr/bin/env python3
"""Clean completed inbox copies after durable, matching turn fingerprints.

Private captures survive crashes. Different or unprovable content is retained;
this utility never deletes a turn merely because a filename matches a receipt.
"""
import hashlib
import json
import os
import sys
import uuid
from pathlib import Path

from claim_turn import sync_dir


def _valid_id(turn_id):
    return bool(turn_id) and turn_id not in ('.', '..') and Path(turn_id).name == turn_id


def _evidence(state, turn_id):
    if not _valid_id(turn_id):
        return None
    try:
        completed = json.loads((state / 'reply-completed' / (turn_id + '.json')).read_text(encoding='utf-8'))
        fingerprint = json.loads((state / 'turn-fingerprints' / (turn_id + '.json')).read_text(encoding='utf-8'))
        digest = fingerprint.get('sha256')
        reply_digest = completed.get('stored_sha256')
        if (completed.get('turn_id') != turn_id or fingerprint.get('turn_id') != turn_id
                or completed.get('kind') not in ('reply', 'error')
                or not isinstance(reply_digest, str) or len(reply_digest) != 64
                or any(ch not in '0123456789abcdef' for ch in reply_digest)
                or not isinstance(digest, str) or len(digest) != 64
                or any(ch not in '0123456789abcdef' for ch in digest)):
            return None
        return digest
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def _process(state, owned):
    turn_id = owned.stem
    expected = _evidence(state, turn_id)
    if expected is None:
        return 'unverified'
    try:
        with owned.open('rb') as file:
            digest = hashlib.file_digest(file, 'sha256').hexdigest()
        if digest != expected:
            return 'conflict'
        owned.unlink()
        sync_dir(owned.parent)
        owned.parent.rmdir()
        sync_dir(owned.parent.parent)
        return 'removed'
    except OSError:
        return 'retained'


def cleanup(state_dir, turn_id, source_dir='turns', *, reviewed_claim=False):
    state = Path(state_dir)
    if source_dir not in ('turns', 'turns-claimed'):
        raise ValueError('unsupported inbox directory')
    if source_dir == 'turns-claimed' and not reviewed_claim:
        return 'review_required'
    if _evidence(state, turn_id) is None:
        return 'unverified'
    source = state / source_dir / (turn_id + '.json')
    if not source.exists():
        return 'absent'
    root = state / 'inbox-cleanup'
    root.mkdir(exist_ok=True)
    sync_dir(state)
    capture = root / uuid.uuid4().hex
    capture.mkdir()
    sync_dir(root)
    try:
        owned = capture / source.name
        os.replace(source, owned)
        sync_dir(capture)
        sync_dir(source.parent)
    except OSError:
        try:
            capture.rmdir()
        except OSError:
            pass
        return 'retained'
    return _process(state, owned)


def sweep(state_dir):
    state = Path(state_dir)
    root = state / 'inbox-cleanup'
    if not root.exists():
        return []
    results = [(p.stem, _process(state, p)) for p in root.glob('*/*.json')]
    for directory in root.iterdir():
        if directory.is_dir():
            try:
                directory.rmdir()  # empty only; never remove retained evidence
                sync_dir(root)
            except OSError:
                pass
    return results


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--sweep':
        results = sweep(sys.argv[2])
        for turn_id, result in results:
            if result != 'removed':
                print(f'{turn_id}: {result}', file=sys.stderr)
    elif len(sys.argv) == 4 and sys.argv[1] == '--turn':
        result = cleanup(sys.argv[2], sys.argv[3])
        if result not in ('removed', 'absent'):
            print(result, file=sys.stderr)
            sys.exit(1)
    elif len(sys.argv) == 4 and sys.argv[1] == '--claim-reviewed':
        # This command is intentionally explicit: a receipt proves delivery,
        # not that every previously woken worker has stopped executing.
        result = cleanup(sys.argv[2], sys.argv[3], 'turns-claimed', reviewed_claim=True)
        if result not in ('removed', 'absent'):
            print(result, file=sys.stderr)
            sys.exit(1)
    else:
        sys.exit('usage: inbox_cleanup.py --sweep STATE | --turn STATE ID | --claim-reviewed STATE ID')
