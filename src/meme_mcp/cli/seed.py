from __future__ import annotations

import atexit
import subprocess
from contextlib import ExitStack
from functools import cache
from importlib import resources
from pathlib import Path

from meme_mcp.config import Settings
from meme_mcp.corpus.seed_memegen import seed_templates
from meme_mcp.corpus.upstream import import_upstream_corpus, write_manifest
from meme_mcp.db.engine import sqlite_path
from meme_mcp.db.templates import SQLiteTemplateRepository
from meme_mcp.rendering.image_store import FilesystemImageStore

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_MANIFEST_PATH = _PROJECT_ROOT / "assets" / "memegen-seed-manifest.json"


@cache
def _default_enrichment_path() -> Path:
    """Resolve the committed enrichment file in both the source tree and the wheel.

    Unlike the manifest (operator-local; the Job overrides --manifest-path), the
    enrichment file must be READ by the deployed seed Job, so it is force-included
    in the wheel and resolved like the renderer's font asset: source-tree path
    first, then the packaged resource (materialized for the process lifetime).
    """
    source_path = _PROJECT_ROOT / "assets" / "memegen-enrichment.json"
    if source_path.is_file():
        return source_path
    ref = resources.files("meme_mcp").joinpath("assets/memegen-enrichment.json")
    stack = ExitStack()
    atexit.register(stack.close)
    return Path(stack.enter_context(resources.as_file(ref)))


def run(
    settings: Settings,
    upstream_path: Path | None = None,
    manifest_path: Path | None = None,
    enrichment_path: Path | None = None,
) -> int:
    db_path = sqlite_path(settings.database_url, Path(settings.storage_dir) / "meme.db")
    repository = SQLiteTemplateRepository(db_path)
    image_store = FilesystemImageStore(settings.image_store_fs_path)
    if upstream_path is not None:
        commit_sha = _git_rev_parse(upstream_path)
        enrichment = enrichment_path or _default_enrichment_path()
        count, manifest = import_upstream_corpus(
            upstream_path, repository, image_store, commit_sha, enrichment_path=enrichment
        )
        target = manifest_path or _DEFAULT_MANIFEST_PATH
        write_manifest(manifest, target)
        print(f"imported {count} templates from {upstream_path} @ {commit_sha[:8]}")
        print(f"manifest written to {target}")
        return 0
    count = seed_templates(repository, image_store)
    print(f"seeded {count} templates")
    return 0


def _git_rev_parse(path: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()
