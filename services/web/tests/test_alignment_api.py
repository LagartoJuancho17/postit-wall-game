"""Gate tests: camera <-> projector alignment, neutral projection and the
overlay check in the web backend (docs/alignment-and-color.md 1.4, 1.5, 2.2,
2.3 and 4). Frames are 320x240 so the whole file stays fast."""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

import cv2
import numpy as np

from services.calibration import ALIGNMENT_FRAMES, ALIGNMENT_REASONS
from services.calibration.tests.render import homography_from_quad, project_to_camera, render_pattern
from services.vision.tests.scene import bgr
from services.web.backend import server
from services.web.backend.server import GameController, RequestError, place_probes, probe_side

CAMERA = (320, 240)
PROJECTOR = (320, 240)
QUAD = ((20, 14), (300, 10), (306, 228), (14, 232))   # projector corners in the camera
LABEL = "Cámara de prueba"
HUES = {"start": 30, "goal": 12, "bumper": 65}


def _jpeg(frame: np.ndarray) -> bytes:
    okay, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 96])
    assert okay
    return encoded.tobytes()


def wall_frame(notes=(("start", 90, 120), ("goal", 230, 120)), extra=()) -> np.ndarray:
    """Gray wall with 24x24 px notes centered at (x, y) in camera px."""
    frame = np.full((CAMERA[1], CAMERA[0], 3), 205, np.uint8)
    for role, x, y in (*notes, *extra):
        cv2.rectangle(frame, (x - 12, y - 12), (x + 12, y + 12), bgr(HUES[role]), -1)
    return frame


