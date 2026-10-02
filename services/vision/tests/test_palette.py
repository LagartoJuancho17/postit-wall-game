"""Gate tests: operator-picked colors drive detection without overlapping roles."""

import random
import unittest

import numpy as np

from contracts.palette import ROLES, ColorRule, Palette
from services.vision import (DEFAULT_PALETTE, DetectionError, build_palette, detect_board,
                             find_notes, hsv_from_hex, rule_from_hex)
from services.vision.palette import MIN_HUE_DISTANCE, hue_distance
from services.vision.tests.scene import GOAL, MARKERS, START, bgr, hex_at, scene


def _center(polygon):
    return np.mean([(point.x, point.y) for point in polygon.points], axis=0)


def _picked(image, expected):
    """The palette an operator gets by touching one note of each role."""
    return {"start": hex_at(image, expected["start"]), "goal": hex_at(image, expected["goal"]),
            "bumper": hex_at(image, expected["greens"][0]),
            "marker": hex_at(image, expected["markers"][0])}


class HexConversionTests(unittest.TestCase):
    def test_hsv_uses_opencv_scale(self):
        self.assertEqual(hsv_from_hex("#ff0000"), (0, 255, 255))
        self.assertEqual(hsv_from_hex("#00FF00"), (60, 255, 255))
        self.assertEqual(hsv_from_hex("#0000ff"), (120, 255, 255))
        self.assertEqual(hsv_from_hex("#ffffff"), (0, 0, 255))

    def test_hsv_matches_opencv_for_drawn_notes(self):
        for hue in (2, 12, 30, 65, 105, 157, 176):
            blue, green, red = bgr(hue)
            got = hsv_from_hex(f"#{red:02x}{green:02x}{blue:02x}")
            self.assertLessEqual(hue_distance(got[0], hue), 1, hue)

    def test_invalid_hex_is_rejected(self):
        for value in ("#fff", "red", "#gg0000", None, 12, "ff0000"):
            with self.assertRaises(ValueError):
                rule_from_hex(value)


class RuleTests(unittest.TestCase):
    def test_default_swatches_lie_inside_their_own_bands(self):
        for role in ROLES:
            rule = DEFAULT_PALETTE.rule(role)
            self.assertIn(hsv_from_hex(rule.hex)[0], rule.hues(), role)

    def test_no_choices_is_exactly_the_default_palette(self):
        self.assertEqual(build_palette({}), DEFAULT_PALETTE)
        self.assertEqual(build_palette({role: None for role in ROLES}), DEFAULT_PALETTE)

    def test_picked_color_gets_a_window_around_its_hue(self):
        rule = rule_from_hex("#3a6df0")
        hue = hsv_from_hex("#3a6df0")[0]
        self.assertEqual((rule.hue_low, rule.hue_high), (hue - 10, hue + 10))
        self.assertEqual(len(rule.hues()), 21)

    def test_red_window_wraps_through_zero(self):
        rule = rule_from_hex("#ff0010")
        self.assertGreater(rule.hue_low, rule.hue_high)
        self.assertIn(0, rule.hues())
        self.assertIn(179, rule.hues())

    def test_pale_colors_lower_the_saturation_floor_but_not_to_the_wall(self):
        vivid, pale = rule_from_hex("#ff3b8d"), rule_from_hex("#f2a7c6")
        self.assertLess(pale.min_saturation, vivid.min_saturation)
        self.assertGreaterEqual(pale.min_saturation, 45)

    def test_gray_and_dark_samples_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "casi gris"):
            rule_from_hex("#c8c4c6")
        with self.assertRaisesRegex(ValueError, "oscuro"):
            rule_from_hex("#1a0505")

    def test_contract_rejects_overlapping_roles(self):
        rule = ColorRule("#f7d84b", 20, 39, 75, 55)
        with self.assertRaisesRegex(ValueError, "se superponen"):
            Palette(start=rule, goal=rule, bumper=DEFAULT_PALETTE.bumper, marker=DEFAULT_PALETTE.marker)


