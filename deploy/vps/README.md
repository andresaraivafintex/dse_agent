# Piloto na VPS (2 vCPU / 8 GB) — runbook da Fase 5 (plano 09)

Kit pronto-para-executar. Pré-requisitos fora da VPS: repo publicado no GitHub
(o release constrói as imagens — **nunca builde na VPS**), Postgres gerenciado
provisionado (Neon/Supabase/RDS: o motivo é backup PITR + restore, não RAM) e
um domínio para os webhooks.

## Ordem de execução

1. **Bootstrap** (como root na VPS): `bash deploy/vps/bootstrap-k3s.sh`
   — swap 4G, k3s pinado, gVisor/runsc + RuntimeClass, helm, sops+age.
2. **Imagens**: rode o workflow `release` (tag `v*`) → baixe o artifact
   `values-digests` e cole os digests no seu values (o perfil estrito recusa
   imagem sem digest).
3. **Secrets (SOPS + age)**: gere `age-keygen`; crie `values-vps-secrets.yaml`
   a partir de `values-vps.example.yaml` (DSN do Postgres gerenciado, keys de
   provider, tokens Jira/Slack, GitHub App) e criptografe:
   `sops -e --age <pubkey> -i values-vps-secrets.yaml`. A chave privada fica
   SÓ na VPS (`/root/.config/sops/age/keys.txt`).
4. **Migrações**: automáticas — o chart roda `scripts/migrate.py` como Job de
   hook a cada install/upgrade.
5. **Install**:
   ```bash
   sops -d values-vps-secrets.yaml | helm upgrade --install dse infra/helm/dse \
     -f infra/helm/dse/values-pilot.yaml \
     -f deploy/vps/values-vps.example.yaml \
     -f /dev/stdin
   ```
6. **Webhooks/TLS**: aponte o DNS para a VPS; traefik do k3s + cert-manager
   (ou o resolvedor ACME do traefik) emitem o certificado; configure os
   webhooks Jira/Slack/GitHub para `https://<dominio>/...` — isto aposenta o
   túnel `.tunnel/` do laptop.
7. **Prova gVisor + promoção dos gates**: com o stack de pé, rode a suíte de
   prova viva K8s apontando o kube-context da VPS e `runtime_class=gvisor`
   (é a `test_k8s_flow_live.py` com `DSE_SANDBOX_RUNTIME_CLASS=gvisor`) e o
   red-team. SÓ ENTÃO o release promove `pilotReadiness.*` — o chart recusa
   piloto sem isso, por desenho.
8. **Restore drill**: teste o restore do Postgres gerenciado UMA vez antes do
   go-live. Sem restore testado, não há go-live.

## Regras duras da VPS 2 vCPU (plano 09)

- Concorrência de sandbox = **1** (obrigatório).
- Builds de imagem: **nunca** na VPS.
- `requests` de CPU garantidos para orchestrator/temporal; limits no pod de
  sandbox (~1.0–1.5 vCPU) — anti-starvation de heartbeat.
- Suíte completa/red-team rodam no CI/laptop; na VPS só smoke + canário.
- Console (`dse_console_pane`) FICA FORA deste go-live (auth default-open) —
  ou atrás de Tailscale/VPN.

## O que ainda é limite conhecido do piloto

- Temporal `auto-setup` single-node (produção madura: Temporal Cloud/cluster).
- `provision_sandbox` clona o repo alvo host-side (runtime docker); o runtime
  K8s da VPS usa o bootstrap in-pod do runner — o clone do repo REAL de
  cliente via egress dentro do pod é o item final da Fase 1, fechado aqui.
