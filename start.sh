#!/bin/sh
# Start Training Log on http://localhost:8080.
# First run creates .venv (Python 3.14) and installs requirements.txt;
# later runs just start the app. Pass --reinstall to rebuild the venv.
set -e
cd "$(dirname "$0")"

if [ "$1" = "--reinstall" ]; then rm -rf .venv; shift; fi

if [ ! -x .venv/bin/python ]; then
    echo "Creating .venv and installing dependencies…"
    if command -v uv >/dev/null 2>&1; then
        uv venv --python 3.14 .venv
        uv pip install --python .venv/bin/python -r requirements.txt
    else
        python3 -m venv .venv
        .venv/bin/pip install -r requirements.txt
    fi
fi

exec .venv/bin/python app.py "$@"
