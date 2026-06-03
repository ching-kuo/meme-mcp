from __future__ import annotations

from pathlib import Path

# Gmail treats the local part case-insensitively, ignores dots, and drops a
# "+suffix"; googlemail.com is the same mailbox namespace. Canonicalizing both
# the operator-typed entry and the incoming ID-token email the same way means a
# friend invited as alice@gmail.com still matches a claim of a.l.i.c.e+meme@...
_GMAIL_DOMAINS = ("gmail.com", "googlemail.com")


def canonical_email(email: str) -> str:
    """Canonical mailbox for allowlist comparison (Gmail-aware).

    Lowercases the whole address; for Gmail/googlemail, strips dots from the
    local part and drops any ``+suffix``. Non-Gmail domains are only lowercased
    (their dot/plus semantics are provider-specific and out of scope this
    release), so this is forward-safe if Workspace addresses are admitted later.
    """
    address = email.strip().lower()
    local, sep, domain = address.partition("@")
    if not sep:
        return address
    if domain in _GMAIL_DOMAINS:
        local = local.split("+", 1)[0].replace(".", "")
    return f"{local}@{domain}"


class FileAllowlist:
    """Operator-managed allowlist with provider-namespaced entries.

    Entry grammar (one per line, ``#`` comments and blanks ignored):

    * ``<login>`` (bare) and ``github:<login>`` -- GitHub-scoped, by login.
    * ``google:<email>``                       -- Google-scoped, by mailbox.

    Matching is provider-scoped: a GitHub query only tests bare/``github:``
    entries and a Google query only tests ``google:`` entries, so a Google email
    can never match a bare GitHub entry and vice versa (R7, R8). Gmail addresses
    are canonicalized identically on both sides before comparison (R16).
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def is_allowlisted(self, value: str) -> bool:
        """Whether ``value`` is authorized.

        ``value`` is a bare GitHub login, ``github:<login>``, or
        ``google:<email>``. is_authorized passes the bare login for GitHub and
        the namespaced mailbox for Google.
        """
        provider, sep, subject = value.partition(":")
        if not sep:
            provider, subject = "github", value
        github_entries, google_entries = self._parsed()
        if provider == "github":
            return subject.strip().lower() in github_entries
        if provider == "google":
            return canonical_email(subject) in google_entries
        return False

    def __contains__(self, value: object) -> bool:
        return isinstance(value, str) and self.is_allowlisted(value)

    def _parsed(self) -> tuple[set[str], set[str]]:
        """Read the file once into (github logins, canonical google mailboxes)."""
        github: set[str] = set()
        google: set[str] = set()
        if not self.path.exists():
            return github, google
        for raw in self.path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            provider, sep, subject = line.partition(":")
            if not sep:
                github.add(line.lower())
            elif provider.lower() == "github":
                github.add(subject.strip().lower())
            elif provider.lower() == "google":
                google.add(canonical_email(subject))
        return github, google

    def entries(self) -> list[str]:
        """All stored entries, lowercased and sorted, for operator listing.

        Preserves the namespaced prefix so ``meme-mcp allowlist list`` shows the
        provider; this is a display view, not the match form.
        """
        if not self.path.exists():
            return []
        return sorted({
            line.strip().lower()
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        })

    def _canonical_entry(self, entry: str) -> str:
        """Stored form of an entry: a ``google:`` mailbox is canonicalized so add
        and remove agree with ``is_allowlisted`` matching.

        Without this, an alias invite (``google:a.l.i.c.e+x@gmail.com``) would be
        stored verbatim but `remove google:alice@gmail.com` -- an exact-string
        delete -- would miss it, leaving a live invite behind after a de-invite.
        """
        value = entry.strip().lower()
        provider, sep, subject = value.partition(":")
        if sep and provider == "google":
            return f"google:{canonical_email(subject)}"
        return value

    def add(self, entry: str) -> None:
        value = self._canonical_entry(entry)
        if not value:
            return
        allowed = set(self.entries())
        allowed.add(value)
        self._write(sorted(allowed))

    def remove(self, entry: str) -> None:
        allowed = set(self.entries())
        allowed.discard(self._canonical_entry(entry))
        self._write(sorted(allowed))

    def _write(self, allowed: list[str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        content = "".join(f"{entry}\n" for entry in allowed)
        self.path.write_text(content, encoding="utf-8")
