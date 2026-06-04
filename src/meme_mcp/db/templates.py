from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal, Protocol

from meme_mcp.db.vectors import VectorStore
from meme_mcp.retrieval.search import Candidate, TemplateRecord, search

# A semantic hit adds at most this much to a lexical candidate's score. Kept
# well below the term/name-match weights so semantic recall reorders within a
# lexical tier and surfaces near-miss candidates, but never overrides a strong
# lexical (name/origin) match (U7: additive boost, not destructive).
SEMANTIC_BOOST_WEIGHT = 1.0


class QueryEmbedder(Protocol):
    def embed_query(self, query: str) -> list[float]: ...


@dataclass(frozen=True)
class TemplateCreate:
    template_id: str
    slug: str
    name: str
    source: Literal["memegen", "friend"]
    metadata: dict[str, Any]
    slot_definitions: list[dict[str, Any]]
    image_path: str
    perceptual_hash: str
    exact_hash: str


@dataclass(frozen=True)
class TemplateRow:
    template_id: str
    slug: str
    name: str
    source: str
    metadata: dict[str, Any]
    slot_definitions: list[dict[str, Any]]
    image_path: str
    perceptual_hash: str
    exact_hash: str

    def as_record(self) -> TemplateRecord:
        return TemplateRecord(
            template_id=self.template_id,
            slug=self.slug,
            name=self.name,
            metadata=self.metadata,
            slot_definitions=self.slot_definitions,
        )


class SQLiteTemplateRepository:
    def __init__(
        self,
        path: str | Path,
        embedder: QueryEmbedder | None = None,
        vector_store: VectorStore | None = None,
    ) -> None:
        # embedder/vector_store are optional and default to None so every
        # existing caller (seed.py, migrate, gc_uploads, tests) keeps the pure
        # lexical behavior with no change. They are only wired together where
        # requests are served (app.py), enabling the additive semantic layer.
        self.embedder = embedder
        self.vector_store = vector_store
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS templates (
                    id TEXT PRIMARY KEY,
                    slug TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    source TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    slot_definitions_json TEXT NOT NULL,
                    image_path TEXT NOT NULL,
                    perceptual_hash TEXT NOT NULL,
                    exact_hash TEXT NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def upsert(self, template: TemplateCreate) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO templates (
                    id, slug, name, source, metadata_json, slot_definitions_json,
                    image_path, perceptual_hash, exact_hash
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    slug = excluded.slug,
                    name = excluded.name,
                    source = excluded.source,
                    metadata_json = excluded.metadata_json,
                    slot_definitions_json = excluded.slot_definitions_json,
                    image_path = excluded.image_path,
                    perceptual_hash = excluded.perceptual_hash,
                    exact_hash = excluded.exact_hash,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    template.template_id,
                    template.slug,
                    template.name,
                    template.source,
                    json.dumps(template.metadata, sort_keys=True),
                    json.dumps(template.slot_definitions, sort_keys=True),
                    template.image_path,
                    template.perceptual_hash,
                    template.exact_hash,
                ),
            )

    def get(self, template_id: str) -> TemplateRow:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, slug, name, source, metadata_json, slot_definitions_json,
                       image_path, perceptual_hash, exact_hash
                FROM templates
                WHERE id = ?
                """,
                (template_id,),
            ).fetchone()
        if row is None:
            raise KeyError(template_id)
        return _row_from_sql(row)

    def list_records(self) -> list[TemplateRecord]:
        return [row.as_record() for row in self.list_rows()]

    def list_rows(self) -> list[TemplateRow]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, slug, name, source, metadata_json, slot_definitions_json,
                       image_path, perceptual_hash, exact_hash
                FROM templates
                ORDER BY name
                """
            ).fetchall()
        return [_row_from_sql(row) for row in rows]

    def search(
        self,
        query: str,
        filters: dict[str, Any] | None = None,
        top_k: int = 5,
        outcome_lookup: Callable[[str], int] | None = None,
    ) -> list[Candidate]:
        # Return Candidates directly: down-converting to TemplateRecord here
        # dropped similarity_score and matched_fields before they could reach the
        # MCP find envelope, so an origin_name_match (or any match tag) never
        # surfaced to the agent (U7/KTD9). Callers that only need identity read
        # template_id/slug/name, which Candidate also carries.
        lexical = search(self.list_records(), query, filters, top_k, outcome_lookup)
        if self.embedder is None or self.vector_store is None:
            return lexical
        return self._merge_semantic(query, lexical, top_k)

    def _merge_semantic(
        self,
        query: str,
        lexical: list[Candidate],
        top_k: int,
    ) -> list[Candidate]:
        """Fold semantic recall into the lexical ranking as an additive boost.

        Semantic-only hits (a template the lexical pass dropped) are admitted
        but capped, so semantic recall reorders/widens the result set without
        letting it explode past the existing top_k <= 5 cap. ANY failure -- the
        embedding endpoint being down, the embedder raising, or a store-side
        dimension mismatch (mixed-dimension table -> _cosine strict-zip
        ValueError, or SQLiteVecStore.search query-length ValueError) -- degrades
        to the lexical result rather than raising (U7: must not 500).
        """
        try:
            assert self.embedder is not None
            assert self.vector_store is not None
            query_vector = self.embedder.embed_query(query)
            raw_hits = self.vector_store.search(query_vector, max(top_k, 1))
        except Exception:
            return lexical
        # A zero (or negative) cosine carries no signal -- e.g. an orthogonal or
        # zero query vector. Dropping it keeps the boost truly additive: such a
        # hit must not invent a new candidate nor tag an existing one (the
        # English-regression invariant).
        hits = {template_id: score for template_id, score in raw_hits if score > 0.0}
        boosted = [
            replace(
                candidate,
                similarity_score=candidate.similarity_score
                + SEMANTIC_BOOST_WEIGHT * hits[candidate.template_id],
                matched_fields=[*candidate.matched_fields, "semantic"],
            )
            if candidate.template_id in hits
            else candidate
            for candidate in lexical
        ]
        seen = {candidate.template_id for candidate in lexical}
        # Semantic-only hits are surfaced as new candidates carrying just the
        # semantic boost; identity (slug/name/slots) comes from the stored row.
        rows = {row.template_id: row for row in self.list_rows()}
        extras: list[Candidate] = []
        for template_id, score in hits.items():
            if template_id in seen or template_id not in rows:
                continue
            row = rows[template_id]
            extras.append(
                Candidate(
                    template_id=row.template_id,
                    slug=row.slug,
                    name=row.name,
                    similarity_score=SEMANTIC_BOOST_WEIGHT * score,
                    matched_fields=["semantic"],
                    slot_definitions=row.slot_definitions,
                    suggested_slot_fills=[],
                    metadata=row.metadata,
                )
            )
        merged = sorted(
            [*boosted, *extras],
            key=lambda item: item.similarity_score,
            reverse=True,
        )
        return merged[: min(top_k, 5)]


def _row_from_sql(row: tuple[Any, ...]) -> TemplateRow:
    return TemplateRow(
        template_id=str(row[0]),
        slug=str(row[1]),
        name=str(row[2]),
        source=str(row[3]),
        metadata=json.loads(str(row[4])),
        slot_definitions=json.loads(str(row[5])),
        image_path=str(row[6]),
        perceptual_hash=str(row[7]),
        exact_hash=str(row[8]),
    )
