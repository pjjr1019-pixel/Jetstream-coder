# Deskhand → Jetstream Coder handoff

Updated November 15, 2026 (America/New_York). User resumed integration after requesting this handoff; it records the exact state at that transition.

## Goal and constraints

User wants every standalone Deskhand feature preserved inside Jetstream Coder, including phone orientation/resolution adjustments, both sliding/resizable drawers, streaming/input, chat/history/voice, runtime/agent feeds and desktop settings. Most desktop UI belongs under Jetstream Settings. Top-right phone icon opens Settings → Phone connection directly. Reuse original Deskhand code and keep backend/runtime files within Jetstream's folder. Keep Jetstream V0.001. Do not claim complete feature parity from compilation or fixtures. Preserve existing unrelated/uncommitted work. No commit, push, tag change or production Apply enablement is authorized by these changes. Existing user authorization covers building, packaging, updating the verification Android app and testing the embedded host/phone workflow.

## Locations

- Repository: `C:\Users\Phlip\Desktop\Jetstream Coder`
- Shared work record: `C:\Users\Phlip\Desktop\Jetstream Coder\SHARED_NOTES.md`
- Prior Copilot report: `C:\Users\Phlip\Desktop\Jetstream Coder\COPILOT_REPORT.md` (contains older blockers; consult later shared notes too).
- Imported Windows source: `C:\Users\Phlip\Desktop\Jetstream Coder\integrations\deskhand\windows`
- Imported Android source: `C:\Users\Phlip\Desktop\Jetstream Coder\integrations\deskhand\android`
- Imported Python service: `C:\Users\Phlip\Desktop\Jetstream Coder\integrations\deskhand\assistant_service`
- Imported supporting runtime: `integrations\deskhand\vendor`, `integrations\deskhand\windows\package-dependencies`, `integrations\deskhand\assistant_service\runtime` beneath repo root.
- Imported VM worker source: `C:\Users\Phlip\Desktop\Jetstream Coder\integrations\deskhand\vm`
- Task Agent: `C:\Users\Phlip\Desktop\Jetstream Coder\TaskAgent`
- Provenance: `C:\Users\Phlip\Desktop\Jetstream Coder\integrations\deskhand\source-manifest.json` (245 original entries/hashes; records original paths). Imported Windows source 100.051; Android 100.050. These are development sources, not proof of installed-release parity.
- Standalone source roots are under `C:\Users\Phlip\Desktop\Deskhand` (including `speed-work\windows-100051-source\Deskhand-windows`) and the Android original path recorded in the manifest. Original folders were not modified.
- Host config/pairings: `%LOCALAPPDATA%\JetstreamCoder\Deskhand-verification\config.json`; embedded Tailscale state is under that isolated verification profile. Preserve it. Never print protected device secrets or mailbox tokens.
- Jetstream local job descriptor: `%APPDATA%\jetstream-coder\deskhand-job-api.json`; contains a credential, so inspect selected non-secret properties only.
- ADB: `C:\Users\Phlip\AppData\Local\Android\Sdk\platform-tools\adb.exe`
- Phone: Samsung SM-A356U, serial `RFCY10TCC8J`. Verification package `com.deskhand.android.verification`; launcher `com.deskhand.android.MainActivity`. It coexists with original Deskhand.

## Build/shortcut state at this handoff

NEWEST bundle: `C:\Users\Phlip\Desktop\Jetstream Coder\artifacts\Jetstream-V0.001-1794718828486\Jetstream\electron.exe`.

That bundle includes the expanded desktop feature UI, new Windows helper/bridge and updated Android APK. Packaging succeeded. It has NOT yet been live-smoke-tested, launched for the user or assigned to the desktop shortcut in this continuation.

Desktop shortcut `C:\Users\Phlip\Desktop\Jetstream Coder.lnk` still points to the PRIOR verified bundle `C:\Users\Phlip\Desktop\Jetstream Coder\artifacts\Jetstream-V0.001-1794717913451\Jetstream\electron.exe` (no arguments).

Last verified live embedded host: PID 22296 from bundle `artifacts\Jetstream-V0.001-1794717275420\Jetstream\resources\app\Deskhand\Deskhand.exe`, flags `--jetstream --minimized`. Re-query processes before acting: IDs may change. Same-profile singleton means launching a new bundle can keep attaching to this older host, which lacks the newly added `features`, `settings` and `assistant` operations. Therefore verify/stop only the old embedded verification host and restart the new packaged one before claiming new live controls work. Do not stop standalone Deskhand or shared Task Agent. Earlier disposable host PID 21360 was verified against its exact dev output path and stopped to release a build lock.

## Implemented previously and retained

