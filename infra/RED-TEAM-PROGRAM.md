# Fintex DSE — Red-team program (WSF-E8-T3, Phase 4)

**Status: P0 — must be standing BEFORE the first real client repository.**

This document defines the DSE's continuous red-team program: owner, cadence, scope, and the
honest boundary between what is **automated today** (an executable suite that fails the build) and
what is a **manual item** of the program (not yet automatable in this environment).

Mandatory companions: `infra/THREAT-MODEL.md` (the threats this program exercises) and the
`services/platform/tests/test_red_team.py` suite (its materialization in CI).

---

## 1. Owner and responsibilities (RACI)

| Role | Name/function | Responsibility |
|---|---|---|
| **Owner (Accountable)** | **WS-F Security Lead** (platform/security) | Maintains this program, the threat model and the executable suite; signs off the pilot gate package "client security/data review passed". |
| Executor (Responsible) | Platform engineering (WS-F) + a rotating engineer from each workstream per quarter | Runs the drills, writes new attacks, fixes regressions. |
| Consulted | Owners of WS-A (intake), WS-C (sandbox/egress/skill), WS-D (gateway), WS-E (validation/merge-base) | Review attacks against their controls; receive the regression `spawn`s/issues. |
| Informed | Architect + pilot stakeholder | Receive the report for each cycle and the security go/no-go verdict. |

**Owner named in this phase:** the WS-F Security Lead is the default owner until the pilot client
names a security contact; from then on the program runs jointly (the client may require
their own third-party pentest — see §5, external item).

---

## 2. Cadence

| Trigger | What runs | Who |
|---|---|---|
| **Every CI run (each PR)** | The entire executable `test_red_team.py` suite (21 attacks). A control that regresses = red build. | Automatic |
| **Every release / tag** | Executable suite + `helm template` for topologies A and B + plaintext-secret scanner. | Automatic |
| **Before the 1st client repo (P0)** | Full manual cycle (§4) + review of the threat model against the client's concrete deployment (model tier, topology). | Owner + executor |
| **Quarterly** | Full manual cycle; rotation of the executing engineer; review of new threats (new adapters, new substrate, new provider). | Owner + rotation |
| **Ad-hoc** | On every change to a security control (signature verification, egress allowlist, isolation, skill promotion), the author adds/adjusts the corresponding attack IN THE SAME PR. | Author of the change |
| **Post-incident** | A new attack case reproducing the incident is added to the suite before closing the postmortem (security regression test). | Owner |

---

## 3. Scope — threats exercised (mapped to the threat model)

The scope is exactly the set of threats in `THREAT-MODEL.md`. State A = automated in the
suite; M = manual item (§5).

| Threat (threat model §) | Attack | State |
|---|---|---|
| Forged task injection (2.1) | Forged HMAC signature / wrong key / missing key rejected; replay outside the window | **A** (`TestForgedWebhook`) |
| Indirect prompt injection / OWASP LLM01 (2.2) | Invisible/bidi Unicode + a planted secret in the `content_snapshot`; containment via egress | **A** (`TestPromptInjection`) |
| Exfiltration / SSRF (2.5) | GET to pastebin/telegram/metadata + host-confusion bypass, through the proxy | **A** (`TestPromptInjection::test_egress_denies_exfiltration`) |
| Cross-tenant leak (2.9) | A reads B's skill/retrieval/audit/token/artifact → fail-closed + audit | **A** (`TestCrossTenant`) |
| Malicious skill / self-promotion (2.4) | A candidate tries to become active/approved without a human approver → refused; a candidate is never served to the Planner | **A** (`TestMaliciousSkill`) |
| Credential theft/replay (2.5) | Replay of an ephemeral token against a real upstream | **M** (needs a sandbox + a controlled upstream — cross-WS) |
| Privilege confusion (2.2/2.8) | Forged steering; unauthorized operator on the console | **A** (partial: steering in `ingest-gateway/tests/test_steering.py`); **M** for the console without a real IdP |
| Supply-chain drift (2.9) | Tampered OSS dependency / unsigned image / known CVE | **M** (no SBOM/signing/CVE scanning in CI — highest-priority item on the manual list) |
| Automatic merge / P3 (2.3) | A merge path in the source | **A** (static invariant `orchestrator/tests/test_review_loop.py::test_no_automatic_merge_path_in_source`) |
| Audit ledger tampering (2.9) | UPDATE/DELETE on `audit_log` as `dse_app` | **A** (verified in addendum 03; `packages/dse_audit/tests`) |

