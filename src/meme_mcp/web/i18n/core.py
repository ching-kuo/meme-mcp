"""Lightweight i18n engine: locale negotiation and catalog-backed translation.

The single source of truth is :data:`meme_mcp.web.i18n.catalog.MESSAGES`, a typed
``dict[str, dict[str, str]]`` keyed by a dotted message id, each carrying an
``en`` and a ``zh-TW`` string. This module reads that catalog; it never owns
copy itself.

Locale precedence (the only place it lives) is::

    lang cookie  ->  Accept-Language header  ->  DEFAULT ("en")

``t()`` resolves a key in the active locale, falling back to ``en`` and then to
the literal key, and applies ``str.format`` interpolation defensively: a catalog
typo or a missing/extra kwarg degrades to unformatted text rather than raising a
500. Catalog values may use **named placeholders only** (``{count}``) -- never
positional (``{0}``), attribute (``{obj.attr}``), or index (``{0[k]}``) access;
:func:`lint_placeholders` rejects anything else, closing the format-string
injection surface.
"""

from __future__ import annotations

from string import Formatter
from typing import TYPE_CHECKING

from meme_mcp.web.i18n.catalog import MESSAGES

if TYPE_CHECKING:
    from starlette.requests import Request

Catalog = dict[str, dict[str, str]]

SUPPORTED: tuple[str, ...] = ("en", "zh-TW")
DEFAULT = "en"
COOKIE_NAME = "lang"

_FORMATTER = Formatter()


def negotiate_accept_language(header: str | None) -> str | None:
    """Best-effort map of an ``Accept-Language`` header to a supported locale.

    Returns ``"zh-TW"`` for any ``zh*`` family entry (``zh``, ``zh-Hant``,
    ``zh-TW``, ``zh-HK``), ``"en"`` for any ``en*`` entry, and ``None`` when no
    entry matches (the caller then falls back to :data:`DEFAULT`). ``q=`` weights
    establish ordering but are otherwise ignored; the first matching entry in
    descending-quality order wins. Malformed input never raises.
    """

    if not header:
        return None
    weighted: list[tuple[float, int, str]] = []
    for index, raw in enumerate(header.split(",")):
        token = raw.strip()
        if not token:
            continue
        parts = token.split(";")
        tag = parts[0].strip().lower()
        if not tag:
            continue
        quality = 1.0
        for param in parts[1:]:
            name, _, value = param.partition("=")
            if name.strip().lower() == "q":
                try:
                    quality = float(value.strip())
                except ValueError:
                    quality = 0.0
        # q=0 means "not acceptable" (RFC 7231 §5.3.1): drop it so an explicitly
        # rejected language is never selected, falling through to DEFAULT.
        if quality <= 0:
            continue
        # -quality sorts highest-quality first; index keeps equal-q entries in
        # header order.
        weighted.append((-quality, index, tag))
    for _, _, tag in sorted(weighted):
        if tag.startswith("zh"):
            return "zh-TW"
        if tag.startswith("en"):
            return "en"
    return None


def resolve_locale(request: Request) -> str:
    """Resolve the active locale for a request.

    Reads the ``lang`` cookie first and honors it when it names a supported
    locale; otherwise negotiates the ``Accept-Language`` header; otherwise
    returns :data:`DEFAULT`. An invalid or hostile cookie value (``fr``,
    ``../x``) is ignored, never trusted as-is.
    """

    cookie = request.cookies.get(COOKIE_NAME)
    if cookie in SUPPORTED:
        return cookie
    negotiated = negotiate_accept_language(request.headers.get("accept-language"))
    return negotiated or DEFAULT


def _interpolate(template: str, kwargs: dict[str, object]) -> str:
    """Apply ``str.format`` defensively; return the raw string on any mismatch.

    A missing placeholder (``KeyError``), a positional/index slip
    (``IndexError``), or a malformed spec (``ValueError``) degrades to the
    unformatted string rather than raising -- a display bug, never a 500.
    """

    try:
        return template.format(**kwargs)
    except (KeyError, IndexError, ValueError):
        return template


