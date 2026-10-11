#!/bin/sh
# Keep hook ancestry bound to this TUI rather than a shared background daemon.
set -eu
exec codex --no-daemon "$@"
