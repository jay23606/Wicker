# Sidecars

Companion containers the app runs alongside the wiki go here — one
subdirectory per sidecar, each a Docker build context (a `Dockerfile`
plus its sources). A sidecar is reachable from the wiki by its service
name on the instance network (e.g. `http://my-service:8000`).

Sidecars are deployed through Canasta's first-class `canasta sidecar`
primitive: you declare each one once, orchestrator-neutrally, and Canasta
renders and deploys it identically on Compose and Kubernetes.

## How it maps to `wicker.yaml`

Each sidecar is one entry under `sidecars:` in
[`../wicker.yaml`](../wicker.yaml):

```yaml
sidecars:
  - name: my-service
    build: ./sidecars/my-service     # build context (or use `image:` to pull)
    ports: [8000]                    # internal only — reachable by service name
    env:
      MY_APP_SHARED_SECRET: "${MY_APP_SHARED_SECRET:-}"
    healthcheck:
      path: /health
      port: 8000
    # volumes:
    #   - name: data
    #     mountPath: /data
    #     persistent: true           # named volume (Compose) / PVC (k8s)
    #     size: 1Gi
```

Use `build:` for an image built from this directory, or `image:` to pull a
prebuilt one. A persistent volume becomes a named volume on Compose and a
ReadWriteOnce PVC on k8s.

> Note: `sysctls` and `secrets` are not in the sidecar primitive's current
> scope — Wicker warns and drops them if present.
