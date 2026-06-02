from __future__ import annotations

import argparse
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from meme_mcp.auth.allowlist import FileAllowlist, canonical_email
from meme_mcp.auth.google_pins import SQLiteGooglePinStore
from meme_mcp.auth.pat import (
    DEFAULT_CAPABILITY,
    DEFAULT_TTL_DAYS,
    VALID_CAPABILITIES,
    SQLitePatStore,
    issue_pat,
    list_pats,
)
from meme_mcp.cli.gc_renders import run as run_gc_renders
from meme_mcp.cli.gc_uploads import run as run_gc_uploads
from meme_mcp.cli.migrate import run as run_migrate
from meme_mcp.cli.reindex_embeddings import make_embedder, reindex_embeddings
from meme_mcp.cli.seed import run as run_seed
from meme_mcp.config import Settings, validate_at_startup
from meme_mcp.db.engine import sqlite_path
from meme_mcp.db.templates import SQLiteTemplateRepository
from meme_mcp.db.vectors import EmbeddingMetaStore, SQLiteVecStore


def main() -> None:
    raise SystemExit(run())


def run(argv: Sequence[str] | None = None, settings: Settings | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    app_settings = settings or Settings()  # type: ignore[call-arg]
    if args.command == "check-env":
        validate_at_startup(app_settings)
        print("environment ok")
        return 0
    if args.command == "allowlist":
        return _run_allowlist(args, app_settings)
    if args.command == "pat":
        return _run_pat(args, app_settings)
    if args.command == "pin":
        return _run_pin(args, app_settings)
    if args.command == "reindex-embeddings":
        return _run_reindex_embeddings(app_settings)
    if args.command == "seed-memegen":
        upstream = Path(args.upstream_path) if args.upstream_path else None
        manifest = Path(args.manifest_path) if args.manifest_path else None
        enrichment = Path(args.enrichment_path) if args.enrichment_path else None
        return run_seed(
            app_settings,
            upstream_path=upstream,
            manifest_path=manifest,
            enrichment_path=enrichment,
        )
    if args.command == "gc-renders":
        # Neither flag given (the cronjob's invocation) falls back to the
        # configured retention, so the GC sweep and the signed-URL TTL ceiling
        # read the same RENDER_GC_TTL_DAYS and cannot drift.
        ttl_days = args.ttl_days
        if ttl_days is None and args.max_bytes is None:
            ttl_days = app_settings.render_gc_ttl_days
        return run_gc_renders(
            app_settings,
            ttl_days=ttl_days,
            max_bytes=args.max_bytes,
            dry_run=args.dry_run,
        )
    if args.command == "gc-uploads":
        return run_gc_uploads(app_settings, dry_run=args.dry_run)
    if args.command == "migrate":
        return run_migrate(
            app_settings,
            target_db=args.target_db,
            target_s3_endpoint=args.target_s3_endpoint,
            target_s3_bucket=args.target_s3_bucket,
            target_s3_access_key=args.target_s3_access_key,
            target_s3_secret_key=args.target_s3_secret_key,
            target_s3_region=args.target_s3_region,
            dry_run=args.dry_run,
        )
    parser.error(f"unknown command: {args.command}")
    return 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="meme-mcp")
    subcommands = parser.add_subparsers(dest="command", required=False)
    subcommands.add_parser("check-env")

    allowlist = subcommands.add_parser("allowlist")
    allowlist_commands = allowlist.add_subparsers(dest="allowlist_command", required=True)
    allowlist_commands.add_parser("list")
    allowlist_add = allowlist_commands.add_parser("add")
    allowlist_add.add_argument("entry", help="bare GitHub login, github:<login>, or google:<email>")
    allowlist_add.add_argument("--provider", choices=["github", "google"], default=None)
    allowlist_remove = allowlist_commands.add_parser("remove")
    allowlist_remove.add_argument("entry")
    allowlist_remove.add_argument("--provider", choices=["github", "google"], default=None)

    pat = subcommands.add_parser("pat")
    pat_commands = pat.add_subparsers(dest="pat_command", required=True)
    pat_issue = pat_commands.add_parser("issue")
    pat_issue.add_argument(
        "principal",
        help="GitHub login, a Gmail address, or google:<email> (resolved to google:<sub>)",
    )
    pat_issue.add_argument(
        "--ttl-days",
        type=int,
        default=DEFAULT_TTL_DAYS,
        help="Days until the PAT expires (0 means never expires)",
    )
    pat_issue.add_argument(
        "--scope",
        choices=list(VALID_CAPABILITIES),
        default=DEFAULT_CAPABILITY,
        help="Capability scope; readwrite is the historical default",
    )
    pat_commands.add_parser("list")
    pat_revoke = pat_commands.add_parser("revoke")
    pat_revoke.add_argument(
        "principal", help="GitHub login or google:<sub> whose active PAT to revoke"
    )

    pin = subcommands.add_parser("pin")
    pin_commands = pin.add_subparsers(dest="pin_command", required=True)
    pin_show = pin_commands.add_parser("show")
    pin_show.add_argument("email")
    pin_commands.add_parser("list")
    pin_revoke = pin_commands.add_parser("revoke")
    pin_revoke.add_argument("identifier", help="invited Gmail address or the pinned google sub")
    subcommands.add_parser("reindex-embeddings")
    seed = subcommands.add_parser("seed-memegen")
    seed.add_argument("--upstream-path", default=None)
    seed.add_argument("--manifest-path", default=None)
    seed.add_argument("--enrichment-path", default=None)
    gc = subcommands.add_parser("gc-renders")
    gc.add_argument("--ttl-days", type=int, default=None)
    gc.add_argument("--max-bytes", type=int, default=None)
    gc.add_argument("--dry-run", action="store_true")
    gc_uploads = subcommands.add_parser("gc-uploads")
    gc_uploads.add_argument("--dry-run", action="store_true")
    migrate = subcommands.add_parser("migrate")
    migrate.add_argument("--target-db", required=True)
    migrate.add_argument("--target-s3-endpoint", required=True)
    migrate.add_argument("--target-s3-bucket", required=True)
    migrate.add_argument("--target-s3-access-key", required=True)
    migrate.add_argument("--target-s3-secret-key", required=True)
    migrate.add_argument("--target-s3-region", default="us-east-1")
    migrate.add_argument("--dry-run", action="store_true")
    return parser


