"""Gate tests: USB camera control through the web backend, with a fake bridge."""

from __future__ import annotations

import unittest
from http import HTTPStatus

from services.uvc import UvcError
from services.web.backend.server import GameController, RequestError

STATUS = {"available": True, "device": {"vendor": "046d", "product": "082d", "name": "C920"},
          "capabilities": {"whiteBalanceMode": ["manual", "continuous"]},
          "settings": {"whiteBalanceMode": "continuous"}}


class FakeBridge:
    def __init__(self, fail: str | None = None):
        self.calls: list[tuple] = []
        self.fail = fail

    def status(self, vendor, product):
        self.calls.append(("status", vendor, product))
        if self.fail:
            raise UvcError(self.fail)
        return STATUS

    def apply(self, settings, vendor, product):
        self.calls.append(("apply", settings, vendor, product))
        if self.fail:
            raise UvcError(self.fail)
        return {**STATUS, "settings": {**STATUS["settings"], **settings}, "applied": [["wb_auto", 0]]}


class UsbCameraTests(unittest.TestCase):
    def test_status_comes_from_the_bridge(self) -> None:
        bridge = FakeBridge()
        game = GameController(log_path=None, uvc=bridge)
        self.assertEqual(game.usb_camera("046d", "082d"), STATUS)
        self.assertEqual(bridge.calls, [("status", "046d", "082d")])

    def test_unavailable_usb_is_a_reason_not_an_error(self) -> None:
        game = GameController(log_path=None, uvc=FakeBridge("no se encontró la cámara USB pedida"))
        self.assertEqual(game.usb_camera(None, None),
                         {"available": False, "reason": "no se encontró la cámara USB pedida"})

    def test_apply_accepts_only_manual_settings(self) -> None:
        bridge = FakeBridge()
        game = GameController(log_path=None, uvc=bridge)
        result = game.usb_apply({"vendor": "046d", "product": "082d",
                                 "settings": {"whiteBalanceMode": "manual", "colorTemperature": 4000,
                                              "gain": 0}})
        self.assertEqual(result["settings"]["whiteBalanceMode"], "manual")
        self.assertEqual(bridge.calls[0][1], {"whiteBalanceMode": "manual", "colorTemperature": 4000.0,
                                              "gain": 0.0})
        for settings in ({"whiteBalanceMode": "continuous"}, {"focusMode": "manual", "zoom": 2}, {}):
            with self.subTest(settings=settings):
                with self.assertRaises(RequestError) as caught:
                    game.usb_apply({"settings": settings})
                self.assertEqual(caught.exception.status, HTTPStatus.BAD_REQUEST)
        self.assertEqual(len(bridge.calls), 1)

    def test_a_camera_refusal_is_a_bad_gateway_with_the_reason(self) -> None:
        game = GameController(log_path=None, uvc=FakeBridge("la cámara rechazó focus=999"))
        with self.assertRaises(RequestError) as caught:
            game.usb_apply({"settings": {"focusMode": "manual"}})
        self.assertEqual(caught.exception.status, HTTPStatus.BAD_GATEWAY)
        self.assertIn("rechazó focus=999", str(caught.exception))

    def test_usb_calls_do_not_change_the_game(self) -> None:
        game = GameController(log_path=None, uvc=FakeBridge())
        before = game.snapshot()
        game.usb_camera(None, None)
        game.usb_apply({"settings": {"exposureMode": "manual"}})
        self.assertEqual(game.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