class Rig:
    def __init__(self, case: unittest.TestCase) -> None:
        self.now = 0.0
        self.directory = tempfile.TemporaryDirectory()
        case.addCleanup(self.directory.cleanup)
        self.log = Path(self.directory.name) / "events.jsonl"
        self.game = GameController(log_path=self.log, clock=lambda: self.now)

    def events(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def act(self, name: str, **data):
        return self.game.action({"action": name, **data})

    def open(self, size=PROJECTOR):
        return self.act("projection_open", width=size[0], height=size[1])

    def preview(self, frame=None, label=LABEL):
        return self.game.preview_jpeg(_jpeg(wall_frame() if frame is None else frame), label)

    def feed(self, layout, *, black=False, quad=QUAD):
        G = homography_from_quad(PROJECTOR, quad)
        for bit, inverse in ALIGNMENT_FRAMES:
            pattern = np.zeros_like(render_pattern(layout, bit, inverse)) if black \
                else render_pattern(layout, bit, inverse)
            frame = project_to_camera(pattern, G, CAMERA)
            self.game.align_frame_jpeg(_jpeg(frame), f"{bit}:{int(inverse)}", LABEL)
        return G

    def align(self):
        self.open()
        self.preview()
        layout = self.act("align_start")["alignment"]["layout"]
        G = self.feed(layout)
        return self.act("align_solve"), G


class AlignmentLifecycleTests(unittest.TestCase):
    def test_success_recovers_the_true_mapping_and_moves_the_game_to_projector_px(self):
        rig = Rig(self)
        state, G = rig.align()
        alignment = state["alignment"]
        self.assertEqual(alignment["status"], "ready")
        self.assertIsNone(alignment["layout"])
        self.assertEqual(alignment["camera"], {"label": LABEL, "width": 320, "height": 240})
        self.assertEqual(alignment["projector"], {"width": 320, "height": 240})
        self.assertEqual(alignment["needed"], 14)
        self.assertEqual(alignment["received"], 0)
        self.assertEqual(set(alignment["quality"]), {"decoded", "total", "fit", "inliers", "holdout",
                                                     "holdout_p95_px", "holdout_max_px", "outliers", "coverage"})
        # H maps camera px to projector px: compare with inv(G) inside the projection.
        H = np.array(alignment["matrix"])
        truth = np.linalg.inv(G)
        points = np.float32([[x, y] for x in (60, 160, 260) for y in (50, 120, 190)]).reshape(-1, 1, 2)
        error = np.linalg.norm(cv2.perspectiveTransform(points, H) - cv2.perspectiveTransform(points, truth), axis=2)
        self.assertLess(float(error.max()), 1.0)
        self.assertEqual(state["projection"]["mode"], "live")
        self.assertTrue(state["message"].startswith("Proyector alineado: error "))
        self.assertEqual(alignment["overlay"]["status"], "unchecked")
        self.assertFalse(alignment["overlay"]["fill"])
        self.assertEqual(alignment["overlay"]["detail"], "El relleno de color todavía no se verificó.")
        # The next live frame builds the board in projector px.
        state = rig.preview() and rig.game.snapshot()
        self.assertEqual(state["game"]["space"], "projector")
        self.assertEqual((state["game"]["width"], state["game"]["height"]), PROJECTOR)
        start = state["board"]["start"]["points"]
        camera_start = state["camera_board"]["start"]["points"]
        mapped = cv2.perspectiveTransform(np.float32([[p["x"], p["y"]] for p in camera_start]).reshape(-1, 1, 2), H)
        self.assertLess(float(np.abs(mapped.reshape(-1, 2) - np.float32([[p["x"], p["y"]] for p in start])).max()), 0.01)
        self.assertIn("alignment_ready", [event["event"] for event in rig.events()])

    def test_collecting_state_shows_the_layout_and_counts_frames(self):
        rig = Rig(self)
        rig.open()
        rig.preview()
        state = rig.act("align_start")
        self.assertEqual(state["alignment"]["status"], "collecting")
        self.assertEqual(state["projection"]["mode"], "align")
        self.assertEqual(state["message"], "Alineando cámara y proyector…")
        layout = state["alignment"]["layout"]
        self.assertEqual(len(layout["frames"]), 14)
        G = homography_from_quad(PROJECTOR, QUAD)
        frame = _jpeg(project_to_camera(render_pattern(layout, 0, False), G, CAMERA))
        self.assertEqual(rig.game.align_frame_jpeg(frame, "0:0"), {"received": 1, "needed": 14})
        # The same key replaces its frame: memory stays bounded at 14.
        self.assertEqual(rig.game.align_frame_jpeg(frame, "0:0"), {"received": 1, "needed": 14})
        self.assertEqual(rig.game.snapshot()["alignment"]["received"], 1)

    def test_failure_reports_the_decoder_reason_and_never_leaves_the_projector_patterned(self):
        rig = Rig(self)
        rig.open()
        rig.preview()
        layout = rig.act("align_start")["alignment"]["layout"]
        rig.feed(layout, black=True)
        state = rig.act("align_solve")
        self.assertEqual(state["alignment"]["status"], "failed")
        self.assertIsNone(state["alignment"]["matrix"])
        self.assertEqual(state["projection"]["mode"], "live")
        self.assertTrue(state["alignment"]["detail"].startswith("La cámara vio solo 0 de"))
        self.assertEqual(state["message"], state["alignment"]["detail"])
        failed = [event for event in rig.events() if event["event"] == "alignment_failed"]
        self.assertEqual(failed[-1]["code"], "few_points")

    def test_missing_frames_fail_with_the_frames_reason(self):
        rig = Rig(self)
        rig.open()
        rig.preview()
        rig.act("align_start")
        state = rig.act("align_solve")
        self.assertEqual(state["alignment"]["status"], "failed")
        self.assertEqual(state["alignment"]["detail"], ALIGNMENT_REASONS["frames"])

    def test_refusals_carry_the_contract_texts(self):
        rig = Rig(self)
        cases = [
            (lambda: rig.act("align_start"), 409, "Abrí la ventana del proyector con J."),
            (lambda: rig.act("align_solve"), 409, "No hay una alineación en curso."),
            (lambda: rig.game.align_frame_jpeg(_jpeg(wall_frame()), "0:0"), 409, "No hay una alineación en curso."),
            (lambda: rig.game.align_frame_jpeg(_jpeg(wall_frame()), "7:0"), 400, "Cabecera X-Postit-Align inválida."),
            (lambda: rig.game.align_frame_jpeg(_jpeg(wall_frame()), None), 400, "Cabecera X-Postit-Align inválida."),
            (lambda: rig.game.check_frame_jpeg(_jpeg(wall_frame()), "luz"), 400, "Cabecera X-Postit-Check inválida."),
            (lambda: rig.game.check_frame_jpeg(_jpeg(wall_frame()), "neutral"), 409, "Primero alineá el proyector."),
            (lambda: rig.act("check_solve"), 409, "Primero alineá el proyector."),
        ]
        for call, status, text in cases:
            with self.assertRaises(RequestError) as caught:
                call()
            self.assertEqual((int(caught.exception.status), str(caught.exception)), (status, text))
        rig.open()
        with self.assertRaises(RequestError) as caught:
            rig.act("align_start")
        self.assertEqual(str(caught.exception), "Primero tiene que llegar imagen de la cámara.")
        rig.preview()
        rig.act("launch")
        with self.assertRaises(RequestError) as caught:
            rig.act("align_start")
        self.assertEqual(str(caught.exception), "Terminá o pausá el intento antes de alinear.")

    def test_a_frame_of_another_size_is_refused(self):
        rig = Rig(self)
        rig.open()
        rig.preview()
        rig.act("align_start")
        big = np.zeros((300, 400, 3), np.uint8)
        with self.assertRaises(RequestError) as caught:
            rig.game.align_frame_jpeg(_jpeg(big), "0:0")
        self.assertEqual(str(caught.exception), "El cuadro de alineación no coincide con el tamaño de la cámara.")

    def test_cancel_and_closing_the_projector_end_a_run(self):
        rig = Rig(self)
        rig.open()
        rig.preview()
        rig.act("align_start")
        state = rig.act("align_cancel")
        self.assertEqual((state["alignment"]["status"], state["projection"]["mode"]), ("none", "live"))
        rig.act("align_start")
        state = rig.act("projection_close")
        self.assertEqual(state["alignment"]["status"], "none")
        self.assertEqual(state["alignment"]["detail"], "Alineación cancelada: se cerró el proyector.")


class InvalidationTests(unittest.TestCase):
    def _stale(self, change, reason):
        rig = Rig(self)
        rig.align()
        state = change(rig)
        self.assertEqual(state["alignment"]["status"], "stale", reason)
        self.assertIsNone(state["alignment"]["matrix"])
        self.assertEqual(state["alignment"]["detail"], server.STALE_DETAILS[reason])
        self.assertEqual(state["game"]["space"], "camera")
        logged = [event for event in rig.events() if event["event"] == "alignment_invalidated"]
        self.assertEqual(logged[-1]["reason"], reason)

    def test_projector_resize(self):
        self._stale(lambda rig: rig.open((640, 480)), "projector_resized")

    def test_camera_changed(self):
        self._stale(lambda rig: rig.act("camera_changed"), "camera_changed")

    def test_another_camera_label(self):
        self._stale(lambda rig: rig.preview(label="Otra cámara") and rig.game.snapshot(), "camera_label")

    def test_another_camera_size(self):
        frame = cv2.resize(wall_frame(), (400, 300))
        self._stale(lambda rig: rig.preview(frame) and rig.game.snapshot(), "camera_size")

    def test_same_size_reopen_keeps_the_alignment(self):
        rig = Rig(self)
        rig.align()
        self.assertEqual(rig.open()["alignment"]["status"], "ready")


class NeutralAndOverlayTests(unittest.TestCase):
    def test_projection_neutral_follows_the_blackout_rule(self):
        rig = Rig(self)
        rig.open()
        self.assertEqual(rig.act("projection_neutral")["projection"]["mode"], "neutral")
        self.assertEqual(rig.act("projection_restore")["projection"]["mode"], "live")
        rig.preview()
        rig.act("launch")
        with self.assertRaises(RequestError) as caught:
            rig.act("projection_neutral")
        self.assertEqual(int(caught.exception.status), 409)

    def test_probes_sit_on_bare_wall_inside_the_camera_view(self):
        rig = Rig(self)
        state, _ = rig.align()
        probes = state["alignment"]["overlay"]["probes"]
        self.assertEqual([probe["role"] for probe in probes], ["start", "goal", "bumper", "marker"])
        side = probe_side(*PROJECTOR)
        H = np.array(state["alignment"]["matrix"])
        notes = [note for role in state["live_detection"]["roles"].values() for note in role["notes"]]
        for probe in probes:
            xs = [p["x"] for p in probe["points"]]
            ys = [p["y"] for p in probe["points"]]
            self.assertAlmostEqual(max(xs) - min(xs), side)
            for note in notes:
                mapped = cv2.perspectiveTransform(np.float32([[p["x"], p["y"]] for p in note["points"]]).reshape(-1, 1, 2), H)
                nx, ny = mapped[:, 0, 0], mapped[:, 0, 1]
                separated = (max(xs) <= nx.min() - side or nx.max() + side <= min(xs) or
                             max(ys) <= ny.min() - side or ny.max() + side <= min(ys))
                self.assertTrue(separated, probe["role"])

    def test_probe_placement_is_deterministic_and_skips_unmappable_notes(self):
        H = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
        first = place_probes(None, H, (320, 240), (320, 240))
        self.assertEqual(first, place_probes(None, H, (320, 240), (320, 240)))
        self.assertEqual(len(first), 4)
        crowded = {"roles": {"start": {"notes": [{"points": [{"x": 0, "y": 0}, {"x": 320, "y": 0},
                                                             {"x": 320, "y": 240}, {"x": 0, "y": 240}]}]}}}
        self.assertEqual(place_probes(crowded, H, (320, 240), (320, 240)), [])

    def _check(self, rig, overlay_frame):
        rig.game.check_frame_jpeg(_jpeg(wall_frame()), "neutral")
        self.assertEqual(rig.game.check_frame_jpeg(_jpeg(overlay_frame), "overlay"),
                         {"received": ["neutral", "overlay"]})
        return rig.act("check_solve")

    def test_same_detection_turns_the_fill_on(self):
        rig = Rig(self)
        rig.align()
        overlay = self._check(rig, wall_frame())["alignment"]["overlay"]
        self.assertEqual((overlay["status"], overlay["fill"], overlay["detail"]),
                         ("ok", True, "Relleno de color verificado con la cámara."))
        logged = [event for event in rig.events() if event["event"] == "overlay_check"][-1]
        self.assertEqual((logged["result"], logged["probes_hit"]), ("ok", []))

    def test_a_probe_seen_as_a_post_it_turns_the_fill_off(self):
        rig = Rig(self)
        state, G = rig.align()
        probe = state["alignment"]["overlay"]["probes"][1]   # the goal probe
        cx = sum(p["x"] for p in probe["points"]) / 4
        cy = sum(p["y"] for p in probe["points"]) / 4
        (x, y), = cv2.perspectiveTransform(np.float32([[[cx, cy]]]), G)[0]
        overlay = self._check(rig, wall_frame(extra=(("goal", int(round(x)), int(round(y))),)))
        overlay = overlay["alignment"]["overlay"]
        self.assertEqual((overlay["status"], overlay["fill"]), ("fill_off", False))
        self.assertEqual(overlay["detail"], "La cámara toma el color proyectado sobre la pared por un post-it "
                                            "de llegada; se proyectan solo bordes.")

    def test_a_changed_count_turns_the_fill_off(self):
        rig = Rig(self)
        rig.align()
        overlay = self._check(rig, wall_frame(notes=(("start", 90, 120),)))["alignment"]["overlay"]
        self.assertEqual(overlay["detail"], "El color proyectado cambiaba la detección (llegada: 1 → 0); "
                                            "se proyectan solo bordes.")

    def test_check_needs_both_frames_and_clears_them(self):
        rig = Rig(self)
        rig.align()
        rig.game.check_frame_jpeg(_jpeg(wall_frame()), "neutral")
        with self.assertRaises(RequestError) as caught:
            rig.act("check_solve")
        self.assertEqual(str(caught.exception), "Faltan los cuadros de verificación.")
        self._check(rig, wall_frame())
        with self.assertRaises(RequestError):
            rig.act("check_solve")


class RimBridgeTests(unittest.TestCase):
    """Contract 2.5: the backend discounts the rims its projector draws."""

    @staticmethod
    def _frames():
        before = np.full((240, 320, 3), 205, np.uint8)
        cv2.rectangle(before, (100, 80), (160, 120), bgr(HUES["bumper"]), -1)
        full = np.full((240, 320, 3), 205, np.uint8)
        cv2.rectangle(full, (100, 80), (173, 120), bgr(HUES["bumper"]), -1)
        # Unaligned, projector size == camera size: the contain fit is the
        # identity, so the rim of the earlier note lies 6 px right of x = 160
        # and cuts a 5 px strip (not a note by itself) off the paper.
        cv2.line(full, (166, 70), (166, 130), (255, 255, 255), 3)
        return before, full

    def test_a_strip_our_rim_cut_off_stays_part_of_the_paper(self):
        rig = Rig(self)
        rig.open()
        before, full = self._frames()
        self.assertEqual(rig.preview(before)["roles"]["bumper"]["count"], 1)
        preview = rig.preview(full)
        self.assertEqual(preview["roles"]["bumper"]["count"], 1)
        xs = [p["x"] for p in preview["roles"]["bumper"]["notes"][0]["points"]]
        self.assertLess(abs(min(xs) - 100), 1.5)
        self.assertLess(abs(max(xs) - 173), 1.5)

    def test_without_our_rim_drawn_nothing_is_discounted(self):
        rig = Rig(self)
        rig.open()
        before, full = self._frames()
        rig.preview(before)
        rig.act("projection_neutral")   # the projector draws no rims now
        xs = [p["x"] for p in rig.preview(full)["roles"]["bumper"]["notes"][0]["points"]]
        self.assertLess(max(xs), 166, "the strip is lost without the bridge")

    def test_two_close_papers_never_flip_through_the_live_loop(self):
        """Regression (review, 2026-10-21): bumpers 5-8 px apart counted
        2, 1, 2, 1... because the bridge joined them across our rim."""
        for gap in range(3, 11):
            rig = Rig(self)
            rig.open()
            frame = np.full((240, 320, 3), 205, np.uint8)
            cv2.rectangle(frame, (100, 80), (140, 120), bgr(HUES["bumper"]), -1)
            cv2.rectangle(frame, (141 + gap, 80), (181 + gap, 120), bgr(HUES["bumper"]), -1)
            counts = [rig.preview(frame)["roles"]["bumper"]["count"] for _ in range(6)]
            self.assertEqual(len(set(counts)), 1, f"gap {gap}: {counts}")
            # Below 5 px the JPEG's half-resolution chroma already joins them, steadily.
            if gap >= 5:
                self.assertEqual(counts[0], 2, gap)


class HotPathTests(unittest.TestCase):
    def test_a_tick_never_waits_for_a_detection(self):
        rig = Rig(self)
        rig.open()
        rig.preview()
        rig.act("launch")
        original = server.find_notes
        detected = threading.Event()
        outcome = []

        def slow(frame, palette, bridge=None):
            detected.set()
            time.sleep(0.25)
            return original(frame, palette, bridge)

        def preview():
            try:
                outcome.append(rig.preview())
            except Exception as exc:  # reported below, on the test thread
                outcome.append(exc)

        server.find_notes = slow
        try:
            worker = threading.Thread(target=preview)
            worker.start()
            self.assertTrue(detected.wait(2.0), "the preview never reached the detection")
            started = time.perf_counter()
            rig.game.tick()
            waited = time.perf_counter() - started
            worker.join()
        finally:
            server.find_notes = original
        self.assertLess(waited, 0.1)
        # The detection really ran (a stub that raised would pass vacuously).
        self.assertIsInstance(outcome[0], dict, outcome[0])

    def test_another_camera_label_discards_an_attempt_and_says_so(self):
        rig = Rig(self)
        rig.open()
        rig.preview()
        rig.act("launch")
        state = rig.game.preview_jpeg(_jpeg(wall_frame()), "Otra cámara") and rig.game.snapshot()
        self.assertIn("attempt_discarded", [event["event"] for event in rig.events()])
        self.assertTrue(any(event.get("reason") == "camera_label" for event in rig.events()
                            if event["event"] == "attempt_discarded"))
        self.assertNotEqual(state["phase"], "running")

    def test_physics_refusals_reach_the_operator_in_spanish(self):
        rig = Rig(self)
        rig.open()
        rig.preview()
        # The goal covers the start's right edge: the start's centroid ends
        # up 8.5 px from the goal, inside the 10 px ball radius.
        rig.preview(wall_frame(notes=(("start", 120, 120), ("goal", 136, 120))))
        state = rig.game.snapshot()
        self.assertEqual(state["message"], "El inicio toca la llegada. Separalos.")
        invalid = [event for event in rig.events() if event["event"] == "live_board_invalid"]
        self.assertEqual(invalid[-1]["reason"], "El inicio toca la llegada. Separalos.")


if __name__ == "__main__":
    unittest.main()


class RunRobustnessTests(unittest.TestCase):
    """Regressions from the review of 2026-10-21 (backend)."""

    @staticmethod
    def _boxes(state):
        return [(min(p["x"] for p in probe["points"]), min(p["y"] for p in probe["points"]),
                 max(p["x"] for p in probe["points"]), max(p["y"] for p in probe["points"]))
                for probe in state["alignment"]["overlay"]["probes"]]

    def test_a_slow_run_keeps_the_detection_so_probes_avoid_the_papers(self):
        """Previews pause during a run longer than the 1.5 s detection life:
        the probes were placed as if the wall were bare, one on a paper."""
        quick, slow = Rig(self), Rig(self)
        state_quick, _ = quick.align()
        slow.open()
        slow.preview()
        layout = slow.act("align_start")["alignment"]["layout"]
        G = homography_from_quad(PROJECTOR, QUAD)
        for bit, inverse in ALIGNMENT_FRAMES:
            slow.now += 0.5    # 7 s in all, as with an automatic camera
            frame = project_to_camera(render_pattern(layout, bit, inverse), G, CAMERA)
            slow.game.align_frame_jpeg(_jpeg(frame), f"{bit}:{int(inverse)}", LABEL)
        state_slow = slow.act("align_solve")
        self.assertIsNotNone(state_slow["live_detection"], "the detection is held through the run")
        self.assertEqual(self._boxes(state_slow), self._boxes(state_quick))
        # The overlay check comes next: still held after its frames.
        slow.now += 1.0
        slow.game.check_frame_jpeg(_jpeg(wall_frame()), "neutral")
        slow.now += 1.0
        self.assertIsNotNone(slow.game.snapshot()["live_detection"])
        slow.game.check_frame_jpeg(_jpeg(wall_frame()), "overlay")
        checked = slow.act("check_solve")
        self.assertEqual(checked["alignment"]["overlay"]["status"], "ok", checked["alignment"]["overlay"]["detail"])
        # After the check the normal life applies again.
        slow.now += 2.0
        self.assertIsNone(slow.game.snapshot()["live_detection"])

    def test_an_abandoned_run_is_cancelled_and_never_leaves_patterns_up(self):
        rig = Rig(self)
        rig.open()
        rig.preview()
        rig.act("align_start")
        self.assertEqual(rig.open()["projection"]["mode"], "align", "a same-size reopen keeps the patterns")
        self.assertEqual(rig.act("projection_restore")["projection"]["mode"], "align")
        for name in ("launch", "resume", "projection_black", "projection_neutral"):
            with self.assertRaises(RequestError) as caught:
                rig.act(name)
            self.assertEqual(str(caught.exception), "Esperá a que termine la alineación.", name)
        rig.now += 21.0   # the panel reloaded mid-run
        state = rig.game.snapshot()
        self.assertEqual(state["alignment"]["status"], "none")
        self.assertEqual(state["projection"]["mode"], "live")
        self.assertIn("se interrumpió", state["alignment"]["detail"])
        self.assertTrue(any(e.get("reason") == "stalled" for e in rig.events() if e["event"] == "alignment_cancelled"))

    def test_check_frames_must_match_the_aligned_camera(self):
        rig = Rig(self)
        rig.align()
        small = cv2.resize(wall_frame(), (160, 120))
        with self.assertRaises(RequestError) as caught:
            rig.game.check_frame_jpeg(_jpeg(small), "neutral")
        self.assertEqual(str(caught.exception), "El cuadro de verificación no coincide con el tamaño de la cámara.")

    def test_a_preview_of_the_old_camera_landing_after_a_change_is_dropped(self):
        rig = Rig(self)
        rig.open()
        rig.preview()
        original = server.find_notes

        def change_camera_meanwhile(frame, palette, bridge=None):
            rig.act("camera_changed")   # the operator switches while this frame is measured
            return original(frame, palette, bridge)

        server.find_notes = change_camera_meanwhile
        try:
            answer = rig.game.preview_jpeg(_jpeg(wall_frame()), "Old camera")
        finally:
            server.find_notes = original
        self.assertEqual(answer["roles"]["start"]["count"], 1, "the caller still gets its answer")
        state = rig.game.snapshot()
        self.assertIsNone(state["camera"]["label"])
        self.assertEqual(state["camera"]["width"], 0)
        self.assertIsNone(state["live_detection"])
        server.find_notes = change_camera_meanwhile
        try:
            with self.assertRaises(RequestError) as caught:
                rig.game.scan_jpeg(_jpeg(wall_frame()), "Old camera")
        finally:
            server.find_notes = original
        self.assertEqual(str(caught.exception), "La cámara cambió durante la lectura. Volvé a escanear.")
