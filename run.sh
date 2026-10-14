#!/bin/zsh
# Start the iDotMatrix local API + web control panel (see server/README.md).
cd "$(dirname "$0")"
PORT="${IDM_PORT:-8588}"
exec .venv/bin/uvicorn server:app --app-dir server --host 127.0.0.1 --port "$PORT"
