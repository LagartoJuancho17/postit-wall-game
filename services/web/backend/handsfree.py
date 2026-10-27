"""Hands-free play (contract 3.1): the countdown and the cover gesture.

Tobias, 2026-10-27: "soltar sin teclado". Whoever plays stands at the wall,
not at the Mac. Covering the start note with a hand and taking it away
starts the countdown; so do Space and a Bluetooth button in the panel, through
the same `play` action.

The gesture is read from the live detection (~4 frames a second): the start
note disappears while the goal stays in view, for COVER_MIN_S to COVER_MAX_S,
then comes back where it was. A few frames of flicker are shorter than
COVER_MIN_S (at 0.4 s, two missed frames in a row made 30 false drops an
hour on a wall nobody touched), a note moved elsewhere comes back too far, a
note taken away never comes back in time, and somebody walking in front of
the wall hides the goal too, for COVER_DARK_FRAMES frames in a row (one
missed goal frame is flicker). `services/web/evals/handsfree.py` measures
all of them.

Pure functions on a dict, so the browser port (services/browser/handsfree.mjs)
gets the same answers from the same frames.
"""

from __future__ import annotations

import math

from contracts.model import Point, Polygon

COUNTDOWN_S = 2.4         # 3, 2, 1: three beats before the drop
COUNTDOWN_BEATS = 3
COVER_MIN_S = 1.0         # hidden at least this long: a hand, not a few frames of flicker
COVER_MAX_S = 4.0         # hidden longer: the note was taken away
COVER_DARK_FRAMES = 2     # the goal missing this many frames in a row: somebody in front of the wall
COVER_NEAR_SHARE = 0.05   # of the camera's short side: back "where it was"
# The projector keeps lighting the start's place in the start color, on the
# hand too, and the camera reads that as the start until the ghost dies (it
# shrinks by the fill inset every frame, contract 2.1): 1.9 s in the closed
# loop. A start under COVER_SHRINK of its usual area is that ghost: covered.
COVER_SHRINK = 0.85


def cover_begin() -> dict:
    return {"seen": None, "area": None, "hidden_since": None, "armed": False, "dark": 0, "spoiled": False}


def start_note(notes: dict[str, tuple[Polygon, ...]]) -> tuple[Point, float] | None:
    """The one start note's vertex mean and area (camera px); None without exactly one."""
    starts = notes.get("start", ())
    if len(starts) != 1:
        return None
    points = starts[0].points
    sx = sy = twice = 0.0
    for k, point in enumerate(points):
        nxt = points[(k + 1) % len(points)]
        sx += point.x
        sy += point.y
        twice += point.x * nxt.y - nxt.x * point.y
    return Point(sx / len(points), sy / len(points)), abs(twice) / 2


def cover_update(cover: dict, start: tuple[Point, float] | None, goal_visible: bool, now: float,
                 camera_size: tuple[int, int]) -> tuple[dict, str | None]:
    """One live frame. `start` is start_note's (center, area). Returns the new
    state and its event: "armed" (hidden long enough: taking the hand away
    now plays), "fire" (back where it was) or None."""
    seen, hidden_since = cover["seen"], cover["hidden_since"]
    area = None
    if start is not None:
        start, area = start
        if cover["area"] is not None and area < COVER_SHRINK * cover["area"]:
            start = None   # the projector's ghost on the hand, not the paper
    if start is not None:
        fire = False
        if seen is not None and hidden_since is not None and not cover["spoiled"]:
            hidden = now - hidden_since
            dx, dy = start.x - seen.x, start.y - seen.y
            near = math.sqrt(dx * dx + dy * dy) <= COVER_NEAR_SHARE * min(camera_size)
            fire = near and COVER_MIN_S <= hidden <= COVER_MAX_S
        # The usual area follows the paper slowly (the detector's outline wobbles).
        usual = area if cover["area"] is None or fire or hidden_since is not None else 0.7 * cover["area"] + 0.3 * area
        return {**cover_begin(), "seen": start, "area": usual}, "fire" if fire else None
    if seen is None:
        return cover, None
    if hidden_since is None:
        cover = {**cover, "hidden_since": now, "dark": 0, "spoiled": False}
        hidden_since = now
    dark = 0 if goal_visible else cover["dark"] + 1
    spoiled = cover["spoiled"] or dark >= COVER_DARK_FRAMES   # more than the start went dark
    cover = {**cover, "dark": dark, "spoiled": spoiled, "armed": cover["armed"] and not spoiled}
    hidden = now - hidden_since
    if hidden > COVER_MAX_S:
        return cover_begin(), None   # taken away: the next one seen starts over (its own size too)
    if not cover["armed"] and not spoiled and hidden >= COVER_MIN_S:
        return {**cover, "armed": True}, "armed"
    return cover, None
