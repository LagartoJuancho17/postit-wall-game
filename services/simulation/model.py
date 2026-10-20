"""Wall, projector and camera as one deterministic light model.

Contract: ``docs/alignment-and-color.md`` section 5. Light is linear RGB in
one irradiance unit: the projector's full white at ``brightness == 1`` lights
the wall with 1.0 (before its color response). The camera follows the
contract formula

    pixel = gain * white_balance * reflectance * (ambient + projector_light)

then adds seeded sensor noise, clips, applies gamma 1/2.2 and encodes a JPEG
at quality 85. The same inputs and seed give byte-identical JPEGs, so an eval
built on this model never flakes.

Coordinates: the wall and the camera share camera px. The projector draws in
projector px and its light reaches the wall through the true homography
``G`` (projector px -> camera px), which the game never sees.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from functools import cached_property
from typing import NamedTuple

import cv2
import numpy as np

from .projector import convex_coverage

# Linear RGB reflectance of real paper post-its (neon/canary pads), derived
# from how each one photographs under daylight. All four sit inside the
# default palette's hue bands once the camera encodes them.
PAPER_COLORS: dict[str, tuple[float, float, float]] = {
    "yellow": (0.78, 0.62, 0.06),
    "orange": (0.80, 0.25, 0.04),
    "green": (0.22, 0.55, 0.12),
    "pink": (0.70, 0.12, 0.38),
}
ROLE_PAPER = {"start": "yellow", "goal": "orange", "bumper": "green", "marker": "pink"}
WALL_REFLECTANCE = (0.8, 0.8, 0.8)  # a light gray painted wall
WARM_LIGHT = (1.0, 0.92, 0.80)      # tungsten-like room light, relative RGB
# The camera's white balance is set for the room light, so a white sheet lit
# only by the room looks neutral. Projector light is not neutral for it.
ROOM_WHITE_BALANCE = (0.92, 1.0, 1.15)
# Projector color response (input linear RGB -> emitted RGB): channel
# crosstalk and a cool white point (1.00, 1.00, 1.08), typical of a DLP lamp.
PROJECTOR_COLOR = ((0.94, 0.05, 0.01),
                   (0.03, 0.93, 0.04),
                   (0.01, 0.06, 1.01))
LUMA = np.array((0.2126, 0.7152, 0.0722))  # Rec. 709, linear light
GAMMA = 2.2

_DECODE = ((np.arange(256, dtype=np.float64) / 255.0) ** GAMMA).astype(np.float32)


def apply_homography(matrix: np.ndarray, points: np.ndarray) -> np.ndarray:
    """Map an (N, 2) array of points; rows map in order."""
    points = np.asarray(points, dtype=np.float64).reshape(-1, 2)
    mapped = np.hstack([points, np.ones((len(points), 1))]) @ np.asarray(matrix, np.float64).T
    return mapped[:, :2] / mapped[:, 2:3]


def normalized_inverse(matrix: np.ndarray) -> np.ndarray:
    """Inverse homography with ``h22 == 1`` (the contract's H normalization)."""
    inverse = np.linalg.inv(np.asarray(matrix, dtype=np.float64))
    return inverse / inverse[2, 2]


def homography_from_corners(width: float, height: float, corners) -> np.ndarray:
    """G that sends the corners of a ``width x height`` frame (TL, TR, BR, BL) to ``corners``."""
    source = np.float32([(0, 0), (width, 0), (width, height), (0, height)])
    matrix = cv2.getPerspectiveTransform(source, np.float32(corners)).astype(np.float64)
    return matrix / matrix[2, 2]


def polygon_coverage(corners, width: int, height: int) -> tuple[int, int, np.ndarray] | None:
    """Area coverage of a convex polygon on the pixel grid, in a tight ROI.

    Pixel centers sit at integer coordinates (the detector's convention).
    Coverage is exact along x and sampled on 8 sub-rows along y (see
    ``projector.convex_coverage``), so ground-truth areas carry no fill-rule
    bias: ``cv2.fillPoly`` would make every paper ~0.05 px fatter per edge.
    """
    points = np.asarray(corners, dtype=np.float64).reshape(-1, 2)
    x0 = max(0, int(np.floor(points[:, 0].min())) - 1)
    y0 = max(0, int(np.floor(points[:, 1].min())) - 1)
    x1 = min(width, int(np.ceil(points[:, 0].max())) + 2)
    y1 = min(height, int(np.ceil(points[:, 1].max())) + 2)
    if x1 <= x0 or y1 <= y0:
        return None
    return x0, y0, convex_coverage(points, (x0, y0, x1, y1))


@dataclass(frozen=True)
class Paper:
    """One post-it: a convex quad in camera px with a linear RGB reflectance."""

    role: str
    corners: tuple[tuple[float, float], ...]
    reflectance: tuple[float, float, float]

    def polygon(self) -> np.ndarray:
        return np.asarray(self.corners, dtype=np.float64)

    def area(self) -> float:
        return float(abs(cv2.contourArea(self.polygon().astype(np.float32))))


@dataclass(frozen=True, eq=False)
class Wall:
    """Reflectance in camera px (gray wall plus papers) and the room light."""

    width: int
    height: int
    papers: tuple[Paper, ...] = ()
    ambient: tuple[float, float, float] = tuple(0.1 * c for c in WARM_LIGHT)
    base: tuple[float, float, float] = WALL_REFLECTANCE
    texture: float = 0.02  # +- low-frequency paint unevenness, neutral
    seed: int = 11

    @cached_property
    def reflectance(self) -> np.ndarray:
        """float32 (height, width, 3) linear RGB reflectance, cached."""
        rng = np.random.default_rng(self.seed)
        coarse = rng.uniform(-1.0, 1.0, (9, 16)).astype(np.float32)
        unevenness = cv2.resize(coarse, (self.width, self.height),
                                interpolation=cv2.INTER_CUBIC)
        shade = 1.0 + self.texture * np.clip(unevenness, -1.0, 1.0)
        image = shade[..., None] * np.asarray(self.base, np.float32)
        for paper in self.papers:
            covered = polygon_coverage(paper.corners, self.width, self.height)
            if covered is None:
                continue
            x0, y0, coverage = covered
            h, w = coverage.shape
            region = image[y0:y0 + h, x0:x0 + w]
            alpha = coverage[..., None]
            region[...] = region * (1 - alpha) + np.asarray(paper.reflectance, np.float32) * alpha
        return np.ascontiguousarray(image, dtype=np.float32)

    def without(self, index: int) -> "Wall":
        """The same wall after someone takes paper ``index`` down."""
        papers = self.papers[:index] + self.papers[index + 1:]
        return replace(self, papers=papers)

    def lit(self, level: float, color=WARM_LIGHT) -> "Wall":
        return replace(self, ambient=tuple(level * c for c in color))


@dataclass(eq=False)
class Projector:
    """Turns an sRGB projector frame into wall light in camera px."""

    width: int
    height: int
    to_camera: np.ndarray  # G: projector px -> camera px
    color_matrix: np.ndarray = field(default_factory=lambda: np.asarray(PROJECTOR_COLOR, np.float32))
    black_level: float = 0.003  # leak at input black, relative to white
    blur_sigma: float = 0.8     # projector px, lens defocus
    brightness: float = 1.0
    _footprints: dict = field(default_factory=dict, init=False, repr=False)

    def _footprint(self, size: tuple[int, int]) -> np.ndarray:
        # Where the projector reaches the wall at all (its black level only lands there).
        if size not in self._footprints:
            ones = np.ones((self.height, self.width), np.float32)
            self._footprints[size] = cv2.warpPerspective(
                ones, self.to_camera, size, flags=cv2.INTER_LINEAR,
                borderMode=cv2.BORDER_CONSTANT, borderValue=0)
        return self._footprints[size]

    def light(self, image_rgb: np.ndarray, camera_size: tuple[int, int]) -> np.ndarray:
        """float32 (H_c, W_c, 3) linear irradiance the frame adds to the wall."""
        if (not isinstance(image_rgb, np.ndarray) or image_rgb.dtype != np.uint8 or
                image_rgb.shape != (self.height, self.width, 3)):
            raise ValueError("image_rgb must be uint8 (height, width, 3) in projector px")
        linear = cv2.LUT(image_rgb, _DECODE)
        if self.blur_sigma > 0:
            linear = cv2.GaussianBlur(linear, (0, 0), self.blur_sigma)
        warped = cv2.warpPerspective(linear, self.to_camera, camera_size, flags=cv2.INTER_LINEAR,
                                     borderMode=cv2.BORDER_CONSTANT, borderValue=0)
        if self.black_level > 0:
            warped += self.black_level * self._footprint(camera_size)[..., None]
        emitted = cv2.transform(warped, np.asarray(self.color_matrix, np.float32))
        return emitted * np.float32(self.brightness)


class Capture(NamedTuple):
    jpeg: bytes
    bgr: np.ndarray       # the encoded frame before JPEG compression
    gain: float           # gain used for this frame
    mean_luma: float      # linear mean luminance of the frame, after gain and clip
    clipped: float        # fraction of pixels with a clipped channel


class Camera:
    """Auto (or fixed) exposure, white balance, seeded noise, gamma, JPEG."""

    def __init__(self, width: int, height: int, *, wb_gains=ROOM_WHITE_BALANCE,
                 target: float = 0.18, gain: float | None = None,
                 fixed_gain: float | None = None, seed: int = 0,
                 read_noise: float = 0.0008, shot_noise: float = 2e-5,
                 jpeg_quality: int = 85, max_gain: float = 64.0):
        self.width, self.height = int(width), int(height)
        self.wb_gains = np.asarray(wb_gains, np.float32)
        self.target = float(target)
        self.gain = gain              # next frame's gain under auto exposure
        self.fixed_gain = fixed_gain  # locked exposure: AE state is ignored
        self.read_noise = float(read_noise)
        self.shot_noise = float(shot_noise)
        self.jpeg_quality = int(jpeg_quality)
        self.max_gain = float(max_gain)
        self._rng = np.random.default_rng(seed)

    @property
    def size(self) -> tuple[int, int]:
        return self.width, self.height

    def sensor(self, wall: Wall, light: np.ndarray | None) -> np.ndarray:
        """White-balanced ``reflectance * (ambient + light)``, before gain."""
        if wall.reflectance.shape[:2] != (self.height, self.width):
            raise ValueError("the wall and the camera must share camera px")
        balanced = cv2.multiply(wall.reflectance, tuple(float(g) for g in self.wb_gains) + (0.0,))
        ambient = tuple(float(a) for a in wall.ambient) + (0.0,)
        if light is None:
            return cv2.multiply(balanced, ambient)
        return cv2.multiply(balanced, cv2.add(light, ambient))

    def _metered(self, signal: np.ndarray) -> float:
        luma = float(np.dot(cv2.mean(signal)[:3], LUMA))
        return float(np.clip(self.target / max(luma, 1e-9), 1.0 / self.max_gain, self.max_gain))

    def metered_gain(self, wall: Wall, light: np.ndarray | None) -> float:
        """The gain that puts this scene's mean luminance at the target."""
        return self._metered(self.sensor(wall, light))

    def _expose(self, signal: np.ndarray, gain: float) -> np.ndarray:
        noise = self._rng.standard_normal(signal.shape, dtype=np.float32)
        sigma = cv2.sqrt(cv2.add(cv2.multiply(signal, (self.shot_noise,) * 3 + (0.0,)),
                                 (self.read_noise ** 2,) * 3 + (0.0,)))
        value = cv2.multiply(cv2.add(signal, cv2.multiply(noise, sigma)), (gain,) * 3 + (0.0,))
        return np.clip(value, 0.0, 1.0, out=value)

    def capture(self, wall: Wall, projector_image_rgb: np.ndarray | None,
                projector: Projector | None = None) -> Capture:
        """One frame of the wall lit by ``projector_image_rgb`` (None: projector off)."""
        light = None
        if projector_image_rgb is not None:
            if projector is None:
                raise ValueError("a projector frame needs the projector that shows it")
            light = projector.light(projector_image_rgb, self.size)
        signal = self.sensor(wall, light)
        ideal = self._metered(signal)
        if self.fixed_gain is not None:
            gain = float(self.fixed_gain)
        else:
            gain = ideal if self.gain is None else float(self.gain)
            # Auto exposure: half of the way to the metered gain per frame.
            self.gain = gain + 0.5 * (ideal - gain)
        linear = self._expose(signal, gain)
        rgb = cv2.convertScaleAbs(cv2.pow(linear, 1.0 / GAMMA), alpha=255.0)
        bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
        okay, jpeg = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, self.jpeg_quality])
        if not okay:
            raise RuntimeError("JPEG encoding failed")
        brightest = cv2.max(cv2.max(rgb[..., 0], rgb[..., 1]), rgb[..., 2])
        return Capture(jpeg.tobytes(), bgr, gain,
                       float(np.dot(cv2.mean(linear)[:3], LUMA)),
                       float(np.count_nonzero(brightest == 255)) / brightest.size)


