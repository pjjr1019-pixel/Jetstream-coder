"""deskhand_tunnel.py -- B42 (100.050) chat-tunnel provider for the Deskhand service.

Routes Deskhand chat (phone -> Muse) through the laptop agent's
/chat/* store-and-forward mailbox instead of the dead Muse worker
bridge, while the worker login fix (the real fix) is still pending.

The tunnel is CHAT-ONLY, by construction:
  * TunnelProvider is a provider with the same __call__(request) ->
    Reply contract as muse.relay, so it plugs into HostToolChat at
    the two places the relay is registered (see deskhand_service.py
    _muse_provider()). It submits one turn (plain text + route) to
    the agent mailbox and blocks polling for the reply.
  * It can only move chat text. It has no exec / files / screenshot /
    input capability, and no code path in this module calls any.
  * The service uses the FULL bridge token (read from
    deskhand_bridge_config.json, the file the service already trusts
    for the bridge) and only speaks to an HTTP IP-literal loopback
    origin. Redirects and environment proxies are disabled.
    The phone holds NO key at all in the service-mediated design.
  * Agent-task turns (agent_task_id set) are OUT OF SCOPE for this
    round: the tunnel provider can carry the exchange shape, but the
    service's access_validate gate still requires an attached Muse
    object, so agent turns are refused with 'Muse is unavailable'
    while the worker is down. Plain chat is the scope (see
    BUILD-SPEC-100050-TUNNEL.md section 4).

Config (data dir, deskhand_tunnel_config.json -- created at deploy):
  {"enabled": false, "force": false,
   "agent_url": "http://127.0.0.1:8899",
   "poll_interval_secs": 5, "reply_timeout_secs": 600}
  * enabled=false (default) => the tunnel is inert; the relay path
    is untouched. A missing / unreadable / malformed config reads as
    disabled and holds receipt recovery until valid config is restored.
  * force=true => the tunnel serves the Muse route even when Muse is
    attached. force=false (default) => the live relay wins whenever
    Muse is attached; the tunnel only serves when Muse is absent.
    A human flips force at deploy time. The tunnel never switches
    itself on or off.
  * reply_timeout_secs defaults to 600 (the relay's outer bound).

Logging: turn_id + byte lengths + counters only. Turn text, reply
text, and the token are NEVER logged by this module.

Cancel contract (for the record; deskhand_dispatch.py is untouched
by this build): "partial: stops only the wait for the reply; a turn
already claimed by the VM loop keeps running and its reply ages out
of the agent mailbox by TTL (never replayed)".
"""
import ipaddress
import json
import time
import threading
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener

from deskhand_dispatch import DurableProviderError, Reply, StopConfirmed

TUNNEL_CONFIG_NAME = "deskhand_tunnel_config.json"
BRIDGE_CONFIG_NAME = "deskhand_bridge_config.json"
DEFAULT_AGENT_URL = "http://127.0.0.1:8899"
DEFAULT_POLL_INTERVAL_SECS = 5
DEFAULT_REPLY_TIMEOUT_SECS = 600
MAX_TURN_BYTES = 64 * 1024


def _validate_agent_url(agent_url):
    if not isinstance(agent_url, str) or not agent_url or any(
            char.isspace() or ord(char) < 0x20 or ord(char) == 0x7f
            for char in agent_url):
        raise ValueError("agent_url must be an HTTP loopback origin")
    try:
        parsed = urlsplit(agent_url)
        host = parsed.hostname
        port = parsed.port
        loopback = ipaddress.ip_address(host).is_loopback
    except (TypeError, ValueError):
        loopback = False
        parsed = None
        port = None
    if (not loopback or parsed is None or parsed.scheme != "http"
            or parsed.username is not None or parsed.password is not None
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment
            or port == 0):
        raise ValueError("agent_url must be an HTTP loopback origin without credentials or path")
    return agent_url.rstrip("/")

CANCEL_CONTRACT = ("partial: stops only the wait for the reply; a turn "
                   "already claimed by the VM loop keeps running and its "
                   "reply ages out of the agent mailbox by TTL (never replayed)")


class TunnelError(RuntimeError):
    """The agent mailbox could not be reached or answered badly."""


