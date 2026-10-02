#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
PY=./.venv/bin/python
if [ ! -x "$PY" ]; then
  printf '%s\n' 'Falta .venv: ejecutá la instalación indicada en README.md' >&2
  exit 1
fi
"$PY" -m unittest discover -s . -p 'test_*.py' -q
"$PY" -m services.calibration.evals.synthetic
"$PY" -m services.vision.eval
"$PY" -m services.physics.evals.scenarios
"$PY" -m services.runtime.eval
