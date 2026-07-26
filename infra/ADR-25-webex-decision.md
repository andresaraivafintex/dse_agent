# ADR-25 — Cisco Webex as an intake surface (Fintex DSE)

Status: **Accepted — FORMAL DE-SCOPE (Webex out of the pilot's scope)**. Phase 4. Author: WS-F.
Go/no-go decision executed per addendum 03 §Part 2 #6 and §Part 3 ("either implement it behind the
adapter OR record a formal de-scope").

Nothing is dropped silently — this ADR is the explicit record required by the spirit of the
ADR discipline: the decision, the rationale, and **exactly what reverting it would cost**.

## Context

The DSE receives work requests over chat/ticket surfaces normalized into a
`ConversationEvent` (`dse_contracts.conversation_event`). The adapter interface is already **proven
and mature**: three adapters in production as the mold — `adapter-slack`, `adapter-github`,
`adapter-jira` — all with the same shape (inbound with signature verification → sanitization →
`ConversationEvent`; outbound with "exactly 1 mutable status comment per surface" via
`dse_contracts.mutable_comment.MutableCommentWriter`).

Webex appeared in the initial planning as a possible fourth chat surface. The go/no-go
question: **build `adapter-webex` now, or formally de-scope it?**

Facts weighing on the decision (verified, not assumed):

1. **Teams is the prioritized chat provision**, not Webex (addendum 03 §Part 2 #7 lists the Teams
   adapter as the orphaned chat scope that gets effort; Webex does not).
2. The adapter interface is a **stable mold** — adding a surface is mechanical, but each
   new adapter carries a real, recurring cost: webhook secret registration/rotation, its
   own signature scheme to attack in the red-team (`test_red_team.py::TestForgedWebhook`
   covers Slack/GitHub today; a new one would need its own row), a
   `MutableCommentWriter` back end, tenant mapping (`tenant_platform_bindings`), and a real
   app credential (the longest-lead-time item — addendum 03 §Part 3).
3. **No pilot client asked for Webex.** The surfaces the pilots in sight require
   are Slack and/or GitHub and/or Jira, with Teams as the next provision.
4. Building an adapter with no real consumer violates boring-first (P7): it is attack surface
   and maintenance cost with no demand to justify it.

## Decision

**Formally de-scope Webex from the pilot's engineering scope.** Do not build `adapter-webex`
in this phase or before the pilot goes live. The available chat-adapter effort goes to
**Teams** (the prioritized provision).

This is a **business/prioritization** decision, not a technical limitation: the interface is ready,
the mold exists, and restoring Webex is mechanical (see "How to revert" below). The de-scope is about
**not spending effort now**, not about inability.

### Consequences

- The `ConversationEvent` contract and the adapter interface **remain surface-agnostic** —
  nothing in them presumes the set {Slack, GitHub, Jira, Teams}. Adding Webex later does not require
  a contract change (it is an additive change of a new `platform` value, as Jira was and Teams will be).
- The `platform` enum / routing does not gain a `webex` value now (we do not introduce dead code).
- The threat model (`infra/THREAT-MODEL.md §2.1`) and the red-team (`infra/RED-TEAM-PROGRAM.md §3`)
  cover "adapters" generically; when/if Webex lands, it gets its own signature attack row
  in the suite — an item already anticipated in the procedure (§ "ad-hoc: on every change to a
  security control, the author adds the corresponding attack").

## Alternative considered and rejected: implement it behind the adapter now

Build `adapter-webex` immediately, leveraging the mold. **Rejected** because:
- With no consumer (fact #3), it is cost and attack surface with no return (fact #4, P7).
- It competes for effort with Teams, which **does** have prioritized demand (fact #1).
- An adapter's real lead time is not the code (mechanical) but the **real app credential** +
  the webhook registration — which is only worth pulling when a concrete client uses Webex.

## How to revert (what restoring it would take) — nothing is discarded

If a client requires Webex, restoring it is **mechanical** and estimable, following the mold of the
three existing adapters:

1. **`services/adapter-webex/`** mirroring `adapter-jira/` (the most recent): an inbound handler that
   (a) verifies the Webex webhook signature (HMAC-SHA1/SHA256 over the body with the registration's
   secret — the same pure pattern as `ingest_gateway/security.py`), (b) sanitizes
   (`sanitize_content`), (c) emits a `ConversationEvent` with `platform="webex"`.
2. **Outbound** via a new `MutableCommentWriter` back end (Webex Messages API) — "exactly
   1 mutable status message per surface", identical to the others' contract.
3. **Tenant mapping**: one binding row in `tenant_platform_bindings` (migration 0008) —
   no new migration.
4. **Security**: register the real Webex app + webhook secret in Vault; add the forged-signature
   attack row in `services/platform/tests/test_red_team.py::TestForgedWebhook`;
   if the deployment is topology B/air-gapped, assess whether Webex (external SaaS) is admissible
   at all (it likely requires an explicit exception reviewed by the red-team, §4 of RED-TEAM-PROGRAM.md).
5. **Routing**: add `webex` to the ingest's status-signal routing (additive).

Reference estimate: comparable to the Teams adapter / any new adapter from the mold (≈3 pw, the
same order as the orphaned chat scope in addendum 03 §Part 2 #7). No platform work
(contract, isolation, audit) needs redoing — only the adapter and its credential registration.

## Sign-off

| Role | Decision | Note |
|---|---|---|
| Author (WS-F, platform/security) | **Formal de-scope approved** | Recorded in this ADR; reversal documented and mechanical. |
| Architect | **Signature required** | Consistent with Teams-prioritized (addendum 03) and P7 (boring-first). |
| Pilot stakeholder | **Signature required** | Confirm that no pilot client uses Webex as a primary surface. |

Until the architect/stakeholder signatures are collected, the operational status is
"de-scope proposed by WS-F, pending ratification" — but the engineering decision (do not build
now) already stands, since it is reversible at a known cost and blocks nothing.
