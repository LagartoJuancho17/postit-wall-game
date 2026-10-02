"""Shared, dependency-free data contracts for the wall game."""

from .model import Board, Matrix3, Point, Polygon
from .geometry import quadrilateral_center

__all__ = ["Board", "Matrix3", "Point", "Polygon", "quadrilateral_center"]
