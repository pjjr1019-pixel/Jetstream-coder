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

### 2026-11-14 ~07:02 EST — Muse / Jett

**Date and collaborator:** 2026-11-14 ~07:02 EST — Muse (Jett)

**Starting work:** Re-read SHARED_NOTES.md and HANDOFF.md at Phil's request, to see where the build stands. No code changes in this entry. Work is paused per HANDOFF.md — no implementation until Phil resumes.

**Changes and files:** None — read-only. Verified: V0.001 locked, last full npm test passed 6/6, last build passed Built V0.001, Git remote origin https://github.com/pjjr1019-pixel/Jetstream-coder.git branch main, latest pushed feature commit 1910bfb. Two uncommitted diagnostic files noted in HANDOFF.md: electron/main.ts and scripts/verify-local-proposal.mjs.

**Checks and results:** Read full SHARED_NOTES.md (315 lines) and HANDOFF.md (137 lines). Current blocker is the intermittent real-model project-generation/epoch failure in verify-local-proposal.mjs — cause not established, fail-closed checks must not be weakened. Production Apply remains disabled; change-fixture.ts is test-only, not wired in.

**Remaining work or blockers:** Awaiting Phil resuming work. Will not overlap Codex files without checking with Phil first.

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

### 2026-11-13 — Codex starting V0.001 foundation work

Phil requested completion of partially implemented features, keeping V0.001 until the base is stable. I am working in the editor, verification, and test code. Apply remains blocked until the real review and recovery safeguards are verified.

### 2026-11-13 — Codex V0.001 foundation progress

**Completed:** Corrected PowerShell verification so native process failures propagate their exit code; previous successful verification records are invalidated when a new run begins or verification commands change. Pausing or changing commands during checks interrupts verification. Added Reload with a confirmation before discarding unsaved edits.

**Files:** `electron/main.ts`, `src/main.tsx`, `tests/desktop.test.mjs`, `CHANGELOG.md`, and this handoff.

**Verification:** V0.001 build passed; all six tests passed, including native exit code 7, stale evidence during a running check, and reloading after an external file change.

**Still incomplete:** Independent reviewer, trusted review and approval, production apply and rollback, durable crash recovery, complete indexing and source-control features, model job execution, Muse and the app switcher. Apply remains disabled. Keep all builds at V0.001 until Phil decides the base is stable.

### 2026-11-13 — Codex continuing V0.001

Phil requested that work continue on all partially implemented features. I am working on scratch consistency, conflict detection, and source-control inspection. Real AI Apply remains disabled until independent review and recovery are verified.

### 2026-11-13 — Codex V0.001 scratch and Git progress

**Completed:** Scratch creation checks for project file changes during snapshot preparation and refuses to silently recreate missing scratch. Conflict inspection now catches a new scratch file colliding with a project file. The Source panel can inspect staged and unstaged Git diffs. These checks reduce risks but are not a lock against concurrent external edits.

**Checks:** V0.001 build and all six tests passed, including the new-file collision and Git diff panel on a disposable repository. Real Apply remains disabled; no real reviewer or production rollback is connected.

**Remaining:** Finish independent review and approval, production safe apply and crash recovery, broader indexing and source-control operations, local-model job execution, Muse and app-switcher integration. Keep V0.001 until Phil decides the base is stable.

### 2026-11-13 — Codex starting source-control and job improvements

Phil requested continued completion of the partial V0.001 features. I am working on user-directed Git staging and job history/search; production AI Apply remains disabled.

### 2026-11-13 — Codex V0.001 source and job progress

Added staging of the current saved visible project file using a literal Git pathspec; unsaved editor changes must be saved first. Added job search by request and status. V0.001 build and all six tests passed, including staging and search in a disposable Git project. Files: `electron/main.ts`, `electron/preload.ts`, `src/main.tsx`, `tests/desktop.test.mjs`, `README.md`, and `CHANGELOG.md`.

The broader work remains incomplete: production AI review, approval and Apply, crash-safe rollback, full Git workflow, broader indexing, automated local workers, Muse and app-switcher integration. Apply stays disabled. Keep V0.001 until Phil approves a new version.

