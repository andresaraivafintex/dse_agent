"""H3 — the Coder's skills note under the K8s runtime.

`workspace_skills_note(workspace_dir)` reads the WORKER's filesystem, but on
the K8s runtime the workspace lives in the Pod volume
(`workspace_is_host_visible=False`): the host path does not exist, `ws.is_dir()`
is False and every production Coder turn shipped with an EMPTY note — while the
skills sat materialized in the Pod and the Tester, whose note is built by a
command run INSIDE the Pod, kept listing them. Writing a skill "for the Coder"
was writing into the void, and the symptom (the same error coming back) was
indistinguishable from "the model ignored the rule".

This pins the production shape without a cluster: skills exist ONLY in the
simulated Pod volume, and the instruction that crosses to the runner must list
every one of them.
"""
from __future__ import annotations

import asyncio
import subprocess
from typing import Any

from dse_contracts import (
    AgentTurnResult,
    CheckpointOpResult,
    PostTurnResult,
    RunCoderTurnInput,
)

from sandbox_runtime.activities import _run_coder_turn_impl
from sandbox_runtime.driver import StageExecutionResult
from sandbox_runtime.remote_substrate import RemoteSubstrate

# The three real skills written on 2026-08-06 — the PrimeNG one was authored
# specifically for the Coder and reached nobody.
THE_SKILLS = {
    "providemockstore": "Specs that inject Store must use provideMockStore",
    "primeng-table-typing": "Type PrimeNG table templates via $implicit",
    "setinput-signals": "Set signal inputs with fixture.componentRef.setInput",
}


class SkillsAwarePodDriver:
    """K8s-shaped driver: the workspace is a tmp dir the worker never touches
    with its own filesystem — the only way to see it is `run_in_pod`, which
    really executes the script against the 'Pod volume' (path swapped in, the
    same effect kubectl exec gets by running where /workspace is real)."""

    def __init__(self, pod_workspace: str):
        self.pod_workspace = pod_workspace
        self.turn_instructions: list[str] = []

    @property
    def supports_isolated_stage_execution(self) -> bool:
        return True

    @property
    def workspace_is_host_visible(self) -> bool:
        return False

    def sandbox_id_for(self, work_item_id: str) -> str:
        return f"pod-{work_item_id}"

    def execute_op(
        self, sandbox_id: str, op: str, payload: dict[str, Any], *, timeout_seconds: float = 180.0
    ) -> dict[str, Any]:
        if op == "checkpoint":
            return CheckpointOpResult(sha="a" * 40, phase=str(payload.get("phase", ""))).model_dump()
        if op == "post_turn":
            return PostTurnResult(sha="b" * 40, files_changed=["src/x.ts"]).model_dump()
        raise AssertionError(f"unexpected op: {op}")

    def execute_stage(self, request):
        self.turn_instructions.append(str(request.input_payload.get("instruction", "")))
        return StageExecutionResult(
            stage=request.stage,
            output_payload=AgentTurnResult(done=True).model_dump(),
            exit_code=0,
            duration_seconds=0.01,
        )

    def run_in_pod(
        self, sandbox_id: str, argv: list[str], input_text: str | None = None, *, timeout: int = 120
    ) -> tuple[int, str]:
        script = argv[-1].replace("/workspace", self.pod_workspace)
        proc = subprocess.run(
            [*argv[:-1], script], input=input_text,
            capture_output=True, text=True, timeout=timeout,
        )
        return proc.returncode, proc.stdout or ""


def test_coder_instruction_lists_the_skills_that_live_in_the_pod(tmp_path, work_item_id, state_dir):
    """Production shape: the skills are in the POD workspace (materialized at
    provision time / committed in the target repo) and the worker path for this
    work item does not exist. Reading the note on the worker's filesystem is
    exactly the H3 bug — `""` on every production Coder turn."""
    pod_ws = tmp_path / "pod-volume" / "workspace"
    for key, description in THE_SKILLS.items():
        d = pod_ws / ".claude" / "skills" / key
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            f"---\nname: {key}\ndescription: {description}\n---\n\nrule body\n",
            encoding="utf-8",
        )

    driver = SkillsAwarePodDriver(str(pod_ws))
    remote = RemoteSubstrate(driver=driver, substrate_name="fake")

    asyncio.run(
        _run_coder_turn_impl(
            RunCoderTurnInput(
                work_item_id=work_item_id, tenant_id="tenant-a", instruction="implement it",
            ),
            substrate=remote,
        )
    )

    assert driver.turn_instructions, "the turn never reached the runner"
    instruction = driver.turn_instructions[0]
    assert "## Repository skills (MANDATORY guidance)" in instruction, (
        "the Coder's instruction carries no skills note although the Pod "
        "workspace has three skills — the note was read on the WORKER's "
        "filesystem, where the workspace does not exist (H3)"
    )
    for key, description in THE_SKILLS.items():
        assert f".claude/skills/{key}/SKILL.md" in instruction
        assert description in instruction
