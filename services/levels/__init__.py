"""Levels (contract 3.2): where the start and goal notes go, how many bumpers
a level allows, and the obstacles it projects (walls, spikes, portals).

Tobias, 2026-10-27: "Haya un area definida para poner el bloque de inicio,
el juego solo puede comenzar si se coloca el post it sobre ese AREA. Lo
mismo que para la llegada. Ademas que sea por niveles".

The levels live in levels.json, in shares of the playfield (x of its width,
y of its height), so one level fits any projector or camera size. This
module turns a level into polygons of one space (`level_geometry`), adds
its obstacles to a board (`with_obstacles`) and says whether the notes on
the wall obey it (`level_check`, `level_problem`). Pure functions with the
same operations as services/browser/levels.mjs, so both backends agree.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from contracts.model import Board, Point, Polygon, Portal

LEVELS_PATH = Path(__file__).with_name("levels.json")
FREE_LEVEL = "libre"
# A portal's exit stays clear of every note by the ball's radius and this much.
BALL_RADIUS_PX = 10.0
PORTAL_CLEAR_PX = 4.0
FACINGS = ("up", "down", "left", "right")
COVER_NAMES = {"wall": "una pared", "spikes": "los pinchos", "portal": "un portal"}


class LevelProblem(ValueError):
    """The notes do not obey the level; the text is for the operator."""


@dataclass(frozen=True)
class Level:
    id: str
    name: str
    hint: str
    start_zone: tuple[float, float, float, float] | None
    goal_zone: tuple[float, float, float, float] | None
    bumpers_max: int | None
    walls: tuple[dict[str, Any], ...]
    spikes: tuple[dict[str, Any], ...]
    portals: tuple[dict[str, Any], ...]
    solution: tuple[dict[str, Any], ...]   # bumpers that win it (the solvable eval)


@dataclass(frozen=True)
class LevelGeometry:
    """A level in one space's pixels."""
    width: float
    height: float
    start_zone: Polygon | None
    goal_zone: Polygon | None
    walls: tuple[Polygon, ...]
    spikes: tuple[Polygon, ...]
    facings: tuple[str, ...]          # where each spike strip's teeth point (drawing only)
    portals: tuple[Portal, ...]


def _rect(values, label: str) -> tuple[float, float, float, float]:
    if (not isinstance(values, list) or len(values) != 4
            or any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in values)):
        raise ValueError(f"{label}: se esperaba [x0, y0, x1, y1]")
    x0, y0, x1, y1 = (float(v) for v in values)
    if not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1):
        raise ValueError(f"{label}: fuera de 0..1 o vacío")
    return x0, y0, x1, y1


def _shape(entry, label: str) -> dict[str, Any]:
    if not isinstance(entry, dict):
        raise ValueError(f"{label}: se esperaba un objeto")
    if "rect" in entry:
        return {**entry, "rect": list(_rect(entry["rect"], label))}
    points = entry.get("points")
    if not isinstance(points, list) or len(points) < 3:
        raise ValueError(f"{label}: se esperaba rect o al menos 3 points")
    for point in points:
        if (not isinstance(point, list) or len(point) != 2
                or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 1 for v in point)):
            raise ValueError(f"{label}: cada punto es [x, y] dentro de 0..1")
    return entry


def _level(entry: dict[str, Any], index: int) -> Level:
    label = f"nivel {index}"
    if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or not entry["id"]:
        raise ValueError(f"{label}: falta id")
    label = f"nivel {entry['id']}"
    zones = [None if entry.get(key) is None else _rect(entry[key], f"{label} {key}")
             for key in ("start_zone", "goal_zone")]
    if (zones[0] is None) != (zones[1] is None):
        raise ValueError(f"{label}: las dos zonas o ninguna")
    bumpers = entry.get("bumpers_max")
    if bumpers is not None and (isinstance(bumpers, bool) or not isinstance(bumpers, int) or bumpers < 0):
        raise ValueError(f"{label}: bumpers_max es un entero >= 0 o null")
    spikes = tuple(_shape(s, f"{label} spikes") for s in entry.get("spikes", ()))
    for strip in spikes:
        if strip.get("facing", "up") not in FACINGS:
            raise ValueError(f"{label}: facing es up, down, left o right")
    portals = []
    for portal in entry.get("portals", ()):
        exit_ = portal.get("exit") if isinstance(portal, dict) else None
        if (not isinstance(exit_, list) or len(exit_) != 2
                or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 < v < 1 for v in exit_)):
            raise ValueError(f"{label}: cada portal tiene entry y exit [x, y] dentro de 0..1")
        portals.append({"entry": _shape(portal.get("entry"), f"{label} portal"), "exit": exit_})
    return Level(id=entry["id"], name=str(entry.get("name", entry["id"])), hint=str(entry.get("hint", "")),
                 start_zone=zones[0], goal_zone=zones[1], bumpers_max=bumpers,
                 walls=tuple(_shape(w, f"{label} walls") for w in entry.get("walls", ())),
                 spikes=spikes, portals=tuple(portals), solution=tuple(entry.get("solution", ())))


