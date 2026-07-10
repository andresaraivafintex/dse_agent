# services/sandbox-runtime (WS-C)

Sandbox efêmero por tarefa (Docker rootless), lifecycle como Temporal
Activities, interface de substrato de agente + adapter OpenHands, e sessão
Coder com git de escopo limitado. Ver `CONVENTIONS.md` na raiz do monorepo
para o contrato entre workstreams.

## O que está implementado e funcionando (testado contra Docker/Postgres reais)

### WSC-E1 — Sandbox efêmero por tarefa

- **T1 — Isolamento** (`docker_driver.py`): todo container de sandbox roda
  `--user <uid não-root>`, `--read-only` + `--tmpfs /tmp`, `--cap-drop ALL`,
  `--security-opt no-new-privileges`, sem bind de `/var/run/docker.sock`, e
  conectado exclusivamente à rede Docker interna `dse_sandbox_net`
  (`internal=True` — sem gateway de internet). O único host alcançável de
  dentro do sandbox é o egress-proxy (`services/egress-proxy`), que por sua
  vez está conectado tanto a `dse_sandbox_net` quanto a `dse_net` (que tem
  rota de internet padrão do Docker).
  Provado por `tests/test_network_isolation.py`, que sobe containers Docker
  de verdade (sandbox + egress-proxy + um "upstream" simulando a internet) e
  prova: (a) nenhum mount referencia `docker.sock`; (b) `id -u` dentro do
  sandbox não é `0`; (c) uma requisição direta a um host externo falha; (d)
  uma requisição via egress-proxy a um host permitido funciona; (e) uma
  requisição via egress-proxy a um host fora da allowlist volta `403`.
- **T2 — Caps de recurso + métricas OTel** (`docker_driver.ResourceCaps`,
  `metrics.py`): `--cpus`/`--memory`/`--pids-limit` derivados do `budget` do
  `WorkItem` (chaves `cpu_limit`/`memory_mb`/`pids_limit`/`resource_class`,
  com defaults por `resource_class` em `small`/`medium`/`large`).
  `teardown_sandbox` emite um histograma OTel
  `dse.sandbox.runtime_minutes` com os atributos
  `dse_contracts.constants.OTEL_ATTR_TENANT/WORK_ITEM/STAGE` +
  `dse.resource_class`. Provado por
  `tests/test_resource_caps_and_metrics.py` (inspeciona `HostConfig` real do
  container e lê os data points via `InMemoryMetricReader` do OTel SDK).
- **T3 — Lifecycle como Temporal Activities** (`activities.py`):
  `provision_sandbox`/`checkpoint_sandbox`/`rebuild_sandbox`/
  `teardown_sandbox` decoradas `@activity.defn(name=ACTIVITY_*)` com os
  nomes exatos de `dse_contracts.activities`, retornando
  `SandboxHandle`/`CheckpointRef`. Idempotência: `provision_sandbox`
  procura por label `dse.work_item_id` antes de criar — chamado duas vezes
  para o mesmo `work_item_id`, reaproveita o mesmo container (provado por
  `tests/test_idempotent_provision.py`). `tests/test_temporal_activity_wiring.py`
  prova que são Activities de verdade do Temporal SDK (nomes batem com o
  contrato, executam via `temporalio.testing.ActivityEnvironment` — o
  harness oficial do SDK para testar uma Activity isolada, não um mock).
- **T4 — Checkpoint/rebuild + chaos** (`git_checkpoint.py`,
  `scoped_git.py`): checkpoint = commit + push do branch da tarefa para um
  bare repo git local (`git init --bare`, servindo de "origin" de teste —
  não é um remoto real, conforme permitido pelo enunciado). Rebuild clona o
  bare repo e faz `git checkout` do sha do checkpoint num workspace novo.
  `tests/test_checkpoint_chaos.py` mata o container no meio (`docker kill`)
  e prova que o rebuild recupera os commits sem perda.

### WSC-E3 — Substrato de agente + sessão Coder

- **T1 — Interface + adapters** (`substrate.py`): `AgentSubstrate` é um
  `Protocol` com `create_session`/`run_turn`/`collect_artifacts`.
  `FakeSubstrate` é um adapter in-memory determinístico (roteirizado por
  turno) usado por todos os testes — não faz nenhuma chamada de rede/modelo.
  `OpenHandsSubstrate` é o adapter real sobre o pacote PyPI `openhands-sdk`
  (`pip install openhands-sdk` **funcionou nesta sessão**, v1.21.0 — ver
  "Limitações conhecidas" abaixo para o que falta pra exercitar um turno de
  verdade). O LLM do OpenHands é sempre construído com
  `base_url=<model-gateway>` + `api_key=<virtual key>` +
  `extra_headers=GatewayCallHeaders(...).to_http_headers()` — nunca aponta
  para um SDK/endpoint de provider diretamente.
- **T2 — `run_coder_turn` com git de escopo limitado**
  (`activities.py::run_coder_turn`, `scoped_git.py`): o substrato SÓ edita
  arquivos no workspace — o commit/push para o branch da tarefa é feito
  depois, por código determinístico (`ScopedGitSession`), nunca pelo LLM
  (P1). Duas camadas de enforcement contra force-push/PR/branch errado:
  1. **Toolset**: `ScopedGitSession` expõe só `.commit()`/`.push()` (refspec
     hardcoded), sem `run_git_command`/`create_pull_request`/force-push.
  2. **Escopo do remoto**: um hook `pre-receive` REAL (`install_pre_receive_guard`)
     instalado no bare repo de checkpoint recusa qualquer ref fora do branch
     da tarefa e qualquer non-fast-forward — mesmo que alguém contorne
     `ScopedGitSession` e rode `git push --force` cru.
  3. **Escopo da credencial**: `egress_proxy.credentials.ScopedCredential`
     nunca tem `pull_requests:write`/força — `create_pull_request()`/
     `force_push()` sempre levantam `GitHubScopeError`.
  Provado adversarialmente por `tests/test_run_coder_turn_scoped_git.py`
  (força push cru → rejeitado pelo hook; push pra outro branch → rejeitado;
  `ScopedGitSession.push()` propaga a recusa como `GitScopeViolation`).

