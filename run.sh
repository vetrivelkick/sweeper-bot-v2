#!/bin/bash
cd "$(dirname "$0")"
if [ ! -f .env ]; then
  echo "ERROR: .env file not found. Copy .env.example to .env and fill in credentials."
  exit 1
fi
# P1 #8: Safe .env parsing - use set -a + source instead of unsafe xargs
# xargs breaks on values with spaces, quotes, or special characters
set -a
source .env
set +a
# P1 #7: Respect LIVE_MODE env var (default: paper mode for safety)
MODE="${LIVE_MODE:-paper}"
if [ "$MODE" = "live" ]; then
  echo "WARNING: Starting in LIVE mode with real funds."
  echo "Type 'yes' to confirm, anything else to abort:"
  read -r confirm
  if [ "$confirm" = "yes" ]; then
    python3 main.py --live
  else
    echo "Aborted. Starting in paper mode instead."
    python3 main.py --paper
  fi
else
  python3 main.py --paper
fi
