#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m venv .venv
if [ -f requirements.lock ]; then
  .venv/bin/python -m pip install -r requirements.lock
else
  .venv/bin/python -m pip install -e .
fi
.venv/bin/python -m unittest discover -s tests -v
