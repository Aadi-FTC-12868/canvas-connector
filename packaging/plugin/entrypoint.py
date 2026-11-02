"""Personal plugin configuration, independent of any hosted OAuth grant."""
from __future__ import annotations

import os
from pathlib import Path


def main() -> None:
    from canvas_py import Canvas
    from canvas_py._config import CanvasConfig
    from canvas_connector.server import create_stdio_server
    from dotenv import dotenv_values

    try:
        # A host's explicit account fields are authoritative, including when
        # incomplete: never mix a new URL with an old file's token.
        if "CANVAS_BASE_URL" in os.environ or "CANVAS_API_TOKEN" in os.environ:
            source = os.environ
        else:
            selected = os.environ.get("CANVAS_ENV_PATH")
            path = Path(selected).expanduser() if selected else Path.home() / ".config/canvas-connector/canvas.env"
            if not path.is_file():
                raise ValueError("Personal configuration is missing")
            source = dotenv_values(path, interpolate=False)
        config = CanvasConfig(base_url=source.get("CANVAS_BASE_URL", ""),
                              token=source.get("CANVAS_API_TOKEN", ""))
        setting = source.get("CANVAS_ALLOW_WRITES", "false").strip().lower()
        if setting not in {"true", "false"}:
            raise ValueError("Expected a boolean write option")
    except (ValueError, TypeError, AttributeError, OSError):
        raise SystemExit("Configure your personal Canvas connection using the plugin's setup skill or README. Never paste a token into chat.") from None
    with Canvas(config) as canvas:
        create_stdio_server(canvas=canvas, allow_writes=setting == "true").run(transport="stdio")


if __name__ == "__main__":
    main()