### 2026-11-13 — Codex continuing V0.001 editor and Git work

Phil asked to continue finishing partial features. I am working on source-control controls and editor file-change handling. Production Apply remains blocked until review and recovery are verified.

### 2026-11-13 — Codex V0.001 unstage progress

Added an Unstage current file control, including the no-commit repository case; it updates the Git index only. Built V0.001 and all six tests passed, including unstage and restage in a disposable repository. Files: `electron/main.ts`, `electron/preload.ts`, `src/main.tsx`, `tests/desktop.test.mjs`, `README.md`, and `CHANGELOG.md`.

The larger task remains incomplete: production independent review and approval, safe apply and crash recovery, fuller indexing and source-control operations, local-model jobs, Muse and app switcher. AI Apply remains disabled. Keep V0.001 until Phil approves changing the version.

### 2026-11-13 — Codex continuing V0.001 editor work

Phil requested continued completion of partially implemented features. I am working on external-file notifications and search navigation. Independent AI review and Apply remain blocked.

### 2026-11-13 — Codex V0.001 editor progress

Added warnings on open tabs when the corresponding disk file changes or becomes unavailable, without replacing editor text. Search result selection now opens and navigates to the result line. V0.001 build and all six tests passed, including a disk-change warning preserving unsaved text and opening a search result. Files changed: `src/main.tsx`, `src/style.css`, `tests/desktop.test.mjs`, `CHANGELOG.md`, and this handoff.

The broader task remains unfinished: production independent review and approval, safe apply and crash recovery, fuller indexing and source control, local-model jobs, Muse, and app switcher. AI Apply remains disabled and V0.001 remains the current version.

### 2026-11-13 — Codex continuing V0.001 job and staging work

Phil requested completion of partially implemented features. I am working on explicit job cancellation and safer Git staging. Production AI Apply remains disabled pending verified independent review and recovery.

### 2026-11-13 — Codex V0.001 job cancellation progress

Added explicit cancellation for idle jobs. Cancellation records a history event, invalidates verification evidence, preserves scratch, and blocks later scratch preparation and verification; it survives reopening the project. Git staging now checks the disk fingerprint against the saved editor version. V0.001 build and all six tests passed, including cancellation, restart-by-reopen, retained scratch, and stale-file staging rejection.

Files: `electron/main.ts`, `electron/preload.ts`, `src/main.tsx`, `tests/desktop.test.mjs`, `README.md`, and `CHANGELOG.md`. Still incomplete: production review/approval, safe Apply and crash recovery, fuller source control and index, local model jobs, Muse and switcher. Apply remains disabled. Keep V0.001 until Phil approves a new version.

### 2026-11-13 — Codex continuing V0.001 project configuration

Phil asked for continuous work on the partially implemented features without repeated prompts. I am implementing project-specific ignore settings and testing the resulting search/scratch exclusions. AI Apply remains disabled.

### 2026-11-13 — Codex V0.001 exclusions and requeue progress

Added project-specific Git-style exclusions in Explorer, persisted per project and applied to search and new scratch copies. Existing scratch copies are not changed. Added requeue for cancelled jobs; history and scratch remain, and prior verification stays invalid. Builds remain V0.001. Both builds and all six tests passed, including exclusion persistence and requeue on a disposable project.

Still incomplete: real independent review and approval, production safe apply and crash recovery, broader indexing and source-control features, automated local-model jobs, Muse and app switcher. Apply remains disabled. Files: `electron/main.ts`, `electron/preload.ts`, `src/main.tsx`, `src/style.css`, `tests/desktop.test.mjs`, `README.md`, and `CHANGELOG.md`.

### 2026-11-13 — Codex continuing V0.001 source-control work

Phil requested continued work without repeated prompts. I am adding an explicit in-app staged-change review and commit workflow; it is separate from AI Apply, which remains disabled.

### 2026-11-13 — Codex V0.001 commit workflow progress

Added a Source-panel commit message, staged diff review, and explicit manual commit action. The app checks the staged tree and HEAD against the preview immediately before invoking Git; it uses normal Git identity and hooks. It does not lock out other Git processes or enable AI Apply. The flow requires opening the repository root and currently requires a text diff.

