"""State matrix: what the real web backend accepts in each operator phase.

The browser UI enables its buttons from the ``/api/state`` snapshot alone
(``ui/ui-core.mjs``). This module drives the real ``GameController`` into every
phase an operator can reach, records the snapshot, and records which control
actions the backend accepts there. The JS tests and eval read the resulting
``state-matrix.json`` instead of re-implementing backend rules, so the two
sides can disagree only where a test or eval shows it.

It also records real ``/api/preview`` responses (``preview-samples.json``)
so the panel's detection lights are tested against what the backend sends.

Regenerate after changing backend behavior or the snapshot shape::

    ./.venv/bin/python -m services.web.contract.matrix
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path
from threading import RLock
from typing import Any

import cv2

from services.calibration import ALIGNMENT_FRAMES, alignment_layout
from services.calibration.tests.render import homography_from_quad, project_to_camera, render_pattern
from services.web.backend.eval import FRAME_HUES, _color, _frame, _image, sampled_hex
from services.web.backend.server import GameController, RequestError

MATRIX_PATH = Path(__file__).with_name("state-matrix.json")
PREVIEWS_PATH = Path(__file__).with_name("preview-samples.json")
RECOLORED = {"start": 105, "goal": 176, "bumper": 28, "marker": 140}
PROJECTOR = (1280, 800)
# Each UI control, expressed as the backend calls the browser makes for it.
CONTROLS = ("scan", "launch", "aim", "reset", "resume", "align")
# Where the projector's corners land in the 960x720 synthetic camera frame.
ALIGN_QUAD = ((30, 25), (930, 20), (935, 700), (25, 695))


@lru_cache(maxsize=1)
def _alignment_jpegs() -> tuple[bytes, ...]:
    """The 14 pattern frames as the camera films them; the same for every run."""
    layout = alignment_layout(*PROJECTOR)
    G = homography_from_quad(PROJECTOR, ALIGN_QUAD)
    frames = []
    for bit, inverse in ALIGNMENT_FRAMES:
        bgr = project_to_camera(render_pattern(layout, bit, inverse), G, (960, 720))
        okay, encoded = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, 96])
        assert okay
        frames.append(encoded.tobytes())
    return tuple(frames)


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
        self.controller.scan_jpeg(_frame(430))

    def launch(self, degrees: float) -> None:
        self.act("aim", degrees=degrees)
        self.act("launch")

    def align(self) -> None:
        """A real alignment run: coded patterns filmed through a known homography."""
        state = self.act("align_start")
        layout = state["alignment"]["layout"]
        assert layout == alignment_layout(*PROJECTOR)
        for (bit, inverse), jpeg in zip(ALIGNMENT_FRAMES, _alignment_jpegs()):
            self.controller.align_frame_jpeg(jpeg, f"{bit}:{int(inverse)}")
        assert self.act("align_solve")["alignment"]["status"] == "ready"
        self.controller.preview_jpeg(_frame(430))


    def play_out(self) -> None:
        for _ in range(80):
            self.now += 0.1
            if self.controller.tick()["game"]["status"] != "running":
                self.controller.preview_jpeg(_frame(430))
                return
        raise AssertionError("attempt never finished")


def _waiting_camera(w: _Wall) -> None:
    w.open_projector()


def _partial_live(w: _Wall) -> None:
    w.open_projector()
    image = _image(430)
    image[315:386, 145:216] = 220
    w.controller.preview_jpeg(_jpeg(image))


def _no_markers(w: _Wall) -> None:
    w.open_projector()
    w.controller.preview_jpeg(_no_markers_frame())


def _ready(w: _Wall) -> None:
    w.open_projector()
    w.controller.preview_jpeg(_frame(430))


def _running(w: _Wall) -> None:
    _ready(w)
    w.launch(-10)   # under gravity the lower arc to the goal of _frame(430)


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


def _aligned_ready(w: _Wall) -> None:
    _ready(w)
    w.align()


def _ready_closed(w: _Wall) -> None:
    _ready(w)
    w.act("projection_close")


PHASES: dict[str, Callable[[_Wall], None]] = {
    "projector_closed": lambda w: None,
    "waiting_camera": _waiting_camera,
    "partial_live": _partial_live,
    "no_markers": _no_markers,
    "ready": _ready,
    "aligned_ready": _aligned_ready,
    "ready_closed": _ready_closed,
    "running": _running,
    "paused_open": _paused_open,
    "paused_closed": _paused_closed,
    "won": _won,
    "lost": _lost,
}

_PROBES: dict[str, Callable[[_Wall], None]] = {
    "scan": _Wall.scan,
    "launch": lambda w: w.act("launch"),
    "aim": lambda w: w.act("aim", degrees=0),
    "reset": lambda w: w.act("reset"),
    "resume": lambda w: w.act("resume"),
    "align": lambda w: w.act("align_start"),
}


_REACHED: dict[str, _Wall] = {}


def _reach(phase: str) -> _Wall:
    """An independent copy of a phase; each phase is driven once per process."""
    if phase not in _REACHED:
        wall = _Wall()
        PHASES[phase](wall)
        _REACHED[phase] = wall
    return clone(_REACHED[phase])


def clone(wall: _Wall) -> _Wall:
    """An independent copy of a phase, so each probe skips rebuilding it.

    The controller's values are immutable or replaced on change; containers
    are copied, and the lock and the clock are the copy's own.
    """
    twin = _Wall.__new__(_Wall)
    twin.now = wall.now
    controller = copy.copy(wall.controller)
    for name, value in vars(controller).items():
        if isinstance(value, (list, dict, set)):
            setattr(controller, name, copy.deepcopy(value))
    controller._lock = RLock()
    controller._clock = lambda: twin.now
    twin.controller = controller
    return twin


def build_matrix() -> dict[str, Any]:
    """Drive the real backend; every probe runs on its own copy of the phase."""
    phases: dict[str, Any] = {}
    for name in PHASES:
        wall = _reach(name)
        accepts = {}
        for control in CONTROLS:
            try:
                _PROBES[control](clone(wall))
            except RequestError:
                accepts[control] = False
            else:
                accepts[control] = True
        phases[name] = {"state": wall.controller.snapshot(), "accepts": accepts}
    return {"version": 2, "projector": list(PROJECTOR), "phases": phases}


def _jpeg(image) -> bytes:
    okay, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 96])
    assert okay
    return encoded.tobytes()


def _no_markers_frame() -> bytes:
    image = _image(430)
    for x, y in ((90, 90), (870, 90), (870, 630), (90, 630)):
        image[y - 26:y + 27, x - 26:x + 27] = 220
    return _jpeg(image)


def _controller(choices: dict[str, str]) -> GameController:
    game = GameController(log_path=None)
    for role, hex_color in choices.items():
        game.action({"action": "palette_set", "role": role, "hex": hex_color})
    return game


def _scan_ok(choices: dict[str, str], frame: bytes) -> bool:
    game = _controller(choices)
    game.action({"action": "projection_open", "width": PROJECTOR[0], "height": PROJECTOR[1]})
    try:
        game.scan_jpeg(frame)
    except RequestError:
        return False
    return True


def build_previews() -> dict[str, Any]:
    """Real preview responses, the palette behind each, and whether a scan of
    the same frame succeeds, so the lights can be checked as a predictor."""
    no_start = _image(430)
    no_start[315:386, 145:216] = 220
    extra_corner = _image(430)
    cv2.rectangle(extra_corner, (458, 578), (502, 622), _color(FRAME_HUES["marker"]), -1)
    recolored = _frame(430, RECOLORED)
    picked = {role: sampled_hex(recolored, role) for role in ("marker", "start", "goal", "bumper")}
    samples = {
        "full_wall": ({}, _frame(430)),
        "no_markers": ({}, _no_markers_frame()),
        "missing_start": ({}, _jpeg(no_start)),
        "extra_corner": ({}, _jpeg(extra_corner)),
        "recolored_default_colors": ({}, recolored),
        "recolored_picked_colors": (picked, recolored),
    }
    result = {}
    for name, (choices, frame) in samples.items():
        game = _controller(choices)
        result[name] = {"palette": game.snapshot()["palette"],
                        "preview": game.preview_jpeg(frame),
                        "scan_ok": _scan_ok(choices, frame)}
    return {"version": 2, "samples": result}


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
    for path, content in ((MATRIX_PATH, build_matrix()), (PREVIEWS_PATH, build_previews())):
        path.write_text(json.dumps(content, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                        encoding="utf-8")
        print(f"wrote {path}")


if __name__ == "__main__":
    main()
