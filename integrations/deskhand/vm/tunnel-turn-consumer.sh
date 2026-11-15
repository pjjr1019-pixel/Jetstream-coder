#!/usr/bin/env bash
# Tunnel turn consumer hook (B42 chat tunnel, latency fix 2026-11-12,
# ping fast path 2026-11-12 round 2, greeting fast path + stale
# re-wake throttle 2026-11-12 round 3).
#
# Watches the VM-side turn inbox (turns/*.json) every 5s and wakes a
# worker the moment a chat turn from Phil lands, instead of waiting for
# the 1-minute consumer cron. The worker answers the turn as Jett and
# writes the reply to replies/; the poll loop posts it to the agent.
#
# Ping fast path: an exact "ping" turn (case/whitespace-insensitive)
# is answered by the hook itself — the pong reply is written atomically
# to replies/<id>.json and the claim is deleted, with NO worker wake.
# A worker spin-up costs ~18s, so routing pings around it cuts the
# round trip from ~33s to roughly one hook poll + one loop poll
# (~5-8s typical). Non-ping turns still wake a worker as before.
# (Deskhand chat, Phil's requirement 2026-11-12: a reply in a few
# seconds, not 30. This is the approved fast path for trivial replies.)
#
# Greeting fast path (round 3): exact "hi", "hey", "hello", "yo"
# (case/whitespace-insensitive, optional trailing . ! or ?) is
# answered the same way, with "Hey! I'm here — what do you need?"
# A greeting needs no worker either — it is a hello, not a task.
#
# Claim protocol (no turn ever lost, never double-answered):
# - A turn is CLAIMED by creating a durable no-clobber hard link from
#   turns/ to turns-claimed/. The inbox copy remains until digest-checked
#   completed cleanup. The one-minute consumer cron must stay disabled;
#   otherwise it could consume the retained inbox copy independently.
# - The worker answers the claimed file in turns-claimed/ and deletes
#   it ONLY after the reply file is written to replies/.
# - A fast-path ping/greeting deletes its claim ONLY after the reply
#   file is written (or if a reply file already exists for it). If the
#   reply write fails, the claim is left for the worker.
# - A claimed file older than STALE_SECS (300s) is uncertain, not proof
#   that its worker died. It is retained for operator review and never
#   automatically re-woken. An already-running worker is not stopped.
# - Dry runs (HATCH_HOOK_DRY_RUN=1) never move files, never write
#   replies, never delete claims, never touch markers: they report
#   what they WOULD wake for, so a dry run cannot consume a detection.
set -euo pipefail
source "$HATCH_HOOK_RUNTIME"

STATE_DIR="${TUNNEL_STATE_DIR:-$HOME/workspace/deskhand-coordinator/tunnel-state}"
TURNS_DIR="$STATE_DIR/turns"
CLAIMED_DIR="$STATE_DIR/turns-claimed"
REPLIES_DIR="$STATE_DIR/replies"
PUBLISH_HELPER="${TUNNEL_PUBLISH_HELPER:-$HOME/workspace/deskhand-coordinator/100050-tunnel-build/work/vm/publish_reply.py}"
CLAIM_HELPER="${TUNNEL_CLAIM_HELPER:-$HOME/workspace/deskhand-coordinator/100050-tunnel-build/work/vm/claim_turn.py}"
CLEANUP_HELPER="${TUNNEL_CLEANUP_HELPER:-$HOME/workspace/deskhand-coordinator/100050-tunnel-build/work/vm/inbox_cleanup.py}"
STALE_SECS=300
PONG_REPLY="pong — tunnel's up, I hear you loud and clear."
GREETING_REPLY="Hey! I'm here — what do you need?"

mkdir -p "$CLAIMED_DIR" "$REPLIES_DIR"

dry_run=0
if [ "${HATCH_HOOK_DRY_RUN:-0}" = "1" ]; then
  dry_run=1
fi

# Slow or disconnected turns stay visible in the operator report, while
# repeated five-second hook polls do not flood logs with identical warnings.
stale_notice() {
  local id="$1" message="$2" now marker last
  if [ "$dry_run" -eq 1 ]; then
    return
  fi
  marker="$STATE_DIR/.stale-notice-$id"
  now="$(date +%s)"
  last="$(stat -c %Y "$marker" 2>/dev/null || echo 0)"
  if [ $(( now - last )) -ge 300 ]; then
    touch "$marker"
    log "$message" "{\"turn_id\":\"$id\"}"
  fi
}

# Recover cleanup captures left by a crash before considering new turns.
if [ "$dry_run" -eq 0 ] && compgen -G "$STATE_DIR/inbox-cleanup/*/*.json" >/dev/null; then
  python3 "$CLEANUP_HELPER" --sweep "$STATE_DIR" >/dev/null 2>&1 || true
fi