`src\PhoneConnection.tsx`: integrated network status/sign-in, QR, matching-code pair approval (view/control/reject), cancel pairing, remote pause/resume, disconnect, orientation-follow setting, phone control allow/block/revoke, host AI approvals.

`src\main.tsx`: main Settings has Workspace/Phone connection navigation; phone icon selects the latter. `src\style.css`: dark premium Settings styling, selected section and correct checkbox layout.

`electron\main.ts`, `electron\preload.ts`: typed IPC surface; same-user named-pipe client; controlled backend startup; validated Tailscale login URL opening in system browser. Normal app launch starts/checks the host so a phone need not wait for local Settings to open. Test flags suppress startup. `integrations\deskhand\Start-Deskhand.ps1 -EmbeddedUI` starts hidden with separate verification profile.

`integrations\deskhand\windows\Deskhand.Windows.App\JetstreamDesktopBridge.cs`: current-user-only named pipe, commands run on WinForms UI thread, same backend engine. Pairing approvals time out to Reject. Named pipe derives from uppercase absolute config path SHA256 first 16 hex digits, prefixed `Deskhand.Windows.App.SingleInstance.`, suffixed `.Desktop`.

`electron\job-api.ts` and native `JetstreamClient.cs`: authenticated loopback project-bound API, durable idempotent job reads/actions, encrypted paired phone relay. Candidate Apply/phone approval remain unavailable.

## Changes in this continuation

1. New `src\DeskhandFeatures.tsx`, opened by More Deskhand settings inside Phone connection. Sections:
   - Connection: PC name, relay URL/port, built-in relay, Jetstream Windows sign-in startup.
   - Assistant: original service catalog, assistant/model/effort, Auto/Fast/Smart routing, messages, stop, history, connector replies/action attribution, streamed partial text, local/managed/OpenAI-compatible model configuration, explicit computer access status/enable/disable.
   - Runtime: original host's cached connection/worker/activity state, files, events, turn timing, full message-board detail, twelve diagnostics components, diagnostics export with warning/debug options, worker reconnect.
   - Muse access: sign-in/finish/status, bridge address/port, stop/copy token, desktop/screenshot/command permissions with original typed confirmation phrases, trusted applications, folder rights, timed sessions.
   These controls are fixture-tested and compiled, but not all have been exercised with the real service. Missing remote Muse readiness must stay visible. The frontend does not silently auto-enable computer access when models/history are loaded.

2. New `integrations\deskhand\windows\Deskhand.Windows.App\JetstreamFeatures.cs`; `MainForm` made partial. Reads the original form's cached models/rendered status instead of creating another runtime feed. Connection persistence validates before mutation and uses original relay/engine methods. Assistant whitelist exposes original UI operations; rejects direct fs/exec/job RPCs and non-UI Muse actions. This is a local UI bridge, not a replacement for the separate Coder apply gate.

3. Bridge now accepts `features`, `settings`, `assistant`, `pair-configured`. Connections are accepted concurrently so a slow assistant request cannot prevent status/approval requests. Current-user restriction retained. Electron uses UTF-8 decoding, handles unexpected EOF, gives assistant responses 65s versus 25s normal timeout and does not silently retry an uncertain service command.

4. Electron `deskhand:control` owns startup operation via `app.setLoginItemSettings` and annotates feature settings using `getLoginItemSettings`; does not call old native Deskhand registry startup function. No actual startup change has been tested/applied.

5. Configured-relay QR choice added alongside normal embedded Tailscale QR. Native `JetstreamCreatePairing(bool configuredRelay=false)` uses original engine pairing generation. Not live-tested yet.

6. Android: removed top-level Jetstream jobs overlay that covered the original composer. Passed `onJetstreamClick` through `MainActivity.kt`/`ViewerScreen` into `ViewerLayout.kt`; Jobs now lives in the existing top-right glass toolbar with the same auto-hide timer, before original window buttons and final Stop octagon. Original composer, drawers, gestures and orientation backend remain reused. Updated verification APK was installed successfully with `adb install -r`, preserving pairing.

## Tests and actual evidence

