# ADR-22 — Identity, SSO/SCIM e offboarding (Fintex DSE)

Status: **Accepted** (Fase 2). Autor: WS-F. Substitui a resolução por
auto-registro da Fase 1 (`dse_identity.resolve_principal`) para os usuários do
**console admin**; mantém o auto-registro para atores de chat/VCS (Slack/GitHub/
Jira) que aparecem em `ConversationEvent`.

## Contexto

Na Fase 1, qualquer ator visto numa superfície (menção no Slack, comentário no
GitHub) é resolvido para um `principal_id` único por auto-registro na primeira
aparição (`dse_identity.resolve_principal(platform, platform_user_id)`), com o
mapa em `principals` + `identity_links`. Isso é suficiente para atribuir
autoria de eventos, mas **não** dá:

- **account matching** entre a identidade corporativa (IdP: Okta/Entra/Ping/
  Keycloak) e o principal do DSE;
- **autorização** de quem pode operar o console, aprovar planos, ou steerar
  tarefas;
- **offboarding** — quando alguém sai da empresa, o acesso e o papel de
  approver/steering precisam morrer imediatamente e de forma auditável;
- tratamento de **contractors** (acesso com expiração).

A Fase 2 introduz o gate de aprovação de plano (WS-B), o queue board operável
(WS-F/E6) e access bundles por tenant (WS-F/E3-T2) — todos precisam de uma
identidade de console autenticada por SSO e de uma noção de "ativo vs.
offboardado". Este ADR fecha essas decisões.

## Decisão

### 1. Protocolo: OIDC primeiro, SAML como adaptador

O login do console usa **OpenID Connect (OIDC)** — o IdP emite um `id_token`
(JWT RS256) que o console valida contra o `jwks_uri` do IdP (assinatura, `iss`,
`aud` = client_id, `exp`). Implementado em `dse_platform.sso.OIDCVerifier` +
`login`. Para IdPs que só falam **SAML**, a recomendação é um broker OIDC na
frente (Keycloak/Dex/`oauth2-proxy`) que fala SAML com o IdP e OIDC com o DSE —
o console **não** implementa um parser SAML próprio (menos superfície de
ataque; boring-first, P7). O contrato de verificação (assinatura + claims) é o
mesmo dos dois lados.

### 2. Account matching: por `sub` estável, nunca por email

A chave de matching entre o IdP e o principal do DSE é o **`sub`** (subject) do
`id_token` — um identificador opaco e estável do IdP. **Email não é chave de
matching** (pode ser reatribuído a outra pessoa depois que alguém sai; é PII
mutável). O email é guardado só para exibição/contato.

`dse_console_identity` (migração `0013_wsf2.sql`) é a tabela de matching:
`sso_subject` (UNIQUE) → `principal_id`. O `principal_id` é um `usr_<uuid>`
normal em `principals`.

> **Nota de fundação (limitação real, documentada):** o `identity_links` da
> fundação (`0001_foundation.sql`) tem um CHECK
> `platform IN ('slack','github','jira')`. Não é possível gravar
> `platform = 'sso'` lá, e a migração da fundação não pode ser editada nesta
> fase (regra de convivência). Portanto o principal de um usuário de SSO é
> criado **direto** em `principals` (via `dse_platform.sso.ensure_sso_principal`)
> e o account-matching vive em `dse_console_identity.sso_subject` — **não** em
> `identity_links`. Consumidores continuam vendo um `usr_<uuid>` idêntico ao de
> qualquer outro principal; a assinatura pública não muda. Quando a fundação
> relaxar o CHECK (adicionar `'sso'`), pode-se opcionalmente espelhar o link em
> `identity_links` para unificar chat+VCS+SSO sob o mesmo principal (o campo
> `email`/`sub` daria o join). Registrado como dívida no README do serviço.

### 3. SCIM / provisioning

Fase 2 implementa o caminho de **login just-in-time** (JIT): a primeira vez que
um `sub` válido loga, a identidade de console é criada. Papéis (`operator`,
`approver`, `viewer`, `admin`) podem ser pré-provisionados por um admin via
`provision_console_user` (ou, em produção, por um endpoint SCIM do IdP que
escreve na mesma tabela — o schema já suporta; o endpoint SCIM em si é trabalho
de integração por cliente, fora do escopo do código desta fase — ver README,
gaps). A ausência de papel = `viewer` implícito (não pode operar controles nem
aprovar).

### 4. Offboarding — efeito imediato e em cascata

`dse_platform.sso.offboard(principal_id, reason, actor)` seta
`active = false` + `deactivated_at`. Efeitos, todos imediatos (checados por
request/decisão, não por um job noturno):

| Superfície | Como o offboarding tem efeito |
|---|---|
| **Login do console** | `login()` recusa (`LoginDenied`) e cada request re-checa `is_console_active` (uma sessão já emitida morre no próximo request). |
| **Cascata de approvers do gate de plano** | `access_bundles.resolve_plan_approvers` filtra principals `active = false` (ou expirados). Se a cascata esvaziar, `require_plan_approver` **bloqueia** (P3: nunca auto-aprova). |
| **Steering de tarefas** | `steering_resolution.is_steering_allowed` nega mesmo que o principal ainda esteja na allowlist do WS-A. |

Toda mudança grava audit (`console_user_offboarded`, `console_login_denied`,
`approvers_filtered_offboarded`) — P8.

Regra de projeto: um principal **sem** linha em `dse_console_identity` (ex.: um
CODEOWNER que nunca logou no console) é tratado como **ativo** na cascata de
approver/steering — só removemos os **explicitamente** desativados/expirados.
Isso evita bloquear approvers legítimos que nunca precisaram do console.

### 5. Contractors — acesso com expiração

`is_contractor = true` + `expires_at`. `is_console_active` e a cascata de
approvers tratam `expires_at < now()` exatamente como offboardado (login negado,
removido da cascata). Renovação = atualizar `expires_at` via
`provision_console_user`. Auditável.

## Consequências

- **Positivas:** account matching estável; offboarding com efeito imediato e
  auditável em 3 superfícies; contractors com expiração automática; nenhum
  consumidor da assinatura `principal_id` quebra (o `resolve_principal` da
  fundação continua para chat/VCS).
- **Negativas / dívida:** SSO e chat/VCS não compartilham o mesmo principal
  ainda (o CHECK do `identity_links` da fundação bloqueia unificar) — um mesmo
  humano pode ter um principal de SSO e um principal de GitHub distintos até a
  fundação relaxar o CHECK. Endpoint SCIM real e broker SAML são integração por
  cliente, não incluídos no código desta fase (ver gaps do README).

## Implementação (arquivos)

- `services/platform/dse_platform/sso.py` — `OIDCVerifier`, `login`, `offboard`,
  `provision_console_user`, `ensure_sso_principal`, `is_console_active`.
- `services/platform/dse_platform/dev_idp.py` — IdP OIDC de dev (fixture, mina
  id_tokens RS256 + JWKS) para exercitar o verifier sem um IdP real.
- `services/platform/dse_platform/steering_resolution.py` — offboarding × steering.
- `services/platform/dse_platform/access_bundles.py` — offboarding × cascata de approver.
- `migrations/0013_wsf2.sql` — `dse_console_identity`.
- Login do console: `services/platform/dse_platform/queue_board/app.py` (`/login`).
