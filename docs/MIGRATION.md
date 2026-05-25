# Migrations

## In-place schema migrations

`PendingUploadStore.__init__` runs an idempotent `ALTER TABLE pending_uploads ADD COLUMN
expires_at` against any database that predates the 24h TTL, then backfills `expires_at = now()`
so legacy rows expire on first read. No operator action is required.

## Changing the embedding model

`validate_embedding_model` refuses to boot if the persisted corpus was embedded with a different
model than the configured `EMBEDDING_MODEL`. Remediation:

```bash
uv run meme-mcp reindex-embeddings
```

This regenerates every vector with the configured model and rewrites
`template_embeddings_meta`. The same command also clears any "orphan vectors" — rows in
`template_vectors` with no `template_embeddings_meta` record, which appear on installs that
predate the startup guard.

## SQLite to Postgres and S3

The v1 codebase includes contracts for `PgVectorStore` and `S3ImageStore`, but both remain v1.5
stubs. Before migration, implement those stubs and add parity tests.

Planned migration path:

1. Use `pgloader` to move relational rows from SQLite to Postgres.
2. Run `meme-mcp reindex-embeddings` so vectors are regenerated in pgvector.
3. Sync filesystem image bytes to S3-compatible storage with `rclone sync`.
