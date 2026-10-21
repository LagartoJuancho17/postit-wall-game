"""Hardware-free browser-runtime evaluation with measurable wall geometry.

Run from the project root: ``python -m tests.eval_web_flow``.
The JPEGs model browser canvas uploads; no webcam, projector, or socket is used.
"""

from __future__ import annotations

import json
import math

import cv2

from contracts import Point
from services.runtime.tests.fixtures import (
    GOAL_CENTER,
    MARKER_CENTERS,
    PROJECTOR_SIZE,
    START_CENTER,
    camera_frame,
)
from services.web.backend.server import GameController, RequestError


def _jpeg(*, remove_markers: bool = False, **options: object) -> bytes:
    image = camera_frame(**options)
    if remove_markers:
        background = int(round(220 * float(options.get("brightness", 1.0))))
        for x, y in MARKER_CENTERS:
            image[y - 32:y + 33, x - 32:x + 33] = background
    okay, encoded = cv2.imencode(".jpg", image)
    if not okay:
        raise AssertionError("Could not encode synthetic camera frame")
    return encoded.tobytes()


def _center(polygon: dict[str, object]) -> Point:
    points = polygon["points"]
    return Point(
        sum(point["x"] for point in points) / len(points),
        sum(point["y"] for point in points) / len(points),
    )


def _error(a: Point, b: Point) -> float:
    return math.hypot(a.x - b.x, a.y - b.y)


def _case(name: str, **frame_options: object) -> dict[str, object]:
    controller = GameController(log_path=None)
    width, height = PROJECTOR_SIZE
    controller.action({"action": "projection_open", "width": width, "height": height})
    preview = controller.preview_jpeg(_jpeg(**frame_options))
    state = controller.snapshot()
    if state["phase"] != "ready" or state["board"] != state["camera_board"]:
        raise AssertionError("Live start and goal must prepare a board without scan or calibration")
    if state["live_detection"] != preview or state["projection"]["mode"] != "live":
        raise AssertionError("Projector must receive the exact live detection")
    board = state["board"]
    expected = [Point(*START_CENTER), Point(*GOAL_CENTER), Point(405, 190)]
    actual = [_center(board["start"]), _center(board["goal"]),
              _center(board["bumpers"][0])]
    geometry_error = max(_error(a, b) for a, b in zip(actual, expected))

    controller.action({"action": "aim", "degrees": 0})
    running = controller.action({"action": "launch"})
    if running["game"]["status"] != "running":
        raise AssertionError("Launch must start the attempt")
    try:
        controller.scan_jpeg(_jpeg(green_center=(515, 245)))
    except RequestError as error:
        refused_during_attempt = error.status.value == 409
    else:
        refused_during_attempt = False
    controller.preview_jpeg(_jpeg(green_center=(515, 245), **frame_options))
    if controller.snapshot()["board"] != board:
        raise AssertionError("Live post-it movement changed an active attempt")
    rescanned = controller.action({"action": "reset"})
    new_green = _center(rescanned["board"]["bumpers"][0])
    old_green = actual[2]
    moved_distance = _error(old_green, new_green)
    passed = (geometry_error <= 6 and refused_during_attempt and
              moved_distance >= 100 and rescanned["game"]["status"] == "ready" and
              rescanned["game"]["attempts"] == 1)
    return {"case": name, "passed": passed,
            "max_geometry_error_px": round(geometry_error, 3),
            "moved_bumper_distance_px": round(moved_distance, 3),
            "scan_refused_during_attempt": refused_during_attempt,
            "status_after_rescan": rescanned["game"]["status"]}


def run_eval() -> dict[str, object]:
    cases = []
    for name, options in (("normal", {}),
                          ("dim_and_speckled", {"brightness": 0.62, "speckles": True}),
                          ("no_markers", {"remove_markers": True}),
                          ("no_markers_dim", {"remove_markers": True, "brightness": 0.62})):
        try:
            cases.append(_case(name, **options))
        except (AssertionError, RequestError, ValueError, KeyError, IndexError) as error:
            cases.append({"case": name, "passed": False, "error": str(error)})
    score = sum(bool(case["passed"]) for case in cases) / len(cases)
    return {"score": score, "pass_threshold": 1.0, "cases": cases}


if __name__ == "__main__":
    report = run_eval()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["score"] >= report["pass_threshold"] else 1)
