# Jetstream Job API v1

Jetstream exposes a local HTTP interface for a native Deskhand host adapter. It reads Jetstream's job pipeline; Deskhand must not open Jetstream's SQLite files directly.

## Listener and credential

- The API binds only to `127.0.0.1`; it is not exposed on LAN or Tailscale.
- The versioned base path is `/api/v1`. Responses include `X-Jetstream-Job-API-Version: 1`.
- Requests require `Authorization: Bearer <token>`. Requests from non-loopback peers, requests without valid authorization, unsupported versions, and unknown actions are rejected.
- Jetstream writes a companion descriptor (`deskhand-job-api.json`) in its Electron `userData` directory. It contains the current endpoint, actor name, and bearer token so a same-user native Deskhand host can connect. The descriptor is not sent to the renderer or written to application logs. On non-Windows systems the file is owner-only; on Windows it inherits the `userData` directory ACL. The Windows ACL has not been independently verified, and same-user processes are within this trust boundary.
- The listener chooses an available loopback port at startup and updates the descriptor. A gracefully closed app removes its descriptor. After a crash, Deskhand must treat a stale endpoint as unavailable and reread the descriptor after Jetstream restarts.

## Read operations

All paths below are relative to `/api/v1`.

- `GET /` — API version and active project summary.
- `GET /projects` — zero or one currently open project; returns a hashed project ID and directory name, not the absolute path.
- `GET /jobs?query=...` — jobs, status, and global paused state.
- `GET /jobs/{jobId}` — job summary, ordered history, latest candidate metadata and diff, independent review, candidate check evidence, blocked reasons, and receipt. Configured check commands and output streams are excluded. Candidate diffs, review findings, job requests, and receipts contain project data and may contain local paths; treat them as sensitive.
- `GET /actions/{requestId}` — durable action outcome for the authenticated host.

## Actions

`POST /actions` requires `X-Jetstream-Project: <project.id>` from the last status snapshot and accepts JSON with a stable UUID `requestId` and one supported action. The project ID is included in the durable request fingerprint; new actions against a stale project receive HTTP 409. Existing outcomes are queried by request ID without changing their target. Read requests can also provide this header to reject stale project views.

Actions:

```json
{"requestId":"<uuid>","action":"submit","request":"<job request>"}
{"requestId":"<uuid>","action":"pause"}
{"requestId":"<uuid>","action":"resume"}
{"requestId":"<uuid>","action":"cancel","jobId":12}
{"requestId":"<uuid>","action":"reject","candidateId":34}
```

The authenticated principal is recorded as `deskhand-host`; callers cannot select an actor. A repeated request with the same actor, UUID, and action body returns the saved outcome without dispatching again. Reusing the UUID with a different body returns HTTP 409. An action left in progress after a restart is marked `uncertain`, can be queried by UUID, and is never automatically replayed. A client seeing `uncertain` must inspect job history before deciding what to do next; it must not retry under a new UUID blindly.

Successful job-specific remote actions also add `deskhand_action` entries to Jetstream job history with actor, action, request UUID, and outcome. Pause/resume events are recorded against each existing job; the separate action ledger preserves actions when there are no jobs.

## Safety and current limitations

- No approval, Apply, rollback, arbitrary command, file write, or agent ownership endpoint exists. Phone approval and production Apply remain unavailable.
- Job submission records a request in Jetstream; it does not start an AI worker. The current pipeline may show `blocked: coordinator unavailable`.
- This is a local same-user host interface, not a phone-facing public API. The Deskhand host must proxy selected data/actions over its existing authenticated relay; it must not forward the bearer token to Android or the renderer.
- The token file relies on the OS user-data directory ACL on Windows. Do not copy it into Deskhand logs, reports, source control, Android storage, or a relay payload. The Deskhand development companion implements this contract in its native Windows host. The Android panel uses the paired Deskhand relay.
- Fixture tests verify authentication, versioning, reads, allowed actions, action history, duplicate/reconnect behavior, and uncertain outcomes. They do not prove protection against a compromised same-user process or establish power-loss durability.
