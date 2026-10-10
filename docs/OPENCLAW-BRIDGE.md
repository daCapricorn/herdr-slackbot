# OpenClaw Bridge

Current architecture for the OpenClaw-based Herdr Bridge. API source of truth for this implementation: the installed OpenClaw 2026.9.9 SDK and Slack adapter. See the [behavior contract](SPEC.md) and [user guide](GUIDE.md) for scope and setup. Only the Slack route is currently supported.

## Ownership

- Herdr owns terminals, agent detection and native read/prompt/send-keys.
- Python worker owns reconciled status events, durable pending requests/outbox and one-shot dispatch. It opens no network listener and consumes no Slack events.
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

Methods: `status`, `read`, `send`, `pending`, `answer`, `events`, `ack`. Errors have `id` and `{code,message}`. Logs go to stderr. Lost responses never automatically resend terminal writes.

Events contain stable event IDs, agent identity, timestamp and terminal evidence. `approval.requested` adds request ID, expiry, state sequence, fingerprint and exact options. Unrecognized dialogs become `agent.blocked` without remote options. The tested event reconciler handles fast transitions and reconnection; a scan recovers agents blocked before startup and dialogs changing while still blocked.

Codex approval evidence begins at its current command question; transcript updates above it cannot change the request fingerprint. Unknown screen redraws notify only once per terminal/status generation. Replaced requests become superseded, and queued cards are discarded when the agent advances or the request expires. Dispatch rechecks the status sequence after reading the dialog as well as before it.

## Rollout and scope

1. Save the old implementation (`c9f17c8`).
2. Develop on `feature/openclaw-bridge`, branched from `develop`.
3. Check Python safety tests, JavaScript plugin tests, actual plugin discovery, config validation and live Herdr reads.
4. Configure owner/route, stop the old runtime, restart Gateway.
5. Verify the no-op callback; separately verify a harmless real terminal approval from phone. Provider delivery alone does not verify push notification or terminal dispatch.

Herdr approvals remain owned by Bridge; they are not inserted into OpenClaw's native `exec.approvals` store. Unsupported dialogs require local handling. New-agent creation, arbitrary raw keypads, implicit thread-to-pane routing and auto-approval are not exposed.

Messages may duplicate after ambiguous provider results. Terminal effects never replay. Herdr has no atomic compare-and-send-keys operation, so fingerprint/identity checks cannot eliminate a simultaneous local operator race.

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
