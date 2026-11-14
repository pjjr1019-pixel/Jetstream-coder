# Jetstream Coder

Early-stage Windows desktop editor. Run `npm install`, then `npm run dev`. Run `npm run build` and `npm test` for the build and desktop smoke test. Job records and the rebuildable text index are kept in project-separated SQLite databases in the app's user-data directory.

Root and nested `.gitignore` files, the optional root `.jetstreamignore`, and project exclusions set in the Explorer filter search and new scratch copies; `.env` files, PEM and key files are excluded by default.

The app supports project browsing and editing, disk-change checks before manual saves, a file watcher, indexed text search, Git status, and a persistent interactive PowerShell terminal. Terminal commands are entered by the user and run with the user's Windows permissions. The index also extracts JavaScript, TypeScript, Python, and C# definitions and syntactic call sites; these are not type-resolved call graphs.

Recorded jobs are searchable by request and status and can be cancelled while idle and requeued; cancellation preserves their history and scratch files and invalidates previous verification. Each recorded job can have a baseline and editable scratch copy, a diff preview, a conservative baseline and new-file collision check, and configured build and test commands. Scratch snapshots skip .git, node_modules, dist, dist-electron, build, .jetstream-coder, and symlinks, and have a 256 MB cap; projects may need dependencies separately in scratch. Build and test commands run with the user's Windows permissions: the scratch folder is not an operating-system sandbox, and scripts can access other files and services. Check results are bound to a fingerprint taken before and after execution; a changed scratch tree invalidates the run. Re-run checks after any subsequent scratch change.

The Team panel supports manually entered Ollama questions. A job can request a single-file local candidate: the selected scratch file is sent to the chosen model, and the result is saved in a separate candidate workspace with an attributed, fingerprint-checked diff. The original scratch and project files are not modified. Experimental feedback from a different local model is available, but it is not a final review of the candidate and does not unlock Apply. Muse and GPT integrations, production independent review, review-bound approval, checkpointed apply and rollback, resolved cross-file relationships, cloud usage routing, and discussion remain incomplete. No AI change can be applied to the project through the app. See `docs/integrations.md` for discovery findings.

The gate status compares the current scratch fingerprint with the latest checks, independent review, and approval. Real review and approval adapters do not exist yet, and the gate cannot enable Apply. A verification interrupted by an app shutdown or project switch is marked for retry when the project is reopened.

A test-only checkpoint/apply/rollback prototype lives in `electron/change-fixture.ts`; it is not connected to the app. Disposable-project tests cover conflicts, stale evidence, post-check outcomes, interruption, and rollback. It is not production-safe: file changes by other processes can race the checks and replacement, crash durability is not established, and no verified reviewer or approval interface is connected. Do not use the fixture on a real project.

Each `npm run build` keeps the current version. V0.001 remains in use until Phil decides the base is stable. Before a build, add user-facing changes to `CHANGELOG_PENDING.md`; a successful build adds the notes to the current version's changelog and clears the pending file. A failed build preserves the pending notes.

## Saving versions in Git

Record completed changes in Git commits. Keep the existing V0.001 tag pointing to its original checkpoint; make a new version tag only when Phil approves moving to a new version. Once a remote has been configured, push the commit and its version tag to keep the online copy current. Check the files being committed and never commit credentials, local databases, or generated build artifacts. A build alone updates the displayed version and changelog; it does not automatically commit or push.

The Source panel commit review checks the staged tree and branch against the preview immediately before invoking Git. Git hooks and external Git processes can still change repository state concurrently; inspect Git history if other tools are modifying the repository at the same time. This is a manual Git operation and does not enable AI Apply.
