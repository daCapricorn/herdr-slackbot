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
- Keep pending approvals and outbound messages across Gateway/worker restarts.

Messages show **Herdr name · current model · directory path**, followed by an inline-code highlighted summary and the captured terminal conversation. The name uses Herdr's agent name or tab label. The model comes from reported metadata or the actual Codex/Claude session; an ambiguous match shows `模型未知`. Summaries extract the pending command or latest assistant paragraph using rules, without an LLM call.

Bridge can list and read agents reported by Herdr, including Pi. Unrecognized dialogs, multi-select, typed inputs and file-backed plans need local handling. Agent creation, Home-tab controls and implicit Slack thread-to-pane routing remain features of the archived standalone bot.

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

Clicks and write commands require an authenticated configured owner on the configured channel/account. Optional `herdr_status` and `herdr_read` tools give OpenClaw's model read access; the model receives no approval or terminal-write tool.

## Reliability

Before sending keys, Bridge checks expiry, option membership, terminal/session identity, state sequence, blocked status and dialog fingerprint. It durably consumes the request before sending keys, and never automatically retries an uncertain terminal mutation. Replaced requests and stale queued approval cards are invalidated.

Message delivery is **at least once**: an ambiguous provider acknowledgement can produce duplicate cards. Copies share a request ID and can be answered at most once. Herdr does not provide an atomic compare-and-send-keys operation, so simultaneous local interaction remains a race. Phone push depends on the recipient's Slack and device notification settings; see the [live checklist](docs/LIVE_TEST.md).

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
