# meme-mcp

Private meme retrieval and rendering service with:

- hosted MCP Streamable HTTP at `/mcp`
- compatibility JSON routes under `/api/mcp/*`
- GitHub OAuth browser sessions for friends
- bearer PAT auth for MCP clients
- friend upload analysis/review/approval via a browser `/upload` page or the PAT API
- SQLite + filesystem storage by default

## Local setup

```bash
uv sync --extra dev
cp .env.example .env
```

Set the required GitHub OAuth, VLM, embedding, session, and PAT pepper values in `.env`.

## Operator workflow

```bash
# Seed a deterministic local starter corpus.
uv run meme-mcp seed-memegen

# Or import the full upstream memegen template library from a local clone.
# Pins the upstream commit and per-template SHA-256 in assets/memegen-seed-manifest.json.
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
- `GET /browse` (HTML; an anonymous browser is redirected to GitHub login)
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
- content-addressed rendering, authenticated receipt fetch, path-traversal guard on `/renders/`
- per-friend rate limiting on `find`/`generate`/`record_outcome` across both HTTP and MCP transports
- embedding model-drift startup guard (refuses to boot if persisted vectors disagree with `EMBEDDING_MODEL`)
- global `DecompressionBombWarning` escalation
- public landing page and web browse/search/preview routes
- operator CLI for allowlist, PAT issue, seed, and reindex
- Docker and Kubernetes deployment examples

Known remaining external validation:

- live GitHub OAuth app callback
- live VLM provider call
- live embedding provider call
- live MCP client smoke against `/mcp`
