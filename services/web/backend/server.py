"""Loopback-only HTTP server for a browser-owned camera and projector window.

The browser captures the webcam with getUserMedia. Only a JPEG snapshot taken
while the projector is black crosses into Python. This module never opens a
camera or a desktop window, so macOS attributes camera permission to the
browser displaying the control panel.
"""

from __future__ import annotations

import json
import math
import mimetypes
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import RLock
import time
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import cv2
import numpy as np

from contracts import ROLE_LABELS, ROLES, Board, Point, Polygon, quadrilateral_center
from services.calibration import estimate_homography, transform_board
from services.physics import GameState, GameStatus, launch, ready_state, step
from services.vision import (DEFAULT_PALETTE, EXPECTED_NOTES, build_palette, detect_board,
                             find_notes, frame_quality)
from services.uvc import UvcBridge, UvcError
from services.vision.palette import normalize_hex

from . import camera
from .camera import camera_label as _camera_label


MAX_BODY_BYTES = 8 * 1024 * 1024
LOCAL_NAMES = ("127.0.0.1", "localhost")  # the panel may be opened with either
MAX_FRAME_PIXELS = 12_000_000
_MARKER_LABELS = (
    "superior izquierdo", "superior derecho", "inferior derecho", "inferior izquierdo"
)
_START_TARGET_FRACTIONS = ((0.2, 0.2), (0.8, 0.2), (0.8, 0.8), (0.2, 0.8))


class RequestError(ValueError):
    """A caller error with an explicit HTTP response status."""

    def __init__(self, message: str, status: HTTPStatus = HTTPStatus.BAD_REQUEST):
        super().__init__(message)
        self.status = status


