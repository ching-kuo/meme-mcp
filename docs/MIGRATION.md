# SQLite to Postgres and S3 Migration

The v1 codebase includes contracts for `PgVectorStore` and `S3ImageStore`, but both remain v1.5
stubs. Before migration, implement those stubs and add parity tests.

Planned migration path:

1. Use `pgloader` to move relational rows from SQLite to Postgres.
2. Run `meme-mcp reindex-embeddings --force` so vectors are regenerated in pgvector.
3. Sync filesystem image bytes to S3-compatible storage with `rclone sync`.

