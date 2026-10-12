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

## Enable one private channel per pane

Add `groups:write` and `groups:write.invites` to the bot scopes, retain `groups:read`, and reinstall the Slack App. Enable with:

```bash
openclaw config set plugins.entries.herdr-bridge.config.paneChannels '{"enabled":true,"prefix":"herdr"}'
openclaw gateway restart
```

Every pane, including shells, gets a private channel. Configured owners are invited before it becomes a delivery destination. Names follow pane/tab labels and retain an identity suffix. Renames and terminal moves preserve channel IDs. `/openclaw /herdr channels` in the DM shows bindings. `/openclaw /herdr read`, `/openclaw /herdr pending` and `/openclaw /herdr send <prompt>` in a pane channel infer that pane; explicit cross-pane targets are rejected. Questions still use their exact request ID with `/openclaw /herdr reply`. Closing a pane retains history; `/openclaw /herdr archive` in its channel explicitly archives it. Ordinary channel prose is not automatically submitted to the terminal. For Codex and Claude, new user inputs and completed answers appear in the pane channel, with tool activity reduced to short summaries and no tool output. The sync starts from the latest active turn on first attachment, or from the current end of the rollout when the pane is settled; it does not import old conversation history. Other agents remain notification-only.

OpenClaw sends all messages and handles interactions. A narrow Slack management adapter uses the current resolved OpenClaw bot token in memory for creation, invitation, naming and topic metadata. There is no separate Bridge credential. The selected account must use bot identity. Owner mentions accompany approval, blocked-dialog and question notifications; completion messages contain no owner mention. Actual mobile push remains subject to your Slack settings. The 10-second grace period applies to pending attention events, not completion or answer-result updates.

## Use the OpenClaw bot DM or pane channel

| Text command | Behavior |
|---|---|
| `/openclaw /herdr status` | List agents; select the exact pane ID |
| `/openclaw /herdr read w4:p2` | Read the visible terminal without scrolling a working TUI |
| `/openclaw /herdr pending` | List pending request IDs and their zero-based options |
| `/openclaw /herdr send w4:p2 <prompt>` | Send text to an idle/done agent |
| `/openclaw /herdr answer <request-id> 0` | Answer option index 0 for that request |
| `/openclaw /herdr reply <request-id> <text>` | Send a free-text answer to one Codex async question |
| `/openclaw /herdr check <request-id>` | Check a previous attempt; sends no keys |

`herdr` is an internal OpenClaw command. Slack does not automatically register `/herdr` when the plugin loads. Use the existing OpenClaw Slack App's `/openclaw` command to forward it:

```bash
openclaw config set channels.slack.slashCommand '{"enabled":true,"name":"openclaw"}'
```

Then use `/openclaw /herdr status` or `/openclaw /herdr sync` in the pane channel. The Slack App must define `/openclaw` in its Slash Commands and retain the `commands` scope. If absent, add it to that same App (Socket Mode does not require a public Request URL), rather than creating another App. A custom configured entry name replaces `/openclaw` in these examples. Native `/herdr` is optional and requires separate Slack App registration and native-command configuration.

Top-level notifications contain one bold header (Herdr name, current model, cwd), an inline-code summary, terminal conversation and, for recognized approvals, native buttons. The summary is a bounded rule-based extraction, not an LLM-generated semantic summary. The visible terminal capture remains the evidence and may not include older off-screen history. Models are resolved from actual session metadata; unavailable or ambiguous matches are shown as unknown.

Approval cards, unsupported blocked-dialog notices and Codex async question cards wait **10 seconds from first discovery**. Bridge retains the pending request immediately, but releases its notification only if the same request is still unresolved. Local answers, turn completion, expiry or replacement cancel the queued card. A replacement request starts its own delay; duplicate scans and Gateway restarts preserve the existing deadline. Live-read failures defer delivery until evidence is available. Completion notices and answer-result updates have no added delay. The five-second delivery poll can make a card arrive a few seconds after the deadline; this is a minimum grace period, not an exact phone push timer.

New approval and question cards retain their original header, summary and captured conversation while checking or showing the result; only the approval controls are replaced. This display context is durable and also appears in restart recovery cards. Older requests without saved context keep the result-only fallback.

Approval buttons bind to one request ID and option index. There is no fixed wall-clock expiry. The card first shows checking, then Bridge revalidates the terminal and sends once. Repeated clicks return the existing attempt; superseded requests cannot send. Two consistent observations of state progression and the original approval closing or moving to a new recognized dialog produce `confirmed`. This does not verify command completion.

