#!/usr/bin/bash

cd "$(dirname "$0")" || exit;
CWD="$(pwd)"
# Activate project's venv.sh
source "$CWD/venv/bin/activate"
python3 main.py