# fast_kind <turn-file>: echo "ping" if the turn's text is exactly
# "ping" (case/whitespace-insensitive), "greeting" if it is exactly
# "hi", "hey", "hello" or "yo" (case/whitespace-insensitive, optional
# trailing . ! or ?), else exit 1. Read-only; safe in dry runs.
fast_kind() {
  python3 - "$1" <<'PY'
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as f:
        turn = json.load(f)
except Exception:
    sys.exit(1)
text = turn.get("text", "")
if not isinstance(text, str):
    sys.exit(1)
norm = text.strip().lower()
if norm == "ping":
    print("ping")
    sys.exit(0)
if norm and norm[-1] in ".!?":
    norm = norm[:-1].strip()
if norm in ("hi", "hey", "hello", "yo"):
    print("greeting")
    sys.exit(0)
sys.exit(1)
PY
}

# answer_fast <id> <claimed-file> <reply>: write the reply atomically
# to replies/<id>.json (temp file without a .json suffix, then rename
# into place, so the loop never reads a partial file), then return 0.
# Returns 2 if a reply file already exists for the turn (already
# answered — the caller should just drop the claim). Any other
# non-zero return means the write failed; the caller must leave the
# claim for the worker.
answer_fast() {
  local id="$1" claimed="$2" reply="$3"
  TUNNEL_PUBLISH_HELPER_ACTIVE="$PUBLISH_HELPER" python3 - "$id" "$claimed" "$REPLIES_DIR" "$reply" <<'PY'
import importlib.util, json, os, sys
from pathlib import Path
tid, _claimed, replies, text = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
payload = {"turn_id": tid, "reply": text}
helper = os.environ['TUNNEL_PUBLISH_HELPER_ACTIVE']
spec = importlib.util.spec_from_file_location('tunnel_publish', helper)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
try:
    outcome = module.publish(replies, payload)
except OSError:
    # Even if conflict evidence cannot be saved, an existing first reply
    # is enough reason to hold the claim rather than execute the turn again.
    if (Path(replies) / (tid + '.json')).exists():
        sys.exit(3)
    raise
if outcome == 'conflict':
    sys.exit(3)
sys.exit(0)
PY
}

# handle_claimed_fast <id> <claimed-file>: fast-path one claimed turn.
# Echoes "handled:ping" / "handled:greeting" if it was answered or
# already answered (and the caller may delete the claim), else
# "worker" (leave it for a worker). In dry runs, echoes
# "would-handle:ping" / "would-handle:greeting" or "worker" and
# touches nothing.
handle_claimed_fast() {
  local id="$1" claimed="$2"
  local kind
  kind="$(fast_kind "$claimed")" || { echo "worker"; return 0; }
  if [ "$dry_run" -eq 1 ]; then
    echo "would-handle:$kind"
    return 0
  fi
  local rc=0
  if [ "$kind" = "ping" ]; then
    answer_fast "$id" "$claimed" "$PONG_REPLY" || rc=$?
  else
    answer_fast "$id" "$claimed" "$GREETING_REPLY" || rc=$?
  fi
  if [ "$rc" -eq 0 ] || [ "$rc" -eq 2 ]; then
    echo "handled:$kind"
  elif [ "$rc" -eq 3 ]; then
    echo "conflict"
  else
    echo "worker"
  fi
  return 0
}

# note_fast <outcome> <id>: record a fast-path outcome in the right
# array ("handled:ping" / "would-handle:ping" -> fast_ids,
# "handled:greeting" / "would-handle:greeting" -> greet_ids).
# Returns 0 if the outcome was a fast-path one, else 1.
note_fast() {
  case "$1" in
    handled:ping|would-handle:ping) fast_ids+=("$2"); return 0 ;;
    handled:greeting|would-handle:greeting) greet_ids+=("$2"); return 0 ;;
    *) return 1 ;;
  esac
}

# Claim new turns waiting in turns/
new_ids=()
fast_ids=()
greet_ids=()
for f in "$TURNS_DIR"/*.json; do
  [ -e "$f" ] || continue
  id="$(basename "$f" .json)"
  if [ -e "$STATE_DIR/reply-completed/$id.json" ]; then
    if [ "$dry_run" -eq 0 ]; then
      if python3 "$CLEANUP_HELPER" --turn "$STATE_DIR" "$id" >/dev/null 2>&1; then
        rm -f "$STATE_DIR/.cleanup-warning-$id"
      elif [ ! -e "$STATE_DIR/.cleanup-warning-$id" ]; then
        touch "$STATE_DIR/.cleanup-warning-$id"
        log "completed inbox needs manual review" "{\"turn_id\":\"$id\"}"
      fi
    fi
    continue
  fi
  if [ -e "$CLAIMED_DIR/$id.json" ] || [ -e "$REPLIES_DIR/$id.json" ] || \
     compgen -G "$STATE_DIR/reply-claims/*/$id.json" >/dev/null || \
     [ -e "$STATE_DIR/reply-completed/$id.json" ] || \
     compgen -G "$STATE_DIR/replies-conflicts/*/$id.json" >/dev/null || \
     compgen -G "$STATE_DIR/replies-dead/*/$id.json" >/dev/null; then
    # Normal in-flight entries are revisited on every hook poll; the
    # operator report exposes unresolved entries without log flooding.
    continue
  fi
  if [ "$dry_run" -eq 1 ]; then
    outcome="$(handle_claimed_fast "$id" "$f")"
    if ! note_fast "$outcome" "$id"; then
      new_ids+=("$id")
    fi
  else
    # Durable no-clobber claim; the inbox copy stays for safe reconciliation.
    if python3 "$CLAIM_HELPER" "$f" "$CLAIMED_DIR/$id.json" 2>/dev/null; then
      outcome="$(handle_claimed_fast "$id" "$CLAIMED_DIR/$id.json")"
      case "$outcome" in
        handled:ping)
          rm -f "$CLAIMED_DIR/$id.json"
          fast_ids+=("$id")
          log "ping fast-path answered" "{\"turn_id\":\"$id\"}"
          ;;
        handled:greeting)
          rm -f "$CLAIMED_DIR/$id.json"
          greet_ids+=("$id")
          log "greeting fast-path answered" "{\"turn_id\":\"$id\"}"
          ;;
        conflict)
          log "fast-path reply conflict requires review" "{\"turn_id\":\"$id\"}"
          ;;
        *) new_ids+=("$id") ;;
      esac
    fi
  fi
