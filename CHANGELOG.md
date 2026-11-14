# Jetstream Coder changelog

## V0.001

- Added source-control diff inspection and improved scratch snapshot and new-file conflict checks.

- Added a Reload button with a warning before discarding unsaved edits.

- Hardened verification: native command failures fail checks, and previous passing evidence is invalidated when checks restart or commands change.

- Kept rebuilds at V0.001 until the stable base is ready and Phil chooses to advance the version.

- Added the version number to the bottom-right corner and a changelog opened by clicking it.
- Rebuilds retain V0.001 until the base is stable and the version is deliberately changed; successful builds add pending changelog notes.
- Includes the current editor, interactive terminal, local model panel, job gate, and test-only checkpoint prototype.

