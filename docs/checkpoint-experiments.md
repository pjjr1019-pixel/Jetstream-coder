# Checkpoint and exclusive-write experiments

These modules and tests are development fixtures only. They are not wired to the app's Apply gate and must not be used for real projects.

`checkpoint.ts` prepares and inspects a single-file checkpoint, `exclusive-write.ts` tests an exclusive Windows compare-and-write, and `checkpoint-journal.ts` validates an append-only state sequence. `checkpoint-fixture.ts` combines them only for disposable tests.

Remaining safety work includes preventing target-path substitution and reparse-point races, durable ordering across crash and power loss, handling a process dying during an in-place write without overwriting later edits, recording and presenting recovery safely, integrating exact approval evidence and dirty-editor protection, and running post-apply checks. A thrown test callback is not a real process-termination or power-loss test. Production Apply remains disabled until those requirements are met.