def _namespaced_entry(entry: str, provider: str | None) -> str:
    """Resolve a CLI allowlist arg to a stored entry form.

    An already-namespaced entry is kept; ``--provider`` prefixes a bare value;
    otherwise the value is bare (GitHub-scoped, back-compat).
    """
    if ":" in entry or provider is None:
        return entry
    return f"{provider}:{entry}"


def _run_allowlist(args: argparse.Namespace, settings: Settings) -> int:
    allowlist = FileAllowlist(settings.github_allowlist_path)
    if args.allowlist_command == "list":
        for entry in allowlist.entries():
            print(entry)
        return 0
    entry = _namespaced_entry(args.entry, args.provider)
    provider, sep, subject = entry.partition(":")
    if args.allowlist_command == "add":
        if not sep and "@" in entry:
            # A bare value with @ would be stored as a GitHub login; require an
            # explicit Google namespace so a Gmail invite is never mis-scoped.
            print(f"error: '{entry}' looks like an email; use google:{entry} or --provider google")
            return 2
        allowlist.add(entry)
        return 0
    if args.allowlist_command == "remove":
        allowlist.remove(entry)
        # Terminal revocation (R13): removing a Google invite also deletes the pin
        # so re-inviting the same email cannot reactivate the prior sub. Handle
        # both the namespaced form and a bare email (which a hand-edited file could
        # leave behind) so eviction is never silently skipped.
        google_email = (
            subject if sep and provider.lower() == "google" else entry if "@" in entry else None
        )
        if google_email:
            db_path = sqlite_path(settings.database_url, Path(settings.storage_dir) / "meme.db")
            SQLiteGooglePinStore(db_path).delete_by_email(canonical_email(google_email))
        return 0
    raise SystemExit(f"unknown allowlist command: {args.allowlist_command}")


def _resolve_pat_subject(principal: str, pin_store: SQLiteGooglePinStore) -> str | None:
    """Resolve a CLI `pat issue` arg to the PAT subject.

    A GitHub login passes through (issue_pat namespaces it). A Gmail address or
    ``google:<email>`` is resolved to ``google:<sub>`` via the pin; returns None
    when no pin exists yet (the friend must sign in with Google first).
    """
    provider, sep, rest = principal.partition(":")
    if sep and provider.lower() == "google":
        email = rest
    elif "@" in principal:
        email = principal
    else:
        return principal  # GitHub login
    sub = pin_store.sub_for_email(canonical_email(email))
    return None if sub is None else f"google:{sub}"


