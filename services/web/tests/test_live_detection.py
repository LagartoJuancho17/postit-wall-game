"""Regressions for the shared camera-coordinate live board and safe attempts."""

from __future__ import annotations

import unittest

import cv2

from services.physics import LAUNCH_SPEED_SHARE
from services.web.backend.eval import FRAME_HUES, _color, _image
from services.web.backend.server import GameController, RequestError


def jpeg(*, markers: int = 0, start: bool = True, green_x: int = 430) -> bytes:
    image = _image(green_x)
    for x, y in ((90, 90), (870, 90), (870, 630), (90, 630)):
        image[y - 26:y + 27, x - 26:x + 27] = 220
    for x in range(markers):
        cv2.rectangle(image, (40 + x * 80, 40), (80 + x * 80, 80),
                      _color(FRAME_HUES["marker"]), -1)
    if not start:
        image[315:386, 145:216] = 220
    okay, data = cv2.imencode('.jpg', image)
    assert okay
    return data.tobytes()


class LiveDetectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = 0.0
        self.game = GameController(clock=lambda: self.now, log_path=None)

    def open(self, width: int = 1280, height: int = 800) -> dict:
        return self.game.action({"action": "projection_open", "width": width, "height": height})

    def launch(self) -> dict:
        self.open()
        self.game.preview_jpeg(jpeg())
        return self.game.action({"action": "launch"})

    def test_projector_opens_live_without_scan_and_has_no_calibration_contract(self) -> None:
        state = self.open()
        self.assertEqual(state['projection']['mode'], 'live')
        self.assertIsNone(state['live_detection'])
        self.assertNotIn('calibration', state)
        self.assertNotIn('target', state['projection'])
        for action in ('calibrate', 'calibration_move', 'calibration_confirm'):
            with self.subTest(action=action), self.assertRaises(RequestError):
                self.game.action({'action': action})

    def test_live_preview_makes_board_ready_without_corners_or_scan(self) -> None:
        for count in (0, 1, 5):
            with self.subTest(markers=count):
                preview = self.game.preview_jpeg(jpeg(markers=count), 'C920')
                state = self.game.snapshot()
                self.assertEqual(state['live_detection'], preview)
                self.assertEqual(state['phase'], 'ready')
                self.assertEqual(state['camera']['label'], 'C920')
                self.assertEqual(state['camera_board'], state['board'])
                self.assertEqual(state['board']['playfield']['points'], [
                    {'x': 0, 'y': 0}, {'x': 960, 'y': 0},
                    {'x': 960, 'y': 720}, {'x': 0, 'y': 720}])
                self.assertEqual(state['board']['start'], preview['roles']['start']['notes'][0])
                self.assertEqual(len(state['board']['markers']), count)
                self.assertIsNone(preview['roles']['marker']['expected'])
                self.assertTrue(preview['quality']['ready'])

    def test_partial_preview_keeps_all_visible_polygons_and_clears_stale_ready_board(self) -> None:
        self.open()
        self.game.preview_jpeg(jpeg())
        self.game.preview_jpeg(jpeg(start=False, markers=1))
        state = self.game.snapshot()
        self.assertEqual(state['live_detection']['roles']['bumper']['count'], 1)
        self.assertEqual(state['live_detection']['roles']['marker']['count'], 1)
        self.assertFalse(state['live_detection']['quality']['ready'])
        self.assertIsNone(state['board'])
        self.assertIsNone(state['camera_board'])
        self.assertEqual(state['phase'], 'waiting_camera')
        for action in ('launch', 'reset'):
            with self.subTest(action=action), self.assertRaises(RequestError):
                self.game.action({'action': action})
        self.game.preview_jpeg(jpeg())
        self.assertEqual(self.game.action({'action': 'launch'})['phase'], 'running')

    def test_live_updates_never_move_ball_or_collision_geometry_mid_attempt(self) -> None:
        running = self.launch()
        self.now += 0.1
        before = self.game.tick()
        preview = self.game.preview_jpeg(jpeg(green_x=540))
        after = self.game.snapshot()
        self.assertEqual(after['board'], running['board'])
        self.assertEqual(after['game'], before['game'])
        self.assertNotEqual(after['camera_board']['bumpers'], after['board']['bumpers'])
        self.assertEqual(after['live_detection'], preview)
        reset = self.game.action({'action': 'reset'})
        self.assertEqual(reset['board'], after['camera_board'])
        self.assertEqual(reset['phase'], 'ready')
        self.assertEqual(reset['game']['attempts'], 1)

    def test_invalid_preview_preserves_attempt_but_refuses_reset(self) -> None:
        running = self.launch()
        self.game.preview_jpeg(jpeg(start=False))
        state = self.game.snapshot()
        self.assertEqual(state['board'], running['board'])
        self.assertEqual(state['phase'], 'running')
        self.assertIsNone(state['camera_board'])
        with self.assertRaises(RequestError):
            self.game.action({'action': 'reset'})
        self.assertEqual(self.game.snapshot()['game'], running['game'])

    def test_paused_attempt_keeps_collision_geometry_until_reset(self) -> None:
        self.launch()
        before = self.game.action({'action': 'pause'})
        self.game.preview_jpeg(jpeg(green_x=540))
        state = self.game.snapshot()
        self.assertEqual(state['board'], before['board'])
        self.assertEqual(state['game'], before['game'])
        self.assertNotEqual(state['camera_board']['bumpers'], state['board']['bumpers'])

    def test_projection_resize_preserves_camera_coordinates_attempt_and_speed(self) -> None:
        running = self.launch()
        velocity = self.game.game.velocity
        resized = self.open(1920, 1080)
        self.assertEqual(resized['board'], running['board'])
        self.assertEqual(resized['game'], running['game'])
        self.assertAlmostEqual((velocity.x ** 2 + velocity.y ** 2) ** 0.5, 720 * LAUNCH_SPEED_SHARE)
        self.assertEqual((resized['game']['width'], resized['game']['height']), (960, 720))

    def test_detection_expires_after_1500ms_and_prevents_stale_launch(self) -> None:
        self.open()
        self.game.preview_jpeg(jpeg())
        self.now = 1.5
        self.assertIsNotNone(self.game.snapshot()['live_detection'])
        self.now = 1.501
        expired = self.game.snapshot()
        self.assertIsNone(expired['live_detection'])
        self.assertIsNone(expired['board'])
        self.assertEqual(expired['phase'], 'waiting_camera')
        with self.assertRaises(RequestError):
            self.game.action({'action': 'launch'})

    def test_expiration_does_not_end_an_active_attempt(self) -> None:
        before = self.launch()
        self.now = 1.501
        expired = self.game.snapshot()
        self.assertIsNone(expired['live_detection'])
        self.assertEqual(expired['board'], before['board'])
        self.assertEqual(expired['game'], before['game'])
        with self.assertRaises(RequestError):
            self.game.action({'action': 'reset'})

    def test_changed_frame_size_keeps_attempt_coordinates_until_reset(self) -> None:
        running = self.launch()
        smaller = cv2.resize(_image(540, markers=False), (480, 360))
        okay, data = cv2.imencode('.jpg', smaller)
        self.assertTrue(okay)
        self.game.preview_jpeg(data.tobytes())
        state = self.game.snapshot()
        self.assertEqual((state['camera']['width'], state['camera']['height']), (480, 360))
        self.assertEqual((state['game']['width'], state['game']['height']), (960, 720))
        self.assertEqual(state['board'], running['board'])
        self.assertEqual(state['game']['position'], running['game']['position'])
        reset = self.game.action({'action': 'reset'})
        self.assertEqual((reset['game']['width'], reset['game']['height']), (480, 360))
        self.assertEqual(reset['board'], reset['camera_board'])

    def test_palette_change_drops_live_candidate_but_keeps_current_attempt(self) -> None:
        running = self.launch()
        changed = self.game.action({'action': 'palette_set', 'role': 'bumper', 'hex': '#2ec4b6'})
        self.assertIsNone(changed['live_detection'])
        self.assertIsNone(changed['camera_board'])
        self.assertEqual(changed['board'], running['board'])
        self.assertEqual(changed['game'], running['game'])
        with self.assertRaises(RequestError):
            self.game.action({'action': 'reset'})

    def test_ambiguous_goal_retains_both_contours_without_a_playable_board(self) -> None:
        image = _image(430, markers=False)
        cv2.rectangle(image, (650, 500), (700, 550), _color(FRAME_HUES['goal']), -1)
        okay, data = cv2.imencode('.jpg', image)
        self.assertTrue(okay)
        preview = self.game.preview_jpeg(data.tobytes())
        self.assertEqual(preview['roles']['goal']['count'], 2)
        self.assertEqual(len(preview['roles']['goal']['notes']), 2)
        self.assertFalse(preview['quality']['ready'])
        self.assertIsNone(self.game.snapshot()['board'])

    def test_returned_preview_and_state_cannot_mutate_shared_detection(self) -> None:
        preview = self.game.preview_jpeg(jpeg())
        preview['roles']['start']['notes'].clear()
        state = self.game.snapshot()
        state['live_detection']['roles']['goal']['notes'].clear()
        latest = self.game.snapshot()['live_detection']
        self.assertEqual(len(latest['roles']['start']['notes']), 1)
        self.assertEqual(len(latest['roles']['goal']['notes']), 1)

    def test_camera_change_clears_both_overlay_and_attempt(self) -> None:
        self.launch()
        state = self.game.action({'action': 'camera_changed'})
        self.assertIsNone(state['live_detection'])
        self.assertIsNone(state['board'])
        self.assertEqual(state['game']['status'], 'none')
        self.game.preview_jpeg(jpeg(), 'C920')
        self.game.action({'action': 'launch'})
        self.game.preview_jpeg(jpeg(green_x=540), 'FaceTime')
        state = self.game.snapshot()
        self.assertEqual(state['camera']['label'], 'FaceTime')
        self.assertEqual(state['phase'], 'ready')

    def test_explicit_scan_is_optional_and_requires_neither_projector_nor_blackout(self) -> None:
        state = self.game.scan_jpeg(jpeg())
        self.assertEqual(state['phase'], 'ready')
        self.assertEqual(state['projection']['mode'], 'live')
        self.open()
        self.assertEqual(self.game.scan_jpeg(jpeg())['phase'], 'ready')


if __name__ == '__main__':
    unittest.main()
