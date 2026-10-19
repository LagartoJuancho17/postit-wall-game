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
"$PY" -m services.web.backend.eval
"$PY" -m tests.eval_web_flow
"$PY" -m services.web.contract.camera_trials --check
"$PY" -m services.uvc.smoke
"$PY" -m services.browser.parity.export
node --test services/browser/tests/*.test.mjs
node services/browser/evals/parity.mjs
node --test services/web/tests/*.test.mjs
node services/web/evals/flows.mjs
node services/web/evals/live-projection.mjs
"$PY" -m services.web.evals.eyedropper
