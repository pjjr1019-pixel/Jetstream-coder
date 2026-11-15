"""App-owned Edge session. Never opens a window except an explicit sign-in request."""
from pathlib import Path
import json
import os
import queue
import threading
import time
from urllib.parse import urlsplit

from browser_mailbox import BrowserMailbox
from pad_core import InstanceGuard

MUSE='https://muse.ai/'


def connection_probe(call):
    """Pair the existing Jett chat without consuming or sending any queued prompt.

    Recovery must run even while offline delivery is held. The page adapter
    still enforces its conversation, draft, busy and human-input protections.
    """
    probe = call({'op':'probe'})
    if probe.get('code') == 'needs_pair':
        paired=call({'op':'auto_pair'})
        if paired.get('ok'):
            probe = call({'op':'probe'})
        elif paired.get('code') in ('busy','draft'):
            # An authenticated Jett page that is busy is reachable, not offline.
            # These codes still prevent RelayPump from claiming or sending.
            probe={**probe,'code':paired['code']}
    return probe


class RelayPump:
    def __init__(self, mailbox):
        self.mailbox=mailbox
        self.last_job=None

    def recover_pending(self, *, new_page=False):
        active=self.mailbox.active()
        if active and ((new_page and active['state']=='sending') or
                       (active['state']=='sent' and time.time()-active['updated']>600)):
            self.mailbox.transition(active['id'],active['attempt'],'uncertain')

    def tick(self, call, probe=None):
        box=self.mailbox
        if box.offline_held(): return 'Offline mode · cloud prompts paused'
        probe=probe or call({'op':'probe'})
        active=box.active()
        if active:
            if active['state']=='sent':
                if time.time()-active['updated']>600:
                    box.transition(active['id'],active['attempt'],'uncertain')
                return 'Waiting for reply file'
            if active['state']=='uncertain': return 'Delivery unconfirmed; review before resending'
            observed=call({'op':'observe','id':active['id'],'attempt':active['attempt']})
            box.transition(active['id'],active['attempt'],'sent' if observed.get('state')=='sent' else 'uncertain')
            return 'Checking interrupted send'
        code=probe.get('code','layout')
        if code=='needs_pair':
            result=call({'op':'auto_pair'})
            return 'Connecting to Jett' if result.get('ok') else 'Waiting for Jett main chat'
        if code!='ready': return {'draft':'Muse draft preserved','busy':'Muse is replying',
                                  'quota':'Muse usage limit reached · switching to local AI',
                                  'unavailable':'Muse cannot accept messages · checking local fallback',
                                  'wrong_chat':'Muse chat changed; check sign-in window'}.get(code,'Checking Muse page')
        if self.last_job:
            call({'op':'release','id':self.last_job}); self.last_job=None
        if not box.queued_count(): return 'Ready'
        job=box.claim(probe['documentSession'])
        if not job: return 'Ready'
        self.last_job=job['id']
        try: result=box.send_guarded(job,call)
        except Exception: result={'state':'uncertain'}
        state=result.get('state')
        box.transition(job['id'],job['attempt'],state if state in ('sent','blocked') else 'uncertain')
        return 'Waiting for reply file' if state=='sent' else 'Delivery needs checking'


