"""Local, revocable paired-phone viewing approval; grants no AI/action authority."""
import json,re,time
import companion_data
from companion_data import atomic,paired_certificates

# Temporary AI-control state changes must not disconnect an independently approved viewer.
CONTROL_ONLY_REASONS=frozenset({'timeout','bridge_disconnect','permission_session_expired',
    'remote_unlock_requires_fresh_pin','remote_day_authorization_session','local_input_handover',
    'local_operator_disabled','local_day_authorization_revoke'})

def policy(folder=None):
    try:
        raw=((folder or companion_data.home())/'companion-settings.json').read_text(encoding='utf-8')
        if len(raw)>16384:return {}
        value=json.loads(raw)
        if not isinstance(value,dict):return {}
        trusted=value.get('viewers',[])
        if not isinstance(trusted,list) or not 1<=len(trusted)<=8 or any(not isinstance(v,str) or not re.fullmatch('[a-f0-9]{64}',v) for v in trusted):return {}
        return value
    except (OSError,ValueError):return {}

def enabled(folder=None):
    value=policy(folder)
    return all(value.get(key) is True for key in ('unattended_view','away_access','remote_start'))

def authorized(fingerprint,folder=None):
    value=policy(folder)
    return all(value.get(key) is True for key in ('unattended_view','away_access','remote_start')) and fingerprint in value.get('viewers',[])

def blocked(state,folder=None):
    # An unavailable bridge never becomes an implicit grant.
    if not isinstance(state,dict) or type(state.get('stop_latched')) is not bool:return True
    return state['stop_latched'] and not enabled(folder)

def configure(enabled):
    """Local settings path only; never exported as a phone/cloud operation."""
    if type(enabled) is not bool:raise ValueError('Use a boolean viewing approval')
    try:value=json.loads((companion_data.home()/'companion-settings.json').read_text())
    except (OSError,ValueError):value={}
    if enabled:
        trusted=sorted(paired_certificates())
        if not 1<=len(trusted)<=8:raise ValueError('Pair the intended phone before enabling unattended viewing')
        value.update(unattended_view=True,viewers=trusted,remote_start=True,away_access=True,viewing_approved_at=time.time())
    else:value.update(unattended_view=False,remote_start=False)
    atomic(companion_data.home()/'companion-settings.json',value)
    return value