- `npm run build`: passed V0.001. Existing bundle-size warning only.
- `npm test`: passed 42/42 after the additions.
- `tests\phone-settings.test.mjs`: existing integrated shortcut, pairing/code approval, control revoke, pause/resume and main Settings navigation.
- NEW `tests\deskhand-settings.test.mjs`: connection persistence, runtime/board/timing, original connector history, routing, uncertain-send held without auto-retry, no auto-access grant and explicit typed Muse permissions. Initial exact IPC-error-text assertion failed because Electron wraps errors; fixed to assert alert containing text. Final isolated and full tests passed.
- Native host release build passed. Existing nullable warning MainForm.cs around line 1656 remains.
- Added `--jetstream-settings-check` mode to `integrations\deskhand\windows\Deskhand.Integration.Specs\Program.cs`: 10 checks passed for snapshot, all 12 diagnostics, secret exclusion, rejected direct fs/exec/job RPCs, rejected non-UI Muse actions and invalid relay inputs preserving config. Does not claim full native integration suite was run in this continuation.
- Android offline `testDebugUnitTest assembleDebug -PdeskhandVerification=true`: passed 357/357 tests, zero failures. New `ViewerLayoutTest.jetstreamJobsSharesToolbarWithoutCoveringComposerOrDrawers` verifies geometry, stop, both drawer entry points and composer preservation. Existing API deprecation warnings only.
- Earlier real phone test: existing paired SM-A356U connected/control allowed over embedded Tailscale, address 100.108.34.15. Screenshot confirmed live Windows desktop. Disposable project `C:\Users\Phlip\AppData\Local\Temp\jetstream-phone-acceptance-WF5Xk9` with unchanged sample file. Phone submit/pause/resume/cancel matched actual job API state; job correctly blocked on coordinator unavailable before cancellation. This was an existing pairing, not a new physical QR ceremony.
- Current physical check after updated APK: left file drawer opened correctly and listed This PC/Desktop/C:\, search and resize controls, with original composer unobstructed. Next resize/right drawer attempt was INTERRUPTED: foreground package became `com.deepseek.chat` (likely user using phone). Do not claim resize/right drawer/orientation verified from that attempt. Avoid inspecting unrelated phone content. A temporary `.phone-parity.xml` contained that unrelated hierarchy and is removed at handoff.

## Remaining work / next moves, in order

1. Live-smoke newest bundle and new host. Identify process paths; replace only older embedded verification host. Preserve pairings and existing shared services. Verify `features` returns settings/runtime, all sections render, invalid ops fail, and a long service operation does not block status/approval. Do not infer readiness from an old host still running.
2. Verify feature payloads against real Python service responses: model catalog, history/connector replies/partials, timing, diagnostics export, worker status, Muse connection. Current fixture tests cannot establish external provider availability. Avoid sending new Muse test turns unnecessarily.
3. Phone acceptance when phone available: both edge tabs AND swipe gestures, resizing, close/back, file navigation/search/confirmation, agents/board tabs/refresh. Preserve original behavior in portrait/landscape; Jobs must remain out of composer. Reconfirm status pill, Fit/Stretch/Zoom, keyboard/pinch/pan, touch-control revocation, foreground window actions and stop/restore landscape. Some actions control PC, so use a disposable target window/project.
4. Orientation is still physically UNVERIFIED in this continuation. Original `PhoneDisplay.cs` and `Model.setViewerLandscape`/START path are unchanged. Verification config originally set PhoneFriendlyDisplay=false; inspect current setting and use Jetstream checkbox explicitly for test. Record original Android `accelerometer_rotation` and `user_rotation` before using test rotations; restore both afterward. Check actual host desktop dimensions/rotation, input mapping, and landscape restoration on Stop. Never claim a phone-UI rotation alone proves PC display adjustment.
5. Audit every standalone feature against actual UI/backend behavior. A manifest hash comparison found only HostEngine.cs, MainForm.cs, Program.cs, Android MainActivity.kt and Model.kt differed before this continuation; Android ViewerLayout.kt now has toolbar integration too. Drawers and PhoneDisplay were otherwise exact imported code. This proves code reuse, not all features working. Create a parity matrix with source locations, automated/device evidence and honest blocked status.
6. Audit embedded runtime path dependencies. Python `deskhand_task_agent.py` and `deskhand_task_agent_lifecycle.py` still default to `C:\Users\Phlip\Desktop\Deskhand\Deskhand-windows\Task-agent`; this was discovered but NOT fixed. Preserve intentional shared-agent ownership/account behavior while removing source-folder dependency for packaged fallback. Do not start a duplicate agent or broaden remote access. Packager intentionally excludes `agent_config.json` and secrets; an embedded fallback must obtain credentials via supported configuration, not an accidental source-file dependency.
7. Run appropriate Python service regressions with matching dependencies if modifying it; these were not run in this continuation. Repeat affected desktop/native/Android tests only after fixes. Rebuild/package as needed; keep build=1.
8. After final live smoke, update desktop shortcut to newest verified bundle, launch it and record exact paths/results in shared notes. At this handoff shortcut and last verified live host remain older; do not say newest is deployed yet.

## Commands

From repository root:

```powershell
npm run build
npm test
dotnet build integrations/deskhand/windows/Deskhand.Windows.App -c Release --no-restore
dotnet run --project integrations/deskhand/windows/Deskhand.Integration.Specs -c Release -- --jetstream-settings-check
node scripts/package-companion.mjs
```