---

## 4. Manual cycle procedure (before the 1st client repo and quarterly)

1. **Preparation**: bring up the infra (`make up` in a dedicated environment, NOT the shared one),
   apply migrations, activate the WS-F venv.
2. **Run the executable suite** and confirm 0 failures / 0 unexpected skips:
   `pytest -q services/platform/tests/test_red_team.py`. A skip is acceptable only when the
   target control is legitimately absent from the environment (document why in the report).
3. **Manual attacks** (the M items from §5), with evidence attached (audit logs, HTTP responses,
   console screenshots).
4. **Review the threat model against the concrete deployment**: the model tier (1 PrivateLink / 2
   air-gapped) and the client's topology (A/B) change the surface — confirm that every row of the
   matrix still holds and that the egress allowlist reflects only that client's hosts.
5. **Report**: fill in the template (§6), file regressions as issues/`spawn`s for the owning WS,
   and issue the security go/no-go verdict to the owner.

---

## 5. MANUAL items of the program (not yet automatable) — honest (P8)

Each item states **why** it is not in the suite and **what** would unblock it.

1. **Replay of an ephemeral credential against a real upstream.** Why: it needs a real sandbox
   (WS-C) running a session + a controlled test upstream in order to capture and replay a real
   token — it is not verifiable against the proxy's HTTP interface alone. Unblocker: a cross-WS
   integration test (WS-C + WS-F) at consolidation. Already documented as intent in
   `services/platform/tests/test_egress_proxy_adversarial.py::TestCredentialReuse`.
2. **Supply-chain (the highest-priority item on the manual list).** Why: today there is only a
   manual BOM (`infra/OSS-BOM.md`) + pinned tags in Helm; there is no image signing (cosign), no
   generated SBOM, and no CVE scanning in CI. Unblocker: add SBOM generation + `cosign verify` to
   the build pipeline and a CVE scanner (trivy/grype) as a gate. Until then, it is a manual check
   at each release: BOM diff + review of advisories for the packages in `infra/OSS-BOM.md`.
3. **Console without a real IdP.** Why: the OIDC verifier is real but no IdP is provisioned
   in this session (login = 503 by design). Unblocker: point `DSE_OIDC_*` at a real IdP and
   then automate "a user without the operator claim is refused + audited".
4. **Real GitHub App / Slack / Jira signatures.** Why: the HMAC logic is production code, but the
   secrets come from env/fixture. Unblocker: register the real apps (administrative item with the
   longest lead time — addendum 03 §Part 3) and re-run `TestForgedWebhook` against the real secrets.
5. **Third-party pentest / bug bounty.** Why: an internal red-team has a blind spot for its
   own design. Unblocker: contract an external pentest before go-live with any client that
   requires it; the pilot client may bring their own. Scope handed to the third party = this
   document + the threat model.
6. **Kernel-level sandbox escape.** Why: the resource caps and rootless execution are tested
   (`sandbox-runtime/tests/test_resource_caps_and_metrics.py`, `test_network_isolation.py`), but
   a container-escape 0-day is beyond what an application test covers. Unblocker:
   infra defense in depth (gVisor/Kata, hardened seccomp/AppArmor) + monitoring —
   shared responsibility with the client's platform operations.

No manual item is an **absent** control pretending to be present — each one either has the control
in the code with a gap in *automated verification*, or is a declared infra/business responsibility.

---

## 6. Cycle report template

```
Red-team cycle — <date> — executor: <name> — trigger: <CI|release|pre-client|quarterly|incident>
Environment: <dedicated/ephemeral> · model tier: <1|2> · topology: <A|B>

Executable suite:  <N> passed / <N> failed / <N> skipped
  Skips (with reason): ...
  Regressions (issue/spawn opened for the owning WS): ...

Manual attacks (§5):
  1. Credential replay .......... <done|n/a> — evidence: <link>
  2. Supply-chain (SBOM/CVE) .... <done|n/a> — evidence: <link>
  3. Console/IdP ................ <done|n/a>
  ...

Threat model review vs concrete deployment: <ok|deviations found>
Security verdict: <GO | NO-GO> — rationale: ...
```
