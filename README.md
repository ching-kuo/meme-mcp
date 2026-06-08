# meme-mcp

Private meme retrieval and rendering service with:

- hosted MCP Streamable HTTP at `/mcp`
- compatibility JSON routes under `/api/mcp/*`
- GitHub OAuth browser sessions for friends
- optional Google OAuth sign-in alongside GitHub (off by default) so friends without a GitHub account can join, with full parity
- bearer PAT auth for MCP clients
- self-service PAT management for friends via a browser `/account` page (generate/regenerate/revoke their single token)
- friend upload analysis/review/approval via a browser `/upload` page or the PAT API
- optional reverse-image enrichment (Google Cloud Vision) that recovers a meme's web identity before the VLM fills metadata
- SQLite + filesystem storage by default

## Local setup

```bash
uv sync --extra dev
cp .env.example .env
```

Set the required GitHub OAuth, VLM, embedding, session, and PAT pepper values in `.env`.

`PUBLIC_BASE_URL` is optional: when unset, the canonical externally-visible origin (advertised in
MCP OAuth metadata and used to sign render URLs) is derived from `GITHUB_REDIRECT_URI`. Set it when
running a second OAuth provider so the advertised origin does not depend on a single provider's
redirect URI. **Changing its origin invalidates outstanding/bookmarked signed render URLs**, and
startup fails closed if it conflicts with `GITHUB_REDIRECT_URI`'s origin.

### Google sign-in (optional, off by default)

Lets friends without a GitHub account sign in with Google. Google and GitHub identities are
independent (separate allowlist entries, PATs, history) and never linked. Google friends get full
parity: browse, generate, upload, self-service PATs, and MCP client auth.

- Provision a Google Cloud **OAuth 2.0 "Web application"** client (APIs & Services → Credentials).
  Set the authorized redirect URI to `<PUBLIC_BASE_URL or your GitHub origin>/auth/google/callback`,
  configure the OAuth consent screen, and request the `openid email` scopes.
- Enable with `GOOGLE_OAUTH_ENABLED=true` and set `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, and
  `GOOGLE_REDIRECT_URI` (must end in `/auth/google/callback` and resolve to the app's public
  origin). Startup fails fast if any are missing or the redirect origin does not match.
- **Any verified Google mailbox.** Sign-in requires Google's `email_verified` claim to be strictly
  `true`; the address may be any Google account mailbox, including non-Gmail ones (e.g.
  `@icloud.com`). The domain is not restricted because authorization keys on the full allowlisted
  email plus the immutable `sub` pin, so a Workspace admin cannot mint an arbitrary allowlisted
  address. Invite a friend by their Google account email: `meme-mcp allowlist add google:<email>`.
  (Dot/`+suffix` alias canonicalization still applies to Gmail addresses only; other domains match
  exactly.)
- **Trust-on-first-use.** On a friend's first verified sign-in the app pins their immutable Google
  `sub` to the invited mailbox; PATs and audit bind to `google:<sub>`, not the email, so an email
  rename does not lock them out. The first verified, allowlisted sign-in for an invited email wins
  the pin.
- **Wrong/poisoned pin remediation:** `meme-mcp pin show <email>` inspects the pinned `sub`;
  `meme-mcp pin revoke <email>` evicts it. `pin revoke` is a *rotation* tool — it deletes the pin
  but leaves the invite, so the next sign-in re-pins (confirm the intended `sub` with `pin show`,
  and re-revoke if the wrong account won again). To **deny access entirely**, remove the invite with
  `meme-mcp allowlist remove google:<email>` (this deletes the pin too); the account stays out until
  you re-add the invite. A deleted pin is never silently re-authorized — re-admitting always takes a
  fresh interactive sign-in.

### Reverse-image enrichment (optional, off by default)

A meme's cultural meaning often differs from what the image literally depicts. When enabled,
the upload pipeline sends the EXIF-stripped image bytes to Google Cloud Vision Web Detection
to recover the meme's real identity, then feeds that as grounding to the VLM and stores a
structured `origin` block (name + https source link). This is the **first egress of uploaded
images beyond the VLM provider**:

- Enable with `REVERSE_IMAGE_ENABLED=true` and `GOOGLE_VISION_CREDENTIALS_PATH=/path/to/sa.json`.
  The app fails to start if enabled without a readable credentials file.
- Use a **Vision-only, least-privilege** service account (so a leaked key cannot pivot), and
  keep the JSON key `chmod 600` and out of version control.
- **Re-verify Google's no-retention / no-training data terms before provisioning credentials.**
  This is a gate on enabling the feature, not a footnote: the provider was chosen because it
  uniquely satisfies raw-byte input + no-retention. If that has drifted, do not enable it.
- The web `/upload` toggle defaults **on** (and is hidden when the feature is off); programmatic
  PAT callers must opt in per request with `"identify_online": true`. Egress happens the instant
  the lookup is invoked — a `timeout`/`error` outcome does not mean the image stayed local.
- Failures degrade silently to today's image-only enrichment. Operators distinguish a healthy
  "no match" from systemic failure via the client's redacted liveness logs (see
  `docs/ARCHITECTURE.md`). Cost: 1,000 Vision units/month are free; overage is ~$3.50/1,000.

## Operator workflow

```bash
# Seed a deterministic local starter corpus.
uv run meme-mcp seed-memegen