V0.001 build and all six tests passed, including stale-preview rejection, an initial commit via the UI, and unstage with an existing HEAD, on a disposable repository. Files: `electron/main.ts`, `electron/preload.ts`, `src/main.tsx`, `src/style.css`, `tests/desktop.test.mjs`, `README.md`, and `CHANGELOG.md`.

Still incomplete: independent review and approval, safe Apply and crash recovery, broader indexing, local-model coding jobs, Muse and app switcher. Keep V0.001 until Phil decides otherwise.

### 2026-11-13 — Codex continuing V0.001 file creation

Phil requested continuous work on unfinished features. I am adding guarded file creation to the editor and testing it in a disposable project. AI Apply remains disabled.

### 2026-11-13 — Codex V0.001 file creation verified

Completed and tested new-file creation in the Explorer. It requires a relative path with an existing parent folder and refuses existing files, traversal, and invalid Windows names. `npm run build` passed at V0.001; all six tests passed, including creation and refusal cases on a disposable project. Files: `electron/main.ts`, `electron/preload.ts`, `src/main.tsx`, `src/style.css`, `tests/desktop.test.mjs`, and `CHANGELOG.md`. AI Apply remains disabled. Remaining work includes independent review, approval, safe apply and crash recovery, broader index, model jobs, and Muse integration.

### 2026-11-13 — Codex continuing verification hardening

Working on invalidating in-flight verification if pause or command settings change while final snapshot checks run. AI Apply remains disabled.

### 2026-11-13 — Codex verification progress

Verification now rechecks pause and command settings after the final scratch fingerprint and before accepting results. Built V0.001 and all six tests passed, including pausing an in-flight check. Files: `electron/main.ts`, `tests/desktop.test.mjs`, and `CHANGELOG.md`. Real AI Apply and independent review remain disabled and unfinished.

### 2026-11-13 — Codex continuing V0.001 verification safeguards

Phil asked for continued work without routine questions. I am strengthening verification to detect pause/resume and configuration changes throughout a running check; AI Apply stays disabled.

### 2026-11-13 — Codex verification epoch progress

Verification now tracks a change epoch so pause/resume or changes to command settings during a run invalidate it even if the prior values are restored before completion. V0.001 build and all six tests passed, including pause followed by resume during a running check. Files: `electron/main.ts`, `tests/desktop.test.mjs`, `CHANGELOG.md`, and this handoff. Apply and real independent review remain disabled and unfinished.

### 2026-11-13 — Codex continuing V0.001 job history

Phil requested continued work on partial features without routine questions. I am improving job search and recording verification outcomes. AI Apply remains disabled.

### 2026-11-13 — Codex V0.001 job history progress

Job search includes event kinds and details, and verification completion now records status, snapshot, and validity alongside verification evidence and job status in one SQLite transaction. `npm run build` and all six tests passed, including history search and outcome recording. Files: `electron/main.ts`, `src/main.tsx`, `tests/desktop.test.mjs`, `CHANGELOG.md`, and this handoff. AI Apply remains disabled; real review, approval, safe apply/rollback, crash recovery and other previously listed features remain incomplete. Keep V0.001 until Phil approves a change.

### 2026-11-14 — Codex starting partial-feature implementation

Phil approved the plan to finish partial features while keeping V0.001. I am working first on index rebuild/status and crash recovery. AI Apply stays disabled until independent review and safe recovery are verified.

### 2026-11-14 — Codex V0.001 index progress

Added index status, skipped-file counts, an explicit full rebuild action, and serialized indexing operations. V0.001 build and all six tests passed, including a full rebuild and symbol lookup on a disposable project. Files: `electron/main.ts`, `electron/preload.ts`, `src/main.tsx`, `tests/desktop.test.mjs`, `CHANGELOG.md`, and this handoff. Python/C# indexing, real AI review/approval, production apply/rollback and crash recovery remain incomplete; AI Apply is disabled.

### 2026-11-14 — Codex starting approved V0.001 implementation

Phil approved the continuous partial-feature plan. I am working on project indexing and file operations first; Apply remains disabled pending real independent review and safe recovery.