def load_levels(path: Path = LEVELS_PATH) -> tuple[Level, ...]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("version") != 1:
        raise ValueError("levels.json: versión desconocida")
    levels = tuple(_level(entry, index) for index, entry in enumerate(data["levels"]))
    ids = [level.id for level in levels]
    if len(set(ids)) != len(ids) or FREE_LEVEL not in ids:
        raise ValueError("levels.json: ids repetidos o falta el nivel libre")
    return levels


LEVELS = load_levels()


def level_by_id(level_id: object) -> Level | None:
    return next((level for level in LEVELS if level.id == level_id), None)


def next_level(level: Level) -> Level | None:
    """The level after this one; none after the last, nor after the free one."""
    if level.id == FREE_LEVEL:
        return None
    index = LEVELS.index(level)
    return LEVELS[index + 1] if index + 1 < len(LEVELS) else None


def _polygon(shape: dict[str, Any], width: float, height: float) -> Polygon:
    if "rect" in shape:
        x0, y0, x1, y1 = shape["rect"]
        corners = ((x0, y0), (x1, y0), (x1, y1), (x0, y1))
    else:
        corners = shape["points"]
    return Polygon(tuple(Point(x * width, y * height) for x, y in corners))


def _zone(rect, width: float, height: float) -> Polygon | None:
    return None if rect is None else _polygon({"rect": list(rect)}, width, height)


def level_geometry(level: Level, width: float, height: float) -> LevelGeometry:
    return LevelGeometry(
        width=width, height=height,
        start_zone=_zone(level.start_zone, width, height),
        goal_zone=_zone(level.goal_zone, width, height),
        walls=tuple(_polygon(shape, width, height) for shape in level.walls),
        spikes=tuple(_polygon(shape, width, height) for shape in level.spikes),
        facings=tuple(shape.get("facing", "up") for shape in level.spikes),
        portals=tuple(Portal(_polygon(p["entry"], width, height), Point(p["exit"][0] * width, p["exit"][1] * height))
                      for p in level.portals),
    )


def with_obstacles(board: Board, geometry: LevelGeometry) -> Board:
    return replace(board, walls=geometry.walls, spikes=geometry.spikes, portals=geometry.portals)


# ---------------------------------------------------------------- checks

def _vertex_mean(polygon: Polygon) -> Point:
    points = polygon.points
    sx = sy = 0.0
    for point in points:
        sx += point.x
        sy += point.y
    return Point(sx / len(points), sy / len(points))


def _area2(points) -> float:
    total = 0.0
    for k, a in enumerate(points):
        b = points[(k + 1) % len(points)]
        total += a.x * b.y - b.x * a.y
    return total


def inside(point: Point, polygon: Polygon) -> bool:
    """Inside or on the edge of a convex polygon, either winding."""
    points = polygon.points
    sign = 1.0 if _area2(points) > 0 else -1.0
    for k, a in enumerate(points):
        b = points[(k + 1) % len(points)]
        if sign * ((b.x - a.x) * (point.y - a.y) - (b.y - a.y) * (point.x - a.x)) < 0:
            return False
    return True


def _separated(first: Polygon, second: Polygon) -> bool:
    for polygon in (first, second):
        points = polygon.points
        for k, a in enumerate(points):
            b = points[(k + 1) % len(points)]
            nx, ny = b.y - a.y, a.x - b.x   # unnormalized: only the order of projections matters
            low1 = min(p.x * nx + p.y * ny for p in first.points)
            high1 = max(p.x * nx + p.y * ny for p in first.points)
            low2 = min(p.x * nx + p.y * ny for p in second.points)
            high2 = max(p.x * nx + p.y * ny for p in second.points)
            if high1 < low2 or high2 < low1:
                return True
    return False


