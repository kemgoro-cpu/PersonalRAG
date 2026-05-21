#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

if command -v python >/dev/null 2>&1; then
    exec python scripts/setup.py "$@"
fi

if command -v py >/dev/null 2>&1; then
    exec py -3 scripts/setup.py "$@"
fi

echo "[setup error] Python 3.10+ was not found. Install Python and retry." >&2
exit 1
