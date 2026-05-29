# Postgres + pgvector + S3 deployment example

A copy-paste-able bundle for the v1.5 production topology: a CloudNativePG
(CNPG) Postgres cluster, an S3-compatible object store for image blobs, and
the `meme-mcp` Deployment from `deploy/k8s/`.

Use this when you outgrow the default SQLite + PVC topology — horizontal
scale, managed DB, or off-cluster image durability.

## Prerequisites

- CloudNativePG operator installed in the cluster
  (`kubectl get crd clusters.postgresql.cnpg.io`).
- An S3-compatible bucket reachable from the cluster, plus an access key
  scoped to that bucket (read/write/delete on objects, head on bucket).
- A container image for `meme-mcp` built with `uv sync --extra postgres
  --extra s3` so `psycopg`, `pgvector`, and `boto3` are present.
- A Postgres image that ships the `vector` extension (see "pgvector image"
  below).

## pgvector image

CNPG's default postgres images do not bundle pgvector, but the Alembic
migration that creates the vector column requires it. Two options:

**Option A — build a derived image.** Minimal Dockerfile:

```dockerfile
FROM ghcr.io/cloudnative-pg/postgresql:17.5-standard-trixie
USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends postgresql-17-pgvector \
 && rm -rf /var/lib/apt/lists/*
USER 26
```

Push to your registry and set `spec.imageName` in `cnpg-cluster.yaml` to the
resulting tag.

**Option B — vendor image.** Some vendors publish CNPG-compatible images
with pgvector pre-installed (e.g. `paradedb/paradedb`, Tembo). Verify the
image is signed for CNPG's user/uid contract before adopting one.

The `postInitApplicationSQL` block in `cnpg-cluster.yaml` runs
`CREATE EXTENSION IF NOT EXISTS vector` as the postgres superuser on the
`meme` database during cluster bootstrap. The app user is intentionally not
a superuser, so the extension must be installed at this point — Alembic
revision `0002_vector_ddl` then becomes a no-op.

## Apply order

```bash
# 1. Provision the database. Wait for CNPG to reach Cluster in Healthy state.
kubectl apply -f cnpg-cluster.yaml
kubectl wait --for=condition=Ready cluster/meme-mcp-postgres --timeout=10m

# 2. Build DATABASE_URL from the CNPG-managed app secret.
PGPASS=$(kubectl get secret meme-mcp-postgres-app \
  -o jsonpath='{.data.password}' | base64 -d)

# 3. Materialize secret.yaml with the password substituted in. Edit the
#    other REPLACE_* fields by hand or via your secret manager before
#    applying.
sed "s|REPLACE_WITH_CNPG_PASSWORD|${PGPASS}|" secret.example.yaml > secret.yaml

# 4. Apply the app's configmap + secret. configmap.yaml here overrides the
#    base configmap.yaml in ../../ — apply this one last.
kubectl apply -f secret.yaml
kubectl apply -f configmap.yaml

# 5. Apply the rest of the base manifests unchanged.
kubectl apply -f ../../pvc.yaml \
              -f ../../deployment.yaml \
              -f ../../service.yaml \
              -f ../../ingress.yaml
```

The PVC is still required even on the S3+Postgres path because
`STORAGE_DIR` holds the GitHub allowlist file and operator-managed parity
manifests. Image blobs live in S3.

## Verifying the cutover

```bash
# pgvector available on the app DB
kubectl exec -it meme-mcp-postgres-1 -- \
  psql -U meme -d meme -c "SELECT extname, extversion FROM pg_extension WHERE extname = 'vector';"

# App reached Alembic head and is serving readiness
kubectl rollout status deploy/meme-mcp
kubectl exec deploy/meme-mcp -- /app/.venv/bin/meme-mcp pat list

# S3 reachable from the pod (lists objects under the configured bucket)
kubectl exec deploy/meme-mcp -- /app/.venv/bin/meme-mcp gc-renders --dry-run --ttl-days 365
```

If you are migrating an existing SQLite+PVC deployment, use the
`meme-mcp migrate` orchestrator described in `../../README.md` rather than
just switching env vars — it copies templates, vectors (with re-embed), and
image blobs and writes a `.env.next` diff. Run with `--dry-run` first.

## Rotating the DB password

CNPG can rotate the app password on demand via the Cluster spec or `cnpg`
plugin. After rotation, re-run steps 2–4 above and
`kubectl rollout restart deploy/meme-mcp` so the pod picks up the new DSN.
