# Jetstream and Deskhand development companion



This is an isolated integration candidate. Jetstream stays V0.001. The imported Windows host is Deskhand 100.051 and the imported Android source is 100.050. These sources match development artifacts, not a proven installed pair. `source-manifest.json` records original files and hashes before integration edits. The original Deskhand folders and live credentials were not changed.



## Implemented



The native Windows adapter reads Jetstream's same-user local descriptor, authenticates to its loopback Job API, and forwards bounded results through Deskhand's existing paired, encrypted ChatRequest channel. It never forwards the local bearer. Actions require a control-capable paired device and a current project ID. The Android viewer's **Jetstream jobs** button opens status, job details, diffs, review, checks, receipt, and submit/pause/resume/cancel/reject controls. It polls while open. A pending action is saved per paired PC before sending, survives restart, and retries with the same request ID. Uncertain outcomes remain unresolved rather than being automatically repeated. Phone approval and Apply are unavailable.



The existing desktop stream, pairing, reconnect, and input code is reused. The phone never calls Jetstream's HTTP API directly. Status and API unit tests do not prove actual phone viewing.



## Build and test from the Jetstream root



1. `npm run build` and `npm test` (Windows, Node, Electron; the native adapter integration test also needs .NET 8 SDK).

2. Run `node scripts/import-deskhand-runtime.mjs <Deskhand-100.051-candidate-directory>` to import the Windows runtime, then run `dotnet build integrations/deskhand/windows/Deskhand.Windows.App/Deskhand.Windows.App.csproj -c Release --ignore-failed-sources`.

3. `dotnet run --project integrations/deskhand/windows/Jetstream.Adapter.Specs -c Release`.

4. In `integrations/deskhand/android`, set `ANDROID_HOME` to the installed Android SDK, then run `gradlew.bat testDebugUnitTest assembleDebug --offline --no-daemon -PdeskhandVerification=true`. Uses the existing Gradle cache. The verification APK has a separate Android application ID and does not replace the installed app.

5. `node scripts/package-companion.mjs` creates a new timestamped directory under `artifacts` containing Electron/Jetstream, the Deskhand host, the verification APK, and file hashes. Packaging performs no install or launch. Builds must finish successfully before packaging.



## Use the development bundle



Run `Jetstream/electron.exe` in the bundle to open Jetstream. The Windows host, its runtime, and the verification APK are under `Jetstream/resources/app/Deskhand`. The phone icon opens pairing. `Jetstream/resources/app/Deskhand/Start-Deskhand.ps1` also launches the included host with a separate verification configuration and relay port. It neither imports existing pairing secrets nor changes the installed Deskhand configuration. Pair the verification Android app using that host's QR flow. The host needs .NET 8 Windows Desktop and ASP.NET runtimes; Python service requirements are listed in `Deskhand/assistant_service/requirements.txt`. The imported Windows runtime includes Python, JettPad, Ollama, FFmpeg, and the embedded network library. The phone icon in Jetstream opens Settings > Phone connection. Tailscale sign-in, QR display, pairing approval, remote-access controls and paired-phone management are inside Jetstream; the embedded host runs hidden. The APK is debug-signed and the bundle is not a signed production installer.



Android verification on SM-A356U confirmed an existing paired connection, live Windows desktop viewing, correct disposable-project identification, and submit/pause/resume/cancel actions. Fresh physical QR re-pairing, job detail usability, disconnect/reconnect and duplicate request recovery still need device acceptance. Stop the verification host when done. Streaming, PC sleep/lock behavior, mobile-network access, and physical phone usability are not established by compilation or unit tests.



The shared Muse mailbox is a separate connection. Its actual consumer was found in Deskhand’s `speed-work/vm`; source copies are preserved under `vm`. `vm_tunnel_poll.py` requires Linux `fcntl` ownership locking and a scoped key, while `tunnel-turn-consumer.sh` requires the external Hatch worker runtime. Windows cannot substitute for that running Muse environment. No VM loop or claim was started. The previously submitted test turn remains queued; the external claiming worker must be restored before Muse communication can be called verified. Do not resubmit or claim it blindly.

