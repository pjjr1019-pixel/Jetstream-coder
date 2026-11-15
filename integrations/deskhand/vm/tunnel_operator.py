#!/usr/bin/env python3
"""Read tunnel recovery state and record explicit operator review.

This tool never sends, retries, deletes, moves, or changes a turn or reply;
it never signals a process or reads a chat key. A recorded review is a human
assertion, not proof that a remote action did or did not run.
"""
import argparse
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ID_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$')
REVIEW_OUTCOMES = ('unknown', 'not_sent_after_review', 'completed_elsewhere', 'waiting')


def valid_id(value):
    return bool(isinstance(value, str) and ID_RE.fullmatch(value) and value not in ('.', '..'))


def _names(directory):
    if not directory.is_dir():
        return set()
    return {p.stem for p in directory.glob('*.json') if p.is_file() and valid_id(p.stem)}


def _nested_names(directory):
    if not directory.is_dir():
        return set()
    return {p.stem for p in directory.glob('*/*.json') if p.is_file() and valid_id(p.stem)}


def _unrecognized_count(directory, pattern):
    if not directory.is_dir():
        return 0
    return sum(1 for p in directory.glob(pattern) if p.is_file() and not valid_id(p.stem))


def _valid_completion(path, turn_id):
    try:
        if path.stat().st_size > 4096:
            return False
        row = json.loads(path.read_text(encoding='utf-8'))
        return (isinstance(row, dict) and row.get('turn_id') == turn_id
                and row.get('kind') in ('reply', 'error')
                and isinstance(row.get('stored_sha256'), str)
                and re.fullmatch(r'[0-9a-f]{64}', row['stored_sha256']) is not None)
    except (OSError, UnicodeError, ValueError):
        return False


def collect_status(work_dir, now=None):
    """Return metadata only; never read prompts, replies, key or credentials."""
    root = Path(work_dir)
    now = time.time() if now is None else now
    sources = {
        'inbox': _names(root / 'turns'),
        'claimed': _names(root / 'turns-claimed'),
        'published': _names(root / 'replies'),
        'posting': _nested_names(root / 'reply-claims'),
        'completed': _names(root / 'reply-completed'),
        'conflict': _nested_names(root / 'replies-conflicts'),
        'dead_letter': _nested_names(root / 'replies-dead'),
        'cleanup_held': _nested_names(root / 'inbox-cleanup'),
    }
    unrecognized = {
        name: _unrecognized_count(root / directory, pattern)
        for name, directory, pattern in (
            ('inbox', 'turns', '*.json'), ('claimed', 'turns-claimed', '*.json'),
            ('published', 'replies', '*.json'), ('posting', 'reply-claims', '*/*.json'),
            ('completed', 'reply-completed', '*.json'), ('conflict', 'replies-conflicts', '*/*.json'),
            ('dead_letter', 'replies-dead', '*/*.json'), ('cleanup_held', 'inbox-cleanup', '*/*.json'))
    }
    ids = sorted(set().union(*sources.values()))
    turns = []
    for turn_id in ids:
        evidence = [name for name, values in sources.items() if turn_id in values]
        claim = root / 'turns-claimed' / (turn_id + '.json')
        try:
            claim_age_s = max(0, int(now - claim.stat().st_mtime)) if claim.is_file() else None
        except OSError:
            claim_age_s = None
        valid_completion = ('completed' in evidence and _valid_completion(
            root / 'reply-completed' / (turn_id + '.json'), turn_id))
        if ('conflict' in evidence or 'dead_letter' in evidence or 'cleanup_held' in evidence
                or ('completed' in evidence and not valid_completion)):
            state = 'review_required'
        elif valid_completion:
            state = 'reply_accepted'
        elif 'posting' in evidence or 'published' in evidence:
            state = 'reply_pending_delivery'
        elif 'claimed' in evidence and claim_age_s is not None and claim_age_s >= 300:
            state = 'outcome_uncertain'
        elif 'claimed' in evidence:
            state = 'claimed'
        else:
            state = 'queued_or_retained'
        turns.append({'turn_id': turn_id, 'state': state, 'evidence': evidence,
                      'claim_age_seconds': claim_age_s})

    pid = None
    try:
        candidate = int((root / 'tunnel-poll.pid').read_text(encoding='utf-8').strip())
        if candidate > 0:
            pid = candidate
    except (OSError, ValueError):
        pass
    pid_state = 'missing_or_invalid' if pid is None else 'identity_unverified'
    heartbeat_age_s = None
    try:
        hb = json.loads((root / 'heartbeat.json').read_text(encoding='utf-8'))
        timestamp = datetime.fromisoformat(hb['last_poll_at'].replace('Z', '+00:00'))
        if timestamp.tzinfo is not None:
            heartbeat_age_s = max(0, int(now - timestamp.timestamp()))
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        pass
    return {'turns': turns, 'unrecognized_file_counts': unrecognized,
            'process': {'pid': pid, 'pid_state': pid_state,
            'heartbeat_age_seconds': heartbeat_age_s},
            'retention': {
                'completion_markers': len(_names(root / 'reply-completed')),
                'turn_fingerprints': len(_names(root / 'turn-fingerprints')),
                'policy': 'retained indefinitely; no automatic pruning while worker completion and replay safety cannot be proven',
            },
            'safety': 'Outcomes can be uncertain. A recent claim does not prove a worker is alive. This report does not prove process identity or that work was not executed.'}


def record_review(work_dir, turn_id, outcome, confirm):
    if not valid_id(turn_id) or outcome not in REVIEW_OUTCOMES:
        raise ValueError('invalid turn ID or review outcome')
    if confirm != turn_id:
        raise ValueError('confirmation must equal the exact turn ID')
    root = Path(work_dir)
    status = collect_status(root)
    if not any(row['turn_id'] == turn_id for row in status['turns']):
        raise ValueError('turn has no local evidence; nothing recorded')
    reviews = root / 'operator-reviews'
    reviews.mkdir(mode=0o700, parents=True, exist_ok=True)
    name = f'{turn_id}.{uuid.uuid4().hex}.json'
    path = reviews / name
    record = {'turn_id': turn_id, 'operator_assertion': outcome,
              'recorded_at': datetime.now(timezone.utc).isoformat(),
              'action': 'review_only_no_retry_or_delivery'}
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as file:
            json.dump(record, file, ensure_ascii=True)
            file.flush()
            os.fsync(file.fileno())
        if os.name != 'nt':
            for directory in (reviews, root):
                directory_fd = os.open(directory, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
    except Exception:
        # An incomplete note is never used to authorize any action.
        raise
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work-dir', required=True)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('status', help='show turn evidence and unverified process status')
    record = sub.add_parser('record-review', help='record a human assertion; does not retry or deliver')
    record.add_argument('--turn-id', required=True)
    record.add_argument('--outcome', required=True, choices=REVIEW_OUTCOMES)
    record.add_argument('--confirm-turn-id', required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'status':
            result = collect_status(args.work_dir)
        else:
            result = record_review(args.work_dir, args.turn_id, args.outcome, args.confirm_turn_id)
        print(json.dumps(result, indent=2))
        return 0
    except (ValueError, OSError) as error:
        print('operator review error: ' + str(error), file=__import__('sys').stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
