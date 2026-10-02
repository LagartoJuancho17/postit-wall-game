"""Periodic, hardware-free evaluation of the complete runtime data path.

Run from the project root: ``python -m services.runtime.eval``. Valid cases
must detect, map, fit, and produce a playable ball state. An invalid board
must be rejected before play. Known synthetic geometry makes alignment
error measurable in projector pixels.
"""

import json
import math
import os
from dataclasses import replace

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

from contracts import Point
from services.calibration import estimate_homography, reprojection_errors
from services.physics import GameStatus, launch, ready_state, step
from services.runtime.projector import center
from services.runtime.session import board_fits_screen, calibration_points, prepare_board
from services.runtime.tests.test_calibration_flow import fake_session
from services.runtime.tests.fixtures import (
    GOAL_CENTER,
    MARKER_CENTERS,
    PROJECTOR_SIZE,
    START_CENTER,
    camera_frame,
    camera_observations,
    known_mapping,
    mapped,
)
from services.vision import detect_board


def _distance(a: Point, b: Point) -> float:
    return math.hypot(a.x - b.x, a.y - b.y)


def _noisy_observations(projector_points: tuple[Point, ...]) -> tuple[Point, ...]:
    """Simulate repeatable subpixel webcam localization error."""
    return tuple(
        Point(point.x + 0.22 * math.sin(index * 2.1),
              point.y + 0.22 * math.cos(index * 1.3))
        for index, point in enumerate(camera_observations(projector_points))
    )


def run_eval() -> dict[str, object]:
    fit, checks = calibration_points(*PROJECTOR_SIZE)
    cases = (
        ("normal", 1.0, False, (405, 190)),
        ("dim_with_noise", 0.58, True, (405, 190)),
        ("moved_green_between_attempts", 0.8, True, (515, 245)),
    )
    results: list[dict[str, object]] = []
    for name, brightness, speckles, green_center in cases:
        try:
            matrix = estimate_homography(_noisy_observations(fit), fit)
            holdout_error = max(reprojection_errors(camera_observations(checks), checks, matrix))
            board = prepare_board(
                detect_board(camera_frame(green_center=green_center,
                                          brightness=brightness,
                                          speckles=speckles)),
                matrix,
                *PROJECTOR_SIZE,
            )
            expected = [
                mapped(Point(*START_CENTER), known_mapping()),
                mapped(Point(*GOAL_CENTER), known_mapping()),
                mapped(Point(*green_center), known_mapping()),
                *(mapped(Point(*source), known_mapping()) for source in MARKER_CENTERS),
            ]
            actual = [
                center(board.start),
                center(board.goal),
                center(board.bumpers[0]),
                *board.playfield.points,
            ]
            max_geometry_error = max(_distance(a, b) for a, b in zip(actual, expected))
            inside = board_fits_screen(board, *PROJECTOR_SIZE)
            ready = ready_state(board)
            moving = step(board, launch(board, 0, 300), 0.05)
            passed = (
                holdout_error <= 2
                and max_geometry_error <= 5
                and len(board.markers) == 4
                and len(board.bumpers) == 1
                and inside
                and ready.status == GameStatus.READY
                and moving.status == GameStatus.RUNNING
                and moving.position.x > ready.position.x
            )
            results.append({
                "case": name,
                "passed": passed,
                "holdout_reprojection_error_px": round(holdout_error, 3),
                "max_geometry_error_px": round(max_geometry_error, 3),
                "board_fits_screen": inside,
                "ball_ready": ready.status == GameStatus.READY,
            })
        except (ValueError, RuntimeError, IndexError) as exc:
            results.append({"case": name, "passed": False, "error": str(exc)})
    try:
        fit, _ = calibration_points(*PROJECTOR_SIZE)
        matrix = estimate_homography(_noisy_observations(fit), fit)
        valid = prepare_board(detect_board(camera_frame()), matrix,
                              *PROJECTOR_SIZE)
        impossible = replace(valid, start=valid.bumpers[0])
        identity = ((1.0, 0.0, 0.0),
                    (0.0, 1.0, 0.0),
                    (0.0, 0.0, 1.0))
        prepare_board(impossible, identity, *PROJECTOR_SIZE)
    except ValueError as exc:
        results.append({"case": "invalid_layout_rejected", "passed": True,
                        "reason": str(exc)})
    else:
        results.append({"case": "invalid_layout_rejected", "passed": False})
    edge_session, observations, events = fake_session(bad_last_edge=True)
    try:
        edge_session._automatic_calibration()
    except RuntimeError as exc:
        edge_passed = "desvió" in str(exc) and len(observations) == 17
    else:
        edge_passed = False
    results.append({
        "case": "edge_alignment_error_rejected",
        "passed": edge_passed,
        "max_edge_error_px": events[0][1]["max_edge_error_px"] if events else None,
    })
    score = sum(bool(case["passed"]) for case in results) / len(results)
    return {"score": score, "pass_threshold": 1.0, "cases": results}


if __name__ == "__main__":
    report = run_eval()
    print(json.dumps(report, indent=2))
    raise SystemExit(0 if report["score"] >= report["pass_threshold"] else 1)
