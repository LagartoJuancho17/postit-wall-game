"""The committed state matrix must describe the backend as it behaves today."""

from __future__ import annotations

import json
import unittest

from services.web.contract.matrix import (MATRIX_PATH, PREVIEWS_PATH, build_matrix,
                                          build_previews, shape)

REGENERATE = "Regenerá con: ./.venv/bin/python -m services.web.contract.matrix"
# Fields the UI branches on. Values must match exactly, not just their type.
_ENUM_PATHS = (("phase",), ("game", "status"), ("game", "paused"),
               ("projection", "open"), ("projection", "mode"),
               ("calibration", "status"))


def _dig(value: dict, path: tuple[str, ...]) -> object:
    for key in path:
        value = value[key]
    return value


class StateMatrixContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.live = build_matrix()["phases"]
        cls.committed = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))["phases"]

    def test_committed_matrix_has_the_same_phases(self) -> None:
        self.assertEqual(sorted(self.committed), sorted(self.live), REGENERATE)

    def test_backend_acceptance_matches_committed_matrix(self) -> None:
        for name, phase in self.live.items():
            with self.subTest(phase=name):
                self.assertEqual(self.committed[name]["accepts"], phase["accepts"], REGENERATE)

    def test_snapshot_shape_and_enums_match_committed_matrix(self) -> None:
        for name, phase in self.live.items():
            old = self.committed[name]["state"]
            new = phase["state"]
            with self.subTest(phase=name):
                self.assertEqual(shape(old), shape(new), REGENERATE)
                for path in _ENUM_PATHS:
                    self.assertEqual(_dig(old, path), _dig(new, path),
                                     f"{'.'.join(path)}; {REGENERATE}")

    def test_each_builder_reaches_the_phase_it_names(self) -> None:
        expected = {
            "projector_closed": ("waiting_camera", "none", False, False),
            "waiting_camera": ("waiting_camera", "none", False, True),
            "scanned": ("scanned", "none", False, True),
            "calibrating": ("calibrating", "none", False, True),
            "ready": ("ready", "ready", False, True),
            "ready_closed": ("ready", "ready", False, False),
            "running": ("running", "running", False, True),
            "paused_open": ("paused", "running", True, True),
            "paused_closed": ("paused", "running", True, False),
            "won": ("won", "won", False, True),
            "lost": ("lost", "lost", False, True),
        }
        actual = {name: (p["state"]["phase"], p["state"]["game"]["status"],
                         p["state"]["game"]["paused"], p["state"]["projection"]["open"])
                  for name, p in self.live.items()}
        self.assertEqual(actual, expected)

    def test_committed_preview_samples_match_the_backend(self) -> None:
        live = build_previews()["samples"]
        committed = json.loads(PREVIEWS_PATH.read_text(encoding="utf-8"))["samples"]
        self.assertEqual(sorted(committed), sorted(live), REGENERATE)
        for name, sample in live.items():
            with self.subTest(sample=name):
                old = committed[name]
                self.assertEqual(shape(old), shape(sample), REGENERATE)
                self.assertEqual(old["palette"], sample["palette"], REGENERATE)
                self.assertEqual({r: d["count"] for r, d in old["preview"]["roles"].items()},
                                 {r: d["count"] for r, d in sample["preview"]["roles"].items()},
                                 REGENERATE)
                self.assertEqual(old["scan_ok"], sample["scan_ok"], REGENERATE)

    def test_preview_samples_cover_ok_missing_and_misread_walls(self) -> None:
        samples = build_previews()["samples"]
        counts = {name: {r: d["count"] for r, d in s["preview"]["roles"].items()}
                  for name, s in samples.items()}
        full = {"start": 1, "goal": 1, "bumper": 1, "marker": 4}
        self.assertEqual({name: s["scan_ok"] for name, s in samples.items()},
                         {"full_wall": True, "missing_start": False, "extra_corner": False,
                          "recolored_default_colors": False, "recolored_picked_colors": True})
        self.assertEqual(counts["extra_corner"], {**full, "marker": 5})
        full = {"start": 1, "goal": 1, "bumper": 1, "marker": 4}
        self.assertEqual(counts["full_wall"], full)
        self.assertEqual(counts["missing_start"], {**full, "start": 0})
        self.assertEqual(counts["recolored_default_colors"],
                         {"start": 1, "goal": 0, "bumper": 0, "marker": 5})
        self.assertEqual(counts["recolored_picked_colors"], full)

    def test_shape_ignores_values_but_not_types_or_keys(self) -> None:
        self.assertEqual(shape({"a": 1.5, "b": [{"x": 1}]}), shape({"b": [{"x": 9}], "a": 2}))
        self.assertNotEqual(shape({"a": 1}), shape({"a": "1"}))
        self.assertNotEqual(shape({"a": None}), shape({"a": {}}))
        self.assertNotEqual(shape({"a": True}), shape({"a": 1}))
        self.assertNotEqual(shape({"a": 1}), shape({"a": 1, "b": 1}))


if __name__ == "__main__":
    unittest.main()
