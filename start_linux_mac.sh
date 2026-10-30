#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")"
if ! python3 -c 'import flask' >/dev/null 2>&1; then
    python3 -m pip install -r requirements.txt
fi
printf '\nMonster PvP: http://127.0.0.1:5000\n'
python3 app.py
