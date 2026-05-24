from __future__ import annotations

from pathlib import Path


class FileAllowlist:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def is_allowlisted(self, github_login: str) -> bool:
        return github_login.lower() in self.entries()

    def entries(self) -> list[str]:
        if not self.path.exists():
            return []
        allowed = sorted({
            line.strip().lower()
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        })
        return allowed

    def add(self, github_login: str) -> None:
        login = github_login.strip().lower()
        if not login:
            return
        allowed = set(self.entries())
        allowed.add(login)
        self._write(sorted(allowed))

    def remove(self, github_login: str) -> None:
        allowed = set(self.entries())
        allowed.discard(github_login.strip().lower())
        self._write(sorted(allowed))

    def _write(self, allowed: list[str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        content = "".join(f"{login}\n" for login in allowed)
        self.path.write_text(content, encoding="utf-8")
