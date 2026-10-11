# OpenClaw Bridge

Current architecture for the OpenClaw-based Herdr Bridge. API source of truth for this implementation: the installed OpenClaw 2026.9.9 SDK and Slack adapter. See the [behavior contract](SPEC.md) and [user guide](GUIDE.md) for scope and setup. Only the Slack route is currently supported.

## Ownership

- Herdr owns terminals, agent detection and native read/prompt/send-keys.
- Python worker owns reconciled status events, durable requests/attempts/outbox, one-shot dispatch per attempt and read-only follow-up. It opens no network listener and consumes no Slack events.
- OpenClaw plugin owns worker lifecycle, deterministic human commands, read tools, sender/account checks and portable message presentations.
- OpenClaw Slack adapter owns credentials, Socket Mode, inbound policy, native rendering, provider requests and interaction acknowledgement/edits.

Verified SDK APIs: `registerService`, `registerCommand`, `registerTool`, `registerInteractiveHandler`, `api.runtime.channel.outbound.loadAdapter` and `api.runtime.config.current()`. Delivery uses the channel's normalization, presentation rendering and `sendPayload`.

OpenClaw 2026.9.9 requires `contracts.tools` in the manifest for registered agent tools. Reloading after that declaration changes needs `openclaw plugins reload herdr-bridge --accept-capabilities`. A metadata-only inspect does not import plugin code; use `openclaw plugins inspect herdr-bridge --runtime --json` to verify actual registrations and diagnostics.

Slack 2026.9.9 only executes certain core approval command buttons. Bridge instead uses a portable callback action `herdr-bridge:<request-id>:<option>` and registers the `slack` / `herdr-bridge` namespace. OpenClaw supplies authenticated sender/account/auth context. Callback text is never authorization.

Button labels are compact numbered summaries; full labels and command evidence remain in the terminal excerpt. Slack's renderer downgrades an entire control row to text if any label exceeds its native limit, so labels are bounded before native rendering. The authenticated `herdr-bridge:probe` callback is a no-op connection check that edits the original card with its result instead of relying on an ephemeral reply.

Message titles contain the Herdr display name, actual session model and full cwd. Herdr tab labels provide the name when the agent response has no name field. Codex model resolution prefers the exact session and latest turn context; without a session ID it requires a single live pane and a unique non-subagent workspace session. Claude reads the exact session's latest assistant model. Global model defaults are never used. Below the header, a highlighted inline-code summary extracts the pending command or latest assistant paragraph using deterministic rules, without an LLM call. Captured visible terminal evidence remains below it; it is not a complete off-screen history archive. `presentationTextMode: "fallback"` keeps accessibility/notification text from rendering as a second heading.

## Private protocol

Stdin/stdout carry one JSON object per line:

```json
{"id":"bridge-1","method":"read","params":{"pane":"w4:p2"}}
{"id":"bridge-1","result":{"agent":{"pane_id":"w4:p2"},"text":"..."}}
```

Methods: `status`, `read`, `send`, `pending`, `answer`, `events`, `ack`, `check`, `retry`, `prepare_answer`, `dispatch_reserved`, `bind_card`, `result_cards`, `event_for_delivery`. Errors have `id` and `{code,message}`. A four-thread executor allows status queries during confirmation; responses remain correlated by ID and stdout writes are serialized. Logs go to stderr. Lost responses never automatically resend terminal writes.

Events contain stable event IDs, agent identity, timestamp and terminal evidence. `approval.requested` adds request ID, expiry, state sequence, fingerprint and exact options. Unrecognized dialogs become `agent.blocked` without remote options. The tested event reconciler handles fast transitions and reconnection; a scan recovers agents blocked before startup and dialogs changing while still blocked.

Approval cards, unsupported blocked-dialog notices and Codex async question cards wait **10 seconds from first discovery**. Bridge retains the pending request immediately, but releases its notification only if the same request is still unresolved. Local answers, turn completion, expiry or replacement cancel the queued card. A replacement request starts its own delay; duplicate scans and Gateway restarts preserve the existing deadline. Live-read failures defer delivery until evidence is available. Completion notices and answer-result updates have no added delay. The five-second delivery poll can make a card arrive a few seconds after the deadline; this is a minimum grace period, not an exact phone push timer.

