"""Periodic eval: the web game's ball under gravity (GRAVITY_SHARE, LAUNCH_SPEED_SHARE).

Run from the project root: ``python -m services.physics.evals.gravity``.
Deterministic (seeded). It measures what the change is for:

- playable: on random boards (start in the left third, goal anywhere right
  of it, 16:9 and 4:3 fields) some aim the panel can set (5 degree steps)
  reaches the goal;
- exact: shots in open space land on the analytic parabola (apex, x at the
  apex, landing time) whatever the step;
- bounded: with bumpers anywhere, every attempt ends (won, lost, or the
  MAX_ATTEMPT_S limit) without an error.
"""

from __future__ import annotations

import json
import math
import random

from contracts.model import Board, Point, Polygon
from services.physics import (GRAVITY_SHARE, LAUNCH_SPEED_SHARE, MAX_ATTEMPT_S, GameStatus, launch,
                              ready_state, step)

DT = 1 / 120
AIMS = range(-85, 90, 5)
PASS = {"playable": 0.95, "exact_px": 0.05, "bounded": 1.0}


def rect(cx, cy, w, h) -> Polygon:
    return Polygon((Point(cx - w / 2, cy - h / 2), Point(cx + w / 2, cy - h / 2),
                    Point(cx + w / 2, cy + h / 2), Point(cx - w / 2, cy + h / 2)))


def field(width, height, start, goal, bumpers=()) -> Board:
    return Board(Polygon((Point(0, 0), Point(width, 0), Point(width, height), Point(0, height))),
                 start, goal, tuple(bumpers), ())


def play(board: Board, aim: float, side: float) -> tuple[GameStatus, float]:
    state = launch(board, aim, LAUNCH_SPEED_SHARE * side, GRAVITY_SHARE * side)
    while state.status == GameStatus.RUNNING:
        state = step(board, state, DT)
    return state.status, state.elapsed_s


def playable(rng: random.Random) -> dict:
    boards = winnable = 0
    times = []
    for _ in range(80):
        width, height = rng.choice([(1280, 720), (1920, 1080), (960, 720)])
        side = min(width, height)
        note = 0.08 * side
        start = rect(rng.uniform(0.08, 0.3) * width, rng.uniform(0.35, 0.85) * height, note, note)
        goal = rect(rng.uniform(0.45, 0.92) * width, rng.uniform(0.15, 0.85) * height, note, note)
        board = field(width, height, start, goal)
        try:
            ready_state(board)
        except ValueError:
            continue
        boards += 1
        wins = [t for aim in AIMS for status, t in [play(board, aim, side)] if status == GameStatus.WON]
        if wins:
            winnable += 1
            times.append(min(wins))
    return {"boards": boards, "winnable": winnable, "share": round(winnable / boards, 4),
            "fastest_win_s_median": round(sorted(times)[len(times) // 2], 3) if times else None}


def exact() -> dict:
    worst = 0.0
    for width, height, aim in ((1280, 720, -45), (1920, 1080, -60), (960, 720, -30)):
        side = min(width, height)
        g, v = GRAVITY_SHARE * side, LAUNCH_SPEED_SHARE * side
        start = rect(0.1 * width, 0.9 * height, 0.06 * side, 0.06 * side)
        board = field(width * 10, height * 10, start, rect(width * 9, height * 9, 10, 10))   # open space
        x0, y0 = 0.1 * width, 0.9 * height
        vx, vy = v * math.cos(math.radians(aim)), v * math.sin(math.radians(aim))
        apex_t = -vy / g
        for dt in (1 / 120, 1 / 60):
            state = launch(board, aim, v, g)
            steps = round(apex_t / dt)
            for _ in range(steps):
                state = step(board, state, dt)
            t = steps * dt
            want = (x0 + vx * t, y0 + vy * t + 0.5 * g * t * t)
            worst = max(worst, math.hypot(state.position.x - want[0], state.position.y - want[1]))
    return {"max_error_px": worst}


def bounded(rng: random.Random) -> dict:
    attempts = ended = errors = 0
    longest = 0.0
    for _ in range(150):
        width, height = 1280, 720
        side = min(width, height)
        start = rect(rng.uniform(100, 300), rng.uniform(200, 600), 50, 50)
        goal = rect(rng.uniform(800, 1180), rng.uniform(100, 600), 50, 50)
        bumpers = [rect(rng.uniform(350, 750), rng.uniform(150, 680), rng.uniform(40, 300), rng.uniform(20, 60))
                   for _ in range(rng.randrange(1, 6))]
        board = field(width, height, start, goal, bumpers)
        try:
            ready_state(board)
        except ValueError:
            continue
        attempts += 1
        try:
            status, elapsed = play(board, rng.choice(list(AIMS)), side)
            ended += status in (GameStatus.WON, GameStatus.LOST)
            longest = max(longest, elapsed)
        except RuntimeError:
            errors += 1
    return {"attempts": attempts, "ended": ended, "errors": errors, "share": round(ended / attempts, 4),
            "longest_s": round(longest, 3), "limit_s": MAX_ATTEMPT_S}


def run() -> dict:
    rng = random.Random(41)
    report = {"gravity_share": GRAVITY_SHARE, "launch_speed_share": LAUNCH_SPEED_SHARE,
              "playable": playable(rng), "exact": exact(), "bounded": bounded(rng), "pass": PASS}
    report["passed"] = (report["playable"]["share"] >= PASS["playable"]
                        and report["exact"]["max_error_px"] <= PASS["exact_px"]
                        and report["bounded"]["share"] >= PASS["bounded"] and report["bounded"]["errors"] == 0)
    return report


if __name__ == "__main__":
    result = run()
    print(json.dumps(result, indent=2))
    if not result["passed"]:
        raise SystemExit(1)
