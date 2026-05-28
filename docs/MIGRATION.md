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

`PgVectorStore` ships in v1.5 (`psycopg` + `pgvector`). Install the extra with
`uv sync --extra postgres`. The Alembic `0002_vector_ddl` revision installs the pgvector
extension and creates `template_vectors.embedding vector(1536)` with an ivfflat cosine index
automatically on first boot against Postgres. `S3ImageStore` lands in v1.5 alongside it.

Migration steps:

1. Use `pgloader` to move relational rows from SQLite to Postgres.
2. Run `meme-mcp reindex-embeddings` so vectors are regenerated in pgvector (the embedding
   model startup guard runs against the new DB and refuses to boot if any persisted vector
   disagrees with `EMBEDDING_MODEL`).
3. Sync filesystem image bytes to S3-compatible storage with `rclone sync`.
4. Swap `DATABASE_URL` and `IMAGE_STORE_BACKEND` in `.env`.

Postgres parity tests live in `tests/test_vectors_postgres.py`; configure
`MEMEMCP_TEST_POSTGRES_URL` (and bring up `deploy/docker-compose.test.yml` for a local
pgvector + MinIO instance) to run them.
