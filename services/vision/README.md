# Vision service

`detect_board(frame_bgr: numpy.ndarray) -> contracts.Board` reads one BGR,
`uint8` webcam frame. Capture it with the projector displaying black and the
post-its stationary. It returns camera-pixel polygons for one yellow start,
one orange goal, zero or more green bumpers, and four fuchsia/violet markers.
The `playfield` is the four marker **centers** (diagonal intersections),
ordered top-left, top-right, bottom-right, bottom-left in the webcam image.
The full marker outlines are
also returned in `markers`. The calibration service maps this board to
projector pixels; vision does not modify projector settings.

The detector uses OpenCV HSV thresholding, 3 px morphological cleanup,
contour area and rectangle-shape filters. Hue bands are orange 5–19, yellow
20–39, green 40–90, fuchsia/violet 125–179 on OpenCV's 0–179 scale. The
vivid-note thresholds are saturation ≥75 and value ≥55. A blob must cover
at least `max(80, 0.00012 × frame area)` pixels. Thus notes need to be
visibly separated and much larger than camera noise. Even lighting and
locked camera exposure improve reliability. On any missing or duplicate
required note, `DetectionError` exposes `color`, `expected`, and `found` and
gives a Spanish user-facing message. Green notes may be added or removed
**between** attempts, followed by a new scan.

From the project root:

```sh
python -m unittest services.vision.tests.test_detector
python -m services.vision.eval
```

The first command is the local gate suite. The second reports a measured
center error and a score across four synthetic lighting/perspective/noise
scenes; every case must pass within 6 camera pixels. These tests do not
establish real-camera color accuracy. Before playing on a real wall, inspect
one captured frame, adjust hue thresholds if needed, and run the projector
alignment check in the calibration service.
