#!/usr/bin/env bash
# Stop this checkout's service/session, never kill a port or unrelated process.
set -euo pipefail
project_root="$(cd "$(dirname "$0")/.." && pwd -P)"
unit_name=thinking-helper.service
unit_file="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/$unit_name"
stopped=0
if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
  if systemctl --user cat "$unit_name" >/dev/null 2>&1; then
    existing_root="$(systemctl --user show "$unit_name" --property=WorkingDirectory --value)"
    if [ "$existing_root" != "$project_root" ]; then
      echo 'Refusing to stop a thinking-helper.service owned by another checkout.' >&2; exit 1
    fi
    systemctl --user stop "$unit_name"
    echo 'Stopped thinking-helper.service; it remains installed/enabled for later start.'
    stopped=1
  fi
elif [ -f "$unit_file" ]; then
  echo 'The user unit exists but the systemd user bus is unavailable; cannot confirm it stopped.' >&2; exit 1
fi
if command -v tmux >/dev/null 2>&1 && tmux has-session -t '=thinking-helper' 2>/dev/null; then
  # list-panes targets a pane/window explicitly. Some tmux versions return
  # empty pane variables for display-message -t '=session' without a colon.
  pane_roots="$(tmux list-panes -s -t 'thinking-helper:' -F '#{pane_current_path}')"
  if [ -z "$pane_roots" ]; then echo 'Cannot verify the dedicated tmux panes; refusing to stop.' >&2; exit 1; fi
  while IFS= read -r pane_root; do
    if [ "$pane_root" != "$project_root" ]; then
      echo 'Refusing to stop a thinking-helper tmux session with a pane outside this checkout.' >&2; exit 1
    fi
  done <<< "$pane_roots"
  tmux kill-session -t '=thinking-helper'
  echo 'Stopped the dedicated thinking-helper tmux session.'
  stopped=1
fi
if [ "$stopped" -eq 0 ]; then echo 'No service or dedicated tmux session for this checkout was running.'; fi
