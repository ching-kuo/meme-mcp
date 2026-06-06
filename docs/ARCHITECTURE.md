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
  shrink-loop to find the largest font size that fits the box with memegen-matching margins.
- Font is chosen per caption: a caption containing any CJK codepoint renders in
  `NotoSansTC-Black.otf` (Noto Sans TC Black, OFL 1.1, ~6 MB, covers Latin too so mixed
  captions stay one face); pure-Latin captions keep `Anton-Regular.ttf` and are byte-identical
  to before (the golden visual-parity suite is the hard gate). For CJK, `text_layout.segment_tokens`
  breaks text into wrap tokens — Latin runs stay word-atomic, each CJK char is its own token — and
  `greedy_wrap` applies minimal kinsoku (closing punctuation 」』。，！？、） never starts a line;
  opening 「『（ never ends one). `fit_font`/`select_wrap` reserve stroke-outline room
  (`stroke_ratio`) so the bold CJK outline never clips. Both fonts ship via the same wheel
  `force-include` of `assets/fonts` and the Docker `COPY . .`.
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
  header still authenticates `/browse` programmatically. The signed-in nav carries a Sign-out
  button (`POST /auth/logout`, which clears the session); like every state-changing request it is
  CSRF-protected via the header-only `X-CSRF-Token`, so the button is a small `fetch` carrying the
  per-session token rather than a plain form. The session cookie has no explicit `max_age`, so it
  uses Starlette's 14-day default (a friend stays signed in for two weeks unless they sign out or
  are de-allowlisted, which is re-checked on every request).
- **Prefetch guard on sign-in initiation.** Both OAuth-init GETs (`/auth/google/login` and the
  GitHub branch of `/auth/login`) return a no-op `204` + `Cache-Control: no-store` for speculative
  requests (`Sec-Purpose: prefetch`, `Purpose`/`X-Purpose`, `X-Moz: prefetch`) via `_is_prefetch`.
  Aggressive link preloading (e.g. Arc) would otherwise re-run the init route and rotate the OAuth
  `state` -- Authlib's `set_state_data` keeps only the latest `_state_google_*` -- so the redirect
  the user actually clicked would carry a stale state and the callback would fail with a CSRF state
  mismatch. The guard fires before any session write, so only a real navigation starts the flow.
- **CSP: no inline scripts.** The gateway sets `Content-Security-Policy: default-src 'self'` (see
  `deploy/k8s/` / the infra `httproute`), which blocks inline `<script>` and inline event handlers
  at runtime even though they render fine in tests. All executable JS therefore lives in
  `web/static/*.js` and is included with `<script src=... defer>`; `web/static/base.js` (loaded on
  every page) holds the i18n `t()` bootstrap and the logout handler. The `<script
  type="application/json" id="i18n-catalog">` block is data, not executed, so it is exempt. A
  render guard (`test_rendered_pages_have_no_inline_scripts`) fails the build if an inline script
  reappears. `/browse` renders a gallery (paginated 24 templates per page via `?page=N`; the
  full row set is loaded for the count and search reorder, then sliced in Python, and an
  out-of-range or non-numeric `page` clamps to the nearest valid page rather than erroring)
  whose cards
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
  issuer/resource URLs come from the canonical public base URL resolved by
  `config.resolve_public_base_url`: the optional, provider-independent `PUBLIC_BASE_URL` when set,
  else derived from `GITHUB_REDIRECT_URI` (its public scheme+host, minus the `/auth/callback`
  suffix) — so a hosted deploy advertises `https://<host>` instead of `http://localhost:8000`. This
  origin also signs `rendered_url` values, so `validate_at_startup` fails closed when
  `PUBLIC_BASE_URL` and `GITHUB_REDIRECT_URI` resolve to different origins (a silent origin change
  would invalidate every outstanding signed render URL), and still rejects a `GITHUB_REDIRECT_URI`
  that does not end in `/auth/callback` (it would otherwise bake a broken path into the metadata).
  The session-cookie `Secure` flag follows the same canonical origin (`config.session_cookie_secure`). FastMCP only registers that metadata route inside the mounted `/mcp`
  sub-app, so `create_app` mirrors it on the parent app at the origin root
  (`/.well-known/oauth-protected-resource/mcp`) so the advertised URL actually resolves.
