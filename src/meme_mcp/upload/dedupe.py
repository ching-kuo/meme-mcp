from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DuplicateRecord:
    template_id: str
    exact_hash: str
    perceptual_hash: str


@dataclass(frozen=True)
class DuplicateCheck:
    action: str
    template_id: str | None = None


class DuplicateIndex:
    def __init__(self) -> None:
        self.records: list[DuplicateRecord] = []

    def add(self, template_id: str, exact_hash: str, perceptual_hash: str) -> None:
        self.records.append(DuplicateRecord(template_id, exact_hash, perceptual_hash))


def _hamming_hex(left: str, right: str) -> int:
    return bin(int(left, 16) ^ int(right, 16)).count("1")


def check_duplicates(
    index: DuplicateIndex,
    exact_hash: str,
    perceptual_hash: str,
) -> DuplicateCheck:
    for record in index.records:
        if record.exact_hash == exact_hash:
            return DuplicateCheck("block", record.template_id)
    nearest = min(
        index.records,
        key=lambda item: _hamming_hex(item.perceptual_hash, perceptual_hash),
        default=None,
    )
    if nearest and _hamming_hex(nearest.perceptual_hash, perceptual_hash) <= 5:
        return DuplicateCheck("warn", nearest.template_id)
    return DuplicateCheck("accept")

