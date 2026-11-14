# Jetstream Coder changelog

## V0.001

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

