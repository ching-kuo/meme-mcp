# Architecture

The service keeps pure primitives separate from HTTP and MCP handlers:

- `envelope.py` and `errors.py` define the stable response contract.
- `auth/pat.py` issues high-entropy PATs and stores only HMAC-SHA-256 digests. PAT
  rows carry `expires_at` (NULL means never expires; new tokens default to 90 days) and
  `capability` (`read` or `readwrite`; defaults to `readwrite` for back-compat). The
  verifier's SQL query filters only on `pat_hash`; expiry, revocation, and capability
  are evaluated in Python after fetch and every failure branch runs a constant-time
  compare so the query plan and timing cost are uniform across unknown / revoked /
  expired / corrupt records. The web `/browse` view renders a warning banner when the
  authenticated friend's PAT will expire in fewer than 7 days, sourced from
  `expires_at_for_login`; session/OAuth users never see the banner.
- `upload/` validates bytes before any persistence and strips image metadata by re-encoding.
- `rendering/` writes generated PNGs through a content-addressed `ImageStore`.
- `retrieval/` ranks local template records using typed filters, term overlap, and name boosts.
  When an `outcome_lookup` callable is supplied (live MCP `find` calls thread
  `OutcomeEventStore.recent_used_count` through), templates with recent `used` events from
  `record_outcome` gain +0.05 per event up to a hard cap of +0.20; name matches (+10.0) still
  dominate, so the share signal nudges ties rather than overriding intent. The boost is applied
  after `_name_match` and before the `top_k` cut, and templates with score > 0 surface even
  without a query term hit so frequently-shared templates remain reachable as the corpus grows.
- `db/outcomes.py` (`OutcomeEventStore`) persists agent share-signal events (`used`, `sent`,
  `dropped`) with a CHECK constraint and a `(template_id, ts)` index for the 30-day window
  query. Records can be pruned with `prune(older_than_days=N)`.
- The MCP server exposes three tools: `find`, `generate`, and `record_outcome`. The last one
  emits an `audit/events.py` `record_outcome` event so the JSONL audit log carries the same
  share signal the retrieval boost reads from.
- `db/uploads.py` enforces a 24h TTL on pending uploads; pre-TTL databases are migrated on first
  connection via an idempotent `ALTER TABLE ADD COLUMN` and stale rows are expired immediately.
- `db/vectors.py` exposes `EmbeddingMetaStore`, which records the embedding model used for each
  template's vector. `embeddings.client.validate_embedding_model` runs at app boot and refuses to
  start if any persisted vector disagrees with `EMBEDDING_MODEL`, or if any vector lacks a model
  record (the "orphan vector" case from pre-guard installs).
- `alembic/` holds the migration tree. `alembic/env.py` rewrites async driver URLs
  (`aiosqlite`, `asyncpg`) to their sync counterparts so Alembic's sync command path works.
  `alembic/versions/0001_baseline.py` captures every table the inline-DDL stores create
  (`templates`, `pats` with v1.5's `expires_at`+`capability`, `pending_uploads`,
  `template_vectors`, `template_embeddings_meta`, `generated_receipts`, `outcome_events`).
  `src/meme_mcp/db/migrations.py:run_migrations(settings)` is called at app boot to bring
  any DB to head; the inline `CREATE TABLE IF NOT EXISTS` calls in store `__init__`s remain
  as defense-in-depth so direct-test fixtures that skip the app boot still work.
- `db/engine.py` exposes `sqlite_path(database_url, fallback)` — the single source of truth for
  resolving a `sqlite+aiosqlite:///...` URL to a concrete `Path`. All CLI entrypoints and the app
  factory share this helper.
- `corpus/upstream.py` imports the full `jacebrowning/memegen` template library from a local
  clone. `project_slot_position` maps each upstream text box (anchor_x/y, scale_x/y, align, angle)
  to a canonical 9-band position string (`top|bottom|center|top-left|...|middle-right`) and
  always carries the raw `box` dict so the renderer can reproduce memegen's layout decisions.
  Narrow, off-axis, or rotated boxes additionally retain a `position_override` mapping for
  external callers that already inspect it. `import_upstream_corpus` persists templates and
  returns a manifest of `slug -> SHA-256(image bytes)` pinned to the upstream commit, which
  `cli/seed.py` writes to `assets/memegen-seed-manifest.json` for reproducible seeding.