done

# Clean up orphan re-wake markers: a marker whose claim file is gone
# (its turn was answered and the claim deleted) is dead weight.
# Dry runs never touch markers.
if [ "$dry_run" -eq 0 ]; then
  for m in "$CLAIMED_DIR"/.*.rewoke; do
    [ -e "$m" ] || continue
    mid="$(basename "$m" .rewoke)"
    mid="${mid#.}"
    [ -e "$CLAIMED_DIR/$mid.json" ] || rm -f "$m"
  done
  for m in "$STATE_DIR"/.stale-notice-*; do
    [ -e "$m" ] || continue
    mid="$(basename "$m")"
    mid="${mid#.stale-notice-}"
    [ -e "$CLAIMED_DIR/$mid.json" ] || rm -f "$m"
  done
fi

# Stale claims: claimed but unanswered for over STALE_SECS.
stale_ids=()
now="$(date +%s)"
for f in "$CLAIMED_DIR"/*.json; do
  [ -e "$f" ] || continue
  mtime="$(stat -c %Y "$f")"
  if [ $(( now - mtime )) -gt "$STALE_SECS" ]; then
    id="$(basename "$f" .json)"
    # Publication, private claim, and durable completion overlap in that
    # order. Check in the same order so a reply cannot move between checks
    # and make a completed turn appear unanswered.
    if [ -e "$REPLIES_DIR/$id.json" ] || \
       compgen -G "$STATE_DIR/reply-claims/*/$id.json" >/dev/null || \
       [ -e "$STATE_DIR/reply-completed/$id.json" ]; then
      stale_notice "$id" "reply already present or delivered; claim retained for review"
      continue
    fi
    # A conflicting reply needs human reconciliation; waking another
    # worker cannot resolve it and can create an unbounded conflict pile.
    if compgen -G "$STATE_DIR/replies-conflicts/*/$id.json" >/dev/null; then
      stale_notice "$id" "reply conflict requires review"
      continue
    fi
    # A slow or disconnected worker can still be executing. There is no
    # durable worker-ownership proof that would make re-waking safe.
    # Keep the claim, flag it, and require explicit operator review.
    stale_notice "$id" "stale claim outcome uncertain; no automatic re-wake"
  fi
done

all_ids=("${new_ids[@]}" "${stale_ids[@]}")
if [ "${#fast_ids[@]}" -gt 0 ]; then
  fast_payload="{\"turn_ids\":[$(printf '"%s",' "${fast_ids[@]}" | sed 's/,$//')]}"
  if [ "$dry_run" -eq 1 ]; then
    # Dry runs log no per-ping line (nothing was written), so this is
    # the only record of the pings the fast path would handle.
    log "ping fast-path would handle (dry run, nothing written)" "$fast_payload"
  elif [ "${#all_ids[@]}" -eq 0 ]; then
    log "ping fast-path handled all turns, no worker needed" "$fast_payload"
  fi
fi
if [ "${#greet_ids[@]}" -gt 0 ]; then
  greet_payload="{\"turn_ids\":[$(printf '"%s",' "${greet_ids[@]}" | sed 's/,$//')]}"
  if [ "$dry_run" -eq 1 ]; then
    # Dry runs log no per-greeting line (nothing was written), so this
    # is the only record of the greetings the fast path would handle.
    log "greeting fast-path would handle (dry run, nothing written)" "$greet_payload"
  elif [ "${#all_ids[@]}" -eq 0 ]; then
    log "greeting fast-path handled all turns, no worker needed" "$greet_payload"
  fi
fi
if [ "${#all_ids[@]}" -eq 0 ]; then
  silent "no turns waiting" '{}'
else
  payload="$(python3 -c 'import json,sys; print(json.dumps({"turn_ids": sys.argv[1:]}))' "${all_ids[@]}")"
  if [ "$dry_run" -eq 1 ]; then
    log "turns waiting (dry run, not claimed)" "$payload"
  else
    log "turns claimed" "$payload"
  fi
  wake "chat turn(s) waiting" "$payload"
fi
