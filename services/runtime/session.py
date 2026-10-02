"""Interactive session joining the camera, projector, and independent services."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import cv2
import pygame

from contracts import Board, Matrix3, Point
from services.calibration import (
    detect_projected_dot,
    estimate_homography,
    reprojection_errors,
    transform_board,
    transform_point,
)
from services.physics import BALL_RADIUS_PX, GameStatus, launch, ready_state, step
from services.runtime.projector import Projector, center
from services.vision import DetectionError, detect_board


@dataclass(frozen=True)
class SessionConfig:
    camera_index: int = 0
    display_index: int | None = None
    camera_width: int = 1280
    camera_height: int = 720


class QuitRequested(Exception):
    """The user closed the window or pressed Escape."""


def calibration_points(width: int, height: int) -> tuple[tuple[Point, ...], tuple[Point, ...]]:
    """Nine fit locations and four separate quality-check locations."""
    fit = tuple(Point(width * x, height * y)
                for y in (0.2, 0.5, 0.8) for x in (0.2, 0.5, 0.8))
    checks = tuple(Point(width * x, height * y)
                   for y in (0.35, 0.65) for x in (0.35, 0.65))
    return fit, checks


def choose_display(sizes: list[tuple[int, int]], requested: int | None) -> int:
    if not sizes:
        raise RuntimeError("No hay pantallas disponibles para el proyector.")
    index = (1 if len(sizes) > 1 else 0) if requested is None else requested
    if index < 0 or index >= len(sizes):
        raise ValueError(f"Pantalla {index} no existe. Disponibles: 0 a {len(sizes)-1}.")
    return index


def board_fits_screen(board: Board, width: int, height: int) -> bool:
    polygons = (board.playfield, board.start, board.goal, *board.bumpers)
    return all(0 <= point.x < width and 0 <= point.y < height
               for polygon in polygons for point in polygon.points)


def prepare_board(camera_board: Board, matrix: Matrix3,
                  width: int, height: int) -> Board:
    """Map and validate a scan before it can enter a playable attempt."""
    mapped = transform_board(camera_board, matrix)
    if not board_fits_screen(mapped, width, height):
        raise RuntimeError(
            "Hay post-its fuera del área proyectada. Reubicá el proyector "
            "o los post-its y recalibrá."
        )
    ready_state(mapped)
    return mapped


class Session:
    def __init__(self, config: SessionConfig):
        self.config = config
        self.matrix: Matrix3 | None = None
        self.board: Board | None = None
        self.calibration_detail = ""
        self.log_path = Path(__file__).resolve().parents[2] / "runs" / "events.jsonl"

        # Ask macOS for camera permission before covering the projector with
        # a full-screen window, so the permission prompt remains visible.
        backend = cv2.CAP_AVFOUNDATION if sys.platform == "darwin" else cv2.CAP_ANY
        camera = cv2.VideoCapture(config.camera_index, backend)
        if not camera.isOpened():
            raise RuntimeError(
                f"No se pudo abrir la webcam {config.camera_index}. "
                "Revisá el índice y el permiso de cámara de macOS."
            )
        camera.set(cv2.CAP_PROP_FRAME_WIDTH, config.camera_width)
        camera.set(cv2.CAP_PROP_FRAME_HEIGHT, config.camera_height)
        self.camera = camera
        try:
            pygame.display.init()
            pygame.font.init()
            sizes = list(pygame.display.get_desktop_sizes())
            display = choose_display(sizes, config.display_index)
            screen = pygame.display.set_mode(sizes[display], pygame.FULLSCREEN,
                                             display=display)
            pygame.display.set_caption("Post-it Wall Game")
            self.projector = Projector(screen)
            self.clock = pygame.time.Clock()
        except Exception:
            camera.release()
            pygame.quit()
            raise
        self._log("session_started", camera=config.camera_index,
                  display=display, resolution=list(self.projector.screen.get_size()))

    def _log(self, event: str, **fields: object) -> None:
        payload = {"at": datetime.now(timezone.utc).isoformat(),
                   "event": event, **fields}
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, ensure_ascii=False) + "\n")
        print(json.dumps(payload, ensure_ascii=False), flush=True)

    def _events(self) -> list[pygame.event.Event]:
        events = pygame.event.get()
        for event in events:
            if event.type == pygame.QUIT or (event.type == pygame.KEYDOWN
                                             and event.key == pygame.K_ESCAPE):
                raise QuitRequested
        return events

    def _wait_key(self, allowed: set[int]) -> int:
        while True:
            for event in self._events():
                if event.type == pygame.KEYDOWN and event.key in allowed:
                    return event.key
            self.clock.tick(30)

    def _settled_frame(self, delay_ms: int = 130, frames: int = 5):
        """Wait for projection and buffered camera frames to catch up."""
        assert self.camera is not None
        pygame.time.wait(delay_ms)
        image = None
        for _ in range(frames):
            self._events()
            okay, image = self.camera.read()
            if not okay or image is None:
                raise RuntimeError("La webcam dejó de entregar imágenes.")
        return image

    def _dark_frame(self):
        self.projector.clear()
        return self._settled_frame()

    def _camera_board(self) -> Board:
        frame = self._dark_frame()
        return detect_board(frame)

    def _projected_point_camera(self, projector_point: Point) -> Point:
        dark = self._dark_frame()
        self.projector.dot(projector_point)
        lit = self._settled_frame()
        return detect_projected_dot(dark, lit)

    def _automatic_calibration(self) -> tuple[Matrix3, str]:
        camera_board = self._camera_board()
        fit, checks = calibration_points(self.projector.width, self.projector.height)
        camera_points: list[Point] = []
        projector_points: list[Point] = []
        for target in fit:
            try:
                observed = self._projected_point_camera(target)
            except ValueError:
                continue  # A point outside the webcam view is unusable.
            camera_points.append(observed)
            projector_points.append(target)
        if len(camera_points) < 6:
            raise RuntimeError(
                f"La webcam vio solo {len(camera_points)}/9 puntos proyectados. "
                "Reencuadrá la webcam o usá la calibración manual."
            )
        matrix = estimate_homography(camera_points, projector_points)

        def capture_checks(targets: tuple[Point, ...]) -> tuple[list[Point], list[Point]]:
            observed_points: list[Point] = []
            target_points: list[Point] = []
            for target in targets:
                try:
                    observed = self._projected_point_camera(target)
                except ValueError:
                    continue
                observed_points.append(observed)
                target_points.append(target)
            return observed_points, target_points

        check_camera, check_projector = capture_checks(checks)
        marker_centers = tuple(center(marker) for marker in camera_board.markers)
        field_center = Point(
            sum(marker.x for marker in marker_centers) / 4,
            sum(marker.y for marker in marker_centers) / 4,
        )
        edge_targets = tuple(transform_point(
            Point(marker.x * 0.9 + field_center.x * 0.1,
                  marker.y * 0.9 + field_center.y * 0.1), matrix
        ) for marker in marker_centers)
        edge_camera, edge_projector = capture_checks(edge_targets)
        if len(check_camera) < 3 or len(edge_camera) < 4:
            raise RuntimeError(
                "Faltan puntos para verificar la calibración en el centro o los bordes. "
                "Reintentá o usá la calibración manual."
            )
        errors = reprojection_errors(check_camera + edge_camera,
                                     check_projector + edge_projector, matrix)
        edge_errors = reprojection_errors(edge_camera, edge_projector, matrix)
        maximum = max(errors)
        mean = sum(errors) / len(errors)
        self._log("calibration", mode="automatic", fit_points=len(camera_points),
                  check_points=len(errors), mean_error_px=round(mean, 2),
                  max_error_px=round(maximum, 2),
                  max_edge_error_px=round(max(edge_errors), 2))
        if maximum > BALL_RADIUS_PX / 2:
            raise RuntimeError(
                f"La calibración desvió hasta {maximum:.1f} px en puntos nuevos. "
                "Reintentá o usá la calibración manual."
            )
        return matrix, f"Automática: error máximo {maximum:.1f} px en {len(errors)} controles"

    def _manual_calibration(self) -> tuple[Matrix3, str]:
        camera_board = self._camera_board()
        marker_centers = tuple(center(marker) for marker in camera_board.markers)
        starting = ((0.2, 0.2), (0.8, 0.2), (0.8, 0.8), (0.2, 0.8))
        labels = ("superior izquierdo", "superior derecho",
                  "inferior derecho", "inferior izquierdo")
        targets: list[Point] = []
        for index, ((fx, fy), label) in enumerate(zip(starting, labels), start=1):
            x = self.projector.width * fx
            y = self.projector.height * fy
            while True:
                dt = min(self.clock.tick(60) / 1000.0, 0.05)
                for event in self._events():
                    if event.type == pygame.KEYDOWN and event.key in (pygame.K_RETURN,
                                                                        pygame.K_KP_ENTER):
                        targets.append(Point(x, y))
                        break
                if len(targets) == index:
                    break
                pressed = pygame.key.get_pressed()
                speed = 85 if pygame.key.get_mods() & pygame.KMOD_SHIFT else 420
                x += (int(pressed[pygame.K_RIGHT]) - int(pressed[pygame.K_LEFT])) * speed * dt
                y += (int(pressed[pygame.K_DOWN]) - int(pressed[pygame.K_UP])) * speed * dt
                x = min(max(0.0, x), self.projector.width - 1.0)
                y = min(max(0.0, y), self.projector.height - 1.0)
                self.projector.manual_target(label, Point(x, y), index)
        matrix = estimate_homography(marker_centers, targets)
        self._log("calibration", mode="manual", fit_points=4)
        return matrix, "Manual: comprobá que la cuadrícula coincida con la pared"

    def _scan_board(self, matrix: Matrix3) -> Board:
        camera_board = self._camera_board()
        mapped = prepare_board(camera_board, matrix,
                               self.projector.width, self.projector.height)
        self._log("board_scanned", bumpers=len(mapped.bumpers), markers=4)
        return mapped

    def _setup(self) -> None:
        choice: int | None = None
        while True:
            if choice is None:
                self.projector.notice("CALIBRACIÓN", [
                    "A: automática con puntos proyectados",
                    "M: manual, alinear cuatro cruces con los fucsia",
                    "Poné todos los post-its y mantené la pared despejada.",
                    "Esc: salir",
                ])
                choice = self._wait_key({pygame.K_a, pygame.K_m})
            try:
                if choice == pygame.K_a:
                    matrix, detail = self._automatic_calibration()
                else:
                    matrix, detail = self._manual_calibration()
                board = self._scan_board(matrix)
                while True:
                    self.projector.preview(board, detail)
                    key = self._wait_key({pygame.K_RETURN, pygame.K_KP_ENTER,
                                          pygame.K_a, pygame.K_m})
                    if key in (pygame.K_RETURN, pygame.K_KP_ENTER):
                        self.matrix = matrix
                        self.board = board
                        self.calibration_detail = detail
                        self._log("calibration_accepted", detail=detail)
                        return
                    choice = key
                    break
            except (ValueError, RuntimeError, DetectionError) as exc:
                self._log("setup_error", detail=str(exc))
                self.projector.notice("REVISAR MONTAJE", [
                    str(exc)[:95],
                    "A: reintentar automático  |  M: calibrar manualmente",
                    "Esc: salir",
                ])
                choice = self._wait_key({pygame.K_a, pygame.K_m})

    def _game_loop(self) -> None:
        assert self.board is not None and self.matrix is not None
        board = self.board
        state = ready_state(board)
        aim = -45.0
        accumulator = 0.0
        previous_status = state.status
        speed = min(self.projector.width, self.projector.height) * 0.4
        while True:
            raw_delta = self.clock.tick(60) / 1000.0
            # A suspended or blocked app pauses gameplay instead of silently
            # counting wall time without simulating the missed trajectory.
            delta = 0.0 if raw_delta > 0.25 else raw_delta
            if raw_delta > 0.25 and state.status == GameStatus.RUNNING:
                self._log("simulation_paused", gap_s=round(raw_delta, 3))
            for event in self._events():
                if event.type != pygame.KEYDOWN:
                    continue
                if event.key == pygame.K_c:
                    self._setup()
                    return
                if event.key == pygame.K_s:
                    if state.status == GameStatus.RUNNING:
                        state = ready_state(board)
                        accumulator = 0.0
                        self._log("attempt_aborted", reason="rescan")
                    try:
                        board = self._scan_board(self.matrix)
                    except (RuntimeError, DetectionError, ValueError) as exc:
                        self._log("scan_error", detail=str(exc))
                        self.projector.notice("NO SE PUDO RELEER", [
                            str(exc)[:95], "Enter: volver a intentar",
                        ])
                        self._wait_key({pygame.K_RETURN, pygame.K_KP_ENTER})
                        continue
                    self.board = board
                    state = ready_state(board)
                    accumulator = 0.0
                    self._log("attempt_reset", reason="rescan")
                elif event.key == pygame.K_r:
                    state = ready_state(board)
                    accumulator = 0.0
                    self._log("attempt_reset", reason="restart")
                elif event.key == pygame.K_SPACE and state.status == GameStatus.READY:
                    state = launch(board, aim, speed)
                    self._log("attempt_started", angle_degrees=round(aim, 1),
                              speed_px_s=round(speed, 1))

            if state.status == GameStatus.READY:
                pressed = pygame.key.get_pressed()
                aim += (int(pressed[pygame.K_RIGHT]) - int(pressed[pygame.K_LEFT])) * 95 * delta
                aim = (aim + 180) % 360 - 180
            elif state.status == GameStatus.RUNNING:
                accumulator += delta
                while accumulator >= 1 / 120 and state.status == GameStatus.RUNNING:
                    state = step(board, state, 1 / 120)
                    accumulator -= 1 / 120

            if state.status != previous_status and state.status in (GameStatus.WON,
                                                                     GameStatus.LOST):
                self._log("attempt_finished", result=state.status.value,
                          elapsed_s=round(state.elapsed_s, 3),
                          bounces=state.bounce_count)
            previous_status = state.status
            self.projector.game(board, state, aim)

    def run(self) -> None:
        try:
            self._setup()
            while True:
                self._game_loop()
        except QuitRequested:
            self._log("session_finished")
        finally:
            if self.camera is not None:
                self.camera.release()
            pygame.quit()
