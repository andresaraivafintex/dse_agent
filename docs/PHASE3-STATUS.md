# Fase 3 ("Evidence") — Status da implementação

Data: 2026-07-20. Escopo: implementação real da Fase 3 sobre Fases 1+2 integradas, com os
ajustes do [adendo 02](../../plano-desenvolvimento/02-ADENDO-FASE3-POS-FASE2.md).

## Resumo executivo

- **~470 testes passando, 2 pulados, 0 falhando** (Fase 2 fechou em 399; +71 novos na Fase 3):
  contracts 14 · WS-A 107 · WS-B 47 · WS-C 78 (65 sandbox + 13 egress) · WS-D 51 · WS-E 103 ·
  WS-F 121 (114 platform + 7 audit). Contra Postgres/Temporal/Docker/Vault/LiteLLM **e um
  cluster Kubernetes real (k3d) com Argo CD**.
- **Gate de entrada cumprido antes do build** (a resposta aos 14 bugs de boundary das Fases
  1-2): models de sessão promovidos a `dse_contracts` com `test_activity_boundaries.py`
  validando os payloads literais dos call sites; `RunL2ReviewInput` com `extra="forbid"` (P3 no
  decode); contratos de evidência definidos antes da implementação; cluster k3d + Argo CD.
- **O gate de boundary pagou na hora**: a Fase 3 adicionou 4 Activities de evidência e
  **nenhum bug de boundary** apareceu na integração (contra 9 na Fase 1 e 5 na Fase 2) — os 2
  bugs desta integração foram de outra natureza (ordering e DSN), não de contrato.
- Worker registra **30 Activities** (8 WS-C + 14 WS-E + 8 WS-B locais), sem colisão de nome.

## O que foi construído (real, por workstream)

| WS | Fase 3 entregue | Prova real |
|---|---|---|
| E | Artifact store **Garage v1.1.0** (bucket/tenant, presigned TTL, quarentena integrada à costura do WS-F, log de acesso, multipart), vídeo **@demo Playwright** (webm real), **previews por PR via Argo CD ApplicationSet** no cluster k3d, visual diff (Pillow), L3 completo (reflection + targeted re-runs + episódios de CI-repair) | mp4 10MB round-trip byte-idêntico; URL expirada → negada; namespace de preview criado → **HTTP 200** → TTL reap; quarentena → 404 antes do TTL |
| C | **Playwright na imagem base** do sandbox (`dse-sandbox-base:wsc3`, 2.35GB, chromium) + convenção `demos/<work_item_id>/`; **segundo substrato Claude Agent SDK** (v0.2.124 real) atrás da mesma interface + suíte de conformidade parametrizada | `npx playwright test --grep @demo` via `docker exec` no sandbox rootless → vídeo webm real |
| D | **Failover intra-tier nativo** do LiteLLM (2ª instância eco, mesmo tier) + bateria de chaos estendida (outage total, quota 429, egress não-allowlisted) | failover provado derrubando o container primário com `docker stop`; teste negativo que falha o CI se um fallback cruzar tier (NFR-07) |
| B | **Debounce de evidência (ADR-26)** + iteration caps no review loop + wiring do pipeline de evidência (trigger_preview → demo → visual_diff, degraded não bloqueia) + métrica OTel de tamanho de history | 6 comentários numa janela → ≤1 refresh (time-skipping); fakes que **decodificam com os models reais** do contrato |
| F | **ADR-28 completo**: rotação agendada de secrets (zero downtime provado) + **ESO 2.8.0 real** no k3d (Secret materializa do Vault, teste negativo de escopo); retenção por classificação; **alerta de history ativado** | leitor concorrente durante 5 rotações → zero erro; ExternalSecret fora de escopo nunca fica Ready |

## Bugs de integração (2 — nenhum de contrato, graças ao gate de entrada)

