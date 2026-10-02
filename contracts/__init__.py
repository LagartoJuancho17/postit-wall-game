"""Shared, dependency-free data contracts for the wall game."""

from .model import Board, Matrix3, Point, Polygon
from .geometry import quadrilateral_center
from .palette import ROLE_LABELS, ROLES, ColorRule, Palette

__all__ = ["Board", "ColorRule", "Matrix3", "Palette", "Point", "Polygon",
           "ROLE_LABELS", "ROLES", "quadrilateral_center"]
