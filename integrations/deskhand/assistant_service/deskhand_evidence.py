"""Host-enforced verification evidence, v100.008 (Batch 3, smarts #4).

A receipt carries machine-checkable evidence for its action kind, and
`done/completed` requires it. A bare "launched" / "ok: true" is not
evidence. Evidence is host-observed state, never AI-asserted.

Rules:
- Missing evidence for a completed action means the outcome is
  UNCERTAIN (terminal, never upgraded, never replayed). Callers use
  downgrade_status() to enforce that.
- Evidence never carries file contents, clipboard contents, or secrets:
  for create/write/type it carries a read-back MATCH flag + length, not
  the text itself. (A read action's own data is the exception: the host
  receipt data already carries the read text; this module does not add
  to it.)
"""
from __future__ import annotations

__version__ = "100.008"


def _is_int(value):
    return type(value) is int


def build_evidence(action_name, observation):
    """Build the evidence dict for an action from a host observation dict.
    Returns {"verified": bool, "kind": str, "detail": dict}. `verified`
    is True only when the observation proves the effect happened."""
    if not isinstance(observation, dict):
        return {"verified": False, "kind": action_name, "detail": {"error": "no observation"}}
    if action_name == "filesystem.launch" or action_name == "app.launch_by_name":
        pid = observation.get("pid")
        detail = {"pid": pid, "process_name": observation.get("process_name"),
                  "window_title": observation.get("window_title"),
                  "window_handle": observation.get("window_handle")}
        verified = _is_int(pid) and pid > 0 and bool(observation.get("process_name"))
        return {"verified": verified, "kind": "launch", "detail": detail}
    if action_name in {"filesystem.create", "filesystem.replace"}:
        detail = {"read_back_match": observation.get("read_back_match") is True,
                  "length": observation.get("length")}
        verified = detail["read_back_match"] and _is_int(detail["length"])
        return {"verified": verified, "kind": "write", "detail": detail}
    if action_name == "control.type":
        detail = {"control_text_match": observation.get("control_text_match") is True,
                  "length": observation.get("length")}
        verified = detail["control_text_match"]
        return {"verified": verified, "kind": "type", "detail": detail}
    if action_name in {"filesystem.move", "filesystem.copy", "filesystem.move_folder"}:
        detail = {"destination_contains": observation.get("destination_contains") is True,
                  "destination": observation.get("destination")}
        verified = detail["destination_contains"]
        return {"verified": verified, "kind": "move_copy", "detail": detail}
    if action_name in {"screen.click", "screen.double_click", "control.invoke"}:
        detail = {"post_observe_changed": observation.get("post_observe_changed") is True,
                  "focused": observation.get("focused")}
        verified = detail["post_observe_changed"]
        return {"verified": verified, "kind": "invoke", "detail": detail}
    if action_name == "command.run":
        detail = {"exit_code": observation.get("exit_code"),
                  "output_present": bool(observation.get("output"))}
        # B2-D M6: only exit code 0 is verified success. The old check
        # (_is_int(exit_code)) counted ANY observed exit code -- even
        # 1 -- as verified, so a failed command was indistinguishable
        # from a successful one in receipts/skills/benchmark scoring.
        # The actual code is still recorded in the detail.
        verified = _is_int(detail["exit_code"]) and detail["exit_code"] == 0
        return {"verified": verified, "kind": "command", "detail": detail}
    # 100.030 (A25, G5): four new kinds, each with its own branch BEFORE
    # the read fall-through below, so the fall-through no longer catches
    # these ops. A claim without the read-back is never verified.
    if action_name == "screen.move":
        detail = {"cursor_match": observation.get("cursor_match") is True,
                  "x": observation.get("x"), "y": observation.get("y"),
                  "tolerance_px": observation.get("tolerance_px")}
        verified = detail["cursor_match"]
        return {"verified": verified, "kind": "pointer", "detail": detail}
    if action_name == "screen.drag":
        detail = {"start_match": observation.get("start_match") is True,
                  "end_match": observation.get("end_match") is True,
                  "post_observe_changed": observation.get("post_observe_changed") is True}
        verified = detail["start_match"] and detail["end_match"] and detail["post_observe_changed"]
        return {"verified": verified, "kind": "drag", "detail": detail}
    if action_name == "screen.scroll":
        detail = {"position_changed": observation.get("position_changed") is True}
        verified = detail["position_changed"]
        return {"verified": verified, "kind": "scroll", "detail": detail}
    if action_name in {"control.key", "control.key_down", "control.key_up"}:
        # A key op is never verified on "sent" alone. A non-observable
        # op (e.g. a modifier-only chord) is verified False with
        # observable False, and its status is uncertain, not completed.
        # `observable` defaults to True when the host omits it: the
        # receipt shape always carries it, and absence must not
        # silently downgrade an otherwise observed key press.
        detail = {"sent": observation.get("sent") is True,
                  "post_observe_changed": observation.get("post_observe_changed") is True,
                  "observable": observation.get("observable", True) is True}
        verified = detail["sent"] and detail["post_observe_changed"] and detail["observable"]
        return {"verified": verified, "kind": "key", "detail": detail}
    # Reads / lists / observes: the payload itself is the evidence.
    detail = {"data_present": observation.get("data_present") is True,
              "count": observation.get("count")}
    return {"verified": detail["data_present"], "kind": "read", "detail": detail}


def downgrade_status(status, evidence):
    """A completed action without verified evidence is uncertain.
    Every other status passes through unchanged."""
    if status == "completed" and not evidence.get("verified"):
        return "uncertain"
    return status


def receipt_is_verified(receipt):
    """True only for a completed receipt whose data carries verified
    evidence. Used by skills and the benchmark to catch false success."""
    if not isinstance(receipt, dict) or receipt.get("status") != "completed":
        return False
    evidence = (receipt.get("data") or {}).get("evidence")
    return isinstance(evidence, dict) and evidence.get("verified") is True
