"""Hardware check against the real camera: python -m services.uvc.smoke [--write-test]

Read-only by default: builds the helper, finds the USB camera and reports
which automatics are on. --write-test also switches automatic white balance
off and back to how it was, to prove writes reach the camera. Without a USB
camera it reports "skipped" and exits 0, so it can run anywhere.
"""

from __future__ import annotations

import json
import sys

from . import UvcBridge, UvcError, build


def main(argv: list[str]) -> int:
    report: dict[str, object] = {"check": "uvc_smoke"}
    try:
        build()
        bridge = UvcBridge()
        cameras = bridge.cameras()
        report["cameras"] = cameras
        if not cameras:
            report["result"] = "skipped: no USB camera connected"
            print(json.dumps(report, ensure_ascii=False))
            return 0
        vendor, product = cameras[0]["vendor"], cameras[0]["product"]
        status = bridge.status(vendor, product)
        report["capabilities"] = sorted(status["capabilities"])
        report["settings"] = status["settings"]
        if "--write-test" in argv and "whiteBalanceMode" in status["capabilities"]:
            was_auto = status["settings"].get("whiteBalanceMode") == "continuous"
            off = bridge.apply({"whiteBalanceMode": "manual"}, vendor, product)
            report["write_test_manual"] = off["settings"].get("whiteBalanceMode")
            if was_auto:  # put it back exactly as it was
                restored = bridge.run_raw(vendor, product, "wb_auto=1")
                report["write_test_restored"] = restored
            report["result"] = ("ok" if report["write_test_manual"] == "manual" else "write did not stick")
        else:
            report["result"] = "ok"
    except UvcError as exc:
        report["result"] = f"error: {exc}"
        print(json.dumps(report, ensure_ascii=False))
        return 1
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["result"] in ("ok",) or str(report["result"]).startswith("skipped") else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
