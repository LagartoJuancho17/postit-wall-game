"""Synthetic projector patterns filmed by a synthetic camera, for alignment.

Test helper shared by the gate tests, the realistic eval and later the
simulator and parity export. The projector draws contract 1.2 patterns in
projector px; the light reaches the camera through a known projector →
camera homography `G`, so the true camera → projector mapping is `inv(G)`.

Dots are anti-aliased circles drawn with 8 fractional bits (`cv2.circle`
`shift`), so each dot is centered on its layout coordinate: measured
centroid error is below 0.05 px with no mean bias. Supersampling and then
shrinking with INTER_AREA would shift every dot by a constant fraction of a
pixel (0.375 px at 4x), which is the bias this helper exists to avoid.

Camera model, in linear light (1.0 = full scale at gain 1):
`gain * wall_reflectance * (ambient + projector_light) + reflections`, then
optional gamma, sensor noise (seeded), clipping and JPEG. The wall is gray
with optional colored post-its; a reflection is a static glossy highlight.
"""

from collections.abc import Iterable, Mapping, Sequence

import cv2
import numpy as np

from services.calibration.coded import ALIGNMENT_FRAMES, pattern_dots

SHIFT = 8  # fractional bits for cv2.circle centers and radius
SCALE = 1 << SHIFT

# BGR reflectances of paper notes (yellow, pink, green, blue, orange).
POSTIT_COLORS = (
    (0.15, 0.85, 0.95),
    (0.60, 0.35, 0.95),
    (0.35, 0.85, 0.40),
    (0.90, 0.60, 0.20),
    (0.15, 0.55, 0.95),
)


def render_pattern(layout: Mapping, bit: int, inverse: bool) -> np.ndarray:
    """Pattern frame `{bit, inverse}` in projector px: white dots on black."""

    width, height = int(layout["width"]), int(layout["height"])
    image = np.zeros((height, width), dtype=np.uint8)
    radius = int(round(layout["radius"] * SCALE))
    positions = {dot["id"]: (dot["x"], dot["y"]) for dot in layout["dots"]}
    for dot_id in pattern_dots(layout, bit, inverse):
        x, y = positions[dot_id]
        center = (int(round(x * SCALE)), int(round(y * SCALE)))
        cv2.circle(image, center, radius, 255, -1, cv2.LINE_AA, SHIFT)
    return image


def homography_from_quad(projector_size: Sequence[int], quad: Sequence[Sequence[float]]) -> np.ndarray:
    """G mapping projector corners (0,0),(W,0),(W,H),(0,H) onto camera `quad`."""

    width, height = projector_size
    source = np.float32([[0, 0], [width, 0], [width, height], [0, height]])
    return cv2.getPerspectiveTransform(source, np.float32(quad)).astype(np.float64)


def make_wall(
    camera_size: Sequence[int],
    *,
    gray: float = 0.8,
    postits: Iterable[tuple[Sequence[Sequence[float]], Sequence[float]]] = (),
) -> np.ndarray:
    """Wall reflectance in camera px (H x W x 3 BGR float32, 0..1).

    `postits` are (polygon in camera px, BGR reflectance) pairs.
    """

    width, height = camera_size
    wall = np.full((height, width, 3), gray, dtype=np.float32)
    for polygon, color in postits:
        points = np.round(np.asarray(polygon, dtype=np.float64)).astype(np.int32)
        cv2.fillPoly(wall, [points], tuple(float(c) for c in color), cv2.LINE_8)
    return wall


def _reflection_image(camera_size: Sequence[int], reflections) -> np.ndarray | None:
    reflections = list(reflections)
    if not reflections:
        return None
    width, height = camera_size
    ys, xs = np.mgrid[0:height, 0:width].astype(np.float32)
    image = np.zeros((height, width), dtype=np.float32)
    for x, y, sigma, peak in reflections:
        image += peak * np.exp(-((xs - x) ** 2 + (ys - y) ** 2) / (2.0 * sigma * sigma))
    return image


