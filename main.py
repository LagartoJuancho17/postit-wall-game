"""Launch the macOS wall game. Business logic lives in independent services."""

import argparse
import sys

from services.runtime import Session, SessionConfig


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Juego proyectado con post-its")
    parser.add_argument("--camera", type=int, default=0,
                        help="índice de webcam (predeterminado: 0)")
    parser.add_argument("--display", type=int,
                        help="índice de pantalla; por defecto usa la segunda si existe")
    parser.add_argument("--camera-width", type=int, default=1280)
    parser.add_argument("--camera-height", type=int, default=720)
    args = parser.parse_args(argv)
    if args.camera < 0 or args.camera_width < 320 or args.camera_height < 240:
        parser.error("la cámara debe tener índice >= 0 y resolución de al menos 320×240")
    try:
        session = Session(SessionConfig(args.camera, args.display,
                                        args.camera_width, args.camera_height))
        session.run()
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
