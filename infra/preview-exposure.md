# Exposição dos previews (D3) — local → túnel Cloudflare → VPS

O mecanismo é UM só: o Ingress do preview é gerado com o hostname derivado de
`DSE_PREVIEW_EXTERNAL_HOST` (template com `{namespace}`), e o Traefik do k3d
publica a porta 80 em **localhost:8081**. Mudar de "só eu" para "túnel" para
"VPS" é mudar **para onde o DNS aponta** + o template — zero mudança de código.

## Modo 1 — local, só para você (ATIVO por default)

```
DSE_PREVIEW_EXTERNAL_HOST=http://{namespace}.preview.localhost:8081
```

- Browsers modernos resolvem `*.localhost` → 127.0.0.1 automaticamente.
- Abra o link que o DSE posta no PR (ex.:
  `http://preview-wi-abc123.preview.localhost:8081`) na SUA máquina.
- Pré-requisitos (uma vez): `infra/k8s-local/setup-k3d-argocd.sh` (cluster +
  Traefik + registry) e `infra/k8s-local/gen-kubeconfig-internal.sh`
  (kubeconfig do worker). Re-rode ambos se recriar o cluster.

## Modo 2 — túnel Cloudflare (acesso externo sem abrir porta)

Requer um domínio na sua conta Cloudflare (o plano free basta). Uma vez:

```bash
cloudflared tunnel login
cloudflared tunnel create dse-preview
# DNS wildcard do túnel (uma vez):
cloudflared tunnel route dns dse-preview '*.preview.SEUDOMINIO.com'
```

`~/.cloudflared/config.yml`:

```yaml
tunnel: dse-preview
credentials-file: /Users/<voce>/.cloudflared/<TUNNEL_ID>.json
ingress:
  # todo *.preview.SEUDOMINIO.com cai no Traefik local; o Ingress do k8s
  # roteia pelo Host header (o mesmo hostname do template) — nada mais muda.
  - hostname: "*.preview.SEUDOMINIO.com"
    service: http://localhost:8081
  - service: http_status:404
```

Rode `cloudflared tunnel run dse-preview` e troque só o template:

```
DSE_PREVIEW_EXTERNAL_HOST=https://{namespace}.preview.SEUDOMINIO.com
```

(`https` — o Cloudflare termina o TLS na borda.) Recrie o worker
(`docker compose ... up -d orchestrator`) para o env valer.

> Nota: um *quick tunnel* (`cloudflared tunnel --url`, sem conta) NÃO serve —
> ele dá um hostname aleatório único, e o roteamento dos previews é por
> subdomínio wildcard.

## Modo 3 — VPS com subdomínio (o destino combinado)

Mesmíssimo template do modo 2 (`https://{namespace}.preview.SEUDOMINIO.com`).
O que muda é onde o cluster/Traefik vive:

1. Na VPS: k3s (ou o k3d + compose, idêntico ao local), Traefik exposto em
   80/443, cert wildcard (cert-manager + Let's Encrypt DNS-01).
2. DNS: `*.preview.SEUDOMINIO.com` → A/AAAA da VPS (ou mantenha o cloudflared
   na VPS como ingress — dispensa abrir porta e o TLS segue na borda).
3. `DSE_PREVIEW_EXTERNAL_HOST` fica igual ao modo 2 — nenhum código muda.

## Peças relacionadas

- Imagem do PR (D4): `DSE_PREVIEW_BUILD_IMAGE=true` builda a imagem do head do
  PR quando o workspace tem `Dockerfile` (senão placeholder nginx, motivo
  auditado). Porta do app: `DSE_PREVIEW_APP_PORT` (default 80). Registry local:
  push `localhost:5510`, pull `k3d-dse-registry:5510`.
- TTL: os previews expiram (`DSE_PREVIEW_TTL_SECONDS`, default 1h) e o reaper
  os remove via GitOps.