# Or import the full upstream memegen template library from a local clone
# (checkout the pinned commit first; cloning bare HEAD drifts the corpus).
# Pins the upstream commit and per-template SHA-256 in assets/memegen-seed-manifest.json.
# Each template's source URL is stored as a provenance link (origin), not in usage_context,
# and prose is optionally overlaid from assets/memegen-enrichment.json (--enrichment-path to override).
uv run meme-mcp seed-memegen --upstream-path /path/to/memegen-clone

# Add a GitHub login to the allowlist used by both web sessions and PAT auth.
uv run meme-mcp allowlist add <github-login>

# Invite a Google friend by their Google account email (requires GOOGLE_OAUTH_ENABLED=true).
uv run meme-mcp allowlist add google:<email>          # or: allowlist add <email> --provider google
# Removing a Google invite also evicts the friend's pin (terminal revocation).
uv run meme-mcp allowlist remove google:<email>

# Inspect / evict Google sub->email pins (detect a wrong first-sign-in-wins pin).
uv run meme-mcp pin list
uv run meme-mcp pin show <email>
uv run meme-mcp pin revoke <email-or-sub>

# Issue a PAT. The token is printed once; only its hash is stored. The PAT expires
# after 90 days by default; pass --ttl-days 0 to opt out of expiry, and
# --scope read for read-only access.
#
# Allowlisted friends can self-mint their own token at the web `/account` page
# (bounded to 30/90/365-day expiry; never-expire stays operator-CLI-only). This
# CLI remains the operator fallback and the only way to issue a non-expiring token.
uv run meme-mcp pat issue <github-login> [--ttl-days N] [--scope read|readwrite]
# For a Google friend, pass their Google email (resolved to google:<sub> via the pin; the
# friend must have signed in once). pat revoke accepts a github login or google:<sub>.
uv run meme-mcp pat issue <email> [--ttl-days N] [--scope read|readwrite]
uv run meme-mcp pat revoke <github-login | google:sub>

# Inventory active and revoked PATs.
uv run meme-mcp pat list

# Rebuild template vectors from persisted template metadata.
# Default semantic search uses Ollama's OpenAI-compatible endpoint:
# EMBEDDING_BASE_URL=http://localhost:11434/v1
# EMBEDDING_MODEL=qwen3-embedding:0.6b
# EMBEDDING_DIMENSIONS=1024
# Required after switching EMBEDDING_MODEL or EMBEDDING_DIMENSIONS.
uv run meme-mcp reindex-embeddings --force

