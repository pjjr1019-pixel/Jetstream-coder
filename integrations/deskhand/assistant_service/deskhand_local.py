"""Explicitly configured, text-only Ollama adapter; no auto-discovery or tools."""
import ipaddress
import json
from urllib.parse import urlsplit
from urllib.request import Request as HttpRequest, ProxyHandler, build_opener
from urllib.error import HTTPError

from deskhand_dispatch import Reply, Route


class LocalChat:
    # Batch 2, v100.004: keep the model resident after a turn so the next
    # turn does not pay a cold load. Ollama evicts on memory pressure
    # itself; this is a hint, not a reservation.
    KEEP_ALIVE = "30m"

    def __init__(self, route: Route, endpoint: str, timeout: float = 60, keep_alive: str = KEEP_ALIVE):
        if route.assistant != "local" or route.account or route.effort:
            raise ValueError("local adapter requires a local route without account or effort")
        if not isinstance(keep_alive, str) or not keep_alive or len(keep_alive) > 16:
            raise ValueError("invalid keep_alive")
        self.keep_alive = keep_alive
        url = urlsplit(endpoint)
        try:
            loopback = ipaddress.ip_address(url.hostname).is_loopback
        except (ValueError, TypeError):
            loopback = False
        if not loopback or url.scheme != "http" or url.username or url.password or url.path not in ("", "/") or url.query or url.fragment:
            raise ValueError("explicit HTTP loopback origin required")
        if not 0 < timeout <= 300:
            raise ValueError("invalid timeout")
        self.route = route
        self.endpoint = endpoint.rstrip("/") + "/api/chat"
        self.timeout = timeout
        # Do not forward chat through environment proxy configuration or redirects.
        from urllib.request import HTTPRedirectHandler
        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        self.client = build_opener(ProxyHandler({}), NoRedirect())

    def warmup(self):
        """Load the model now so the first real turn is not a cold load.

        Batch 2, v100.004. Best-effort by design: a failed warm-up only
        means the first turn pays the load (the old behaviour) — it must
        never fail service startup. Uses a one-word prompt; the reply is
        discarded. Returns True when the model answered.
        """
        try:
            body = json.dumps({"model": self.route.model, "stream": False,
                               "keep_alive": self.keep_alive,
                               "messages": [{"role": "user", "content": "ping"}]}).encode()
            with self.client.open(HttpRequest(self.endpoint, body, {"Content-Type": "application/json"}), timeout=self.timeout) as response:
                result = json.loads(response.read(2 * 1024 * 1024 + 1))
            return result.get("done") is True
        except Exception:
            return False

    def stream(self, request, on_delta=None):
        """Stream an Ollama reply, feeding text deltas to on_delta.

        Batch 2, v100.004: POSTs with "stream": true and parses the
        newline-delimited JSON SSE body, validating each chunk exactly
        like the non-streaming path (model match, assistant role, no
        tool calls). Returns the full Reply. A stream failure after
        some text arrived is raised as-is: the dispatcher records the
        turn unknown (delivery may have happened) and never replays it.
        With on_delta=None this behaves like __call__ but streamed.
        """
        if request.route != self.route:
            raise ValueError("local route mismatch")
        body = json.dumps({"model": self.route.model, "stream": True,
                           "keep_alive": self.keep_alive,
                           "messages": [{"role": "user", "content": request.text}]}).encode()
        try:
            response = self.client.open(HttpRequest(self.endpoint, body, {"Content-Type": "application/json"}), timeout=self.timeout)
        except HTTPError as error:
            if error.code == 404:
                raise RuntimeError("The selected local model is unavailable on this server.") from None
            raise
        text = ""
        done = False
        try:
            with response:
                buffer = b""
                while True:
                    chunk = response.read(8192)
                    if not chunk:
                        break
                    buffer += chunk
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        line = line.strip()
                        if not line:
                            continue
                        result = json.loads(line)
                        if result.get("model") not in (None, self.route.model):
                            raise ValueError("unexpected local model reply")
                        message = result.get("message", {})
                        if message.get("tool_calls"):
                            raise ValueError("unexpected local model reply")
                        delta = message.get("content") or ""
                        if delta:
                            text += delta
                            if on_delta is not None:
                                on_delta(text)
                        if result.get("done") is True:
                            done = True
                if buffer.strip():
                    result = json.loads(buffer)
                    if result.get("done") is True:
                        done = True
        except HTTPError as error:
            if error.code == 404:
                raise RuntimeError("The selected local model is unavailable on this server.") from None
            raise
        if not done or not text.strip():
            raise ValueError("empty local model reply")
        if len(text) > 2 * 1024 * 1024:
            raise ValueError("reply too large")
        return Reply(request.turn_id, request.route, text)

    def __call__(self, request):
        if request.route != self.route:
            raise ValueError("local route mismatch")
        body = json.dumps({"model": self.route.model, "stream": False,
                           "keep_alive": self.keep_alive,
                           "messages": [{"role": "user", "content": request.text}]}).encode()
        try:
            with self.client.open(HttpRequest(self.endpoint, body, {"Content-Type": "application/json"}), timeout=self.timeout) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
        except HTTPError as error:
            if error.code == 404:
                raise RuntimeError("The selected local model is unavailable on this server.") from None
            raise
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError("reply too large")
        result = json.loads(raw)
        message = result.get("message", {})
        if result.get("model") != self.route.model or result.get("done") is not True or message.get("role") != "assistant" or message.get("tool_calls"):
            raise ValueError("unexpected local model reply")
        text = message.get("content")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("empty local model reply")
        return Reply(request.turn_id, request.route, text)
