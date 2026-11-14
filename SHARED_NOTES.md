# Jetstream Coder shared notes

This file is a shared handoff for Phil, Muse/Jett, and Codex while building Jetstream Coder. Anyone working on the app can read it and add notes here. Phil's current instructions and the agreed design take precedence over these notes.

## How to use this file

- Read the latest notes and inspect the current code before starting work. The code and test results may have changed since a note was written.
- Add a dated entry with your name, what you worked on, files changed, checks run and their results, and any remaining problems or next steps. Separate verified facts from ideas or assumptions.
- Before editing, note the work you plan to take on. If another person or agent is already changing the same files, coordinate with Phil before overlapping. Record when you finish or stop so the next person can pick up safely.
- Preserve existing notes and each other's changes. Do not include passwords, sign-in codes, API keys, or other secrets.
- This is a shared file, not a live connection or automatic messaging system. A note is visible to the other collaborator when they read the file; it does not start or notify Muse or Codex.

## Current project status

The design is in `jetstream-coder-design.docx`. The implementation is an early Electron, React, TypeScript, and Monaco editor. See `README.md` for current capabilities and limitations, and `docs/integrations.md` for the integration inventory.

As last verified by Codex on 2026-11-13, the app can open and edit a project, search a local text index, show Git status, run user-entered PowerShell commands, record jobs, create baseline and scratch copies, preview diffs, check for baseline conflicts, and run configured build and test commands in scratch. The production build and desktop smoke test passed. Re-run checks after making changes; do not assume these results cover later edits.

AI execution and Apply are disabled. Muse, GPT account access, independent review, the complete verification and approval gate, checkpointed apply, post-apply checks, and rollback are not yet implemented. The PowerShell runner and scratch build/test commands run with the user's Windows permissions; the scratch folder is not an operating-system sandbox.

## Guardrails and next work

- Keep AI changes out of the project until a different model has independently reviewed them, the configured build and tests pass, and Phil has approved the diff. Missing verification is a block, not a pass.
- Maintain the per-account 20% reserve and ask-first maximum effort decisions in the design. Unknown usage must keep a cloud account out of routine rotation.
- Verify any Muse, account, shared-memory, or Jetstream switcher interface before presenting it as connected. Do not automate a signed-in ChatGPT interface without a supported integration, or substitute paid API billing without Phil's approval.
- Next priorities: finish the editor and index; make scratch and job records complete; implement trustworthy independent review and a review-bound verification gate; then implement conflict-aware checkpointed apply, post-apply verification, and safe rollback. Test on a disposable project before using a real one.

## Handoff entries

### 2026-11-13 — Codex

Created this shared handoff at Phil's request. The current app is a partial implementation; the remaining safety pipeline and AI connections are not complete. The latest production build and Electron desktop smoke test passed before this note was created. No code was changed in this entry.

### 2026-11-13 ~21:45 EST — Muse / Jett

**Date and collaborator:** 2026-11-13 ~21:45 EST — Muse (Jett)

**Starting work:** Read SHARED_NOTES.md and inspected current code and design doc before starting any work, per Phil's instruction. No code changes in this entry. I intend to coordinate with Codex through this file and wait for Phil's next work item before overlapping on files.

**Changes and files:** None — read-only review. Verified: design doc is v3, 15 pages, 53,713 bytes, LastWriteTime 2026-11-13 20:50:05, with 20% reserve locked and max effort ask-first. Code matches Codex's 2026-11-13 status: Electron / React / TypeScript / Monaco editor, build.log shows production build passed 21:34:29, src/main.tsx + electron/main.ts + tests/desktop.test.mjs as reviewed. No files changed since 21:34:29 except SHARED_NOTES.md itself.

**Checks and results:** Listed Desktop\Jetstream Coder excluding node_modules; confirmed build.log passed, git has no commits yet (all files untracked), AI execution / Apply still disabled per README and docs/integrations.md. Test run itself not seen in build.log — build pass verified, desktop smoke test pass is Codex's note, not re-verified by me in this entry.

