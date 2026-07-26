"""Stage-scoped toolsets (WSC-E3-T3/T4/T5).

Every Phase 2 agent session gets a toolset that is the ONLY tool surface
available to the substrate. Enforcement is by explicit allowlist + path guard —
not by the prompt's "good will":

- **PlannerToolset (read-only)**: read tools only. Any attempt to write a file,
  run a state-mutating test, or touch git FAILS with `ToolPermissionError`
  (conformance test WSC-E3-T3).
- **TesterToolset**: reads + `run_tests` + `write_file` ONLY under test paths;
  writing outside a test path FAILS (WSC-E3-T4). No git (the test commit is done
  by deterministic code in the Activity, scoped to test paths).
  Phase 3 (WSC-E3-T4b, addendum 02): `demos/<work_item_id>/` is an additional
  ALLOWED write path — it is where the Tester authors the Playwright `@demo`
  test that WS-E's evidence pipeline runs. The permission is scoped to the
  session's work_item: `demos/<other-id>/` stays BLOCKED (per-task isolation).
- **ReviewerToolset**: fresh context — only `read_plan`/`read_diff`. No repo
  access, no Coder history, no git (P3, WSC-E3-T5).

The substrate (`ScriptedAgentSession` in sessions.py, and in production the
OpenHands adapter with its tool registry filtered by this allowlist) never runs
a tool without going through `Toolset.check` first.
"""
from __future__ import annotations

from dataclasses import dataclass

# Paths considered "test" paths (TesterToolset only writes here). Covers the
# common layouts: pytest (`tests/`, `test_*.py`, `*_test.py`, `conftest.py`),
# jest/vitest (`*.test.ts`, `*.spec.ts`, `__tests__/`), go (`*_test.go`).
# Promoted to the shared contract (L1's plan_compliance uses it too — see
# dse_contracts.paths). The re-export keeps existing imports working.
from dse_contracts.paths import _TEST_PATH_RES, is_test_path  # noqa: F401


def demo_dir_for(work_item_id: str) -> str:
    """ADR-27/WSC-E3-T4b convention: canonical directory of a task's `@demo`
    test. The default of `RunDemoEvidenceInput.demo_dir` (foundation contract)
    derives from here — WS-E runs `npx playwright test --grep @demo` in this
    path."""
    return f"demos/{work_item_id}/"


def is_demo_path(path: str, work_item_id: str) -> bool:
    """True if `path` is INSIDE `demos/<work_item_id>/` (this work item's — never
    another's). Rejects `..` so the prefix cannot be escaped."""
    if not work_item_id:
        return False
    p = path.replace("\\", "/").lstrip("/")
    if ".." in p.split("/"):
        return False
    return p.startswith(demo_dir_for(work_item_id))


class ToolPermissionError(Exception):
    """Tool not permitted for the current stage — clean failure (P6)."""


@dataclass(frozen=True)
class ToolInvocation:
    tool: str
    args: dict


class Toolset:
    """Base. `name`, allowlist and `check(invocation)`, which raises when denied."""

    name = "base"
    allowed: frozenset[str] = frozenset()

    def check(self, inv: ToolInvocation) -> None:
        if inv.tool not in self.allowed:
            raise ToolPermissionError(
                f"toolset '{self.name}': tool '{inv.tool}' not allowed "
                f"(allowed: {sorted(self.allowed)})"
            )

    def permits(self, tool: str) -> bool:
        try:
            self.check(ToolInvocation(tool=tool, args={}))
            return True
        except ToolPermissionError:
            return False


class PlannerToolset(Toolset):
    name = "planner"
    # READ only — no writes, no git, no run that mutates state.
    allowed = frozenset(
        {"read_file", "search_code", "repo_map", "read_ticket", "read_agents_md", "read_codeowners", "list_skills"}
    )


class TesterToolset(Toolset):
    """Phase 2: writes only under test paths. Phase 3 (WSC-E3-T4b): when built with
    a `work_item_id`, `demos/<work_item_id>/` becomes an ADDITIONAL allowed write
    path (the Playwright `@demo` test convention, ADR-27) — scoped to the
    session's work item; another work item's `demos/` stays blocked. The
    no-argument constructor preserves the Phase 2 behavior (no writes under
    `demos/`)."""

    name = "tester"
    _READ = frozenset({"read_file", "search_code", "repo_map", "read_ticket"})
    _EXEC = frozenset({"run_tests"})
    _WRITE = frozenset({"write_file"})
    allowed = _READ | _EXEC | _WRITE

    def __init__(self, work_item_id: str = ""):
        self.work_item_id = work_item_id

    def check(self, inv: ToolInvocation) -> None:
        if inv.tool in self._READ or inv.tool in self._EXEC:
            return
        if inv.tool in self._WRITE:
            path = inv.args.get("path", "")
            p = path.replace("\\", "/").lstrip("/")
            if p.startswith("demos/") or p == "demos":
                # EVIDENCE namespace (Phase 3): scoped per work item. The generic
                # test-path rule (e.g. `*.spec.js` anywhere) does NOT apply in
                # here — otherwise one task's Tester could write into another
                # task's demo just by naming the file `*.spec.js`.
                if is_demo_path(path, self.work_item_id):
                    return
                raise ToolPermissionError(
                    f"toolset 'tester': inside 'demos/' writing is allowed ONLY in "
                    f"'{demo_dir_for(self.work_item_id) if self.work_item_id else 'demos/<work_item_id>/ (session without a work item: none)'}'; "
                    f"'{path}' is outside that scope."
                )
            if is_test_path(path):
                return
            raise ToolPermissionError(
                f"toolset 'tester': write_file is only allowed on test paths "
                f"or in '{demo_dir_for(self.work_item_id) if self.work_item_id else 'demos/<work_item_id>/'}'; "
                f"'{path}' is neither. Production code edits belong to the Coder, not the Tester."
            )
        raise ToolPermissionError(
            f"toolset 'tester': tool '{inv.tool}' not allowed "
            f"(no git/PR — the test commit is deterministic in the Activity)."
        )


class ReviewerToolset(Toolset):
    name = "reviewer"
    # Fresh context: ONLY the plan + the diff. No repo, no Coder history, no
    # git. P3 by construction (WSC-E3-T5).
    allowed = frozenset({"read_plan", "read_diff"})
