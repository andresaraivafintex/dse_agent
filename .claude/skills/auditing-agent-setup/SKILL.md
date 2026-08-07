---
name: auditing-agent-setup
description: Audits and repairs a Claude Code setup that produces regression loops — the agent declares work done without running anything, the same bugs return, long sessions drift from earlier decisions. Installs the missing verification mechanism (hooks, executable Definition of Done, test-first gates) and rightsizes CLAUDE.md and skills. Use when the user says work is going in circles, bugs keep coming back, the agent reported success on something broken, or asks to audit or fix their Claude Code configuration. Not for debugging one specific bug, and not for greenfield project setup where nothing is broken yet.
---

# Auditing and repairing a Claude Code setup

Run this when a repo produces regression loops. Work through the phases in order — each one gates the next. Do not skip to Phase 3 fixes before completing the Phase 1 audit, because the fix depends on which layer is empty.

Background and evidence: `references/principios.md`. Opus 5 specifics: `references/opus-5.md`. Read them before Phase 2 if you have not already.

## The model this skill applies

Every piece of Claude Code configuration sits in one of three layers:

| Layer | What it is | Examples | Holds under pressure? |
|---|---|---|---|
| 1. Mechanism | Runs outside the agent's control | hooks, CI, tests, typecheck | Yes |
| 2. Structure | Shapes what the agent sees and reaches | start directory, LSP, plan/grounding files | Mostly |
| 3. Instruction | Text the agent must recall and choose to obey | CLAUDE.md, skills, prompts | Degrades |

**The rule that governs every fix in this skill: never ask at layer 3 for something you can guarantee at layer 1.** A repo in a regression loop almost always has a full layer 3 and an empty layer 1.

## Phase 1 — Audit

Gather facts before changing anything. Report findings as a table before proposing fixes.

**1.1 Find the verification instruments.** Read `package.json` scripts, `Makefile`, `pyproject.toml`, `Cargo.toml`, `go.mod`, and CI config (`.github/workflows/`). Record the real commands for: test, single-test, typecheck/build, lint. If a category has no command, that is a Phase 3 finding, not a blocker.

**1.2 Check layer 1.** Does `.claude/settings.json` define hooks? Which events? Does any hook actually gate — that is, can it exit non-zero and stop the agent? A hook that only formats is not a gate.

**1.3 Check layer 2.** Is there a code-intelligence/LSP plugin? Does the repo have per-directory `CLAUDE.md`, or one root file covering everything? Are there plan or grounding files that survive compaction?

**1.4 Check layer 3 for bloat.** Count lines in every `CLAUDE.md`. List every skill and measure its body. Flag: any `CLAUDE.md` over ~50 lines, any skill body over ~500 lines, any duplicated frontmatter, any skill whose description lacks a "not for" exclusion.

**1.5 Look for the rationalization loophole.** For each skill that claims to confirm, verify, or check anything: does it name a command whose exit code the agent does not control? If it relies on the agent's own judgement of its own output, flag it. This is the single most common defect — a 2026 study of 238 real skills found ~94% carry it.

**1.6 Look for coverage gaps.** Does the test suite cover the code paths where bugs actually recur? Ask the user which bugs came back; check whether a test exists for each. A returning bug with no test is the clearest possible signal.

Report as:

```
| Camada | Item | Estado | Achado |
|--------|------|--------|--------|
| 1 | hooks | ausente | nenhum gate; agente não recebe erro |
| 3 | CLAUDE.md raiz | 240 linhas | virou documentação de arquitetura |
```

## Phase 2 — Diagnose

Name the failure mode. Pick from these; more than one can apply.