def load_tunnel_config(data_dir):
    """Read tunnel settings; invalid config disables use of all endpoints."""
    from pathlib import Path
    path = Path(data_dir) / TUNNEL_CONFIG_NAME
    cfg = {"enabled": False, "force": False,
           "agent_url": DEFAULT_AGENT_URL,
           "poll_interval_secs": DEFAULT_POLL_INTERVAL_SECS,
           "reply_timeout_secs": DEFAULT_REPLY_TIMEOUT_SECS}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        cfg["configuration_error"] = (
            "tunnel configuration is unavailable or malformed; "
            f"refusing endpoint use ({error})")
        return cfg
    if not isinstance(raw, dict):
        cfg["configuration_error"] = "tunnel configuration must be a JSON object"
        return cfg
    if isinstance(raw.get("enabled"), bool):
        cfg["enabled"] = raw["enabled"]
    if isinstance(raw.get("force"), bool):
        cfg["force"] = raw["force"]
    if "agent_url" in raw:
        try:
            cfg["agent_url"] = _validate_agent_url(raw["agent_url"])
        except ValueError as error:
            cfg["enabled"] = False
            cfg["configuration_error"] = str(error)
    for key, default in (("poll_interval_secs", DEFAULT_POLL_INTERVAL_SECS),
                         ("reply_timeout_secs", DEFAULT_REPLY_TIMEOUT_SECS)):
        value = raw.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            cfg[key] = value
    return cfg


def load_agent_token(data_dir):
    """The full agent token, from the bridge config the service
    already trusts. Returns None when the file is missing or has no
    usable token (the caller fails loudly, never silently)."""
    from pathlib import Path
    path = Path(data_dir) / BRIDGE_CONFIG_NAME
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    token = raw.get("token")
    if not isinstance(token, str) or not token or token == "CHANGE-ME":
        return None
    return token


class TunnelHttpClient:
    """Minimal urllib client for the agent's /chat/* endpoints."""

    def __init__(self, agent_url, token, timeout_secs=10):
        if not isinstance(token, str) or not token:
            raise ValueError("tunnel needs the agent token from deskhand_bridge_config.json")
        self.agent_url = _validate_agent_url(agent_url)
        self.token = token
        self.timeout_secs = timeout_secs
        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        self.client = build_opener(ProxyHandler({}), NoRedirect())

    def _request(self, method, path, payload=None):
        body = None
        headers = {"Authorization": "Bearer " + self.token}
        if payload is not None:
            body = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.agent_url + path, data=body,
                                     headers=headers, method=method)
        try:
            with self.client.open(req, timeout=self.timeout_secs) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")[:200]
            raise TunnelError(f"agent {method} {path} -> HTTP {error.code}: {detail}")
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise TunnelError(f"agent {method} {path} unreachable: {error}")

    def submit_turn(self, turn_id, text, route, agent_task_id=None,
                    sent_at_ms=None, received_at_ms=None):
        if len(text.encode()) > MAX_TURN_BYTES:
            raise ValueError("turn text exceeds 64 KiB")
        payload = {"turn_id": turn_id, "text": text,
                   "assistant": route.get("assistant", ""),
                   "model": route.get("model", ""),
                   "account": route.get("account", ""),
                   "effort": route.get("effort", ""),
                   "agent_task_id": agent_task_id,
                   "sent_at_ms": sent_at_ms, "received_at_ms": received_at_ms}
        return self._request("POST", "/chat/submit", payload)

    def fetch_reply(self, turn_id):
        """The undelivered reply for one turn, or None."""
        data = self._request("GET", "/chat/replies?turn_id=" + turn_id)
        replies = data.get("replies", []) if isinstance(data, dict) else []
        return replies[0] if replies else None

    def ack_reply(self, turn_id):
        return self._request("POST", "/chat/ack", {"turn_id": turn_id})

    def wait_reply(self, turn_id, timeout_secs, poll_interval_secs, stop_event):
        """Block until the reply/error for turn_id arrives, the timeout
        elapses (returns None), or stop_event is set (returns None).
        Never raises for a timeout or a stop -- the caller decides."""
        deadline = time.monotonic() + timeout_secs
        while True:
            if stop_event is not None and stop_event.is_set():
                return None
            try:
                reply = self.fetch_reply(turn_id)
            except TunnelError:
                reply = None
            if reply is not None:
                return reply
            if time.monotonic() >= deadline:
                return None
            if stop_event is not None:
                stop_event.wait(poll_interval_secs)
            else:
                time.sleep(poll_interval_secs)


