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
    known_mapping,
    mapped,
)
from services.web.backend.server import GameController, RequestError


def _jpeg(**options: object) -> bytes:
    okay, encoded = cv2.imencode(".jpg", camera_frame(**options))
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
    first = controller.scan_jpeg(_jpeg(**frame_options))
    if first["camera_board"] is None or first["board"] is not None:
        raise AssertionError("First scan must detect notes before calibration")
    controller.action({"action": "calibrate"})
    matrix = known_mapping()
    for marker in MARKER_CENTERS:
        point = mapped(Point(*marker), matrix)
        state = controller.action({"action": "calibration_confirm",
                                   "x": point.x, "y": point.y})
    if state["calibration"]["status"] != "ready" or state["game"]["status"] != "ready":
        raise AssertionError("Four marker confirmations must produce a ready game")
    board = state["board"]
    expected = [mapped(Point(*START_CENTER), matrix),
                mapped(Point(*GOAL_CENTER), matrix),
                mapped(Point(405, 190), matrix)]
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
    controller.action({"action": "reset"})
    controller.action({"action": "projection_black"})
    rescanned = controller.scan_jpeg(_jpeg(green_center=(515, 245)))
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
                          ("dim_and_speckled", {"brightness": 0.62, "speckles": True})):
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
