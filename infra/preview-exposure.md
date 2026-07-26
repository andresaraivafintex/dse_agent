# Preview exposure (D3) — local → Cloudflare tunnel → VPS

There is only ONE mechanism: the preview's Ingress is generated with the hostname derived from
`DSE_PREVIEW_EXTERNAL_HOST` (a template with `{namespace}`), and k3d's Traefik
publishes port 80 on **localhost:8081**. Going from "just me" to "tunnel" to
"VPS" means changing **where DNS points** + the template — zero code changes.

## Mode 1 — local, just for you (ACTIVE by default)

```
DSE_PREVIEW_EXTERNAL_HOST=http://{namespace}.preview.localhost:8081
```

- Modern browsers resolve `*.localhost` → 127.0.0.1 automatically.
- Open the link the DSE posts on the PR (e.g.
  `http://preview-wi-abc123.preview.localhost:8081`) on YOUR machine.
- Prerequisites (one time): `infra/k8s-local/setup-k3d-argocd.sh` (cluster +
  Traefik + registry) and `infra/k8s-local/gen-kubeconfig-internal.sh`
  (the worker's kubeconfig). Re-run both if you recreate the cluster.

## Mode 2 — Cloudflare tunnel (external access without opening a port)

Requires a domain on your Cloudflare account (the free plan is enough). One time:

```bash
cloudflared tunnel login
cloudflared tunnel create dse-preview
# Wildcard DNS for the tunnel (one time):
cloudflared tunnel route dns dse-preview '*.preview.YOURDOMAIN.com'
```

`~/.cloudflared/config.yml`:

```yaml
tunnel: dse-preview
credentials-file: /Users/<you>/.cloudflared/<TUNNEL_ID>.json
ingress:
  # every *.preview.YOURDOMAIN.com lands on the local Traefik; the k8s Ingress
  # routes by Host header (the same hostname as the template) — nothing else changes.
  - hostname: "*.preview.YOURDOMAIN.com"
    service: http://localhost:8081
  - service: http_status:404
```

Run `cloudflared tunnel run dse-preview` and change only the template:

```
DSE_PREVIEW_EXTERNAL_HOST=https://{namespace}.preview.YOURDOMAIN.com
```

(`https` — Cloudflare terminates TLS at the edge.) Recreate the worker
(`docker compose ... up -d orchestrator`) for the env var to take effect.

> Note: a *quick tunnel* (`cloudflared tunnel --url`, no account) does NOT work —
> it hands out a single random hostname, and preview routing is by
> wildcard subdomain.

## Mode 3 — VPS with a subdomain (the agreed destination)

Exactly the same template as mode 2 (`https://{namespace}.preview.YOURDOMAIN.com`).
What changes is where the cluster/Traefik lives:

1. On the VPS: k3s (or k3d + compose, identical to local), Traefik exposed on
   80/443, wildcard cert (cert-manager + Let's Encrypt DNS-01).
2. DNS: `*.preview.YOURDOMAIN.com` → the VPS's A/AAAA (or keep cloudflared
   on the VPS as the ingress — no port to open and TLS still terminates at the edge).
3. `DSE_PREVIEW_EXTERNAL_HOST` stays the same as mode 2 — no code changes.

## Related pieces

- PR image (D4): `DSE_PREVIEW_BUILD_IMAGE=true` builds the image from the PR's
  head when the workspace has a `Dockerfile` (otherwise an nginx placeholder, with the reason
  audited). App port: `DSE_PREVIEW_APP_PORT` (default 80). Local registry:
  push `localhost:5510`, pull `k3d-dse-registry:5510`.
- TTL: previews expire (`DSE_PREVIEW_TTL_SECONDS`, default 1h) and the reaper
  removes them via GitOps.