- **MCP OAuth 2.1 authorization server (`OAUTH_AS_ENABLED`, OFF by default; KTD — supersedes the
  prior "no MCP OAuth" decision).** When enabled, meme-mcp *is* its own authorization server, so
  Claude's native custom-connector UI can connect with a URL + sign-in — no `npx mcp-remote`
  bridge and no boot/wake connect-timeout race (Anthropic's cloud brokers the connection).
  `create_mcp_server` switches FastMCP from `token_verifier=` to
  `auth_server_provider=MemeAuthProvider` (`oauth/`), which auto-mounts the five OAuth routes plus
  RFC 8414 metadata. FastMCP mounts those *inside* `/mcp`, but the metadata advertises them at the
  origin root, so `create_app` mirrors them onto the parent app at the origin root
  (`build_auth_server_routes`) — the same trick as the RFC 9728 mirror — and patches the metadata
  to also advertise the public-client `none` auth method. Human login reuses the existing
  GitHub/Google sign-in; the AS issues its **own** opaque tokens (never forwarding an upstream
  token), gated by a per-`(user, client)` consent screen (`oauth/consent.py`) and the friend
  allowlist *at issuance*. Opaque access/refresh tokens are HMAC-hashed at rest with rotation +
  reuse detection; a confidential client's secret is stored encrypted (reversible, since the SDK
  compares it directly). This **reverses** the earlier decision to ship no RFC 8414 metadata; the
  reversal was deliberate — the recurring launch-race pain and native-connector reach (claude.ai +
  mobile, no shared PAT) now outweigh the avoided authorization-flow complexity. When the flag is
  OFF, behavior is unchanged and the prior incomplete-discovery gap remains for a strict-OAuth
  client (a valid PAT still gets `200`).
- MCP (`/mcp` and `/api/mcp/*`) accepts a PAT in `Authorization: Bearer …` in **both** modes. PAT
  verification is HMAC-SHA-256 with a server-side pepper; in AS mode the provider's
  `load_access_token` recognizes both newly-issued OAuth access tokens **and** existing PATs, so
  the `mcp-remote` + PAT path keeps working with no client change. A token grants `meme:read`
  always and `meme:write` only for a `readwrite` capability (shared `scopes_for_capability`, so
  the PAT and OAuth paths cannot diverge). The MCP `generate` tool and the `/api/mcp/generate`,
  `/api/uploads/analyze`, and `/api/uploads/{id}/approve` HTTP routes gate on `meme:write` (or
  `friend.capability == "readwrite"` for HTTP) before any backend work; read-scope tokens receive
  `UNAUTHORIZED` at the wrapper.

The MCP tool wrappers derive the rate-limit actor from the validated `AccessToken` via
`mcp.server.auth.middleware.auth_context.get_access_token()` — never from
`Context.client_id`, which is client-supplied request metadata and therefore spoofable. Tool
calls without a verified access token raise `UNAUTHORIZED` at the wrapper before any backend
work runs.

## Multi-provider identity (GitHub + Google)

Identity is a provider-namespaced **principal** string — `github:<login>` or `google:<sub>` —
not a bare GitHub login. GitHub and Google identities are independent: separate allowlist
entries, PATs, history, and audit trails, never linked or merged. Google sign-in is config-gated
(`GOOGLE_OAUTH_ENABLED`, OFF by default, mirroring reverse-image); when off, only GitHub login is
offered and the GitHub path is untouched.

- **One authorization predicate, four front doors (KTD).** `auth/authorization.py` is a
  dependency-free leaf exposing `normalize_principal` and `is_authorized(principal, *, allowlist,
  pin_store)`. It imports nothing from `depends`/`session`/`app`, because `session.py` already
  imports `require_pat` from `depends.py`; a leaf both import *down* into is the only placement
  that keeps the import graph acyclic while guaranteeing the browser session
  (`session_login`), the web PAT (`require_pat`), the MCP transport PAT
  (`PatTokenVerifier.verify_token`), and — when the AS is enabled — the MCP OAuth token path
  (`MemeAuthProvider.load_access_token`) cannot diverge. Authorization is re-evaluated **per
  request** against live allowlist + pin state (no caching), so de-allowlisting or evicting a pin
  denies the *next* request, not only fresh logins. `tests/test_oauth_front_door_contract.py`
  asserts the OAuth path delegates to `is_authorized` so a future refactor cannot inline a bare
  membership check.

