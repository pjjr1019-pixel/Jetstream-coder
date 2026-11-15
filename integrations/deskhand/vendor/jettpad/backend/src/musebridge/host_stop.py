"""Read the owning Deskhand host's STOP event; missing authority fails closed."""
import os

from .runtime_paths import root


def ensure_host_allows():
    if root() is None:
        return
    name = os.environ.get('DESKHAND_HOST_STOP_EVENT')
    if not name:
        raise ValueError('Deskhand STOP authority is unavailable')
    try:
        from sunshine.host.windows import StopSignal
        signal = StopSignal(name)
        try:
            stopped = signal.stopped()
        finally:
            signal.close()
    except (OSError, ImportError):
        raise ValueError('Deskhand STOP authority is unavailable') from None
    if stopped:
        raise ValueError('Deskhand STOP is latched')
