from __future__ import annotations

from meme_mcp.envelope import Envelope, make_success
from meme_mcp.retrieval.search import TemplateRecord, project_candidate_english, search


def find_tool(
    records: list[TemplateRecord],
    query: str,
    filters: dict[str, object] | None = None,
) -> Envelope:
    candidates = [
        project_candidate_english(candidate) for candidate in search(records, query, filters)
    ]
    return make_success({"candidates": candidates})