- `cli/gc_renders.py` and the `meme-mcp gc-renders` CLI prune render outputs by TTL
  (`--ttl-days N`) or by max-byte budget (`--max-bytes N`, LRU by `generated_receipts.created_at`).
  Scope is the receipts-table — template seed images have no receipt row and are never touched.
  Each delete is guarded by a per-shard `portalocker` advisory lock so GC does not race a
  concurrent `put`. Missing-blob-with-extant-receipt rows are pruned cleanly. The
  `deploy/k8s/cronjob-gc-renders.yaml` manifest schedules a daily 30-day TTL sweep.
- `rendering/` reads each slot's `box` to derive pixel anchor, alignment, and box dimensions
  (see `_slot_anchor` in `rendering/pipeline.py`). `text_layout.select_wrap` picks the 1/2/3-line
  layout that fills ≥60% of box width while maximizing font size, and `fit_font` runs a
  shrink-loop to find the largest Anton size that fits the box with memegen-matching margins.
  Slots persisted without a `box` (legacy 3-band callers) fall back to synthetic top/center/bottom
  geometry via `_legacy_box_from_position`. Slot rotation (`box.angle`) is applied by drawing
  each rotated slot onto a per-slot transparent RGBA layer, rotating with `BICUBIC` around the
  slot anchor, and compositing back via `Image.alpha_composite`. The hot path stays in `RGB`
  when no slot has a non-zero angle so non-rotated templates pay no overhead. The visual-parity
  suite runs the rotated subset (e.g., `cmm`) at a relaxed dhash threshold (12) because rotation
  interpolates pixel rows; non-rotated cases keep the original threshold of 8.

## Request authentication

Two auth surfaces share one tree:

- Web (`/browse`, `/auth/*`, `/api/templates`, `/renders/*` via session) uses GitHub OAuth with
  PKCE (S256) and a re-validated allowlist on every request. The token exchange targets
  `https://github.com/login/oauth/access_token` and the user-profile fetch targets
  `https://api.github.com/user` — two distinct hosts, not a shared `base_url` client.
- MCP (`/mcp` and `/api/mcp/*`) uses a static PAT in `Authorization: Bearer …`. Verification is
  HMAC-SHA-256 with a server-side pepper. The verifier emits `meme:read` for every valid PAT
  and adds `meme:write` only when the PAT was issued with `readwrite` capability. The MCP
  `generate` tool and the `/api/mcp/generate`, `/api/uploads/analyze`, and
  `/api/uploads/{id}/approve` HTTP routes gate on `meme:write` (or `friend.capability ==
  "readwrite"` for HTTP) before any backend work; read-scope PATs receive `UNAUTHORIZED` at
  the wrapper.

The MCP tool wrappers derive the rate-limit actor from the validated `AccessToken` via
`mcp.server.auth.middleware.auth_context.get_access_token()` — never from
`Context.client_id`, which is client-supplied request metadata and therefore spoofable. Tool
calls without a verified access token raise `UNAUTHORIZED` at the wrapper before any backend
work runs.

## Storage backends

`PgVectorStore` ships in v1.5 (sync `psycopg` + `pgvector.psycopg.register_vector`); the
factory dispatches it for any `postgresql...` URL. The Alembic `0002_vector_ddl` revision
installs the pgvector extension and the `vector(1536)` column with an ivfflat cosine index
on Postgres; SQLite stays on the JSON-backed `template_vectors` table from the baseline.
`S3ImageStore` is still a v1.5 stub until U15 lands. See `docs/MIGRATION.md` for the
end-to-end switch and `tests/test_vectors_postgres.py` for the parity suite.

## Visual parity testing

`tests/test_visual_parity_golden.py` compares the renderer's output against pre-rendered golden
images from `memegen.link` using `imagehash.dhash` Hamming distance (threshold 8 on 256x256). The
suite skips when `/tmp/memegen-upstream/templates` is absent and records distances to
`assets/golden/parity-distances.json`. All 10 representative templates pass under the threshold
after the visual-parity renderer landed
(`docs/plans/2026-05-25-001-feat-visual-parity-renderer-plan.md`).
