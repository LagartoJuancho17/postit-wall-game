"""Periodic eval: the cover gesture (contract 3.1) against a noisy live detection.

Run from the project root: ``python -m services.web.evals.handsfree``.
Deterministic (seeded). The panel sends ~4 frames a second, unevenly
(0.20-0.33 s apart); in each one the detector misses a note with
probability MISS and its center wobbles by WOBBLE_PX. On that stream:

- covers: a hand over the start for 1.2-3 s (the wall says "un segundo"),
  then lifted: it must play once, on the first frame that sees the start
  again (or the next one); shorter covers (0.6-1 s) are reported;
- idle: an hour of a wall nobody touches, with the flicker: no play;
- walker: somebody in front of the wall (start and goal hidden): no play;
- moved: the start taken off and stuck 60-200 px away within a second: no play;
- removed: the start taken off for 5-30 s and stuck back: no play;
- swipe: a hand passing over the start in 0.1-0.3 s: reported, may play;
- ghost: the projector paints the start color on the hand, so the camera
  first sees a "start" that shrinks frame by frame (the closed loop saw it
  for 1.9 s): how long from the hand to "armed".
"""

from __future__ import annotations

import json
import math
import random

from contracts.model import Point
from services.web.backend.handsfree import COVER_DARK_FRAMES, COVER_MAX_S, COVER_MIN_S, cover_begin, cover_update

CAMERA = (1280, 720)
START = Point(400.0, 300.0)
MISS = 0.05
WOBBLE_PX = 2.0
AREA = 4000.0          # the start's area, camera px
AREA_WOBBLE = 0.04     # the detector's outline makes it wobble this much
RUNS = 400
PASS = {"cover_fires": 0.97, "idle_fires_per_hour": 0.0, "walker_fires": 0.0, "moved_fires": 0.0,
        "removed_fires": 0.0, "swipe_fires_max": 0.5,
        # Without the shrink check the ghost delays "armed" to 3.73 s (p95, measured 2026-10-27).
        "ghost_armed_p95_s": 2.0}


class Wall:
    """A detection stream: call `frame(start_at, goal_visible)` per frame."""

    def __init__(self, seed: int) -> None:
        self.rng = random.Random(seed)
        self.now = 0.0
        self.cover = cover_begin()
        self.fires: list[float] = []

    def frame(self, start_at: Point | None, goal_visible: bool = True, area_share: float = 1.0) -> None:
        self.now += self.rng.uniform(0.20, 0.33)
        seen = start_at
        if seen is not None and self.rng.random() < MISS:
            seen = None   # the detector missed it this frame
        note = None
        if seen is not None:
            note = (Point(seen.x + self.rng.gauss(0, WOBBLE_PX), seen.y + self.rng.gauss(0, WOBBLE_PX)),
                    AREA * area_share * (1 + self.rng.uniform(-AREA_WOBBLE, AREA_WOBBLE)))
        goal = goal_visible and self.rng.random() >= MISS
        self.cover, event = cover_update(self.cover, note, goal, self.now, CAMERA)
        self.last_event = event
        if event == "fire":
            self.fires.append(self.now)

    def hold(self, seconds: float, start_at: Point | None, goal_visible: bool = True) -> None:
        end = self.now + seconds
        while self.now < end:
            self.frame(start_at, goal_visible)


def _covers(low: float = 1.2, high: float = 3.0) -> float:
    fired = 0
    for seed in range(RUNS):
        wall = Wall(seed)
        wall.hold(2.0, START)
        wall.hold(wall.rng.uniform(low, high), None)
        lifted = wall.now
        wall.hold(1.0, START)
        fired += any(lifted < t <= lifted + 0.7 for t in wall.fires) and len(wall.fires) == 1
    return fired / RUNS


def _idle_per_hour() -> float:
    wall = Wall(1)
    wall.hold(3600.0, START)
    return float(len(wall.fires))


def _share(seed_base: int, scene) -> float:
    fired = 0
    for seed in range(RUNS):
        wall = Wall(seed_base + seed)
        wall.hold(2.0, START)
        scene(wall)
        fired += bool(wall.fires)
    return fired / RUNS


def _walker(wall: Wall) -> None:
    wall.hold(wall.rng.uniform(0.5, 3.0), None, goal_visible=False)
    wall.hold(1.0, START)


def _moved(wall: Wall) -> None:
    wall.hold(wall.rng.uniform(0.4, 1.0), None)
    angle = wall.rng.uniform(0, 2 * math.pi)
    reach = wall.rng.uniform(60, 200)
    wall.hold(1.5, Point(START.x + reach * math.cos(angle), START.y + reach * math.sin(angle)))


def _removed(wall: Wall) -> None:
    wall.hold(wall.rng.uniform(5.0, 30.0), None)
    wall.hold(1.0, START)


def _swipe(wall: Wall) -> None:
    wall.hold(wall.rng.uniform(0.1, 0.3), None)
    wall.hold(1.0, START)


def _ghost_armed_p95() -> float:
    """From the hand to "armed" when the ghost shrinks by 8% of the side a frame."""
    delays = []
    for seed in range(RUNS):
        wall = Wall(50_000 + seed)
        wall.hold(2.0, START)
        began = wall.now
        side = 1.0
        armed = None
        while armed is None and wall.now - began < COVER_MAX_S:
            side -= 0.08
            wall.frame(START if side > 0.3 else None, area_share=max(0.0, side) ** 2)
            if wall.last_event == "armed":
                armed = wall.now - began
        delays.append(armed if armed is not None else math.inf)
    delays.sort()
    return delays[int(0.95 * (len(delays) - 1))]


def run() -> dict:
    report = {
        "setup": {"camera": list(CAMERA), "miss": MISS, "wobble_px": WOBBLE_PX, "runs": RUNS,
                  "cover_min_s": COVER_MIN_S, "cover_max_s": COVER_MAX_S,
                  "cover_dark_frames": COVER_DARK_FRAMES},
        "cover_fires": round(_covers(), 4),
        "short_cover_fires": round(_covers(0.6, 1.0), 4),
        "idle_fires_per_hour": _idle_per_hour(),
        "walker_fires": round(_share(10_000, _walker), 4),
        "moved_fires": round(_share(20_000, _moved), 4),
        "removed_fires": round(_share(30_000, _removed), 4),
        "swipe_fires": round(_share(40_000, _swipe), 4),
        "ghost_armed_p95_s": round(_ghost_armed_p95(), 3),
        "pass": PASS,
    }
    report["passed"] = (report["cover_fires"] >= PASS["cover_fires"]
                        and report["idle_fires_per_hour"] <= PASS["idle_fires_per_hour"]
                        and report["walker_fires"] <= PASS["walker_fires"]
                        and report["moved_fires"] <= PASS["moved_fires"]
                        and report["removed_fires"] <= PASS["removed_fires"]
                        and report["swipe_fires"] <= PASS["swipe_fires_max"]
                        and report["ghost_armed_p95_s"] <= PASS["ghost_armed_p95_s"])
    return report


if __name__ == "__main__":
    result = run()
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if not result["passed"]:
        raise SystemExit(1)
