"""Run disjoint unittest lanes so the local gate can run them in parallel.

The pre-commit budget is 2 s of wall time on a laptop. The lanes split the
suite by module: the contract matrix (it drives the real backend through
every phase, alignment included), the live detection and alignment API
tests, the rest of services/web, the simulator and calibration, vision,
and "core" for everything else, including import failures.

Each lane imports only its own test modules (found by walking the packages
the way ``unittest discover`` does), so four lanes do not import every test
module four times: that alone cost ~0.6 s per lane.
"""
from __future__ import annotations

from pathlib import Path
import sys
import unittest

LANES = ("matrix", "live", "web", "sim", "vision", "core")
# First match wins; a module prefix ends with "." so test_x never matches test_x_extra.
_PREFIX_LANES = (
    ("services.web.tests.test_contract_matrix.", "matrix"),
    ("services.web.tests.test_live_detection.", "live"),
    ("services.web.tests.test_alignment_api.", "live"),
    # It imports every test module to check the partition: the lightest lane.
    ("tests.test_gate_lanes.", "web"),
    ("services.web.", "web"),
    ("services.simulation.", "sim"),
    ("services.calibration.", "sim"),
    ("services.vision.", "vision"),
)


def lane_for(test_id: str) -> str:
    for prefix, lane in _PREFIX_LANES:
        if test_id.startswith(prefix):
            return lane
    return "core"


def test_cases(suite: unittest.TestSuite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from test_cases(item)
        else:
            yield item


def test_modules(root: str = ".") -> list[str]:
    """Dotted names of the test*.py modules ``discover(root)`` would import."""
    base = Path(root)
    found = []

    def walk(directory: Path) -> None:
        for path in sorted(directory.iterdir()):
            if path.is_dir() and (path / "__init__.py").is_file():
                walk(path)
            elif path.is_file() and path.name.startswith("test") and path.suffix == ".py":
                found.append(".".join(path.relative_to(base).with_suffix("").parts))

    walk(base)
    return found


def main(lane: str) -> int:
    if lane not in LANES:
        raise ValueError(f"lane must be one of {', '.join(LANES)}")
    modules = [name for name in test_modules() if lane_for(name + ".") == lane]
    # A module that fails to import becomes a failing test of its own lane.
    selected = unittest.defaultTestLoader.loadTestsFromNames(modules)
    # Small fixture images are faster without nested OpenCV worker pools.
    cv = sys.modules.get("cv2")
    if cv is not None:
        cv.setNumThreads(1)
    return 0 if unittest.TextTestRunner(verbosity=0).run(selected).wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
