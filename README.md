# Jetstream Coder

Early-stage Windows desktop editor. Run `npm install`, then `npm run dev`. Run `npm run build` and `npm test` for the build and desktop smoke test. Job records and the rebuildable text index are kept in project-separated SQLite databases in the app's user-data directory.

Root and nested `.gitignore` files and the optional root `.jetstreamignore` filter search and scratch copies; `.env` files, PEM and key files are excluded by default.

The app supports project browsing and editing, disk-change checks before manual saves, a file watcher, indexed text search, Git status, and a persistent interactive PowerShell terminal. Terminal commands are entered by the user and run with the user's Windows permissions. The index also extracts JavaScript and TypeScript definitions and syntactic call sites; these are not type-resolved call graphs.

Each recorded job can have a baseline and editable scratch copy, a diff preview, a conservative baseline conflict check, and configured build and test commands. Scratch snapshots skip .git, node_modules, dist, dist-electron, build, .jetstream-coder, and symlinks, and have a 256 MB cap; projects may need dependencies separately in scratch. Build and test commands run with the user's Windows permissions: the scratch folder is not an operating-system sandbox, and scripts can access other files and services. Check results are bound to a fingerprint taken before and after execution; a changed scratch tree invalidates the run. Re-run checks after any subsequent scratch change.

AI jobs and apply remain disabled. The Team panel can list local Ollama models and send a manually entered question; it does not automatically send project files or act as a worker. Muse and GPT integrations, local model job execution, independent review, review-bound approval, checkpointed apply and rollback, language indexing beyond JavaScript/TypeScript, usage routing, and discussion are not implemented. No scratch change can be applied through the app. See `docs/integrations.md` for discovery findings.

The gate status compares the current scratch fingerprint with the latest checks, independent review, and approval. Real review and approval adapters do not exist yet, and the gate cannot enable Apply. A verification interrupted by an app shutdown or project switch is marked for retry when the project is reopened.

A test-only checkpoint/apply/rollback prototype lives in `electron/change-fixture.ts`; it is not connected to the app. Disposable-project tests cover conflicts, stale evidence, post-check outcomes, interruption, and rollback. It is not production-safe: file changes by other processes can race the checks and replacement, crash durability is not established, and no verified reviewer or approval interface is connected. Do not use the fixture on a real project.

Each successful `npm run build` advances the displayed build number and adds an entry to `CHANGELOG.md`. Before a build, add its user-facing changes to `CHANGELOG_PENDING.md`; the build includes those notes and clears the pending file after success. A failed build restores the prior version and changelog and retains the pending notes.

## Saving versions in Git

After a successful versioned build, record its source and `BUILD_VERSION.json` in a Git commit and tag that commit with the displayed version, for example `V0.001`. Once a remote has been configured, push the commit and its version tag to keep the online copy current. Check the files being committed and never commit credentials, local databases, or generated build artifacts. A build alone updates the displayed version and changelog; it does not automatically commit or push.