def _run_pat(args: argparse.Namespace, settings: Settings) -> int:
    db_path = sqlite_path(settings.database_url, Path(settings.storage_dir) / "meme.db")
    store = SQLitePatStore(db_path)
    if args.pat_command == "issue":
        if args.ttl_days < 0:
            print(f"error: --ttl-days must be >= 0 (0 means never expires), got {args.ttl_days}")
            return 2
        subject = _resolve_pat_subject(args.principal, SQLiteGooglePinStore(db_path))
        if subject is None:
            print(
                f"error: no Google pin for '{args.principal}'; the friend must sign in "
                "with Google once before a PAT can be issued"
            )
            return 2
        token = issue_pat(
            store,
            subject,
            settings.pat_hash_pepper.get_secret_value(),
            ttl_days=args.ttl_days,
            capability=args.scope,
        )
        print(token)
        return 0
    if args.pat_command == "revoke":
        # revoke_active normalizes and matches legacy bare rows; google:<sub> works
        # even after the pin is gone (raw subject, no email->sub lookup needed).
        revoked = store.revoke_active(args.principal)
        print("revoked" if revoked else "no active token")
        return 0
    if args.pat_command == "list":
        records = list_pats(store)
        if not records:
            print("no PATs issued")
            return 0
        now = datetime.now(UTC)
        print(f"{'login':<24} {'status':<10} {'scope':<10} {'expires_in':<14} {'last_used':<20}")
        for record in records:
            if record.revoked_at is not None:
                status = "revoked"
                expires_in = "-"
            elif record.expires_at is None:
                status = "active"
                expires_in = "never"
            elif record.expires_at <= now:
                status = "expired"
                expires_in = "expired"
            else:
                status = "active"
                expires_in = f"{(record.expires_at - now).days}d"
            last_used = record.last_used_at.isoformat() if record.last_used_at else "-"
            print(
                f"{record.friend_login:<24} {status:<10} {record.capability:<10} "
                f"{expires_in:<14} {last_used:<20}"
            )
        return 0
    raise SystemExit(f"unknown pat command: {args.pat_command}")


def _run_pin(args: argparse.Namespace, settings: Settings) -> int:
    db_path = sqlite_path(settings.database_url, Path(settings.storage_dir) / "meme.db")
    store = SQLiteGooglePinStore(db_path)
    def _print_pins(rows: list[tuple[str, str, str]]) -> None:
        for sub, email, created_at in rows:
            print(f"{email}\tgoogle:{sub}\t{created_at}")

    if args.pin_command == "show":
        canonical = canonical_email(args.email)
        matches = [pin for pin in store.all_pins() if pin[1] == canonical]
        if not matches:
            print(f"no pin for {args.email}")
        else:
            _print_pins(matches)
        return 0
    if args.pin_command == "list":
        pins = store.all_pins()
        if not pins:
            print("no pins")
        else:
            _print_pins(pins)
        return 0
    if args.pin_command == "revoke":
        identifier = args.identifier
        # `pin list`/`pin show` print the sub in google:<sub> form; strip the
        # prefix so an operator can copy it back verbatim and still match the
        # stored raw sub (otherwise the delete silently matches nothing).
        if identifier.lower().startswith("google:"):
            identifier = identifier[len("google:") :]
        if "@" in identifier:
            removed = store.delete_by_email(canonical_email(identifier))
        else:
            removed = store.delete_by_sub(identifier)
        print("revoked" if removed else "no matching pin")
        return 0
    raise SystemExit(f"unknown pin command: {args.pin_command}")


def _run_reindex_embeddings(settings: Settings) -> int:
    db_path = sqlite_path(settings.database_url, Path(settings.storage_dir) / "meme.db")
    templates = SQLiteTemplateRepository(db_path)
    vectors = SQLiteVecStore(db_path, dimensions=settings.embedding_dimensions)
    meta = EmbeddingMetaStore(db_path)
    embedder = make_embedder(
        settings.embedding_model,
        settings.embedding_api_key.get_secret_value(),
        settings.embedding_base_url,
    )
    count = reindex_embeddings(templates, vectors, embedder, meta)
    print(f"reindexed {count} templates")
    return 0


if __name__ == "__main__":
    main()
