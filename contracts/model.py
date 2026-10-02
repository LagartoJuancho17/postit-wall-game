"""Coordinates are pixels in one image plane, either camera or projector.

The service boundary is explicit: vision returns camera coordinates;
calibration maps the complete Board into projector coordinates; physics
only receives projector coordinates.
"""

from dataclasses import dataclass
from typing import TypeAlias


@dataclass(frozen=True)
class Point:
    x: float
    y: float


@dataclass(frozen=True)
class Polygon:
    points: tuple[Point, ...]


@dataclass(frozen=True)
class Board:
    playfield: Polygon
    start: Polygon
    goal: Polygon
    bumpers: tuple[Polygon, ...]
    markers: tuple[Polygon, ...]  # top-left, top-right, bottom-right, bottom-left


Matrix3: TypeAlias = tuple[tuple[float, float, float], ...]
