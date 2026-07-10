# Contracts changelog — Fintex DSE

Histórico de versões dos pacotes publicados em `packages/` (contratos
estáveis inter-workstream, conforme `CONVENTIONS.md`). Mantido por WS-F
(WSF-E0) como parte da fundação de CI/CD de plataforma.

## Regra de mudança de contrato

> **Mudança de contrato exige aprovação do arquiteto-chefe.**

Concretamente:

1. **Aditivo é sempre permitido sem aprovação prévia** (adicionar um campo
   opcional novo, uma função nova, uma constante nova) — desde que não
   remova, renomeie ou mude o tipo/assinatura de nada que já existe
   (`CONVENTIONS.md`: "adicione um campo/tipo novo sem remover ou renomear o
   que já existe"). Isto cobre a extensão de `dse_audit` feita pelo WS-F
   nesta Fase 1 (`dse_audit.queries` — ver entrada abaixo).
2. **Qualquer mudança breaking** (remover/renomear campo público, mudar
   assinatura de função pública, mudar semântica de um status/enum já
   consumido por outro workstream) requer:
   - um PR isolado só com a mudança de contrato (nunca misturado com lógica
     de negócio de um serviço específico);
   - aprovação explícita do arquiteto-chefe do programa (não apenas do lead
     do workstream que precisa da mudança) — P3 (nenhuma sessão de agente
     aprova o próprio trabalho) se aplica aqui também: quem propõe a mudança
     de contrato não pode ser quem a aprova;
   - um bump de versão MAJOR (ver semver abaixo) e uma entrada nova neste
     changelog **antes** do merge, não depois;
   - notificação nos canais dos workstreams consumidores (ver "consumido
     por" em cada entrada) — o merge não deve surpreender quem depende do
     contrato.
3. Nenhum workstream deve reimplementar um contrato já publicado (ex.: uma
   cópia local de `ConversationEvent` ou um segundo caminho de escrita no
   audit ledger fora de `dse_audit.emit`) — isso quebra a garantia de "fonte
   única de verdade" que o contrato existe para prover.

Versionamento: semver (`MAJOR.MINOR.PATCH`) por pacote, declarado no
`pyproject.toml` de cada um. MAJOR = breaking; MINOR = aditivo; PATCH = fix
sem mudança de superfície pública.

## Pacotes e versões atuais

| Pacote | Versão | Dono | Consumido por |
|---|---|---|---|
| `dse_contracts` (`packages/contracts`) | 0.1.0 | Fundação | WS-A, WS-B, WS-C, WS-D, WS-E, WS-F |
| `dse_audit` (`packages/dse_audit`) | 0.1.0 | Fundação (mínimo) → **estendido pelo WS-F na Fase 1** | Todos (via `emit`); `dse_audit.queries` (reconstrução/export) consumido por qualquer serviço/relatório de compliance |
| `dse_identity` (`packages/dse_identity`) | 0.1.0 | Fundação (mínimo) | WS-A (adapters resolvem `platform_user_id` antes de gravar `actor`) |

## Entradas

### `dse_audit` 0.1.0 → extensão aditiva (WSF-E1-T2, sem bump de versão declarado no pyproject — ver nota abaixo)

- **O quê:** novo módulo `dse_audit/queries.py` com
  `reconstruct_work_item_history(work_item_id) -> list[dict]`,
  `export_audit_range(tenant_id, start, end) -> list[dict]` e
  `export_audit_range_csv(...) -> str`. Reexportado em `dse_audit/__init__.py`
  junto dos símbolos já existentes (`emit`, `get_connection` — nenhum deles
  foi removido/renomeado).
- **Por quê:** exit criterion da Fase 1 ("first audit-based reconstruction
  exercise passes") + export compliance-grade por tenant/período.
- **Tipo de mudança:** aditivo (regra 1 acima) — não requer aprovação prévia
  do arquiteto-chefe, mas **está documentado aqui para visibilidade cross-
  workstream**, já que `packages/dse_audit` é um diretório da fundação e
  outros workstreams podem (razoavelmente) não esperar mudanças nele.
- **Nota de processo:** o `pyproject.toml` de `dse_audit` continua declarando
  `version = "0.1.0"` — recomendação do WS-F para a consolidação final: bump
  para `0.2.0` (MINOR, aditivo) no PR de integração, já que uma versão nova
  de fato foi publicada.
- **Consumido por:** qualquer serviço/relatório que precise responder "o que
  aconteceu com o WorkItem X" ou produzir um export de auditoria — nenhum
  consumidor real ainda integrado nesta sessão (cross-workstream, integração
  na fase de consolidação).

### `services/platform` (dse-platform) 0.1.0 → novo pacote (WSF-E2-T3a)

- **O quê:** `dse_secrets` — cliente do Vault (`SecretsClient`, `get_secret`,
  `put_secret`, `delete_secret`). Não é um pacote em `packages/` porque é
  específico do WS-F (plataforma), mas é publicado como contrato de consumo
  estável para WS-A/WS-C/WS-D (assinatura documentada em
  `services/platform/README.md`).
- **Tipo de mudança:** novo pacote, não é uma mudança de contrato existente —
  não requer aprovação do arquiteto-chefe para a v0.1.0 inicial, mas
  mudanças futuras na assinatura pública de `SecretsClient` seguem a regra 2
  acima assim que WS-A/WS-C/WS-D integrarem de fato.

## Como propor uma mudança breaking

1. Abra uma issue/PR descrevendo o campo/assinatura afetado, por que o
   aditivo não é suficiente, e todos os consumidores conhecidos.
2. Marque o arquiteto-chefe do programa para revisão.
3. Após aprovação, faça o bump de versão + esta entrada no changelog no
   mesmo PR do contrato (antes de qualquer PR que dependa da mudança).
