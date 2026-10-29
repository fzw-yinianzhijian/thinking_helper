#!/usr/bin/env bash
# Prefer the persistent user unit; keep tmux as an explicit fallback.
set -euo pipefail
project_root="$(cd "$(dirname "$0")/.." && pwd -P)"
cd "$project_root"
unit_name=thinking-helper.service
unit_file="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/$unit_name"
check_port() {
  if [ ! -x "$project_root/.venv/bin/python" ]; then
    echo 'Missing venv; run bash scripts/setup.sh first.' >&2; exit 1
  fi
  "$project_root/.venv/bin/python" - "$1" <<'PY'
import socket, sys
with socket.socket() as probe:
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind(("127.0.0.1", int(sys.argv[1])))
    except OSError as error:
        raise SystemExit("Port is occupied by another listener; no second supervisor was started.") from error
PY
}
unit_installed=0
if [ -f "$unit_file" ]; then unit_installed=1; fi
if command -v systemctl >/dev/null 2>&1 && systemctl --user cat "$unit_name" >/dev/null 2>&1; then unit_installed=1; fi
if [ "$unit_installed" -eq 1 ]; then
  if ! command -v systemctl >/dev/null 2>&1 || ! systemctl --user show-environment >/dev/null 2>&1; then
    echo 'The user unit is installed but its user bus is unavailable; refusing a second tmux supervisor.' >&2; exit 1
  fi
  existing_root="$(systemctl --user show "$unit_name" --property=WorkingDirectory --value)"
  if [ "$existing_root" != "$project_root" ]; then
    echo 'The installed user unit belongs to another checkout; refusing to start it.' >&2; exit 1
  fi
  if command -v tmux >/dev/null 2>&1 && tmux has-session -t '=thinking-helper' 2>/dev/null; then
    echo 'Stop the dedicated tmux service with bash scripts/stop-remote.sh before starting the user unit.' >&2; exit 1
  fi
  if ! systemctl --user is-active --quiet "$unit_name"; then check_port 8890; fi
  systemctl --user start "$unit_name"
  echo 'Requested thinking-helper.service start; inspect with systemctl --user status thinking-helper.service --no-pager'
  exit 0
fi
if ! command -v tmux >/dev/null 2>&1; then
  echo 'No user unit or tmux available. Install with bash scripts/install-user-service.sh.' >&2; exit 1
fi
if tmux has-session -t '=thinking-helper' 2>/dev/null; then
  echo 'Dedicated tmux service already exists; inspect with: tmux attach -t thinking-helper'
  exit 0
fi
check_port "${TH_PORT:-8890}"
mkdir -p data
printf -v start_command 'cd %q && exec bash scripts/supervise.sh >> data/server.log 2>&1' "$project_root"
tmux new-session -d -s thinking-helper "$start_command"
echo 'Started the fallback tmux supervisor. From your Mac: ssh -N -L 8890:127.0.0.1:8890 LLM_from_scratch'
