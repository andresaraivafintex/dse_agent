# Fintex DSE — Programa de red-team (WSF-E8-T3, Fase 4)

**Status: P0 — deve estar em pé ANTES do primeiro repositório de cliente real.**

Este documento define o programa contínuo de red-team do DSE: dono, cadência, escopo, e a
fronteira honesta entre o que é **automatizado hoje** (suíte executável que falha o build) e o
que é **item manual** do programa (ainda não automatizável neste ambiente).

Companheiro obrigatório: `infra/THREAT-MODEL.md` (as ameaças que este programa exercita) e a
suíte `services/platform/tests/test_red_team.py` (a materialização em CI).

---

## 1. Dono e responsabilidades (RACI)

| Papel | Nome/função | Responsabilidade |
|---|---|---|
| **Dono (Accountable)** | **Security Lead do WS-F** (plataforma/segurança) | Mantém este programa, o threat model e a suíte executável; assina o pacote do pilot gate "client security/data review passed". |
| Executor (Responsible) | Engenharia de plataforma (WS-F) + rotativo de 1 engenheiro por workstream por trimestre | Roda os drills, escreve novos ataques, corrige regressões. |
| Consultado (Consulted) | Donos de WS-A (intake), WS-C (sandbox/egress/skill), WS-D (gateway), WS-E (validação/merge-base) | Revisam ataques contra seus controles; recebem os `spawn`/issues de regressão. |
| Informado (Informed) | Arquiteto + stakeholder do piloto | Recebem o relatório de cada ciclo e o veredito de go/no-go de segurança. |

**Dono nomeado nesta fase:** o Security Lead do WS-F é o dono default até o cliente do piloto
nomear um contato de segurança; a partir daí o programa roda em conjunto (o cliente pode exigir
seu próprio pentest de terceiros — ver §5, item externo).

---

## 2. Cadência

| Gatilho | O que roda | Quem |
|---|---|---|
| **Todo CI (cada PR)** | Suíte executável `test_red_team.py` inteira (21 ataques). Um controle que regride = build vermelho. | Automático |
| **Toda release / tag** | Suíte executável + `helm template` de topologias A e B + scanner de plaintext secrets. | Automático |
| **Antes do 1º repo de cliente (P0)** | Ciclo manual completo (§4) + revisão do threat model contra o deployment concreto do cliente (tier de modelo, topologia). | Dono + executor |
| **Trimestral** | Ciclo manual completo; rotação do engenheiro executor; revisão de novas ameaças (novos adapters, novo substrato, novo provider). | Dono + rotativo |
| **Ad-hoc** | A cada mudança em um controle de segurança (assinatura, egress allowlist, isolamento, promoção de skill), o autor adiciona/ajusta o ataque correspondente NO MESMO PR. | Autor da mudança |
| **Pós-incidente** | Um novo caso de ataque reproduzindo o incidente é adicionado à suíte antes de fechar o postmortem (teste de regressão de segurança). | Dono |

---

## 3. Escopo — ameaças exercitadas (mapeadas ao threat model)

O escopo é exatamente o conjunto de ameaças do `THREAT-MODEL.md`. Estado A = automatizado na
suíte; M = item manual (§5).

| Ameaça (threat model §) | Ataque | Estado |
|---|---|---|
| Forged task injection (2.1) | Assinatura HMAC forjada / chave errada / ausente rejeitada; replay fora da janela | **A** (`TestForgedWebhook`) |
| Indirect prompt injection / OWASP LLM01 (2.2) | Unicode invisível/bidi + secret plantado no `content_snapshot`; contenção via egress | **A** (`TestPromptInjection`) |
| Exfiltração / SSRF (2.5) | GET para pastebin/telegram/metadata + bypass por confusão de host, via proxy | **A** (`TestPromptInjection::test_egress_denies_exfiltration`) |
| Vazamento cross-tenant (2.9) | A lê skill/retrieval/audit/token/artifact de B → fail-closed + audit | **A** (`TestCrossTenant`) |
| Skill maliciosa / auto-promoção (2.4) | Candidate tenta virar active/approved sem aprovador humano → recusado; candidate nunca é servida ao Planner | **A** (`TestMaliciousSkill`) |
| Roubo/replay de credencial (2.5) | Replay de token efêmero contra upstream real | **M** (precisa sandbox + upstream controlado — cross-WS) |
| Confusão de privilégio (2.2/2.8) | Steering forjado; operador não-autorizado no console | **A** (parcial: steering em `ingest-gateway/tests/test_steering.py`); **M** para o console sem IdP real |
| Supply-chain drift (2.9) | Dependência OSS adulterada / imagem não assinada / CVE conhecido | **M** (sem SBOM/assinatura/scan de CVE no CI — item de maior prioridade da lista manual) |
| Merge automático / P3 (2.3) | Path de merge no source | **A** (invariante estático `orchestrator/tests/test_review_loop.py::test_no_automatic_merge_path_in_source`) |
| Adulteração do audit ledger (2.9) | UPDATE/DELETE em `audit_log` como `dse_app` | **A** (verificado no adendo 03; `packages/dse_audit/tests`) |

