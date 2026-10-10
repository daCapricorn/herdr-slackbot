# OpenClaw Bridge

Decision source: [shared design conversation](https://chatgpt.com/share/e/6aca5d56-0d34-83ed-bb4a-9c63564f8784). API source of truth: installed OpenClaw 2026.9.9 SDK and Slack adapter.

## Ownership

- Herdr owns terminals, agent detection and native read/prompt/send-keys.
- Python worker owns reconciled status events, durable pending requests/outbox and one-shot dispatch. It opens no network listener and consumes no Slack events.
- OpenClaw plugin owns worker lifecycle, deterministic human commands, read tools, sender/account checks and portable message presentations.
- OpenClaw Slack adapter owns credentials, Socket Mode, inbound policy, native rendering, provider requests and interaction acknowledgement/edits.

Verified SDK APIs: `registerService`, `registerCommand`, `registerTool`, `registerInteractiveHandler`, `api.runtime.channel.outbound.loadAdapter` and `api.runtime.config.current()`. Delivery uses the channel's normalization, presentation rendering and `sendPayload`.

Slack 2026.9.9 only executes certain core approval command buttons. Bridge instead uses a portable callback action `herdr-bridge:<request-id>:<option>` and registers the `slack` / `herdr-bridge` namespace. OpenClaw supplies authenticated sender/account/auth context. Callback text is never authorization.

## Private protocol

Stdin/stdout carry one JSON object per line:

```json
{"id":"bridge-1","method":"read","params":{"pane":"w4:p2"}}
{"id":"bridge-1","result":{"agent":{"pane_id":"w4:p2"},"text":"..."}}
```

Methods: `status`, `read`, `send`, `pending`, `answer`, `events`, `ack`. Errors have `id` and `{code,message}`. Logs go to stderr. Lost responses never automatically resend terminal writes.

Events contain stable event IDs, agent identity, timestamp and terminal evidence. `approval.requested` adds request ID, expiry, state sequence, fingerprint and exact options. Unrecognized dialogs become `agent.blocked` without remote options. The tested event reconciler handles fast transitions and reconnection; a scan recovers agents blocked before startup and dialogs changing while still blocked.

## Rollout and scope

1. Save the old implementation (`c9f17c8`).
2. Develop on `feature/openclaw-bridge`, branched from `develop`.
3. Check Python safety tests, JavaScript plugin tests, actual plugin discovery, config validation and live Herdr reads.
4. Configure owner/route, stop the old runtime, restart Gateway.
5. Verify a harmless real approval from phone. Provider delivery alone does not verify push notification or human approval.

Herdr approvals remain owned by Bridge; they are not inserted into OpenClaw's native `exec.approvals` store. Unsupported dialogs require local handling. New-agent creation, arbitrary raw keypads, implicit thread-to-pane routing and auto-approval are not exposed.

Messages may duplicate after ambiguous provider results. Terminal effects never replay. Herdr has no atomic compare-and-send-keys operation, so fingerprint/identity checks cannot eliminate a simultaneous local operator race.

Legacy tests read `legacy/herdr-plugin.toml`. The root manifest registers `herdr-bridge` and has no old runtime startup hook.
