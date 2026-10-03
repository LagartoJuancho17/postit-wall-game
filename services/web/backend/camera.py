"""Saved camera presets: fixed white balance, exposure and focus per camera.

The browser owns the camera and applies a preset through MediaStreamTrack
constraints. This module validates what the browser sends, keeps one preset
per camera label and stores them in a JSON file. Every mode saved here is
"manual": nothing is left to the camera's automatic algorithms.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MODES = ("whiteBalanceMode", "exposureMode", "focusMode")
VALUES = ("colorTemperature", "exposureTime", "focusDistance",
          "brightness", "contrast", "saturation", "sharpness", "gain")
MAX_LABEL = 200
MAX_REPORT_BYTES = 20_000


def camera_label(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_LABEL:
        raise ValueError(f"camera debe ser el nombre de la cámara (1 a {MAX_LABEL} caracteres).")
    return value.strip()


def camera_settings(value: object) -> dict[str, Any]:
    """Only known keys, every mode manual, every value a finite number."""
    if not isinstance(value, dict) or not value:
        raise ValueError("settings debe ser un objeto con ajustes de cámara.")
    unknown = sorted(set(value) - set(MODES) - set(VALUES))
    if unknown:
        raise ValueError(f"Ajuste de cámara desconocido: {', '.join(unknown)}.")
    clean: dict[str, Any] = {}
    for key in MODES:
        if key in value:
            if value[key] != "manual":
                raise ValueError(f"{key} tiene que ser manual: nada queda en automático.")
            clean[key] = "manual"
    for key in VALUES:
        if key in value:
            number = value[key]
            if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number):
                raise ValueError(f"{key} debe ser un número finito.")
            clean[key] = float(number)
    return clean


def report(value: object) -> dict[str, Any]:
    """A bounded, JSON-serializable object the browser sends for the log."""
    if not isinstance(value, dict):
        raise ValueError("report debe ser un objeto.")
    if len(json.dumps(value, ensure_ascii=False)) > MAX_REPORT_BYTES:
        raise ValueError("El reporte de cámara es demasiado grande.")
    return value


def preset_entry(settings: dict[str, Any], *, score: object = None,
                 black: object = False) -> dict[str, Any]:
    score = float(score) if isinstance(score, (int, float)) and not isinstance(score, bool) \
        and math.isfinite(score) else None
    return {"settings": settings, "score": score, "black": bool(black),
            "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def load_presets(path: Path) -> dict[str, dict[str, Any]]:
    """Presets from disk; raises ValueError when the file is damaged."""
    data = json.loads(path.read_text(encoding="utf-8"))
    presets = data["presets"]
    if not isinstance(presets, dict):
        raise ValueError("presets debe ser un objeto")
    loaded = {}
    for label, entry in presets.items():
        loaded[camera_label(label)] = {
            "settings": camera_settings(entry["settings"]),
            "score": entry.get("score"),
            "black": bool(entry.get("black", False)),
            "saved_at": str(entry.get("saved_at", "")),
        }
    return loaded


def save_presets(path: Path, presets: dict[str, dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(".tmp")
    partial.write_text(json.dumps({"version": 1, "presets": presets}, ensure_ascii=False,
                                  indent=1, sort_keys=True) + "\n", encoding="utf-8")
    partial.replace(path)
