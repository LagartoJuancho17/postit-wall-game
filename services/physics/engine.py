"""Swept-circle collisions with convex post-it polygons.

The ball crosses each edge or rounded vertex at a calculated time. This
avoids frame-dependent tunneling through thin targets and resolves multiple
rebounds within a single frame.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
import math

from contracts.model import Board, Point, Polygon


BALL_RADIUS_PX = 10.0
_EPS = 1e-9
_POSITION_NUDGE_PX = 1e-7
_MAX_EVENTS_PER_STEP = 1024


class GameStatus(str, Enum):
    READY = "ready"
    RUNNING = "running"
    WON = "won"
    LOST = "lost"


@dataclass(frozen=True)
class GameState:
    position: Point
    velocity: Point
    elapsed_s: float
    bounce_count: int
    status: GameStatus


@dataclass(frozen=True)
class _Hit:
    time_s: float
    normal: Point
    kind: str


def _dot(a: Point, b: Point) -> float:
    return a.x * b.x + a.y * b.y


def _sub(a: Point, b: Point) -> Point:
    return Point(a.x - b.x, a.y - b.y)


def _add_scaled(a: Point, v: Point, t: float) -> Point:
    return Point(a.x + v.x * t, a.y + v.y * t)


def _edges(polygon: Polygon):
    points = polygon.points
    for i, a in enumerate(points):
        yield a, points[(i + 1) % len(points)]


def _area2(polygon: Polygon) -> float:
    return sum(a.x * b.y - b.x * a.y for a, b in _edges(polygon))


def _validate_polygon(polygon: Polygon, label: str) -> None:
    points = polygon.points
    if len(points) < 3 or any(not math.isfinite(v) for p in points for v in (p.x, p.y)):
        raise ValueError(f"{label} must have at least three finite points")
    area = _area2(polygon)
    if abs(area) < _EPS:
        raise ValueError(f"{label} has zero area")
    sign = 1 if area > 0 else -1
    for i, a in enumerate(points):
        b = points[(i + 1) % len(points)]
        c = points[(i + 2) % len(points)]
        cross = (b.x - a.x) * (c.y - b.y) - (b.y - a.y) * (c.x - b.x)
        if cross * sign < -_EPS:
            raise ValueError(f"{label} must be convex and ordered")
        if (b.x - a.x) ** 2 + (b.y - a.y) ** 2 < _EPS:
            raise ValueError(f"{label} has a repeated point")


def _validate_board(board: Board) -> None:
    for label, polygon in (
        ("playfield", board.playfield),
        ("start", board.start),
        ("goal", board.goal),
        *((f"bumper {i}", polygon) for i, polygon in enumerate(board.bumpers)),
    ):
        _validate_polygon(polygon, label)


def _centroid(polygon: Polygon) -> Point:
    area2 = _area2(polygon)
    cx = sum((a.x + b.x) * (a.x * b.y - b.x * a.y) for a, b in _edges(polygon))
    cy = sum((a.y + b.y) * (a.x * b.y - b.x * a.y) for a, b in _edges(polygon))
    return Point(cx / (3 * area2), cy / (3 * area2))


def _edge_normals(polygon: Polygon):
    orientation = 1 if _area2(polygon) > 0 else -1
    for a, b in _edges(polygon):
        dx, dy = b.x - a.x, b.y - a.y
        length = math.hypot(dx, dy)
        yield a, b, Point(orientation * dy / length, -orientation * dx / length)


def _fits_playfield(position: Point, playfield: Polygon) -> bool:
    return all(
        _dot(_sub(position, a), normal) <= -BALL_RADIUS_PX + _EPS
        for a, _, normal in _edge_normals(playfield)
    )


def _distance_squared_to_segment(p: Point, a: Point, b: Point) -> float:
    edge = _sub(b, a)
    parameter = max(0.0, min(1.0, _dot(_sub(p, a), edge) / _dot(edge, edge)))
    near = _add_scaled(a, edge, parameter)
    delta = _sub(p, near)
    return _dot(delta, delta)


def _overlaps_polygon(position: Point, polygon: Polygon) -> bool:
    if all(_dot(_sub(position, a), n) <= _EPS for a, _, n in _edge_normals(polygon)):
        return True
    radius2 = BALL_RADIUS_PX * BALL_RADIUS_PX
    return any(_distance_squared_to_segment(position, a, b) <= radius2 + _EPS for a, b in _edges(polygon))


def ready_state(board: Board) -> GameState:
    """Put a motionless ball at the area centroid of the yellow start note."""
    _validate_board(board)
    position = _centroid(board.start)
    if not _fits_playfield(position, board.playfield):
        raise ValueError("start centroid must fit inside playfield with ball radius")
    if _overlaps_polygon(position, board.goal):
        raise ValueError("start overlaps goal")
    if any(_overlaps_polygon(position, obstacle) for obstacle in board.bumpers):
        raise ValueError("start overlaps a bumper")
    return GameState(position, Point(0.0, 0.0), 0.0, 0, GameStatus.READY)


def launch(board: Board, angle_degrees: float, speed_px_s: float) -> GameState:
    """Launch from yellow. An angle of 0° aims right; 90° aims down."""
    if not math.isfinite(angle_degrees) or not math.isfinite(speed_px_s) or speed_px_s <= 0:
        raise ValueError("angle must be finite and speed_px_s must be positive and finite")
    state = ready_state(board)
    angle = math.radians(angle_degrees)
    return replace(
        state,
        velocity=Point(speed_px_s * math.cos(angle), speed_px_s * math.sin(angle)),
        status=GameStatus.RUNNING,
    )


def _swept_polygon_hit(position: Point, velocity: Point, polygon: Polygon) -> _Hit | None:
    """First impact against the radius-expanded boundary of a convex polygon."""
    candidates: list[_Hit] = []
    for a, b, normal in _edge_normals(polygon):
        distance = _dot(_sub(position, a), normal)
        approach = _dot(velocity, normal)
        if distance >= BALL_RADIUS_PX - _EPS and approach < -_EPS:
            t = (BALL_RADIUS_PX - distance) / approach
            if t >= -_EPS:
                q = _add_scaled(position, velocity, max(0.0, t))
                edge = _sub(b, a)
                u = _dot(_sub(q, a), edge) / _dot(edge, edge)
                if -_EPS <= u <= 1.0 + _EPS:
                    candidates.append(_Hit(max(0.0, t), normal, "polygon"))

    speed2 = _dot(velocity, velocity)
    if speed2 > _EPS:
        for vertex in polygon.points:
            relative = _sub(position, vertex)
            c = _dot(relative, relative) - BALL_RADIUS_PX**2
            if c < -_EPS:
                continue
            b = 2 * _dot(relative, velocity)
            discriminant = b * b - 4 * speed2 * c
            if discriminant < 0:
                continue
            t = (-b - math.sqrt(max(0.0, discriminant))) / (2 * speed2)
            if t < -_EPS:
                continue
            q = _add_scaled(position, velocity, max(0.0, t))
            radial = _sub(q, vertex)
            radial_length = math.hypot(radial.x, radial.y)
            if radial_length > _EPS:
                normal = Point(radial.x / radial_length, radial.y / radial_length)
                if _dot(velocity, normal) < -_EPS:
                    candidates.append(_Hit(max(0.0, t), normal, "polygon"))
    return min(candidates, key=lambda hit: hit.time_s) if candidates else None


def _playfield_exit(position: Point, velocity: Point, playfield: Polygon) -> _Hit | None:
    candidates = []
    for a, _, normal in _edge_normals(playfield):
        distance = _dot(_sub(position, a), normal)
        outward_speed = _dot(velocity, normal)
        if outward_speed > _EPS:
            t = (-BALL_RADIUS_PX - distance) / outward_speed
            if t >= -_EPS:
                candidates.append(_Hit(max(0.0, t), normal, "exit"))
    return min(candidates, key=lambda hit: hit.time_s) if candidates else None


def step(board: Board, state: GameState, dt_s: float) -> GameState:
    """Advance a fixed board by ``dt_s``; terminal states remain unchanged.

    Outcomes happen at the exact collision time inside the frame. Goal overlap
    wins; crossing the playable inset loses. Contact with a green note reflects
    velocity using the impact normal and increments ``bounce_count``.
    """
    if not math.isfinite(dt_s) or dt_s < 0:
        raise ValueError("dt_s must be nonnegative and finite")
    if state.status != GameStatus.RUNNING or dt_s == 0:
        return state
    if not _fits_playfield(state.position, board.playfield):
        return replace(state, status=GameStatus.LOST)
    if _overlaps_polygon(state.position, board.goal):
        return replace(state, status=GameStatus.WON)

    position, velocity = state.position, state.velocity
    elapsed, bounce_count, remaining = state.elapsed_s, state.bounce_count, dt_s
    for _ in range(_MAX_EVENTS_PER_STEP):
        events: list[_Hit] = []
        goal = _swept_polygon_hit(position, velocity, board.goal)
        if goal is not None:
            events.append(replace(goal, kind="goal"))
        for obstacle in board.bumpers:
            hit = _swept_polygon_hit(position, velocity, obstacle)
            if hit is not None:
                events.append(replace(hit, kind="bumper"))
        exit_hit = _playfield_exit(position, velocity, board.playfield)
        if exit_hit is not None:
            events.append(exit_hit)
        if not events:
            return GameState(_add_scaled(position, velocity, remaining), velocity, elapsed + remaining, bounce_count, GameStatus.RUNNING)

        # A simultaneous goal contact takes priority over a bumper or boundary.
        priority = {"goal": 0, "exit": 1, "bumper": 2}
        hit = min(events, key=lambda candidate: (candidate.time_s, priority[candidate.kind]))
        if hit.time_s > remaining + _EPS:
            return GameState(_add_scaled(position, velocity, remaining), velocity, elapsed + remaining, bounce_count, GameStatus.RUNNING)
        travel = min(hit.time_s, remaining)
        position = _add_scaled(position, velocity, travel)
        elapsed += travel
        remaining -= travel
        if hit.kind == "goal":
            return GameState(position, velocity, elapsed, bounce_count, GameStatus.WON)
        if hit.kind == "exit":
            return GameState(position, velocity, elapsed, bounce_count, GameStatus.LOST)
        normal = hit.normal
        velocity = _sub(velocity, Point(2 * _dot(velocity, normal) * normal.x, 2 * _dot(velocity, normal) * normal.y))
        position = _add_scaled(position, normal, _POSITION_NUDGE_PX)
        bounce_count += 1
        if remaining <= _EPS:
            return GameState(_add_scaled(position, velocity, remaining), velocity, elapsed + remaining, bounce_count, GameStatus.RUNNING)
    raise RuntimeError(f"more than {_MAX_EVENTS_PER_STEP} collisions in one step; reduce dt_s or speed")
