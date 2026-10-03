"""UVC controls <-> the camera vocabulary the panel already speaks.

The panel searches and saves presets with MediaTrack names (whiteBalanceMode,
colorTemperature, exposureMode, exposureTime, focusMode, focusDistance and
the extras). These pure functions translate a helper reading into that shape
and a requested preset into an ordered list of UVC writes.
"""

from __future__ import annotations

from typing import Any

AE_MANUAL = 1
AE_AUTOMATIC = (8, 2, 4)  # aperture priority, full auto, shutter priority
# MediaTrack name -> UVC control for plain values.
VALUES = {
    "colorTemperature": "wb_temperature",
    "exposureTime": "exposure",
    "focusDistance": "focus",
    "brightness": "brightness",
    "contrast": "contrast",
    "saturation": "saturation",
    "sharpness": "sharpness",
    "gain": "gain",
}
# A value can only be written while its automatic is off.
NEEDS_MANUAL = {"colorTemperature": "whiteBalanceMode", "exposureTime": "exposureMode",
                "focusDistance": "focusMode"}


def _range(control: dict[str, Any] | None) -> dict[str, int] | None:
    if not control or "min" not in control or "max" not in control or control["max"] < control["min"]:
        return None
    return {"min": control["min"], "max": control["max"], "step": max(1, control.get("res") or 1)}


def capabilities(raw: dict[str, Any]) -> dict[str, Any]:
    """MediaTrack-shaped capabilities of a helper reading."""
    controls = raw.get("controls", {})
    caps: dict[str, Any] = {}
    if controls.get("wb_auto", {}).get("settable"):
        caps["whiteBalanceMode"] = ["manual", "continuous"]
    ae = controls.get("ae_mode", {})
    if ae.get("settable"):
        supported = ae.get("res", 0)  # GET_RES of the AE mode is the bitmap of supported modes
        modes = (["manual"] if supported & AE_MANUAL else []) + \
            (["continuous"] if any(supported & mode for mode in AE_AUTOMATIC) else [])
        if modes:
            caps["exposureMode"] = modes
    if controls.get("focus_auto", {}).get("settable"):
        caps["focusMode"] = ["manual", "continuous"]
    for name, control in VALUES.items():
        found = _range(controls.get(control))
        if found and controls[control].get("settable"):
            caps[name] = found
    return caps


def settings(raw: dict[str, Any]) -> dict[str, Any]:
    """MediaTrack-shaped current settings of a helper reading."""
    controls = raw.get("controls", {})
    current: dict[str, Any] = {}
    if "cur" in controls.get("wb_auto", {}):
        current["whiteBalanceMode"] = "continuous" if controls["wb_auto"]["cur"] else "manual"
    if "cur" in controls.get("ae_mode", {}):
        current["exposureMode"] = "manual" if controls["ae_mode"]["cur"] == AE_MANUAL else "continuous"
    if "cur" in controls.get("focus_auto", {}):
        current["focusMode"] = "continuous" if controls["focus_auto"]["cur"] else "manual"
    for name, control in VALUES.items():
        if "cur" in controls.get(control, {}):
            current[name] = controls[control]["cur"]
    return current


def _snap(value: float, control: dict[str, Any]) -> int:
    low, high = control["min"], control["max"]
    step = max(1, control.get("res") or 1)
    clamped = min(high, max(low, value))
    return int(min(high, low + round((clamped - low) / step) * step))


def plan(requested: dict[str, Any], raw: dict[str, Any]) -> tuple[list[tuple[str, int]], list[str]]:
    """UVC writes for a (manual-only) preset, automatics off first, and what was skipped.

    Values whose automatic would still be on are skipped: the camera rejects
    them. Controls the camera lacks are skipped too.
    """
    controls = raw.get("controls", {})
    current = settings(raw)
    writes: list[tuple[str, int]] = []
    skipped: list[str] = []
    for mode, control, off in (("whiteBalanceMode", "wb_auto", 0), ("exposureMode", "ae_mode", AE_MANUAL),
                               ("focusMode", "focus_auto", 0)):
        if requested.get(mode) == "manual":
            if controls.get(control, {}).get("settable"):
                writes.append((control, off))
            else:
                skipped.append(mode)
    for name, control in VALUES.items():
        if name not in requested:
            continue
        mode = NEEDS_MANUAL.get(name)
        manual = mode is None or requested.get(mode) == "manual" or current.get(mode) == "manual"
        if not manual or _range(controls.get(control)) is None or not controls[control].get("settable"):
            skipped.append(name)
            continue
        writes.append((control, _snap(requested[name], controls[control])))
    return writes, skipped
