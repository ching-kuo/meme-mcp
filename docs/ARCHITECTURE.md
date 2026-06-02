# Architecture

The service keeps pure primitives separate from HTTP and MCP handlers:

- `envelope.py` and `errors.py` define the stable response contract.
- `auth/pat.py` issues high-entropy PATs and stores only HMAC-SHA-256 digests. PAT
  rows carry `expires_at` (NULL means never expires; new tokens default to 90 days) and
  `capability` (`read` or `readwrite`; defaults to `readwrite` for back-compat). The
  verifier's SQL query filters only on `pat_hash`; expiry, revocation, and capability
  are evaluated in Python after fetch and every failure branch runs a constant-time
  compare so the query plan and timing cost are uniform across unknown / revoked /
  expired / corrupt records. `revoke_active(login)` revokes the single active row for a
  login (used by the web `/account` flow) and `current_status(login)` reads the latest row's
  display fields (state `none|active|expired|revoked`, scope, expiry, last-used) without ever
  returning the hash; both reuse the same parse-before-compare / raw-revoked fail-closed
  discipline as `verify` and stay out of the timing-sensitive verify path. The web `/browse`
  view renders a warning banner when the authenticated friend's PAT will expire in fewer than
  7 days, sourced from `expires_at_for_login`; the banner links to `/account` so the friend can
  regenerate it themselves.
- `upload/` validates bytes before any persistence and strips image metadata by re-encoding.
- `rendering/` writes generated PNGs through a content-addressed `ImageStore`. `render_meme`
  prepends the public app base URL (the `_public_app_base_url` value, same origin as OAuth
  metadata) so `rendered_url` is an absolute `https://host/renders/...` link an MCP client can
  fetch without knowing the server host out of band.
- `rendering/signing.py` signs that URL with a short-lived `?exp=&sig=` HMAC (key derived from
  `session_secret` with a domain tag) before it goes into the `generate` receipt. The auth-gated
  `GET /renders/...` route accepts a live signature *in lieu of* session/PAT auth, so an image
  client (Claude Desktop, a browser `<img>`) that cannot replay the caller's Bearer PAT still
  loads the PNG -- the presigned-URL model. Possession of the signed URL is the capability; the
  TTL (`RENDER_URL_TTL_SECONDS`, default 7 days) is bounded at startup by `RENDER_GC_TTL_DAYS` so a
  live URL never outlives its GC'd blob. Absent/invalid signatures fall back to auth + the
  receipt-ownership check, which still gates the web detail page and ad-hoc fetches.
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
- `audit/sink.py` (`JsonlAuditSink`) is constructed once into `app.state.audit_sink` at app
  composition (path from `AUDIT_LOG_PATH`, defaulting to `<storage_dir>/audit.jsonl`). It writes
  one JSON object per line at `0600` and self-rotates at 100 MB (`audit.jsonl.1`), so it needs no
  separate `gc` story. The web `/account` flow is the first live emitter of `pat_issued` /
  `pat_revoked` events; emission is best-effort (the sink swallows write errors and the
  `pat_web.py` helpers swallow any sink exception) so an audit-write failure never blocks the
  user action. Event payloads carry actor, outcome, scope, and `expires_in_days` only — never
  the token plaintext or its hash.
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
  `cli/seed.py` writes to `assets/memegen-seed-manifest.json` for reproducible seeding. The seed
  Job must `git checkout` that pinned commit before importing — cloning bare HEAD drifts the corpus.
