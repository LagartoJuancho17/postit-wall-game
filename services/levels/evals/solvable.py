"""Periodic eval: every level can be won, needs what it says, and forgives a hand.

Run from the project root: ``python -m services.levels.evals.solvable``.
Deterministic (seeded). For each level of levels.json except "libre", on the
reference projector (1920 x 1080, 76 mm post-its on a wall ~1.1 m tall):

- its stored solution obeys the level (zones, bumper limit, nothing covered)
  and wins from the zone centers;
- the bare drop (no bumpers) wins only when the level allows none: every
  other level needs its bumpers;
- robustness: the share of throws that still win with the solution stuck by
  hand (each bumper 8 px off and 4 degrees turned, start and goal off their
  zone centers by 5% of the zone), over 100 throws. A level under
  PASS["robustness_min"] is too fiddly to play on a real wall;
- the same solution at 1280 x 800 (the panel tests' projector), reported.
"""

from __future__ import annotations

import json

from services.levels import FREE_LEVEL, LEVELS
from services.levels.design import FIELD, play, robustness

TRIALS = 100
PASS = {"solutions_win": 1.0, "bare_needs_bumpers": 1.0, "robustness_min": 0.5}


def run() -> dict:
    levels = {}
    for level in LEVELS:
        if level.id == FREE_LEVEL:
            continue
        solution = list(level.solution)
        won = play(level, solution)
        bare = play(level, [])
        levels[level.id] = {
            "bumpers": len(solution), "bumpers_max": level.bumpers_max,
            "solution_wins": won.status == "won" and won.problem is None,
            "solution_problem": won.problem,
            "bare": bare.ended_by,
            "bare_ok": (bare.status == "won") == (level.bumpers_max == 0),
            "robustness": round(robustness(level, solution, trials=TRIALS, seed=7), 3),
            "small_projector_wins": play(level, solution, size=(1280, 800)).status == "won",
        }
    count = len(levels)
    report = {
        "setup": {"field": list(FIELD), "trials": TRIALS},
        "levels": levels,
        "solutions_win": sum(v["solution_wins"] for v in levels.values()) / count,
        "bare_needs_bumpers": sum(v["bare_ok"] for v in levels.values()) / count,
        "robustness_min": min(v["robustness"] for v in levels.values()),
        "pass": PASS,
    }
    report["passed"] = (report["solutions_win"] >= PASS["solutions_win"]
                        and report["bare_needs_bumpers"] >= PASS["bare_needs_bumpers"]
                        and report["robustness_min"] >= PASS["robustness_min"])
    return report


if __name__ == "__main__":
    result = run()
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if not result["passed"]:
        raise SystemExit(1)
