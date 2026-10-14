# Bridge transaction and error contract

Public state-changing operations own their database transaction. Internal approval and input helpers stage updates without committing. Transcript savepoints run inside an explicit owning transaction. Source cursors, completion claims and their outbox records roll back together when that operation fails.

An approval dispatch or prompt reservation commits before any terminal write. In-memory operation ownership is claimed after successful commit while the state lock remains held. A failed commit produces no terminal write and leaves no operation ownership behind. A lost write acknowledgement remains uncertain and does not trigger an automatic resend.

## RPC errors

The Python worker returns `error.code`, `error.message`, `error.details` and `error.outcome`. The OpenClaw Worker preserves these fields on its rejected error. Human-readable messages are retained, but callers can use stable codes instead of parsing those messages.

`outcome` describes terminal effects of the current invocation:

| Outcome | Meaning |
| --- | --- |
| `not_sent` | Evidence shows this invocation did not submit a terminal operation. An earlier durable attempt may still exist. |
| `unknown` | The error does not establish whether a terminal operation was submitted or accepted. |

Neither outcome authorizes retry. Request and attempt state still determine whether an explicit retry is allowed. Transport acknowledgement does not confirm terminal acceptance or task completion.

Representative codes include `unknown_method`, `invalid_prompt`, `stale_submission`, `worker_not_running`, `worker_timeout`, `worker_send_failed` and `worker_disconnected`. Timeout, disconnect and write errors after invocation preserve `unknown`. Invalid RPCs before invocation and proven pre-write guards return `not_sent`.

## Verification, 2026-10-14

The combined transaction and error changes passed 1478 Python tests with five opt-in live tests skipped, and all 57 JavaScript tests. Independent review covered helper transaction ownership, deferred foreign-key commit failures, reservation durability, unchanged transaction logic after error classification, and conservative post-attempt errors. A real Python process and the actual Worker exchanged NDJSON errors for an invalid method and a post-invocation exception, preserving `not_sent` and `unknown` respectively. That probe performed no terminal or Slack operations.

The global-lock I/O correction is a separate pending stage. These results do not establish that a slow pane cannot block unrelated requests, or verify new smartphone notifications.
