# Topologia B — tudo no VPC do cliente (WSF-E5-T3, Fase 4)

O chart `infra/helm/dse` suporta duas topologias de deployment. Este documento descreve a
**topologia B** (a mais estrita) e, sobretudo, o **custo operacional** que ela implica.

## A vs B em uma frase

- **Topologia A** (default, `values.yaml`): uma instalação = um tenant = um namespace dentro do
  VPC/K8s do cliente. Em produção, PERMITE apontar componentes de infra para serviços
  **gerenciados/compartilhados** — Postgres gerenciado (RDS/CloudSQL), Temporal Cloud, Vault HA
  externo, Bedrock via PrivateLink. É o ponto de amortização de custo.
- **Topologia B** (`values-topology-b.yaml`): o TIER MAIS ESTRITO. **TUDO** roda dentro do VPC do
  cliente, sem nenhuma dependência externa gerenciada nem compartilhada — Postgres, Temporal
  (+ console), Vault, **e o próprio modelo** (self-hosted / air-gapped, Tier 2). Nada de dado
  (nem de controle, nem de inferência) sai do VPC.

Mapeia no data-flow diagram Tier 2 do `infra/THREAT-MODEL.md §3.2`.

## Como renderizar/validar

```bash
# Topologia A (base)
helm template dse-acme infra/helm/dse

# Topologia B (base + overlay estrito)
helm template dse-acme infra/helm/dse \
    -f infra/helm/dse/values.yaml \
    -f infra/helm/dse/values-topology-b.yaml

helm lint infra/helm/dse
helm lint infra/helm/dse -f infra/helm/dse/values-topology-b.yaml
```

(Não é necessário `helm install` real — a validação é `lint` + `template` renderizando YAML
válido, conforme o aceite da tarefa.)

## O que muda estruturalmente em B

| Componente | Topologia A (produção recomendada) | Topologia B (estrita) |
|---|---|---|
| Postgres | pode ser RDS/CloudSQL gerenciado | StatefulSet in-cluster obrigatório; PITR é responsabilidade do cliente no VPC |
| Temporal | pode ser Temporal Cloud | self-hosted in-cluster (+ UI in-VPC) |
| Vault | pode ser Vault HA externo/HSM do cliente | in-cluster, `devMode: false`, unseal pelo cliente |
| Modelo | Bedrock via PrivateLink (dado no VPC via endpoint privado) | **model-server self-hosted in-cluster (GPU)** — `modelServer.enabled: true` |
| Egress allowlist | api.github.com, slack.com, *.amazonaws.com | **só hosts internos** (git/registry espelhados); nenhum host público |
| Console (queue board / Temporal UI) | in-VPC | in-VPC (idem A) |
| ESO / NetworkPolicy | opcional / on | ESO on + NetworkPolicy default-deny obrigatório |

## Custo operacional — NFR-08 × N (o ponto principal)

**NFR-08** é o custo operacional de manter uma stack DSE em pé (compute + storage + o esforço
humano de operar Postgres/Temporal/Vault/observabilidade/patching). Em topologia A, boa parte
desse custo **amortiza** porque componentes pesados podem ser gerenciados (o provedor opera o
Postgres/Temporal) e/ou compartilhados entre tenants do mesmo operador.

Em **topologia B, nada amortiza**: cada cliente recebe uma **stack standalone completa** dentro do
próprio VPC. Portanto, para **N clientes** em topologia B, o custo operacional é aproximadamente:

```
custo_total_B  ≈  N × (NFR-08 stack completa self-hosted)
```

contra a topologia A, onde:

```
custo_total_A  ≈  N × (NFR-08 componentes leves)  +  custo_fixo(serviços gerenciados/compartilhados)
```

### De onde vem a multiplicação, concretamente

Cada instalação B carrega, por cliente, **sem diluição**:

1. **Postgres self-managed** — não há um time de RDS operando por você. Backup/PITR, upgrades de
   versão maior, tuning e monitoramento de disco são **× N**.
2. **Temporal self-hosted** — cluster de orquestração + sua própria persistência e UI, operado e
   atualizado (Worker Versioning, ver `infra/RUNBOOK-UPGRADE.md`) **× N**.
3. **Vault HA in-cluster** — unseal, rotação, DR do cofre, **× N** (não um Vault central).
4. **GPU para o model-server air-gapped** — o item mais caro. Uma GPU (ou pool) dedicada por
   cliente, ociosa fora dos picos, **× N**. Não há amortização de inferência entre clientes
   (que é justamente a economia de um endpoint gerenciado como Bedrock).
5. **Observabilidade + patching + on-call** — o custo humano de operar N stacks isoladas cresce
   quase linearmente; não há "um pane de vidro" central por definição do air-gap.

### Implicação de negócio (registrada honestamente)

- Topologia B só se paga para o tier de clientes cuja exigência regulatória/contratual **proíbe**
  qualquer saída de dado do VPC (o motivo de existir). Para os demais, topologia A com PrivateLink
  (Tier 1) entrega a mesma residência de dado de inferência a uma fração do custo.
- O pricing do piloto deve refletir a multiplicação: um cliente em topologia B não pode ser
  precificado como um tenant marginal de uma stack compartilhada — ele **é** a stack.
- A GPU dedicada é o driver dominante de custo e o item de maior lead time de provisionamento no
  VPC do cliente; começar cedo (é caminho crítico junto com as credenciais reais — adendo 03
  §Parte 3).

## Estado (P8 — honesto)

- O empacotamento (overlay + template do model-server) está completo e validado por
  `helm lint` + `helm template` (ambas as topologias renderizam YAML válido).
- O `model-server` air-gapped concreto (imagem/serving) é **P2** (WSD-E5-T2/T3): o mecanismo de
  provider custom já está provado (echo provider, `services/model-gateway/tests/test_echo_provider.py`);
  a imagem de serving real não bloqueia o piloto.
- `helm install` real em um cluster com GPU device plugin não foi executado (fora do escopo do
  aceite e sem hardware de GPU nesta sessão). Em cluster sem GPU, deixar `modelServer.gpu: 0`
  para o `helm template`/`lint` validar sem exigir `nvidia.com/gpu`.
