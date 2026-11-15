"""OpenAI-compatible local chat-completions transport; loopback only."""
import json
from urllib.error import HTTPError
from urllib.request import Request

from deskhand_dispatch import Reply
from deskhand_local import LocalChat


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate tool argument")
        result[key] = value
    return result


class OpenAILocalChat(LocalChat):
    def __init__(self, route, endpoint, timeout=60):
        super().__init__(route, endpoint, timeout)
        self.endpoint = endpoint.rstrip("/") + "/v1/chat/completions"

    def _request(self, messages, tools=None):
        payload = {"model": self.route.model, "messages": messages, "stream": False}
        if tools is not None:
            payload["tools"] = tools
        body = json.dumps(payload, ensure_ascii=True).encode()
        if len(body) > 2 * 1024 * 1024:
            raise ValueError("local request too large")
        try:
            with self.client.open(Request(self.endpoint, body, {"Content-Type": "application/json"}), timeout=self.timeout) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
        except HTTPError as error:
            if error.code == 404:
                raise RuntimeError("The local server endpoint or selected model is unavailable.") from None
            raise
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError("local response too large")
        result = json.loads(raw, object_pairs_hook=_unique)
        choices = result.get("choices") if isinstance(result, dict) else None
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            raise ValueError("unexpected local response")
        if result.get("model") != self.route.model:
            raise ValueError("local server returned a different model")
        message = choices[0].get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            raise ValueError("invalid local response")
        calls = message.get("tool_calls")
        if calls is not None:
            if not isinstance(calls, list) or len(calls) > 4:
                raise ValueError("invalid local tool calls")
            ids = set()
            for call in calls:
                if not isinstance(call, dict) or call.get("type") != "function" or not isinstance(call.get("id"), str) or not call["id"] or len(call["id"]) > 256 or call["id"] in ids:
                    raise ValueError("invalid local tool call identity")
                ids.add(call["id"])
                function = call.get("function")
                if not isinstance(function, dict) or not isinstance(function.get("arguments"), str):
                    raise ValueError("invalid local tool call")
                function["arguments"] = json.loads(function["arguments"], object_pairs_hook=_unique,
                    parse_constant=lambda _: (_ for _ in ()).throw(ValueError("invalid number")))
        return message

    def stream(self, request, on_delta=None):
        """Stream an OpenAI-compatible reply (SSE deltas), Batch 2.

        Parses `data: {...}` lines; a `data: [DONE]` line ends the
        stream. Each chunk must name this model (when present) and
        carry no tool calls — a chunk with tool calls is rejected, so
        a streamed tool call can never slip past the adapter. Returns
        the full Reply; on_delta receives the accumulated text.
        """
        if request.route != self.route:
            raise ValueError("local route mismatch")
        payload = {"model": self.route.model,
                   "messages": [{"role": "user", "content": request.text}], "stream": True}
        body = json.dumps(payload, ensure_ascii=True).encode()
        try:
            response = self.client.open(Request(self.endpoint, body, {"Content-Type": "application/json"}), timeout=self.timeout)
        except HTTPError as error:
            if error.code == 404:
                raise RuntimeError("The local server endpoint or selected model is unavailable.") from None
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
                        if not line.startswith(b"data:"):
                            continue
                        data = line[5:].strip()
                        if data == b"[DONE]":
                            done = True
                            continue
                        result = json.loads(data, object_pairs_hook=_unique)
                        if result.get("model") not in (None, self.route.model):
                            raise ValueError("local server returned a different model")
                        choices = result.get("choices")
                        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
                            raise ValueError("unexpected local response")
                        delta = choices[0].get("delta") or {}
                        if delta.get("tool_calls"):
                            raise ValueError("unexpected local model reply")
                        piece = delta.get("content") or ""
                        if piece:
                            text += piece
                            if on_delta is not None:
                                on_delta(text)
                        if choices[0].get("finish_reason") is not None:
                            done = True
        except HTTPError as error:
            if error.code == 404:
                raise RuntimeError("The local server endpoint or selected model is unavailable.") from None
            raise
        if not done or not text.strip():
            raise ValueError("unexpected local model reply")
        return Reply(request.turn_id, request.route, text)

    def __call__(self, request):
        if request.route != self.route:
            raise ValueError("local route mismatch")
        message = self._request([{"role": "user", "content": request.text}])
        text = message.get("content")
        if message.get("tool_calls") or not isinstance(text, str) or not text.strip():
            raise ValueError("unexpected local model reply")
        return Reply(request.turn_id, request.route, text)


class OpenAILocalToolTransport:
    def __init__(self, chat):
        self.chat = chat

    def __call__(self, messages, tools):
        converted = []
        pending = []
        deferred_images = []
        for message in messages:
            role = message.get("role")
            if role == "assistant" and message.get("tool_calls"):
                calls = []
                for call in message["tool_calls"]:
                    function = call["function"]
                    calls.append({"id": call["id"], "type": "function", "function": {"name": function["name"], "arguments": json.dumps(function["arguments"], ensure_ascii=False)}})
                    pending.append((call["id"], function["name"]))
                converted.append({"role": "assistant", "content": message.get("content"), "tool_calls": calls})
            elif role == "tool":
                if not pending or pending[0][1] != message.get("tool_name"):
                    raise ValueError("tool result does not match model call")
                call_id, _ = pending.pop(0)
                converted.append({"role": "tool", "tool_call_id": call_id, "content": message["content"]})
                if not pending and deferred_images:
                    converted.extend(deferred_images)
                    deferred_images.clear()
            elif role == "user" and message.get("images"):
                image_message = {"role": "user", "content": [{"type": "text", "text": message.get("content", "")}] +
                    [{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + image}} for image in message["images"]]}
                (deferred_images if pending else converted).append(image_message)
            else:
                converted.append({"role": role, "content": message.get("content", "")})
        if pending:
            raise ValueError("missing model tool result")
        response = self.chat._request(converted, tools)
        # Some compatible servers occasionally return an empty completion.
        # No action has been dispatched at this point, so one fresh generation
        # is safe; never retry a response containing any tool calls.
        if not response.get("tool_calls") and not str(response.get("content") or "").strip():
            response = self.chat._request(converted, tools)
        return response
