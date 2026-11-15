"""Single source of truth for the Tailscale addresses JettPad pins.

The PC address and the paired-phone address were previously hard-coded as
literals in five places (companion_data, away_readiness, companion_service,
sunshine/host/runtime, sunshine/host/admission) plus display strings in
sunshine/host/ui. Every consumer now imports from here.

PROPER SOURCE STILL MISSING: these are the last-known tailnet addresses, not
discovered values. The PC's own address IS discoverable (``tailscale status``
/ ``musebridge.config.tailscale_address`` + ``musebridge.cli`` discovery);
no discovery path exists for the phone address — no peer name or DNS name
for the phone is recorded anywhere in the tree. If either address changes,
update the two constants below and re-verify pairing, readiness probing,
and Sunshine admission. Do not re-scatter literals.
"""

PC_TAILSCALE_IP = '100.123.38.60'
PHONE_TAILSCALE_IP = '100.107.53.70'
