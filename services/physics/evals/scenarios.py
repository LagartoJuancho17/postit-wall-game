"""Periodic quality eval: realistic trajectories, with a numeric pass threshold.

Run from the project root: ``python -m services.physics.evals.scenarios``.
All scenarios are deterministic so a failed score can be reproduced locally.
"""

import json
import math

from contracts.model import Board, Point, Polygon
from services.physics import GameStatus, launch, step


def rect(left, top, right, bottom):
    return Polygon((Point(left, top), Point(right, top), Point(right, bottom), Point(left, bottom)))


def setup(start, goal, bumpers=()):
    return Board(rect(0, 0, 500, 300), start, goal, bumpers, ())


def run():
    cases = [
        ("direct goal", setup(rect(40, 140, 60, 160), rect(300, 140, 320, 160)), 0, 100, 3, GameStatus.WON, 0),
        ("single rebound", setup(rect(40, 140, 60, 160), rect(20, 140, 30, 160), (rect(200, 100, 220, 200),)), 0, 100, 3, GameStatus.WON, 1),
        ("miss and exit", setup(rect(40, 140, 60, 160), rect(300, 20, 320, 40)), 0, 100, 6, GameStatus.LOST, 0),
        ("fast goal", setup(rect(40, 140, 60, 160), rect(300, 140, 320, 160)), 0, 50_000, 0.01, GameStatus.WON, 0),
        ("corner rebound", setup(rect(140, 40, 160, 60), rect(450, 250, 470, 270), (rect(200, 100, 220, 120),)), 45, math.sqrt(2) * 100, 0.5, GameStatus.RUNNING, 1),
    ]
    results = []
    for name, field, angle, speed, duration, expected_status, expected_bounces in cases:
        state = step(field, launch(field, angle, speed), duration)
        passed = state.status == expected_status and state.bounce_count == expected_bounces
        results.append({
            "scenario": name,
            "passed": passed,
            "status": state.status.value,
            "bounces": state.bounce_count,
            "elapsed_s": round(state.elapsed_s, 6),
        })
    score = sum(item["passed"] for item in results) / len(results)
    report = {"score": score, "pass_threshold": 1.0, "passed": score >= 1.0, "scenarios": results}
    print(json.dumps(report, indent=2))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    run()