The durable event JSON stores `notify_after`; older queued events derive it from their original discovery timestamp. `events` filters by eligibility before selecting its 20-item ready batch. `event_for_delivery` repeats identity, expiry and unresolved-request checks directly before the plugin invokes the portable sender, including live dialog fingerprints and exact structured question evidence. Delivery revalidation never writes terminal input.

OpenClaw acknowledges Slack transport before invoking Bridge. That acknowledgement is separate from the business result. A Slack choice first creates a durable `reserved` attempt without writing keys, edits the card to checking, then revalidates owner and live dialog before `dispatch_reserved`. Commit persists `dispatching` before the one key write. Request idempotency prevents repeated commits, and per-pane exclusion prevents overlapping dispatch/initial confirmation.

A five-second polling window checks identity, status/sequence and visible dialog. Two consistent observations of an advanced sequence and the original dialog disappearing produce `confirmed`, including a recognized new blocked dialog. An unchanged original approval produces `unconfirmed`; unreadable/replaced agents produce `uncertain`. A scan every five seconds continues read-only checks for up to two minutes, bounded by approval expiry; restarted workers resume observation only. Legacy `dispatched` records remain consumed. Diagnostic records retain attempt IDs, fixed options/keys, send timing, focus/revision and observations; logs omit command contents.

Repeated choice clicks return current results. `check:<request-id>` is read-only. `retry:<request-id>:<attempt-id>` authorizes one new attempt of the same option only after original identity, sequence, blocked dialog and expiry checks. Commit revalidates after the progress edit. Old retry tokens cannot create another attempt. Unknown results expose retry only after fresh checks establish the unchanged original dialog. A reserved attempt whose progress edit failed is explicitly resumable, with no automatic write.

Result changes queue unique `approval.updated` events and persist authenticated card locations in SQLite; acknowledging an older event cannot delete a newer result. Background edits read the latest result inside a per-card queue. During the process, they reuse the authenticated callback's `respond.editMessage`; no response URLs are persisted. After restart/editor eviction, one replacement recovery card per request update is sent via OpenClaw's portable adapter. The public 2026.9.9 SDK has no general background edit method, and Slack's `approval.native` runtime context exists only when OpenClaw native approvals are enabled, so Bridge does not depend on that context or load provider clients/tokens. Failed result delivery remains queued and does not block unrelated events.

Herdr 0.9.3's [send-keys handler](https://github.com/herdrdev/herdr/blob/v0.9.3/src/app/api/agents.rs) returns `ok` after `runtime.try_send_bytes` accepts the encoded bytes. It does not wait for the agent TUI to process them. This is why Bridge must observe terminal acceptance separately.

Codex approval evidence begins at its current command question; transcript updates above it cannot change the request fingerprint. Unknown screen redraws notify only once per terminal/status generation. Replaced requests become superseded, and queued cards are discarded when the agent advances or the request expires. Dispatch rechecks the status sequence after reading the dialog as well as before it.

## Codex async question adapter