---

## 4. Procedimento do ciclo manual (antes do 1º repo de cliente e trimestral)

1. **Preparação**: subir a infra (`make up` num ambiente dedicado, NÃO o compartilhado), aplicar
   migrações, ativar o venv do WS-F.
2. **Rodar a suíte executável** e confirmar 0 falhas / 0 skips inesperados:
   `pytest -q services/platform/tests/test_red_team.py`. Um skip é aceitável só quando o
   controle-alvo legitimamente não está no ambiente (documentar por quê no relatório).
3. **Ataques manuais** (os itens M da §5), com evidência anexada (logs de audit, respostas HTTP,
   screenshots do console).
4. **Revisão do threat model contra o deployment concreto**: o tier de modelo (1 PrivateLink / 2
   air-gapped) e a topologia (A/B) do cliente mudam a superfície — confirmar que cada linha da
   matriz ainda vale e que a allowlist do egress reflete só os hosts daquele cliente.
5. **Relatório**: preencher o template (§6), listar regressões como issues/`spawn` para o WS dono,
   e emitir o veredito de go/no-go de segurança para o dono.

---

## 5. Itens MANUAIS do programa (ainda não automatizáveis) — honesto (P8)

Cada item diz **por que** não está na suíte e **o que** o desbloquearia.

1. **Replay de credencial efêmera contra upstream real.** Por quê: precisa de um sandbox real
   (WS-C) rodando uma sessão + um upstream de teste controlado para capturar e repetir um token
   de verdade — não é verificável só contra a interface HTTP do proxy. Desbloqueio: teste de
   integração cross-WS (WS-C + WS-F) na consolidação. Já documentado como intenção em
   `services/platform/tests/test_egress_proxy_adversarial.py::TestCredentialReuse`.
2. **Supply-chain (o item de maior prioridade da lista manual).** Por quê: hoje só há BOM manual
   (`infra/OSS-BOM.md`) + tags pinadas no Helm; não há assinatura de imagem (cosign), SBOM
   gerado, nem scan de CVE no CI. Desbloqueio: adicionar geração de SBOM + `cosign verify` no
   pipeline de build e um scanner de CVE (trivy/grype) como gate. Até lá, é verificação manual a
   cada release: diff da BOM + revisão de advisories dos pacotes de `infra/OSS-BOM.md`.
3. **Console sem IdP real.** Por quê: o verificador OIDC é real mas nenhum IdP está provisionado
   nesta sessão (login = 503 por design). Desbloqueio: apontar `DSE_OIDC_*` para um IdP real e
   então automatizar "usuário sem claim de operador é recusado + auditado".
4. **Assinatura real de GitHub App / Slack / Jira.** Por quê: a lógica HMAC é de produção, mas os
   secrets são de env/fixture. Desbloqueio: registrar as apps reais (item administrativo de maior
   lead time — adendo 03 §Parte 3) e re-rodar `TestForgedWebhook` contra os secrets reais.
5. **Pentest de terceiros / bug bounty.** Por quê: um red-team interno tem ponto cego sobre o
   próprio design. Desbloqueio: contratar pentest externo antes do go-live com cliente que o
   exija; o cliente do piloto pode trazer o seu. Escopo entregue ao terceiro = este documento +
   o threat model.
6. **Escape de sandbox a nível de kernel.** Por quê: os caps de recurso e o rootless são testados
   (`sandbox-runtime/tests/test_resource_caps_and_metrics.py`, `test_network_isolation.py`), mas
   um 0-day de container escape está fora do que um teste de aplicação cobre. Desbloqueio:
   defesa em profundidade de infra (gVisor/Kata, seccomp/AppArmor endurecido) + monitoramento —
   responsabilidade compartilhada com a operação de plataforma do cliente.

Nenhum item manual é um controle **ausente** que se finge presente — todos ou têm o controle no
código com uma lacuna de *verificação* automatizada, ou são responsabilidade de infra/negócio
declarada.

---

## 6. Template de relatório de ciclo

```
Ciclo de red-team — <data> — executor: <nome> — gatilho: <CI|release|pre-cliente|trimestral|incidente>
Ambiente: <dedicado/efêmero> · tier de modelo: <1|2> · topologia: <A|B>

Suíte executável:  <N> passaram / <N> falharam / <N> skip
  Skips (com razão): ...
  Regressões (issue/spawn aberto p/ WS dono): ...

Ataques manuais (§5):
  1. Replay de credencial ...... <feito|n/a> — evidência: <link>
  2. Supply-chain (SBOM/CVE) .... <feito|n/a> — evidência: <link>
  3. Console/IdP ................ <feito|n/a>
  ...

Revisão do threat model vs deployment concreto: <ok|desvios encontrados>
Veredito de segurança: <GO | NO-GO> — justificativa: ...
```
