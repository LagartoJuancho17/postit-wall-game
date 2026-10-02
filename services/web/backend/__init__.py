"""HTTP adapter and state controller for the browser interface."""

from .server import GameController, create_server, serve

__all__ = ["GameController", "create_server", "serve"]
