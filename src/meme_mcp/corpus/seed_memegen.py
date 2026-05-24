from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SeedTemplate:
    slug: str
    name: str
    slot_count: int


def seed_fixture(templates: list[SeedTemplate]) -> list[dict[str, object]]:
    return [
        {"slug": template.slug, "name": template.name, "slot_count": template.slot_count}
        for template in templates
    ]

