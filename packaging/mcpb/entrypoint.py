"""Personal desktop entrypoint; uses only the host's explicit configuration."""
from __future__ import annotations

import os


def main() -> None:
    from canvas_py import Canvas
    from canvas_py._config import CanvasConfig
    from canvas_connector.server import create_stdio_server

    try:
        config = CanvasConfig(base_url=os.environ.get("CANVAS_BASE_URL", ""),
                              token=os.environ.get("CANVAS_API_TOKEN", ""))
        setting = os.environ.get("CANVAS_ALLOW_WRITES", "false").strip().lower()
        if setting not in {"true", "false"}:
            raise ValueError("Expected a boolean write option")
    except (ValueError, TypeError):
        raise SystemExit("Configure a valid HTTPS Canvas origin, your own token, and the write option in the extension settings.") from None
    # Do not discover a .env file or inherit CANVAS_ENV_PATH from another app.
    with Canvas(config) as canvas:
        create_stdio_server(canvas=canvas, allow_writes=setting == "true").run(transport="stdio")


if __name__ == "__main__":
    main()
