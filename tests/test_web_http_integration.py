"""HTTP checks that use synthetic frames and never open a physical camera."""

from __future__ import annotations

import io
import json
import unittest

import cv2

from services.runtime.tests.fixtures import camera_frame
from services.web.backend.server import GameController, create_server


class _MemorySocket:
    """Enough of a socket for BaseHTTPRequestHandler; never touches the network."""

    def __init__(self, request: bytes):
        self.input = io.BytesIO(request)
        self.output = io.BytesIO()

    def makefile(self, mode: str, _buffering: int = -1):
        return self.input if "r" in mode else self.output

    def sendall(self, data: bytes) -> None:
        self.output.write(data)


class WebHttpIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.port = 8765
        self.server = create_server(
            port=self.port, controller=GameController(log_path=None), bind=False
        )

    def tearDown(self) -> None:
        self.server.server_close()

    def request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, object]]:
        content = body or b""
        all_headers = {
            "Host": f"127.0.0.1:{self.port}",
            "Connection": "close",
            "Content-Length": str(len(content)),
            **(headers or {}),
        }
        head = f"{method} {path} HTTP/1.1\r\n".encode("ascii")
        head += b"".join(f"{key}: {value}\r\n".encode("ascii")
                         for key, value in all_headers.items()) + b"\r\n"
        socket = _MemorySocket(head + content)
        self.server.RequestHandlerClass(socket, ("127.0.0.1", 0), self.server)
        response_head, separator, payload = socket.output.getvalue().partition(b"\r\n\r\n")
        self.assertEqual(separator, b"\r\n\r\n")
        status = int(response_head.split(b"\r\n", 1)[0].split()[1])
        try:
            result = json.loads(payload) if payload else {}
        except json.JSONDecodeError:
            result = {}
        return status, result

    def action(self, name: str, **data: object) -> tuple[int, dict[str, object]]:
        body = json.dumps({"action": name, **data}).encode("utf-8")
        return self.request("POST", "/api/action", body, {
            "Content-Type": "application/json", "X-Postit-Client": "1"
        })

    @staticmethod
    def jpeg(**options: object) -> bytes:
        okay, encoded = cv2.imencode(".jpg", camera_frame(**options))
        if not okay:
            raise AssertionError("Synthetic JPEG encoding failed")
        return encoded.tobytes()

    def test_control_requests_require_local_client_header_and_origin(self) -> None:
        action = json.dumps({"action": "projection_open", "width": 1280, "height": 800}).encode()
        self.assertEqual(
            self.request("POST", "/api/action", action,
                         {"Content-Type": "application/json"})[0],
            403,
        )
        self.assertEqual(
            self.request("POST", "/api/action", action, {
                "Content-Type": "application/json",
                "X-Postit-Client": "1",
                "Origin": "https://attacker.example",
            })[0],
            403,
        )
        self.assertEqual(
            self.request("POST", "/api/action", action, {
                "Content-Type": "application/json",
                "X-Postit-Client": "1",
                "Origin": f"http://127.0.0.1:{self.port}",
            })[0],
            200,
        )

    def test_localhost_works_like_127_0_0_1_but_other_hosts_do_not(self) -> None:
        action = json.dumps({"action": "projection_open", "width": 1280, "height": 800}).encode()
        write = {"Content-Type": "application/json", "X-Postit-Client": "1"}
        self.assertEqual(self.request("GET", "/api/state", headers={"Host": f"localhost:{self.port}"})[0], 200)
        self.assertEqual(self.request("POST", "/api/action", action, {
            **write, "Host": f"localhost:{self.port}", "Origin": f"http://localhost:{self.port}"})[0], 200)
        for host in (f"evil.example:{self.port}", f"localhost:{self.port + 1}", "localhost"):
            with self.subTest(host=host):
                self.assertEqual(self.request("GET", "/api/state", headers={"Host": host})[0], 403)
        for origin in (f"http://localhost:{self.port + 1}", f"http://evil.example:{self.port}",
                       f"https://localhost:{self.port}"):
            with self.subTest(origin=origin):
                self.assertEqual(self.request("POST", "/api/action", action, {**write, "Origin": origin})[0], 403)

    def test_frame_upload_records_the_camera_from_an_encoded_header(self) -> None:
        self.assertEqual(self.action("projection_open", width=1280, height=800)[0], 200)
        status, state = self.request("POST", "/api/frame", self.jpeg(), {
            "Content-Type": "image/jpeg", "X-Postit-Client": "1",
            "X-Postit-Camera": "C%C3%A1mara%20de%20iPhone%20de%20Tob%C3%ADas"})
        self.assertEqual(status, 200)
        self.assertEqual(state["camera"]["label"], "Cámara de iPhone de Tobías")

    def test_frame_upload_rejects_invalid_and_oversize_data(self) -> None:
        self.assertEqual(self.action("projection_open", width=1280, height=800)[0], 200)
        headers = {"Content-Type": "image/jpeg", "X-Postit-Client": "1"}
        self.assertEqual(self.request("POST", "/api/frame", b"not a jpeg", headers)[0], 400)
        self.assertEqual(self.request("POST", "/api/frame", b"x" * (8 * 1024 * 1024 + 1), headers)[0], 413)
        huge_dimensions = (b"\xff\xd8\xff\xc0\x00\x11\x08\xff\xff\xff\xff"
                           b"\x03\x01\x11\x00\x02\x11\x00\x03\x11\x00")
        self.assertEqual(self.request("POST", "/api/frame", huge_dimensions, headers)[0], 413)

    def test_http_live_flow_starts_without_scan_or_calibration_and_resets_latest_board(self) -> None:
        self.assertEqual(self.request("GET", "/api/state")[0], 200)
        self.assertEqual(self.action("projection_open", width=1280, height=800)[0], 200)
        headers = {"Content-Type": "image/jpeg", "X-Postit-Client": "1"}
        status, preview = self.request("POST", "/api/preview", self.jpeg(), headers)
        self.assertEqual(status, 200)
        status, state = self.request("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(state["live_detection"], preview)
        self.assertEqual(state["game"]["status"], "ready")
        self.assertNotIn("calibration", state)
        self.assertEqual(state["board"], state["camera_board"])
        self.assertEqual(len(state["board"]["bumpers"]), 1)
        self.assertEqual(self.action("calibrate")[0], 400)

        self.assertEqual(self.action("launch")[1]["game"]["status"], "running")
        self.assertEqual(self.request("POST", "/api/frame", self.jpeg(), headers)[0], 409)
        before = state["board"]["bumpers"][0]["points"]
        status, _ = self.request("POST", "/api/preview", self.jpeg(green_center=(515, 245)), headers)
        self.assertEqual(status, 200)
        running = self.request("GET", "/api/state")[1]
        self.assertEqual(running["board"]["bumpers"][0]["points"], before)
        status, updated = self.action("reset")
        self.assertEqual(status, 200)
        after = updated["board"]["bumpers"][0]["points"]
        self.assertGreater(abs(after[0]["x"] - before[0]["x"]), 100)
        self.assertEqual(updated["game"]["status"], "ready")
        self.assertEqual(updated["game"]["attempts"], 1)

    def test_preview_route_checks_client_type_and_size_then_reports_roles(self) -> None:
        headers = {"Content-Type": "image/jpeg", "X-Postit-Client": "1"}
        self.assertEqual(self.request("POST", "/api/preview", self.jpeg(),
                                      {"Content-Type": "image/jpeg"})[0], 403)
        self.assertEqual(self.request("POST", "/api/preview", self.jpeg(), {
            "Content-Type": "application/json", "X-Postit-Client": "1"})[0], 415)
        self.assertEqual(self.request("POST", "/api/preview",
                                      b"x" * (8 * 1024 * 1024 + 1), headers)[0], 413)
        status, preview = self.request("POST", "/api/preview", self.jpeg(), headers)
        self.assertEqual(status, 200)
        self.assertEqual({role: data["count"] for role, data in preview["roles"].items()},
                         {"start": 1, "goal": 1, "bumper": 1, "marker": 4})
        state = self.request("GET", "/api/state")[1]
        self.assertEqual(state["phase"], "ready")
        self.assertEqual(state["live_detection"], preview)

    def test_palette_actions_over_http(self) -> None:
        status, state = self.action("palette_set", role="goal", hex="#2ec4b6")
        self.assertEqual(status, 200)
        self.assertTrue(state["palette"]["roles"]["goal"]["custom"])
        status, refused = self.action("palette_set", role="goal", hex="nope")
        self.assertEqual(status, 400)
        self.assertIn("#rrggbb", refused["error"])
        self.assertTrue(refused["state"]["palette"]["roles"]["goal"]["custom"])
        status, state = self.action("palette_reset")
        self.assertEqual(status, 200)
        self.assertFalse(state["palette"]["custom"])

    def test_usb_camera_routes_read_and_fix_settings(self) -> None:
        from services.web.tests.test_usb_camera import FakeBridge
        bridge = FakeBridge()
        self.server.server_close()  # replaced by one whose USB bridge is fake
        self.server = create_server(port=self.port, controller=GameController(log_path=None, uvc=bridge),
                                    bind=False)
        status, camera = self.request("GET", "/api/camera?vendor=046d&product=082d")
        self.assertEqual((status, camera["available"]), (200, True))
        self.assertEqual(bridge.calls[0], ("status", "046d", "082d"))
        body = json.dumps({"vendor": "046d", "product": "082d",
                           "settings": {"focusMode": "manual"}}).encode()
        self.assertEqual(self.request("POST", "/api/camera", body,
                                      {"Content-Type": "application/json"})[0], 403)
        status, applied = self.request("POST", "/api/camera", body, {
            "Content-Type": "application/json", "X-Postit-Client": "1"})
        self.assertEqual(status, 200)
        self.assertEqual(applied["settings"]["focusMode"], "manual")
        auto = json.dumps({"settings": {"focusMode": "continuous"}}).encode()
        self.assertEqual(self.request("POST", "/api/camera", auto, {
            "Content-Type": "application/json", "X-Postit-Client": "1"})[0], 400)

    def test_camera_change_clears_detection_and_projector_resize_keeps_board(self) -> None:
        headers = {"Content-Type": "image/jpeg", "X-Postit-Client": "1"}
        self.assertEqual(self.action("projection_open", width=1280, height=800)[0], 200)
        status, state = self.request("POST", "/api/frame", self.jpeg(), headers)
        self.assertEqual(status, 200)
        self.assertEqual(state["phase"], "ready")
        status, resized = self.action("projection_open", width=1024, height=768)
        self.assertEqual(status, 200)
        self.assertEqual(resized["board"], state["board"])
        self.assertEqual(resized["live_detection"], state["live_detection"])
        self.assertEqual(resized["projection"]["mode"], "live")

        status, changed = self.action("camera_changed")
        self.assertEqual(status, 200)
        self.assertIsNone(changed["camera_board"])
        self.assertIsNone(changed["board"])
        self.assertIsNone(changed["live_detection"])
        self.assertEqual(changed["projection"]["mode"], "live")
        self.assertEqual(self.action("launch")[0], 409)

    def test_alignment_and_check_routes_over_http(self) -> None:
        jpeg = {"Content-Type": "image/jpeg", "X-Postit-Client": "1"}
        # Same client, content type and origin rules as every other upload.
        status, payload = self.request("POST", "/api/align_frame", self.jpeg(),
                                       {"Content-Type": "image/jpeg", "X-Postit-Align": "0:0"})
        self.assertEqual(status, 403)
        status, payload = self.request("POST", "/api/align_frame", b"{}",
                                       {**jpeg, "Content-Type": "application/json", "X-Postit-Align": "0:0"})
        self.assertEqual((status, payload["error"]), (415, "Content-Type esperado: image/jpeg."))
        status, payload = self.request("POST", "/api/align_frame", self.jpeg(), {**jpeg, "X-Postit-Align": "0:0"})
        self.assertEqual((status, payload["error"]), (409, "No hay una alineación en curso."))
        self.assertEqual(payload["state"]["alignment"]["status"], "none")
        status, payload = self.request("POST", "/api/align_frame", self.jpeg(), {**jpeg, "X-Postit-Align": "9:9"})
        self.assertEqual((status, payload["error"]), (400, "Cabecera X-Postit-Align inválida."))
        status, payload = self.request("POST", "/api/check_frame", self.jpeg(), {**jpeg, "X-Postit-Check": "x"})
        self.assertEqual((status, payload["error"]), (400, "Cabecera X-Postit-Check inválida."))
        status, payload = self.request("POST", "/api/check_frame", self.jpeg(), {**jpeg, "X-Postit-Check": "neutral"})
        self.assertEqual((status, payload["error"]), (409, "Primero alineá el proyector."))
        # A run in progress takes frames of the camera's size.
        self.action("projection_open", width=1280, height=720)
        self.request("POST", "/api/preview", self.jpeg(), {**jpeg, "X-Postit-Camera": "C%C3%A1mara"})
        status, state = self.action("align_start")
        self.assertEqual((status, state["alignment"]["status"]), (200, "collecting"))
        status, payload = self.request("POST", "/api/align_frame", self.jpeg(),
                                       {**jpeg, "X-Postit-Align": "3:1", "X-Postit-Camera": "C%C3%A1mara"})
        self.assertEqual((status, payload), (200, {"received": 1, "needed": 14}))
        status, state = self.action("align_cancel")
        self.assertEqual(state["alignment"]["status"], "none")

    def test_preview_records_camera_label_and_partial_overlay_even_when_board_invalid(self) -> None:
        headers = {"Content-Type": "image/jpeg", "X-Postit-Client": "1",
                   "X-Postit-Camera": "C%C3%A1mara%20de%20iPhone%20de%20Tob%C3%ADas"}
        status, preview = self.request("POST", "/api/preview", self.jpeg(), headers)
        self.assertEqual(status, 200)
        self.assertEqual(self.request("GET", "/api/state")[1]["camera"]["label"],
                         "Cámara de iPhone de Tobías")
        from services.web.tests.test_live_detection import jpeg
        status, preview = self.request("POST", "/api/preview", jpeg(start=False), headers)
        self.assertEqual(status, 200)
        self.assertEqual(preview["roles"]["bumper"]["count"], 1)
        state = self.request("GET", "/api/state")[1]
        self.assertEqual(state["live_detection"], preview)
        self.assertIsNone(state["board"])
        self.assertEqual(self.action("launch")[0], 409)


if __name__ == "__main__":
    unittest.main()
