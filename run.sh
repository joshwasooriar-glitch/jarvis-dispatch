#!/usr/bin/env bash
# One-command start: creates a venv, installs deps, runs the smoke test, starts the server.
set -e
cd "$(dirname "$0")"
if [ ! -d .venv ]; then python3 -m venv .venv; fi
source .venv/bin/activate
pip install -q --upgrade pip
pip install -q -r requirements.txt
[ -f .env ] || cp .env.example .env
python tests/smoke_test.py
echo ""
echo "Open http://localhost:8000"
python -m jarvis.app