# --- The real setup: iPhone 1280x720, projector 1920x1080 -------------------

CAMERA_SIZE = (1280, 720)
PROJECTOR_SIZE = (1920, 1080)
# Where the projector's corners (TL, TR, BR, BL) land in the camera: a little
# keystone and rotation, never the uniform scale the contain transform assumes.
PROJECTOR_CORNERS_IN_CAMERA = ((96.0, 52.0), (1190.0, 74.0), (1166.0, 668.0), (112.0, 646.0))
# The camera looks at the wall slightly from the side and below.
_CAMERA_TILT = ((0.0, 0.0), (1280.0, 10.0), (1271.0, 720.0), (7.0, 707.0))
# (role, center, (width, height), angle degrees) in camera px before the tilt.
STANDARD_LAYOUT = (
    ("marker", (210, 150), (46, 46), -6), ("marker", (1060, 140), (46, 46), 4),
    ("marker", (1080, 590), (46, 46), -3), ("marker", (220, 600), (46, 46), 7),
    ("start", (380, 400), (62, 62), -12), ("goal", (930, 330), (62, 62), 16),
    ("bumper", (600, 230), (84, 52), 20), ("bumper", (690, 520), (84, 52), -25),
)


def standard_papers(width: int = CAMERA_SIZE[0], height: int = CAMERA_SIZE[1],
                    layout=STANDARD_LAYOUT) -> tuple[Paper, ...]:
    """The eval's wall: 4 pink corners, yellow start, orange goal, 2 green bumpers."""
    sx, sy = width / CAMERA_SIZE[0], height / CAMERA_SIZE[1]
    tilt = homography_from_corners(CAMERA_SIZE[0], CAMERA_SIZE[1], _CAMERA_TILT)
    papers = []
    for role, (cx, cy), (w, h), angle in layout:
        box = cv2.boxPoints(((cx, cy), (w, h), angle)).astype(np.float64)
        corners = apply_homography(tilt, box) * (sx, sy)
        papers.append(Paper(role, tuple((float(x), float(y)) for x, y in corners),
                            PAPER_COLORS[ROLE_PAPER[role]]))
    return tuple(papers)


def standard_rig(*, ambient: float = 0.1, seed: int = 0, brightness: float = 1.0,
                 fixed_gain: float | None = None,
                 scale: float = 1.0) -> tuple[Wall, Projector, Camera]:
    """Wall, projector and camera of the real setup.

    ``scale`` shrinks every size and coordinate alike (gate tests use 0.25:
    a 320x180 camera and a 480x270 projector with the same geometry).
    """
    width, height = (int(round(v * scale)) for v in CAMERA_SIZE)
    projector_size = tuple(int(round(v * scale)) for v in PROJECTOR_SIZE)
    corners = [(x * scale, y * scale) for x, y in PROJECTOR_CORNERS_IN_CAMERA]
    wall = Wall(width, height, standard_papers(width, height)).lit(ambient)
    projector = Projector(*projector_size, homography_from_corners(*projector_size, corners),
                          brightness=brightness)
    camera = Camera(width, height, seed=seed, fixed_gain=fixed_gain)
    return wall, projector, camera
