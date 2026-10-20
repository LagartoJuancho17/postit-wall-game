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
from contracts.geometry import offset_convex_polygon
from services.calibration import (ALIGNMENT_FRAMES, alignment_layout, decode_alignment,
                                  transform_board, transform_point)
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

# Camera <-> projector alignment and the overlay check
# (docs/alignment-and-color.md sections 1.4, 1.5 and 2.3).
ALIGN_NEEDED = len(ALIGNMENT_FRAMES)
# What the projector draws around each note (services/web/ui/live-view.mjs,
# contract 2.1), in projector px; the backend discounts that light (2.5).
RIM_OFFSET_PX = 6.0
RIM_WIDTH_PX = 3.0
RIM_MARGIN_PX = 4.0   # blur and alignment slack around the stroke; it only joins pieces (contract 2.5)
CHECK_KINDS = ("neutral", "overlay")
OVERLAY_MIN_IOU = 0.85
PROBE_GRID = range(1, 10)          # candidate centers at tenths of the viewport
PROBE_CAMERA_MARGIN = 8            # camera px a probe corner keeps from the frame edge
STALE_DETAILS = {
    "projector_resized": "Cambió el tamaño de la proyección: alineá de nuevo.",
    "camera_changed": "Cambió la cámara: alineá de nuevo.",
    "camera_label": "Cambió la cámara: alineá de nuevo.",
    "camera_size": "Cambió la resolución de la cámara: alineá de nuevo.",
}
OVERLAY_UNCHECKED = "El relleno de color todavía no se verificó."
OVERLAY_OK = "Relleno de color verificado con la cámara."
# The physics engine validates in English; the operator reads Spanish.
PHYSICS_MESSAGES = {
    "start centroid must fit inside playfield with ball radius":
        "El inicio está demasiado cerca del borde del área de juego.",
    "start overlaps goal": "El inicio toca la llegada. Separalos.",
    "start overlaps a bumper": "El inicio toca un rebote. Separalos.",
}
PHYSICS_FALLBACK = "Un post-it detectado no tiene una forma válida."


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


def _rectangle(width: float, height: float) -> Polygon:
    return Polygon((Point(0, 0), Point(width, 0), Point(width, height), Point(0, height)))


def _camera_board(notes: dict[str, tuple[Polygon, ...]], width: int, height: int) -> Board:
    """The live board in camera pixels; corners do not define the playable area.

    Only the counts are checked here: whether the board is playable depends
    on the space the game runs in (camera, or projector when aligned).
    """
    for role in ("start", "goal"):
        if len(notes[role]) != 1:
            raise ValueError(f"Post-its de {ROLE_LABELS[role]}: se esperaba 1; "
                             f"se detectaron {len(notes[role])}.")
    return Board(playfield=_rectangle(width, height),
                 start=notes["start"][0], goal=notes["goal"][0],
                 bumpers=notes["bumper"], markers=notes["marker"])


def _operator_reason(exc: Exception) -> str:
    """The Spanish text an operator reads for an invalid live board."""
    text = str(exc)
    if text.startswith("Post-its de "):
        return text
    return PHYSICS_MESSAGES.get(text, PHYSICS_FALLBACK)


def _align_key(header: str | None) -> tuple[int, bool]:
    """``X-Postit-Align: <bit>:<0|1>`` -> (bit, inverse)."""
    if header is None or len(header) != 3 or header[1] != ":" or header[0] not in "0123456" \
            or header[2] not in "01":
        raise RequestError("Cabecera X-Postit-Align inválida.")
    return int(header[0]), header[2] == "1"


def _round3(value: Any) -> Any:
    return round(value, 3) if isinstance(value, float) and math.isfinite(value) else value


def _normalized_inverse(matrix) -> np.ndarray:
    inverse = np.linalg.inv(np.asarray(matrix, dtype=np.float64))
    return inverse / inverse[2, 2]


