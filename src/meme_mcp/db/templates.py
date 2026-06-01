from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from meme_mcp.retrieval.search import Candidate, TemplateRecord, search


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
    def __init__(self, path: str | Path) -> None:
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
        return search(self.list_records(), query, filters, top_k, outcome_lookup)


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