**Remaining work or blockers:** Awaiting Phil's next work item. Will not overlap Codex files without checking with Phil first. Next priorities per notes: finish editor/index, complete scratch/job records, independent review + review-bound verification gate, then checkpointed apply + rollback — test on a disposable project first.

### Entry template

**Date and collaborator:**

**Starting work:**

**Changes and files:**

**Checks and results:**

**Remaining work or blockers:**

### 2026-11-13 — Codex starting work

Phil asked Codex to continue all nine original steps. I am taking the editor, index, job pipeline, safety gate, provider integration, and tests in this workspace. Muse's latest note says it is waiting and made no code edits. I will record completed changes and verification below; unverified live integrations and Apply remain blocked.

### 2026-11-13 — Codex progress and handoff

**Work completed:** Added before/after scratch-tree fingerprints and durable verification-run records. A scratch change during checks invalidates the run; symlinks and oversized trees block fingerprinting. Added job-event history in the UI. Root `.gitignore` and `.jetstreamignore` patterns now filter indexing and scratch copies, with `.env`, `.env.*`, `.pem`, and `.key` exclusions. Nested ignore files are not yet interpreted.

**Local models:** Started the installed Ollama service and verified `/api/tags` returned the installed models. A short direct test prompt to `qwen3.5:4b` returned `ready`. The Team panel now reports local status and can send a manually entered question; it does not send project files automatically or execute jobs. Muse and GPT integrations remain unavailable.

**Files changed:** `electron/main.ts`, `electron/preload.ts`, new `electron/evidence.ts`, `src/main.tsx`, `src/style.css`, `tests/desktop.test.mjs`, `package.json`, `package-lock.json`, `README.md`, and `docs/integrations.md`. Added npm package `ignore`.

**Checks:** `npm run build` passed. `npm test` passed after the changes, covering manual save conflicts, scratch isolation, ignore exclusions, project conflict checks, paused and missing-command verification, failure and scratch-change invalidation, and a simulated Ollama panel response. The direct Ollama inference test was separate from the app UI test.

**Remaining:** Phil asked for all nine stages, but they are not complete. Persistent interactive terminal, symbols/relationships and nested ignore handling, complete durable jobs, trustworthy independent AI review, approval bound to exact change, checkpointed apply and safe rollback, real Muse and GPT integrations, autonomous local work, parallel orchestration, usage routing/dashboard, and discussion remain. Apply and AI jobs remain disabled. Scratch check commands are not OS-sandboxed. Do not treat these partial changes or fixture results as a completed gate.

### 2026-11-13 — Codex starting foundation work

Phil requested the three foundation milestones: editor/index, durable jobs and verification, and safe apply/rollback with disposable-project tests. Muse's most recent entry says it was waiting and did not edit code. I am working in the Electron, renderer, and test files; AI Apply remains blocked pending real independent review.

### 2026-11-13 — Codex foundation progress

**Completed:** Added a persistent interactive PowerShell terminal with xterm/node-pty. Added nested `.gitignore` handling and JavaScript/TypeScript syntax indexing for named definitions and syntactic call sites (not a type-resolved call graph). Added gate-status inspection that compares the current scratch fingerprint, latest checks and commands, review, and approval; the gate is fail-closed and production has no route to manufacture reviews or approvals. Job creation and scratch-record updates are transactional. A verification interrupted by reopening a project is marked for retry.

**Files changed:** `electron/main.ts`, `electron/preload.ts`, new `electron/gate.ts`, `src/main.tsx`, new `src/TerminalPanel.tsx`, `src/style.css`, `tests/desktop.test.mjs`, new `tests/gate.test.mjs`, `package.json`, `package-lock.json`, and `README.md`.

