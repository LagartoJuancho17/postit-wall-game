# Runtime

`Session` owns the webcam and the full-screen projector window. It captures
black frames for vision, alternates black and white projected-dot frames for
automatic calibration, then freezes the mapped board geometry for one attempt.
`S` reads the board again; `C` creates a new camera-to-projector homography.

The projector's SDL display index and webcam's OpenCV index are CLI options.
The default is display 1 when a second screen exists and camera 0. Event data
is appended as JSON lines to `runs/events.jsonl` and is never committed.

The tests cover display selection, calibration sample geometry, screen bounds,
and CLI validation. The evaluation runs the contract chain on a synthetic
camera frame. A real wall, webcam, projector and macOS camera permission are
needed for physical acceptance.