def overlaps(first: Polygon, second: Polygon) -> bool:
    """Whether two convex polygons share any point (separating axis test)."""
    return not _separated(first, second)


def _near(point: Point, polygon: Polygon, distance: float) -> bool:
    if inside(point, polygon):
        return True
    limit = distance * distance
    points = polygon.points
    for k, a in enumerate(points):
        b = points[(k + 1) % len(points)]
        ex, ey = b.x - a.x, b.y - a.y
        length2 = ex * ex + ey * ey
        t = 0.0 if length2 == 0 else max(0.0, min(1.0, ((point.x - a.x) * ex + (point.y - a.y) * ey) / length2))
        dx, dy = point.x - (a.x + t * ex), point.y - (a.y + t * ey)
        if dx * dx + dy * dy <= limit:
            return True
    return False


def level_check(level: Level, geometry: LevelGeometry, notes: dict[str, tuple[Polygon, ...]]) -> dict[str, Any]:
    """How the notes (in the geometry's space) stand against the level.

    start / goal: None without zones, else whether exactly one such note has
    its vertex mean inside its zone. bumpers: how many green notes.
    covered: which obstacle kinds a note lies on ("wall", "spikes",
    "portal"), in that order; a note near a portal's exit covers it too.
    """
    def placed(role: str, zone: Polygon | None) -> bool | None:
        if zone is None:
            return None
        found = notes.get(role, ())
        return len(found) == 1 and inside(_vertex_mean(found[0]), zone)

    papers = [*notes.get("start", ()), *notes.get("goal", ()), *notes.get("bumper", ())]
    covered = []
    if any(overlaps(paper, wall) for paper in papers for wall in geometry.walls):
        covered.append("wall")
    if any(overlaps(paper, strip) for paper in papers for strip in geometry.spikes):
        covered.append("spikes")
    if any(overlaps(paper, portal.entry) or _near(portal.exit, paper, BALL_RADIUS_PX + PORTAL_CLEAR_PX)
           for paper in papers for portal in geometry.portals):
        covered.append("portal")
    return {"start": placed("start", geometry.start_zone), "goal": placed("goal", geometry.goal_zone),
            "bumpers": len(notes.get("bumper", ())), "bumpers_max": level.bumpers_max, "covered": covered}


def level_problem(check: dict[str, Any]) -> str | None:
    """The first thing to fix, in the operator's words; None when it plays."""
    if check["start"] is False:
        return "Poné el post-it de inicio dentro de su recuadro."
    if check["goal"] is False:
        return "Poné el post-it de llegada dentro de su recuadro."
    limit, count = check["bumpers_max"], check["bumpers"]
    if limit is not None and count > limit:
        if limit == 0:
            return f"Este nivel se juega sin rebotes: sacá {count}."
        return f"Este nivel deja usar {limit} rebote{'s' if limit != 1 else ''}: sacá {count - limit}."
    if check["covered"]:
        return f"Un post-it tapa {COVER_NAMES[check['covered'][0]]}: movelo."
    return None


# ---------------------------------------------------------------- json

def _points_json(polygon: Polygon) -> dict[str, list[dict[str, float]]]:
    return {"points": [{"x": round(p.x, 3), "y": round(p.y, 3)} for p in polygon.points]}


def geometry_json(geometry: LevelGeometry) -> dict[str, Any]:
    return {
        "width": geometry.width, "height": geometry.height,
        "start_zone": None if geometry.start_zone is None else _points_json(geometry.start_zone),
        "goal_zone": None if geometry.goal_zone is None else _points_json(geometry.goal_zone),
        "walls": [_points_json(wall) for wall in geometry.walls],
        "spikes": [{**_points_json(strip), "facing": facing}
                   for strip, facing in zip(geometry.spikes, geometry.facings)],
        "portals": [{"entry": _points_json(portal.entry),
                     "exit": {"x": round(portal.exit.x, 3), "y": round(portal.exit.y, 3)}}
                    for portal in geometry.portals],
    }


__all__ = ["BALL_RADIUS_PX", "FREE_LEVEL", "LEVELS", "Level", "LevelGeometry", "LevelProblem", "geometry_json", "inside",
           "level_by_id", "level_check", "level_geometry", "level_problem", "load_levels", "next_level",
           "overlaps", "with_obstacles"]
