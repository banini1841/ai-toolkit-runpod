#!/usr/bin/env bash
# Linux/macOS wrapper — runs the Python installer.
# Usage: ./install.sh [--ai-toolkit /path/to/ai-toolkit]
set -e
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$(command -v python3 || command -v python)"
exec "$PY" "$DIR/install.py" "$@"
