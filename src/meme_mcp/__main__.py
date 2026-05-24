from __future__ import annotations

import argparse

from meme_mcp.config import Settings, validate_at_startup


def main() -> None:
    parser = argparse.ArgumentParser(prog="meme-mcp")
    parser.add_argument("command", nargs="?", default="check-env")
    args = parser.parse_args()
    if args.command == "check-env":
        validate_at_startup(Settings())  # type: ignore[call-arg]
        print("environment ok")
        return
    raise SystemExit(f"unknown command: {args.command}")


if __name__ == "__main__":
    main()
