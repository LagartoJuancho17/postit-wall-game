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
class Portal:
    """A level's portal (contract 3.2): a ball touching `entry` comes out at `exit`."""
    entry: Polygon
    exit: Point


@dataclass(frozen=True)
class Board:
    playfield: Polygon
    start: Polygon
    goal: Polygon
    bumpers: tuple[Polygon, ...]
    markers: tuple[Polygon, ...]  # top-left, top-right, bottom-right, bottom-left
    # A level's projected obstacles (contract 3.2), in the board's space: walls
    # bounce like bumpers, spikes lose, portals move the ball. None in "libre".
    walls: tuple[Polygon, ...] = ()
    spikes: tuple[Polygon, ...] = ()
    portals: tuple[Portal, ...] = ()


Matrix3: TypeAlias = tuple[tuple[float, float, float], ...]
