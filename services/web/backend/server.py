"""Loopback-only HTTP server for a browser-owned camera and projector window.

The browser captures the webcam with getUserMedia. JPEG previews cross into
Python for shared live post-it detection. This module never opens a camera
or a desktop window, so macOS attributes camera permission to the
browser displaying the control panel.
"""

from __future__ import annotations

import copy
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

from contracts import ROLE_LABELS, ROLES, Board, Point, Polygon
from services.physics import GameState, GameStatus, launch, ready_state, step
from services.vision import (DEFAULT_PALETTE, build_palette,
                             find_notes, frame_quality)
from services.uvc import UvcBridge, UvcError
from services.vision.palette import normalize_hex

from . import camera
from .camera import camera_label as _camera_label


MAX_BODY_BYTES = 8 * 1024 * 1024
LOCAL_NAMES = ("127.0.0.1", "localhost")  # the panel may be opened with either
MAX_FRAME_PIXELS = 12_000_000
LIVE_DETECTION_MAX_AGE_S = 1.5
LIVE_EXPECTED_NOTES = {"start": 1, "goal": 1, "bumper": None, "marker": None}


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


def _live_board(notes: dict[str, tuple[Polygon, ...]], width: int, height: int) -> Board:
    """Use camera pixels directly; corners do not define the playable area."""
    for role in ("start", "goal"):
        if len(notes[role]) != 1:
            raise ValueError(f"Post-its de {ROLE_LABELS[role]}: se esperaba 1; "
                             f"se detectaron {len(notes[role])}.")
    board = Board(
        playfield=Polygon((Point(0, 0), Point(width, 0),
                           Point(width, height), Point(0, height))),
        start=notes["start"][0], goal=notes["goal"][0],
        bumpers=notes["bumper"], markers=notes["marker"],
    )
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
        self.game: GameState | None = None
        self.projector_open = False
        self.projector_width = 0
        self.projector_height = 0
        self.projection_mode = "live"
        self.live_detection: dict[str, Any] | None = None
        self._last_detection: float | None = None
        self.board_width = 0
        self.board_height = 0
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
        if self.game is not None:
            if self.paused and self.game.status == GameStatus.RUNNING:
                return "paused"
            return self.game.status.value
        return "waiting_camera"

    def _snapshot_unlocked(self) -> dict[str, Any]:
        self._expire_detection_unlocked()
        game = self.game
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
            "live_detection": copy.deepcopy(self.live_detection),
            "game": {
                "status": game.status.value if game else "none",
                "width": self.board_width,
                "height": self.board_height,
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
            },
        }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._snapshot_unlocked()

    def _expire_detection_unlocked(self) -> None:
        if (self._last_detection is not None and
                self._clock() - self._last_detection > LIVE_DETECTION_MAX_AGE_S):
            self._clear_detection_unlocked()
            self._changed("Esperando detección en vivo de la cámara.")
            self._log("live_detection_expired")

    def _clear_detection_unlocked(self) -> None:
        self.live_detection = None
        self._last_detection = None
        self.camera_board = None
        if self.game is None or self.game.status == GameStatus.READY:
            self.board = None
            self.game = None
            self.board_width = self.board_height = 0

    def _read_frame_unlocked(self, frame: np.ndarray,
                             label: str | None) -> tuple[dict[str, Any], str | None]:
        notes = find_notes(frame, self.palette)
        height, width = frame.shape[:2]
        preview = {
            "width": width,
            "height": height,
            "roles": {role: {"count": len(notes[role]), "expected": LIVE_EXPECTED_NOTES[role],
                             "notes": [_polygon_json(polygon) for polygon in notes[role]]}
                      for role in ROLES},
            "quality": frame_quality(frame, self.palette, notes),
        }
        error = None
        try:
            candidate = _live_board(notes, width, height)
        except ValueError as exc:
            candidate, error = None, str(exc)
        preview["quality"]["ready"] = candidate is not None
        if label is not None and self.frame_camera is not None and label != self.frame_camera:
            # A camera source change cannot keep an attempt in an unrelated image plane.
            self.board = self.game = None
            self.board_width = self.board_height = 0
            self.paused = False
            self._last_tick = None
            self._accumulator = 0.0
        previous = self.camera_board
        self.live_detection = preview
        self._last_detection = self._clock()
        self.frame_width, self.frame_height = width, height
        if label is not None:
            self.frame_camera = label
        self.camera_board = candidate
        # An attempt keeps its collision geometry, including while paused or
        # displaying its result. Only the overlay follows the moving notes.
        if self.game is None or self.game.status == GameStatus.READY:
            self.board = candidate
            self.game = ready_state(candidate) if candidate is not None else None
            self.board_width = width if candidate is not None else 0
            self.board_height = height if candidate is not None else 0
        if previous is None and candidate is not None:
            self._changed("Post-its detectados. Listo para lanzar.")
            self._log("live_board_ready", bumpers=len(candidate.bumpers),
                      camera_width=width, camera_height=height)
        elif previous is not None and candidate is None:
            self._changed(error or "No se detecta un tablero válido.")
            self._log("live_board_invalid", reason=error)
        else:
            self.revision += 1
        return preview, error

    @staticmethod
    def _frame_label(label: str | None) -> str | None:
        if label is not None:
            try:
                return _camera_label(label)
            except ValueError as exc:
                raise RequestError(str(exc)) from exc
        return None

    def action(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._expire_detection_unlocked()
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
                self.projection_mode = "live"
                self._changed("Ventana del proyector abierta. Mostrando detección en vivo.")
                if changed_size:
                    self._log("projection_resized", width=width, height=height)
            elif action == "camera_changed":
                self.board = self.game = None
                self._clear_detection_unlocked()
                self.frame_width = self.frame_height = 0
                self.frame_camera = None
                self.paused = False
                self._last_tick = None
                self._accumulator = 0.0
                self.projection_mode = "live"
                self._changed("Cambió la cámara. Esperando detección en vivo.")
                self._log("camera_changed")
            elif action == "projection_close":
                self.projector_open = False
                self.projection_mode = "live"
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
                self.projection_mode = "live"
                self.revision += 1
            elif action in ("aim", "aim_left", "aim_right"):
                if self.game is None or self.game.status != GameStatus.READY:
                    raise RequestError("La dirección solo se cambia antes del lanzamiento.",
                                       HTTPStatus.CONFLICT)
                aim = (_finite_number(payload.get("degrees"), "degrees") if action == "aim"
                       else self.aim_degrees + (-5 if action == "aim_left" else 5))
                self.aim_degrees = (aim + 180) % 360 - 180
                self.revision += 1
            elif action == "launch":
                if (not self.projector_open or self.camera_board is None or
                        self.board is None or self.game is None):
                    raise RequestError("Abrí el proyector y mostrale a la cámara un inicio y una llegada.",
                                       HTTPStatus.CONFLICT)
                if self.game.status != GameStatus.READY:
                    raise RequestError("Reiniciá el intento antes de volver a lanzar.",
                                       HTTPStatus.CONFLICT)
                speed = min(self.board_width, self.board_height) * 0.4
                self.game = launch(self.board, self.aim_degrees, speed)
                self.attempts += 1
                self.paused = False
                self._last_tick = self._clock()
                self._accumulator = 0.0
                self.projection_mode = "live"
                self._changed("Pelota lanzada.")
                self._log("attempt_started", attempt=self.attempts,
                          angle_degrees=round(self.aim_degrees, 1), speed_px_s=round(speed, 1))
            elif action == "reset":
                if self.camera_board is None:
                    raise RequestError("La cámara debe detectar un inicio y una llegada para reiniciar.",
                                       HTTPStatus.CONFLICT)
                self.board = self.camera_board
                self.board_width, self.board_height = self.frame_width, self.frame_height
                self.game = ready_state(self.board)
                self.paused = False
                self._last_tick = None
                self._accumulator = 0.0
                self.projection_mode = "live"
                self._changed("Intento reiniciado con los post-its de la vista en vivo.")
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
                self._clear_detection_unlocked()
                self._changed(f"Color de {ROLE_LABELS[role]} actualizado.")
                self._log("palette_changed", role=role, hex=rule.hex,
                          hue_low=rule.hue_low, hue_high=rule.hue_high)
            elif action == "palette_reset":
                self.palette_choices = {role: None for role in ROLES}
                self.palette = DEFAULT_PALETTE
                self._save_palette()
                self._clear_detection_unlocked()
                self._changed("Colores originales restaurados.")
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
                self.projection_mode = "live"
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

    def preview_jpeg(self, data: bytes, camera: str | None = None) -> dict[str, Any]:
        """Publish every visible note and update the next playable camera board.

        Partial walls are still broadcast to the projector. Running attempts
        retain the board they launched on, so moving a note never teleports a ball.
        """
        label = self._frame_label(camera)
        frame = _decode_jpeg(data)
        with self._lock:
            try:
                preview, _ = self._read_frame_unlocked(frame, label)
            except ValueError as exc:
                raise RequestError(str(exc)) from exc
            return copy.deepcopy(preview)

    def scan_jpeg(self, data: bytes, camera: str | None = None) -> dict[str, Any]:
        """Optional explicit refresh/reset, without blackout or calibration."""
        label = self._frame_label(camera)
        frame = _decode_jpeg(data)
        with self._lock:
            if self.game is not None and self.game.status == GameStatus.RUNNING and not self.paused:
                raise RequestError("Terminá o pausá el intento antes de escanear.",
                                   HTTPStatus.CONFLICT)
            try:
                _, error = self._read_frame_unlocked(frame, label)
            except ValueError as exc:
                raise RequestError(str(exc)) from exc
            if self.camera_board is None:
                raise RequestError(error or "No se detecta un tablero válido.")
            self.board = self.camera_board
            self.board_width, self.board_height = self.frame_width, self.frame_height
            self.game = ready_state(self.board)
            self.paused = False
            self._accumulator = 0.0
            self._last_tick = None
            self.projection_mode = "live"
            self._changed("Tablero actualizado con la detección en vivo.")
            self._log("board_scanned", bumpers=len(self.board.bumpers),
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
                    "/api.mjs": "api.mjs", "/live-view.mjs": "live-view.mjs",
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
                    label = self.headers.get("X-Postit-Camera")
                    result = controller.preview_jpeg(body, unquote(label) if label else None)
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