### 2026-11-14 — Codex V0.001 Python and C# indexing

Added Tree-sitter parsing for Python and C# named definitions and syntactic call sites. Calls are displayed as possible calls, not resolved relationships. Existing syntax indexes migrate by rebuilding on project open. The V0.001 build and all six tests passed, including Python and C# definitions/calls on a disposable project. Files: `electron/main.ts`, `src/main.tsx`, `scripts/build.mjs`, `package.json`, `package-lock.json`, `tests/desktop.test.mjs`, `README.md`, and `CHANGELOG.md`. Real AI review, approval, safe apply/rollback and crash recovery remain incomplete; Apply remains disabled.

### 2026-11-14 — Codex implementing approved V0.001 plan

Starting with editor folder creation and index/project-switch correctness. Continue through remaining features; AI Apply stays blocked pending independently verified review and recovery.

### 2026-11-14 — Codex V0.001 editor, Git and recovery progress

Added guarded folder creation, read-only Git history and branch inspection, waited for active indexing before closing a project's database, and recorded interrupted verification transactionally on project reopening. V0.001 build and all six tests passed, including folder validation, history lookup and reopening during verification on a disposable project. This does not prove process-crash recovery. Files: `electron/main.ts`, `electron/preload.ts`, `src/main.tsx`, `tests/desktop.test.mjs`, `CHANGELOG.md`, and this handoff. The approved plan is still incomplete; real independent review, approval, safe Apply and rollback remain disabled.

### 2026-11-14 — Codex continuing approved V0.001 implementation

Phil requested sustained work on the approved partially implemented features. I am adding per-file Git inspection and deleted-file staging while retaining the AI Apply block.

### 2026-11-14 — Codex local proposal and Git progress

Per-file Git status, diff, deletion staging and unstaging are implemented. Local Ollama can generate a bounded single-file proposal from a selected scratch file, store the proposal in project history, and request experimental feedback from a model with a different digest. Reopening preserves the proposal. A disposable real-model run using qwen3.5:4b and qwen3.5:9b passed; neither scratch nor project was changed. This is not the final independent review of a scratch change and does not unlock Apply. Safe application, conflict and crash recovery, approval, and verified coordinator remain incomplete. Keep V0.001.

### 2026-11-14 — Codex commit and gate safeguards

Commit review now checks the branch reference as well as the commit and staged tree; a switch between branches at the same commit requires review again. Gate status reports project snapshot conflicts and cancelled jobs. V0.001 build and all six tests passed. Apply and final review remain incomplete.

### 2026-11-14 — Codex Git rename handling

Per-file rename diffs include the old and new paths, and unstaging a rename restores both paths in the index without altering working files. Copies do not unstage the original. V0.001 remains the release version.

### 2026-11-14 — Codex scratch cleanup

A failed scratch creation cleans up only the new, exclusively created temporary workspace. Tested using an oversized file in a disposable project; existing workspaces remain intact. V0.001 is unchanged. This handles ordinary creation failures, not process-crash cleanup.

### 2026-11-14 — Codex interrupted scratch recovery

Scratch preparation is recorded before copying. Reopening a project records interrupted attempts and preserves any incomplete directory for inspection; ordinary errors are recorded and clean up the workspace created by that attempt. V0.001 remains unchanged. Recovery was tested by injecting an interrupted record; a real process-kill test and production Apply recovery remain outstanding.

### 2026-11-14 — Codex actual scratch interruption test

The optional `scripts/verify-scratch-crash.mjs` test held a disposable scratch copy and forced the test Electron main process to exit. Reopening the same project reported interrupted scratch preparation. This does not test production Apply or rollback crash recovery.

### 2026-11-14 — Codex import index

Search indexes JavaScript/TypeScript static import declarations, including module names, aliases, default, namespace and side-effect imports. Updates and deletions rebuild the records. Build V0.001 and all six tests passed, including alias/update/deletion checks. These are syntactic relationships; full symbol resolution is not implemented.

### 2026-11-14 — Codex Python import index

Python imports, aliases and wildcard imports now appear in project search. V0.001 built and all six tests passed, including import alias and wildcard checks. These are syntax records, not resolved references.