1. **Ordering do gate de plano (WS-B).** A edição do review loop na Fase 3 mudou o timing e
   expôs uma inversão: o workflow setava `status=awaiting_plan_approval` **antes** de gravar a
   projeção durável `plan_approval_gate`. Um observador (queue board, ou o roteamento de signal
   do WS-A que dispara `SIGNAL_PLAN_APPROVAL` com base no status) podia ver o estado com o gate
   ainda ausente. **Corrigido**: grava o gate antes de flipar o status — a projeção existe
   quando o estado se torna observável.
2. **DSN do job de retenção (WS-F) — na verdade uma nota operacional, não bug de código.** O
   teste `test_artifact_purge_skipped_without_delete_grant` falhou no meu harness de integração
   porque exportei `DSE_DATABASE_URL` como o **superuser `dse`** (para aplicar migrações); a
   retenção conecta por esse DSN e `current_user=dse` **tem** DELETE, então purgou em vez de
   pular. Com o DSN correto (`dse_app`), 16/16 passam. **Nota operacional load-bearing
   registrada**: o job de retenção (`dse_platform.retention`) DEVE rodar como `dse_app`, nunca
   como owner do banco — senão a proteção estrutural de DELETE (o mesmo princípio que blinda o
   `audit_log`) é contornada. O serviço `platform-jobs` no compose já usa o DSN de app; a nota
   deve entrar no runbook de deploy do WS-F.

## Exit criteria da Fase 3 (Seção 16) — honestamente

| Critério | Status |
|---|---|
| UC1 com evidência de vídeo completa (mp4/webm, presigned URL com TTL) | **Atendido** — vídeo real gravado por Playwright, publicado no Garage, URL com TTL; provado por teste |
| PRs backend-only pulam preview sem bloquear | **Atendido** — paths-filter determinístico (FR-20); `skipped_backend_only` conta como sucesso |
| Links de evidência expiram por política | **Atendido** — Garage responde negado a presigned expirada; teste real |
| Suíte multipart/IAM do Garage validada contra workload real | **Atendido** — mp4 de 10MB, multipart explícito, round-trip byte-idêntico |
| Caps de preview por tenant + debounce comprovados por contagem | **Atendido** — teste de contagem (WS-E) + debounce time-skipping (WS-B) |

## O que falta (não escondido)

- **GitHub App real**: previews, L3 targeted re-runs e o vídeo `@demo` contra um preview de um
  PR **real** seguem com `FakeGitHubClient` (a lógica é real contra a API; falta a App
  registrada). É o mesmo bloqueio administrativo das Fases 1-2 — e a Fase 3 é onde ele mais
  pesa (preview por PR contra repo fake tem valor limitado). **Disparar já.**
- **@demo roda no host, não no sandbox do WS-C no fluxo integrado**: o WS-C provou execução
  DENTRO do sandbox; o pipeline do WS-B ainda executa a Activity de evidência no worker. Unir
  os dois (rodar `@demo` dentro do sandbox provisionado, contra o preview URL) é integração
  fina pendente.
- **URL de preview é in-cluster** (probe via port-forward/NodePort); expor ao reviewer externo
  precisa de ingress real no cluster do cliente.
- **Reaper de TTL** é job Python GitOps (decisão documentada — kube-janitor brigaria com o
  selfHeal do Argo CD); annotation `janitor/ttl` já gravada como upgrade path.
- Substrato com **inferência real** (Claude Agent SDK / OpenHands): construção, wiring e seleção
  provados; um turno com modelo real exige gateway + provider pagos (mesma limitação desde a
  Fase 1).

## Como rodar

```
cd fase1
make up && make migrate
./infra/k8s-local/setup-k3d-argocd.sh   # cluster + Argo CD (idempotente)
./infra/k8s-local/setup-eso.sh          # External Secrets Operator (WS-F)
# testes: venv ativado; platform/validation com DSN dse_app (NÃO superuser — ver bug 2):
#   DSE_DATABASE_URL=postgresql://dse_app:dse_app_dev_only@localhost:5432/dse \
#     bash -c 'source .venv-wsf/bin/activate && cd services/platform && pytest -q'
```
