from __future__ import annotations

from meme_mcp.envelope import Envelope, make_success
from meme_mcp.retrieval.search import TemplateRecord, search


def find_tool(
    records: list[TemplateRecord],
    query: str,
    filters: dict[str, object] | None = None,
) -> Envelope:
    candidates = [candidate.__dict__ for candidate in search(records, query, filters)]
    return make_success({"candidates": candidates})
