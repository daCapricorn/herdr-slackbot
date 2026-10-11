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

Methods: `status`, `read`, `send`, `pending`, `answer`, `events`, `ack`, `check`, `retry`, `prepare_answer`, `dispatch_reserved`, `bind_card`, `result_cards`. Errors have `id` and `{code,message}`. A four-thread executor allows status queries during confirmation; responses remain correlated by ID and stdout writes are serialized. Logs go to stderr. Lost responses never automatically resend terminal writes.

Events contain stable event IDs, agent identity, timestamp and terminal evidence. `approval.requested` adds request ID, expiry, state sequence, fingerprint and exact options. Unrecognized dialogs become `agent.blocked` without remote options. The tested event reconciler handles fast transitions and reconnection; a scan recovers agents blocked before startup and dialogs changing while still blocked.

OpenClaw acknowledges Slack transport before invoking Bridge. That acknowledgement is separate from the business result. A Slack choice first creates a durable `reserved` attempt without writing keys, edits the card to checking, then revalidates owner and live dialog before `dispatch_reserved`. Commit persists `dispatching` before the one key write. Request idempotency prevents repeated commits, and per-pane exclusion prevents overlapping dispatch/initial confirmation.

A five-second polling window checks identity, status/sequence and visible dialog. Two consistent observations of an advanced sequence and the original dialog disappearing produce `confirmed`, including a recognized new blocked dialog. An unchanged original approval produces `unconfirmed`; unreadable/replaced agents produce `uncertain`. A scan every five seconds continues read-only checks for up to two minutes, bounded by approval expiry; restarted workers resume observation only. Legacy `dispatched` records remain consumed. Diagnostic records retain attempt IDs, fixed options/keys, send timing, focus/revision and observations; logs omit command contents.

Repeated choice clicks return current results. `check:<request-id>` is read-only. `retry:<request-id>:<attempt-id>` authorizes one new attempt of the same option only after original identity, sequence, blocked dialog and expiry checks. Commit revalidates after the progress edit. Old retry tokens cannot create another attempt. Unknown results expose retry only after fresh checks establish the unchanged original dialog. A reserved attempt whose progress edit failed is explicitly resumable, with no automatic write.

Result changes queue unique `approval.updated` events and persist authenticated card locations in SQLite; acknowledging an older event cannot delete a newer result. Background edits read the latest result inside a per-card queue. During the process, they reuse the authenticated callback's `respond.editMessage`; no response URLs are persisted. After restart/editor eviction, one replacement recovery card per request update is sent via OpenClaw's portable adapter. The public 2026.9.9 SDK has no general background edit method, and Slack's `approval.native` runtime context exists only when OpenClaw native approvals are enabled, so Bridge does not depend on that context or load provider clients/tokens. Failed result delivery remains queued and does not block unrelated events.

Herdr 0.9.3's [send-keys handler](https://github.com/herdrdev/herdr/blob/v0.9.3/src/app/api/agents.rs) returns `ok` after `runtime.try_send_bytes` accepts the encoded bytes. It does not wait for the agent TUI to process them. This is why Bridge must observe terminal acceptance separately.

Codex approval evidence begins at its current command question; transcript updates above it cannot change the request fingerprint. Unknown screen redraws notify only once per terminal/status generation. Replaced requests become superseded, and queued cards are discarded when the agent advances or the request expires. Dispatch rechecks the status sequence after reading the dialog as well as before it.

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