- **Idempotent, prefix-preserving normalization (KTD).** `normalize_principal` is the single place
  the default `github:` prefix is applied. A value already carrying a known `provider:` prefix is
  returned unchanged; a legacy bare value becomes `github:<value>` only when it matches a
  conservative login charset; a bare value containing `@` or `:` is rejected, never smuggled into a
  GitHub principal. Legacy bare values (allowlist entries, PAT rows, sessions, receipts, pending
  uploads) read as `github:<value>` with no data migration — reads match both the namespaced
  principal and the bare form, and a reissue revokes a friend's pre-namespace PAT.

- **Two resolvers, one identity type (KTD).** Both callbacks converge on a frozen
  `ResolvedIdentity(provider, subject, email, email_verified)`. GitHub stays hand-rolled (plain
  JSON); Google uses Authlib (`auth/google_oauth.py`) for OIDC discovery, PKCE, `state`, `nonce`,
  and ID-token validation. The Google callback reads `sub`/`email`/`email_verified` from the
  nonce-validated `id_token` (`token["userinfo"]`), never a separate `/userinfo` fetch, so the
  authz-bearing claims stay bound to the validated token. A separate callback path
  (`/auth/google/callback`) is the RFC 9700 mix-up defense for running two IdPs.

- **Trust-on-first-use `sub` pinning (KTD).** The operator invites a friend by their Google account
  email (`google:<email>`). On the first sign-in where `email_verified` is strictly boolean `true`
  (the domain is not restricted to `@gmail.com` — any verified Google mailbox, e.g. `@icloud.com`,
  is accepted, because authorization keys on the full allowlisted email plus the `sub` pin, so a
  Workspace admin cannot mint an arbitrary allowlisted address), the
  app records a durable `sub -> email` pin (`auth/google_pins.py`, `google_pins` table) and the
  principal becomes `google:<sub>` — the immutable subject, never the mutable email, so an email
  rename does not revoke access (a returning `sub` is authorized against its pinned, allowlisted
  email; the live claim email is ignored). The `email UNIQUE` constraint enforces
  **first-sign-in-wins**: a second `sub` claiming an already-pinned email is rejected at the DB
  layer. PATs and audit bind to `google:<sub>`; the allowlist stays email-keyed and
  operator-friendly, with Gmail canonicalization (lowercase, strip local-part dots, drop `+suffix`)
  applied to Gmail addresses only — other domains match exactly — identically on the stored entry
  and the claim.

- **Terminal revocation and the residual race (KTD).** Both `pin revoke` and `allowlist remove
  google:<email>` **delete the pin row**; the deleted pin is never silently re-authorized, so
  re-establishing access always takes a fresh interactive sign-in (R13). The two differ by intent:
  `allowlist remove` is the **de-invite / kick-out** path — it removes the invite *and* the pin, so
  the account is denied for as long as the invite is absent (re-admitting requires the operator to
  re-add the invite). `pin revoke` deletes the pin but **leaves the invite**, so it is a *rotation*
  tool (evict a wrong first-sign-in-wins pin): the next sign-in re-pins, and if the invite still
  stands the same `sub` can re-win it — the residual race re-applies until the operator confirms the
  intended `sub` via `pin show`. The accepted residual risk: because an email
  address can be reclaimed, whoever first presents a verified, allowlisted address wins the pin; the
  strict `email_verified` gate, terminal revocation, and operator inspection (`pin show`/`pin list`)
  are the mitigations, and the documented remediation for a wrong pin is revoke-pin → re-invite →
  confirm the new `sub`. Hand-editing the allowlist file to remove a line denies access while removed but
  does not delete the pin; `pin revoke` is the authoritative rotation path. MCP transport auth is
  unchanged — a Google friend gets parity purely by holding a PAT whose subject is `google:<sub>`;
  no OAuth is wired into the MCP transport and RFC 8414 metadata stays intentionally absent.

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

