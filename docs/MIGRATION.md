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

`PgVectorStore` ships in v1.5 (`psycopg` + `pgvector`). Install with `uv sync --extra postgres`.
The Alembic `0002_vector_ddl` revision installs the pgvector extension and creates
`template_vectors.embedding vector(1536)` with an ivfflat cosine index automatically on first
boot against Postgres.

`S3ImageStore` also ships in v1.5 (sync `boto3` against any S3-compatible endpoint). Install
with `uv sync --extra s3`. Configure `IMAGE_STORE_BACKEND=s3` plus the `S3_*` settings
(`S3_ENDPOINT`, `S3_BUCKET`, `S3_REGION`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`).
Content-addressed key layout matches the filesystem store exactly so blobs sync 1:1 via
`rclone` without rewriting paths.

Migration steps (handled by `meme-mcp migrate`; manual breakdown listed for reference):

1. Use `pgloader` to move relational rows from SQLite to Postgres.
2. Run `meme-mcp reindex-embeddings` so vectors are regenerated in pgvector (the embedding
   model startup guard runs against the new DB and refuses to boot if any persisted vector
   disagrees with `EMBEDDING_MODEL`).
3. Sync filesystem image bytes to S3-compatible storage with `rclone sync`.
4. Swap `DATABASE_URL` and `IMAGE_STORE_BACKEND` in `.env`.

### Single-command cutover

```
uv run meme-mcp migrate \
  --target-db postgresql+psycopg://user:pass@host/db \
  --target-s3-endpoint https://s3.example.com \
  --target-s3-bucket meme-mcp \
  --target-s3-access-key KEY \
  --target-s3-secret-key SECRET \
  --target-s3-region us-east-1 \
  --dry-run
```

`--dry-run` validates `pgloader` and `rclone` on `$PATH`, Postgres connectivity + the
`vector` extension, S3 connectivity + write/read/delete permission, and source readability.
Without `--dry-run` the command also `chmod 0444`s `storage_dir` for the duration of the
run (restored on success or error), runs the three steps in order, and writes a suggested
`.env.next` for the operator to merge. Failure exit codes map to error strings:
`PGLOADER_FAILED`, `RCLONE_FAILED`, `PGVECTOR_MISSING`, `S3_UNREACHABLE`,
`EXTERNAL_CLI_MISSING`, `POSTGRES_UNREACHABLE`, `REINDEX_FAILED`, `SOURCE_UNREADABLE`.

Postgres parity tests live in `tests/test_vectors_postgres.py`; configure
`MEMEMCP_TEST_POSTGRES_URL` (and bring up `deploy/docker-compose.test.yml` for a local
pgvector + MinIO instance) to run them.