### 2026-11-14 — Codex C# using index

C# using directives, aliases and static imports are now searchable as syntactic relationships. V0.001 build and all six tests passed, including alias and static cases. Full symbol resolution and production AI Apply remain incomplete.

### 2026-11-14 — Codex local request history

Local proposal and experimental review attempts and errors are recorded. In-flight requests are recorded as interrupted on project reopening; this recovery case was simulated in the desktop tests. V0.001 built and all six tests passed. This does not make proposal review sufficient for production Apply.

### 2026-11-14 — Codex local usage display

The Usage panel shows input/output tokens reported by Ollama for successful local proposal and review requests, and counts attempts without complete reported usage as unknown. Manual chat is not included. Cloud usage and reserves remain unknown; no cloud routing is enabled. V0.001 and all six tests pass.

### 2026-11-14 — Codex paused and detailed handoff

Phil requested a pause and a detailed handoff. See `HANDOFF.md` in the project root. Two diagnostic changes are deliberately uncommitted: `electron/main.ts` and `scripts/verify-local-proposal.mjs`. The current real-model test has an intermittent project-generation/epoch failure that remains under investigation; the last build succeeded at V0.001, and the last complete automated suite before the diagnostic wording edits passed six tests. Production Apply remains disabled.

### 2026-11-14 — Codex local scratch worker in progress

Phil approved the single-file candidate plan. Building a separate source/work candidate snapshot and keeping the project and existing scratch untouched. Real-model generation failures remain under investigation; Apply remains disabled.

### 2026-11-14 — Codex single-file candidate progress

The local worker creates an attributed separate source/work candidate, preserves original scratch and project files, and displays a fingerprint-checked diff. V0.001 build and six automated tests passed; multiple disposable real-model runs passed. A deterministic forced-exit test reports interrupted candidate creation and preserves original files. The earlier intermittent project-generation error was not reproduced or conclusively diagnosed; retain fail-closed checks and investigate if it recurs. Production Apply remains unavailable.

### 2026-11-14 — Codex real-model diagnostic cause

The intermittent project-change error was reproduced with lifecycle and click tracing: an extra trusted click on Open a project in the visible disposable test window triggered a third project open during the request. The fail-closed generation check worked as designed. The real-model diagnostic now hides its disposable window; the hidden run passed, and the desktop regression suite passed 6/6 including an intentional in-flight project reopen. Production Apply remains disabled; V0.001 remains unchanged.

### 2026-11-14 — Copilot candidate check reporting and verification

Candidate evidence now reports checks as passed only when the complete build/test run is valid and matches the candidate snapshot and current commands; receipts also identify missing post-apply checks. The disposable real-model diagnostic now uses a real Node syntax check and functional test, prints the candidate diff/review/results, and passed with qwen3.5:4b and qwen3.5:9b. `npm run build`, `npm test` (21/21), and forced-exit candidate recovery passed. Production Apply remains disabled. See `COPILOT_REPORT.md` for files, evidence, limitations, and follow-up review points.

### Copilot checkpoint recovery continuation

Project reopen now records interrupted checkpoint state in durable job history and keeps Apply blocked; receipts flag recovery inspection, and explicit test-only recovery records its result without enabling production Apply. App-restart and process-interruption coverage passed, and the final suite passed 23/23. See `COPILOT_REPORT.md` for detailed evidence and remaining safety gaps.

### 2026-11-14 — Copilot recovery-status reconciliation

Preserved Codex's deduplicated-warning and multiple-checkpoint blocking fixes. Project reopen now also reconciles a previously blocked job if all checkpoints are clean, covering recovery completed before a status write was interrupted. Restart tests cover corrupt journals, missing checkpoint paths, later-edit refusal, and multiple checkpoints. Build, full tests (23/23), focused interruption tests, scratch-crash and real-model disposable diagnostics passed. Production Apply remains disabled. See `COPILOT_REPORT.md` for details and unverified safeguards.

### 2026-11-14 — Copilot recovery history and docs

