import json
import os
from pathlib import Path
import tempfile
import threading
import time

WRITE_LOCK = threading.RLock()


def atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name, suffix='.new')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(value, f, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


class Emitter:
    """Only the parent consumes this pipe and updates the shared state/journal."""
    def __call__(self, kind, **data):
        with WRITE_LOCK:
            print(json.dumps({'kind': kind, 'ts': time.time(), **data}, allow_nan=False), flush=True)


def quiet_hours_error(start,end):
    if start is None and end is None:
        return None
    def valid(value):
        return (isinstance(value,str) and len(value)==5 and value[2]==':'
                and all(c in '0123456789' for c in value[:2]+value[3:])
                and 0<=int(value[:2])<24 and 0<=int(value[3:])<60)
    if not valid(start) or not valid(end):
        return 'Quiet hours disabled: start and end must both be HH:MM (00:00-23:59) or both unset'
    return None


class QuietWarningReporter:
    """Report a changed invalid schedule once; never spam the audio loop."""
    def __init__(self,emit):
        self.emit=emit;self.last=None;self.lock=threading.RLock()
    def __call__(self,warning):
        with self.lock:
            if warning==self.last:return
            self.last=warning
            self.emit('event',event='voice.quiet_hours_invalid' if warning else 'voice.quiet_hours_restored',
                      reason=warning or 'Quiet-hours configuration is valid again')


def load_settings(path,on_quiet_warning=None):
    defaults = dict(muted=True, listening_enabled=True, quiet_start=None, quiet_end=None,
                    stt_model='base.en', stt_device='cpu', wake_threshold=1e-15)
    try:
        values = json.loads(Path(path).read_text(encoding='utf-8'))
        defaults.update({k: values[k] for k in defaults if k in values})
    except FileNotFoundError:
        pass
    if type(defaults['muted']) is not bool or type(defaults['listening_enabled']) is not bool:
        raise ValueError('Invalid mute setting')
    import math
    threshold=defaults['wake_threshold']
    if type(threshold) not in (float,int) or not math.isfinite(threshold) or not 0<threshold<=1:
        raise ValueError('Invalid wake threshold')
    if defaults['stt_model'] not in {'base.en', 'small.en'} or defaults['stt_device'] not in {'cpu', 'cuda'}:
        raise ValueError('Invalid recognition rung')
    warning=quiet_hours_error(defaults['quiet_start'],defaults['quiet_end'])
    if warning:
        # A malformed optional schedule must not wedge all audio. Preserve the file
        # for local repair; disable only that schedule and surface the problem.
        defaults.update(quiet_start=None,quiet_end=None)
    if on_quiet_warning is not None:on_quiet_warning(warning)
    return defaults
