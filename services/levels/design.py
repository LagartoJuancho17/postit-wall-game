"""Play a level with simulated post-its, and look for bumpers that win it.

    python -m services.levels.design <level-id> [--bumpers N]

Used to design levels.json: prints the bumper placements that win from the
zone centers, best first, ready to paste as the level's "solution". The
solvable eval (services/levels/evals/solvable.py) replays those solutions.

The game drops the ball under the start note (services/physics drop), so a
search follows the ball: it tries a bumper on the path of the current
throw, at several heights and tilts, keeps the throws that end nearest the
goal, and goes one bumper deeper from those (a beam search).
"""

from __future__ import annotations

import argparse
import json
import math
import random
from dataclasses import dataclass

from contracts.model import Board, Point, Polygon
from services.physics import GRAVITY_SHARE, GameStatus, drop, step

from . import Level, level_by_id, level_check, level_geometry, level_problem, with_obstacles

FIELD = (1920, 1080)         # the reference projector
NOTE_SHARE = 0.07            # a 76 mm post-it on a wall ~1.1 m tall, as a share of the short side
DT_S = 1 / 120               # the server's step
MAX_S = 8.0                  # a throw that has not ended by then counts as lost
# How far off a hand sticks a note (robustness): px and degrees for bumpers,
# a share of the zone's size for start and goal around the zone center.
JITTER_PX = 8.0
JITTER_DEG = 4.0
ZONE_JITTER = 0.05
EDGE_PX = 20.0           # a solution's notes stay this far inside the field


def square(cx: float, cy: float, side: float, degrees: float = 0.0) -> Polygon:
    angle = math.radians(degrees)
    cos, sin = math.cos(angle), math.sin(angle)
    half = side / 2
    corners = ((-half, -half), (half, -half), (half, half), (-half, half))
    return Polygon(tuple(Point(cx + x * cos - y * sin, cy + x * sin + y * cos) for x, y in corners))


def zone_center(zone) -> tuple[float, float]:
    x0, y0, x1, y1 = zone
    return (x0 + x1) / 2, (y0 + y1) / 2


@dataclass(frozen=True)
class Throw:
    status: str
    ended_by: str
    bounces: int
    path: tuple[Point, ...]       # every 4th step's position
    closest_px: float             # nearest the ball came to the goal note's center
    problem: str | None


def notes_for(level: Level, bumpers, size=FIELD, start=None, goal=None, side=None):
    """Post-its as the camera would give them, in `size` px: start and goal at
    their zone centers (or `start` / `goal`, shares of the field), bumpers as
    {"center": [x, y], "angle": degrees}."""
    width, height = size
    side = side if side is not None else NOTE_SHARE * min(width, height)
    sx, sy = start if start is not None else zone_center(level.start_zone or (0.1, 0.1, 0.2, 0.2))
    gx, gy = goal if goal is not None else zone_center(level.goal_zone or (0.8, 0.8, 0.9, 0.9))
    return {"start": (square(sx * width, sy * height, side),), "goal": (square(gx * width, gy * height, side),),
            "bumper": tuple(square(b["center"][0] * width, b["center"][1] * height, side, b.get("angle", 0.0))
                            for b in bumpers),
            "marker": ()}


def play(level: Level, bumpers, size=FIELD, **where) -> Throw:
    width, height = size
    notes = notes_for(level, bumpers, size, **where)
    geometry = level_geometry(level, width, height)
    problem = level_problem(level_check(level, geometry, notes))
    field = Polygon((Point(0, 0), Point(width, 0), Point(width, height), Point(0, height)))
    board = with_obstacles(Board(field, notes["start"][0], notes["goal"][0], notes["bumper"], ()), geometry)
    goal = notes["goal"][0].points
    gx = sum(p.x for p in goal) / 4
    gy = sum(p.y for p in goal) / 4
    try:
        state = drop(board, min(width, height) * GRAVITY_SHARE)
    except ValueError as exc:
        return Throw("invalid", str(exc), 0, (), math.inf, problem)
    path, closest = [state.position], math.inf
    for index in range(int(MAX_S / DT_S)):
        state = step(board, state, DT_S)
        closest = min(closest, math.hypot(state.position.x - gx, state.position.y - gy))
        if index % 4 == 0:
            path.append(state.position)
        if state.status != GameStatus.RUNNING:
            break
    return Throw(state.status.value, state.ended_by, state.bounce_count, tuple(path), closest, problem)


def robustness(level: Level, bumpers, trials: int = 60, seed: int = 0, size=FIELD) -> float:
    """Share of throws that still win with the notes stuck by hand: each
    bumper JITTER_PX off and JITTER_DEG turned, start and goal anywhere
    within ZONE_JITTER of their zone centers (shares of the zone's size)."""
    rng = random.Random(seed)
    width, height = size
    wins = 0
    for _ in range(trials):
        moved = [{"center": [b["center"][0] + rng.uniform(-JITTER_PX, JITTER_PX) / width,
                             b["center"][1] + rng.uniform(-JITTER_PX, JITTER_PX) / height],
                  "angle": b.get("angle", 0.0) + rng.uniform(-JITTER_DEG, JITTER_DEG)} for b in bumpers]

        def near(zone):
            x0, y0, x1, y1 = zone
            cx, cy = zone_center(zone)
            return (cx + rng.uniform(-ZONE_JITTER, ZONE_JITTER) * (x1 - x0),
                    cy + rng.uniform(-ZONE_JITTER, ZONE_JITTER) * (y1 - y0))
        wins += play(level, moved, size, start=near(level.start_zone), goal=near(level.goal_zone)).status == "won"
    return wins / trials


def search(level: Level, depth: int, beam: int = 6, size=FIELD) -> list[list[dict]]:
    """Bumper sets of up to `depth` notes that win from the zone centers."""
    width, height = size
    wins: list[list[dict]] = []
    frontier: list[tuple[float, list[dict], Throw]] = [(0.0, [], play(level, [], size))]
    for _ in range(depth):
        scored = []
        for _, placed, throw in frontier:
            for point in throw.path[2::2]:
                for below in (18, 32, 46):
                    for angle in (-55, -45, -35, -25, -15, 15, 25, 35, 45, 55):
                        center = [round(point.x / width, 4), round((point.y + below) / height, 4)]
                        half = NOTE_SHARE * min(width, height) * 0.72   # a turned note's half diagonal, roughly
                        if not (EDGE_PX + half <= point.x <= width - EDGE_PX - half
                                and point.y + below <= height - EDGE_PX - half):
                            continue   # a note hanging off the wall is no solution
                        bumpers = [*placed, {"center": center, "angle": angle}]
                        result = play(level, bumpers, size)
                        if result.problem is not None or result.status == "invalid":
                            continue
                        if result.status == "won":
                            wins.append(bumpers)
                        scored.append((result.closest_px, bumpers, result))
        scored.sort(key=lambda item: item[0])
        frontier = scored[:beam]
        if wins:
            break
    return wins


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("level")
    parser.add_argument("--bumpers", type=int, default=None)
    args = parser.parse_args()
    level = level_by_id(args.level)
    if level is None:
        parser.error(f"no hay nivel {args.level}")
    bare = play(level, [])
    print(json.dumps({"bare": [bare.status, bare.ended_by, round(bare.closest_px, 1)]}))
    depth = args.bumpers if args.bumpers is not None else (level.bumpers_max or 0)
    wins = search(level, depth) if depth else []
    ranked = sorted(((robustness(level, w, trials=30), w) for w in wins), key=lambda item: -item[0])
    print(json.dumps({"wins": len(wins), "best": ranked[:5]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
