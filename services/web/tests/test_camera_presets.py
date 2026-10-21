"""Gate tests: fixed camera presets are validated, stored, logged and served."""

from __future__ import annotations

import json
import tempfile
import unittest
from http import HTTPStatus
from pathlib import Path

from services.web.backend.eval import _frame
from services.web.backend.server import GameController, RequestError

C920 = "HD Pro Webcam C920 (046d:082d)"
LOCKED = {"whiteBalanceMode": "manual", "colorTemperature": 4000,
          "exposureMode": "manual", "exposureTime": 200,
          "focusMode": "manual", "focusDistance": 35, "brightness": 128}


class CameraPresetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.path = root / "camera-presets.json"
        self.log_path = root / "events.jsonl"
        self.game = GameController(log_path=self.log_path, camera_presets_path=self.path)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def events(self) -> list[dict]:
        return [json.loads(line) for line in self.log_path.read_text().splitlines()]

    def save(self, **extra) -> dict:
        return self.game.action({"action": "camera_preset_save", "camera": C920,
                                 "settings": LOCKED, **extra})

    def test_snapshot_starts_without_presets(self) -> None:
        self.assertEqual(self.game.snapshot()["camera_presets"], {})

    def test_saved_preset_is_served_persisted_logged_and_restored(self) -> None:
        state = self.save(score=1234.5, black=True, trials=31,
                          capabilities={"focusMode": ["manual", "continuous"]})
        preset = state["camera_presets"][C920]
        self.assertEqual(preset["settings"]["colorTemperature"], 4000.0)
        self.assertEqual(preset["settings"]["exposureMode"], "manual")
        self.assertEqual((preset["score"], preset["black"]), (1234.5, True))
        self.assertIn(C920, state["message"])
        event = self.events()[-1]
        self.assertEqual(event["event"], "camera_preset_saved")
        self.assertEqual((event["trials"], event["capabilities"]["focusMode"][0]), (31, "manual"))
        restarted = GameController(log_path=None, camera_presets_path=self.path)
        self.assertEqual(restarted.snapshot()["camera_presets"], state["camera_presets"])

    def test_nothing_automatic_is_accepted(self) -> None:
        for settings, words in (
            ({**LOCKED, "whiteBalanceMode": "continuous"}, "manual"),
            ({**LOCKED, "focusMode": "single-shot"}, "manual"),
            ({**LOCKED, "exposureTime": float("nan")}, "finito"),
            ({**LOCKED, "exposureTime": "200"}, "finito"),
            ({**LOCKED, "iso": 100}, "desconocido"),
            ({}, "objeto"),
        ):
            with self.subTest(settings=settings):
                with self.assertRaises(RequestError) as caught:
                    self.game.action({"action": "camera_preset_save", "camera": C920,
                                      "settings": settings})
                self.assertEqual(caught.exception.status, HTTPStatus.BAD_REQUEST)
                self.assertIn(words, str(caught.exception))
        self.assertEqual(self.game.snapshot()["camera_presets"], {})
        self.assertFalse(self.path.exists())

    def test_camera_label_is_required_and_bounded(self) -> None:
        for label in (None, "", "   ", "x" * 201, 7):
            with self.subTest(label=label):
                with self.assertRaises(RequestError):
                    self.game.action({"action": "camera_preset_save", "camera": label,
                                      "settings": LOCKED})

    def test_clear_removes_only_that_camera(self) -> None:
        self.save()
        self.game.action({"action": "camera_preset_save", "camera": "FaceTime HD",
                          "settings": {"exposureMode": "manual", "exposureTime": 100}})
        state = self.game.action({"action": "camera_preset_clear", "camera": C920})
        self.assertEqual(list(state["camera_presets"]), ["FaceTime HD"])
        self.assertEqual(list(json.loads(self.path.read_text())["presets"]), ["FaceTime HD"])

    def test_report_is_logged_without_changing_the_game(self) -> None:
        before = self.game.snapshot()
        self.game.action({"action": "camera_report", "camera": C920,
                          "report": {"kind": "applied", "requested": LOCKED,
                                     "actual": {"exposureTime": 200}}})
        self.assertEqual(self.game.snapshot(), before)
        event = self.events()[-1]
        self.assertEqual((event["event"], event["report"]["kind"]), ("camera_report", "applied"))
        with self.assertRaises(RequestError):
            self.game.action({"action": "camera_report", "camera": C920, "report": "x" * 10})
        with self.assertRaises(RequestError):
            self.game.action({"action": "camera_report", "camera": C920,
                              "report": {"blob": "x" * 30_000}})

    def test_damaged_presets_file_is_ignored(self) -> None:
        for content in ("nope", "{}", '{"presets": []}',
                        '{"presets": {"C920": {"settings": {"focusMode": "continuous"}}}}'):
            with self.subTest(content=content):
                self.path.write_text(content)
                game = GameController(log_path=self.log_path, camera_presets_path=self.path)
                self.assertEqual(game.snapshot()["camera_presets"], {})
                self.assertEqual(self.events()[-1]["event"], "camera_presets_ignored")


