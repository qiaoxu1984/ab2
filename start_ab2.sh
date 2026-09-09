#!/bin/sh
# Prepare and start all AB2 services on macOS with one command.
set -eu

# Resolve the repository directory so every service uses the same project root.
ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$ROOT_DIR"

# Create the project virtual environment on first launch and reuse it afterward.
PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
if [ ! -x "$PYTHON_BIN" ]; then
    python3 -m venv "$ROOT_DIR/.venv"
fi

# Install or update all Python dependencies before starting the services.
"$PYTHON_BIN" -m pip install -r requirements.txt
mkdir -p "$ROOT_DIR/data/service-logs"

# Use the existing Manager LAN address by default; override it through the environment when needed.
MANAGER_URL="${AB2_MANAGER_URL:-ws://172.18.67.71:8000/ws/agent}"

# Start Manager, Agent, and the local control page in the background with separate logs.
nohup "$PYTHON_BIN" -m uvicorn manager.main:app --host 0.0.0.0 --port 8000 >"$ROOT_DIR/data/service-logs/manager.log" 2>&1 &
nohup "$PYTHON_BIN" -m agent.service --manager "$MANAGER_URL" --config data/agent.json --web-port 8020 >"$ROOT_DIR/data/service-logs/agent.log" 2>&1 &
nohup "$PYTHON_BIN" control.py --host 127.0.0.1 --port 8010 >"$ROOT_DIR/data/service-logs/control.log" 2>&1 &

# Give the control page a moment to bind before opening the browser.
sleep 2
open "http://127.0.0.1:8010/"
printf '%s\n' 'AB2 Manager、Agent 和控制页已启动。'
