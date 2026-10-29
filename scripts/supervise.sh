#!/usr/bin/env bash
# Own only this application's process; shutdown is explicit via the tmux session.
set -u
cd "$(dirname "$0")/.."
stopping=0
child=''
trap 'stopping=1; if [ -n "$child" ]; then kill -TERM "$child" 2>/dev/null || true; fi' TERM INT HUP
while [ "$stopping" -eq 0 ]; do
  bash scripts/start.sh &
  child=$!
  wait "$child" || true
  child=''
  if [ "$stopping" -eq 1 ]; then break; fi
  echo 'thinking-helper exited; restarting in 3 seconds. Durable tasks remain in SQLite.'
  sleep 3
done