Localized review (U4, R9): the web `/upload/analyze` route resolves the author's UI locale via
`resolve_locale(request)` (the same lang-cookie → Accept-Language → `en` chain as the chrome) and
passes it into `analyze_image`; the PAT door passes `locale="en"` (agents review the canonical
English proposal). `analyze_image` always emits the machine zh-TW counterpart and drift-gates it
regardless of view. The review form's flat fields are the author's-locale VIEW: `upload.js` reads
`<html lang>`, and when it names a content locale it renders `locales[locale]` values (per-field
English fallback) and reconstructs the approve payload by mapping edits into `locales[locale]`
while carrying the canonical top-level English (and the machine counterpart) verbatim from the
analyze proposal — Chinese never replaces the English `name`, so `template_id`/slug keep deriving
from it. The per-field `_meta` from the proposal rides along so the server's
`_stamp_human_locale_edits` can diff edited-vs-machine and stamp only changed fields human; an
approve payload that lacks `locales` is merged (not overwritten) against the stored row via
`merge_locales`, so a form built before a backfill cannot clobber a backfilled block.

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

## Bilingual UI (i18n)

The web UI renders in English or Traditional Chinese (`zh-TW`). The whole layer lives in
`web/i18n/` and adds no runtime dependency and no build step (KTD1): `catalog.py` is a typed
`MESSAGES: dict[str, dict[str, str]]` keyed by a dotted message id, each carrying an `en` and a
`zh-TW` string, and it is the single source of truth for both server-rendered templates and the
client-side JS strings. `core.py` is the pure engine — `resolve_locale`, `t`, `plural`,
`js_catalog`, and two lint helpers — with no Starlette coupling beyond a type-only import.

- **Locale precedence (one place only):** `resolve_locale(request)` reads the `lang` cookie first
  and honors it when it names a supported locale; otherwise it negotiates `Accept-Language`
  (`negotiate_accept_language`: `zh*` → `zh-TW`, `en*` → `en`, `q=0` dropped per RFC 7231, no match
  → `None`); otherwise it returns `DEFAULT = "en"`. The manual switch is the load-bearing mechanism
  and auto-detect is best-effort: `Accept-Language` is the browser's advertised language, not the
  OS, so a `zh-TW` reader on an en-locale browser lands on English first and is served by the
  always-visible switch, not by detection.
- **Injection via context processor (no per-route plumbing, KTD3):** `_i18n_context` in `app.py` is
  attached to the shared `Jinja2Templates` instance (the same one `pat_routes.py` reuses via
  `app.state.web_templates`), so every `TemplateResponse` receives `t`/`plural` bound to the
  resolved locale plus `locale`, `supported_locales`, and `js_catalog_json` — the per-route context
  dicts are untouched. `LocaleVaryMiddleware` adds `Vary: Cookie, Accept-Language` to HTML
  responses so a future CDN keys cached pages on the locale signals.
- **`t()` fallback and interpolation:** lookup resolves `MESSAGES[key][locale]`, falling back to the
  `en` string and then to the literal key (R5 — a defensive path the guard tests catch, never a
  shipped state, since `check_completeness` requires both locales for every key). Interpolation uses
  `str.format` wrapped to degrade to the unformatted string on a typo/mismatch rather than 500.
  Catalog values may use **named placeholders only** (`{count}`); `lint_placeholders` rejects
  positional/attribute/index access, closing the format-string injection surface (KTD4).
- **Manual switch:** `GET /lang/{locale}` validates the locale against `SUPPORTED`, sets the `lang`
  cookie (`Path=/`, `Max-Age` ~1yr, `SameSite=Lax`, `HttpOnly`, `Secure` off only on localhost —
  mirroring the session cookie), and 303-redirects to a validated relative target. `safe_lang_return`
  in `web/csrf.py` is a thin parameterized delegate of `safe_next` (same anti-open-redirect core,
  one implementation) with the switch's own all-rendered-pages allowlist (including the landing `/`,
  which the login allowlist omits) and query preservation so a switch on a search page keeps the
  query. The switch is a plain GET that sets a non-sensitive preference cookie, so it needs no CSRF
  token; the trade-off (a cross-site GET can force a wrong UI language, one-click-correctable) is an
  explicitly accepted risk. The switcher control (`base.html`) renders before/outside the
  `web_session` nav guard with own-script autonym labels, so it is identical and comprehensible on
  anonymous and signed-in pages alike.