class UnavailableProvider:
    """Stands in when the tunnel was requested but could not be built
    (e.g. no bridge token). Every turn fails loudly with the stored
    reason instead of the service failing to start."""

    def __init__(self, message):
        self.message = message
        self.stopping = threading.Event()

    def prepare_turn(self):
        self.stopping.clear()

    def request_stop(self):
        self.stopping.set()

    def close(self):
        pass

    def reply_saved(self, request):
        pass

    def __call__(self, request):
        raise RuntimeError("Chat tunnel unavailable: " + self.message)


class TunnelProvider:
    """Provider that carries one chat turn through the agent mailbox.

    Same call shape as muse.relay: __call__(request) -> Reply, plus
    the optional hooks the dispatcher uses (prepare_turn,
    request_stop, close, reply_saved). Agent-task turns are refused
    here only by the service's access_validate gate, not by this
    class -- see the module docstring for the out-of-scope note.
    """

    cancel_contract = CANCEL_CONTRACT

    def __init__(self, client, reply_timeout_secs=DEFAULT_REPLY_TIMEOUT_SECS,
                 poll_interval_secs=DEFAULT_POLL_INTERVAL_SECS, mailbox=None):
        self.client = client
        self.mailbox = mailbox
        self.reply_timeout_secs = reply_timeout_secs
        self.poll_interval_secs = poll_interval_secs
        self.stopping = threading.Event()
        self.agent = False
        self._lock = threading.Lock()
        # Defines a linearization boundary between a pre-submit stop and a
        # potentially delivered submission; the lock is never held over I/O.
        self._submit_gate = threading.Lock()
        self._submit_started = False
        self._counts = {"turns_submitted": 0, "replies_received": 0,
                        "reply_errors": 0, "timeouts": 0, "transport_errors": 0,
                        "last_error": None}

    def _bump(self, key, error=None):
        with self._lock:
            self._counts[key] = self._counts.get(key, 0) + 1
            if error is not None:
                self._counts["last_error"] = str(error)[:200]

    def status(self):
        with self._lock:
            return dict(self._counts)

    def prepare_turn(self):
        with self._submit_gate:
            self.stopping.clear()
            self._submit_started = False

    def request_stop(self):
        """Stop waiting for the reply. The turn may still be answered
        by the VM loop afterwards; its reply ages out of the mailbox
        by TTL and is never delivered to the wrong turn."""
        with self._submit_gate:
            self.stopping.set()

    def close(self):
        pass

    def reply_saved(self, request):
        # The dispatcher calls this only after saving the correlated reply.
        # On failure the agent retains the reply for later reconciliation.
        result = self.client.ack_reply(request.turn_id)
        if self.mailbox is not None and isinstance(result, dict) and result.get('turn_id') == request.turn_id and result.get('delivered') is True:
            self.mailbox.clear_tunnel_receipt(request.turn_id)

    def reconcile_receipts(self, stop_event=None):
        """Recover only recorded plain-tunnel turns; never submit a turn here."""
        if self.mailbox is None:
            return
        for row in self.mailbox.pending_tunnel_receipts():
            if stop_event is not None and stop_event.is_set():
                break
            try:
                # Ack is idempotent: retry even when a previous response was
                # lost after the agent had already marked the reply delivered.
                if row['status'] in ('completed', 'failed'):
                    ack = self.client.ack_reply(row['id'])
                    if stop_event is not None and stop_event.is_set():
                        break
                    if isinstance(ack, dict) and ack.get('turn_id') == row['id'] and ack.get('delivered') is True:
                        self.mailbox.clear_tunnel_receipt(row['id'])
                    continue
                result = self.client.fetch_reply(row['id'])
                if stop_event is not None and stop_event.is_set():
                    break
                if not isinstance(result, dict) or result.get('turn_id') != row['id']:
                    continue
                if result.get('error'):
                    reply, error = None, 'Chat tunnel reply error: ' + str(result['error'])[:500]
                else:
                    reply, error = result.get('reply'), None
                if not self.mailbox.resolve_tunnel_receipt(row['id'], reply=reply, error=error):
                    continue
                if stop_event is not None and stop_event.is_set():
                    break
                ack = self.client.ack_reply(row['id'])
                if stop_event is not None and stop_event.is_set():
                    break
                if isinstance(ack, dict) and ack.get('turn_id') == row['id'] and ack.get('delivered') is True:
                    self.mailbox.clear_tunnel_receipt(row['id'])
            except (TunnelError, OSError):
                # Keep the marker for the next pass, including after restart.
                continue

    def reconcile_historical_terminals(self, rows, stop_event=None):
        """Acknowledge only old replies that exactly match a durable local result."""
        for row in rows:
            if stop_event is not None and stop_event.is_set():
                break
            turn_id = row.get('id')
            if not isinstance(turn_id, str) or not turn_id:
                continue
            try:
                result = self.client.fetch_reply(turn_id)
                if stop_event is not None and stop_event.is_set():
                    break
                if not isinstance(result, dict) or result.get('turn_id') != turn_id:
                    continue
                if row.get('status') == 'completed':
                    matches = (isinstance(row.get('reply'), str)
                               and result.get('reply') == row['reply']
                               and not result.get('error'))
                else:
                    expected = result.get('error')
                    matches = (isinstance(expected, str) and bool(expected)
                               and row.get('error') ==
                               'Chat tunnel reply error: ' + expected[:500])
                if not matches or (stop_event is not None and stop_event.is_set()):
                    continue
                ack = self.client.ack_reply(turn_id)
                if (stop_event is not None and stop_event.is_set()):
                    break
                if not (isinstance(ack, dict) and ack.get('turn_id') == turn_id
                        and ack.get('delivered') is True):
                    continue
            except (TunnelError, OSError):
                continue

    def __call__(self, request):
        self.agent = request.agent_task_id is not None
        # prepare_turn clears a previous stop under the dispatcher's admission
        # lock. Never clear here: a stop can arrive after admission but before
        # the adapter is invoked.
        if self.stopping.is_set():
            raise StopConfirmed('Tunnel stopped before submission')
        # The provider never logs turn text -- only the turn id and
        # the byte length (the transport itself carries no logging).
        if len(request.text.encode()) > MAX_TURN_BYTES:
            raise ValueError("turn text exceeds 64 KiB")
        if self.mailbox is not None and request.agent_task_id is None:
            self.mailbox.mark_tunnel_dispatch(request.turn_id)
        with self._submit_gate:
            stopped_before_submit = self.stopping.is_set()
            if not stopped_before_submit:
                # From this point a stop is partial, even if the network call
                # has not yet been scheduled: delivery is conservatively
                # uncertain and never treated as confirmed cancellation.
                self._submit_started = True
        if stopped_before_submit:
            if self.mailbox is not None and request.agent_task_id is None:
                self.mailbox.clear_tunnel_receipt(request.turn_id)
            raise StopConfirmed('Tunnel stopped before submission')
        try:
            self.client.submit_turn(
                request.turn_id, request.text,
                {"assistant": request.route.assistant,
                 "model": request.route.model,
                 "account": request.route.account,
                 "effort": request.route.effort},
                agent_task_id=request.agent_task_id,
                sent_at_ms=int(time.time() * 1000),
                received_at_ms=int(time.time() * 1000))
        except (TunnelError, ValueError) as error:
            self._bump("transport_errors", error)
            raise RuntimeError("Chat tunnel submit failed: " + str(error))
        self._bump("turns_submitted")
        reply = self.client.wait_reply(
            request.turn_id, self.reply_timeout_secs,
            self.poll_interval_secs, self.stopping)
        if reply is None:
            if self.stopping.is_set():
                raise RuntimeError("Tunnel wait stopped after submit; delivery may still complete")
            self._bump("timeouts", "no reply within %ss" % self.reply_timeout_secs)
            raise TimeoutError("Chat tunnel: no reply for turn %s within %ss"
                               % (request.turn_id, self.reply_timeout_secs))
        if not isinstance(reply, dict) or reply.get('turn_id') != request.turn_id:
            raise TunnelError('Chat tunnel reply identity mismatch')
        if reply.get("error"):
            self._bump("reply_errors", reply["error"])
            raise DurableProviderError("Chat tunnel reply error: " + str(reply["error"])[:500])
        self._bump("replies_received")
        return Reply(request.turn_id, request.route, reply.get("reply") or "")
