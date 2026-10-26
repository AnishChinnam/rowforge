#!/bin/zsh
set -e
cd -- "$(dirname -- "$0")"
if [[ ! -x .venv/bin/python ]]; then
  python3 -m venv .venv
  .venv/bin/python -m pip install -r requirements.lock
  .venv/bin/python -m pip install -e . --no-deps
fi
print "Open http://127.0.0.1:8082 — keep this window open; press Control+C to stop."
exec .venv/bin/python -m rowforge demo