- **Norm without instrument.** Config demands real evidence but defines no command. Most common.
- **Unbraked autonomy.** Long unattended runs with layer 1 empty. Amplified by any instruction resembling "keep working without stopping" — that phrasing measurably increases the rate at which agents fake or shortcut completion.
- **Instruction bloat.** Layer 3 so large it displaces working context and goes stale.
- **Blind refactor.** No LSP or code graph, so cross-file changes miss call sites.
- **Compaction drift.** Long sessions where hour-3 work contradicts hour-1 decisions because grounding lived only in conversation.
- **Missing regression net.** Bugs return because fixes never left a test behind.

State which apply and why, citing the Phase 1 table. Do not propose fixes yet.

## Phase 3 — Repair, in this order

Stop after each step and confirm it works before starting the next. Fixing layer 3 first is wasted effort.

**3.1 Install the gate (layer 1).** Copy `assets/verify-edit.sh` and `assets/gate-stop.sh` into `.claude/hooks/`, edit the command block at the top of each to the real commands found in 1.1, `chmod +x` both, and register them per `assets/settings-exemplo.json`. Verify by making a deliberate type error and confirming the agent receives it in the same turn.

Two things that break this silently: a missing `chmod +x`, and a matcher whose case does not match (`edit` never matches `Edit`).

**3.2 Establish test-first for bug fixes.** The rule: write the failing test, run it, **commit it while it fails**, then fix without touching the test. Committing the red test is the load-bearing part — it makes any later alteration of the test visible in the diff. Add one line to `CLAUDE.md` stating this; do not add more.

**3.3 Make the Definition of Done executable.** Every acceptance criterion maps to a command with an expected exit code. A criterion no command can prove is capped at IMPLEMENTED-NOT-VERIFIED and reported as such at the start, never dressed up as done. Install the `/goal` command if the user does not have it.

**3.4 Add layer 2 if 3.1–3.3 did not close the loop.** Code-intelligence plugin for the language; start sessions from the relevant subdirectory rather than the repo root; grounding and plan written to files under `.claude/work/`.

**3.5 Rightsize layer 3, last.** Run `/doctor` if available. Cut each `CLAUDE.md` to commands, invariants, gotchas, and definition of done — nothing that can be learned by reading the code. Move anything longer into a skill so it loads on demand. Delete skills that duplicate what a hook now enforces.

**Do not** add instructions telling the agent to verify, double-check, or re-check its work. On current models that produces over-verification and self-verification loops without improving correctness. Give the instrument, not the exhortation. See `references/opus-5.md`.

## Phase 4 — Confirm

The repair worked if all four hold. Measure, do not assume.

1. A deliberate type error is surfaced to the agent in the same turn, without the user pointing it out.
2. A fix cannot be declared done while the relevant test fails.
3. Each returning bug named in 1.6 now has a committed regression test.
4. Every criterion in the next DoD maps to a command.

If any fails, return to the corresponding Phase 3 step. Do not compensate with more instructions — that is the loop this skill exists to break.

## Anti-rationalization

Apply while running this skill and install in any skill that claims to verify.

| If you find yourself thinking | Do this instead |
|---|---|
| "This test is outdated or wrong" | Do not edit it. Report BLOCKED with the reason. |
| "The code is obviously correct, no need to run it" | Run it. Obviousness is not an exit code. |
| "I'll adjust the assertion to match the new behaviour" | Only if the DoD asked for a behaviour change. Otherwise BLOCKED. |
| "It fails because of the environment, not my change" | Prove it: run at the previous commit. Passed before? It's yours. |
| "I'll skip this criterion and report the rest" | Report it explicitly as BLOCKED. Omission is not permitted. |
| "The setup is basically fine, just needs tuning" | Complete Phase 1 before concluding that. |

## Reference material

- `references/principios.md` — the three-layer model and the evidence behind each fix.
- `references/opus-5.md` — what changed with Opus 5 and which older advice it retires.
- `assets/verify-edit.sh`, `assets/gate-stop.sh` — hook scripts to copy and edit.
- `assets/settings-exemplo.json` — hook registration.
- `assets/CLAUDE-template.md` — the shape a per-area CLAUDE.md should have.
