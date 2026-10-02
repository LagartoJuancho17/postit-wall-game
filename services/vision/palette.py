"""Default detection colors and colors picked by the operator.

The defaults are the hand-tuned hue bands this detector always used. A
picked color becomes a window of ±HUE_TOLERANCE around its hue, with
saturation and value floors scaled from the sample, so a pale note still
passes while the white wall does not.

Roles never share a hue. Between a picked role and any other role, each hue
goes to the role whose color is circularly closer, and a hue exactly halfway
goes to neither. Two default roles keep their hand-tuned bands. The result
depends only on the choices, never on the order they were made in.
"""

from __future__ import annotations

import colorsys
from collections.abc import Mapping
from dataclasses import replace

from contracts.palette import ROLE_LABELS, ROLES, ColorRule, Palette

HUE_TOLERANCE = 10
MIN_HUE_DISTANCE = 8
MIN_SAMPLE_SATURATION = 45
MIN_SAMPLE_VALUE = 40

DEFAULT_PALETTE = Palette(
    start=ColorRule("#f7d84b", 20, 39, 75, 55),
    goal=ColorRule("#ff9b49", 5, 19, 75, 55),
    bumper=ColorRule("#6cc678", 40, 90, 75, 55),
    marker=ColorRule("#d75ab9", 125, 179, 75, 55),
)


def normalize_hex(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("El color debe tener la forma #rrggbb.")
    text = value.strip().lower()
    if len(text) != 7 or text[0] != "#" or any(c not in "0123456789abcdef" for c in text[1:]):
        raise ValueError("El color debe tener la forma #rrggbb.")
    return text


def hsv_from_hex(value: str) -> tuple[int, int, int]:
    """Hex color in OpenCV 8-bit HSV: hue 0..179, saturation and value 0..255."""
    text = normalize_hex(value)
    red, green, blue = (int(text[i:i + 2], 16) / 255 for i in (1, 3, 5))
    hue, saturation, value_ = colorsys.rgb_to_hsv(red, green, blue)
    return round(hue * 180) % 180, round(saturation * 255), round(value_ * 255)


def hue_distance(a: int, b: int) -> int:
    difference = abs(a - b) % 180
    return min(difference, 180 - difference)


def rule_from_hex(value: str) -> ColorRule:
    text = normalize_hex(value)
    hue, saturation, brightness = hsv_from_hex(text)
    if saturation < MIN_SAMPLE_SATURATION:
        raise ValueError("Ese color es casi gris: la pared se confundiría con el post-it. "
                         "Elegí un tono más vivo.")
    if brightness < MIN_SAMPLE_VALUE:
        raise ValueError("Ese color es demasiado oscuro: se confundiría con las sombras.")
    return ColorRule(
        hex=text,
        hue_low=(hue - HUE_TOLERANCE) % 180,
        hue_high=(hue + HUE_TOLERANCE) % 180,
        min_saturation=min(120, max(45, round(saturation * 0.5))),
        min_value=min(120, max(40, round(brightness * 0.4))),
    )


def _arc(rule: ColorRule, hues: set[int]) -> ColorRule:
    if not hues:
        raise ValueError("No queda ningún tono libre para ese color.")
    starts = [hue for hue in hues if (hue - 1) % 180 not in hues]
    if len(starts) != 1:
        raise ValueError("Los colores elegidos se cruzan entre sí. Probá con otros tonos.")
    low = high = starts[0]
    while (high + 1) % 180 in hues:
        high = (high + 1) % 180
    return replace(rule, hue_low=low, hue_high=high)


def build_palette(choices: Mapping[str, str | None], changed: str | None = None) -> Palette:
    """Palette from picked colors; ``None`` (or a missing role) keeps the default.

    ``changed`` names the role the operator just edited, so a refusal speaks
    about that role instead of whichever role comes first.
    """
    unknown = set(choices) - set(ROLES)
    if unknown:
        raise ValueError(f"Rol desconocido: {', '.join(sorted(unknown))}.")
    custom = {role for role in ROLES if choices.get(role) is not None}
    base = {role: rule_from_hex(choices[role]) if role in custom else DEFAULT_PALETTE.rule(role)
            for role in ROLES}
    center = {role: hsv_from_hex(base[role].hex)[0] for role in ROLES}
    owned = {role: set(base[role].hues()) for role in ROLES}
    for index, first in enumerate(ROLES):
        for second in ROLES[index + 1:]:
            if first not in custom and second not in custom:
                continue
            if hue_distance(center[first], center[second]) < MIN_HUE_DISTANCE:
                if changed in (first, second):
                    picked = changed
                else:
                    picked = first if first in custom else second
                other = second if picked == first else first
                raise ValueError(
                    f"El color de {ROLE_LABELS[picked]} se parece demasiado al de "
                    f"{ROLE_LABELS[other]}. Elegí otro o cambiá primero el de {ROLE_LABELS[other]}.")
            for role, rival in ((first, second), (second, first)):
                owned[role] = {hue for hue in owned[role]
                               if hue_distance(hue, center[role]) < hue_distance(hue, center[rival])}
    return Palette(**{role: _arc(base[role], owned[role]) for role in ROLES})


def palette_choices_json(choices: Mapping[str, str | None]) -> dict[str, str | None]:
    return {role: choices.get(role) for role in ROLES}