# Generate the reviewable zh-TW metadata overlay for the seed corpus. Reads the
# English enrichment asset, asks the VLM for Traditional Chinese, runs the drift
# gate (retry once then skip), and writes assets/memegen-enrichment.zh-TW.json.
# Review the artifact, commit it, then re-seed (below) and reindex.
uv run meme-mcp translate-corpus
# Re-import the corpus so templates carry the zh-TW overlay (locales.zh-TW), then
# reindex so embeddings cover the bilingual text.
uv run meme-mcp seed-memegen --upstream-path <memegen-checkout> \
  --zh-tw-enrichment-path assets/memegen-enrichment.zh-TW.json
uv run meme-mcp reindex-embeddings --force
```

## Run locally

```bash
uv run uvicorn meme_mcp.app:create_configured_app --factory --host 127.0.0.1 --port 8000
```

Useful routes:

- `GET /` (public landing page)
- `GET /healthz`
- `GET /readyz`
- `GET /account` (HTML page where an allowlisted friend generates/regenerates/revokes their MCP PAT and copies it once; anonymous browsers are redirected to sign-in)
- `GET /auth/login` (GitHub login, or a provider chooser when Google sign-in is enabled), `GET /auth/google/login` + `GET /auth/google/callback` (Google OIDC, when enabled)
- `GET /browse` (HTML gallery with template previews, paginated 24 per page via `?page=N`; cards link to the detail page; an anonymous browser is redirected to sign-in)
- `GET /templates/{template_id}` (HTML detail page: full preview plus metadata, slots, and fingerprint; auth-gated like `/browse`)
- `GET /templates/{template_id}/image` (the gallery's preview image; auth-gated like `/browse`)
- `GET /api/templates?q=deploy`
- `POST /api/templates/{template_id}/preview`
- `GET /api/mcp/tools`
- `POST /api/mcp/find`
- `POST /api/mcp/generate`
- `POST /api/mcp/record_outcome`
- `GET /renders/{prefix}/{filename}` (serves the PNG; accepts the signed `?exp=&sig=` token from a
  `generate` receipt, or session/PAT auth + receipt ownership)

## Container image

CI builds and publishes the image to GitHub Container Registry on every push to `main`, on
`v*` tags, and on manual `workflow_dispatch` runs. The push is gated on the lint/type-check/test
job (`.github/workflows/build-and-push.yml`), so a red commit never publishes. Images are
`linux/amd64`.

```bash
docker pull ghcr.io/ching-kuo/meme-mcp:latest
```

Tags: `latest` (tip of `main`), the branch name, `sha-<short>`, and `MAJOR.MINOR.PATCH` /
`MAJOR.MINOR` on `v*` releases. The manifests under `deploy/k8s/` reference
`ghcr.io/ching-kuo/meme-mcp:latest`; override the `image:` to pull from a private registry.
GHCR packages are private by default, so make the package public (or attach an
`imagePullSecret`) before an unauthenticated cluster can pull it.

## MCP client snippets

### Native custom connector (OAuth, recommended)

When the server runs with `OAUTH_AS_ENABLED=true`, add it directly in Claude's **Settings →
Connectors → Add custom connector**: enter `https://your-host.example/mcp` and sign in when
prompted. Claude registers itself (Dynamic Client Registration), you sign in with the same
GitHub/Google account the operator allowlisted, approve the one-time per-app consent screen, and
you are connected — no Node, no `npx`, no config file, and no shared token to copy around. Because
Anthropic's cloud brokers the connection (not your laptop), the boot/wake connect-timeout race
that affects the local `mcp-remote` bridge does not occur, and the connector works on claude.ai,
Desktop, and mobile. The server delegates login to GitHub/Google and issues its own short-lived,
allowlist-gated tokens (it never sees or forwards your GitHub/Google token).

The PAT + `mcp-remote` path below remains fully supported — it is the only option when the AS is
disabled, and an existing PAT keeps working against an AS-enabled server too.

### PAT + `mcp-remote`

