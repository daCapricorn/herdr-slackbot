#!/bin/sh
# Use the normal Codex runtime; --no-daemon remains an explicit optional argument.
set -eu
exec codex "$@"