- **Memegen metadata: relocation + enrichment.** The upstream `source` URL is provenance, not a
  usage description, so `_build_metadata` puts it in an `origin = {source_url}` block (scheme
  normalized http→https so the existing https-only `sanitize_url`/`origin_source_url_safe` gates
  accept it) instead of `usage_context`. This keeps the URL out of the keyword haystack and the
  embedding (both already exclude `origin`). Empty `description`/`emotion`/`usage_context` are
  optionally overlaid from `assets/memegen-enrichment.json` (web-grounded prose authored offline,
  keyed by slug; force-included in the wheel and resolved like the renderer's font asset). The
  whole metadata dict is routed through `hard_sanitize_metadata` before upsert — the same
  clean-data path uploads use — so authored prose cannot reach the `find`/MCP sink unsanitized; a
  missing/malformed enrichment file degrades to relocation-only.
- `cli/gc_renders.py` and the `meme-mcp gc-renders` CLI prune render outputs by TTL
  (`--ttl-days N`) or by max-byte budget (`--max-bytes N`, LRU by `generated_receipts.created_at`).
  With neither flag it falls back to `RENDER_GC_TTL_DAYS` (default 30) — the single retention knob
  that also caps the signed render-URL TTL (`validate_at_startup`), so a URL can never outlive its
  blob. Scope is the receipts-table — template seed images have no receipt row and are never
  touched. Each delete is guarded by a per-shard `portalocker` advisory lock so GC does not race a
  concurrent `put`. Missing-blob-with-extant-receipt rows are pruned cleanly. The
  `deploy/k8s/cronjob-gc-renders.yaml` manifest schedules a daily sweep (no flag, inherits the knob).
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

- Web (`/` landing, `/browse`, `/auth/*`, `/api/templates`, `/renders/*` via session) uses GitHub
  OAuth with PKCE (S256) and a re-validated allowlist on every request. `GET /` is a public
  landing page (no auth) that points anonymous visitors at GitHub login; an anonymous browser
  hitting `/browse` (no PAT header and no allowlisted session) is 303-redirected to
  `/auth/login?next=/browse`, the same pattern as `/upload`, rather than handed a JSON 401. A PAT
  header still authenticates `/browse` programmatically. `/browse` renders a gallery whose cards
  show a real preview served by `GET /templates/{template_id}/image` (auth-gated the same way;
  content type is taken from the stored file extension, not sniffed, so it renders under the
  gateway's `X-Content-Type-Options: nosniff`, and a missing template/blob is a 404). Each card
  links to `GET /templates/{template_id}`, a detail page (`web/templates/detail.html`) that shows
  the full-size preview plus the template's name, id, source, description, tags, metadata
  attributes, slot list, and a collapsible fingerprint (hashes + stored path). The detail route is
  auth-gated and `find_limiter`-metered exactly like `/browse`, so it cannot be used to enumerate
  template IDs; an anonymous browser is 303-redirected to `/auth/login?next=/templates/<id>` and
  returned to that page after login (`safe_next` accepts single-segment `/templates/<id>` paths,
  but not the `/image` sub-route or `.`/`..` segments). An unknown id is a 404.
  The token exchange targets
  `https://github.com/login/oauth/access_token` and the user-profile fetch targets
  `https://api.github.com/user` — two distinct hosts, not a shared `base_url` client.
- The Streamable HTTP transport is mounted at `/mcp` (real endpoint `/mcp/`). A bare `/mcp` is
  normalized to `/mcp/` in-process by `McpSlashNormalizeMiddleware` so the mount serves it
  without a 307 (which `mcp-remote` cannot follow on POST). The app lifespan
  (`_app_lifespan`) runs the FastMCP `session_manager` for the app's lifetime; a mounted
  sub-app's own lifespan is not run by Starlette, so without this the transport raises "Task
  group is not initialized" on first request.
- The transport runs a DNS-rebinding guard that 421s any Host/Origin not on its allowlist.
  FastMCP only auto-allows localhost, so a public deploy passes its gateway host via
  `MCP_ALLOWED_HOSTS` / `MCP_ALLOWED_ORIGINS` (JSON arrays, wired through `create_mcp_server`'s
  `allowed_hosts`/`allowed_origins`); without them every authenticated request 421s. Bearer-PAT
  auth is the real access control — no ambient browser credentials exist to rebind — so the
  allowlist is defense-in-depth.
- On an unauthenticated MCP request the transport returns `401` with a `WWW-Authenticate`
  header whose `resource_metadata` points at the RFC 9728 document. The advertised OAuth
  issuer/resource URLs are derived from `GITHUB_REDIRECT_URI` (its public scheme+host, minus the
  `/auth/callback` suffix) via `_public_app_base_url`, not hardcoded — so a hosted deploy
  advertises `https://<host>` instead of `http://localhost:8000`. `validate_at_startup` rejects a
  `GITHUB_REDIRECT_URI` that does not end in `/auth/callback` (it would otherwise bake a broken
  path into the metadata). FastMCP only registers that metadata route inside the mounted `/mcp`
  sub-app, so `create_app` mirrors it on the parent app at the origin root
  (`/.well-known/oauth-protected-resource/mcp`) so the advertised URL actually resolves.
  Known limitation: the document lists the app as its own `authorization_server`, but the app
  serves no RFC 8414 `/.well-known/oauth-authorization-server` (MCP auth is static-PAT, not an
  OAuth authorization-code flow). A client that presents a valid PAT gets `200` and never walks
  the discovery chain; only a token-less strict-OAuth client would hit the gap.
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

## Web upload surface

`GET /upload` is a single-page, session-authed screen (`web/templates/upload.html` +
`web/static/upload.js` + `web/static/upload.css`, vanilla JS, no build step). The shared
`styles.css` (loaded on every page) holds the "Quiet Craft" design system — the `:root` tokens,
the `prefers-color-scheme` light/dark palette adapted from Apple's HIG (no pure black; surfaces
lighten with elevation; the accent desaturates in dark mode), the icon variables, base body
theming, and the `.browse`/`.template-card`/`.detail__*` gallery and detail-page rules — so `/`,
`/browse`, `/templates/<id>`, and `/upload` share one look. `upload.css` layers only the
`.upload-*` component rules on top and loads through a `{% block head %}` in `base.html`. The
global `[hidden] { display: none !important; }` rule restores the UA `[hidden]` semantics that an
author `display` (e.g. `.upload-notice { display: flex }`) would otherwise override, which is what
made hidden notices render as empty bars. An unauthenticated or non-allowlisted
visitor is 303-redirected to `/auth/login?next=/upload`; only an allowlisted session reaches
the page, which mints the per-session CSRF token (`web/csrf.py:ensure_csrf_token`) and renders
it into a `<meta name="csrf-token">` tag. The client previews the chosen file locally via
`URL.createObjectURL` (no server-side pending-image route), base64-encodes it, and POSTs the
session-authed JSON endpoints `POST /upload/analyze`, `POST /upload/approve/{id}`, and
`POST /upload/discard/{id}` (`web/upload_routes.py`), each carrying the token as an
`X-CSRF-Token` header. Those endpoints delegate to the shared `upload/service.py` so the
browser and PAT (`/api/uploads/*`) front doors cannot drift. The stored image is
EXIF-stripped and re-encoded, so the page discloses that the saved template may differ from
the local preview. Analyze bodies are capped by the pre-buffer `BodySizeGuardMiddleware`
(~14 MB on `Content-Length`) fronting both analyze paths before the body is buffered. The
client renders the standard JSON error envelope inline (size/type rejection, exact-duplicate
409, near-duplicate non-blocking warn, VLM-suspect acknowledgment gate, rate-limit, CSRF
reject, opaque `NOT_FOUND`, and a 401 session-expired prompt that preserves edited fields).
The `/upload` nav link in `base.html` renders only for an allowlisted session. The session and
PAT authenticators are shared helpers in `auth/session.py` (a PAT never authenticates a web
route -- the web endpoints call them with no Authorization header). Approval validation in the
shared service requires a non-empty, non-placeholder template name, a deliberate tightening
that applies to the PAT `/api/uploads/{id}/approve` path too.

The presentation is a self-contained design system in `upload.css`: CSS custom-property tokens
with a `prefers-color-scheme` dark theme and a `prefers-reduced-motion` fallback, WCAG AA
contrast, and line-art icons drawn with CSS `mask` data-URIs (no fonts, no network). The flow
is choreographed entirely on the client without touching the contract: a drag-or-click
dropzone (a visually-hidden but keyboard-focusable `<input type="file">`, fronted by a
document-level guard that swallows stray file drops so a missed target cannot navigate the tab
away), a file card showing name / size / client-read pixel dimensions, a three-step wayfinder
driven by a `data-current` phase attribute on the root, an animated analyzing state, and a
two-column review at >=720px that places the near-duplicate warn banner and the suspect-ack
gate directly above Approve/Discard. Inline error notices carry `role="alert"`, and focus
moves to the new step on each transition so keyboard and screen-reader users are not stranded
on a now-hidden control.

Pending uploads have a 24h TTL. Discard (and abandonment) deletes only the pending row; the
blob is reclaimed by the daily `gc-uploads` sweep (`cli/gc_uploads.py`, see
`deploy/k8s/README.md`), which is reference-aware -- it deletes a content-addressed blob only
when no template and no surviving pending row references it. `analyze_image` records the
pending row (from `ImageStore.path_for`) before writing the blob, so a re-upload's reference is
observable to the sweep before the bytes land; the grace window is a defense-in-depth margin.

## Web account surface

`GET /account` is a single-page, session-authed screen (`web/templates/account.html` +
`web/static/account.js` + `web/static/account.css`, vanilla JS, no build step) where an
allowlisted friend manages their single MCP PAT without operator involvement. It reuses the
`/upload` front-door pattern exactly: an anonymous or non-allowlisted visitor is 303-redirected
to `/auth/login?next=/account` (`safe_next` accepts `/account`), the page mints the per-session
CSRF token into a `<meta name="csrf-token">` tag, and the two state-changing endpoints
(`web/pat_routes.py`) authenticate session-only via `_session_friend` (a PAT header can never
drive them — they call `friend_from_request_or_header` with no Authorization) and require the
`X-CSRF-Token` header. The `/account` nav link in `base.html` renders only for an allowlisted
session (`has_web_session`). Like `upload.css`, `account.css` layers only its own component
rules on top of the shared `styles.css` and drives every color off the `:root` design tokens, so
the page tracks the `prefers-color-scheme` light/dark palette (an earlier version hardcoded
white panels with inherited text and went unreadable in dark mode).

- `POST /account/token` generates (when none active) or regenerates (when active — the existing
  one-active-token model means `issue_pat` auto-revokes the prior row). `POST /account/token/revoke`
  marks the active row revoked. Both run `app.state.pat_admin_limiter.hit(login)` after auth+CSRF,
  a per-user `RATE_PAT_ADMIN_PER_HOUR` cap; revoking the PAT leaves the web session untouched.
- `auth/pat_web.py` is the request-independent helper layer: it validates the web-allowed scope
  (`read`/`readwrite`) and TTL (a fixed `{30, 90, 365}` set — never-expire `ttl_days=0` is rejected
  with `INVALID_INPUT` and stays operator-CLI-only, the one combination kept behind operator
  involvement), delegates to the store, and emits the `pat_issued`/`pat_revoked` audit events.
- One-time reveal: the plaintext is returned only in the `POST /account/token` success envelope
  (served `Cache-Control: no-store`, fetched with `cache: "no-store"`, so no browser/proxy can
  replay it) and never persisted (only the HMAC digest is) or rendered into the template, so a
  reload cannot re-expose it. The client shows it in a reveal panel with a copy button and a
  "cannot be shown again" warning; regenerate and revoke each gate on a `window.confirm` step.
  The page otherwise renders only the non-secret status (`current_status`): state badge, scope,
  expiry, last-used.

## Reverse-image enrichment

`reverse_image/client.py` (`GoogleVisionClient`) is an optional, deploy-gated step inserted into
the shared `analyze_image` pipeline between the dedupe `block` gate and the VLM call (KTD5). It
exists because a vision model reading the pixels of "Is This a Pigeon?" sees "an anime man
gesturing at a butterfly" and fills `usage_context` as the literal inverse of the meme's real
use; the cultural meaning lives on the web, not in the pixels.

- **Provider + contract.** Google Cloud Vision Web Detection, chosen because it uniquely accepts
  raw image bytes inline (no second public-URL egress surface) under a no-retention data policy.
  The client wraps a reused `ImageAnnotatorClient` built once at startup from an explicit
  service-account file (never the process-wide `GOOGLE_APPLICATION_CREDENTIALS`, which would leak
  the credential scope to every Google client in-process — `config.validate_at_startup` warns when
  it is set). `detect()` returns a frozen `WebDetectionResult(status, grounding, origin)` and
  **never raises** into the pipeline: every SDK/in-body/oversize failure maps to a status, so the
  upload always falls back to today's image-only enrichment.
- **Confidence floor (KTD3).** Web Detection returns *visual-similarity* matches, not identity
  confidence — a derivative meme can confidently reverse-match an old template and produce a
  clean-but-wrong identity that poisons metadata worse than the literal description. So a match
  above a configurable floor on the top web-entity score is `success` (grounding fed to the VLM
  with R3 "prefer over literal" precedence, origin stamped `status="high"`); a weaker match is
  `low_confidence` (origin captured for human review at `status="low"`, grounding fed *without*
  precedence, no `find` alias). The floor's calibration is unproven until measured against the
  real-upload efficacy matrix (validation playbook) — Vision scores are unbounded relevance
  values, not 0–1 probabilities.
- **Untrusted input (R8/KTD6).** All web-recovered text is treated as untrusted. The service is
  the single sanitization owner: `vlm/sanitize.sanitize_web_results` cleans the grounding text and
  `clean_origin_value` cleans the stored origin, enforcing a clean-data invariant (a field still
  tripping `flag_anomalies` after sanitization is hard-dropped to empty) so stored origin — and
  therefore `find`/MCP output to agents — is guaranteed clean without a read-time pass.
  `source_url` is https-allowlisted via `sanitize_url` (a canonical change to
  `hard_sanitize_metadata`, so the friend's edited URL on approve is covered too; it also rejects
  userinfo URLs like `https://trusted.com@evil.example/x` that impersonate a trusted host) and
  rendered through autoescape; `detail.html` additionally gates the link `href` server-side because Jinja
  autoescape does not neutralize a `javascript:` href. The in-prompt isolation (grounding fenced
  in `WEB_CONTEXT` markers, framed as data-not-instructions) is best-effort defense-in-depth; the
  structural defenses are the out-of-band store-sanitize, the https allowlist, and autoescape.
- **The `origin` block** lives inside `metadata_json` (no schema migration): `{name, source_url,
  status}`. `origin.name` is a provenance/search alias distinct from the editable display
  `metadata.name`; `retrieval/search.py` applies the existing `+10` name-boost to an
  `origin.name` hit (tagged `origin_name_match`) only when the persisted `origin.status == "high"`
  — the runtime status does not survive to query time, so the trust bit must be stored. Approve
  gates promotion to `status="high"` on an explicit `origin_reviewed` signal that ONLY the web
  review surface passes (the friend saw and could edit the origin fields); the PAT/API approve door
  never promotes, so a programmatic client cannot launder a low-confidence origin to high-weight by
  omitting `origin.status`. `origin.source_url` is excluded from the term-match haystack (a URL is
  not descriptive text). Memegen-seeded rows reuse this block in its provenance-only shape —
  `{source_url}` with no `name`/`status` — so the reference link renders (the detail page titles
  that panel "Source" rather than "Origin" and labels the row "Reference") but never earns the
  `origin_name_match` alias bonus.
- **Egress departure (privacy).** Enabling the feature sends uploaded images off-box to Google for
  the first time beyond the VLM provider. Egress occurs the instant the lookup is invoked — a
  `timeout`/`error` status does **not** mean the bytes stayed local; no-retention is provider
  policy, not a technical guarantee. The web-form toggle (default on, hidden when the feature is
  off) is the only pre-send guard; PAT clients are opt-in per call (`identify_online=true`),
  defaulting OFF so enabling the feature never silently begins egressing for programmatic callers
  (KTD7). Only EXIF-stripped, re-encoded bytes are ever sent.
- **Operator liveness (KTD10).** Silent degradation protects the friend's UX but would hide a
  fully-broken integration (expired/wrong-scope credentials, quota exhaustion, region errors) —
  every upload would silently revert to the inverted-output behavior the feature exists to fix. So
  the client emits redacted structured logs (no image bytes, no provider `error.message`)
  distinguishing `timeout`/`error` from the expected `no_match`, so operators can alert on a
  sustained failure rate. The friend-facing `reverse_image_status` on the analyze response
  (`success`/`low_confidence`/`no_match`/`skipped`/`unavailable`) is distinct from this: it only
  shapes the review-form copy, never shows an error.

## Storage backends

`PgVectorStore` ships in v1.5 (sync `psycopg` + `pgvector.psycopg.register_vector`); the
factory dispatches it for any `postgresql...` URL. The Alembic `0002_vector_ddl` revision
installs the pgvector extension and the `vector(1536)` column with an ivfflat cosine index
on Postgres; SQLite stays on the JSON-backed `template_vectors` table from the baseline.
`S3ImageStore` ships in v1.5 as well (sync `boto3` against S3 or any S3-compatible endpoint
like MinIO, R2, B2); content-addressed keys match the filesystem store's `<aa>/<bb..>.ext`
layout, `put` is idempotent via `HeadObject`-then-`PutObject`, and `get` raises
`FileNotFoundError` on `NoSuchKey` so callers see the same shape as the filesystem path.
`make_image_store` dispatches on `image_store_backend`. See `docs/MIGRATION.md` for the
end-to-end switch and `tests/test_vectors_postgres.py` + `tests/test_s3_image_store*.py` for
the parity / smoke suites.

## Visual parity testing

`tests/test_visual_parity_golden.py` compares the renderer's output against pre-rendered golden
images from `memegen.link` using `imagehash.dhash` Hamming distance (threshold 8 on 256x256). The
suite skips when `/tmp/memegen-upstream/templates` is absent and records distances to
`assets/golden/parity-distances.json`. All 10 representative templates pass under the threshold
after the visual-parity renderer landed.
