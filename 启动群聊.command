#!/bin/sh
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
if [ ! -x .venv/bin/python ]; then
    python3 install.py
fi
exec .venv/bin/python app2/app.py
