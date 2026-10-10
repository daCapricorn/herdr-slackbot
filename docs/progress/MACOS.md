# macOS implementation and verification

Implemented 2026-10-10 on macOS arm64, Python 3.12.15, Herdr 0.9.3 / protocol 22.

Changes:

- Native AF_UNIX transport shares existing NDJSON request/stream framing and safe CLI fallback rules. Stream shutdown wakes a blocked reader.
- Darwin process creation times and child enumeration use psutil. Readiness and stop identity checks retain PID-reuse protection.
- Shell detection handles POSIX paths and login shells as well as Windows paths. A newly created macOS pane with no foreground metadata gets POSIX command syntax. Literal backslashes in POSIX paths survive quoting.
- macOS build/startup/actions and shell bootstrap coexist with the existing Windows PowerShell entrypoints. macOS action IDs have the `-macos` suffix.
- Config honors Herdr's injected binary path, and macOS config discovery has the verified local-layout fallback.

Verification:

- Full offline suite: `.venv/bin/python -m pytest -q -p no:cacheprovider` → **840 passed, 5 skipped**. The skips are the opt-in Herdr live tests.
- Actual running Herdr read-only checks: `HERDR_LIVE_TESTS=1 .venv/bin/python -m pytest -q -p no:cacheprovider tests/test_live.py` → **5 passed**. Verified native socket RPC, error replies, CLI agreement, live subscription cancellation, and subscription-manager initialization.
- `sh scripts/setup.sh --skip-config` successfully ran ensurepip for the existing environment, built the editable wheel and installed the package with its macOS dependency.
- `pip check` passed; shell syntax checks and `git diff --check` passed.
- `herdr plugin link <checkout> --disabled` accepted the actual manifest. Action listing registered all macOS actions. The local plugin remains disabled pending Slack setup.
- New regressions cover real local socket fragmentation/Unicode, ACK/event leftovers, blocked-read cancellation, unknown mutation outcomes, macOS process creation and exit, quoting/default shell, manifest platform entries, and invoking the wrapper from another cwd.

Real Slack is not yet configured. The plugin config has neither `SLACK_BOT_TOKEN` nor `SLACK_APP_TOKEN`, and no paired owner. No real Slack messages or approvals were sent. Complete the [macOS setup guide](../MACOS.md) to verify that final integration.

The Windows code paths and their offline assertions pass on this macOS run. A native Windows execution was not performed.
