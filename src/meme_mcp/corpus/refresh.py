from __future__ import annotations


def should_preserve_metadata(metadata_edited_at: object | None) -> bool:
    return metadata_edited_at is not None

