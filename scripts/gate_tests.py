"""Run disjoint web/core unittest lanes so the local gate can run in parallel."""
from __future__ import annotations

import sys
import unittest


def lane_for(test_id: str) -> str:
    return "web" if test_id.startswith("services.web.") else "core"


def test_cases(suite: unittest.TestSuite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from test_cases(item)
        else:
            yield item


def main(lane: str) -> int:
    if lane not in ("web", "core"):
        raise ValueError("lane must be web or core")
    cases = test_cases(unittest.defaultTestLoader.discover("."))
    selected = unittest.TestSuite(case for case in cases if lane_for(case.id()) == lane)
    # Small fixture images are faster without nested OpenCV worker pools.
    cv = sys.modules.get("cv2")
    if cv is not None:
        cv.setNumThreads(1)
    return 0 if unittest.TextTestRunner(verbosity=0).run(selected).wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