class HeadlessBrowser:
    def __init__(self, data, *, delivery_enabled=False, playwright_factory=None):
        self.data=Path(data)
        self.profile=self.data/'MuseBrowser'
        self.delivery_enabled=delivery_enabled
        self.playwright_factory=playwright_factory
        self.commands=queue.Queue()
        self.stopping=threading.Event()
        self.thread=None
        self._status='Stopped'
        self.cloud_ready=False
        self._probe_code=None
        self._diagnostic_at=0
        self._diagnostic_key=None

    def _publish_diagnostics(self, box):
        """Local metadata only; diagnostics must never interrupt delivery."""
        now=time.time()
        if now-self._diagnostic_at<1:return
        self._diagnostic_at=now
        try:
            active=box.active()
            summary={'status':self._status,'probe':self._probe_code,
                     'cloud_ready':self.cloud_ready,'queued':box.queued_count(),
                     'active_id':active['id'] if active else None,
                     'active_state':active['state'] if active else None}
            key=json.dumps(summary,sort_keys=True)
            snapshot={'ts':now,'pid':os.getpid(),**summary,
                      'active_age_s':round(max(0,now-active['updated']),1) if active else None}
            temp=self.data/'browser-health.json.tmp'
            temp.write_text(json.dumps(snapshot),encoding='utf-8')
            temp.replace(self.data/'browser-health.json')
            if key!=self._diagnostic_key:
                with (self.data/'browser-health.jsonl').open('a',encoding='utf-8') as stream:
                    stream.write(json.dumps(snapshot)+'\n')
                self._diagnostic_key=key
        except Exception:pass  # A failed diagnostic write must not affect a send or its ownership.

    def status(self): return self._status

    def start(self):
        if self.thread and self.thread.is_alive(): return
        while True:
            try: self.commands.get_nowait()
            except queue.Empty: break
        self.stopping.clear()
        self.thread=threading.Thread(target=self._run,name='jett-headless-edge',daemon=True)
        self.thread.start()

    def stop(self):
        self.stopping.set()
        if self.thread: self.thread.join(15)
        if self.thread and self.thread.is_alive(): raise RuntimeError('Private browser is still stopping')

    def sign_in(self):
        self.start(); self.commands.put('sign_in')

    def finish_sign_in(self): self.commands.put('finish_sign_in')

    def leave_uncertain(self): self.commands.put('leave_uncertain')

    def _delivery_allowed(self):
        if not self.delivery_enabled: return False
        try: return json.loads((self.data/'browser-mode.json').read_text())=={'mode':'headless'}
        except FileNotFoundError: return True  # Fresh desktop profile uses the private browser too.
        except (OSError,ValueError): return False

    def _run(self):
        guard=InstanceGuard(self.data/'headless-controller')
        box=context=None
        if not guard.acquire(): self._status='Private browser is already running'; return
        try:
            factory=self.playwright_factory
            if factory is None:
                from playwright.sync_api import sync_playwright
                factory=sync_playwright
            box=BrowserMailbox(self.data)
            pump=RelayPump(box)
            visible=False
            sign_in_pending=False
            retry=5
            next_attempt=0
            connected_since=None
            def close_context():
                nonlocal context,connected_since
                self.cloud_ready=False;self._probe_code=None
                if context:
                    try: context.close()
                    except Exception: pass
                context=None
                connected_since=None
            with factory() as playwright:
                while not self.stopping.is_set():
                    self._publish_diagnostics(box)
                    if self._delivery_allowed(): pump.recover_pending(new_page=context is None)
                    try:
                        command=self.commands.get_nowait()
                    except queue.Empty: command=None
                    if command=='leave_uncertain': box.abandon_uncertain()
                    if command=='sign_in': sign_in_pending=True
                    if sign_in_pending:
                        active=box.active() if self._delivery_allowed() else None
                        if active and active['state']=='sending':
                            self._status='Finishing the current request before sign-in'
                        else:
                            close_context(); visible=True; sign_in_pending=False; next_attempt=0
                    if command=='finish_sign_in' and visible:
                        close_context(); visible=False; next_attempt=0
                    if context is None:
                        if time.monotonic()<next_attempt:
                            self.stopping.wait(.1); continue
                        self._status='Sign in to Muse' if visible else 'Starting private browser'
                        try:
                            context=playwright.chromium.launch_persistent_context(str(self.profile),
                                channel='msedge',headless=not visible,chromium_sandbox=True,
                                timeout=10000,no_viewport=False,viewport={'width':1280,'height':900})
                            if self.stopping.is_set(): break
                            context.set_default_timeout(5000)
                            script_dir=Path(__file__).parent/'browser-extension'
                            script='\n'.join((script_dir/name).read_text(encoding='utf-8') for name in ('core.js','relay_page.js'))
                            from musebridge.runtime_paths import documents, muse_chat_url
                            reply_root = documents().as_posix()
                            chat_url = muse_chat_url()
                            context.add_init_script("if(location.origin==='https://muse.ai'){globalThis.__jettReplyDocuments="+json.dumps(reply_root)+";globalThis.__deskhandChatUrl="+json.dumps(chat_url)+";"+script+"\nglobalThis.__jettPad=JettPage.create();}")
                            page=context.pages[0] if context.pages else context.new_page()
                            page.goto(chat_url or MUSE,wait_until='domcontentloaded',timeout=8000)
                            connected_since=time.monotonic()
                        except Exception:
                            close_context()
                            # A failed explicit sign-in attempt must not repeatedly open windows.
                            visible=False
                            self._status='Private browser unavailable; retrying invisibly'
                            next_attempt=time.monotonic()+retry; retry=min(30,retry*2)
                            continue
                    if connected_since is not None and time.monotonic()-connected_since>30: retry=5
                    if visible:
                        self._status='Complete sign-in, then choose Done signing in'
                        try: page.wait_for_timeout(200)
                        except Exception:
                            close_context(); visible=False; next_attempt=0
                        continue
                    try:
                        if urlsplit(page.url).netloc!='muse.ai':
                            self._status='Sign-in required'; self.cloud_ready=False; page.wait_for_timeout(300); continue
                        state=page.evaluate("""() => {
                            const log=document.querySelector('[role="log"][aria-label="Chat messages"]');
                            const outside=n=>!log?.contains(n) && !!n.getClientRects().length;
                            return {adapter:typeof __jettPad==='function',chat:!!log,
                              login:[...document.querySelectorAll('input[type="email"],input[type="password"]')].some(outside) ||
                                [...document.querySelectorAll('button,a')].some(n=>outside(n)&&/^sign in$|^log in$/i.test(n.innerText.trim()))};
                        }""")
                        if state['login']: self._status='Sign-in required'; self.cloud_ready=False;self._probe_code=None
                        elif not state['adapter']: self._status='Waiting for Muse page'; self.cloud_ready=False
                        else:
                            call=lambda message:page.evaluate('(message)=>__jettPad(message)',message)
                            readiness=connection_probe(call)
                            self._probe_code=readiness.get('code')
                            self.cloud_ready=readiness.get('code') in ('ready','busy','draft')
                            if self._delivery_allowed(): self._status=pump.tick(call,probe=readiness)
                            else:
                                probe=call({'op':'probe'})
                                if probe.get('code')=='needs_pair': call({'op':'auto_pair'})
                                self._status=('Browser delivery paused; check browser mode' if self.delivery_enabled else
                                    'Ready for validation' if probe.get('code')=='ready' else 'Checking Jett main chat')
                        page.wait_for_timeout(100)
                    except Exception:
                        # A crashed page never permits automatic replay of an uncertain send.
                        self._status='Private browser reconnecting'
                        self.cloud_ready=False
                        close_context()
                        next_attempt=time.monotonic()+retry; retry=min(30,retry*2)
                if context: context.close()
        except Exception:
            self._status='Private browser unavailable; check setup'
        finally:
            self.cloud_ready=False
            if context:
                try: context.close()
                except Exception: pass
            if box: box.close()
            guard.close()
            if self.stopping.is_set(): self._status='Stopped'
