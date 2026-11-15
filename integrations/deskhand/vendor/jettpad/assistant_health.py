"""Bounded, non-secret capability and error summaries for assistant turns."""
from urllib.error import HTTPError,URLError
import time

def snapshot(home,tools,vision,request=None):
    if request is None:
        from musebridge.lifecycle import local_request
        request=local_request
    try:state=request(home)
    except Exception:state=None
    if not isinstance(state,dict):access='unknown (bridge status unavailable)'
    elif state.get('stop_latched') is True:access='unavailable (STOP is active)'
    elif state.get('connected') is False:access='unavailable (bridge disconnected)'
    elif state.get('desktop_control_enabled') is False:access='unavailable (desktop control is disabled)'
    elif isinstance(state.get('session'),dict) and type(state['session'].get('expires_at')) in (int,float) and state['session']['expires_at']<=time.time():access='unavailable (local session expired)'
    elif state.get('connected') is True and state.get('desktop_control_enabled') is True and state.get('stop_latched') is False:access='available subject to executor admission, current session and local permissions'
    else:access='unknown (incomplete bridge status)'
    if not tools:access='unavailable (this model has no PC tools)'
    features=state.get('features') if isinstance(state,dict) else None
    def configured(name):
        value=features.get(name) if isinstance(features,dict) else None
        return 'enabled' if value is True else 'disabled' if value is False else 'unknown'
    return ('PC tools: '+('supported' if tools else 'unavailable')+'; visual input: '+('supported' if vision else 'unavailable')+'; PC control: '+access+'; screenshot grant: '+configured('screenshots')+'; execution grant: '+configured('exec')+'. Grants and session are rechecked at execution. No direct email, calendar, or external services are available unless explicitly provided by a tool. Actual executor receipts determine outcomes.')

def error_message(error,mode):
    from assistant_executor import UncertainAction
    text=str(error).casefold()
    if isinstance(error,UncertainAction) or 'outcome is uncertain' in text or 'could not be confirmed' in text or 'unconfirmed' in text:
        return 'The action outcome is unknown. Check the existing job status before deciding what to do; do not resend it automatically.'
    if 'stop' in text or 'revoked' in text or 'cancelled' in text:
        return 'The action was stopped or cancelled. Check STOP and the current authorization locally; review existing job status before any new request.'
    if 'permission' in text or 'approval' in text or 'protected context' in text or 'disabled' in text:
        return 'The action was refused by an existing permission or protection. Check the local control settings and the target; do not bypass protections.'
    if isinstance(error,TimeoutError) or 'timed out' in text or 'timeout' in text:
        return 'The request timed out. If a PC action was submitted, check its existing job status before retrying.'
    if mode=='local':
        if isinstance(error,HTTPError) and error.code==404:return 'The selected local model is unavailable. Check that it is installed.'
        if isinstance(error,HTTPError) and error.code==429:return 'The local model service is busy. Wait, then retry the same model.'
        if isinstance(error,(HTTPError,URLError,ConnectionError)):return 'The local model service is unavailable or rejected the request. Start Ollama or check the selected model.'
        return 'The local model could not complete the reply. Check the model; review any existing PC job before retrying.'
    if 'sign' in text or 'auth' in text or 'login' in text:return 'Codex sign-in could not be verified. Check the selected account and sign in if needed.'
    if 'model' in text:return 'The selected Codex model is unavailable or could not be verified. Check the model for this account.'
    return 'Codex could not complete the reply. Check the selected account and connection; review any existing PC job before retrying.'


def failure_category(error, mode):
    """Return a bounded label, never exception text or credentials."""
    text=str(error).casefold()
    if 'unconfirmed' in text or 'uncertain' in text:return 'uncertain_action'
    if any(x in text for x in ('stop','revoked','cancelled')):return 'stopped_or_cancelled'
    if any(x in text for x in ('protected context','permission','approval','disabled')):return 'permission_refused'
    if isinstance(error,TimeoutError) or 'timed out' in text or 'timeout' in text:return 'timeout'
    if isinstance(error,HTTPError):
        if error.code==404:return 'model_unavailable'
        if error.code==429:return 'service_busy'
        if error.code in (401,403):return 'authentication'
        if error.code>=500:return 'service_error'
        return 'http_error'
    if isinstance(error,(URLError,ConnectionError)):return 'service_unavailable'
    if mode=='codex' and any(x in text for x in ('sign in','sign-in','auth','login')):return 'authentication'
    if 'model' in text and 'unavailable' in text:return 'model_unavailable'
    if any(x in text for x in ('out of memory','out-of-memory','insufficient memory','cuda out of memory')):return 'insufficient_memory'
    return 'unknown'
