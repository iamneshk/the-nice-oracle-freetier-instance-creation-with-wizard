#!/usr/bin/env bash

set -euo pipefail

show_help() {
    cat <<'EOF'
Usage: ./run.sh <command>

Commands:
  wizard    Interactively collect and save configuration
  validate  Validate the current configuration
  launch    Launch the instance using current configuration

Examples:
  ./run.sh wizard
  ./run.sh validate
  ./run.sh launch
EOF
}

if [ "$#" -eq 0 ]; then
    show_help
    exit 0
fi

if [ -f .venv/bin/activate ]; then
    source .venv/bin/activate
else
    python3 -m venv .venv
    source .venv/bin/activate
fi

python -m pip install --upgrade pip setuptools wheel
python -m pip install -r requirements.txt

python3 main.py "$@"

deactivate
