from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from meme_mcp.auth.allowlist import FileAllowlist
from meme_mcp.auth.pat import SQLitePatStore, issue_pat
from meme_mcp.config import Settings, validate_at_startup


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
    allowlist_add.add_argument("github_login")
    allowlist_remove = allowlist_commands.add_parser("remove")
    allowlist_remove.add_argument("github_login")

    pat = subcommands.add_parser("pat")
    pat_commands = pat.add_subparsers(dest="pat_command", required=True)
    pat_issue = pat_commands.add_parser("issue")
    pat_issue.add_argument("github_login")
    return parser


def _run_allowlist(args: argparse.Namespace, settings: Settings) -> int:
    allowlist = FileAllowlist(settings.github_allowlist_path)
    if args.allowlist_command == "add":
        allowlist.add(args.github_login)
        return 0
    if args.allowlist_command == "remove":
        allowlist.remove(args.github_login)
        return 0
    if args.allowlist_command == "list":
        for login in allowlist.entries():
            print(login)
        return 0
    raise SystemExit(f"unknown allowlist command: {args.allowlist_command}")


def _run_pat(args: argparse.Namespace, settings: Settings) -> int:
    if args.pat_command != "issue":
        raise SystemExit(f"unknown pat command: {args.pat_command}")
    db_path = _sqlite_path(settings.database_url, Path(settings.storage_dir) / "meme.db")
    store = SQLitePatStore(db_path)
    token = issue_pat(store, args.github_login, settings.pat_hash_pepper.get_secret_value())
    print(token)
    return 0


def _sqlite_path(database_url: str, fallback: Path) -> Path:
    if database_url.startswith("sqlite:///"):
        return Path(database_url.removeprefix("sqlite:///"))
    if database_url.startswith("sqlite+aiosqlite:///"):
        return Path(database_url.removeprefix("sqlite+aiosqlite:///"))
    return fallback


if __name__ == "__main__":
    main()