Added a deduplicated `checkpoint_reconciled` event when a previously blocked job reopens with all checkpoints clean. Restart tests now assert this event and fail-closed handling for corrupt/missing checkpoints. Updated README and checkpoint experiment notes to distinguish disposable-only recovery from production Apply. Build, full suite (23/23), real-model candidate and forced scratch-crash diagnostics passed. Production Apply remains disabled. See `COPILOT_REPORT.md`.

### 2026-11-14 — Copilot recovery episode IDs

Reconciliation history now assigns distinct episode numbers, deduplicates clean reopens, and preserves fail-closed multi-checkpoint/corrupt/missing behavior. Restart test confirms two separate recovery episodes yield distinct events. `npm run build`, `npm test` (24/24), the disposable real-model workflow, and scratch-crash diagnostic passed. Production Apply remains disabled. See `COPILOT_REPORT.md`.

### Copilot local worker cancellation

Job cancellation now aborts only that job's local proposal/review request; regression coverage verifies unrelated manual chat is not aborted and the project file remains unchanged. Build and full suite passed (25/25); disposable real-model and scratch-crash diagnostics passed. Production Apply remains disabled. See `COPILOT_REPORT.md`.

### Copilot unsaved-editor approval gate

The app now reports dirty editor paths to the main process and blocks candidate approval/checkpoint preparation when state is unknown or unsaved; a disposable Electron regression verifies blocking and clean reload recovery. Build and full suite passed (26/26); the disposable real-model workflow and forced scratch-crash diagnostic passed. Production Apply remains unavailable and disabled; model approval was simulated and Apply used only the test route. See `COPILOT_REPORT.md` for exact files, results, and remaining safety gaps.

## Jetstream / Deskhand shared Laptop Agent

Jetstream V0.001 now prefers the existing Deskhand Task-agent configuration at `C:\Users\Phlip\Desktop\Deskhand\Deskhand-windows\Task-agent\agent_config.json`. It verifies local health and the scoped chat credential with a non-consuming request for a random turn ID. Credentials stay in Deskhand and are never sent to the renderer. Jetstream does not start a competing agent or stop the shared process; if Deskhand is configured but unavailable, the status remains unavailable until Deskhand starts it. When no Deskhand configuration exists, Jetstream can start the bundled agent with a local-only, remote-control-off configuration. Muse account connectivity and coordination remain unverified; no chat or coding job was submitted. Build passed at V0.001; full tests 29/29 passed; live shared attach and detach checks passed.

## Muse chat transport continuation

Jetstream now offers manual Muse messages through Deskhand's existing scoped chat mailbox, route muse/muse/deskhand. The main process keeps the scoped key out of the renderer, records Jetstream's own messages in its user-data SQLite database, polls only their turn IDs, and saves replies before acknowledging them. A failed submission is marked uncertain and is not automatically resent. This is manual chat, not Muse job coordination; model identity, account usage and reserve are not verified. A Phil-authorized read-only connection-test turn `2ae38c8a-6d8a-426d-8f06-4a5d8b20645a` was accepted by Deskhand's mailbox but remained queued without a reply during verification. Do not resubmit blindly; check the existing turn if the remote consumer becomes available. `npm run build` passed V0.001 and `npm test` passed 30/30. The live connection is not yet verified end to end.

## 2026-11-14 — Copilot Deskhand integration inventory

Updated `COPILOT_REPORT.md` with the Deskhand source/build comparison, existing Jetstream integration ownership, test results, and blockers. The Windows 100.051 candidate does not match the available 100.050 executable/service; the requested Android candidate is incomplete, while the separate 100.050 Android source does not match the available 100.038 APK. Android unit tests were freshly run on that separate baseline (`.\gradlew.bat testDebugUnitTest --offline --no-daemon --rerun-tasks`: BUILD SUCCESSFUL, 32 tasks); Jetstream build passed at V0.001 and `npm test` passed 30/30. No phone/device or Deskhand host was available for remote-view acceptance. The existing Muse test turn still has no verified reply; it was not resubmitted or claimed. No Deskhand source was copied, installed, deployed, or restarted. Keep phone job actions and production Apply unavailable until the source, authenticated API, phone path, and separate Apply safeguards are verified.

## 2026-11-14 — Copilot Jetstream Job API continuation

