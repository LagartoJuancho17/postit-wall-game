"""Read and set a USB (UVC) camera's controls on macOS, outside the browser.

Chrome on macOS offers no manual white balance, exposure or focus for some
cameras (the C920 among them), while the camera itself accepts these UVC
requests directly. ``native/uvc_control.c`` sends them through IOKit; this
module builds it on first use and speaks to it in JSON.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from threading import Lock
from typing import Any, Union

# A program path, or a full command such as ["/bin/sh", "fake.sh"].
Command = Union[Path, str, Sequence[str]]


def _command(program: Command) -> list[str]:
    return [str(program)] if isinstance(program, (Path, str)) else [str(part) for part in program]

from . import mapping

SOURCE = Path(__file__).parent / "native" / "uvc_control.c"
BINARY = Path(__file__).parent / "build" / "uvc-control"
_BUILD_LOCK = Lock()


class UvcError(RuntimeError):
    """The helper is unavailable or the camera refused a request."""


def build(source: Path = SOURCE, binary: Path = BINARY, compiler: Command | None = None) -> Path:
    """The compiled helper; rebuilt when missing or older than its source."""
    with _BUILD_LOCK:
        if binary.exists() and binary.stat().st_mtime >= source.stat().st_mtime:
            return binary
        if compiler is None:
            if sys.platform != "darwin":
                raise UvcError("El control USB de la cámara solo existe en macOS.")
            compiler = shutil.which("clang")
        if not compiler:
            raise UvcError("Falta clang para compilar el control USB. Instalá las Command Line Tools de Xcode.")
        binary.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            [*_command(compiler), "-O2", "-Wall", "-framework", "IOKit", "-framework", "CoreFoundation",
             str(source), "-o", str(binary)],
            capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            raise UvcError(f"No compiló el control USB: {result.stderr.strip()[:300]}")
        return binary


def run(args: list[str], helper: Command | None = None, timeout: float = 5.0) -> dict[str, Any]:
    """One helper call; its JSON reply or a UvcError with the camera's reason."""
    helper = helper or build()
    try:
        result = subprocess.run([*_command(helper), *args], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise UvcError("La cámara no respondió a tiempo por USB.") from exc
    try:
        reply = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise UvcError(f"Respuesta inválida del control USB: {result.stdout[:120]!r}") from exc
    if result.returncode != 0 or not isinstance(reply, dict) or "error" in reply:
        detail = reply.get("error") if isinstance(reply, dict) else None
        raise UvcError(detail or f"El control USB terminó con código {result.returncode}.")
    return reply


def _hex4(value: str) -> str:
    if not isinstance(value, str) or len(value) != 4 or any(c not in "0123456789abcdefABCDEF" for c in value):
        raise UvcError("vendor y product tienen que ser 4 dígitos hexadecimales.")
    return value.lower()


class UvcBridge:
    """Serialized helper calls in the panel's vocabulary (see ``mapping``)."""

    def __init__(self, helper: Command | None = None):
        self._helper = helper
        self._lock = Lock()

    def _run(self, args: list[str]) -> dict[str, Any]:
        with self._lock:
            return run(args, self._helper)

    def cameras(self) -> list[dict[str, Any]]:
        return self._run(["list"]).get("cameras", [])

    def resolve(self, vendor: str | None, product: str | None) -> tuple[str, str]:
        if vendor and product:
            return _hex4(vendor), _hex4(product)
        cameras = self.cameras()
        if len(cameras) != 1:
            raise UvcError(f"Hay {len(cameras)} cámaras USB; indicá cuál con vendor y product.")
        return cameras[0]["vendor"], cameras[0]["product"]

    def status(self, vendor: str | None = None, product: str | None = None) -> dict[str, Any]:
        vendor, product = self.resolve(vendor, product)
        raw = self._run(["get", vendor, product])
        return {"available": True, "device": raw.get("device", {}),
                "capabilities": mapping.capabilities(raw), "settings": mapping.settings(raw)}

    def run_raw(self, vendor: str, product: str, *writes: str) -> dict[str, Any]:
        """Direct UVC writes (name=value), only for restoring in the smoke check."""
        raw = self._run(["set", _hex4(vendor), _hex4(product), *writes])
        return mapping.settings(raw)

    def apply(self, requested: dict[str, Any], vendor: str | None = None,
              product: str | None = None) -> dict[str, Any]:
        vendor, product = self.resolve(vendor, product)
        before = self._run(["get", vendor, product])
        writes, skipped = mapping.plan(requested, before)
        raw = self._run(["set", vendor, product, *[f"{name}={value}" for name, value in writes]]) \
            if writes else before
        return {"available": True, "device": raw.get("device", {}),
                "capabilities": mapping.capabilities(raw), "settings": mapping.settings(raw),
                "applied": [list(write) for write in writes], "skipped": skipped}
