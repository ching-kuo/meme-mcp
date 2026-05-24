from __future__ import annotations

from pathlib import Path


class FileAllowlist:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def is_allowlisted(self, github_login: str) -> bool:
        if not self.path.exists():
            return False
        allowed = {
            line.strip().lower()
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        }
        return github_login.lower() in allowed