Added a loopback-only authenticated Jetstream `/api/v1` job API, durable idempotency ledger, and disposable unit/Electron coverage for job reads, candidate diff/review/check evidence, submit/pause/resume/cancel/reject, duplicate requests, and uncertain restart outcomes. Candidate metadata omits its path; check commands/output are redacted, though diffs and receipts can contain local paths and project content. `npm run build` passed at V0.001; `npm test` passed 38/38; focused API tests passed 8/8. The desktop fixture used mocked model responses only; no real-model run or Phil approval occurred, and Apply remains unavailable. The native Deskhand adapter/relay/phone panel and real-phone viewing are still not implemented. A non-consuming Muse GET returned HTTP 200 but no undelivered reply for the existing test turn; no claim, resubmit, or acknowledgement was made, and the external VM claimant state remains unknown. See `COPILOT_REPORT.md` for source provenance, safety limits, test details, Git/version state, and recommended next steps.

## 2026-11-14 — Embedded Deskhand controls in Jetstream Settings

Implemented `src/PhoneConnection.tsx`, the Settings Phone connection section, and the top-right phone shortcut directly into that section. Tailscale sign-in opens the provider browser login; network status, QR generation/cancellation, pending pairing approval (view/control/reject with confirmation code), pause/resume remote access, disconnect, orientation, paired-phone control/revoke, and host approval requests are rendered inside Jetstream. Normal workspace settings remain a separate section. No second Deskhand window is required for these controls.

The embedded Windows host now accepts `--jetstream --minimized` and serves the same-user-only `.Desktop` named pipe through `integrations/deskhand/windows/Deskhand.Windows.App/JetstreamDesktopBridge.cs`. Calls run on the existing host UI thread and reuse HostEngine/MainForm methods; `electron/main.ts` and `electron/preload.ts` expose restricted renderer operations. `integrations/deskhand/Start-Deskhand.ps1 -EmbeddedUI` starts the host hidden using the existing isolated `%LOCALAPPDATA%\JetstreamCoder\Deskhand-verification` configuration, preserving verification pairings. Imported backend/runtime files remain under this repository and packaged bundle. Standalone Deskhand files were not changed.

Real Android verification: attached Samsung SM-A356U (RFCY10TCC8J), verification app, existing paired phone connected with control allowed; embedded network Running at 100.108.34.15. Android screenshot showed the live Windows desktop. Opened a disposable project `C:\Users\Phlip\AppData\Local\Temp\jetstream-phone-acceptance-WF5Xk9`; the phone identified the correct project, submitted job #1 `Phone test only - do not modify files`, paused (API confirmed true), resumed (API confirmed false), and cancelled the job (phone and API both confirmed cancelled). Project sample file stayed unchanged. Submit correctly showed `blocked: coordinator unavailable` before cancellation. This proves transport and job controls, not live Muse coding execution. Existing Muse queued turn was not consumed or resubmitted. Fresh QR re-pairing on physical Android was not repeated; real pairing approval is covered by the integrated Settings fixture test, not a new physical pairing ceremony. Desktop streaming was verified over Tailscale while USB supplied ADB inspection; cellular/off-site reliability remains unverified.

Added `tests/phone-settings.test.mjs` exercising shortcut -> main Settings, Tailscale sign-in, pending approval code and control grant, phone control revoke, pause/resume, and section navigation through fixture IPC. The isolated test passed. V0.001 retained. Production Apply and remote candidate approval remain unavailable. Native chat/history, startup/service diagnostics, and other standalone Deskhand auxiliary panels are not all ported by this change; these are not required for the paired desktop/job path. Muse VM worker still needs its external Linux runtime.
Startup refinement: Jetstream now proactively starts/checks its embedded phone host at normal app launch, so an already paired phone can connect without somebody first opening Settings on the PC. Existing test/agent-disable flags prevent test processes from starting the real host. The host preserves its remote session independently of opening/closing Settings. Final regression run before this guarded startup call: npm test 41/41; Settings test and full suite cover the final navigation. Build and packaging retain V0.001.
Final output: `artifacts/Jetstream-V0.001-1794717913451/Jetstream/electron.exe`. Packaged smoke test used the real host and confirmed Settings > Phone connection, a visible QR, and SM-A356U Connected / Control allowed. Final affected tests passed 2/2 after the guarded startup addition; preceding full suite passed 41/41. Build succeeded V0.001 (existing bundle-size warning only). Desktop shortcut updated to this final bundle and app launched. Fresh QR scanning/approval on Android and off-site/mobile-network testing remain pending; do not interpret the existing-pair transport checks as a new physical pairing test.

