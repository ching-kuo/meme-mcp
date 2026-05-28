# meme-mcp

Private meme retrieval and rendering service with:

- hosted MCP Streamable HTTP at `/mcp`
- compatibility JSON routes under `/api/mcp/*`
- GitHub OAuth browser sessions for friends
- bearer PAT auth for MCP clients
- friend upload analysis/review/approval
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

- `GET /healthz`
- `GET /readyz`
- `GET /browse`
- `GET /api/templates?q=deploy`
- `POST /api/templates/{template_id}/preview`
- `GET /api/mcp/tools`
- `POST /api/mcp/find`
- `POST /api/mcp/generate`
- `POST /api/mcp/record_outcome`
- `GET /renders/{prefix}/{filename}`

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
- web browse/search/preview routes
- operator CLI for allowlist, PAT issue, seed, and reindex
- Docker and Kubernetes deployment examples

Known remaining external validation:

- live GitHub OAuth app callback
- live VLM provider call
- live embedding provider call
- live MCP client smoke against `/mcp`
