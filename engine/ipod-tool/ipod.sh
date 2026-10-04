#!/bin/bash
# Wrapper: keeps the Mac awake during long conversions and finds the tool next to itself.
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY=$(command -v python3 || command -v python)
[ -z "$PY" ] && { echo "ERROR: python3 not found"; exit 1; }
if command -v caffeinate >/dev/null; then
  exec caffeinate -i "$PY" "$DIR/ipod_sync.py" "$@"
else
  exec "$PY" "$DIR/ipod_sync.py" "$@"
fi