def t(key: str, locale: str, **kwargs: object) -> str:
    """Translate ``key`` into ``locale`` with ``en`` then key-literal fallback.

    Looks up ``MESSAGES[key][locale]``, falling back to the ``en`` string and
    finally to the literal ``key`` (R5 -- a defensive path the tests are built to
    catch, never a shipped state). When ``kwargs`` are supplied they are
    interpolated via :func:`_interpolate`.
    """

    entry = MESSAGES.get(key)
    if entry is None:
        return key
    value = entry.get(locale) or entry.get(DEFAULT) or key
    if kwargs:
        return _interpolate(value, kwargs)
    return value


def plural(n: int, base_key: str, locale: str, **kwargs: object) -> str:
    """Select the ``.one`` / ``.other`` variant of ``base_key`` for a count.

    English selects ``f"{base_key}.one"`` when ``n == 1`` and
    ``f"{base_key}.other"`` otherwise. ``zh-TW`` has no plural inflection, so it
    always uses the ``.other`` form. ``n`` is injected as ``count`` (so catalog
    values can write ``{count}``) alongside any extra ``kwargs``.
    """

    suffix = ".one" if (locale == DEFAULT and n == 1) else ".other"
    return t(base_key + suffix, locale, count=n, **kwargs)


def js_catalog(locale: str) -> dict[str, str]:
    """Return the ``js.*`` subset of the catalog for one locale.

    Keys retain their ``js.`` prefix so the client ``t()`` helper looks them up
    by the same id used in the catalog. Server-only keys (``nav.*`` etc.) are
    excluded to keep the embedded ``window.I18N`` blob tight (KTD6).
    """

    return {
        key: (entry.get(locale) or entry.get(DEFAULT) or key)
        for key, entry in MESSAGES.items()
        if key.startswith("js.")
    }


def _field_names(value: str) -> list[str]:
    """Yield the raw field names of every replacement field in ``value``.

    ``{{`` / ``}}`` escapes carry ``field_name is None`` and are skipped.
    """

    return [name for _, name, _, _ in _FORMATTER.parse(value) if name is not None]


def lint_placeholders(catalog: Catalog | None = None) -> list[str]:
    """Return catalog keys whose values use a non-named placeholder.

    A named placeholder is a bare Python identifier (``{count}``). Anything else
    -- positional (``{0}``), attribute (``{obj.attr}``), or index (``{0[k]}``)
    access -- is rejected, since those reach into object internals and widen the
    format-string injection surface (KTD4).
    """

    catalog = MESSAGES if catalog is None else catalog
    offenders: list[str] = []
    for key, entry in catalog.items():
        for value in entry.values():
            try:
                names = _field_names(value)
            except ValueError:
                offenders.append(key)
                break
            if any(not name.isidentifier() for name in names):
                offenders.append(key)
                break
    return offenders


def check_completeness(catalog: Catalog | None = None) -> list[str]:
    """Return human-readable problems with catalog locale coverage (R6).

    Flags any key missing a non-empty ``en`` or ``zh-TW`` value, and any
    ``*.one`` plural key lacking a matching ``*.other`` (or vice versa).
    An empty list means the catalog is in lockstep.
    """

    catalog = MESSAGES if catalog is None else catalog
    problems: list[str] = []
    for key, entry in catalog.items():
        for locale in SUPPORTED:
            if not entry.get(locale):
                problems.append(f"{key}: missing {locale}")
    keys = set(catalog)
    for key in keys:
        if key.endswith(".one") and f"{key[:-4]}.other" not in keys:
            problems.append(f"{key}: missing matching .other")
        if key.endswith(".other") and f"{key[:-6]}.one" not in keys:
            problems.append(f"{key}: missing matching .one")
    return problems