class BuildPaletteTests(unittest.TestCase):
    def test_pick_near_a_default_splits_the_hues_between_both(self):
        palette = build_palette({"start": "#ffc400"})  # yellow-orange, near the orange goal
        start, goal = hsv_from_hex("#ffc400")[0], hsv_from_hex(DEFAULT_PALETTE.goal.hex)[0]
        self.assertIn(start, palette.start.hues())
        self.assertIn(goal, palette.goal.hues())
        self.assertFalse(palette.start.hues() & palette.goal.hues())
        for hue in palette.start.hues():
            self.assertLess(hue_distance(hue, start), hue_distance(hue, goal))

    def test_two_default_roles_keep_their_hand_tuned_bands(self):
        palette = build_palette({"start": "#3a6df0"})
        self.assertEqual(palette.goal, DEFAULT_PALETTE.goal)

    def test_too_similar_colors_are_refused_with_the_other_role_named(self):
        with self.assertRaisesRegex(ValueError, "llegada"):
            build_palette({"start": DEFAULT_PALETTE.goal.hex})
        with self.assertRaisesRegex(ValueError, "inicio"):
            build_palette({"start": "#3a6df0", "goal": "#3d6ef2"})

    def test_refusal_speaks_about_the_role_just_changed(self):
        # Regression: with two custom roles the message named the first role
        # in order ("inicio") although the operator had just changed "rebotes".
        choices = {"start": "#3a6df0", "bumper": "#3d6ef2"}
        with self.assertRaises(ValueError) as caught:
            build_palette(choices, changed="bumper")
        self.assertTrue(str(caught.exception).startswith("El color de rebotes se parece"))
        self.assertIn("cambiá primero el de inicio", str(caught.exception))
        with self.assertRaises(ValueError) as caught:
            build_palette(choices, changed="start")
        self.assertTrue(str(caught.exception).startswith("El color de inicio se parece"))

    def test_unknown_role_is_refused(self):
        with self.assertRaisesRegex(ValueError, "Rol desconocido"):
            build_palette({"ball": "#ff0000"})

    def test_random_palettes_are_disjoint_contiguous_and_keep_their_color(self):
        rng = random.Random(53)
        built = 0
        for _ in range(400):
            choices = {role: (f"#{rng.randrange(256):02x}{rng.randrange(256):02x}"
                              f"{rng.randrange(256):02x}" if rng.random() < 0.6 else None)
                       for role in ROLES}
            try:
                palette = build_palette(choices)
            except ValueError as error:
                # Only the documented refusals are allowed.
                self.assertRegex(str(error), "casi gris|oscuro|se parece demasiado")
                continue
            built += 1
            seen = set()
            for role in ROLES:
                rule = palette.rule(role)
                self.assertFalse(seen & rule.hues())
                seen |= rule.hues()
                self.assertIn(hsv_from_hex(rule.hex)[0], rule.hues(), (choices, role))
        self.assertGreater(built, 150)
        self.assertGreaterEqual(MIN_HUE_DISTANCE, 8)

    def test_result_does_not_depend_on_the_order_of_choices(self):
        choices = {"marker": "#e0607a", "start": "#3a6df0", "bumper": "#ffd23f", "goal": "#2ec4b6"}
        reordered = dict(reversed(list(choices.items())))
        self.assertEqual(build_palette(choices), build_palette(reordered))


class DetectionWithPickedColorsTests(unittest.TestCase):
    RECOLORED = {"start": 105, "goal": 176, "bumper": 28, "marker": 140}

    def test_recolored_wall_needs_its_own_palette(self):
        image, expected = scene(hues=self.RECOLORED, speckles=True)
        with self.assertRaises(DetectionError):
            detect_board(image)
        board = detect_board(image, build_palette(_picked(image, expected)))
        self.assertLess(np.linalg.norm(_center(board.start) - expected["start"]), 5)
        self.assertLess(np.linalg.norm(_center(board.goal) - expected["goal"]), 5)
        self.assertEqual(len(board.bumpers), 2)
        for actual, target in zip(board.playfield.points, expected["markers"]):
            self.assertLess(np.linalg.norm((actual.x, actual.y) - target), 5)

    def test_pastel_notes_are_found_from_their_sampled_color(self):
        # Saturation 70 sits under the default floor of 75: only a floor
        # scaled from the sample finds these notes.
        image, expected = scene(saturation=70, brightness=0.8)
        with self.assertRaises(DetectionError):
            detect_board(image)
        board = detect_board(image, build_palette(_picked(image, expected)))
        self.assertEqual(len(board.markers), 4)
        self.assertLess(np.linalg.norm(_center(board.start) - expected["start"]), 5)

    def test_find_notes_reports_partial_walls_without_raising(self):
        image, _ = scene()
        image[275:385, 120:230] = 225  # remove the start note
        notes = find_notes(image)
        self.assertEqual(len(notes["start"]), 0)
        self.assertEqual(len(notes["goal"]), 1)
        self.assertEqual(len(notes["marker"]), 4)
        self.assertEqual(len(notes["bumper"]), 2)
        self.assertEqual(set(notes), set(ROLES))

    def test_find_notes_and_detect_board_agree_on_a_full_wall(self):
        image, _ = scene(perspective=True, speckles=True)
        notes, board = find_notes(image), detect_board(image)
        self.assertEqual(notes["start"][0], board.start)
        self.assertEqual(notes["goal"][0], board.goal)
        self.assertEqual(set(notes["marker"]), set(board.markers))

    def test_error_names_the_role_not_a_fixed_color(self):
        image, _ = scene(hues=self.RECOLORED)
        palette = build_palette(_picked(image, {"start": START, "goal": GOAL,
                                                "greens": [(375, 220)], "markers": MARKERS}))
        image[275:385, 120:230] = 225
        with self.assertRaises(DetectionError) as caught:
            detect_board(image, palette)
        self.assertEqual(caught.exception.role, "start")
        self.assertIn("inicio", str(caught.exception))
        self.assertNotIn("amarill", str(caught.exception))

    def test_palette_must_be_a_contract_palette(self):
        image, _ = scene()
        with self.assertRaises(TypeError):
            detect_board(image, {"start": "#ff0000"})


if __name__ == "__main__":
    unittest.main()
