#!/usr/bin/env bash
# Backwards-compatible entry point — the full installer now lives in
# setup.command (double-clickable in Finder). This just forwards to it.
exec "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/setup.command" "$@"
