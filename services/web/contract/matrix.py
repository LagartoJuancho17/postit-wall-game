"""State matrix: what the real web backend accepts in each operator phase.

The browser UI enables its buttons from the ``/api/state`` snapshot alone
(``ui/ui-core.mjs``). This module drives the real ``GameController`` into every
phase an operator can reach, records the snapshot, and records which control
actions the backend accepts there. The JS tests and eval read the resulting
``state-matrix.json`` instead of re-implementing backend rules, so the two
sides can disagree only where a test or eval shows it.

Regenerate after changing backend behavior or the snapshot shape::

    ./.venv/bin/python -m services.web.contract.matrix
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from contracts import quadrilateral_center
from services.web.backend.eval import _frame
from services.web.backend.server import GameController, RequestError

MATRIX_PATH = Path(__file__).with_name("state-matrix.json")
PROJECTOR = (1280, 800)
# Each UI control, expressed as the backend calls the browser makes for it.
CONTROLS = ("scan", "calibrate", "launch", "aim", "reset", "resume")


class _Wall:
    """A controller on a fake clock, plus the steps that reach each phase."""

    def __init__(self) -> None:
        self.now = 0.0
        self.controller = GameController(log_path=None, clock=lambda: self.now)

    def act(self, name: str, **data: object) -> dict[str, Any]:
        return self.controller.action({"action": name, **data})

    def open_projector(self) -> None:
        self.act("projection_open", width=PROJECTOR[0], height=PROJECTOR[1])

    def scan(self) -> None:
        self.act("projection_black")
        self.controller.scan_jpeg(_frame(430))

    def calibrate(self) -> None:
        self.act("calibrate")
        for marker in self.controller.camera_board.markers:
            point = quadrilateral_center(marker)
            self.act("calibration_confirm", x=point.x + 80, y=point.y + 30)

    def launch(self, degrees: float) -> None:
        self.act("aim", degrees=degrees)
        self.act("launch")

    def play_out(self) -> None:
        for _ in range(80):
            self.now += 0.1
            if self.controller.tick()["game"]["status"] != "running":
                return
        raise AssertionError("attempt never finished")


def _waiting_camera(w: _Wall) -> None:
    w.open_projector()


def _scanned(w: _Wall) -> None:
    w.open_projector()
    w.scan()


def _calibrating(w: _Wall) -> None:
    _scanned(w)
    w.act("calibrate")


def _ready(w: _Wall) -> None:
    _scanned(w)
    w.calibrate()


def _running(w: _Wall) -> None:
    _ready(w)
    w.launch(0)


def _paused_closed(w: _Wall) -> None:
    _running(w)
    w.act("projection_close")


def _paused_open(w: _Wall) -> None:
    _paused_closed(w)
    w.open_projector()


def _won(w: _Wall) -> None:
    _running(w)
    w.play_out()


def _lost(w: _Wall) -> None:
    _ready(w)
    w.launch(180)
    w.play_out()


def _ready_closed(w: _Wall) -> None:
    _ready(w)
    w.act("projection_close")


PHASES: dict[str, Callable[[_Wall], None]] = {
    "projector_closed": lambda w: None,
    "waiting_camera": _waiting_camera,
    "scanned": _scanned,
    "calibrating": _calibrating,
    "ready": _ready,
    "ready_closed": _ready_closed,
    "running": _running,
    "paused_open": _paused_open,
    "paused_closed": _paused_closed,
    "won": _won,
    "lost": _lost,
}

_PROBES: dict[str, Callable[[_Wall], None]] = {
    "scan": _Wall.scan,
    "calibrate": lambda w: w.act("calibrate"),
    "launch": lambda w: w.act("launch"),
    "aim": lambda w: w.act("aim", degrees=0),
    "reset": lambda w: w.act("reset"),
    "resume": lambda w: w.act("resume"),
}


def _reach(phase: str) -> _Wall:
    wall = _Wall()
    PHASES[phase](wall)
    return wall


def build_matrix() -> dict[str, Any]:
    """Drive the real backend; every probe starts from a freshly built phase."""
    phases: dict[str, Any] = {}
    for name in PHASES:
        accepts = {}
        for control in CONTROLS:
            wall = _reach(name)
            try:
                _PROBES[control](wall)
            except RequestError:
                accepts[control] = False
            else:
                accepts[control] = True
        phases[name] = {"state": _reach(name).controller.snapshot(),
                        "accepts": accepts}
    return {"version": 1, "projector": list(PROJECTOR), "phases": phases}


def shape(value: Any) -> Any:
    """Keys and JSON types only, so geometry values may drift without noise."""
    if isinstance(value, dict):
        return {key: shape(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [shape(value[0])] if value else []
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    return "string"


def main() -> None:
    MATRIX_PATH.write_text(
        json.dumps(build_matrix(), ensure_ascii=False, indent=1, sort_keys=True) + "\n",
        encoding="utf-8")
    print(f"wrote {MATRIX_PATH}")


if __name__ == "__main__":
    main()
