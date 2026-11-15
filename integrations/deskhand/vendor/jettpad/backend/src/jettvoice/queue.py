"""Durable playback ownership; no replay after uncertain audio side effects."""
import json
from pathlib import Path
import re
import sqlite3
import time
from .common import atomic

NAME = re.compile(r'^(\d{6})-(\d+(?:\.\d+)?)-([A-Za-z0-9_-]{1,64})\.mp3$')


class VoiceQueue:
    def __init__(self, inbox, data, provenance, emit, known_reason):
        self.root = Path(inbox)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / 'played').mkdir(exist_ok=True)
        Path(data).mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(Path(data) / 'playback.sqlite3')
        self.db.execute('CREATE TABLE IF NOT EXISTS playback(name TEXT PRIMARY KEY, state TEXT, sidecar TEXT)')
        self.db.commit()
        self.provenance, self.emit, self.known_reason = provenance, emit, known_reason
        # The intent to output was durable before any output. Unknown drain is not success.
        for name, sidecar in self.db.execute("SELECT name,sidecar FROM playback WHERE state='playing'").fetchall():
            receipt = self.root / 'played' / (Path(name).stem + '.json')
            try:
                completed = json.loads(receipt.read_text()).get('completed') is True
            except (OSError, ValueError):
                completed = False
            if completed:
                with self.db:
                    self.db.execute("UPDATE playback SET state='done' WHERE name=?", (name,))
            else:
                self.receipt(name, json.loads(sidecar), completed=False, reason='interrupted_or_uncertain_shutdown')

    def receipt(self, name, sidecar, completed=False, **extra):
        reason=extra.pop('reason','audio drained')
        value = {**sidecar, 'completed': completed, 'played_ts': time.time(),
                 'playback_reason':reason, **extra}
        atomic(self.root / 'played' / (Path(name).stem + '.json'), value)
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO playback VALUES (?,?,?)',
                            (name, 'done' if completed else 'interrupted', json.dumps(sidecar)))
        self.emit('event', event='voice.played' if completed else 'voice.interrupted', reason=reason, ref=name)

    def candidates(self):
        candidates = []
        for path in self.root.glob('*.mp3'):
            if self.db.execute('SELECT 1 FROM playback WHERE name=?', (path.name,)).fetchone():
                continue
            receipt=self.root/'played'/(path.stem+'.json')
            if receipt.exists():
                try:
                    previous=json.loads(receipt.read_text())
                    if previous.get('completed') is True or (previous.get('completed') is False and not previous.get('held')):
                        with self.db:
                            self.db.execute('INSERT OR IGNORE INTO playback VALUES (?,?,?)',
                                (path.name,'done' if previous['completed'] else 'interrupted',json.dumps(previous)))
                        self.emit('event',event='voice.reconciled',reason='Canonical terminal receipt prevents replay',ref=path.name)
                        continue
                except (OSError,ValueError):
                    # An unreadable receipt has an unknown outcome, not permission to replay.
                    self.emit('event',event='voice.receipt_unreadable',reason='Held for local review; no replay',ref=path.name)
                    continue
            match = NAME.fullmatch(path.name)
            if not match:
                self.reject(path.name, {}, 'Invalid voice filename')
                continue
            try:
                sidebytes, _ = self.provenance(path.with_suffix('.json'))
                side = json.loads(sidebytes)
                audio, digest = self.provenance(path)
            except json.JSONDecodeError:
                self.reject(path.name, {}, 'Authenticated sidecar contains malformed JSON')
                continue
            except ValueError as e:
                if 'No authenticated' not in str(e):
                    self.reject(path.name, {}, 'Voice provenance verification failed: '+str(e))
                continue
            except OSError:
                # Arrival is two independent commits. Incomplete pairs wait; never play raw files.
                continue
            try:
                if type(side.get('seq')) is not int or side['seq'] != int(match[1]):
                    raise ValueError('Voice sequence mismatch')
                if side.get('priority') not in {'normal', 'urgent'} or not isinstance(side.get('transcript'), str):
                    raise ValueError('Invalid voice sidecar')
                if type(side.get('created_ts')) not in (int, float) or not isinstance(side.get('reason'), str):
                    raise ValueError('Invalid voice timestamp/reason')
                if len(side['reason'].strip()) < 12 or not self.known_reason(side):
                    raise ValueError('Speech lacks a meaningful known request/result/reminder/event')
                side['sha256'] = digest
                candidates.append((path.name, side, audio))
            except ValueError as e:
                self.reject(path.name, side, str(e))
        return sorted(candidates, key=lambda item: (item[1]['priority'] != 'urgent', item[1]['seq'], item[0]))

    def reject(self, name, side, reason):
        self.receipt(name, side, completed=False, reason=reason, dropped=True)

    def hold(self, item, reason):
        name, side, _ = item
        target = self.root / 'played' / (Path(name).stem + '.json')
        if target.exists():
            old = json.loads(target.read_text())
            if old.get('held_reason') == reason:
                return
        atomic(target, {**side, 'held': True, 'held_reason': reason, 'completed': False})
        self.emit('event', event='voice.held', reason=reason, ref=name)

    def claim(self, item):
        name, side, _ = item
        with self.db:
            self.db.execute('INSERT INTO playback VALUES (?,?,?)', (name, 'playing', json.dumps(side)))
        self.emit('event', event='voice.started', reason='Verified authenticated audio; playback claimed', ref=name)

    def close(self):
        self.db.close()
