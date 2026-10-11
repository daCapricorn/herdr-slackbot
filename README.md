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
- Forward Codex CLI async questions from Queued follow-up inputs, including while the agent is working. Answer a suggested choice in Slack or use `/herdr reply` for free text.
- Keep pending approvals/questions and outbound messages across Gateway/worker restarts.

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

Send `/herdr status` as a text command in the OpenClaw bot DM. See the [guide](docs/GUIDE.md) for command routing, optional read tools, Windows paths and migration from the old bot.

## Commands

| Command | Result |
|---|---|
| `/herdr status` | Agents and exact pane IDs |
| `/herdr read w4:p2` | That pane's visible terminal |
| `/herdr pending` | Request IDs and zero-based options |
| `/herdr send w4:p2 inspect the project` | Prompt an idle/done agent |
| `/herdr answer <request-id> 0` | Answer one exact request after live checks |
| `/herdr reply <request-id> <text>` | Answer an exact Codex async question with free text |
| `/herdr check <request-id>` | Inspect a previous attempt without sending keys |

Clicks and write commands require an authenticated configured owner on the configured channel/account. Optional `herdr_status` and `herdr_read` tools give OpenClaw's model read access; the model receives no approval or terminal-write tool.

## Codex queued questions

The async question adapter has been checked against **Codex CLI 0.162.1**. It reads structured local session records from `CODEX_HOME` (default `~/.codex`); the Gateway must run as the same user with access to those records. Without a reported session ID, matching requires one live Codex pane and one unarchived non-subagent session in that directory. Ambiguous sessions are not guessed. Other agents retain their existing dialog handling.

Each question card contains its full title and suggestions, alongside the terminal capture. Discovery never opens the local question menu or sends input. A click uses an exact expanded menu, or the known empty main composer to submit Codex’s native question reply. When Herdr misclassifies that collapsed summary as blocked, the guarded path uses one encoded pane submission instead of `agent.prompt`. Free text requires the empty main composer. Drafts, other approvals and unknown layouts prevent submission. Confirmation requires the exact question ID and answer in a durable Codex user-message record; a state change alone is insufficient. Unanswered questions expire when their originating turn ends, or after the Bridge request’s 15-minute lifetime. This supports async questions, not ordinary queued message editing.

## Reliability

Before sending keys, Bridge checks expiry, option membership, terminal/session identity, state sequence, blocked status and dialog fingerprint. Slack clicks first reserve an attempt and show progress, then recheck the live dialog before sending once. A send acknowledgement alone cannot produce a success card: unchanged approvals show `unconfirmed`, while stable evidence of progression shows `confirmed`. Confirmation does not verify command completion. A five-second initial check is followed by read-only background checks for up to two minutes, bounded by request expiry. Replaced requests and stale queued approval cards are invalidated.

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
