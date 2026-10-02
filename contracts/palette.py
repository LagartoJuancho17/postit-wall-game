"""Which camera colors count as each kind of post-it.

Hues use OpenCV's 0..179 scale; saturation and value use 0..255. A rule with
``hue_low > hue_high`` wraps through red (for example 170..8). Rules of
different roles never share a hue, so one note cannot count for two roles.
The vision service owns the defaults and how a picked color becomes a rule;
this module only defines the shared shape and its JSON form.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

ROLES = ("start", "goal", "bumper", "marker")
ROLE_LABELS = {"start": "inicio", "goal": "llegada", "bumper": "rebotes", "marker": "esquinas"}
_HEX = re.compile(r"^#[0-9a-f]{6}$")


@dataclass(frozen=True)
class ColorRule:
    hex: str  # swatch shown to the operator, "#rrggbb"
    hue_low: int
    hue_high: int
    min_saturation: int
    min_value: int

    def __post_init__(self) -> None:
        if not isinstance(self.hex, str) or not _HEX.match(self.hex):
            raise ValueError("El color debe tener la forma #rrggbb.")
        for name, top in (("hue_low", 179), ("hue_high", 179),
                          ("min_saturation", 255), ("min_value", 255)):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= top:
                raise ValueError(f"{name} debe ser un entero entre 0 y {top}.")

    def hues(self) -> frozenset[int]:
        if self.hue_low <= self.hue_high:
            return frozenset(range(self.hue_low, self.hue_high + 1))
        return frozenset(range(self.hue_low, 180)) | frozenset(range(0, self.hue_high + 1))


@dataclass(frozen=True)
class Palette:
    start: ColorRule
    goal: ColorRule
    bumper: ColorRule
    marker: ColorRule

    def __post_init__(self) -> None:
        owner: dict[int, str] = {}
        for role in ROLES:
            for hue in self.rule(role).hues():
                if hue in owner:
                    raise ValueError(f"Los colores de {ROLE_LABELS[owner[hue]]} y "
                                     f"{ROLE_LABELS[role]} se superponen.")
                owner[hue] = role

    def rule(self, role: str) -> ColorRule:
        if role not in ROLES:
            raise ValueError(f"Rol desconocido: {role}.")
        return getattr(self, role)

    def to_json(self) -> dict[str, dict[str, Any]]:
        return {role: asdict(self.rule(role)) for role in ROLES}