`questions.py` reads the local Codex state database and incrementally tails the exactly matched rollout. The source contract is pinned to Codex CLI [rust-v0.162.1](https://github.com/openai/codex/tree/rust-v0.162.1), commit `092d3acd6bec3e3a14bdc7e7a2810ab628ab759d`: `core/src/tools/handlers/request_user_input_async.rs`, `tui/src/bottom_pane/async_questions/state.rs`, `tui/src/async_question_reply.rs` and `context-fragments/src/answered_question.rs` under `codex-rs/`. These files establish completed async message items, question identity, bounded suggestions and native human reply framing. This does not infer questions from model output or introduce a second agent protocol service.

Only completed `event_msg.item_completed` async `AgentMessage` items in the active turn create `question.requested` events. Identity is the compact JSON array `["request_user_input_async", message_id, index]`. A user `response_item.message` containing the native `<send_user_message_question_reply>` envelope resolves its exact IDs. Turn completion/abort clears remaining questions. Partial rollout lines are deferred, file replacement revalidates session identity, and reads are serialized across scan/confirmation threads. The database is opened read-only; no Codex state is modified.

Exact Herdr session identity is preferred. Fallback matching requires both a unique live Codex pane for the cwd and a unique unarchived non-subagent Codex thread for that cwd. Discovery never opens the queued menu. Question requests survive ordinary Herdr status/sequence changes and carry their own turn-bound fingerprint and Bridge expiry. They are independent of command approvals.

Before dispatch, Bridge checks the exact pending question, terminal identity, visible input surface, current state consistency and then structured evidence again. An exact expanded suggestion menu supports one-digit submission. A collapsed pending-count summary plus known empty main composer permits `agent.prompt` with Codex's native question reply, including while working. Live testing found Herdr can report the collapsed summary as blocked. Only for that verified input surface, one `pane.send_input` call encodes the native reply and Enter together; this is chosen before dispatch, never a fallback after an uncertain write. Unrecognized blocked dialogs still reject submission. Free-text `/herdr reply` uses this composer path only. Layout changes, drafts, mismatched counts or unrelated blocked approvals reject the write. Local skip has no durable reply event, so input checks reject mismatched menus/counts while the card can remain until turn end.

Question confirmation polls for the exact ID and answer in durable user input, with two consistent observations. It does not rely on `blocked → working`, approval disappearance or prompt send receipts. Same-attempt idempotency, explicit retry and read-only recovery remain shared with approvals. Question results carry `kind: codex_question` to avoid claiming a command approval closed. A text command without an interactive card binding receives a portable follow-up status message before its result event is acknowledged. Existing authenticated callback editors continue to update choice cards.

The textual empty-composer guard recognizes bounded known layouts; it is not an atomic lock against local typing between the final read and prompt submission. Ordinary queued messages, synchronous questions, multi-select and other agent question protocols are not implemented by this adapter.

## Rollout and scope

1. Save the old implementation (`c9f17c8`).
2. Develop on `feature/openclaw-bridge`, branched from `develop`.
3. Check Python safety tests, JavaScript plugin tests, actual plugin discovery, config validation and live Herdr reads.
4. Configure owner/route, stop the old runtime, restart Gateway.
5. Verify the no-op callback; separately verify a harmless real terminal approval from phone. Provider delivery alone does not verify push notification or terminal dispatch.

Herdr approvals remain owned by Bridge; they are not inserted into OpenClaw's native `exec.approvals` store. Unsupported dialogs require local handling. New-agent creation, arbitrary raw keypads, implicit thread-to-pane routing and auto-approval are not exposed.

Messages may duplicate after ambiguous provider results. Each terminal attempt is sent at most once by Bridge; only an explicit eligible retry permits a new attempt. Herdr has no atomic compare-and-send-keys operation or agent-processing receipt, so fingerprint/identity checks cannot eliminate simultaneous local input or delayed-input races, and terminal exactly-once handling is not guaranteed.

Legacy tests read `legacy/herdr-plugin.toml`. The root manifest registers `herdr-bridge` and has no old runtime startup hook.

## Verified on 2026-10-10

- Full Python regression after the heading update: 877 passed, 5 opt-in live tests skipped. JavaScript plugin tests: 8 passed.
- Real NDJSON worker: `status` returned 3 local agents; `read` returned the requested pane; process exited cleanly on EOF.
- Actual OpenClaw runtime inspection: `herdr` command, `herdr-bridge` service, `herdr_status` / `herdr_read` tools, no diagnostics.
- Gateway owns the running worker; the old standalone bot process is stopped. Slack account `default` is connected.
- OpenClaw provider receipts and a readback of the real bot DM confirmed native approval cards with `openclaw:callback_button` actions and exact Bridge request IDs. These are top-level messages.
- A real `herdr-bridge:probe` interaction reached the handler and updated the Slack DM card to “Herdr Bridge callback connected. No terminal operation was performed.” Both the Gateway callback log and Slack UI confirmed this.
- A real, read-only `/usr/bin/uname -s` approval completed through Slack. Durable state recorded `dispatched` with exactly one dispatch log entry; the command returned `Darwin`. The recipient confirmed an OpenClaw push while Slack was backgrounded and the phone locked, and that the original card showed `dispatched` after the click. This verifies this configured phone/account, not every Slack notification policy.
- Bridge safety coverage includes status-sequence changes during discovery and immediately before dispatch. Callback audit logs record request IDs and selected indices without command contents.
- The installed Slack renderer produced one header, then the highlighted summary, transcript, request context and native buttons. A real DM preview confirmed the Herdr tab name, actual model and cwd with no duplicate plain heading.

Local configuration is in OpenClaw, with owner-only Slack DM ingress and the two owner-gated read tools enabled. The ignored `openclaw.local.patch.json` contains this machine's paths and owner ID, not tokens.

## Approval confirmation follow-up, 2026-10-10

- Regression after adding terminal confirmation: Python 886 passed, 5 opt-in live tests skipped; JavaScript 9 passed. New cases cover successful send receipts with dropped input, delayed acceptance, transient frames/read failures, replacement agents, next dialogs and crashes during confirmation.
- After reloading Gateway, a real `/usr/bin/uname -m` approval was answered through Slack. Exactly one digit was sent. Two consistent observations confirmed the original approval closed after approximately 630 ms; durable state recorded `confirmed`, and the command returned `arm64`.
- The reported intermittent input failure has not been reproduced live. The fix removes success claims based only on send acknowledgement and retains diagnostics for the next occurrence; it does not claim to eliminate input loss inside Herdr or the agent TUI.

## Approval recovery follow-up, 2026-10-10

- Reviewed [herdr-remote's interaction handling](https://github.com/dcolinmorgan/herdr-remote/blob/bb70162eb6d3ab6413974b14cdd7f3763f14a38c/relay/herdr_telegram.py#L652-L686), [live prompt guard](https://github.com/dcolinmorgan/herdr-remote/blob/bb70162eb6d3ab6413974b14cdd7f3763f14a38c/relay/herdr_relay.py#L3255-L3333) and double-tap tests at commit `bb70162`. Adopted progress-before-write, pane exclusion and live prompt checks in Bridge's own implementation. That reference reports CLI send success without verifying terminal acceptance; Bridge retains terminal confirmation and durable attempt results.
- Python regression: 905 passed, 5 opt-in live tests skipped. JavaScript: 20 passed. Coverage includes duplicate choices/retry tokens, concurrent dispatch, progress-edit failure, live revalidation, crashes without replay, bounded follow-up and durable result delivery. Installed Slack renderer separately produced native check/retry buttons.
- At 22:57 Dubai time, a real `/usr/bin/uname -s` approval reached the Slack callback. Request `c2e5ffe54096436397386da9aaacdec6`, attempt `38af9b333a794b4bb87be03c311ebf06`, sent one digit (`1`), observed stable `working` sequence 532 and the original dialog gone after 644 ms, and persisted `confirmed`. The command returned `Darwin`; reading the real Slack card confirmed the final status and cleared choice controls.
- Live verification exposed the conditional native-approval context dependency. Replaced it with callback edits plus portable recovery delivery. Gateway restart delivered the persisted result update successfully and cleared the outbox; a subsequent authenticated callback also completed and its queued background update was delivered. No terminal input was replayed by recovery.
- The intermittent dropped-input case and explicit retry have deterministic regression coverage, but have not been reproduced as a real failing Herdr approval. These results do not establish that the reported input-loss rate is eliminated.

## Codex async question verification, 2026-10-11

- Full regression after the final input-state fix: **940 Python tests passed**, 5 opt-in live tests skipped; **25 JavaScript tests passed**.
- The first live color question exposed Herdr reporting a collapsed queued question as `blocked` while the TUI displayed `Working`. The Slack choice was rejected before input. The final adapter distinguishes that verified empty-composer UI from unrelated approvals and chooses one encoded pane submission before dispatch.
- At 10:19 Dubai time, the owner answered the second color question only through Slack. Request `c8fcfee6f9bb4803a3a84199794c8e94`, attempt `e74ae554e90b442bbd2f2fe40dd4358b`, submitted the native answer envelope once. Codex recorded `紫色标签` for exact question ID `["request_user_input_async","call_ae330806efd14d2cb0f7444c6210f004",0]`; two observations produced `confirmed` after 649 ms. No terminal command was executed by this test.
- Independent Slack readback of original card `1791699571.404789` confirmed “Codex recorded the answer to this question” and no remaining choice buttons. The durable result update was delivered and its outbox event cleared.
- This live result covers collapsed async questions on Codex CLI 0.162.1, Herdr 0.9.3 and OpenClaw 2026.9.9. Expanded-menu selection, free-text commands, drafts, explicit retries and restart no-replay have deterministic test coverage; they were not all exercised live in this run. A separate locked-phone push test was not repeated for question cards.
