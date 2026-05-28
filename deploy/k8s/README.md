# Kubernetes

Example manifests for the hosted deployment path. Copy `secret.example.yaml` to `secret.yaml`
locally; `secret.yaml` is gitignored.

## Storage and database

The current production-ready storage path is SQLite plus filesystem images on the `meme-mcp-storage`
PVC. Postgres/pgvector and S3 are still v1.5 stubs in this codebase, so keep Kubernetes deployments
on the PVC-backed SQLite path unless those stubs have been implemented and tested.

Use these settings in the `meme-mcp` ConfigMap:

```yaml
data:
  STORAGE_DIR: "/data"
  DATABASE_URL: "sqlite+aiosqlite:////data/meme-mcp.db"
  IMAGE_STORE_BACKEND: "filesystem"
  IMAGE_STORE_FS_PATH: "/data/images"
```

The Deployment mounts the PVC at `/data`. The pod-level `fsGroup: 10001` lets the non-root app user
write the mounted volume.

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
          image: meme-mcp:latest
          securityContext:
            allowPrivilegeEscalation: false
            runAsNonRoot: true
            runAsUser: 10001
          command: ["/bin/sh", "-lc"]
          args:
            - |
              set -e
              git clone https://github.com/jacebrowning/memegen.git /tmp/memegen
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