Mint your token first: sign in at `https://your-host.example/account` and click Generate
(pick a scope and expiry), then copy the plaintext — it is shown exactly once. Export it as
`MEME_MCP_PAT` for the Codex snippet, or paste it into the Claude Desktop
`AUTH_HEADER` value below.

Codex CLI:

```toml
[mcp_servers.meme_mcp]
url = "https://your-host.example/mcp"
bearer_token_env_var = "MEME_MCP_PAT"
```

Claude Desktop through `mcp-remote`:

```json
{
  "mcpServers": {
    "meme-mcp": {
      "command": "npx",
      "env": {
        "AUTH_HEADER": "Bearer <paste-your-pat-here>",
        "npm_config_cache": "/tmp/npm-cache"
      },
      "args": [
        "-y",
        "-p",
        "node@24",
        "-p",
        "mcp-remote@latest",
        "mcp-remote",
        "https://your-host.example/mcp/",
        "--transport",
        "http-only",
        "--header",
        "Authorization:${AUTH_HEADER}"
      ]
    }
  }
}
```

Both `/mcp` and `/mcp/` reach the transport; the server normalizes the bare path
in-process. (Against an older deploy that 307-redirects bare `/mcp`, use the
trailing-slash form `…/mcp/`, since `mcp-remote` cannot replay a POST across the
redirect.)

Pin `mcp-remote` to Node 24 or 22 for now. In local reproduction against the live
service, `mcp-remote@0.1.38` under Node 26 intermittently failed with
`Unexpected content type: null` or undici connect timeouts even though the same PAT
and endpoint returned `200 OK` via plain `curl`. Running the same `mcp-remote`
command under Node 24/22 succeeded without server changes.

If you stay on the bridge, `scripts/meme-mcp-launch.sh` is an interim wrapper that polls
`/healthz` before exec'ing `mcp-remote`, turning the boot/wake connect-timeout race into a brief
wait (set the config `command` to the script instead of `npx`). The native custom connector above
fixes the race structurally and is the preferred path once `OAUTH_AS_ENABLED` is on.

## Bilingual UI (en / zh-TW)

The web UI is available in English and Traditional Chinese (`zh-TW`). A language
switch is shown on every page (labelled with each language's own name, so it is
usable even if you cannot read the current one); the choice is stored in a
`lang` cookie that persists across sessions and wins over the browser's
`Accept-Language` header on every later request. On a first visit with no cookie,
the language is best-effort auto-detected from `Accept-Language` (this reflects
the browser's advertised language, not the OS, so the always-visible switch is
the real guarantee). Translations live in a single dict catalog
(`src/meme_mcp/web/i18n/catalog.py`) that serves both server-rendered templates
and client-side JS strings; no build step or new runtime dependency is added.
This section covers UI chrome. Friend-authored template content (names,
descriptions, tags) carries its own Traditional Chinese payload — see
**Bilingual template metadata** below.

## Bilingual template metadata (en / zh-TW)

Template metadata is canonical English with an optional Traditional Chinese
overlay, so friends who are not proficient in English can read and search
templates in `zh-TW`. The VLM produces both languages on upload; a drift gate
rejects Simplified-Chinese or mainland-vocabulary leakage in the `zh-TW` output
(falling back to English-only for the affected fields). On approve, the fields a
friend edits are recorded as human-authored and protected from later machine
backfill. Chinese queries to the web `/browse` search and the MCP `find` tool
match both the lexical zh-TW text (CJK bigram scoring) and its meaning (semantic
embedding); the MCP `find` response always projects to canonical English so
agents see a single stable contract.

Semantic search defaults to a local Ollama OpenAI-compatible endpoint
(`EMBEDDING_BASE_URL=http://localhost:11434/v1`, `EMBEDDING_MODEL=qwen3-embedding:0.6b`,
`EMBEDDING_DIMENSIONS=1024`). After changing the model or dimensions, rebuild the
index with `uv run meme-mcp reindex-embeddings --force` (the `--force` flag clears
stale vectors before rebuilding so the boot guard does not stay latched).

