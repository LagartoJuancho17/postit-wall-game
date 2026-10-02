# Calibration service

Converts webcam coordinates to projector coordinates for a flat wall. This
service has no camera or window dependency: the application supplies captured
frames and projector positions, then stores the returned `Matrix3` while the
equipment stays fixed.

## Procedure

1. Freeze webcam focus, exposure and white balance if the camera allows it.
2. Display a black projector frame and capture `dark_bgr`.
3. Project one small white dot at a known projector pixel. Capture `lit_bgr`.
4. Call `detect_projected_dot(dark_bgr, lit_bgr)` to get its webcam pixel.
5. Repeat at well-spread projector positions, at least four and preferably
   8–12. Pair the points in the same order and call `estimate_homography`.
6. Calculate `reprojection_errors` and project a separate check grid. Keep
   the setup only if check points land within half the game ball's radius.
7. Detect the four fuchsia post-its and game post-its in webcam pixels. Call
   `transform_board` before passing the board to physics.

The four fuchsia post-its mark the playable quadrilateral. Their positions
alone cannot determine the camera/projector mapping: the white dots establish
which webcam pixels correspond to known projector pixels. Moving the webcam,
projector, focus, or resolution requires a new calibration. A non-flat wall
cannot be described exactly by one homography.

An unclear or missing projected dot raises `ValueError`; the application should
recapture the frame instead of accepting an uncertain correspondence. OpenCV
RANSAC rejects mismatched observations when more than four pairs are supplied.

## Verification

From the project root, with its dependencies installed:

```sh
python -m unittest services.calibration.tests.test_calibration
python -m services.calibration.evals.synthetic
```

The eval prints measured error as JSON and fails unless the 95th percentile
held-out mapping error and dot center error are each at most one pixel.