def _map(point: Point, matrix) -> Point | None:
    try:
        return transform_point(point, matrix)
    except ValueError:
        return None


def _boxes_intersect(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _grown(box: tuple[float, float, float, float], by: float) -> tuple[float, float, float, float]:
    return (box[0] - by, box[1] - by, box[2] + by, box[3] + by)


def probe_side(width: int, height: int) -> int:
    """Side of an overlay-check probe square in projector px (contract 2.3)."""
    return max(32, round(0.05 * min(width, height)))


def place_probes(live_detection: dict[str, Any] | None, matrix, projector: tuple[int, int],
                 camera: tuple[int, int]) -> list[dict[str, Any]]:
    """One bare-wall test square per role, deterministic (contract 2.3).

    Keep-out boxes come from the snapshot's rounded live notes, so the
    Python and JS backends place identical squares.
    """
    width, height = projector
    camera_width, camera_height = camera
    side = probe_side(width, height)
    keep_out = []
    for role in ROLES:
        notes = (live_detection or {}).get("roles", {}).get(role, {}).get("notes", [])
        for note in notes:
            mapped = [_map(Point(point["x"], point["y"]), matrix) for point in note["points"]]
            if not mapped or any(point is None for point in mapped):
                continue
            xs, ys = [point.x for point in mapped], [point.y for point in mapped]
            keep_out.append(_grown((min(xs), min(ys), max(xs), max(ys)), side))
    inverse = _normalized_inverse(matrix)
    low = PROBE_CAMERA_MARGIN
    chosen: list[tuple[str, tuple[float, float, float, float]]] = []
    for role in ROLES:
        for j in PROBE_GRID:
            found = None
            for i in PROBE_GRID:
                cx, cy = width * i / 10, height * j / 10
                square = (cx - side / 2, cy - side / 2, cx + side / 2, cy + side / 2)
                if any(_boxes_intersect(square, box) for box in keep_out):
                    continue
                if any(_boxes_intersect(square, _grown(box, side)) for _, box in chosen):
                    continue
                corners = [_map(Point(x, y), inverse) for x, y in
                           ((square[0], square[1]), (square[2], square[1]),
                            (square[2], square[3]), (square[0], square[3]))]
                if any(corner is None or not (low <= corner.x <= camera_width - 1 - low and
                                              low <= corner.y <= camera_height - 1 - low)
                       for corner in corners):
                    continue
                found = square
                break
            if found is not None:
                chosen.append((role, found))
                break
    return [{"role": role, "points": [{"x": x, "y": y} for x, y in
                                      ((box[0], box[1]), (box[2], box[1]), (box[2], box[3]), (box[0], box[3]))]}
            for role, box in chosen]


def _polygon_iou(first: Polygon, second: Polygon) -> float:
    a = np.array([(point.x, point.y) for point in first.points], np.float32)
    b = np.array([(point.x, point.y) for point in second.points], np.float32)
    area_a, area_b = abs(cv2.contourArea(a)), abs(cv2.contourArea(b))
    intersection, _ = cv2.intersectConvexConvex(a, b)
    union = area_a + area_b - intersection
    return float(intersection / union) if union > 0 else 0.0


def _match_role(neutral: list[Polygon], overlay: list[Polygon]) -> float | None:
    """Greedy IoU matching; the smallest IoU among matched pairs (None: no pair)."""
    pairs = sorted(((_polygon_iou(a, b), i, j) for i, a in enumerate(neutral)
                    for j, b in enumerate(overlay)), key=lambda pair: (-pair[0], pair[1], pair[2]))
    used_a: set[int] = set()
    used_b: set[int] = set()
    matched = []
    for iou, i, j in pairs:
        if i in used_a or j in used_b:
            continue
        used_a.add(i)
        used_b.add(j)
        matched.append(iou)
    return min(matched) if matched else None


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
        self.board_space = "camera"           # space of self.board: "camera" or "projector"
        # The last live notes and the playable board they give, in the game's
        # space; kept so a change of alignment re-maps them without a new frame.
        self._last_notes: tuple[dict[str, tuple[Polygon, ...]], int, int] | None = None
        self._candidate: tuple[Board, str, int, int] | None = None
        # Alignment (contract 1.4) and the overlay check (contract 2.3).
        self._align_status = "none"
        self._align_layout: dict[str, Any] | None = None
        self._align_frames: dict[tuple[int, bool], np.ndarray] = {}
        self._align_matrix: list[list[float]] | None = None
        self._align_camera: dict[str, Any] | None = None
        self._align_projector: dict[str, int] | None = None
        self._align_quality: dict[str, Any] | None = None
        self._align_detail = "Sin alinear."
        self._align_code: str | None = None   # decoder failure code of the last run
        self._overlay: dict[str, Any] = self._overlay_state("unchecked", [])
        self._check_frames: dict[str, np.ndarray] = {}
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
            "alignment": self._alignment_json(),
            "game": {
                "status": game.status.value if game else "none",
                "space": self.board_space if game else self._next_space(),
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

    # ------------------------------------------------------------ alignment state

    @staticmethod
    def _overlay_state(status: str, probes: list[dict[str, Any]], detail: str | None = None) -> dict[str, Any]:
        return {"status": status, "fill": status == "ok",
                "detail": detail or (OVERLAY_OK if status == "ok" else OVERLAY_UNCHECKED),
                "probes": probes}

    def _next_space(self) -> str:
        return "projector" if self._align_status == "ready" else "camera"

    def _alignment_json(self) -> dict[str, Any]:
        return {
            "status": self._align_status,
            "layout": copy.deepcopy(self._align_layout) if self._align_status == "collecting" else None,
            "matrix": [list(row) for row in self._align_matrix] if self._align_matrix is not None else None,
            "camera": dict(self._align_camera) if self._align_camera else None,
            "projector": dict(self._align_projector) if self._align_projector else None,
            "received": len(self._align_frames),
            "needed": ALIGN_NEEDED,
            "quality": dict(self._align_quality) if self._align_quality else None,
            "detail": self._align_detail,
            "code": self._align_code,
            "overlay": {**self._overlay, "probes": [
                {"role": probe["role"], "points": [{"x": round(point["x"], 3), "y": round(point["y"], 3)}
                                                   for point in probe["points"]]}
                for probe in self._overlay["probes"]]},
        }

    def _drop_alignment_unlocked(self, status: str, detail: str) -> None:
        self._align_status = status
        self._align_layout = None
        self._align_frames = {}
        self._align_matrix = None
        self._align_camera = None
        self._align_projector = None
        self._align_quality = None
        self._align_detail = detail
        self._align_code = None
        self._overlay = self._overlay_state("unchecked", [])
        self._check_frames = {}
        if self.projection_mode == "align":
            self.projection_mode = "live"
        self._recompute_candidate_unlocked()

    def _invalidate_alignment_unlocked(self, reason: str) -> None:
        """A measured mapping no longer fits the camera or the projector window."""
        if self._align_status not in ("ready", "collecting"):
            return
        detail = STALE_DETAILS[reason]
        self._drop_alignment_unlocked("stale", detail)
        self._changed(detail)
        self._log("alignment_invalidated", reason=reason)

    # ------------------------------------------------------------ game geometry

    def _game_board_unlocked(self, camera_board: Board, width: int, height: int) -> tuple[Board, str, int, int]:
        """The playable board in the game's space (contract 1.5); raises ValueError."""
        if self._align_status == "ready" and self._align_matrix is not None:
            mapped = transform_board(camera_board, self._align_matrix)
            board = Board(playfield=_rectangle(self.projector_width, self.projector_height),
                          start=mapped.start, goal=mapped.goal,
                          bumpers=mapped.bumpers, markers=mapped.markers)
            ready_state(board)
            return board, "projector", self.projector_width, self.projector_height
        ready_state(camera_board)
        return camera_board, "camera", width, height

    def _candidate_unlocked(self, notes: dict[str, tuple[Polygon, ...]], width: int, height: int):
        """(camera board, game board tuple, operator reason) for one set of live notes."""
        try:
            camera_board = _camera_board(notes, width, height)
            return camera_board, self._game_board_unlocked(camera_board, width, height), None
        except ValueError as exc:
            return None, None, _operator_reason(exc)

    def _apply_candidate_unlocked(self) -> None:
        """An idle or ready game follows the latest playable board."""
        if self._candidate is None:
            self.board = self.game = None
            self.board_width = self.board_height = 0
            self.board_space = self._next_space()
            return
        board, space, width, height = self._candidate
        self.board = board
        self.game = ready_state(board)
        self.board_space, self.board_width, self.board_height = space, width, height

    def _recompute_candidate_unlocked(self) -> None:
        """Re-map the last live notes after the alignment changed."""
        if self._last_notes is None:
            return
        notes, width, height = self._last_notes
        self.camera_board, self._candidate, _ = self._candidate_unlocked(notes, width, height)
        if self.game is None or self.game.status == GameStatus.READY:
            self._apply_candidate_unlocked()

    def _expire_detection_unlocked(self) -> None:
        if (self._last_detection is not None and
                self._clock() - self._last_detection > LIVE_DETECTION_MAX_AGE_S):
            self._clear_detection_unlocked()
            self._changed("Esperando detección en vivo de la cámara.")
            self._log("live_detection_expired")

    def _clear_detection_unlocked(self) -> None:
        self.live_detection = None
        self._last_detection = None
        self._last_notes = None
        self._candidate = None
        self.camera_board = None
        if self.game is None or self.game.status == GameStatus.READY:
            self._apply_candidate_unlocked()

    @staticmethod
    def _measure(frame: np.ndarray, palette, bridge: np.ndarray | None = None):
        """The expensive part of a live frame, run OUTSIDE the game lock so a
        projector tick never waits for a detection (contract section 4)."""
        notes = find_notes(frame, palette, bridge)
        return notes, frame_quality(frame, palette, notes, ready=False)

    def _rim_footprint_unlocked(self, width: int, height: int) -> np.ndarray | None:
        """Where the projector's own rims fall in this camera frame (contract 2.5).

        The rims follow the current live notes: each note goes to projector
        px (through H when aligned, else the contain fit the unaligned window
        uses), is moved RIM_OFFSET_PX outward, and comes back to camera px,
        drawn as a stroke wide enough for the rim plus blur.
        """
        detection = self.live_detection
        if (detection is None or not self.projector_open or self.projection_mode != "live"
                or (detection["width"], detection["height"]) != (width, height)
                or self.projector_width <= 0 or self.projector_height <= 0):
            return None
        if self._align_status == "ready" and self._align_matrix is not None:
            to_projector = np.asarray(self._align_matrix, dtype=np.float64)
        else:
            scale = min(self.projector_width / width, self.projector_height / height)
            to_projector = np.array([[scale, 0.0, (self.projector_width - width * scale) / 2],
                                     [0.0, scale, (self.projector_height - height * scale) / 2],
                                     [0.0, 0.0, 1.0]])
        to_camera = _normalized_inverse(to_projector)
        footprint = np.zeros((height, width), np.uint8)
        drawn = False
        for role in ROLES:
            for note in detection["roles"][role]["notes"]:
                camera = [Point(point["x"], point["y"]) for point in note["points"]]
                projected = [_map(point, to_projector) for point in camera]
                if any(point is None for point in projected):
                    continue
                rim = offset_convex_polygon([(p.x, p.y) for p in projected], RIM_OFFSET_PX)
                if rim is None:
                    continue
                back = [_map(Point(x, y), to_camera) for x, y in rim]
                if any(point is None for point in back):
                    continue
                # Camera px per projector px around this note.
                cx = sum(p.x for p in camera) / len(camera)
                cy = sum(p.y for p in camera) / len(camera)
                there = _map(Point(cx + 1.0, cy), to_projector)
                here = _map(Point(cx, cy), to_projector)
                stretch = math.hypot(there.x - here.x, there.y - here.y) if there and here else 1.0
                thickness = max(3, math.ceil((RIM_WIDTH_PX + 2 * RIM_MARGIN_PX) / max(stretch, 1e-6)))
                stroke = np.array([[math.floor(p.x + 0.5), math.floor(p.y + 0.5)] for p in back], np.int32)
                cv2.polylines(footprint, [stroke], True, 255, thickness, cv2.LINE_8)
                drawn = True
        return footprint if drawn else None

    def _store_frame_unlocked(self, notes: dict[str, tuple[Polygon, ...]], quality: dict[str, Any],
                              width: int, height: int,
                              label: str | None) -> tuple[dict[str, Any], str | None]:
        preview = {
            "width": width,
            "height": height,
            "roles": {role: {"count": len(notes[role]), "expected": LIVE_EXPECTED_NOTES[role],
                             "notes": [_polygon_json(polygon) for polygon in notes[role]]}
                      for role in ROLES},
            "quality": quality,
        }
        if self._align_status in ("ready", "collecting") and self._align_camera is not None:
            if label is not None and label != self._align_camera["label"]:
                self._invalidate_alignment_unlocked("camera_label")
            elif (width, height) != (self._align_camera["width"], self._align_camera["height"]):
                self._invalidate_alignment_unlocked("camera_size")
        if label is not None and self.frame_camera is not None and label != self.frame_camera:
            # A camera source change cannot keep an attempt in an unrelated image plane.
            if self.game is not None:
                self._changed("Cambió la cámara: el intento se descartó.")
                self._log("attempt_discarded", reason="camera_label")
            self.board = self.game = None
            self.board_width = self.board_height = 0
            self.paused = False
            self._last_tick = None
            self._accumulator = 0.0
        camera_board, candidate, error = self._candidate_unlocked(notes, width, height)
        preview["quality"]["ready"] = camera_board is not None
        previous = self.camera_board
        self.live_detection = preview
        self._last_detection = self._clock()
        self._last_notes = (notes, width, height)
        self.frame_width, self.frame_height = width, height
        if label is not None:
            self.frame_camera = label
        self.camera_board = camera_board
        self._candidate = candidate
        # An attempt keeps its collision geometry, including while paused or
        # displaying its result. Only the overlay follows the moving notes.
        if self.game is None or self.game.status == GameStatus.READY:
            self._apply_candidate_unlocked()
        if previous is None and camera_board is not None:
            self._changed("Post-its detectados. Listo para lanzar.")
            self._log("live_board_ready", bumpers=len(camera_board.bumpers),
                      camera_width=width, camera_height=height, space=candidate[1])
        elif previous is not None and camera_board is None:
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
                    self._invalidate_alignment_unlocked("projector_resized")
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
                self._invalidate_alignment_unlocked("camera_changed")
            elif action == "projection_close":
                self.projector_open = False
                self.projection_mode = "live"
                if self.game and self.game.status == GameStatus.RUNNING:
                    self.paused = True
                self._last_tick = None
                self._changed("Ventana del proyector cerrada. La partida quedó pausada.")
                if self._align_status == "collecting":
                    self._drop_alignment_unlocked("none", "Alineación cancelada: se cerró el proyector.")
                    self._log("alignment_cancelled", reason="projection_close")
            elif action == "projection_black":
                if self.game and self.game.status == GameStatus.RUNNING and not self.paused:
                    raise RequestError("Terminá o pausá el intento antes de escanear.",
                                       HTTPStatus.CONFLICT)
                self.projection_mode = "black"
                self._changed("Proyección en negro. Ahora podés capturar la pared.")
            elif action == "projection_neutral":
                if self.game and self.game.status == GameStatus.RUNNING and not self.paused:
                    raise RequestError("Terminá o pausá el intento antes de escanear.",
                                       HTTPStatus.CONFLICT)
                self.projection_mode = "neutral"
                self._changed("Proyección neutra: solo el fondo, para leer colores.")
            elif action == "projection_restore":
                self.projection_mode = "live"
                self.revision += 1
            elif action == "align_start":
                if not self.projector_open:
                    raise RequestError("Abrí la ventana del proyector con J.", HTTPStatus.CONFLICT)
                if self.frame_width <= 0:
                    raise RequestError("Primero tiene que llegar imagen de la cámara.", HTTPStatus.CONFLICT)
                if self.game and self.game.status == GameStatus.RUNNING and not self.paused:
                    raise RequestError("Terminá o pausá el intento antes de alinear.", HTTPStatus.CONFLICT)
                self._drop_alignment_unlocked("collecting", "Alineando cámara y proyector…")
                self._align_layout = alignment_layout(self.projector_width, self.projector_height)
                self._align_camera = {"label": self.frame_camera, "width": self.frame_width,
                                      "height": self.frame_height}
                self.projection_mode = "align"
                self._changed("Alineando cámara y proyector…")
                self._log("alignment_started", width=self.projector_width, height=self.projector_height,
                          dots=len(self._align_layout["dots"]))
            elif action == "align_solve":
                if self._align_status != "collecting":
                    raise RequestError("No hay una alineación en curso.", HTTPStatus.CONFLICT)
                self._solve_alignment_unlocked()
            elif action == "align_cancel":
                if self._align_status == "collecting":
                    self._drop_alignment_unlocked("none", "Alineación cancelada.")
                    self._changed("Alineación cancelada.")
                    self._log("alignment_cancelled", reason="panel")
                else:
                    self.revision += 1
            elif action == "align_clear":
                self._drop_alignment_unlocked("none", "Sin alinear.")
                self._changed("Alineación borrada.")
                self._log("alignment_cleared")
            elif action == "check_solve":
                self._solve_overlay_check_unlocked()
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
                if self.camera_board is None or self._candidate is None:
                    raise RequestError("La cámara debe detectar un inicio y una llegada para reiniciar.",
                                       HTTPStatus.CONFLICT)
                self._apply_candidate_unlocked()
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

    # ------------------------------------------------------------ alignment

    def _solve_alignment_unlocked(self) -> None:
        """Decode the 14 pattern frames (contract 1.3) and adopt or reject H."""
        result = decode_alignment(self._align_frames, self._align_layout)
        self._align_frames = {}
        self.projection_mode = "live"
        if not result["ok"]:
            reason = result["reason"]
            self._drop_alignment_unlocked("failed", reason)
            self._align_code = result["code"]
            self._changed(reason)
            self._log("alignment_failed", code=result["code"], reason=reason,
                      decoded=result.get("decoded"), total=result.get("total"),
                      inliers=result.get("inliers"), holdout_p95_px=_round3(result.get("holdout_p95_px")),
                      coverage=_round3(result.get("coverage")), rejected=result.get("rejected"))
            return
        camera = self._align_camera
        self._align_status = "ready"
        self._align_layout = None
        self._align_matrix = [[float(value) for value in row] for row in result["matrix"]]
        self._align_camera = camera
        self._align_projector = {"width": self.projector_width, "height": self.projector_height}
        self._align_quality = {key: _round3(result[key]) for key in (
            "decoded", "total", "fit", "inliers", "holdout", "holdout_p95_px",
            "holdout_max_px", "outliers", "coverage")}
        probes = place_probes(self.live_detection, self._align_matrix,
                              (self.projector_width, self.projector_height),
                              (camera["width"], camera["height"]))
        self._overlay = self._overlay_state("unchecked", probes)
        self._check_frames = {}
        detail = (f"Proyector alineado: error {result['holdout_p95_px']:.1f} px "
                  f"con {result['decoded']} puntos.")
        self._align_detail = detail
        self._recompute_candidate_unlocked()
        self._changed(detail)
        self._log("alignment_ready", **self._align_quality, probes=len(probes),
                  width=self.projector_width, height=self.projector_height)

    def align_frame_jpeg(self, data: bytes, align: str | None, camera: str | None = None) -> dict[str, Any]:
        """Store one pattern frame of a run in progress (gray, at most 14)."""
        self._frame_label(camera)
        key = _align_key(align)
        frame = _decode_jpeg(data)
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        with self._lock:
            if self._align_status != "collecting":
                raise RequestError("No hay una alineación en curso.", HTTPStatus.CONFLICT)
            height, width = gray.shape
            if (width, height) != (self.frame_width, self.frame_height):
                raise RequestError("El cuadro de alineación no coincide con el tamaño de la cámara.")
            self._align_frames[key] = gray
            self.revision += 1
            return {"received": len(self._align_frames), "needed": ALIGN_NEEDED}

    def check_frame_jpeg(self, data: bytes, kind: str | None) -> dict[str, Any]:
        """Store the neutral or the overlay frame of the overlay check."""
        if kind not in CHECK_KINDS:
            raise RequestError("Cabecera X-Postit-Check inválida.")
        frame = _decode_jpeg(data)
        with self._lock:
            if self._align_status != "ready":
                raise RequestError("Primero alineá el proyector.", HTTPStatus.CONFLICT)
            self._check_frames[kind] = frame
            return {"received": sorted(self._check_frames)}

    def _solve_overlay_check_unlocked(self) -> None:
        """Contract 2.3: does the colored fill change detection, or look like a note?"""
        if self._align_status != "ready":
            raise RequestError("Primero alineá el proyector.", HTTPStatus.CONFLICT)
        if set(self._check_frames) != set(CHECK_KINDS):
            raise RequestError("Faltan los cuadros de verificación.", HTTPStatus.CONFLICT)
        neutral_frame, overlay_frame = self._check_frames["neutral"], self._check_frames["overlay"]
        self._check_frames = {}
        try:
            neutral = find_notes(neutral_frame, self.palette)
            overlay = find_notes(overlay_frame, self.palette)
        except ValueError as exc:
            raise RequestError(str(exc)) from exc
        probes = self._overlay["probes"]
        half = probe_side(self.projector_width, self.projector_height) / 2
        boxes = [(probe["role"], (min(p["x"] for p in probe["points"]) - half,
                                  min(p["y"] for p in probe["points"]) - half,
                                  max(p["x"] for p in probe["points"]) + half,
                                  max(p["y"] for p in probe["points"]) + half)) for probe in probes]
        hit_roles: set[str] = set()
        kept: dict[str, list[Polygon]] = {}
        for role in ROLES:
            kept[role] = []
            for polygon in overlay[role]:
                cx = sum(point.x for point in polygon.points) / len(polygon.points)
                cy = sum(point.y for point in polygon.points) / len(polygon.points)
                mapped = _map(Point(cx, cy), self._align_matrix)
                hits = [probe_role for probe_role, box in boxes if mapped is not None and
                        box[0] <= mapped.x <= box[2] and box[1] <= mapped.y <= box[3]]
                if hits:
                    hit_roles.update(hits)
                else:
                    kept[role].append(polygon)
        roles_log = {}
        failing = None
        for role in ROLES:
            before, after = len(neutral[role]), len(kept[role])
            min_iou = _match_role(list(neutral[role]), kept[role])
            roles_log[role] = {"neutral": before, "overlay": after,
                               "min_iou": round(min_iou, 3) if min_iou is not None else None}
            if failing is None and (before != after or (min_iou is not None and min_iou < OVERLAY_MIN_IOU)):
                failing = (role, before, after)
        probes_hit = [role for role in ROLES if role in hit_roles]
        if probes_hit:
            detail = (f"La cámara toma el color proyectado sobre la pared por un post-it de "
                      f"{ROLE_LABELS[probes_hit[0]]}; se proyectan solo bordes.")
            self._overlay = self._overlay_state("fill_off", probes, detail)
        elif failing is not None:
            role, before, after = failing
            change = f"{before} → {after}" if before != after else "forma"
            what = f"{ROLE_LABELS[role]}: {change}" if before != after else f"{ROLE_LABELS[role]}: forma"
            detail = f"El color proyectado cambiaba la detección ({what}); se proyectan solo bordes."
            self._overlay = self._overlay_state("fill_off", probes, detail)
        else:
            self._overlay = self._overlay_state("ok", probes)
        self._changed(self._overlay["detail"])
        self._log("overlay_check", result=self._overlay["status"], fill=self._overlay["fill"],
                  probes_hit=probes_hit, roles=roles_log)

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
        notes, quality, palette = self._measure_or_refuse(frame)
        height, width = frame.shape[:2]
        with self._lock:
            if self.palette is not palette:
                # The colors changed while detecting: these notes belong to
                # the old palette, so they must not replace the cleared view.
                return {"width": width, "height": height,
                        "roles": {role: {"count": len(notes[role]), "expected": LIVE_EXPECTED_NOTES[role],
                                         "notes": [_polygon_json(p) for p in notes[role]]} for role in ROLES},
                        "quality": {**quality, "ready": False}}
            self._expire_detection_unlocked()
            preview, _ = self._store_frame_unlocked(notes, quality, width, height, label)
            return copy.deepcopy(preview)

    def _measure_or_refuse(self, frame: np.ndarray):
        height, width = frame.shape[:2]
        with self._lock:
            palette = self.palette
            bridge = self._rim_footprint_unlocked(width, height)
        try:
            notes, quality = self._measure(frame, palette, bridge)
        except ValueError as exc:
            raise RequestError(str(exc)) from exc
        return notes, quality, palette

    def scan_jpeg(self, data: bytes, camera: str | None = None) -> dict[str, Any]:
        """Optional explicit refresh/reset, without blackout or calibration."""
        label = self._frame_label(camera)
        frame = _decode_jpeg(data)
        with self._lock:
            if self.game is not None and self.game.status == GameStatus.RUNNING and not self.paused:
                raise RequestError("Terminá o pausá el intento antes de escanear.",
                                   HTTPStatus.CONFLICT)
        notes, quality, palette = self._measure_or_refuse(frame)
        height, width = frame.shape[:2]
        with self._lock:
            if self.game is not None and self.game.status == GameStatus.RUNNING and not self.paused:
                raise RequestError("Terminá o pausá el intento antes de escanear.",
                                   HTTPStatus.CONFLICT)
            if self.palette is not palette:
                raise RequestError("Los colores cambiaron durante la lectura. Volvé a escanear.",
                                   HTTPStatus.CONFLICT)
            self._expire_detection_unlocked()
            _, error = self._store_frame_unlocked(notes, quality, width, height, label)
            if self.camera_board is None or self._candidate is None:
                raise RequestError(error or "No se detecta un tablero válido.")
            self._apply_candidate_unlocked()
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
                    "/align-run.mjs": "align-run.mjs",
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
                if path not in ("/api/action", "/api/frame", "/api/preview", "/api/camera",
                                "/api/align_frame", "/api/check_frame"):
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
                elif path == "/api/align_frame":
                    label = self.headers.get("X-Postit-Camera")
                    result = controller.align_frame_jpeg(body, self.headers.get("X-Postit-Align"),
                                                         unquote(label) if label else None)
                elif path == "/api/check_frame":
                    result = controller.check_frame_jpeg(body, self.headers.get("X-Postit-Check"))
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
