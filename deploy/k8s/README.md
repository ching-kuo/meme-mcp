# Kubernetes

Example manifests for the hosted deployment path. Copy `secret.example.yaml` to `secret.yaml`
locally; `secret.yaml` is gitignored.

## Image

The manifests reference `ghcr.io/ching-kuo/meme-mcp:latest`, published by CI
(`.github/workflows/build-and-push.yml`) on pushes to `main`, `v*` tags, and manual runs.
Pin a specific build with the `sha-<short>` or `MAJOR.MINOR.PATCH` tag instead of `latest` for
reproducible rollouts. The GHCR package is private by default; make it public or add an
`imagePullSecret` so the cluster can pull it. To use a different registry, override every
`image:` field (Deployment plus both CronJobs).

## Storage and database

Two supported topologies:

- **SQLite + filesystem on the `meme-mcp-storage` PVC** — the default. Single-pod, simplest to
  operate, suitable for small-to-moderate corpus sizes.
- **Postgres + pgvector + S3-compatible object storage** — ships in v1.5 (see `docs/MIGRATION.md`).
  Use this when you need horizontal scale, a managed DB, or off-cluster image durability.

### SQLite + filesystem (default)

ConfigMap settings:

```yaml
data:
  STORAGE_DIR: "/data"
  DATABASE_URL: "sqlite+aiosqlite:////data/meme-mcp.db"
  IMAGE_STORE_BACKEND: "filesystem"
  IMAGE_STORE_FS_PATH: "/data/images"
```

The Deployment mounts the PVC at `/data`. The pod-level `fsGroup: 10001` lets the non-root app user
write the mounted volume.

### Postgres + pgvector + S3

`cnpg-cluster.example.yaml` provisions a single-instance CloudNativePG cluster as a starting
point — adjust `instances`, `storage.size`, and add a `bootstrap` block before production use.
The pgvector extension is installed automatically by Alembic revision `0002_vector_ddl` on first
boot against Postgres.

ConfigMap settings:

```yaml
data:
  STORAGE_DIR: "/data"
  DATABASE_URL: "postgresql+psycopg://meme:<password>@meme-mcp-postgres-rw/meme"
  IMAGE_STORE_BACKEND: "s3"
  S3_ENDPOINT: "https://s3.example.com"
  S3_BUCKET: "meme-mcp"
  S3_REGION: "us-east-1"
```

`S3_ACCESS_KEY_ID` and `S3_SECRET_ACCESS_KEY` belong in the `meme-mcp` Secret, not the ConfigMap.
Build the image with `uv sync --extra postgres --extra s3` so `psycopg`, `pgvector`, and `boto3`
are available at runtime.

A copy-paste-able bundle for this topology — a production-shaped CNPG cluster (HA, anti-affinity,
pgvector pre-installed at bootstrap), an S3-specific ConfigMap, and a Secret stub with the
DATABASE_URL pattern — lives in `examples/s3-postgres/`. See its README for the apply order and
the one-liner that builds DATABASE_URL from the CNPG-managed app password.

### Migrations run at boot

`src/meme_mcp/db/migrations.py:run_migrations` runs on startup and brings the configured database
to Alembic head, so no manual `alembic upgrade head` step is needed. Inline `CREATE TABLE IF NOT
EXISTS` calls in store `__init__`s remain as defense-in-depth, but the canonical schema lives in
`alembic/versions/`.

### Cutover from SQLite+filesystem to Postgres+S3

Use the `meme-mcp migrate` orchestrator. Run a `--dry-run` first to validate that `pgloader` and
`rclone` are on `$PATH`, that the target Postgres has the `vector` extension installed, and that
the S3 bucket allows head/put/get/delete:

```bash
kubectl exec deploy/meme-mcp -- /app/.venv/bin/meme-mcp migrate \
  --target-db postgresql+psycopg://meme:<password>@meme-mcp-postgres-rw/meme \
  --target-s3-endpoint https://s3.example.com \
  --target-s3-bucket meme-mcp \
  --target-s3-access-key "$S3_ACCESS_KEY_ID" \
  --target-s3-secret-key "$S3_SECRET_ACCESS_KEY" \
  --target-s3-region us-east-1 \
  --dry-run
```

