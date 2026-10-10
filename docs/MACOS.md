# Herdr Bridge on macOS

The OpenClaw Gateway starts the Bridge worker. Use the current [README](../README.md#setup) and [guide](GUIDE.md); old `setup-macos` / `start-macos` actions belong to the [archived standalone bot](../legacy/docs/MACOS.md).

## Runtime paths

Create a virtual environment in the checkout and install the runtime:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -e .
openclaw plugins install --link ./openclaw
```

Use absolute paths in plugin configuration. A macOS Gateway service may not inherit your interactive shell's PATH, so set `herdrBin` when needed. The standard Herdr socket is `~/.config/herdr/herdr.sock`; supply its expanded absolute path as `socketPath` if discovery does not find the server. The checkout can still be named `herdr-slackbot`.

Configure the existing OpenClaw Slack channel and owner allowlist, apply the [patch template](../openclaw/example.patch.json), then restart Gateway. If migrating, stop the former standalone bot first as described in the [migration guide](GUIDE.md#migrating-from-herdr-slackbot).

```bash
openclaw gateway restart
openclaw gateway status
openclaw plugins inspect herdr-bridge --runtime --json
```

## Verification

The new integration was verified on macOS with Herdr 0.9.3 and OpenClaw 2026.9.9. A real read-only command approval returned `Darwin` through Slack; phone push and the card's `dispatched` state were separately confirmed. Follow [LIVE_TEST.md](LIVE_TEST.md) for your account/device.

Development dependencies and checks:

```bash
.venv/bin/python -m pip install -e '.[dev,legacy-slack]'
.venv/bin/python -m pytest -q
cd openclaw
node --test test/*.test.js
```

The default Python run skips opt-in live tests. Existing `tests/test_live.py` checks the Herdr client and requires `HERDR_LIVE_TESTS=1` plus `HERDR_SOCKET_PATH`; it does not verify OpenClaw delivery or phone notifications.