The seed corpus (memegen templates) carries a one-time, reviewable
`assets/memegen-enrichment.zh-TW.json` overlay (machine provenance, drift-gated);
re-seeding with `--zh-tw-enrichment-path` attaches it as `locales.zh-TW`. The
shipped overlay is web-grounded: each meme was researched online for its
Traditional-Chinese / Taiwan usage and localized from the curated English
enrichment. `meme-mcp translate-corpus` is the reproducible text-only
regeneration path (it re-fills only missing slugs). New uploads are translated
inline by the VLM at analyze time. See the CLI block above for the one-time
backfill sequence.

zh-TW captions render with bundled `assets/fonts/NotoSansTC-Black.otf` (Noto Sans
TC, SIL Open Font License 1.1 — see `assets/fonts/NotoSansTC-OFL.txt` and
`LICENSES/SIL-OFL-1.1.txt`), selected per caption by CJK detection; pure-Latin
captions keep the existing Anton font. CJK lines wrap on character boundaries
with minimal kinsoku, Latin words stay atomic.

Emoji render in full color via bundled `assets/fonts/NotoColorEmoji.ttf` (Noto
Color Emoji, SIL Open Font License 1.1 — see `assets/fonts/NotoColorEmoji-OFL.txt`).
Because PIL draws one font per call, a caption containing emoji is laid out run by
run — text in the caption font, each emoji cluster (including ZWJ sequences, flags,
skin tones, and keycaps) composited from the color font — so emoji no longer render
as empty `.notdef` boxes. Long captions are measured at the height PIL truly renders
them, so text in short edge-anchored boxes is no longer clipped at the image edge.

## Development verification

```bash
.venv/bin/ruff check
.venv/bin/mypy src
.venv/bin/python -m pytest
```

Current local verification target: all three commands pass.

## Current v1 status

Implemented:

- MCP tool registration via official FastMCP
- PAT hashing and file-backed allowlist enforcement
- GitHub OAuth session flow with PKCE state (token exchange against `github.com`, user fetch against `api.github.com`)
- optional Google OAuth (OIDC via Authlib) sign-in alongside GitHub, with provider-namespaced identities and trust-on-first-use `sub` pinning
- persisted templates, receipts, pending uploads (with 24h TTL), and vectors
- upload validation, EXIF-stripping re-encode, duplicate detection, VLM review fallback
- optional reverse-image enrichment via Google Cloud Vision (deploy-gated, per-upload toggle)
- content-addressed rendering; render URLs carry a short-lived signed `?exp=&sig=` token (so image
  clients can load them) and otherwise require auth + receipt ownership; path-traversal guard on `/renders/`
- per-friend rate limiting on `find`/`generate`/`record_outcome` across both HTTP and MCP transports
- embedding model-drift startup guard (refuses to boot if persisted vectors disagree with `EMBEDDING_MODEL`)
- global `DecompressionBombWarning` escalation
- public landing page and web browse/search/detail/preview routes
- bilingual (en / zh-TW) template metadata with a zh-CN drift gate, human-wins provenance merge, CJK bigram + semantic search, English-only MCP projection, a one-time reviewable corpus translation overlay, and CJK caption rendering (bundled Noto Sans TC)
- full-color emoji in captions (bundled Noto Color Emoji, mixed-font run layout) and edge-clip-free text fitting (height measured as PIL renders it)
- self-service web PAT management at `/account` (session-authed, CSRF-protected, per-user rate limited, one-time plaintext reveal)
- structured JSONL audit log (`audit.jsonl` under the storage dir) carrying `pat_issued`/`pat_revoked` events (never the token or its hash)
- operator CLI for allowlist, PAT issue, seed, and reindex
- Docker and Kubernetes deployment examples

Known remaining external validation:

- live GitHub OAuth app callback
- live VLM provider call
- live Google Cloud Vision call + reverse-image efficacy matrix (calibrate the confidence floor; see the validation playbook)
- live embedding provider call
- live MCP client smoke against `/mcp`