## 2026-11-15 — Expanded Deskhand feature parity continuation

Detailed current-state handoff saved in `HANDOFF_DESKHAND_INTEGRATION.md`, including paths, newest bundle versus older shortcut/live host, implemented Settings feature bridge, Android toolbar regression fix, test results (Jetstream 42/42, Android 357/357, native boundary 10/10), incomplete physical drawer/orientation tests, source-path Task Agent dependency and next steps. User resumed integration after asking for handoff; continue from its Next moves rather than treating all parity as complete.

## 2026-11-15 — Live new-host smoke and phone drawer/orientation evidence

New host from `artifacts/Jetstream-V0.001-1794718828486` replaced only the verified old embedded host. Live Settings loaded all 12 diagnostics and 9 real catalog routes; Muse page rendered; concurrent status returned in 4ms and no nonempty alerts were present. Both real phone drawers opened and resized (files handle x506→703; agents x506→335), and Board tab opened. Agents feed reports unavailable/unknown: not full live feed acceptance. Orientation follow caused actual PC 900×1600 portrait and 1600×900 landscape with phone hierarchy rotation=1. Test stopped after phone foreground changed; original phone rotation preferences (auto=1, user=0) and host follow=false were restored. Host disconnect left landscape1600×900. Explicit phone Stop, touch mapping, voice and fresh physical pairing still pending. Imported Android native libraries trigger the 16KB compatibility debug notice; acknowledged OK without hiding warning. Full details in `docs/deskhand-feature-parity.md`. Do not infer complete parity or Muse reply readiness from these checks.

Embedded Task Agent source-path follow-up implemented: AssistantClient now supplies bundle/check-out-local DESKHAND_TASK_AGENT_DIR and fails locally if assets are missing. Both imported Python adapters honor it without overriding explicit constructor configuration. New path tests2/2, original Task Agent client14/14 and lifecycle21/21 passed. No credentials copied or remote-access grant changed. See parity doc; prior handoff "path dependency not fixed" is superseded for embedded service code resolution.

Final bundle1794719656536 native/package smoke passed (9 catalog routes, no alerts). Embedded host and desktop shortcut now use that bundle; app launched. Version V0.001 retained. Detailed handoff `HANDOFF_DESKHAND_INTEGRATION.md` and feature matrix `docs/deskhand-feature-parity.md` updated. Remaining external acceptance is not claimed complete.

## 2026-11-15 — User-requested rebuild and launch

Rebuilt Windows host and Jetstream successfully at V0.001; packaged `artifacts/Jetstream-V0.001-1794719863119`. Updated desktop `Jetstream Coder.lnk` to its `Jetstream/electron.exe`, replaced only the previously identified embedded verification host with this bundle's host, and launched Jetstream. This supersedes1794719656536 as current desktop build. Existing pairings/profile retained. Build-only request: prior test results remain recorded; no feature changes or new full-suite run in this rebuild.

## 2026-11-15 — Five-recommendation continuation

See `docs/prototype-milestone-2026-11-15.md`. Real local model proposal/review/build/test/disposable apply/rollback passed with simulated approval. Native16KB Go library rebuilt from preserved source and APK alignment passes; installed Android build. Muse readiness payload/host-only checks corrected (13 native checks,30 Python Muse-service tests); browser observed Ready after user sign-in. Read-only turn ead84160-8572-4896-b893-11169a0bdd34 cancelled, no reply; do not replay. Earlier old tunnel turn untouched. Full desktop/recovery and Android reruns in progress after toolbar overlap regression discovered on device. User authorized Git milestone backup; preserve V0.001 tag. Initial last-turn tool rejection was automatic approval review usage-limit failure; command not run, resumed only after user requested continuation.
