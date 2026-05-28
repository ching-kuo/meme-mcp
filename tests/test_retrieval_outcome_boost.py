from __future__ import annotations

from meme_mcp.retrieval.search import (
    OUTCOME_BOOST_CAP,
    OUTCOME_BOOST_PER_USE,
    TemplateRecord,
    search,
)


def _record(template_id: str, *, name: str = "", desc: str = "") -> TemplateRecord:
    return TemplateRecord(
        template_id=template_id,
        slug=template_id,
        name=name or template_id,
        metadata={"description": desc, "tags": []},
        slot_definitions=[],
    )


def test_no_outcome_lookup_leaves_scores_unchanged() -> None:
    records = [_record("drake", desc="ship green")]
    baseline = search(records, "ship", filters=None, top_k=5)
    boosted = search(records, "ship", filters=None, top_k=5, outcome_lookup=lambda _tid: 0)
    assert baseline[0].similarity_score == boosted[0].similarity_score


def test_one_used_event_adds_five_percent() -> None:
    records = [_record("drake", desc="ship green")]
    [candidate] = search(records, "ship", outcome_lookup=lambda tid: 1 if tid == "drake" else 0)
    base = search(records, "ship")[0].similarity_score
    assert candidate.similarity_score == base + OUTCOME_BOOST_PER_USE


def test_four_events_hit_the_cap() -> None:
    records = [_record("drake", desc="ship green")]
    [candidate] = search(records, "ship", outcome_lookup=lambda tid: 4 if tid == "drake" else 0)
    base = search(records, "ship")[0].similarity_score
    assert candidate.similarity_score == base + OUTCOME_BOOST_CAP


def test_ten_events_stay_at_cap() -> None:
    records = [_record("drake", desc="ship green")]
    [candidate] = search(records, "ship", outcome_lookup=lambda tid: 10 if tid == "drake" else 0)
    base = search(records, "ship")[0].similarity_score
    assert candidate.similarity_score == base + OUTCOME_BOOST_CAP


def test_boost_promotes_borderline_candidate_into_results() -> None:
    """A template that doesn't match the query at all but has lots of recent usage
    should still surface when its boost exceeds zero — `search` already promotes
    score>0 candidates regardless of term overlap.
    """
    records = [
        _record("drake", desc="ship green"),
        _record("orphan", desc="unrelated"),
    ]
    results = search(records, "ship", outcome_lookup=lambda tid: 4 if tid == "orphan" else 0)
    template_ids = [c.template_id for c in results]
    assert "orphan" in template_ids


def test_name_match_dominates_boost_when_template_doesnt_match_query() -> None:
    """A template whose name matches the query gets +10.0 — that stays well above any
    outcome boost capped at +0.20. Asserts the ordering invariant the plan calls out.
    """
    records = [
        _record("drake", desc="unrelated text"),  # name match for "drake"
        _record("ship", desc="ship green"),  # body match + outcome boost
    ]
    results = search(records, "drake", outcome_lookup=lambda tid: 4 if tid == "ship" else 0)
    assert results[0].template_id == "drake"


def test_outcome_boost_field_appears_in_matched_fields() -> None:
    records = [_record("drake", desc="ship green")]
    [candidate] = search(records, "ship", outcome_lookup=lambda _tid: 1)
    assert "outcome_boost" in candidate.matched_fields


def test_boost_skipped_when_recent_count_is_zero() -> None:
    records = [_record("drake", desc="ship green")]
    [candidate] = search(records, "ship", outcome_lookup=lambda _tid: 0)
    assert "outcome_boost" not in candidate.matched_fields