def project_to_camera(
    projector_image: np.ndarray,
    G: np.ndarray,
    camera_size: Sequence[int],
    *,
    wall: np.ndarray | None = None,
    ambient: float = 0.2,
    black_level: float = 0.03,
    brightness: float = 1.0,
    gain: float = 0.8,
    blur: float = 0.0,
    gamma: float | None = None,
    noise: float = 0.0,
    jpeg: int | None = None,
    reflections: Iterable[tuple[float, float, float, float]] = (),
    rng: np.random.Generator | None = None,
) -> np.ndarray:
    """Camera BGR uint8 frame of the wall lit by `projector_image`.

    `G` maps projector px to camera px. `black_level` is the light a black
    projector frame still emits; `brightness` scales projector light against
    `ambient`; `gain` is the camera exposure for this frame; `blur` the
    optical Gaussian sigma in camera px; `noise` the sensor sigma in 8-bit
    levels; `reflections` are static (x, y, sigma, peak) highlights in linear
    light, added before the gain so they saturate like a real glare.
    """

    width, height = camera_size
    pattern = projector_image.astype(np.float32) / np.float32(255.0)
    light = cv2.warpPerspective(pattern, G, (width, height), flags=cv2.INTER_LINEAR,
                                borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    if black_level:
        footprint = cv2.warpPerspective(np.ones_like(pattern), G, (width, height),
                                        flags=cv2.INTER_LINEAR,
                                        borderMode=cv2.BORDER_CONSTANT, borderValue=0)
        light = black_level * footprint + (1.0 - black_level) * light
    light = light * np.float32(brightness)
    if blur > 0:
        light = cv2.GaussianBlur(light, (0, 0), blur)
    if wall is None:
        wall = make_wall(camera_size)
    radiance = wall * (np.float32(ambient) + light)[..., None]
    glare = _reflection_image(camera_size, reflections)
    if glare is not None:
        radiance = radiance + glare[..., None]
    value = np.float32(gain) * radiance
    if gamma:
        value = np.power(np.clip(value, 0.0, 1.0), np.float32(1.0 / gamma))
    pixels = value * np.float32(255.0)
    if noise:
        if rng is None:
            raise ValueError("noise needs a seeded rng")
        pixels = pixels + rng.standard_normal(pixels.shape, dtype=np.float32) * np.float32(noise)
    frame = np.clip(np.rint(pixels), 0, 255).astype(np.uint8)
    if jpeg:
        ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, int(jpeg)])
        if not ok:
            raise RuntimeError("JPEG encoding failed")
        frame = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
    return frame


def to_gray(frame_bgr: np.ndarray) -> np.ndarray:
    """Grayscale exactly as the backends compute it (contract 1.3 input)."""

    return cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)


def capture_frames(
    layout: Mapping,
    G: np.ndarray,
    camera_size: Sequence[int],
    *,
    seed: int = 0,
    drift: float = 0.0,
    stale: tuple[int, bool] | None = None,
    blend: Mapping[tuple[int, bool], float] | None = None,
    gain: float = 0.8,
    **options,
) -> dict[tuple[int, bool], np.ndarray]:
    """The 14 grayscale frames keyed by (bit, inverse), as the server stores them.

    `drift` multiplies each frame's gain by a seeded uniform factor in
    [1 - drift, 1 + drift] (auto exposure hunting). `stale=(bit, inverse)`
    films that frame with the other pattern of its pair, as when the camera
    delivers a frame from before the projector updated: both frames of the
    pair then show the same dots. `blend={(bit, inverse): a}` films a frame
    caught mid-transition: a share `a` of the previous pattern in the
    sequence (the warm-up, equal to frame 0, before the first). Other
    keyword options go to `project_to_camera`.
    """

    rng = np.random.default_rng(seed)
    wall = options.pop("wall", None)
    if wall is None:
        wall = make_wall(camera_size)
    blend = dict(blend or {})
    frames = {}
    previous = ALIGNMENT_FRAMES[0]  # warm-up frame
    for bit, inverse in ALIGNMENT_FRAMES:
        shown = (bit, not inverse) if stale == (bit, inverse) else (bit, inverse)
        image = render_pattern(layout, *shown)
        mix = blend.get((bit, inverse), 0.0)
        if mix:
            earlier = render_pattern(layout, *previous).astype(np.float32)
            image = (1.0 - mix) * image.astype(np.float32) + mix * earlier
        frame_gain = gain * (1.0 + rng.uniform(-drift, drift)) if drift else gain
        bgr = project_to_camera(image, G, camera_size,
                                wall=wall, gain=frame_gain, rng=rng, **options)
        frames[(bit, inverse)] = to_gray(bgr)
        previous = (bit, inverse)
    return frames


def truth_errors(
    matrix: Sequence[Sequence[float]],
    G: np.ndarray,
    camera_size: Sequence[int],
    projector_size: Sequence[int],
    *,
    step: int = 8,
) -> np.ndarray:
    """|H(c) - inv(G)(c)| in projector px on a camera grid inside the projection.

    The grid covers the whole camera frame every `step` px; points whose true
    projector position falls outside the projector image are left out.
    """

    width, height = camera_size
    projector_width, projector_height = projector_size
    xs, ys = np.meshgrid(np.arange(0, width, step, dtype=np.float64),
                         np.arange(0, height, step, dtype=np.float64))
    points = np.stack([xs.ravel(), ys.ravel()], axis=1).reshape(-1, 1, 2)
    truth = cv2.perspectiveTransform(points, np.linalg.inv(G)).reshape(-1, 2)
    estimate = cv2.perspectiveTransform(points, np.asarray(matrix, dtype=np.float64)).reshape(-1, 2)
    inside = ((truth[:, 0] >= 0) & (truth[:, 0] <= projector_width - 1)
              & (truth[:, 1] >= 0) & (truth[:, 1] <= projector_height - 1))
    return np.hypot(*(estimate[inside] - truth[inside]).T)