- **Client JS (KTD6):** `base.html` emits the active locale's `js.*` catalog subset as a
  `<script type="application/json" id="i18n-catalog">` blob plus a tiny bootstrap that parses it into
  `window.I18N` and exposes a `t(key, vars)` mirroring the server interpolation contract — both in
  `<head>` before the deferred `account.js`/`upload.js`, so `window.I18N` is defined when they run.
  `account.js`/`upload.js` read every user-facing string through `t()`; account token status enum
  values (`state`/`scope`/`none`/`never`) localize at the display layer from the same `js.token.*`
  keys the server uses, so first paint and AJAX re-render agree (the API contract and stored values
  are unchanged). The blob serializer escapes every `<` to `<` (`_js_catalog_json`) so a catalog
  value containing `</script>` cannot break out of the tag; `JSON.parse` restores it.
- **Coverage guards:** mechanical tests keep the catalog in lockstep (`check_completeness`), forbid
  raw keys in rendered output, check every catalog key is referenced (orphan check), and grep both
  templates and JS for known pre-existing English literals left outside a `t(...)`/`I18N` lookup —
  the load-bearing guards against a half-translated UI, since presence/completeness tests cannot
  catch a string silently left hardcoded. Translation *correctness* (vs. mere key presence) is gated
  by a native `zh-TW` review before shipping, not by the mechanical tests. Scope here is UI chrome —
  the chrome is fully localized, while friend-authored template content carries its own bilingual
  payload (see **Bilingual template metadata** below). Locale-aware date formatting is deferred.

## Bilingual template metadata

Template metadata is canonical English at the top level with an optional locale overlay so
non-English-proficient friends can read and search templates in Traditional Chinese. The layer is
distinct from the UI i18n above: i18n localizes chrome strings; this localizes friend-authored
content (`name`, `description`, `emotion`, `usage_context`, `tags`). `metadata_locales.py` owns the
overlay model; the supported content locales are `{"zh-TW"}` (`SUPPORTED_CONTENT_LOCALES`).

- **Shape.** Localized values live under `metadata.locales["zh-TW"]`, mirroring the five
  `LOCALIZED_FIELDS`, alongside a `_meta` block carrying per-field provenance:
  `{field: {"source": "human"|"machine", "drift": "pass"|"failed"}}`. Top-level English is always
  authoritative — it backs the slug, the MCP contract, and the English fallback.
- **Resolution.** `localize()` resolves one field as requested-locale-then-English fallback;
  `localized_metadata(metadata, locale)` overlays the whole display copy for the web layer;
  `english_metadata()` strips `locales` for the canonical agent-facing projection.
- **Provenance and merge (U4).** The VLM produces machine zh-TW; `upload.service._prepare_machine_locales`
  stamps it `source: "machine"`. On approve, `_stamp_human_locale_edits` diffs the submitted locale
  fields against the *sanitized* pending baseline (so sanitization-only normalization is not mistaken
  for an edit) and re-stamps only the fields the friend actually changed as `source: "human"`. `merge_locales` then enforces human-wins/machine-fills: a stored human value is
  never overwritten by an incoming machine value, and a missing incoming locale preserves the stored
  block. This protects friend edits from a later machine backfill while still letting backfill improve
  untouched machine fields.
- **zh-CN drift gate + one retry (U3/U4).** `vlm/drift.py` rejects Simplified-Chinese /
  mainland-vocabulary leakage in the model's zh-TW output. `check_drift` uses `hanzidentifier` as
  the authoritative classifier — Simplified or Mixed text is rejected — and a curated
  `MAINLAND_VOCAB_DENYLIST` (e.g. 視頻→影片, 質量→品質, 軟件→軟體) that ALWAYS runs to catch whole
  mainland words, including ones spelled entirely with characters that are also valid Traditional
  (后台→後台, 程序→程式), which the identifier classifies as `BOTH`. The per-character `SIMPLIFIED_ONLY`
  scan is a fallback used ONLY when `hanzidentifier` is unavailable; it lists genuinely Simplified-only
  characters, deliberately excluding shared Traditional characters (件 信 台 程 …) so legitimate zh-TW
  prose is not false-positive-rejected. `upload.service._resolve_machine_locales` runs the gate at analyze time: on a clean
  pass it stamps via `_prepare_machine_locales`; on failure it issues ONE constrained re-prompt
  (`enrich_template(drift_retry=True)`, which tightens the zh-TW instruction) and re-checks. If the
  retry is also dirty (or unusable), the zh-TW content is dropped (English-only) but per-field
  `drift: "failed"` provenance is persisted inside `locales._meta` so the failure is recorded and a
  later backfill can target it — a top-level `_`-prefixed marker would be silently stripped by
  `hard_sanitize_metadata`. The retry is the ONLY extra LLM call; approve never re-enters this path,
  so it never blocks on or re-invokes the model. A schema-degraded response with no zh-TW prose is
  left English-only with no empty locale block written.
