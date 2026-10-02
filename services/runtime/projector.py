"""Projector rendering. All geometry here is already in projector pixels."""

from __future__ import annotations

import math

import pygame

from contracts import Board, Point, Polygon, quadrilateral_center
from services.physics import GameState, GameStatus, BALL_RADIUS_PX


WHITE = (245, 247, 255)
CYAN = (80, 230, 235)
YELLOW = (255, 222, 55)
ORANGE = (255, 146, 48)
GREEN = (70, 235, 105)
MAGENTA = (245, 78, 215)
RED = (255, 95, 105)


def outline_points(polygon: Polygon) -> list[tuple[int, int]]:
    return [(round(p.x), round(p.y)) for p in polygon.points]


def center(polygon: Polygon) -> Point:
    return quadrilateral_center(polygon)


def grid_segments(corners: Polygon) -> list[tuple[Point, Point]]:
    """Lines inside a clockwise quadrilateral for visual alignment review."""
    tl, tr, br, bl = corners.points

    def lerp(a: Point, b: Point, t: float) -> Point:
        return Point(a.x + (b.x - a.x) * t, a.y + (b.y - a.y) * t)

    lines = []
    for t in (0.25, 0.5, 0.75):
        lines.append((lerp(tl, bl, t), lerp(tr, br, t)))
        lines.append((lerp(tl, tr, t), lerp(bl, br, t)))
    return lines


class Projector:
    def __init__(self, screen: pygame.Surface):
        self.screen = screen
        self.width, self.height = screen.get_size()
        self.font = pygame.font.SysFont("Helvetica", 28)
        self.small = pygame.font.SysFont("Helvetica", 22)
        self.big = pygame.font.SysFont("Helvetica", 54, bold=True)

    def clear(self) -> None:
        self.screen.fill((0, 0, 0))
        pygame.display.flip()

    def text(self, value: str, x: int, y: int, *, big: bool = False,
             small: bool = False,
             color: tuple[int, int, int] = WHITE) -> None:
        font = self.big if big else self.small if small else self.font
        image = font.render(value, True, color)
        self.screen.blit(image, (x, y))

    def wrapped(self, value: str, x: int, y: int,
                color: tuple[int, int, int] = WHITE) -> int:
        """Render a message within the projector width, returning next y."""
        words = value.split()
        line = ""
        for word in words:
            candidate = f"{line} {word}" if line else word
            if line and self.font.size(candidate)[0] > self.width - x - 35:
                self.text(line, x, y, color=color)
                y += 38
                line = word
            else:
                line = candidate
        if line:
            self.text(line, x, y, color=color)
            y += 38
        return y

    def notice(self, title: str, lines: list[str]) -> None:
        self.screen.fill((0, 0, 0))
        y = max(45, self.height // 3 - 75)
        self.text(title, 55, y, big=True)
        y += 82
        for line in lines:
            y = self.wrapped(line, 58, y) + 6
        pygame.display.flip()

    def dot(self, point: Point) -> None:
        self.screen.fill((0, 0, 0))
        pygame.draw.circle(self.screen, WHITE, (round(point.x), round(point.y)), 16)
        pygame.display.flip()

    def manual_target(self, label: str, point: Point, index: int) -> None:
        self.screen.fill((0, 0, 0))
        x, y = round(point.x), round(point.y)
        pygame.draw.line(self.screen, WHITE, (x - 25, y), (x + 25, y), 3)
        pygame.draw.line(self.screen, WHITE, (x, y - 25), (x, y + 25), 3)
        pygame.draw.circle(self.screen, MAGENTA, (x, y), 9, 2)
        self.text(f"{index}/4  {label}", 35, 24)
        self.text("Flechas mover  |  Shift preciso  |  Enter guardar", 35,
                  self.height - 42, small=True)
        pygame.display.flip()

    def _polygon(self, polygon: Polygon, color: tuple[int, int, int],
                 width: int = 3) -> None:
        points = outline_points(polygon)
        if len(points) >= 3:
            pygame.draw.polygon(self.screen, color, points, width)

    def _board(self, board: Board, *, show_grid: bool) -> None:
        self._polygon(board.playfield, CYAN, 3)
        if show_grid:
            for a, b in grid_segments(board.playfield):
                pygame.draw.line(self.screen, (60, 105, 115),
                                 (round(a.x), round(a.y)),
                                 (round(b.x), round(b.y)), 1)
        self._polygon(board.start, YELLOW, 4)
        self._polygon(board.goal, ORANGE, 4)
        for bumper in board.bumpers:
            self._polygon(bumper, GREEN, 3)
        for marker in board.markers:
            p = center(marker)
            pygame.draw.circle(self.screen, MAGENTA,
                               (round(p.x), round(p.y)), 8, 2)

    def preview(self, board: Board, detail: str) -> None:
        self.screen.fill((0, 0, 0))
        self._board(board, show_grid=True)
        self.text("Revisá la alineación de contornos y cuadrícula", 35, 20)
        self.text(detail, 35, 62, small=True)
        self.text("Enter jugar  |  A auto  |  M manual  |  Esc salir",
                  35, self.height - 42, small=True)
        pygame.display.flip()

    def game(self, board: Board, state: GameState, aim_degrees: float,
             message: str = "") -> None:
        self.screen.fill((0, 0, 0))
        self._board(board, show_grid=False)
        if state.status == GameStatus.READY:
            x = round(state.position.x)
            y = round(state.position.y)
            rad = math.radians(aim_degrees)
            endpoint = (round(x + math.cos(rad) * 70),
                        round(y + math.sin(rad) * 70))
            pygame.draw.line(self.screen, WHITE, (x, y), endpoint, 3)
            pygame.draw.circle(self.screen, WHITE, endpoint, 5)
        pygame.draw.circle(self.screen, WHITE,
                           (round(state.position.x), round(state.position.y)),
                           round(BALL_RADIUS_PX))
        pygame.draw.circle(self.screen, (22, 28, 36),
                           (round(state.position.x), round(state.position.y)),
                           round(BALL_RADIUS_PX), 2)
        if state.status == GameStatus.WON:
            self.text("¡LLEGASTE!", 35, 20, big=True, color=GREEN)
        elif state.status == GameStatus.LOST:
            self.text("FUERA DEL CAMPO", 35, 20, big=True, color=RED)
        else:
            self.text("Apuntá con ← → y lanzá con Espacio", 35, 20)
        self.text(f"Rebotes: {state.bounce_count}   Tiempo: {state.elapsed_s:.1f} s",
                  35, 82)
        if message:
            self.text(message, 35, 124, color=RED)
        self.text("R reiniciar  |  S releer  |  C calibrar  |  Esc salir",
                  35, self.height - 42, small=True)
        pygame.display.flip()
