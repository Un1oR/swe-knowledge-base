#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"

MYST="$REPO_ROOT/.venv/bin/myst"

if [[ ! -x "$MYST" ]]; then
  echo "error: MyST CLI not found at $MYST" >&2
  echo "hint: create .venv and install requirements.txt first" >&2
  exit 1
fi

if ! NODE_BIN="$(command -v node 2>/dev/null)"; then
  echo "error: node is not available in PATH" >&2
  exit 1
fi

echo "node: $NODE_BIN" >&2

if [[ "$NODE_BIN" == /snap/bin/node ]]; then
  SNAP_NODE_DIR="$(node -p 'require("node:path").dirname(process.execPath)' 2>/dev/null || true)"
  if [[ -n "$SNAP_NODE_DIR" && -x "$SNAP_NODE_DIR/node" ]]; then
    echo "snap node detected: prepending $SNAP_NODE_DIR to PATH" >&2
    export PATH="$SNAP_NODE_DIR:$PATH"
  else
    echo "error: node is installed via snap, but the real node binary was not found" >&2
    exit 1
  fi
fi

if ! command -v npm >/dev/null 2>&1; then
  echo "error: npm is not available in PATH" >&2
  exit 1
fi

echo "effective node: $(command -v node)" >&2
echo "effective npm: $(command -v npm)" >&2

cd "$REPO_ROOT"
exec "$MYST" build --html "$@"