- **Sanitization (U1).** `vlm/sanitize._sanitize_locales_block` keeps only `SUPPORTED_CONTENT_LOCALES`,
  validates `_meta` entries against the `source`/`drift` enums, and enforces per-field shape — `tags`
  must be a list, prose fields must be strings; wrong-shaped values are dropped, not coerced.
  `flag_anomalies` walks locale content (skipping `_`-prefixed keys) and trips `imperative_prompt` on
  Chinese-language injection phrasing (e.g. 忽略之前, 系統：) as well as the English markers, since
  zh-TW content is part of the prompt-injection threat surface. `_clean_string` NFKC-folds
  unconditionally, so fullwidth ASCII in zh-TW prose folds to ASCII while Han characters are untouched.
- **CJK search (U6).** `retrieval/search.py` routes CJK-bearing query terms away from the
  whitespace/substring path (a one-char substring hit would otherwise full-score every haystack
  containing that character) and into a bigram-overlap path: a multi-char query scores only on
  consecutive-pair overlap, and a single-char query (which has no bigram) gets a damped flat boost.
  The flattened haystack includes locale content but skips `_meta`.
- **Embedding (U7).** `embeddings.client.embedding_text` appends each locale block's prose and tags to
  the embedded text so zh-TW queries retrieve by meaning, not just lexical overlap. Semantic search
  defaults to a local Ollama OpenAI-compatible endpoint (`EMBEDDING_MODEL=qwen3-embedding:0.6b`,
  `EMBEDDING_DIMENSIONS=1024`); vectors are L2-normalized on both the index and query paths. Switching
  model or dimensions requires `meme-mcp reindex-embeddings --force`, which clears the vector and meta
  stores before rebuilding so the boot guard (`validate_embedding_model`) does not stay latched on
  stale rows.
- **MCP projection (U8).** `project_candidate_english` (and the `find` wrappers) project candidates
  through `english_metadata`, so `locales` never leaks into the agent-facing MCP/HTTP `find` response —
  agents always see canonical English.
- **Corpus backfill (U5).** The seed corpus has no upload flow, so its zh-TW is produced once as a
  reviewable `assets/memegen-enrichment.zh-TW.json` overlay. The shipped overlay is web-grounded: each
  meme was researched online for its Traditional-Chinese / Taiwan usage and localized from the English
  enrichment asset, then drift-gated per field (drifted fields fall back to English; nothing drifted is
  written). The `translate-corpus` CLI is the reproducible text-only regeneration path (VLM-translates
  the English asset, same per-field drift gate with retry-once-then-skip, fills only missing slugs).
  `corpus/upstream.py` attaches the overlay
  as `locales["zh-TW"]` with per-field machine provenance (`drift: "pass"`) at import, sanitized through
  the same locales dispatch; `name` stays English (localize falls back). `import_upstream_corpus` reads
  the stored row and runs `merge_locales` before the full-row upsert, so a re-seed never clobbers a
  human-corrected zh-TW field with the rebuilt machine overlay (the importer honors the same human-wins
  invariant as approve). Clearing/deleting a stored human locale value is post-approval editing, which
  is deferred (see Scope Boundaries in the plan).

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
  `origin_name_match` alias bonus. `_sanitize_origin_block` is whitelist-only (`ALLOWED_ORIGIN_KEYS =
  {name, source_url, status}`): any other key — including a nested or `_`-prefixed value — is dropped,
  so origin cannot become an unscanned passthrough channel (the anomaly scan skips `_`-prefixed keys).
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
