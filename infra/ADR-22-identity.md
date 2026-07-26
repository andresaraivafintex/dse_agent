# ADR-22 — Identity, SSO/SCIM and offboarding (Fintex DSE)

Status: **Accepted** (Phase 2). Author: WS-F. Supersedes Phase 1's
auto-registration resolution (`dse_identity.resolve_principal`) for **admin
console** users; keeps auto-registration for chat/VCS actors (Slack/GitHub/
Jira) that show up in a `ConversationEvent`.

## Context

In Phase 1, any actor seen on a surface (a Slack mention, a GitHub
comment) is resolved to a unique `principal_id` by auto-registration on first
appearance (`dse_identity.resolve_principal(platform, platform_user_id)`), with the
map living in `principals` + `identity_links`. That is enough to attribute
event authorship, but it does **not** give:

- **account matching** between the corporate identity (IdP: Okta/Entra/Ping/
  Keycloak) and the DSE principal;
- **authorization** for who may operate the console, approve plans, or steer
  tasks;
- **offboarding** — when someone leaves the company, their access and their
  approver/steering role must die immediately and auditably;
- handling of **contractors** (access with an expiry).

Phase 2 introduces the plan approval gate (WS-B), the operable queue board
(WS-F/E6) and per-tenant access bundles (WS-F/E3-T2) — all of which need a
console identity authenticated by SSO and a notion of "active vs.
offboarded". This ADR settles those decisions.

## Decision

### 1. Protocol: OIDC first, SAML via an adapter

Console login uses **OpenID Connect (OIDC)** — the IdP issues an `id_token`
(RS256 JWT) that the console validates against the IdP's `jwks_uri` (signature, `iss`,
`aud` = client_id, `exp`). Implemented in `dse_platform.sso.OIDCVerifier` +
`login`. For IdPs that only speak **SAML**, the recommendation is an OIDC broker in
front (Keycloak/Dex/`oauth2-proxy`) that speaks SAML to the IdP and OIDC to the DSE —
the console does **not** implement its own SAML parser (less attack
surface; boring-first, P7). The verification contract (signature + claims) is the
same on both sides.

### 2. Account matching: by stable `sub`, never by email

The matching key between the IdP and the DSE principal is the `id_token`'s
**`sub`** (subject) — an opaque, stable identifier from the IdP. **Email is not a
matching key** (it can be reassigned to another person after someone leaves; it is mutable
PII). Email is kept only for display/contact.

`dse_console_identity` (migration `0013_wsf2.sql`) is the matching table:
`sso_subject` (UNIQUE) → `principal_id`. The `principal_id` is an ordinary `usr_<uuid>`
in `principals`.

> **Foundation note (real limitation, documented):** the foundation's
> `identity_links` (`0001_foundation.sql`) has a
> `platform IN ('slack','github','jira')` CHECK. It is not possible to write
> `platform = 'sso'` there, and the foundation migration cannot be edited in this
> phase (coexistence rule). Therefore an SSO user's principal is
> created **directly** in `principals` (via `dse_platform.sso.ensure_sso_principal`)
> and the account matching lives in `dse_console_identity.sso_subject` — **not** in
> `identity_links`. Consumers still see a `usr_<uuid>` identical to that of
> any other principal; the public signature does not change. Once the foundation
> relaxes the CHECK (adding `'sso'`), the link can optionally be mirrored into
> `identity_links` to unify chat+VCS+SSO under the same principal (the
> `email`/`sub` field would provide the join). Recorded as debt in the service README.

### 3. SCIM / provisioning

Phase 2 implements the **just-in-time (JIT) login** path: the first time
a valid `sub` logs in, the console identity is created. Roles (`operator`,
`approver`, `viewer`, `admin`) can be pre-provisioned by an admin via
`provision_console_user` (or, in production, by a SCIM endpoint on the IdP that
writes to the same table — the schema already supports it; the SCIM endpoint itself is
per-client integration work, out of scope for this phase's code — see README,
gaps). Absence of a role = implicit `viewer` (cannot operate controls nor
approve).

### 4. Offboarding — immediate, cascading effect

`dse_platform.sso.offboard(principal_id, reason, actor)` sets
`active = false` + `deactivated_at`. Effects, all immediate (checked per
request/decision, not by a nightly job):

| Surface | How offboarding takes effect |
|---|---|
| **Console login** | `login()` refuses (`LoginDenied`) and every request re-checks `is_console_active` (an already-issued session dies on the next request). |
| **Plan-gate approver cascade** | `access_bundles.resolve_plan_approvers` filters out principals with `active = false` (or expired). If the cascade empties out, `require_plan_approver` **blocks** (P3: never auto-approves). |
| **Task steering** | `steering_resolution.is_steering_allowed` denies even if the principal is still on the WS-A allowlist. |

Every change writes an audit record (`console_user_offboarded`, `console_login_denied`,
`approvers_filtered_offboarded`) — P8.

Design rule: a principal **without** a row in `dse_console_identity` (e.g. a
CODEOWNER who never logged into the console) is treated as **active** in the
approver/steering cascade — we only remove those **explicitly** deactivated/expired.
This avoids blocking legitimate approvers who never needed the console.

### 5. Contractors — access with an expiry

`is_contractor = true` + `expires_at`. `is_console_active` and the approver
cascade treat `expires_at < now()` exactly like offboarded (login denied,
removed from the cascade). Renewal = update `expires_at` via
`provision_console_user`. Auditable.

## Consequences

- **Positive:** stable account matching; offboarding with immediate, auditable
  effect on 3 surfaces; contractors with automatic expiry; no consumer of the
  `principal_id` signature breaks (the foundation's `resolve_principal` still
  handles chat/VCS).
- **Negative / debt:** SSO and chat/VCS do not share the same principal
  yet (the foundation's `identity_links` CHECK blocks unification) — the same
  human may have a distinct SSO principal and GitHub principal until the
  foundation relaxes the CHECK. A real SCIM endpoint and a SAML broker are per-client
  integration work, not included in this phase's code (see the README gaps).

## Implementation (files)

- `services/platform/dse_platform/sso.py` — `OIDCVerifier`, `login`, `offboard`,
  `provision_console_user`, `ensure_sso_principal`, `is_console_active`.
- `services/platform/dse_platform/dev_idp.py` — dev OIDC IdP (fixture, mints
  RS256 id_tokens + JWKS) to exercise the verifier without a real IdP.
- `services/platform/dse_platform/steering_resolution.py` — offboarding × steering.
- `services/platform/dse_platform/access_bundles.py` — offboarding × approver cascade.
- `migrations/0013_wsf2.sql` — `dse_console_identity`.
- Console login: `services/platform/dse_platform/queue_board/app.py` (`/login`).
