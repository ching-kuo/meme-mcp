from __future__ import annotations

from meme_mcp.envelope import Envelope, make_success
from meme_mcp.rendering.image_store import FilesystemImageStore
from meme_mcp.rendering.pipeline import TemplateSpec, render_meme


def generate_tool(
    spec: TemplateSpec,
    slot_fills: list[str],
    image_store: FilesystemImageStore,
    dry_run: bool = False,
) -> Envelope:
    if dry_run:
        return make_success({"template_id": spec.template_id, "rendered_url": None, "hash": None})
    result = render_meme(spec, slot_fills, image_store)
    return make_success(
        {"template_id": spec.template_id, "rendered_url": result.rendered_url, "hash": result.hash}
    )
