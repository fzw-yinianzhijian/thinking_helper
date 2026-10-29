#!/usr/bin/env bash
# Install only this checkout's unprivileged persistent service.
set -euo pipefail
project_root="$(cd "$(dirname "$0")/.." && pwd -P)"
unit_name=thinking-helper.service
unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
dry_run=0
case "${1:-}" in
  --dry-run) dry_run=1 ;;
  --help|-h) echo 'Usage: bash scripts/install-user-service.sh [--dry-run]'; exit 0 ;;
  '') ;;
  *) echo 'Usage: bash scripts/install-user-service.sh [--dry-run]' >&2; exit 2 ;;
esac
if [ "$#" -gt 1 ]; then echo 'Only --dry-run is supported.' >&2; exit 2; fi
case "$project_root" in *$'\n'*|*$'\r'*) echo 'Project path must not contain a newline.' >&2; exit 1 ;; esac
unit_root=${project_root//\\/\\\\}
unit_root=${unit_root//\"/\\\"}
unit_root=${unit_root//%/%%}
exec_root=${unit_root//\$/\$\$}

render_unit() {
  cat <<EOF
[Unit]
Description=Thinking Helper persistent background service

[Service]
Type=simple
WorkingDirectory=$unit_root
ExecStart="$exec_root/.venv/bin/python" -m uvicorn app.main:app --host 127.0.0.1 --port 8890
Restart=on-failure
RestartSec=3
TimeoutStopSec=30
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
EOF
}

if [ "$dry_run" -eq 1 ]; then
  echo "Dry run: would install $unit_dir/$unit_name"
  render_unit
  echo 'Would check the venv and user bus, reject a dedicated tmux session or occupied port, then run:'
  echo 'systemctl --user daemon-reload'
  echo 'systemctl --user enable --now thinking-helper.service'
  echo 'No files, services, sudo or linger settings changed.'
  exit 0
fi

if [ ! -x "$project_root/.venv/bin/python" ]; then
  echo 'Missing .venv/bin/python; run bash scripts/setup.sh first.' >&2; exit 1
fi
"$project_root/.venv/bin/python" -c 'import uvicorn' || { echo 'uvicorn is missing from the venv.' >&2; exit 1; }
if ! command -v systemctl >/dev/null 2>&1 || ! systemctl --user show-environment >/dev/null 2>&1; then
  echo 'A working systemd user bus is required. No system settings were changed.' >&2; exit 1
fi
if command -v tmux >/dev/null 2>&1 && tmux has-session -t '=thinking-helper' 2>/dev/null; then
  echo 'The dedicated tmux service exists. First run bash scripts/stop-remote.sh, then rerun this installer.' >&2
  exit 1
fi
if systemctl --user cat "$unit_name" >/dev/null 2>&1; then
  existing_root="$(systemctl --user show "$unit_name" --property=WorkingDirectory --value)"
  if [ "$existing_root" != "$project_root" ]; then
    echo 'Refusing to replace a thinking-helper.service owned by another checkout.' >&2; exit 1
  fi
fi
if ! systemctl --user is-active --quiet "$unit_name"; then
  "$project_root/.venv/bin/python" - <<'PY'
import socket
with socket.socket() as probe:
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind(("127.0.0.1", 8890))
    except OSError as error:
        raise SystemExit("Port 8890 is occupied. Stop its known owner before installing; no process was killed.") from error
PY
fi
mkdir -p "$unit_dir"
unit_temp="$(mktemp "$unit_dir/.thinking-helper.XXXXXX")"
trap 'rm -f "$unit_temp"' EXIT
render_unit > "$unit_temp"
chmod 644 "$unit_temp"
mv "$unit_temp" "$unit_dir/$unit_name"
systemctl --user daemon-reload
systemctl --user enable --now "$unit_name"
echo 'Installed and enabled thinking-helper.service. Check:'
echo 'systemctl --user status thinking-helper.service --no-pager'
echo 'journalctl --user -u thinking-helper.service -n 40 --no-pager'
