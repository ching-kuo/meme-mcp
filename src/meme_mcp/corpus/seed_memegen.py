from __future__ import annotations

from dataclasses import dataclass, field
from io import BytesIO

from PIL import Image, ImageDraw

from meme_mcp.db.templates import SQLiteTemplateRepository, TemplateCreate
from meme_mcp.rendering.image_store import FilesystemImageStore
from meme_mcp.upload.validation import compute_hashes


@dataclass(frozen=True)
class SeedTemplate:
    slug: str
    name: str
    slot_count: int
    description: str = ""
    tags: list[str] = field(default_factory=list)


DEFAULT_SEED_TEMPLATES = [
    SeedTemplate(
        slug="drake",
        name="Drake Hotline Bling",
        slot_count=2,
        description="Reject one option and approve another.",
        tags=["choice", "reaction"],
    ),
    SeedTemplate(
        slug="distracted-boyfriend",
        name="Distracted Boyfriend",
        slot_count=3,
        description="A person is tempted by a new option while ignoring the current one.",
        tags=["temptation", "tradeoff"],
    ),
    SeedTemplate(
        slug="two-buttons",
        name="Two Buttons",
        slot_count=2,
        description="A stressed choice between two difficult options.",
        tags=["decision", "stress"],
    ),
]


def seed_fixture(templates: list[SeedTemplate]) -> list[dict[str, object]]:
    return [
        {"slug": template.slug, "name": template.name, "slot_count": template.slot_count}
        for template in templates
    ]


def seed_templates(
    repository: SQLiteTemplateRepository,
    image_store: FilesystemImageStore,
    templates: list[SeedTemplate] | None = None,
) -> int:
    count = 0
    for template in templates or DEFAULT_SEED_TEMPLATES:
        image = _placeholder_template_image(template)
        exact_hash, perceptual_hash = compute_hashes(image)
        image_path = image_store.put(image, "png")
        repository.upsert(
            TemplateCreate(
                template_id=f"memegen-{template.slug}",
                slug=template.slug,
                name=template.name,
                source="memegen",
                metadata={
                    "name": template.name,
                    "description": template.description,
                    "emotion": "contextual",
                    "usage_context": template.description,
                    "tags": template.tags,
                    "format": "static",
                },
                slot_definitions=_slot_definitions(template.slot_count),
                image_path=image_path,
                perceptual_hash=perceptual_hash,
                exact_hash=exact_hash,
            )
        )
        count += 1
    return count


def _slot_definitions(slot_count: int) -> list[dict[str, str]]:
    positions = ["top", "bottom", "center", "top-left"]
    return [
        {"name": f"slot_{index + 1}", "position": positions[index % len(positions)]}
        for index in range(slot_count)
    ]


def _placeholder_template_image(template: SeedTemplate) -> bytes:
    image = Image.new("RGB", (640, 360), "#1f2937")
    draw = ImageDraw.Draw(image)
    draw.rectangle((24, 24, 616, 336), outline="#f9fafb", width=4)
    draw.text((48, 48), template.name, fill="#f9fafb")
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()
