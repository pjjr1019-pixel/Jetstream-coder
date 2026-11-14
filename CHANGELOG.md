# Jetstream Coder changelog

## V0.001

- Local workers create a separate single-file scratch candidate with a saved source snapshot, model attribution and reviewable diff.

- Usage shows local proposal and review token counts reported by Ollama and labels unreported or interrupted attempts unknown.

- Interrupted local proposal or review requests are recorded when a project reopens.

- Job history records local proposal and experimental review attempts and failures.

- Search indexes C# using directives, including aliases and static imports.

- Search also indexes Python import statements, aliases and wildcard imports.

- Search now indexes JavaScript and TypeScript import declarations, including aliases and source modules, and updates them when files change.

- Local proposal review rejects the author model name even if its installed digest has changed.

- Interrupted scratch preparation is recorded on project reopening, with the incomplete path retained for inspection.

- Failed scratch creation removes its own incomplete temporary workspace instead of leaving an orphan.

- Per-file rename diffs and unstaging now include both the original and new paths.

- Gate status now reports cancelled jobs and project files that differ from the scratch baseline.

- Commit previews now also verify the current branch, even when two branches point to the same commit.

- Added per-file Git change inspection and tracked-deletion stage/unstage.
- Added recoverable single-file local scratch proposals and experimental review by a model with a different digest; project Apply remains unavailable.

- Untracked project files can be previewed from Git status while excluded files remain hidden.

- Added per-file Git status and diff inspection, including staging and unstaging tracked deletions.

- Added per-file Git status and diff inspection, including staging tracked deletions.

- Interrupted verification is recorded in job history on project recovery.

- Added Git history and branch inspection.

- Added project folder creation and improved index safety when switching projects.

- Added Python and C# definition and possible call indexing.

- Added index status and an explicit rebuild control; index updates are serialized.

- Job search now includes recorded history, and verification outcomes are recorded in the job history.

- Verification now invalidates runs if the team is paused or check settings are changed and restored mid-run.

- Improved verification to recheck pause and command settings before recording results.

- Added creation of new files within the open project without overwriting existing files.

- Added a staged-change review and commit flow for the open Git repository.

- Cancelled jobs can be requeued while retaining their history; checks must be repeated.

- Added project-specific exclusion settings for search and new scratch copies.

- Added job cancellation with retained history and scratch files. Staging now checks that the file on disk matches the saved editor version.

- Search results open at the matching line; open files warn when they change on disk.

- Added unstage for the current file, including new repositories with no commits.

- Added staging for the current saved file and search for recorded jobs.

- Added source-control diff inspection and improved scratch snapshot and new-file conflict checks.

- Added a Reload button with a warning before discarding unsaved edits.

- Hardened verification: native command failures fail checks, and previous passing evidence is invalidated when checks restart or commands change.

- Kept rebuilds at V0.001 until the stable base is ready and Phil chooses to advance the version.

- Added the version number to the bottom-right corner and a changelog opened by clicking it.
- Rebuilds retain V0.001 until the base is stable and the version is deliberately changed; successful builds add pending changelog notes.
- Includes the current editor, interactive terminal, local model panel, job gate, and test-only checkpoint prototype.

