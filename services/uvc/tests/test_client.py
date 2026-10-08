"""Gate tests: the helper protocol, with fake helpers instead of a camera.

The fakes are /bin/sh scripts: they start in milliseconds, which keeps this
file well inside the gate's 2 s budget.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from services.uvc import UvcBridge, UvcError, build, run

FIXTURE = Path(__file__).parent / "c920_raw.json"


def fake(directory: Path, body: str, name: str = "uvc-control") -> list[str]:
    """A /bin/sh command: a brand-new executable costs ~350 ms on its first
    run while macOS assesses it; a script handed to /bin/sh does not."""
    path = directory / name
    path.write_text(body)
    return ["/bin/sh", str(path)]


class RunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_json_reply_is_returned(self) -> None:
        helper = fake(self.root, "printf '%s\\n' '{\"cameras\": []}'\n")
        self.assertEqual(run(["list"], helper), {"cameras": []})

    def test_camera_errors_become_uvc_errors_with_the_reason(self) -> None:
        helper = fake(self.root, "printf '%s\\n' '{\"error\": \"la cámara rechazó focus=999\"}'\nexit 2\n")
        with self.assertRaisesRegex(UvcError, "rechazó focus=999"):
            run(["set", "046d", "082d", "focus=999"], helper)

    def test_garbage_and_silence_are_errors(self) -> None:
        for body in ("echo not json\n", "exit 3\n"):
            with self.subTest(body=body):
                with self.assertRaises(UvcError):
                    run(["list"], fake(self.root, body))

    def test_a_hung_helper_times_out(self) -> None:
        with self.assertRaisesRegex(UvcError, "a tiempo"):
            run(["list"], fake(self.root, "sleep 5\n"), timeout=0.1)


class BridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.log = self.root / "calls.log"
        self.helper = fake(self.root, f"""printf '%s\\n' "$*" >> '{self.log}'
if [ "$1" = list ]; then
  printf '%s\\n' '{{"cameras": [{{"vendor": "046d", "product": "082d", "name": "C920"}}]}}'
else
  cat '{FIXTURE}'
fi
""")
        self.bridge = UvcBridge(self.helper)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def calls(self) -> list[list[str]]:
        return [line.split() for line in self.log.read_text().splitlines()]

    def test_status_speaks_the_panel_vocabulary(self) -> None:
        status = self.bridge.status("046D", "082d")
        self.assertTrue(status["available"])
        self.assertEqual(status["settings"]["focusMode"], "continuous")
        self.assertIn("exposureTime", status["capabilities"])
        self.assertEqual(self.calls(), [["get", "046d", "082d"]])

    def test_apply_turns_automatics_off_first_then_sets_values(self) -> None:
        result = self.bridge.apply({"whiteBalanceMode": "manual", "colorTemperature": 4100,
                                    "focusMode": "manual", "focusDistance": 40}, "046d", "082d")
        self.assertEqual(self.calls()[-1], ["set", "046d", "082d", "wb_auto=0", "focus_auto=0",
                                            "wb_temperature=4100", "focus=40"])
        self.assertEqual(result["applied"][0], ["wb_auto", 0])

    def test_a_single_camera_is_found_without_ids(self) -> None:
        self.bridge.status()
        self.assertEqual(self.calls()[0], ["list"])

    def test_without_ids_zero_or_several_cameras_say_why_in_plain_words(self) -> None:
        for cameras, words in (("[]", "ninguna cámara USB conectada"),
                               ('[{"vendor": "046d", "product": "082d"}, {"vendor": "1234", "product": "5678"}]',
                                "hay 2 cámaras USB")):
            with self.subTest(cameras=cameras):
                bridge = UvcBridge(fake(self.root, f"printf '%s\\n' '{{\"cameras\": {cameras}}}'\n", "list"))
                with self.assertRaisesRegex(UvcError, words):
                    bridge.status()

    def test_bad_ids_are_refused_before_calling_the_helper(self) -> None:
        for vendor in ("46d", "zzzz", "046d0"):
            with self.subTest(vendor=vendor):
                with self.assertRaisesRegex(UvcError, "hexadecimales"):
                    self.bridge.status(vendor, "082d")
        self.assertFalse(self.log.exists())


class BuildTests(unittest.TestCase):
    COMPILER = """while [ $# -gt 0 ]; do
  if [ "$1" = -o ]; then shift; printf built > "$1"; fi
  shift
done
printf x >> "$(dirname "$0")/compiled.log"
"""

    def test_build_reuses_a_fresh_binary_and_rebuilds_a_stale_one(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, binary = root / "helper.c", root / "build" / "helper"
            source.write_text("int main(void) { return 0; }\n")
            compiler = fake(root, self.COMPILER, "cc")
            build(source, binary, compiler)
            build(source, binary, compiler)
            self.assertEqual((root / "compiled.log").read_text(), "x")
            later = time.time() + 5
            os.utime(source, (later, later))
            build(source, binary, compiler)
            self.assertEqual((root / "compiled.log").read_text(), "xx")

    def test_a_failing_compile_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "helper.c"
            source.write_text("broken")
            compiler = fake(root, "echo 'error: boom' >&2\nexit 1\n", "cc")
            with self.assertRaisesRegex(UvcError, "No compiló"):
                build(source, root / "build" / "helper", compiler)


if __name__ == "__main__":
    unittest.main()
