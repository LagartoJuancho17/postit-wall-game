"""Start the local web control room and projector window."""

from __future__ import annotations

import argparse
import subprocess
import sys
import webbrowser
from pathlib import Path

from services.web.backend.server import create_server


def open_browser(url: str) -> None:
    """Open Chrome on this Mac so its existing camera permission can be used."""
    chrome = Path("/Applications/Google Chrome.app")
    if sys.platform == "darwin" and chrome.exists():
        try:
            subprocess.Popen(
                ["open", "-a", "Google Chrome", url],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return
        except OSError:
            pass
    webbrowser.open(url)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Juego de pared en el navegador")
    parser.add_argument("--port", type=int, default=8765,
                        help="puerto local (predeterminado: 8765)")
    parser.add_argument("--no-browser", action="store_true",
                        help="no abrir el navegador automáticamente")
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("el puerto debe estar entre 0 y 65535")

    try:
        server = create_server(port=args.port)
    except OSError as exc:
        print(f"No se pudo iniciar el servidor local: {exc}", file=sys.stderr)
        return 1
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"Juego disponible en {url}", flush=True)
    print("Ctrl+C detiene el servidor.", flush=True)
    try:
        if not args.no_browser:
            open_browser(url)
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        print("Servidor detenido.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