## O que está com fixture/mock local (documentado, não escondido)

- **`FakeSubstrate`** é o substrato usado em TODOS os testes desta suíte —
  não faz chamada de modelo nenhuma, edita arquivos a partir de um script
  Python fornecido pelo teste. Isso é deliberado: testar a plumbing real
  (Docker + git + Temporal) sem depender do model-gateway do WS-D estar de
  pé nem gastar inferência real.
- **`model_gateway_client.mint_virtual_key`**: tenta `POST
  {DSE_MODEL_GATEWAY_URL}/internal/virtual-keys` (default
  `http://localhost:4000`, a porta reservada do WS-D). Se o endpoint não
  responder (WS-D ainda não estava de pé quando este workstream rodou seus
  testes), cai para uma virtual key de fixture (`fixture-vk-<work_item_id>-
  <random>`) — isso é **claramente sinalizado** no campo `fixture: bool` do
  retorno e usado nos testes. Desabilite com
  `DSE_MODEL_GATEWAY_ALLOW_FIXTURE=0` para forçar falha limpa (P6) em vez de
  fixture silenciosa.
- **Container Temporal da infra compartilhada está fora do ar nesta sessão**:
  `docker ps -a` mostra `dse_temporal` como `Exited (1)` — a imagem
  `temporalio/auto-setup:1.24` reclama de
  `config/dynamicconfig/development-sql.yaml` ausente. Isso é
  `docker-compose.yml` (fundação, fora do escopo de edição do WS-C) — não
  tentei consertar. Por isso os testes de Activity usam
  `temporalio.testing.ActivityEnvironment` (o harness oficial do SDK, real,
  não mock) em vez de um Worker conectado a um servidor Temporal de
  verdade — o SDK e a lógica das Activities são 100% reais, só não há
  workflow/worker rodando ponta a ponta nesta sessão.

## O que falta para produção

- **`OpenHandsSubstrate` com execução de ferramentas realmente dentro do
  sandbox**: hoje usa `openhands.sdk.LocalWorkspace` (executa no processo
  que roda o SDK, isto é, no worker do WS-B). Produção deveria trocar por
  `openhands.sdk.RemoteWorkspace`, apontando para um `openhands-agent-server`
  (também disponível como dependência transitiva do `openhands-sdk`, pacote
  `openhands-agent-server`) rodando DENTRO do container provisionado por
  `docker_driver.py` — só assim a execução de bash/edição de arquivo do
  agente fica de fato dentro do sandbox isolado. Não implementado por não
  ser exercitável sem o model-gateway do WS-D de pé com um provider real
  configurado no LiteLLM.
- **Imagem de sandbox com git pré-instalado**
  (`docker/Dockerfile.sandbox-base`): escrita e documentada, mas não
  publicada em nenhum registry — os testes usam `python:3.11-slim` puro
  (sem git) para os cenários que não precisam de git dentro do container
  (isolamento de rede), e os cenários que precisam de git
  (checkpoint/scoped-git) rodam os comandos git contra o path do host que é
  o mesmo bind mount do workspace do container (documentado em
  `git_checkpoint.py` — o conteúdo é idêntico dos dois lados por ser bind
  mount, então isso é real, só não exercita o binário `git` de dentro do
  processo do container). Produção deveria usar
  `docker build -f docker/Dockerfile.sandbox-base` e publicar a imagem, e aí
  sim rodar os comandos git via `docker exec` de dentro do container.
- **Integração real com `services/model-gateway` (WS-D)**: `mint_virtual_key`
  já tenta o endpoint real primeiro; a integração ponta-a-ponta acontece na
  fase de integração entre workstreams, quando o WS-D estiver publicando
  `/internal/virtual-keys` de verdade.
- **`sandbox_leases`/`egress_credential_leases` (migração `0004_wsc.sql`)**
  são bookkeeping operacional adicional (além do `audit_log` via
  `dse_audit.emit`, que é o registro obrigatório de P8) — best-effort, cai
  silenciosamente para "sem persistência extra" se o Postgres não estiver
  alcançável (nunca quebra o path principal).

## Como rodar os testes

```bash
python3.12 -m venv .venv-wsc
source .venv-wsc/bin/activate
pip install -e packages/contracts -e packages/dse_audit -e packages/dse_identity
pip install -e services/sandbox-runtime -e services/egress-proxy
pip install pytest

DSE_DATABASE_URL=postgresql://dse:dse_dev_only@localhost:5432/dse python3 scripts/migrate.py

DSE_DATABASE_URL=postgresql://dse:dse_dev_only@localhost:5432/dse \
  pytest -q services/sandbox-runtime/tests
```

Requer Docker rodando (containers reais são criados/destruídos) e Postgres
da fundação em `localhost:5432` (não precisa do Temporal server — ver nota
acima sobre `ActivityEnvironment`).

**Resultado real nesta sessão**: `15 passed` neste pacote isoladamente / `28
passed` somando `services/egress-proxy` (13 testes, ver README de lá), `0
failed`, `0 skipped` (o teste condicional de `OpenHandsSubstrate` roda de
verdade porque `openhands-sdk` instalou com sucesso neste ambiente).
