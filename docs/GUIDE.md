# Herdr Bridge user guide

Bridge runs as an OpenClaw Gateway service. Configure the existing OpenClaw Slack channel first; Bridge does not run a separate Slack Socket Mode client.

## Install

From the checkout, follow the [README setup](../README.md#setup). On macOS see [MACOS.md](MACOS.md). Install the Python runtime with `pip install -e .`; the `legacy-slack` extra and `requirements.txt` are only needed for the archived bot or the full regression suite.

Install the OpenClaw plugin with `openclaw plugins install --link ./openclaw`. If the CLI asks for confirmation of a trusted local path, follow its trust instructions. The plugin declares two optional read tools in `contracts.tools`. If upgrading an existing installation whose capability declarations changed, reload with:

```bash
openclaw plugins reload herdr-bridge --accept-capabilities
```

The new root Herdr manifest provides Gateway status/restart actions. Its purpose is not to launch a standalone bot; the Python worker is started by OpenClaw.

## Configure

Copy [example.patch.json](../openclaw/example.patch.json) to a private local file, fill in absolute paths, Slack member ID and account ID, then apply it with `openclaw config patch --file /path/to/your-patch.json`. Keep local configuration out of Git. Use `*.local.patch.json` for a gitignored filename.

| Setting | Meaning |
|---|---|
| `python` | Absolute path to the installed virtual environment's Python |
| `projectRoot` | Absolute checkout path containing `herdr_bridge/` |
| `socketPath` | Optional Herdr Unix socket or Windows named pipe; defaults to Herdr discovery |
| `herdrBin` | Optional absolute Herdr executable path if the Gateway PATH cannot find it |
| `ownerIds` | Slack member IDs allowed to issue human commands/click approvals |
| `route.channel` | `slack`, the currently supported route |
| `route.accountId` | Configured OpenClaw Slack account, usually `default` |
| `route.to` | DM recipient, for example `user:YOUR_SLACK_MEMBER_ID` |

OpenClaw's Slack ingress must also allow the owner. Configure `dmPolicy: "allowlist"` and `allowFrom: ["YOUR_SLACK_MEMBER_ID"]` in the Slack channel configuration. Bridge's owner check is additional to the host's authenticated sender policy. Slack credentials stay in OpenClaw.

For Windows, use the absolute `.venv/Scripts/python.exe` path and escape backslashes in JSON. The Herdr transport supports Windows, but this new OpenClaw integration has only been tested live on macOS.

For optional natural-language terminal inspection, allow `herdr_status` and `herdr_read` in the trusted OpenClaw agent's tool policy, for example using `tools.alsoAllow`. Preserve existing policy entries when editing it. The factory supplies these tools only to the host's current owner and revalidates the invocation. Neither tool writes to a terminal.

## Start and inspect

```bash
openclaw gateway restart
openclaw gateway status
openclaw plugins inspect herdr-bridge --runtime --json
openclaw channels status
```

Runtime inspection should show the `herdr` command, `herdr-bridge` service and the two read tools, without diagnostics. Metadata-only plugin inspection does not load the runtime code.

The Gateway supervises the Python worker and stores the SQLite request/outbox database in its service state directory. Do not start a second worker against the same state. Logs go through OpenClaw; callback logs include the request ID and option index without command contents.

## Use the OpenClaw bot DM

| Text command | Behavior |
|---|---|
| `/herdr status` | List agents; select the exact pane ID |
| `/herdr read w4:p2` | Read the visible terminal without scrolling a working TUI |
| `/herdr pending` | List pending request IDs and their zero-based options |
| `/herdr send w4:p2 <prompt>` | Send text to an idle/done agent |
| `/herdr answer <request-id> 0` | Answer option index 0 for that request |
| `/herdr check <request-id>` | Check a previous attempt; sends no keys |

`herdr` is an OpenClaw text command, not a newly registered native Slack slash command. If Slack's composer rejects an unregistered slash, use your OpenClaw installation's configured native command entry point to pass the `herdr` command. Creating another Slack app is unnecessary.

Top-level notifications contain one bold header (Herdr name, current model, cwd), an inline-code summary, terminal conversation and, for recognized approvals, native buttons. The summary is a bounded rule-based extraction, not an LLM-generated semantic summary. The visible terminal capture remains the evidence and may not include older off-screen history. Models are resolved from actual session metadata; unavailable or ambiguous matches are shown as unknown.

Approval buttons bind to one request ID and option index. The card first shows checking, then Bridge revalidates the terminal and sends once. Repeated clicks return the existing attempt; expired or superseded requests cannot send. Two consistent observations of state progression and the original approval closing or moving to a new recognized dialog produce `confirmed`. This does not verify command completion.

After five seconds without confirmation, the card reports `unconfirmed` or unknown acceptance and continues read-only background checks for up to two minutes, bounded by expiry. **检查状态** reads again without sending. **重新提交** appears only when fresh checks find the exact original blocked approval and unchanged state sequence; it explicitly sends the same option as one new attempt, with another live check immediately before sending. Previous input can arrive late, so retries carry that limitation. A failed progress edit leaves a reserved attempt that has not sent keys and can be resumed explicitly after checks. Unknown acceptance cannot expose retry until checking establishes eligibility.

Gateway restarts never automatically resend terminal input. While the process remains active, background results update the original card. After restart, a new recovery card carries the same request ID and applicable controls; the previous card may retain its earlier display. The durable record/logs retain each attempt, timing and observed evidence. Existing `dispatched` records describe a legacy send acknowledgement, not terminal acceptance.

Unknown dialogs, multi-select, typed inputs and file-backed plans need local handling. Inspecting an agent such as Pi does not imply remote approval support for that agent's dialogs.

## Migrating from herdr-slackbot

1. Keep the former implementation/configuration available for rollback. The pre-migration fork baseline is commit `c9f17c8`.
2. Stop the old standalone bot using its configuration directory:

   ```bash
   sh scripts/herdr-slackbot.sh stop --config-dir /path/to/old/plugin/config
   ```

   Windows installations use the old manifest's stop action or documented process control in the [archived guide](../legacy/docs/GUIDE.md).
3. Configure the existing OpenClaw bot and install/enable the Bridge plugin as above.
4. Restart Gateway, inspect runtime registration, and run the [live checklist](LIVE_TEST.md).

Old `.env` tokens, pairing codes, bot DMs and thread associations are not imported. Bridge uses OpenClaw authentication and a new request/outbox state store. The archived Home tab, new-agent modals and thread-reply routing are not exposed by the new plugin. The root manifest has no old startup hook; older scripts/imports remain for compatibility.

## Troubleshooting

| Symptom | Check |
|---|---|
| Worker cannot start | Gateway-accessible `python`/`projectRoot`, installed Python package, state lock |
| No agents / read failure | Running Herdr server, socket path and `herdrBin` |
| Commands rejected | OpenClaw ingress allowlist, owner ID, channel and account match |
| Cards arrive but buttons fail | Runtime interactive registration, sender authorization, callback logs |
| Approval rejected as stale | Read the current terminal; use the current pending request |
| No phone push | Slack/device notification policy, availability, background/lock-screen behavior |
| Duplicate card | Shared request ID returns the current attempt; only the separate retry button permits another submission |
| Approval still open | Use 检查状态; submit again only if the attempt-bound 重新提交 button appears |

Check delivery, callback, terminal dispatch and phone push independently. A successful provider receipt alone verifies none of the other three.