Without `--dry-run` the command `chmod 0550`s `STORAGE_DIR` (read+execute, no write) for the run,
executes `pgloader` then `reindex-embeddings` then `rclone sync`, and writes `.env.next` with the
suggested ConfigMap/Secret diff. After cutover, apply the new ConfigMap values, rotate the Secret
with the S3 credentials, and `kubectl rollout restart deploy/meme-mcp`. See `docs/MIGRATION.md`
for the full procedure and the failure-code table.

## Seed memegen templates

`memegen.link` is not queried at runtime. The app imports template images and metadata into its own
SQLite database, then renders locally.

After deploying the app, seed the built-in deterministic starter corpus once:

```bash
kubectl exec deploy/meme-mcp -- /app/.venv/bin/meme-mcp seed-memegen
kubectl exec deploy/meme-mcp -- /app/.venv/bin/meme-mcp reindex-embeddings
kubectl rollout restart deploy/meme-mcp
```

To import the full upstream memegen template library, run a one-off Kubernetes Job against the same
PVC. The Docker image includes `git` so the Job can clone upstream directly:

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: meme-mcp-seed-memegen
spec:
  template:
    spec:
      restartPolicy: Never
      securityContext:
        fsGroup: 10001
      containers:
        - name: seed
          image: ghcr.io/ching-kuo/meme-mcp:latest
          securityContext:
            allowPrivilegeEscalation: false
            runAsNonRoot: true
            runAsUser: 10001
          command: ["/bin/sh", "-lc"]
          args:
            - |
              set -e
              git clone https://github.com/jacebrowning/memegen.git /tmp/memegen
              # Pin the checkout so re-seeds reproduce the corpus the in-tree
              # manifest was generated from -- cloning bare HEAD drifts the corpus.
              git -C /tmp/memegen checkout 3e1cdd13a2e914d51b96fc5916b507904fb74d5a
              /app/.venv/bin/meme-mcp seed-memegen \
                --upstream-path /tmp/memegen \
                --manifest-path /data/memegen-seed-manifest.json
              /app/.venv/bin/meme-mcp reindex-embeddings
          envFrom:
            - configMapRef:
                name: meme-mcp
            - secretRef:
                name: meme-mcp
          volumeMounts:
            - name: storage
              mountPath: /data
      volumes:
        - name: storage
          persistentVolumeClaim:
            claimName: meme-mcp-storage
```

Apply and watch it:

```bash
kubectl apply -f meme-mcp-seed-job.yaml
kubectl logs job/meme-mcp-seed-memegen -f
kubectl rollout restart deploy/meme-mcp
```

Re-run the Job when you intentionally want to refresh from upstream memegen. The seed command is an
upsert into existing `source="memegen"` templates. Completed Jobs aren't re-executed by
`kubectl apply`, so delete first:

```bash
kubectl delete job meme-mcp-seed-memegen --ignore-not-found
kubectl apply -f meme-mcp-seed-job.yaml
```

`--manifest-path` routes the parity manifest to the writable PVC because `/app/assets/` is root-owned
in the container image. The in-tree `assets/memegen-seed-manifest.json` is only updated by operators
running `seed-memegen --upstream-path` locally before committing.

## Render GC

`cronjob-gc-renders.yaml` schedules a daily 30-day TTL sweep over `generated_receipts` and their
backing image blobs. Each delete is guarded by a per-shard `portalocker` advisory lock so the GC
does not race a concurrent `put`. Template seed images have no receipt row and are never touched.

Apply once per environment:

```bash
kubectl apply -f deploy/k8s/cronjob-gc-renders.yaml
```

Tune `--ttl-days` or switch to a max-byte LRU budget by editing the `command:` array (e.g.
`["uv", "run", "meme-mcp", "gc-renders", "--max-bytes", "5368709120"]` for a 5 GiB cap). Use
`--dry-run` locally to preview the deletion set before changing the schedule.

## Pending-upload GC

`cronjob-gc-uploads.yaml` schedules a daily sweep over expired pending-upload rows (24h TTL)
and their orphaned image blobs. Discarding or abandoning an upload deletes only the pending
row; this sweep is the sole path that reclaims the blob. It is reference-aware -- a blob is
deleted only when no template row and no surviving pending row (live, or expired-but-within
the grace window) references it, so a content-addressed blob shared across uploads is never
removed while anything still needs it. `analyze` records the pending row before writing the
blob, so a concurrent re-upload's reference is visible to the sweep. Unlike render GC it
builds the image store via `make_image_store_from_settings`, so it reclaims blobs on both the
filesystem and S3 backends (a filesystem-only construction would silently no-op on S3).

Apply once per environment:

```bash
kubectl apply -f deploy/k8s/cronjob-gc-uploads.yaml
```

Use `meme-mcp gc-uploads --dry-run` locally to preview the row/blob counts before changing the
schedule.

## PAT administration

`meme-mcp pat` runs against the same SQLite (or Postgres) database the Deployment uses. Issue and
audit tokens by execing into the pod:

```bash
kubectl exec deploy/meme-mcp -- /app/.venv/bin/meme-mcp pat issue alice \
  --ttl-days 90 --scope readwrite