After five seconds without confirmation, the card reports `unconfirmed` or unknown acceptance and continues read-only background checks for up to two minutes. **检查状态** reads again without sending. **重新提交** appears only when fresh checks find the exact original blocked approval and unchanged state sequence; it explicitly sends the same option as one new attempt, with another live check immediately before sending. Previous input can arrive late, so retries carry that limitation. A failed progress edit leaves a reserved attempt that has not sent keys and can be resumed explicitly after checks. Unknown acceptance cannot expose retry until checking establishes eligibility.

Gateway restarts never automatically resend terminal input. While the process remains active, background results update the original card. After restart, a new recovery card carries the same request ID and applicable controls; the previous card may retain its earlier display. The durable record/logs retain each attempt, timing and observed evidence. Existing `dispatched` records describe a legacy send acknowledgement, not terminal acceptance.

Unknown dialogs, multi-select, typed approval inputs and file-backed plans need local handling. Inspecting an agent such as Pi does not imply remote approval support for that agent's dialogs.

## Codex Queued follow-up inputs

Codex CLI async questions can remain queued while the agent works, without a Herdr `blocked` transition. Bridge scans structured session evidence every five seconds and forwards each question independently to its pane channel, or the configured DM when pane channels are disabled. The card keeps the full question, full suggestion labels and terminal capture. Click a suggestion, or send `/openclaw /herdr reply <request-id> <your answer>` for free text. Neither discovery nor delivery opens the local menu or answers automatically.

The adapter has been checked against Codex CLI **0.162.1**. The Gateway worker needs read access to the same user's `CODEX_HOME` (default `~/.codex`), including its state database and rollout files. An exact Herdr session ID takes precedence. Otherwise there must be only one live Codex pane and one unarchived non-subagent Codex session for that directory; session ambiguity disables discovery/dispatch.

Leave the main composer empty when answering remotely. A collapsed question uses Codex's native question-reply envelope through Herdr's prompt API (or one encoded pane submission when Herdr labels the verified collapsed UI blocked); this special path can answer while Codex works, unlike ordinary `/openclaw /herdr send`. If the exact question menu is already expanded, a single-digit suggestion may be selected there. Free text, larger option indices, unknown layouts, local drafts or a different open approval require returning to the empty main composer first. No automatic menu navigation is performed.

The card first shows checking. `confirmed` means the matching question ID and exact answer were recorded in the Codex session, even if Herdr remains `working`. A question disappearing, a turn ending or a different local answer cannot confirm your Slack selection. Repeated clicks and restart recovery never replay input. Read-only checks and eligible explicit retries reuse the approval recovery infrastructure. A free-text command has no callback editor, so background results arrive in a new status message. Old question cards may remain visible but cannot answer an expired request.

Unanswered questions remain available while their exact native question is pending. The originating turn ending, the question disappearing or its identity/options changing invalidates the old card. Locally skipping a question has no durable answer record; the visible question count/menu prevents writing to mismatched input. Herdr cannot atomically verify and submit against simultaneous local typing, so keep the composer untouched during a remote answer. Ordinary queued message editing and non-async question tools are outside this adapter's scope.

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
| Queued Codex question missing | Same-user Codex state access, exact/unambiguous session match, active turn |
| Question answer rejected | Empty main composer or exact visible question menu; active question/expiry |
| No phone push | Slack/device notification policy, availability, background/lock-screen behavior |
| Duplicate card | Shared request ID returns the current attempt; only the separate retry button permits another submission |
| Approval still open | Use 检查状态; submit again only if the attempt-bound 重新提交 button appears |

Check delivery, callback, terminal dispatch and phone push independently. A successful provider receipt alone verifies none of the other three.

### Codex sync session resolution

`/openclaw /herdr sync` in a pane channel shows `syncing`, `pending`, or `unavailable`, with the reason and resolved thread ID. In the DM, use `/openclaw /herdr sync <pane>`.

When Herdr omits `agent_session`, automatic discovery requires exactly one Codex pane in the directory and exactly one non-archived, non-subagent Codex thread there. The rollout's `session_meta` must match its database ID and cwd. Neither modification time nor the newest thread is used to guess a session.

For ambiguous directories, use `/openclaw /herdr bind <thread-id>` in the pane channel, or `/openclaw /herdr bind <pane> <thread-id>` in the DM. Obtain the exact ID from the intended Codex conversation. Binding is a Bridge-only association; it does not send terminal input or modify Codex state. It persists across Gateway restart, follows terminal movement, and does not transfer to a recreated terminal. A validated live Herdr session ID supersedes an old saved binding automatically. Manual binding to a different live Herdr session is rejected. Without a native identity, a changed directory stops sync and a conversation switch still requires an explicit bind.