class ScannedCameraTests(unittest.TestCase):
    """The board remembers which camera scanned it, so a reload keeps it."""

    def test_scan_records_camera_change_forgets_and_projector_resize_preserves_it(self) -> None:
        game = GameController(log_path=None)
        self.assertIsNone(game.snapshot()["camera"]["label"])
        game.action({"action": "projection_open", "width": 1280, "height": 800})
        state = game.scan_jpeg(_frame(430), "HD Pro Webcam C920 (046d:082d)")
        self.assertEqual(state["camera"]["label"], "HD Pro Webcam C920 (046d:082d)")
        self.assertIsNone(game.action({"action": "camera_changed"})["camera"]["label"])
        game.action({"action": "projection_black"})
        game.scan_jpeg(_frame(430), "Cámara de iPhone de Tobías")
        resized = game.action({"action": "projection_open", "width": 1024, "height": 768})
        self.assertEqual(resized["camera"]["label"], "Cámara de iPhone de Tobías")

    def test_a_partial_scan_updates_camera_and_clears_stale_ready_board(self) -> None:
        game = GameController(log_path=None)
        game.action({"action": "projection_open", "width": 1280, "height": 800})
        game.scan_jpeg(_frame(430), "C920")
        game.action({"action": "projection_black"})
        with self.assertRaises(RequestError):
            game.scan_jpeg(_frame(430, {"start": 105, "goal": 176, "bumper": 28, "marker": 140}), "Otra")
        self.assertEqual(game.snapshot()["camera"]["label"], "Otra")
        self.assertIsNone(game.snapshot()["board"])

    def test_an_unreasonable_label_is_refused(self) -> None:
        game = GameController(log_path=None)
        game.action({"action": "projection_open", "width": 1280, "height": 800})
        with self.assertRaises(RequestError):
            game.scan_jpeg(_frame(430), "x" * 201)


class ProjectionRestoreTests(unittest.TestCase):
    """A blackout without a new board (failed scan, camera search) must end."""

    def ready_game(self) -> GameController:
        game = GameController(log_path=None)
        game.action({"action": "projection_open", "width": 1280, "height": 800})
        game.scan_jpeg(_frame(430))
        return game

    def test_game_view_comes_back_after_a_blackout(self) -> None:
        game = self.ready_game()
        self.assertEqual(game.action({"action": "projection_black"})["projection"]["mode"], "black")
        self.assertEqual(game.action({"action": "projection_restore"})["projection"]["mode"], "live")

    def test_live_view_restores_without_a_board_or_projector(self) -> None:
        game = GameController(log_path=None)
        self.assertEqual(game.action({"action": "projection_restore"})["projection"]["mode"], "live")
        game.action({"action": "projection_open", "width": 1280, "height": 800})
        self.assertEqual(game.action({"action": "projection_restore"})["projection"]["mode"], "live")


class PreviewQualityTests(unittest.TestCase):
    def test_preview_reports_quality_for_ranking_camera_settings(self) -> None:
        quality = GameController(log_path=None).preview_jpeg(_frame(430))["quality"]
        self.assertEqual(set(quality), {"ready", "clipped", "dark", "brightness", "sharpness",
                                        "note_saturation"})
        self.assertTrue(quality["ready"])
        self.assertLess(quality["clipped"], 0.01)
        self.assertGreater(quality["sharpness"], 0)


if __name__ == "__main__":
    unittest.main()
