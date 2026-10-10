# herdr-bridge

Connect local Herdr agents to OpenClaw. Herdr owns terminals, Bridge owns event correlation and exact terminal responses, and OpenClaw owns Slack authentication, rendering, and delivery.

```text
Slack ↔ OpenClaw Gateway / Slack plugin ↔ Herdr Bridge plugin
                                         ↕ private NDJSON stdio
                                      Python worker ↔ Herdr ↔ agents
```

Requires **Python 3.11+**, **Herdr 0.8.2+**, and **OpenClaw 2026.9.9+**. Checked against OpenClaw 2026.9.9 on macOS. Configure Slack credentials in OpenClaw; Bridge needs no Slack token.

## Setup

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
openclaw plugins install --link ./openclaw
```

Configure `plugins.entries.herdr-bridge.config` with absolute paths:

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

An editable patch template is in `openclaw/example.patch.json`. After filling in paths and owner ID, apply it with `openclaw config patch --file <your-patch.json>`. Set OpenClaw Slack `dmPolicy` to `allowlist` and `allowFrom` to your owner ID so authenticated DM commands can enter the plugin.

`socketPath` also accepts a Windows named pipe. Set `herdrBin` if Herdr is outside the Gateway PATH. On Windows use `.venv/Scripts/python.exe`. The checkout name may remain `herdr-slackbot`.

Stop the previous standalone bot, then restart Gateway:

```bash
sh scripts/herdr-slackbot.sh stop --config-dir /path/to/old/plugin/config
openclaw gateway restart
openclaw gateway status
```

Send `/herdr status` as a **text command in the OpenClaw bot DM**. If Slack's composer treats an unregistered slash as a native Slack command, use OpenClaw's configured native command to invoke it. Do not create a second Slack app for Bridge.

## Usage

| Command | Result |
|---|---|
| `/herdr status` | All running agents with exact pane IDs |
| `/herdr read w4:p2` | Visible terminal without scrolling a working TUI |
| `/herdr pending` | Pending request IDs and zero-based options |
| `/herdr send w4:p2 inspect the project` | Prompt an idle/done agent |
| `/herdr answer <request-id> 0` | Answer one exact request after live revalidation |

Recognized Codex command approvals and Claude single-choice dialogs become portable `MessagePresentation` buttons. Clicks use OpenClaw's authenticated plugin callback path. Notifications go to the configured DM as top-level messages.

Messages show **Herdr name · current model · directory path** once, followed by an inline-code highlighted content summary and the full terminal conversation. The display name uses Herdr's named agent or its tab label (for example `codex-slackbot`). Models come from reported metadata or the actual Codex/Claude session; ambiguous or unavailable matches show `模型未知`, never the global default. Codex workspace matching requires one live pane and one non-subagent session in that directory.

Bridge lists/reads any kind reported by Herdr, including Pi. Unrecognized dialogs, multi-select, typed inputs and file-backed plans require local handling; remote keys are disabled for these.

Optional `herdr_status` and `herdr_read` tools let OpenClaw inspect terminals in natural language. Enable them only in a trusted agent's tool policy. Models receive no approval or terminal-write tool. Human writes require an authenticated configured owner, channel and account.

## Reliability

Pending requests and an outbound queue live in Gateway service state. Before sending keys, Bridge checks expiry, option membership, terminal/session identity, state sequence, blocked status and dialog fingerprint. It durably consumes a request before pressing keys. Crashes and lost replies never replay a terminal mutation. One state lock prevents overlapping workers.

Transcript redraws do not create new command approvals. Replaced requests are invalidated; stale or expired queued cards are dropped. The no-op connection check updates its original card so the callback result stays visible.

Message delivery is **at least once**: failed sends stay queued; a lost provider acknowledgement may produce duplicate cards. Copies share the same request ID and can be answered at most once. Gateway/worker restarts preserve state.

## Development

Git Flow: `main` for releases, `develop` for integration, `feature/*` from `develop`, and `release/*`/`hotfix/*` when needed. Baseline: `c9f17c8`; migration: `feature/openclaw-bridge`.

```bash
.venv/bin/python -m pytest -q
cd openclaw
node --test test/*.test.js
```

See [architecture and migration](docs/OPENCLAW-BRIDGE.md). The tested terminal client, event reconciler and dialog parser now live in `herdr_bridge/`. Compatibility imports preserve the former implementation and regression suite in `herdr_slackbot/`; its manifest/README are archived in [legacy](legacy/README.md). Slack dependencies are the optional `legacy-slack` extra. The new runtime never loads the legacy Slack app, transport or layout engine.
