#!/usr/bin/env python3
"""Create a durable no-clobber turn claim; keep the inbox copy on failure."""
import os
import sys
import hashlib
import json
import tempfile
from pathlib import Path


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def claim(source, dest):
    source, dest = Path(source), Path(dest)
    with source.open('rb') as file:
        os.fsync(file.fileno())
    os.link(source, dest)  # fails if another worker owns this ID
    sync_dir(dest.parent)
    # The fingerprint binds later plaintext cleanup to the exact claimed
    # bytes, even if a delayed duplicate replaces the inbox pathname.
    state = dest.parent.parent
    fingerprint_dir = state / 'turn-fingerprints'
    fingerprint_dir.mkdir(exist_ok=True)
    sync_dir(state)
    digest = hashlib.sha256(dest.read_bytes()).hexdigest()
    target = fingerprint_dir / dest.name
    fd, tmp_name = tempfile.mkstemp(prefix='.fingerprint-', dir=fingerprint_dir)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as file:
            json.dump({'turn_id': dest.stem, 'sha256': digest}, file)
            file.flush()
            os.fsync(file.fileno())
        try:
            os.link(tmp_name, target)
        except FileExistsError:
            current = json.loads(target.read_text(encoding='utf-8'))
            if current != {'turn_id': dest.stem, 'sha256': digest}:
                raise ValueError('existing turn fingerprint differs')
        sync_dir(fingerprint_dir)
    finally:
        os.unlink(tmp_name)
        sync_dir(fingerprint_dir)
    # Retain the original inbox entry. Removing a path another process can
    # replace is not atomic with an inode check; retaining it cannot erase a
    # new submission. The hook suppresses entries whose claim/receipt exists.
    sync_dir(source.parent)


if __name__ == '__main__':
    if len(sys.argv) != 3:
        sys.exit('usage: claim_turn.py SOURCE DEST')
    claim(sys.argv[1], sys.argv[2])