Android, from `C:\Users\Phlip\Desktop\Jetstream Coder\integrations\deskhand\android`:

```powershell
$env:ANDROID_HOME='C:\Users\Phlip\AppData\Local\Android\Sdk'
.\gradlew.bat testDebugUnitTest assembleDebug --offline --no-daemon -PdeskhandVerification=true
```

Host script accepts `-EmbeddedUI -HostExe <absolute newest bundled Deskhand.exe>`. Its profile singleton prevents replacing an existing old host automatically. Newest bundle has Android APK under `Jetstream\resources\app\Deskhand\Deskhand-Android-verification.apk`.

## External blockers and cautions

Muse queued connection-test turn `2ae38c8a-6d8a-426d-8f06-4a5d8b20645a` has no verified reply. External Linux VM claimant uses fcntl/Hatch runtime; do not resubmit/claim/ack blindly. Local imported UI cannot prove the external worker restored. No cellular/off-site, PC lock/sleep, fresh physical QR re-pairing, actual Muse sign-in, broad permissions grants, startup entry or diagnostics save acceptance has been established by the new fixture tests.

Working tree is extensively dirty from multiple prior sessions, with imported integrations and many tests untracked. Do not reset/clean/revert preexisting work. No commits/pushes made. Production Apply remains disabled.

## Continuation after the handoff

New embedded host was started from bundle1794718828486 and passed a real live UI/service smoke. Both phone drawers opened/resized, Board tab opened, and actual PC portrait/landscape dimensions were measured. Rotation settings and host follow preference restored. Read `docs\deskhand-feature-parity.md` for authoritative per-feature acceptance and remaining gaps. Android shows a baseline16KB library alignment debug warning. Agent source feeds remain unavailable. This supersedes the earlier "new host not yet live tested" handoff state; shortcut still needs updating after final verification. User resumed work; do not restart the audit from scratch.

## Bundle-local service path fix (supersedes earlier path blocker)

AssistantClient now sets DESKHAND_TASK_AGENT_DIR to nearest bundled/check-out TaskAgent directory (or expected local path if assets are missing); imported client/lifecycle adapters honor it. No standalone-source execution fallback in embedded mode. Explicit constructor configurations retained. Tests: new unittest2/2, original executable client script14/14, lifecycle21/21. Shared account/credential ownership remains separate; credentials are not copied into packaged assets. New native build/package will follow; consult latest shared notes for final artifact path.

## Final artifact for this continuation

Newest bundle is now `C:\Users\Phlip\Desktop\Jetstream Coder\artifacts\Jetstream-V0.001-1794719656536\Jetstream\electron.exe` (supersedes1794718828486). Final native boundary checks10/10 and packaged live model-catalog smoke9 routes/no alerts passed after the path fix. Embedded host was switched to this final bundle; desktop shortcut updated and app launched. All code remains V0.001. No commit/push/tag/production Apply change.

Next work is acceptance against external/live integrations: agents/board source feed, verified remote Muse reply, fresh physical QR approval, phone touch mapping/voice/Stop and off-site/sleep cases. Android native16KB debug warning remains. The matrix explicitly separates these from preserved source/unit evidence. Do not claim all Deskhand behavior100% verified.

## Most recent rebuild/launch

Current desktop bundle now `C:\Users\Phlip\Desktop\Jetstream Coder\artifacts\Jetstream-V0.001-1794719863119\Jetstream\electron.exe`. Windows and Electron rebuild passed V0.001; shortcut updated; embedded host switched to this artifact; app launched on user request. Earlier artifact paths above describe historical verification outputs.

## Five-recommendation continuation 2026-11-15

See `docs/prototype-milestone-2026-11-15.md` for all current changes, tests and concrete blockers. Native Android Go bridge source imported to `integrations/deskhand/embedded-network`; both supported ABIs rebuilt16KB aligned with script `scripts/build-android-network.ps1`. Final APK verifier checks ELF LOAD and ZIP alignment; parser4/4. Android357/357 and desktop42/42 pass after connection pill/toolbar overlap fix; Python Muse30/30/native13/13. Old test fixture missing from vendor distribution restored in assistant_service/fixtures (test only).

Phone coding test script `scripts/verify-phone-job.mjs` exists, but live attempt did not complete: app closed; pending request17bfcf0f-977e-45da-bcca-cbb68ba65269 unresolved. Preserve pending state and do not retry across projects. Real local-model desktop diagnostic did pass with simulated approval/test-only apply/rollback. Muse read-only ead84160-8572-4896-b893-11169a0bdd34 cancelled/no reply; no replay. User confirmed signed in; browser Ready. Live Muse reply/feed and physical fresh pairing/voice/off-site/sleep acceptance remain unverified. Work continues independent packaging and authorized source backup; preserve historical V0.001 tag.
