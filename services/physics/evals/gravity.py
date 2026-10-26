"""Periodic eval: the web game's dropped ball under gravity (GRAVITY_SHARE).

Run from the project root: ``python -m services.physics.evals.gravity``.
Deterministic (seeded). The ball is never aimed: it starts at rest under
the start note (``drop``) and falls. It measures what that change is for:

- exact: a drop in open space follows y0 + g t^2 / 2 whatever the step;
- straight: a goal under the start is reached by the bare drop;
- steerable: with the goal off to the side, one tilted post-it (a ramp the
  player can stick on the wall; seeded random placements) guides the ball to
  it, on random boards; every drop tried is also a stress case that must not
  raise (it found the V wedge and the rounded-corner crawl, 2026-10-26);
- bounded: with bumpers anywhere, every drop ends (won, lost, or the
  MAX_ATTEMPT_S limit) without an error.
"""

from __future__ import annotations

import json
import math
import random

from contracts.model import Board, Point, Polygon
from services.physics import GRAVITY_SHARE, MAX_ATTEMPT_S, GameStatus, drop, drop_point, drop_ready_state, step

DT = 1 / 120
PASS = {"exact_px": 0.05, "straight": 1.0, "steerable": 0.95, "bounded": 1.0}
RAMP_TRIES = 300


def rect(cx, cy, w, h, degrees=0.0) -> Polygon:
    c, s = math.cos(math.radians(degrees)), math.sin(math.radians(degrees))
    corners = ((-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2))
    return Polygon(tuple(Point(cx + x * c - y * s, cy + x * s + y * c) for x, y in corners))


def field(width, height, start, goal, bumpers=()) -> Board:
    return Board(Polygon((Point(0, 0), Point(width, 0), Point(width, height), Point(0, height))),
                 start, goal, tuple(bumpers), ())


def play(board: Board, side: float) -> tuple[GameStatus, float]:
    state = drop(board, GRAVITY_SHARE * side)
    while state.status == GameStatus.RUNNING:
        state = step(board, state, DT)
    return state.status, state.elapsed_s


def playable(board: Board) -> bool:
    try:
        drop_ready_state(board)
    except ValueError:
        return False
    return True


def exact() -> dict:
    worst = 0.0
    for width, height in ((1280, 720), (1920, 1080), (960, 720)):
        side = min(width, height)
        g = GRAVITY_SHARE * side
        board = field(width, height * 20, rect(0.5 * width, 0.1 * height, 0.06 * side, 0.06 * side),
                      rect(10, height * 19, 10, 10))   # open space below
        y0 = drop_point(board).y
        for dt in (1 / 120, 1 / 60, 1 / 30):
            state = drop(board, g)
            for k in range(1, round(0.6 / dt) + 1):
                state = step(board, state, dt)
                t = k * dt
                worst = max(worst, abs(state.position.y - (y0 + 0.5 * g * t * t)),
                            abs(state.position.x - 0.5 * width))
    return {"max_error_px": worst}


def straight(rng: random.Random) -> dict:
    boards = wins = 0
    for _ in range(60):
        width, height = rng.choice([(1280, 720), (1920, 1080), (960, 720)])
        side = min(width, height)
        note = 0.08 * side
        x = rng.uniform(0.1, 0.9) * width
        board = field(width, height, rect(x, rng.uniform(0.1, 0.4) * height, note, note),
                      rect(x + rng.uniform(-0.3, 0.3) * note, rng.uniform(0.6, 0.9) * height, note, note))
        if not playable(board):
            continue
        boards += 1
        wins += play(board, side)[0] == GameStatus.WON
    return {"boards": boards, "won": wins, "share": round(wins / boards, 4)}


def steerable(rng: random.Random) -> dict:
    """Goal off to the side: does one tilted post-it (a ramp) get the ball there?

    Up to RAMP_TRIES seeded random ramps per board (anywhere between start and
    goal, 1-5 notes long, 5-60 degrees toward the goal). Every drop tried also
    counts for `stress`: none may raise.
    """
    boards = winnable = drops = errors = 0
    tries = []
    for _ in range(40):
        width, height = rng.choice([(1280, 720), (1920, 1080), (960, 720)])
        side = min(width, height)
        note = 0.08 * side
        sx, sy = rng.uniform(0.15, 0.85) * width, rng.uniform(0.1, 0.3) * height
        gx = min(max(sx + rng.choice((-1, 1)) * rng.uniform(1.5, 4.0) * note, 0.08 * width), 0.92 * width)
        gy = rng.uniform(0.7, 0.9) * height
        start, goal = rect(sx, sy, note, note), rect(gx, gy, note, note)
        if not playable(field(width, height, start, goal)):
            continue
        boards += 1
        for tried in range(1, RAMP_TRIES + 1):
            ramp = rect(sx + rng.uniform(-0.3, 1.0) * (gx - sx), rng.uniform(sy + note, gy - note),
                        rng.uniform(1, 5) * note, 0.5 * note, rng.uniform(5, 60) * (1 if gx > sx else -1))
            board = field(width, height, start, goal, (ramp,))
            if not playable(board):
                continue
            drops += 1
            try:
                won = play(board, side)[0] == GameStatus.WON
            except RuntimeError:
                errors += 1
                continue
            if won:
                winnable += 1
                tries.append(tried)
                break
    return {"boards": boards, "winnable": winnable, "share": round(winnable / boards, 4),
            "ramps_tried_median": sorted(tries)[len(tries) // 2] if tries else None,
            "stress": {"drops": drops, "errors": errors}}


def bounded(rng: random.Random) -> dict:
    attempts = ended = errors = 0
    longest = 0.0
    for _ in range(150):
        width, height = 1280, 720
        side = min(width, height)
        start = rect(rng.uniform(100, 1180), rng.uniform(60, 250), 50, 50)
        goal = rect(rng.uniform(100, 1180), rng.uniform(400, 680), 50, 50)
        bumpers = [rect(rng.uniform(100, 1180), rng.uniform(300, 700), rng.uniform(40, 300), rng.uniform(20, 60),
                        rng.uniform(-40, 40)) for _ in range(rng.randrange(1, 6))]
        board = field(width, height, start, goal, bumpers)
        if not playable(board):
            continue
        attempts += 1
        try:
            status, elapsed = play(board, side)
            ended += status in (GameStatus.WON, GameStatus.LOST)
            longest = max(longest, elapsed)
        except RuntimeError:
            errors += 1
    return {"attempts": attempts, "ended": ended, "errors": errors, "share": round(ended / attempts, 4),
            "longest_s": round(longest, 3), "limit_s": MAX_ATTEMPT_S}


def run() -> dict:
    rng = random.Random(41)
    report = {"gravity_share": GRAVITY_SHARE, "exact": exact(), "straight": straight(rng),
              "steerable": steerable(rng), "bounded": bounded(rng), "pass": PASS}
    report["passed"] = (report["exact"]["max_error_px"] <= PASS["exact_px"]
                        and report["straight"]["share"] >= PASS["straight"]
                        and report["steerable"]["share"] >= PASS["steerable"]
                        and report["steerable"]["stress"]["errors"] == 0
                        and report["bounded"]["share"] >= PASS["bounded"] and report["bounded"]["errors"] == 0)
    return report


if __name__ == "__main__":
    result = run()
    print(json.dumps(result, indent=2))
    if not result["passed"]:
        raise SystemExit(1)
