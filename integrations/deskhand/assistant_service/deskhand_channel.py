"""Deskhand-owned structured channel, v100.008 (Batch 3, smarts #2).

B2-D (100.030): the parse/validate path was removed -- it was test-only
(parse_channel_message, validate_grants, encode, new_id, and their
field/grant constants and helpers). Nothing in production ever called
it: the typed channel never replaced chat JSON, and the only production
importer (deskhand_plan) is itself removed. What remains is the small
surface production actually used: CAPABILITIES and grant_for().

Safety note: the STOP gates that used to live in validate_grants /
parse_channel_message were unenforced anywhere in production (the
parser had no production caller), so removing them removes no live
enforcement. Grants are enforced by the host, not by this module.
"""
from __future__ import annotations

__version__ = "100.008"

CAPABILITIES = frozenset({"filesystem", "window", "pointer", "accessibility", "command", "clipboard"})


def grant_for(grants, capability):
    for grant in grants:
        if grant["capability"] == capability:
            return grant
    return None
