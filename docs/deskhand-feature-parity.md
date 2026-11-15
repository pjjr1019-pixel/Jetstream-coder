# Deskhand feature parity and acceptance — V0.001

Updated 2026-11-15. This matrix distinguishes preserved implementation, tests and live acceptance. It does not declare every external integration operational.

| Feature | Reused implementation / Jetstream surface | Evidence | Remaining acceptance |
|---|---|---|---|
| Embedded network, encrypted phone link | Original native node, relay and secure session; Settings → Phone connection | Live Running, existing phone reconnects to new host | Fresh physical QR ceremony; mobile-network/off-site |
| QR and matching-code pairing approval | Original HostEngine pairing; approval decisions inside Settings | Desktop fixture; live QR display; existing pairing retained | Scan/approve/reject a new physical pairing |
| LAN/configured relay pairing | Original engine QR generation; configured relay button | Native build; existing transport tests | Live configured-relay pairing |
| Pause, disconnect, per-phone control/revoke | Original engine methods | Desktop tests; earlier real-phone job/connection test | Full revoke/re-pair acceptance |
| Follow phone orientation | Unmodified PhoneDisplay.cs and original phone START orientation payload; Settings toggle | Real PC measured 900×1600 with phone portrait, 1600×900 with phone landscape; phone hierarchy rotation=1 | Touch mapping during both modes and explicit Android Stop button |
| Display cleanup | Original disconnect and PhoneDisplay restoration | Host disconnect leaves PC landscape 1600×900; original follow=false and phone auto-rotate=1/user_rotation=0 restored | Lock/sleep and failure recovery |
| Left sliding file drawer | Original ViewerLayout/DesktopBrowserController | Real phone opens This PC, Desktop, C:\; resize handle moved x506→703 | Edge swipe, search/open confirmation in both orientations |
| Right sliding agents drawer | Original AgentsDrawer.kt | Real phone opens Agents and Board tabs; resize handle moved x506→335 | Edge swipe and complete live-feed data |
| Agents, worker and message-board data | Original runtime-stream/polling pipeline; also desktop Runtime section | UI and tests; phone correctly reports feed unavailable | External source feed is missing; do not present empty/unknown as live data |
| Viewer Fit/Stretch/Zoom, input/keyboard/pinch/pan | Original Android viewer code retained | Existing Android regression suite | Full real-device gesture/touch acceptance |
| Foreground-window actions and Stop | Original ForegroundWindowButtons/controller; original Stop octagon remains last | Existing Android tests plus new toolbar test | Disposable-window real-device acceptance |
| Composer, chat history and voice | Original Android code retained | New Jobs no-overlap regression; composer fully visible on device | Live voice/reply/audio and routed chat acceptance |
| Jetstream jobs | Existing encrypted Deskhand chat relay + authenticated loopback Job API | Earlier phone submit/pause/resume/cancel checked actual API state | Actual coding coordinator availability; Apply stays unavailable |
| Desktop assistant/model/effort/routing/local model | Original AssistantClient/Python operations exposed in expanded Settings | Real catalog returned 9 routes; fixture routing/history/send-error tests | Real model chat/reply and local runtime configuration |
| Desktop assistant history, partials and connector attribution | Original service history schema; rendered turn/reply/action rows | Fixture includes original connector reply; no automatic retry on uncertain send | Actual streamed partial/connector delivery |
| Muse sign-in, bridge, permissions, folders and timed sessions | Original service operations with original confirmation fields; Settings forms | Live status page; permission payload fixture; no grants applied in test | Actual sign-in, intentional permission/session changes and remote reply |
| Desktop activity/events/board/timing | Original form cached models, single original service feed | Live snapshot and fixture full detail/timing test | Full external feed freshness |
| Twelve-component diagnostics / filtered export | Original health rows and diagnostics_export operation | Live all 12 rows; native boundary tests | Export-save acceptance and real filtering with available source logs |
| Windows sign-in startup | Jetstream app login-item API; no old Deskhand registry overwrite | Implemented, types/build pass | Intentional startup enable/disable and reboot test |
| Embedded backend/runtime assets | Original imported C#/Python/Go DLL/Android AAR/dependencies packaged under Jetstream | Timestamped bundle manifest and builds | Embedded service code resolves locally; shared account availability remains separate |

## Current outputs

- Latest bundle: `C:\Users\Phlip\Desktop\Jetstream Coder\artifacts\Jetstream-V0.001-1794719656536\Jetstream\electron.exe`. Desktop shortcut updated; app launched.
- Native tests: 10/10 new isolated boundary/settings checks.
- Jetstream tests: 42/42 full suite.
- Android tests: 357/357 full unit/Compose suite; updated verification APK installed preserving pairing.
- Actual new host live smoke: features loaded, all twelve diagnostics, nine real catalog routes, Muse status controls, no nonempty renderer alert. Concurrent status returned in 4ms while opening service UI.

## Concrete external/baseline limitations

The phone agents panel reports `Agents feed not connected` and unknown runtime sources. A local connected worker heartbeat is not proof of a live Muse reply or complete subagent feed. Previously queued Muse tunnel turn `2ae38c8a-6d8a-426d-8f06-4a5d8b20645a` was not consumed/resubmitted. The remote Linux claimant/source runtime must be restored/verified for this acceptance.

The imported Android debug build displays an Android 16KB-native-library alignment notice for libgojni.so and libandroidx.graphics.path.so. Acknowledged OK for verification; did not choose Don't Show Again. Original native dependencies were reused, so this remains a debug/release compatibility issue. It was not fixed or hidden.

The phone foreground changed during two checks (user activity/notification shade). Tests did not inspect unrelated app content. Orientation settings were restored afterward: accelerometer_rotation=1 and user_rotation=0. Host Follow phone orientation was returned to its original false value. No permanent phone-setting change is intended.

The imported Task Agent Python adapters still have a standalone source-folder default. The separate Jetstream Task Agent manager deliberately attaches to the existing shared agent using a scoped credential and never stops it. Packaging excludes credentials. Do not remove this shared ownership/account boundary or copy full-control secrets merely to make diagnostics green.

## Embedded path follow-up

The Task Agent source-folder fallback is now removed for the embedded service: AssistantClient supplies DESKHAND_TASK_AGENT_DIR from the nearest bundled/check-out TaskAgent directory. If the bundle is incomplete it supplies the expected bundle-local path, so it fails missing rather than executing standalone source code. Both Python adapters honor this override while preserving explicit constructor paths/token sources. Two new unittest checks passed; original client script14/14 and lifecycle script21/21 passed. These original tests are executable scripts, not pytest functions (a pytest attempt collected none; corrected to execute the scripts). No credentials copied, no remote-control flag changed, no shared agent stopped. The separately configured shared chat credential and external VM/feed availability remain distinct concerns.
