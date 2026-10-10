# Running herdr-slackbot on macOS

Requirements: Herdr 0.8.2+, Python 3.11+, and permission to create/install a Slack app in your workspace. This fork keeps the Windows entrypoints and adds native macOS transport and plugin actions.

## Install your local fork

Install Python if needed:

```sh
brew install python
```

Inside a Herdr terminal, run these commands from your checkout:

```sh
herdr plugin link "$PWD"
sh scripts/setup.sh
herdr plugin action invoke setup-macos --plugin herdr-slackbot
```

The bootstrap selects Python 3.11+ and creates `.venv`. To use a specific interpreter, set `HERDR_SLACKBOT_PYTHON` to its path before running setup. For example:

```sh
HERDR_SLACKBOT_PYTHON=/opt/homebrew/bin/python3.12 sh scripts/setup.sh
```

The wizard opens a tab, creates the Slack app manifest, checks your tokens, starts the bridge, and waits for owner pairing. Send the pairing command shown in the bridge pane to the bot in Slack. Your slash command is still `/herdr-<username>` unless you customize it.

Tokens and the owner ID are stored under the path printed by `herdr plugin config-dir herdr-slackbot`, normally `~/.config/herdr/plugins/config/herdr-slackbot/.env`. They are not stored in the checkout. A build or bootstrap only writes the config skeleton; the interactive wizard completes Slack setup.

For GitHub distribution, use `herdr plugin install YOUR-ACCOUNT/YOUR-FORK` instead of linking. Install runs the macOS build step; local link does not run builds. Enabled startup hooks run when a Herdr server starts, not immediately on link.

## Control and diagnose the bridge

Run from a Herdr terminal so the commands inherit the intended `HERDR_SOCKET_PATH`:

```sh
herdr plugin action invoke start-macos --plugin herdr-slackbot
herdr plugin action invoke status-macos --plugin herdr-slackbot
herdr plugin action invoke restart-macos --plugin herdr-slackbot
herdr plugin action invoke stop-macos --plugin herdr-slackbot
```

The command palette uses the same human-readable action titles as Windows. Action IDs have a `-macos` suffix because Herdr requires action IDs to be unique across all platforms. See the [Herdr manifest reference](https://raw.githubusercontent.com/herdrdev/herdr/v0.9.3/docs/next/website/src/content/docs/plugins.mdx).

If you linked the plugin with `--disabled` for validation, enable it before invoking an action:

```sh
herdr plugin enable herdr-slackbot
```

Direct commands are also available:

```sh
sh scripts/herdr-slackbot.sh check
sh scripts/herdr-slackbot.sh status
sh scripts/herdr-slackbot.sh launch
sh scripts/herdr-slackbot.sh open-wizard
```

The bridge opens its own tab in workspace `herdr-slack`. `HERDR_BIN_PATH` and `HERDR_SOCKET_PATH` are inherited from Herdr. You can override the executable with `HERDR_BIN` in `.env`. macOS connections use Unix-domain sockets with the same NDJSON API as Windows named pipes. Event-stream close calls socket shutdown to unblock a waiting reader. Process identity uses creation time via psutil so restart, stop, and ready markers can distinguish reused PIDs.

If setup reports that an existing `.venv` is too old, recreate that environment with Python 3.11+ before retrying. A checkout copied from Windows also needs a new macOS environment. If a command reports missing Slack tokens, run the setup wizard; Python and Herdr connectivity alone do not configure Slack.

## Development checks

```sh
.venv/bin/python -m pip install pytest
.venv/bin/python -m pytest -q
HERDR_LIVE_TESTS=1 .venv/bin/python -m pytest -q tests/test_live.py
```

The live Herdr tests are read-only and need a running server plus `HERDR_SOCKET_PATH`. They do not post to Slack. Use [LIVE_TEST.md](LIVE_TEST.md) for the real Slack completion, approval, and pairing checks after configuration.

The macOS suite includes real local Unix-socket RPC, fragmented UTF-8/NDJSON, subscription shutdown, no resend after an unknown mutating outcome, process creation/exit, readiness, and running the shell wrapper from a different working directory.
