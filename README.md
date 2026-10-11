# Herdr Bridge

**Local Herdr terminal agents, connected to OpenClaw for notifications and human approvals.**

[한국어](README.ko.md) · [User guide](docs/GUIDE.md) · [Architecture](docs/OPENCLAW-BRIDGE.md) · [Migration](docs/GUIDE.md#migrating-from-herdr-slackbot)

This fork has evolved from a standalone Slack bot into `herdr-bridge`. Keep coding in Herdr, receive agent updates on your phone, inspect a specific terminal, and answer a recognized approval through the existing OpenClaw bot. OpenClaw owns messaging credentials, delivery and authenticated callbacks; Bridge owns the exact connection to the local agent.

```text
Slack ↔ OpenClaw Gateway / Slack adapter ↔ Herdr Bridge plugin
                                              ↕ private NDJSON stdio
                                         Python worker ↔ Herdr ↔ agents
```

The current integration supports **Slack DMs through OpenClaw**. The core and message presentation are independent of Slack, but other channel routes are not implemented. The checkout and GitHub repository can retain the name `herdr-slackbot`; the Python package and OpenClaw plugin are named `herdr-bridge`.

## What it does

- Notify the configured owner when an agent finishes or needs attention, as top-level DM messages.
- List agents and read an exact pane's visible terminal conversation, including command output.
- Send a prompt to an idle or completed agent.
- Show recognized Codex command approvals and Claude single-choice dialogs with native buttons, then revalidate the exact request before sending keys.
- Forward Codex CLI async questions from Queued follow-up inputs, including while the agent is working. Answer a suggested choice in Slack or use `/openclaw /herdr reply` for free text.
- Keep pending approvals/questions and outbound messages across Gateway/worker restarts.
- Optionally stream new Codex conversation messages into each pane's private Slack channel; tool activity is summarized without tool output.

Messages show **Herdr name · current model · directory path**, followed by an inline-code highlighted summary and the captured terminal conversation. The name uses Herdr's agent name or tab label. The model comes from reported metadata or the actual Codex/Claude session; an ambiguous match shows `模型未知`. Summaries extract the pending command or latest assistant paragraph using rules, without an LLM call.

Bridge can list and read agents reported by Herdr, including Pi. Unrecognized dialogs, multi-select, typed approval inputs and file-backed plans need local handling. Agent creation, Home-tab controls and implicit Slack thread-to-pane routing remain features of the archived standalone bot.

## Setup

Requires Python **3.11+**, Herdr **0.8.2+**, Node.js **22+**, and OpenClaw. The plugin uses the **OpenClaw 2026.9.9 SDK**; that version and Herdr 0.9.3 have been checked on macOS. Windows transport support is retained, but the new Gateway flow has not been verified live on Windows.

With Herdr running and OpenClaw's Slack channel configured:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -e .
openclaw plugins install --link ./openclaw
```

Configure and enable `plugins.entries.herdr-bridge` using the [patch template](openclaw/example.patch.json):

```json
{
  "python": "/absolute/path/herdr-bridge/.venv/bin/python",
  "projectRoot": "/absolute/path/herdr-bridge",
  "socketPath": "/Users/you/.config/herdr/herdr.sock",
  "ownerIds": ["YOUR_SLACK_MEMBER_ID"],
  "route": {
    "channel": "slack",
    "accountId": "default",
    "to": "user:YOUR_SLACK_MEMBER_ID"
  }
}
```

Use absolute paths visible to the Gateway service. Set OpenClaw's Slack `dmPolicy` to `allowlist` and `allowFrom` to the owner ID. Set `herdrBin` if Herdr is outside the Gateway's PATH. Bridge needs **no Slack token and no second Slack app**.

```bash
openclaw config patch --file /path/to/your-patch.json
openclaw gateway restart
openclaw gateway status
```

Enable `channels.slack.slashCommand` with `{"enabled":true,"name":"openclaw"}`, then send `/openclaw /herdr status` in the OpenClaw bot DM. The Slack App must already have its `/openclaw` slash command configured; plugin registration does not create a native `/herdr` Slack command. See the [guide](docs/GUIDE.md) for command routing, optional read tools, Windows paths and migration from the old bot.

## Commands

| Command | Result |
|---|---|
| `/openclaw /herdr status` | Agents and exact pane IDs |
| `/openclaw /herdr read w4:p2` | That pane's visible terminal |
| `/openclaw /herdr sync w4:p2` | Conversation sync status and failure reason |
| `/openclaw /herdr bind w4:p2 <thread-id>` | Associate an exact Codex conversation; no terminal input |
| `/openclaw /herdr pending` | Request IDs and zero-based options |
| `/openclaw /herdr send w4:p2 inspect the project` | Prompt an idle/done agent |
| `/openclaw /herdr answer <request-id> 0` | Answer one exact request after live checks |
| `/openclaw /herdr reply <request-id> <text>` | Answer an exact Codex async question with free text |
| `/openclaw /herdr check <request-id>` | Inspect a previous attempt without sending keys |

Clicks and write commands require an authenticated configured owner on the configured channel/account. Optional `herdr_status` and `herdr_read` tools give OpenClaw's model read access; the model receives no approval or terminal-write tool.

## Codex queued questions

The async question adapter has been checked against **Codex CLI 0.162.1**. It reads structured local session records from `CODEX_HOME` (default `~/.codex`); the Gateway must run as the same user with access to those records. Without a reported session ID, matching requires one live Codex pane and one unarchived non-subagent session in that directory. Ambiguous sessions are not guessed. Other agents retain their existing dialog handling.

Each question card contains its full title and suggestions, alongside the terminal capture. Discovery never opens the local question menu or sends input. A click uses an exact expanded menu, or the known empty main composer to submit Codex’s native question reply. When Herdr misclassifies that collapsed summary as blocked, the guarded path uses one encoded pane submission instead of `agent.prompt`. Free text requires the empty main composer. Drafts, other approvals and unknown layouts prevent submission. Confirmation requires the exact question ID and answer in a durable Codex user-message record; a state change alone is insufficient. Unanswered questions remain valid while their exact native question is pending; ending the turn or replacing its identity invalidates the card. This supports async questions, not ordinary queued message editing.

Approval cards, unsupported blocked-dialog notices and Codex async question cards wait **10 seconds from first discovery**. Bridge retains the pending request immediately, but releases its notification only if the same request is still unresolved. Local answers, turn completion, expiry or replacement cancel the queued card. A replacement request starts its own delay; duplicate scans and Gateway restarts preserve the existing deadline. Live-read failures defer delivery until evidence is available. Completion notices and answer-result updates have no added delay. The five-second delivery poll can make a card arrive a few seconds after the deadline; this is a minimum grace period, not an exact phone push timer.

## Pane channels

With `paneChannels.enabled: true`, Bridge creates one private Slack channel for every Herdr pane, including ordinary shell panes, and invites the configured owners. Pane events and approval results use that channel instead of the aggregate DM. Names follow the pane/tab display name; renaming updates the channel while preserving its ID. A terminal moved across workspaces retains its channel under the new pane ID. Closed panes retain their channel as history; `/openclaw /herdr archive` in that channel explicitly archives it. For Codex panes, new user inputs and completed assistant answers are also streamed into the channel; tool calls appear as short summaries without tool output. On first attachment, a working or blocked pane starts from the latest active turn; a settled pane starts at the current end of its rollout. Older session history is not imported. Claude and other agents keep their current notification-only behavior.

OpenClaw still sends messages and handles authenticated interactions. Its 2026.9.9 SDK does not expose Slack channel creation/rename/invitation, so a narrow management adapter calls the Slack Conversations API using the current OpenClaw bot credential in memory. No separate token is configured or persisted by Bridge. Add `groups:write` and `groups:write.invites` alongside the existing `groups:read` scope, then reinstall the Slack App. The selected account must use bot identity and a resolved bot token. Without the opt-in setting, existing DM routing continues.

Channel commands infer the bound pane: `/openclaw /herdr read`, `/openclaw /herdr pending` and `/openclaw /herdr send <prompt>`. Explicit cross-pane targets and requests from a different channel are rejected. Pending-request messages mention the owners; completion notices do not; the 10-second attention delay remains unchanged. Channel notification and mobile settings still govern actual push delivery.

## Reliability

Codex conversation sync persists per-terminal/thread cursors and pending messages across Gateway restarts. Read/enqueue failures roll back the cursor, seen IDs and outbox together. A failed conversation send holds later messages from that pane to preserve order while other panes continue. Validated live Herdr session IDs replace old manual bindings. Optional Codex lifecycle hooks report exact `/new` and resume identities, including when `transcript_path` is null; review new hooks in the **Codex CLI** `/hooks` menu before they can run. Automatic association requires `codex --no-daemon` (or `bash scripts/codex-herdr.sh`), because the current shared daemon does not provide reliable pane context to its hooks. Shared-daemon sessions retain explicit binding support. Completion-event deduplication prevents resume/startup transitions from re-notifying an old answer. See [session-switching setup](docs/GUIDE.md#codex-session-switching-on-macos). Claude conversation sync remains outside this adapter.

Before sending keys, Bridge checks option membership, terminal/session identity, state sequence, blocked status and dialog fingerprint. Slack clicks first reserve an attempt and show progress, then recheck the live dialog before sending once. A send acknowledgement alone cannot produce a success card: unchanged approvals show `unconfirmed`, while stable evidence of progression shows `confirmed`. Confirmation does not verify command completion. A five-second initial check is followed by read-only background checks for up to two minutes. Replaced requests and stale queued approval cards are invalidated.

Message delivery is **at least once**: an ambiguous provider acknowledgement can produce duplicate cards. Copies share a request ID; repeated clicks return the current result without resending. An unchanged live approval can expose a separate **重新提交** button bound to that attempt, which explicitly permits one new submission of the same option after fresh checks. Unknown acceptance cannot be retried until checks establish the original unchanged dialog. No keys are automatically replayed. Herdr cannot atomically compare the dialog and send keys, and earlier input can arrive late, so even explicit retries cannot guarantee exactly-once terminal handling. Phone push depends on Slack/device settings; see the [live checklist](docs/LIVE_TEST.md).

## Development and project history

Git Flow uses `develop` for integration, `feature/*` for changes, `main` for releases, and `release/*` / `hotfix/*` when needed. Version `0.2.0` denotes the new Bridge implementation; a version in package metadata does not imply a tagged release.

```bash
.venv/bin/python -m pip install -e '.[dev,legacy-slack]'
.venv/bin/python -m pytest -q
cd openclaw
node --test test/*.test.js
```

See the [documentation index](docs/README.md) and [changelog](CHANGELOG.md). The terminal client, event reconciler and dialog parser live in `herdr_bridge/`. `herdr_slackbot/` retains compatibility imports and the old runtime for regression/migration; the new runtime does not load its Slack app or layout engine. Old installation guides are archived in [legacy](legacy/README.md).

Based on the original herdr-slackbot by paper3300. [MIT license](LICENSE).