kubectl exec deploy/meme-mcp -- /app/.venv/bin/meme-mcp pat list
```

`--ttl-days 0` issues a non-expiring token; `--scope read` issues a read-only token (the `generate`
tool and `/api/mcp/generate`, `/api/uploads/analyze`, `/api/uploads/{id}/approve` HTTP routes
return `UNAUTHORIZED` to read-scope callers). The `/browse` view shows a banner to authenticated
friends whose PAT will expire in fewer than 7 days.

## Allowlist administration

Entries are provider-namespaced: a bare login or `github:<login>` invites a GitHub user;
`google:<email>` invites a Google user (who pins to their immutable Google `sub` on first
verified sign-in). Matching is provider-scoped, so a Google email can never satisfy a GitHub
entry and vice versa.

```bash
kubectl exec deploy/meme-mcp -- /app/.venv/bin/meme-mcp allowlist add github:octocat
kubectl exec deploy/meme-mcp -- /app/.venv/bin/meme-mcp allowlist add google:friend@gmail.com
kubectl exec deploy/meme-mcp -- /app/.venv/bin/meme-mcp allowlist list
# `remove google:<email>` is a full de-invite: it also deletes the sub pin.
kubectl exec deploy/meme-mcp -- /app/.venv/bin/meme-mcp allowlist remove google:friend@gmail.com
```

The allowlist file lives at `GITHUB_ALLOWLIST_PATH` (defaults under `STORAGE_DIR`) and is
re-validated on every web request, so revocations take effect immediately without a rollout.

### Declarative allowlist (ConfigMap)

Instead of the CLI writing a file on the PVC, you can manage the allowlist in-repo by pointing
`GITHUB_ALLOWLIST_PATH` at a ConfigMap mounted read-only:

```yaml
# allowlist ConfigMap (one entry per line; "#" comments allowed)
apiVersion: v1
kind: ConfigMap
metadata:
  name: meme-mcp-allowlist
data:
  allowlist.txt: |
    friend-one              # bare login == github:friend-one
    github:friend-two
    google:friend@gmail.com # Google invite (pins to sub on first sign-in)
---
# in the Deployment: set GITHUB_ALLOWLIST_PATH: "/etc/meme-mcp/allowlist.txt" and add
volumeMounts:
  - name: allowlist
    mountPath: /etc/meme-mcp   # whole dir, NOT subPath, so updates propagate
    readOnly: true
volumes:
  - name: allowlist
    configMap:
      name: meme-mcp-allowlist
```

This keeps the allowlist reproducible and review-able, and survives a fresh PVC. Because the
kubelet hot-updates mounted ConfigMaps and the app re-reads the file per request, editing the
ConfigMap and running `kubectl apply` changes access with no pod restart. The trade-off: the
mount is read-only, so `meme-mcp allowlist add/remove` cannot be used -- edit the ConfigMap
instead. Removing a `google:<email>` line de-invites the friend (access is blocked on the next
request), but the `sub` pin remains in the database; run `meme-mcp pin revoke google:<sub>` to
clear it if you want a re-invite to re-pin a fresh account. `OPERATOR_GITHUB_LOGIN` is
display-only (it names the operator on the restricted page) and does not by itself grant access,
so the operator's login must also be in the allowlist.