**Checks:** `npm run build` passed; `npm test` passed both tests. The desktop test exercised terminal state across commands, nested ignores, symbol/call lookup, scratch and job recovery on project reopening, paused/missing/failed checks, changed-scratch invalidation, and interrupted verification. The gate unit test used synthetic evidence to check missing, stale, same-model, and invalid approval cases. Synthetic gate evidence was not stored in the app.

**Still required:** These three milestones are not complete. The app has no verified reviewer integration, no user approval flow bound to real review, no checkpointed apply, post-apply verification, or rollback. The gate remains informational and Apply remains disabled. The index supports syntax in JS/TS only and calls are not resolved across files. Terminal and build/test commands are not OS-sandboxed. Full restart/crash recovery and safe apply must be tested with disposable projects before production use.

### 2026-11-13 — Codex starting checkpoint work

Phil requested checkpointed apply, rollback, exact-evidence approval, and disposable-project testing. I am building and testing the change engine as a fixture; production Apply remains disabled until real review and concurrency safety are verified. No Muse edits are recorded after the preceding handoff.

### 2026-11-13 — Codex checkpoint prototype handoff

**Completed:** Added `electron/change-fixture.ts`, a test-only checkpoint/apply/rollback prototype; it is not wired into the app. It compares a baseline and proposed scratch tree, checks gate evidence and project conflicts, saves original bytes and a journal outside the project, runs a supplied post-check, and can restore a checkpoint while rejecting later file edits. Gate evidence now binds review and approval to both the baseline and the scratch snapshot. Existing SQLite review/approval tables gain a `base_hash` column; old records without a baseline hash remain blocked.

**Checks:** `npm run build` and `npm test` passed (five tests). Disposable-project tests covered added, changed and deleted files, missing approval, stale scratch or baseline, project conflicts, successful and failed post-checks, later edits blocking rollback, and a simulated interruption. These tests use synthetic reviewer and approval evidence; they are not a live review or complete process-crash recovery test.

**Real model exercise:** Ollama's `qwen3.5:4b` generated a small sample and `qwen3.5:9b` responded with a review. The review was truncated and made a questionable broad claim, so this does not verify reviewer quality or a production reviewer integration.

**Important remaining work:** Real Apply is still disabled. The prototype has a check-to-write race with external edits and lacks established crash durability, trusted reviewer/approval integration, and full post-apply command orchestration. Do not use it on a real project. Next work should resolve those safety gaps, verify the reviewer and bind actual approval to the exact evidence, then test the complete workflow on a disposable project.

### 2026-11-13 — Codex starting version and changelog

Phil requested a clickable version number and a changelog, with the version updated on each rebuild. I am editing the build script, renderer, and desktop test. Existing app windows may still show older builds until restarted.

### 2026-11-13 — Codex version and changelog handoff

**Completed:** Added a clickable bottom-right build version and changelog dialog. The version starts at V0.001 and `npm run build` increments it for each successful rebuild. Add change notes to `CHANGELOG_PENDING.md` before building; successful builds move those notes into `CHANGELOG.md`, while failed builds restore the version/changelog and retain pending notes. A rebuild without notes is labeled accordingly.

**Checks:** Latest app build is V0.001. `npm test` passed all six tests, including the changelog dialog and temporary build tests for success, increment, and failure recovery. An existing open window may be an older build; launch the desktop shortcut for the new build. AI Apply remains disabled.

### 2026-11-13 — Git version history

Phil requested local and private GitHub version history. For every completed versioned rebuild, review the changes for credentials and project-only data, commit the source and version/changelog together, create the matching version tag (for example `V0.001`), and push the commit and tag once the GitHub remote and authentication are available. Git saves are not automatic on `npm run build`; do not claim an online copy exists until the push is verified. Preserve existing staged work and do not silently commit unrelated files.


### 2026-11-13 — Version policy update

Phil directs that all current rebuilds remain V0.001 until the base is stable and he decides to move to V0.002. `npm run build` no longer increments the version; pending notes are added under the current version. Keep the existing V0.001 Git tag on its original commit; use additional commits for later work within V0.001.