def _jpeg_dimensions(data: bytes) -> tuple[int, int]:
    """Read JPEG SOF dimensions before letting the decoder allocate memory."""
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        raise RequestError("La captura debe ser un JPEG válido.")
    offset = 2
    frame_markers = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                     0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
    while offset + 3 < len(data):
        if data[offset] != 0xFF:
            raise RequestError("La captura JPEG está dañada.")
        while offset < len(data) and data[offset] == 0xFF:
            offset += 1
        if offset >= len(data):
            break
        marker = data[offset]
        offset += 1
        if marker in (0xD9, 0xDA):
            break
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:
            continue
        if offset + 2 > len(data):
            break
        length = int.from_bytes(data[offset:offset + 2], "big")
        if length < 2 or offset + length > len(data):
            raise RequestError("La captura JPEG está dañada.")
        if marker in frame_markers:
            if length < 7:
                raise RequestError("La captura JPEG está dañada.")
            height = int.from_bytes(data[offset + 3:offset + 5], "big")
            width = int.from_bytes(data[offset + 5:offset + 7], "big")
            if width <= 0 or height <= 0:
                raise RequestError("La captura JPEG no tiene dimensiones válidas.")
            if width * height > MAX_FRAME_PIXELS:
                raise RequestError("La captura supera el límite de 12 megapíxeles.",
                                   HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            return width, height
        offset += length
    raise RequestError("La captura JPEG no contiene una imagen válida.")


def _decode_jpeg(data: bytes) -> np.ndarray:
    _jpeg_dimensions(data)
    try:
        frame = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    except cv2.error as exc:
        raise RequestError("No se pudo decodificar la imagen de la cámara.") from exc
    if frame is None:
        raise RequestError("No se pudo decodificar la imagen de la cámara.")
    return frame


def _point_json(point: Point) -> dict[str, float]:
    return {"x": round(point.x, 3), "y": round(point.y, 3)}


def _polygon_json(polygon: Polygon) -> dict[str, list[dict[str, float]]]:
    return {"points": [_point_json(point) for point in polygon.points]}


def _board_json(board: Board | None) -> dict[str, Any] | None:
    if board is None:
        return None
    return {
        "playfield": _polygon_json(board.playfield),
        "start": _polygon_json(board.start),
        "goal": _polygon_json(board.goal),
        "bumpers": [_polygon_json(p) for p in board.bumpers],
        "markers": [_polygon_json(p) for p in board.markers],
    }


def _finite_number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RequestError(f"{label} debe ser un número finito.")
    number = float(value)
    if not math.isfinite(number):
        raise RequestError(f"{label} debe ser un número finito.")
    return number


def _prepare_projector_board(camera_board: Board, matrix: Any,
                             width: int, height: int) -> Board:
    """Validate the mapped board at this web runtime's service boundary."""
    board = transform_board(camera_board, matrix)
    polygons = (board.playfield, board.start, board.goal, *board.bumpers)
    if not all(0 <= point.x < width and 0 <= point.y < height
               for polygon in polygons for point in polygon.points):
        raise ValueError("Hay post-its fuera del área proyectada. Recalibrá el montaje.")
    ready_state(board)
    return board


class GameController:
    """Serialized game transitions; safe for concurrent panel and popup polls."""

    def __init__(self, *, log_path: Path | None = None,
                 clock: Any = time.monotonic, palette_path: Path | None = None,
                 camera_presets_path: Path | None = None, uvc: Any = None):
        self._lock = RLock()
        # USB camera control, outside the game lock: it never touches game state.
        self.uvc = uvc if uvc is not None else UvcBridge()
        self._clock = clock
        self.log_path = log_path
        self.palette_path = palette_path
        self.palette_choices: dict[str, str | None] = {role: None for role in ROLES}
        self.palette = DEFAULT_PALETTE
        self._load_palette()
        self.camera_presets_path = camera_presets_path
        self.camera_presets: dict[str, dict[str, Any]] = {}
        self._load_camera_presets()
        self.camera_board: Board | None = None
        self.board: Board | None = None
        self.matrix = None
        self.game: GameState | None = None
        self.projector_open = False
        self.projector_width = 0
        self.projector_height = 0
        self.projection_mode = "black"
        self._calibration_targets: list[Point] = []
        self._calibration_target: Point | None = None
        self.aim_degrees = -45.0
        self.attempts = 0
        self.paused = False
        self._last_tick: float | None = None
        self._accumulator = 0.0
        self.frame_width = 0
        self.frame_height = 0
        self.frame_camera: str | None = None  # label of the camera the board was scanned with
        self.message = "Abrí la cámara en el navegador y luego la ventana del proyector con J."
        self.revision = 0

    def _log(self, event: str, **fields: object) -> None:
        if self.log_path is None:
            return
        entry = {"at": datetime.now(timezone.utc).isoformat(),
                 "event": event, **fields}
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _changed(self, message: str) -> None:
        self.message = message
        self.revision += 1

    def _load_palette(self) -> None:
        """Restore the operator's colors; a damaged file falls back to defaults."""
        if self.palette_path is None or not self.palette_path.exists():
            return
        try:
            saved = json.loads(self.palette_path.read_text(encoding="utf-8"))["choices"]
            choices = {role: (normalize_hex(saved[role]) if saved.get(role) is not None else None)
                       for role in ROLES}
            if set(saved) - set(ROLES):
                raise ValueError("unknown role")
            palette = build_palette(choices)
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            self._log("palette_ignored", path=str(self.palette_path), reason=str(exc))
            return
        self.palette_choices, self.palette = choices, palette

    def _save_palette(self) -> None:
        if self.palette_path is None:
            return
        try:
            self.palette_path.parent.mkdir(parents=True, exist_ok=True)
            partial = self.palette_path.with_suffix(".tmp")
            partial.write_text(json.dumps({"version": 1, "choices": self.palette_choices},
                                          ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
            partial.replace(self.palette_path)
        except OSError as exc:
            # The colors still apply now; only surviving a restart is lost.
            self._log("palette_not_saved", path=str(self.palette_path), reason=str(exc))

    def _load_camera_presets(self) -> None:
        if self.camera_presets_path is None or not self.camera_presets_path.exists():
            return
        try:
            self.camera_presets = camera.load_presets(self.camera_presets_path)
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            self._log("camera_presets_ignored", path=str(self.camera_presets_path), reason=str(exc))

    def _save_camera_presets(self) -> None:
        if self.camera_presets_path is None:
            return
        try:
            camera.save_presets(self.camera_presets_path, self.camera_presets)
        except OSError as exc:
            self._log("camera_presets_not_saved", path=str(self.camera_presets_path),
                      reason=str(exc))

    def _palette_json(self) -> dict[str, Any]:
        rules = self.palette.to_json()
        return {
            "custom": any(choice is not None for choice in self.palette_choices.values()),
            "roles": {role: {**rules[role], "custom": self.palette_choices[role] is not None}
                      for role in ROLES},
        }

    def _phase(self) -> str:
        if self._calibration_target is not None:
            return "calibrating"
        if self.game is not None:
            if self.paused and self.game.status == GameStatus.RUNNING:
                return "paused"
            return self.game.status.value
        if self.camera_board is not None:
            return "scanned"
        return "waiting_camera"

    def _snapshot_unlocked(self) -> dict[str, Any]:
        game = self.game
        target = None
        if self._calibration_target is not None:
            target = {**_point_json(self._calibration_target),
                      "index": len(self._calibration_targets) + 1,
                      "label": _MARKER_LABELS[len(self._calibration_targets)]}
        return {
            "revision": self.revision,
            "phase": self._phase(),
            "message": self.message,
            "camera": {"status": "browser", "width": self.frame_width,
                       "height": self.frame_height, "label": self.frame_camera},
            "camera_board": _board_json(self.camera_board),
            "board": _board_json(self.board),
            "palette": self._palette_json(),
            "camera_presets": json.loads(json.dumps(self.camera_presets)),
            "calibration": {
                "status": ("collecting" if self._calibration_target is not None else
                           "ready" if self.matrix is not None else "none"),
                "index": len(self._calibration_targets), "total": 4,
                "detail": ("Alineá la cruz con cada post-it de esquina."
                           if self._calibration_target is not None else
                           "Manual: comprobá la alineación de los contornos."
                           if self.matrix is not None else "Sin calibrar"),
                "error_px": None,
            },
            "game": {
                "status": game.status.value if game else "none",
                "position": _point_json(game.position) if game else None,
                "bounces": game.bounce_count if game else 0,
                "elapsed_s": round(game.elapsed_s, 3) if game else 0.0,
                "aim_degrees": round(self.aim_degrees, 3),
                "attempts": self.attempts,
                "paused": self.paused,
            },
            "projection": {
                "open": self.projector_open,
                "mode": self.projection_mode,
                "width": self.projector_width,
                "height": self.projector_height,
                "target": target,
            },
        }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._snapshot_unlocked()

    def _new_target(self) -> None:
        index = len(self._calibration_targets)
        fx, fy = _START_TARGET_FRACTIONS[index]
        self._calibration_target = Point(self.projector_width * fx,
                                         self.projector_height * fy)

    def _projector_point(self, payload: dict[str, Any]) -> Point:
        if self._calibration_target is None:
            raise RequestError("Primero iniciá la calibración.", HTTPStatus.CONFLICT)
        x = _finite_number(payload.get("x"), "x")
        y = _finite_number(payload.get("y"), "y")
        if not (0 <= x < self.projector_width and 0 <= y < self.projector_height):
            raise RequestError("La cruz debe quedar dentro de la ventana del proyector.")
        return Point(x, y)

    def action(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            action = payload.get("action")
            if not isinstance(action, str):
                raise RequestError("Falta action.")
            if action == "projection_open":
                width = _finite_number(payload.get("width"), "width")
                height = _finite_number(payload.get("height"), "height")
                if (not width.is_integer() or not height.is_integer() or
                        not 320 <= width <= 8192 or not 240 <= height <= 8192):
                    raise RequestError("El proyector necesita tamaño válido entre 320×240 y 8192×8192.")
                width, height = int(width), int(height)
                changed_size = (width, height) != (self.projector_width,
                                                   self.projector_height)
                self.projector_width, self.projector_height = width, height
                self.projector_open = True
                if changed_size:
                    self.matrix = None
                    self.camera_board = None
                    self.board = None
                    self.game = None
                    self.frame_width = 0
                    self.frame_height = 0
                    self.frame_camera = None
                    self._calibration_targets = []
                    self._calibration_target = None
                    self.paused = False
                    self.projection_mode = "black"
                    self._changed("Cambió el tamaño del proyector. Escaneá y calibrá otra vez.")
                    self._log("projection_resized", width=width, height=height)
                else:
                    self.projection_mode = ("calibration" if self._calibration_target else
                                            "game" if self.board else "black")
                    self._changed("Ventana del proyector abierta.")
            elif action == "camera_changed":
                self.camera_board = None
                self.board = None
                self.matrix = None
                self.game = None
                self.frame_width = 0
                self.frame_height = 0
                self.frame_camera = None
                self._calibration_targets = []
                self._calibration_target = None
                self.paused = False
                self._last_tick = None
                self._accumulator = 0.0
                self.projection_mode = "black"
                self._changed("Cambió la cámara. Escaneá y calibrá otra vez.")
                self._log("camera_changed")
            elif action == "projection_close":
                self.projector_open = False
                self.projection_mode = "black"
                if self.game and self.game.status == GameStatus.RUNNING:
                    self.paused = True
                self._last_tick = None
                self._changed("Ventana del proyector cerrada. La partida quedó pausada.")
            elif action == "projection_black":
                if self.game and self.game.status == GameStatus.RUNNING and not self.paused:
                    raise RequestError("Terminá o pausá el intento antes de escanear.",
                                       HTTPStatus.CONFLICT)
                self.projection_mode = "black"
                self._changed("Proyección en negro. Ahora podés capturar la pared.")
            elif action == "projection_restore":
                # After a blackout that produced no new board (a failed scan or
                # a camera search), show again what the projector was showing.
                if self.projector_open:
                    self.projection_mode = ("calibration" if self._calibration_target else
                                            "game" if self.board else "black")
                    self.revision += 1
            elif action == "calibrate":
                if not self.projector_open:
                    raise RequestError("Abrí la ventana del proyector con J.", HTTPStatus.CONFLICT)
                if self.camera_board is None:
                    raise RequestError("Primero escaneá los post-its con la proyección negra.",
                                       HTTPStatus.CONFLICT)
                if self.game and self.game.status == GameStatus.RUNNING and not self.paused:
                    raise RequestError("Terminá o pausá el intento antes de recalibrar.",
                                       HTTPStatus.CONFLICT)
                self.matrix = None
                self.board = None
                self.game = None
                self.paused = False
                self._calibration_targets = []
                self._new_target()
                self.projection_mode = "calibration"
                self._changed("Alineá la cruz 1/4 con el post-it de esquina superior izquierdo.")
                self._log("calibration_started", mode="manual")
            elif action == "calibration_move":
                self._calibration_target = self._projector_point(payload)
                self.revision += 1
            elif action == "calibration_confirm":
                point = (self._projector_point(payload)
                         if "x" in payload or "y" in payload else self._calibration_target)
                if point is None or self.camera_board is None:
                    raise RequestError("Primero iniciá la calibración.", HTTPStatus.CONFLICT)
                targets = [*self._calibration_targets, point]
                if len(targets) == 4:
                    camera_points = [quadrilateral_center(p)
                                     for p in self.camera_board.markers]
                    try:
                        matrix = estimate_homography(camera_points, targets)
                        board = _prepare_projector_board(
                            self.camera_board, matrix,
                            self.projector_width, self.projector_height)
                    except (ValueError, RuntimeError) as exc:
                        raise RequestError(f"Calibración inválida: {exc}") from exc
                    self.matrix = matrix
                    self.board = board
                    self.game = ready_state(board)
                    self._calibration_targets = []
                    self._calibration_target = None
                    self.projection_mode = "game"
                    self._accumulator = 0.0
                    self._last_tick = None
                    self._changed("Calibración lista. Revisá los contornos sobre la pared.")
                    self._log("calibration_completed", mode="manual",
                              bumpers=len(board.bumpers), width=self.projector_width,
                              height=self.projector_height)
                else:
                    self._calibration_targets = targets
                    self._new_target()
                    self._changed(f"Alineá la cruz {len(targets)+1}/4 con el post-it "
                                  f"de esquina {_MARKER_LABELS[len(targets)]}.")
            elif action in ("aim", "aim_left", "aim_right"):
                if self.game is None or self.game.status != GameStatus.READY:
                    raise RequestError("La dirección solo se cambia antes del lanzamiento.",
                                       HTTPStatus.CONFLICT)
                aim = (_finite_number(payload.get("degrees"), "degrees") if action == "aim"
                       else self.aim_degrees + (-5 if action == "aim_left" else 5))
                self.aim_degrees = (aim + 180) % 360 - 180
                self.revision += 1
            elif action == "launch":
                if not self.projector_open or self.board is None or self.game is None:
                    raise RequestError("Abrí el proyector, escaneá y calibrá antes de lanzar.",
                                       HTTPStatus.CONFLICT)
                if self.game.status != GameStatus.READY:
                    raise RequestError("Reiniciá el intento antes de volver a lanzar.",
                                       HTTPStatus.CONFLICT)
                speed = min(self.projector_width, self.projector_height) * 0.4
                self.game = launch(self.board, self.aim_degrees, speed)
                self.attempts += 1
                self.paused = False
                self._last_tick = self._clock()
                self._accumulator = 0.0
                self.projection_mode = "game"
                self._changed("Pelota lanzada.")
                self._log("attempt_started", attempt=self.attempts,
                          angle_degrees=round(self.aim_degrees, 1), speed_px_s=round(speed, 1))
            elif action == "reset":
                if self.board is None:
                    raise RequestError("Primero escaneá y calibrá el tablero.", HTTPStatus.CONFLICT)
                self.game = ready_state(self.board)
                self.paused = False
                self._last_tick = None
                self._accumulator = 0.0
                self.projection_mode = "game" if self.projector_open else "black"
                self._changed("Intento reiniciado. Podés mover post-its y volver a escanear.")
                self._log("attempt_reset")
            elif action == "pause":
                if self.game is None or self.game.status != GameStatus.RUNNING:
                    raise RequestError("No hay un intento en marcha.", HTTPStatus.CONFLICT)
                self._tick_unlocked()
                self.paused = True
                self._last_tick = None
                self._changed("Intento pausado.")
            elif action == "palette_set":
                role = payload.get("role")
                if role not in ROLES:
                    raise RequestError("Rol de post-it desconocido.")
                try:
                    choices = {**self.palette_choices, role: normalize_hex(payload.get("hex"))}
                    palette = build_palette(choices, changed=role)
                except ValueError as exc:
                    raise RequestError(str(exc)) from exc
                self.palette_choices, self.palette = choices, palette
                self._save_palette()
                rule = palette.rule(role)
                self._changed(f"Color de {ROLE_LABELS[role]} actualizado."
                              + (" Escaneá otra vez para aplicarlo al tablero."
                                 if self.camera_board is not None else ""))
                self._log("palette_changed", role=role, hex=rule.hex,
                          hue_low=rule.hue_low, hue_high=rule.hue_high)
            elif action == "palette_reset":
                self.palette_choices = {role: None for role in ROLES}
                self.palette = DEFAULT_PALETTE
                self._save_palette()
                self._changed("Colores originales restaurados."
                              + (" Escaneá otra vez para aplicarlos al tablero."
                                 if self.camera_board is not None else ""))
                self._log("palette_reset")
            elif action == "camera_preset_save":
                try:
                    label = camera.camera_label(payload.get("camera"))
                    settings = camera.camera_settings(payload.get("settings"))
                    capabilities = camera.report(payload.get("capabilities") or {})
                except ValueError as exc:
                    raise RequestError(str(exc)) from exc
                entry = camera.preset_entry(settings, score=payload.get("score"),
                                            black=payload.get("black"))
                self.camera_presets[label] = entry
                self._save_camera_presets()
                self._changed(f"Ajuste fijo de cámara guardado para {label}.")
                trials = payload.get("trials")
                self._log("camera_preset_saved", camera=label, settings=settings,
                          score=entry["score"], black=entry["black"],
                          trials=trials if isinstance(trials, int) else None,
                          capabilities=capabilities)
            elif action == "camera_preset_clear":
                try:
                    label = camera.camera_label(payload.get("camera"))
                except ValueError as exc:
                    raise RequestError(str(exc)) from exc
                if self.camera_presets.pop(label, None) is not None:
                    self._save_camera_presets()
                    self._changed(f"Ajuste de cámara borrado para {label}.")
                    self._log("camera_preset_cleared", camera=label)
            elif action == "camera_report":
                try:
                    label = camera.camera_label(payload.get("camera"))
                    details = camera.report(payload.get("report"))
                except ValueError as exc:
                    raise RequestError(str(exc)) from exc
                self._log("camera_report", camera=label, **{"report": details})
            elif action == "resume":
                if (not self.projector_open or self.game is None or
                        self.game.status != GameStatus.RUNNING or not self.paused):
                    raise RequestError("No hay un intento pausado o falta el proyector.",
                                       HTTPStatus.CONFLICT)
                self.paused = False
                self._last_tick = self._clock()
                self.projection_mode = "game"
                self._changed("Intento reanudado.")
            else:
                raise RequestError(f"Acción desconocida: {action}")
            return self._snapshot_unlocked()

    def usb_camera(self, vendor: str | None, product: str | None) -> dict[str, Any]:
        """The USB camera's controls in the panel's vocabulary, or why not."""
        try:
            return self.uvc.status(vendor, product)
        except UvcError as exc:
            return {"available": False, "reason": str(exc)}

    def usb_apply(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Fix the USB camera's settings; only manual modes are accepted."""
        try:
            settings = camera.camera_settings(payload.get("settings"))
        except ValueError as exc:
            raise RequestError(str(exc)) from exc
        try:
            return self.uvc.apply(settings, payload.get("vendor"), payload.get("product"))
        except UvcError as exc:
            raise RequestError(f"La cámara no aceptó el ajuste por USB: {exc}",
                               HTTPStatus.BAD_GATEWAY) from exc

    def preview_jpeg(self, data: bytes) -> dict[str, Any]:
        """Live detection for the camera panel; never changes the game.

        Uses the scan's detector and the current palette, but no count is
        required, so a partial wall still shows what is visible.
        """
        frame = _decode_jpeg(data)
        with self._lock:
            palette = self.palette
        try:
            notes = find_notes(frame, palette)
        except ValueError as exc:
            raise RequestError(str(exc)) from exc
        height, width = frame.shape[:2]
        return {
            "width": width,
            "height": height,
            "roles": {role: {"count": len(notes[role]), "expected": EXPECTED_NOTES[role],
                             "notes": [_polygon_json(polygon) for polygon in notes[role]]}
                      for role in ROLES},
            "quality": frame_quality(frame, palette, notes),
        }

    def scan_jpeg(self, data: bytes, camera: str | None = None) -> dict[str, Any]:
        """Read the board; ``camera`` names the camera, so a later start with
        another camera knows this board does not fit it."""
        if camera is not None:
            try:
                camera = _camera_label(camera)
            except ValueError as exc:
                raise RequestError(str(exc)) from exc
        frame = _decode_jpeg(data)
        with self._lock:
            if not self.projector_open:
                raise RequestError("Abrí la ventana del proyector con J antes de escanear.",
                                   HTTPStatus.CONFLICT)
            if self.projection_mode != "black":
                raise RequestError("Poné la proyección en negro antes de escanear.",
                                   HTTPStatus.CONFLICT)
            if self.game is not None and self.game.status == GameStatus.RUNNING and not self.paused:
                raise RequestError("Terminá o pausá el intento antes de escanear.",
                                   HTTPStatus.CONFLICT)
            try:
                camera_board = detect_board(frame, self.palette)
                board = (_prepare_projector_board(camera_board, self.matrix,
                                                  self.projector_width, self.projector_height)
                         if self.matrix is not None else None)
                game = ready_state(board) if board is not None else None
            except (ValueError, RuntimeError) as exc:
                raise RequestError(str(exc)) from exc
            self.camera_board = camera_board
            self.board = board
            self.game = game
            self.frame_height, self.frame_width = frame.shape[:2]
            self.frame_camera = camera
            self.paused = False
            self._accumulator = 0.0
            self._last_tick = None
            self._calibration_targets = []
            self._calibration_target = None
            self.projection_mode = "game" if board is not None else "black"
            self._changed("Tablero leído. Iniciá la calibración." if board is None else
                          "Tablero actualizado. Listo para otro intento.")
            self._log("board_scanned", bumpers=len(camera_board.bumpers),
                      calibrated=board is not None,
                      camera_width=self.frame_width, camera_height=self.frame_height)
            return self._snapshot_unlocked()

    def _tick_unlocked(self) -> None:
        if (self.game is None or self.board is None or
                self.game.status != GameStatus.RUNNING or self.paused or
                not self.projector_open):
            return
        now = self._clock()
        if self._last_tick is None:
            self._last_tick = now
            return
        elapsed = now - self._last_tick
        self._last_tick = now
        if elapsed < 0 or elapsed > 0.25:
            self._accumulator = 0.0
            self._log("simulation_paused", gap_s=round(elapsed, 3))
            return
        self._accumulator += elapsed
        old_status = self.game.status
        while self._accumulator >= 1 / 120 and self.game.status == GameStatus.RUNNING:
            self.game = step(self.board, self.game, 1 / 120)
            self._accumulator -= 1 / 120
        if self.game.status != old_status:
            self._changed("¡Llegaste!" if self.game.status == GameStatus.WON else
                          "La pelota salió del campo.")
            self._log("attempt_finished", result=self.game.status.value,
                      elapsed_s=round(self.game.elapsed_s, 3),
                      bounces=self.game.bounce_count)

    def tick(self) -> dict[str, Any]:
        with self._lock:
            self._tick_unlocked()
            return self._snapshot_unlocked()


def create_server(port: int = 0, controller: GameController | None = None,
                  static_dir: Path | None = None, *,
                  bind: bool = True) -> ThreadingHTTPServer:
    """Return a loopback HTTP server; ``bind=False`` supports socket-free tests."""
    if not isinstance(port, int) or not 0 <= port <= 65535:
        raise ValueError("port must be between 0 and 65535")
    runs = Path(__file__).resolve().parents[3] / "runs"
    controller = controller or GameController(
        log_path=runs / "web-events.jsonl", palette_path=runs / "palette.json",
        camera_presets_path=runs / "camera-presets.json",
    )
    static_dir = static_dir or Path(__file__).resolve().parents[1] / "ui"

    class Handler(BaseHTTPRequestHandler):
        server_version = "PostitWall/1.0"

        def log_message(self, format: str, *args: Any) -> None:
            pass  # Game events go to runs/web-events.jsonl; avoid noisy frame polls.

        def _local_hosts(self) -> set[str]:
            # Only loopback names with this exact port: an attacker's domain
            # rebound to 127.0.0.1 still arrives with its own Host.
            return {f"{name}:{self.server.server_port}" for name in LOCAL_NAMES}

        def _allowed_host(self) -> bool:
            return self.headers.get("Host") in self._local_hosts()

        def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _error(self, error: RequestError) -> None:
            self._send_json(error.status, {"error": str(error),
                                           "state": controller.snapshot()})

        def _check_host(self) -> None:
            if not self._allowed_host():
                raise RequestError("Host no permitido.", HTTPStatus.FORBIDDEN)

        def _check_write_headers(self) -> None:
            self._check_host()
            if self.headers.get("X-Postit-Client") != "1":
                raise RequestError("Falta la cabecera X-Postit-Client: 1.",
                                   HTTPStatus.FORBIDDEN)
            origin = self.headers.get("Origin")
            if origin is not None and origin not in {f"http://{host}" for host in self._local_hosts()}:
                raise RequestError("Origen no permitido.", HTTPStatus.FORBIDDEN)

        def _read_body(self) -> bytes:
            length_header = self.headers.get("Content-Length")
            if length_header is None:
                raise RequestError("Falta Content-Length.", HTTPStatus.LENGTH_REQUIRED)
            try:
                length = int(length_header)
            except ValueError as exc:
                raise RequestError("Content-Length inválido.") from exc
            if length < 0:
                raise RequestError("Content-Length inválido.")
            if length > MAX_BODY_BYTES:
                raise RequestError("La captura o acción supera 8 MB.",
                                   HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            body = self.rfile.read(length)
            if len(body) != length:
                raise RequestError("El cuerpo de la solicitud está incompleto.")
            return body

        def do_GET(self) -> None:
            try:
                self._check_host()
                path = urlsplit(self.path).path
                if path == "/api/state":
                    self._send_json(HTTPStatus.OK, controller.snapshot())
                    return
                if path == "/api/tick":
                    self._send_json(HTTPStatus.OK, controller.tick())
                    return
                if path == "/api/camera":
                    query = parse_qs(urlsplit(self.path).query)
                    vendor = (query.get("vendor") or [None])[0]
                    product = (query.get("product") or [None])[0]
                    self._send_json(HTTPStatus.OK, controller.usb_camera(vendor, product))
                    return
                files = {
                    "/": "index.html", "/projector": "projector.html",
                    "/app.css": "app.css", "/app.js": "app.js",
                    "/projector.js": "projector.js", "/ui-core.mjs": "ui-core.mjs",
                }
                if path == "/favicon.ico":
                    self.send_response(HTTPStatus.NO_CONTENT)
                    self.end_headers()
                    return
                name = files.get(path)
                if name is None:
                    raise RequestError("Ruta desconocida.", HTTPStatus.NOT_FOUND)
                file_path = static_dir / name
                try:
                    body = file_path.read_bytes()
                except FileNotFoundError as exc:
                    raise RequestError("Interfaz web no instalada.",
                                       HTTPStatus.NOT_FOUND) from exc
                mime = ("text/javascript" if name.endswith(".mjs") else
                        mimetypes.guess_type(name)[0] or "application/octet-stream")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", mime + ("; charset=utf-8" if
                                 mime.startswith("text/") or mime == "application/javascript"
                                 else ""))
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)
            except RequestError as exc:
                self._error(exc)

        def do_POST(self) -> None:
            try:
                self._check_write_headers()
                path = urlsplit(self.path).path
                if path not in ("/api/action", "/api/frame", "/api/preview", "/api/camera"):
                    raise RequestError("Ruta desconocida.", HTTPStatus.NOT_FOUND)
                content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip()
                expected = ("application/json" if path in ("/api/action", "/api/camera")
                            else "image/jpeg")
                if content_type != expected:
                    raise RequestError(f"Content-Type esperado: {expected}.",
                                       HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
                body = self._read_body()
                if path == "/api/frame":
                    label = self.headers.get("X-Postit-Camera")
                    result = controller.scan_jpeg(body, unquote(label) if label else None)
                elif path == "/api/preview":
                    result = controller.preview_jpeg(body)
                else:
                    try:
                        payload = json.loads(body)
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        raise RequestError("JSON inválido.") from exc
                    if not isinstance(payload, dict):
                        raise RequestError("La acción debe ser un objeto JSON.")
                    result = (controller.usb_apply(payload) if path == "/api/camera"
                              else controller.action(payload))
                self._send_json(HTTPStatus.OK, result)
            except RequestError as exc:
                self._error(exc)

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler,
                                 bind_and_activate=bind)
    if not bind:
        server.server_port = port  # ``server_bind`` normally sets this.
    server.daemon_threads = True
    return server


def serve(host: str = "127.0.0.1", port: int = 8765) -> None:
    """Serve the local web app until interrupted; never bind another host."""
    if host != "127.0.0.1":
        raise ValueError("El servidor solo puede escuchar en 127.0.0.1")
    server = create_server(port=port)
    print(f"Juego web: http://127.0.0.1:{server.server_port}", flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
