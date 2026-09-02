#!/bin/bash
cd "$(dirname "$0")"
if [ ! -f .env ]; then
  echo "ERROR: .env file not found. Copy .env.example to .env and fill in credentials."
  exit 1
fi
export $(grep -v '^#' .env | xargs)
python3 main.py --paper
