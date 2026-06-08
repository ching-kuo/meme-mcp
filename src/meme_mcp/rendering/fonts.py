from __future__ import annotations

import atexit
from contextlib import ExitStack
from importlib import resources
from pathlib import Path


def bundled_font_path(filename: str) -> str:
    """Resolve a font shipped under ``assets/fonts`` to a real filesystem path.

    Prefers the source-tree copy during development; otherwise
    ``importlib.resources.as_file`` materializes a path even when the package is
    loaded from a zip. The ExitStack keeps any temp file alive for the process
    lifetime so PIL can reopen it on every render.
    """
    source_path = Path(__file__).resolve().parents[3] / "assets" / "fonts" / filename
    if source_path.is_file():
        return str(source_path)
    ref = resources.files("meme_mcp").joinpath(f"assets/fonts/{filename}")
    stack = ExitStack()
    atexit.register(stack.close)
    return str(stack.enter_context(resources.as_file(ref)))