These directory and binding rules apply only while the Codex title export is off. With the export enabled, the title is the only identity source and its failure is never replaced by them (see [Codex](#codex)).

### Agent plugins and session association

Codex and Claude have independent plugins/readers behind the same Bridge contract. Plugins report the exact agent/session through `pane.report_agent_session`; Bridge owns durable synchronization, terminal validation, channel routing, approvals and delivery through OpenClaw. Plugins never send Slack messages or terminal input.

Build local packages against this checkout's installed virtual environment:

```bash
.venv/bin/python scripts/build-agent-plugins.py
codex plugin marketplace add "$PWD/.agent-plugins"
codex plugin add herdr-bridge-codex@herdr-bridge-local
claude plugin marketplace add "$PWD/.agent-plugins"
claude plugin install herdr-bridge-claude@herdr-bridge-local --scope user
```

The generated `.agent-plugins` directory is ignored by Git and includes an absolute checkout/Python locator. Keep the virtual-environment path, including its symlink name: resolving it to the base interpreter loses the installed environment. Rebuild and update/reinstall the plugins after moving the checkout or virtual environment. This is a local development installation, not a portable published package.

Review and trust the two Codex plugin handlers in its CLI `/hooks` menu: `SessionStart` and `UserPromptSubmit`. Installing/enabling the plugin does not grant hook trust. Claude loads its installed hooks at startup or `/reload-plugins`. Private plugin evidence under each agent's `herdr-bridge/hooks/<session-id>-plugin.json` records event, session, sequence, report outcome and error type; it excludes prompts and credentials.

After trusting the Codex plugin, retire only the earlier companion handlers to avoid duplicate calls:

```bash
.venv/bin/python scripts/install-codex-session-hook.py --remove
```

This preserves the managed Herdr hook, other handlers and Codex trust configuration, and saves a one-time migration backup. The old installer remains available for existing deployments; new installations should use agent plugins.

#### Codex

Normal `codex`, `codex resume` and `bash scripts/codex-herdr.sh` keep the default shared-daemon runtime. In that runtime, with Codex CLI 0.162.1, command hooks run in the daemon with its original pane environment and receive no TUI/client ID. A `resume <id>` argv also goes stale after `/new`. Neither identifies the thread a pane is showing, and the plugin refuses to invent a mapping from them.

Codex's native terminal-title export does identify it. Each TUI client renders the `session-id` title item from its current thread (the full ID, or its first 29 characters followed by `...`). `/new` clears it, and resuming or selecting another thread updates it. Enable it once for regular `codex`:

```bash
.venv/bin/python scripts/configure-codex-identity.py        # --home <dir> for another CODEX_HOME
```

The helper:

- Puts `session-id` first in `[tui].terminal_title` and keeps your other title items in order (when unset, Codex's default non-activity items `thread-name` and `project-name`).
- Removes `activity`/`spinner`/`status`: they can prefix the title, and `activity`/`spinner` anywhere add an "Action Required" prefix while Codex is blocked.
- Leaves all other config bytes, hooks, trust, auth, model and permission settings unchanged.
- Keeps a one-time `config.toml.herdr-bridge-backup` (mode 0600) and replaces the file atomically.
- Refuses malformed or unsupported layouts and edits made concurrently by something else.

**Restart existing Codex clients after changing title settings; running clients keep their startup settings.** A per-launch override such as `codex -c 'tui.terminal_title=["session-id","thread","current-dir"]' resume <id>` also works within the supported config bounds below. Direct foreground `-c`/`--config` overrides of `tui.terminal_title` take precedence over `$CODEX_HOME/config.toml` in command-line order.

Bridge supports this user config and direct CLI title override, with a readable matching foreground Codex process. It refuses profile selection (`--profile`/`-p`, or config profile selectors), top-level CLI `tui=...` overrides, and ancestor `.codex/config.toml` files that set `tui.terminal_title`. The ancestor scan excludes `$CODEX_HOME` and conservatively checks every parent rather than reproducing Codex's project trust/root discovery. Unrelated ancestor `tui` settings are allowed. Invalid or unreadable user, ancestor or system configs, system title/profile settings, `/etc/codex/managed_config.toml`, forced macOS Codex preferences, and an inability to check those preferences make identity unavailable. These checks read config and preference metadata; they do not modify either. Package defaults and enterprise/cloud title layers are outside this bounded adapter's support.

While the export is enabled (the first effective title item is `session-id`/`thread-id`), Bridge reads each Codex pane's title on every scan:

- **Accepts:** only an exact ID or 29-character prefix as the first ` | `-separated item, or as the whole title when it is the only item. While Codex generates a thread name it appends a space and one Braille spinner frame (`⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏`) to that item; only that suffix is tolerated. Leading text, other items and trailing IDs are never searched.
- **Resolves:** exactly one active root thread in the pane's directory, whose full ID must be a well-formed thread ID. Another thread sharing the prefix, in any directory, makes the title ambiguous. The rollout header is checked too.
- **Re-checks:** the pane, terminal, directory, parsed session prefix and foreground Codex process afterwards. Spinner frames and thread-name changes are cosmetic and do not invalidate an unchanged prefix.
- **Keeps the result** for that scan only. It is never saved as a binding.

If config cannot be established within these bounds, or the enabled title has no ID (for example, after `/new` until the new thread exists), is invalid, ambiguous or changes its session prefix during the read, `/openclaw /herdr sync` reports the pane `unavailable` with the reason. Conversation sync, model display and question cards then stop. Bridge does not fall back to Herdr's earlier native session, a saved binding or a directory match. Pane and channel names still come from the pane/tab label, never from the title. This path has deterministic test coverage; live verification is pending.

The export also lets the plugin's `SessionStart`/`UserPromptSubmit` hooks register a shared-daemon pane. Bridge scans every 5 seconds and an idle pane's first scan starts at the end of the rollout, so without a hook a quick first prompt after `/new` or a resume could finish before that scan and be skipped. The hook selects the one Codex pane in the payload's directory whose title currently resolves, as above, to the payload's exact root thread ID. After validating the rollout and calculating its start offset, it rechecks the pane's terminal, directory, foreground process and title immediately before saving the offset and reporting the session to Herdr. Two panes showing the same thread, a title that has not yet changed after `/new`, a child thread or a nested agent's mismatched `CODEX_THREAD_ID` register nothing; the next prompt's hook or the scan then applies. The inherited `HERDR_PANE_ID`, Herdr's native session, the resume argv and the directory are never used to pick the pane, and the hook never saves a binding. Existing Codex clients must be restarted once after enabling the export.

When a matching foreground process and supported config establish that export is off, the previous native/manual contract applies. After `/new` or switching sessions, use `/openclaw /herdr bind <thread-id>` in that pane's channel. Unknown or unsupported config never permits this fallback.

An optional `codex --no-daemon` runtime allows automatic hook registration by proving that a foreground process in exactly one pane is the hook's ancestor; that proof takes precedence over the title. Exact root database/rollout identity, terminal and process are revalidated; null `transcript_path` is accepted. Nested agents and stale overlapping hooks cannot replace the association. This compatibility path is not a Bridge-wide startup requirement.

#### Claude

On macOS Claude Code 2.1.287 exposes `sessions/<foreground-pid>.json` under `CLAUDE_CONFIG_DIR` or `~/.claude`. The adapter checks PID, process creation time, interactive kind, cwd, exact session ID and the current Herdr foreground process/terminal. It never selects the newest file or guesses from a directory. Plugin hooks additionally prove Claude is their foreground ancestor and recheck the registry immediately before reporting. Sidechain/nested sessions cannot overwrite the root association. If the registry is unavailable, an exact Herdr session or explicit binding is required.

Claude transcripts must contain a matching root `sessionId` and `cwd`. Stable transcript UUIDs, API message IDs and tool IDs drive deduplication. Assistant text blocks remain staged in SQLite across scans and restarts until `turn_duration` or the next genuine user prompt commits a terminal answer. Thinking, tool results, metadata and subagent rows are excluded; tool calls use short summaries with no output blobs. A partial trailing line is retried without consuming it. An idle screen alone does not commit a split answer.

#### Common recovery

A private terminal/session offset record captures the start of streaming; new Claude sessions may be registered before their JSONL exists. Existing cursors take precedence, so switching back continues the same stream. Cursor, seen IDs, staging and outbox writes commit together. Periodic checks recover a final answer written after the terminal's idle transition and claim its completion notification once. Directory/terminal replacement and invalid identity fail closed.

Provider failures leave messages queued and preserve order within each pane. Gateway restart preserves pending messages, bindings and acknowledged cursors. Delivery remains at least once: acceptance immediately before a lost provider acknowledgement can still duplicate a retry. Existing 10-second attention delay and terminal-confirmation rules apply to both supported agents. Claude multiselect/plan/free-text dialogs retain their existing capability limits; this change does not claim equivalence to Codex async-question APIs.

For read-only delivery evidence:

```bash
node scripts/verify-agent-sync.mjs ~/.openclaw/openclaw.json <channel-id> <unique-marker>
```

The verifier prints matching timestamps and roles without tokens or unrelated chat contents. It checks the latest 100 channel messages.
