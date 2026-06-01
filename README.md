# meme-mcp

Private meme retrieval and rendering service with:

- hosted MCP Streamable HTTP at `/mcp`
- compatibility JSON routes under `/api/mcp/*`
- GitHub OAuth browser sessions for friends
- bearer PAT auth for MCP clients
- friend upload analysis/review/approval via a browser `/upload` page or the PAT API
- optional reverse-image enrichment (Google Cloud Vision) that recovers a meme's web identity before the VLM fills metadata
- SQLite + filesystem storage by default

## Local setup

```bash
uv sync --extra dev
cp .env.example .env
```

Set the required GitHub OAuth, VLM, embedding, session, and PAT pepper values in `.env`.

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

# Issue a PAT. The token is printed once; only its hash is stored. The PAT expires
# after 90 days by default; pass --ttl-days 0 to opt out of expiry, and
# --scope read for read-only access.
uv run meme-mcp pat issue <github-login> [--ttl-days N] [--scope read|readwrite]

# Inventory active and revoked PATs.
uv run meme-mcp pat list

# Rebuild template vectors from persisted template metadata.
# Required after switching EMBEDDING_MODEL or EMBEDDING_DIMENSIONS.
uv run meme-mcp reindex-embeddings
```

## Run locally

```bash
uv run uvicorn meme_mcp.app:create_configured_app --factory --host 127.0.0.1 --port 8000
```

Useful routes:

- `GET /` (public landing page)
- `GET /healthz`
- `GET /readyz`
- `GET /browse` (HTML gallery with template previews; cards link to the detail page; an anonymous browser is redirected to GitHub login)
- `GET /templates/{template_id}` (HTML detail page: full preview plus metadata, slots, and fingerprint; auth-gated like `/browse`)
- `GET /templates/{template_id}/image` (the gallery's preview image; auth-gated like `/browse`)
- `GET /api/templates?q=deploy`
- `POST /api/templates/{template_id}/preview`
- `GET /api/mcp/tools`
- `POST /api/mcp/find`
- `POST /api/mcp/generate`
- `POST /api/mcp/record_outcome`
- `GET /renders/{prefix}/{filename}`

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
      "args": [
        "mcp-remote",
        "https://your-host.example/mcp",
        "--header",
        "Authorization: Bearer ${MEME_MCP_PAT}"
      ]
    }
  }
}
```

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
- persisted templates, receipts, pending uploads (with 24h TTL), and vectors
- upload validation, EXIF-stripping re-encode, duplicate detection, VLM review fallback
- optional reverse-image enrichment via Google Cloud Vision (deploy-gated, per-upload toggle)
- content-addressed rendering, authenticated receipt fetch, path-traversal guard on `/renders/`
- per-friend rate limiting on `find`/`generate`/`record_outcome` across both HTTP and MCP transports
- embedding model-drift startup guard (refuses to boot if persisted vectors disagree with `EMBEDDING_MODEL`)
- global `DecompressionBombWarning` escalation
- public landing page and web browse/search/detail/preview routes
- operator CLI for allowlist, PAT issue, seed, and reindex
- Docker and Kubernetes deployment examples

Known remaining external validation:

- live GitHub OAuth app callback
- live VLM provider call
- live Google Cloud Vision call + reverse-image efficacy matrix (calibrate the confidence floor; see the validation playbook)
- live embedding provider call
- live MCP client smoke against `/mcp`
