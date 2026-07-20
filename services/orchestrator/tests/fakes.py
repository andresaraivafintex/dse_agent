"""Activities FAKE que implementam a MESMA assinatura/nome/tipo de retorno
das Activities cross-workstream de `dse_contracts.activities` (donas: WS-C e
WS-E). Permitem provar a maquina de estados do workflow (WSB-E2-T3) sem
depender dos outros workstreams terem terminado suas implementacoes reais —
exatamente como pedido no enunciado da tarefa.

Postgres e Temporal em si NUNCA sao mockados aqui — so as duas fronteiras de
Activity que pertencem a outros workstreams. As Activities locais do WS-B
(`update_work_item_status`, `check_clarification_completeness`,
`emit_audit_event`) usadas pelos testes SAO as reais, batendo no Postgres
real da infra (ver `conftest.py`).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError


def _maybe_fail_closed(state: "FakeControlPlane", name: str) -> None:
    """WSB-E5-T3b — simula uma recusa de politica fail-closed do caminho de
    modelo/egress (egress-proxy down, virtual key expirada, kill switch). Levanta
    um ApplicationError NAO-retryable com um marcador que o
    `_run_model_activity` do workflow reconhece — falha limpa, sem output
    truncado (P6). Marcado como FAKE de fronteira (WS-D/WS-C reais fazem isso
    de verdade)."""
    spec = state.fail_closed_on.get(name)
    if not spec:
        return
    times = spec.get("times", 0)
    if times <= 0:
        return
    spec["times"] = times - 1
    marker = spec.get("marker", "egress_proxy_unreachable_fail_closed")
    raise ApplicationError(marker, type="EgressFailClosed", non_retryable=True)


def _maybe_transient_fail(state: "FakeControlPlane", name: str) -> None:
    """WSB-E5-T3b — simula uma oscilacao TRANSIENTE do gateway (LiteLLM cai e
    volta mid-task). Erro RETRYABLE: o proprio Temporal retenta a Activity ate
    passar — a durabilidade absorve a oscilacao sem perder progresso nem
    truncar output (P6/P8)."""
    spec = state.transient_fail_on.get(name)
    if not spec:
        return
    times = spec.get("times", 0)
    if times <= 0:
        return
    spec["times"] = times - 1
    raise ApplicationError("gateway_oscillation_transient", type="GatewayUnavailable",
                           non_retryable=False)

from dse_contracts.activities import (
    ACTIVITY_CHECKPOINT_SANDBOX,
    ACTIVITY_CONSUME_CI_STATUS,
    ACTIVITY_FINALIZE_PR,
    ACTIVITY_POST_TRACKING_COMMENT,
    ACTIVITY_PROVISION_SANDBOX,
    ACTIVITY_REBUILD_SANDBOX,
    ACTIVITY_RUN_CODER_TURN,
    ACTIVITY_RUN_L1_PIPELINE,
    ACTIVITY_RUN_L2_REVIEW,
    ACTIVITY_RUN_PLANNER_TURN,
    ACTIVITY_RUN_TESTER_TURN,
    ACTIVITY_TEARDOWN_SANDBOX,
    CheckpointRef,
    CiStatusResult,
    CoderTurnResult,
    L1Finding,
    L1Result,
    L2Verdict,
    PrRef,
    SandboxHandle,
)
from dse_contracts.plan_artifact import PlanArtifact


@dataclass
class FakeControlPlane:
    """Estado mutavel injetado num teste especifico para dirigir o
    comportamento das fakes (quantas vezes L1 falha, sequencia de status de
    CI, se checkpoint falha N vezes antes de funcionar, etc.)."""

    l1_fail_times: int = 0
    ci_sequence: list[str] = field(default_factory=lambda: ["green"])
    checkpoint_fail_times: int = 0
    provision_calls: int = 0
    coder_turn_calls: int = 0
    checkpoint_calls: int = 0
    rebuild_calls: int = 0
    teardown_calls: int = 0
    finalize_calls: int = 0
    l1_calls: int = 0
    pr_counter: int = 1000
    calls_log: list[str] = field(default_factory=list)
    # permite ao teste "travar" uma activity ate ser liberada (usado pelo chaos test
    # para simular uma Activity longa em andamento quando o worker morre)
    coder_turn_hang_event: Any = None

    # --- Fase 2: Planner / Tester / Reviewer L2 (WS-C/WS-E fronteiras) ---
    planner_calls: int = 0
    tester_calls: int = 0
    l2_calls: int = 0
    # risco declarado pelo Planner (default low -> auto-aprova o gate)
    plan_risk_class: str = "low"
    plan_expected_files: list[str] = field(default_factory=lambda: ["app.py"])
    planner_cost_usd: float = 0.0
    tester_cost_usd: float = 0.0
    l2_cost_usd: float = 0.0
    coder_cost_usd: float = 0.01
    # L2: falha N vezes (objecoes) antes de aprovar
    l2_fail_times: int = 0
    l2_objections: list[str] = field(default_factory=lambda: ["app.py:12 sem teste"])
    # captura do ULTIMO payload que a fake L2 recebeu (prova de isolamento P3)
    last_l2_payload: dict | None = None
    # simula recusa fail-closed do caminho de modelo (WSB-E5-T3b): nome da
    # activity -> {"times": N, "marker": str}. Erro NAO-retryable -> falha limpa.
    fail_closed_on: dict = field(default_factory=dict)
    # simula oscilacao TRANSIENTE do gateway (LiteLLM instavel mid-task): nome
    # da activity -> {"times": N}. Erro RETRYABLE -> Temporal retenta ate passar.
    transient_fail_on: dict = field(default_factory=dict)


def build_fake_activities(state: FakeControlPlane) -> list[Any]:
    async def run_planner_turn(payload: dict) -> PlanArtifact:
        state.planner_calls += 1
        state.calls_log.append("run_planner_turn")
        _maybe_fail_closed(state, ACTIVITY_RUN_PLANNER_TURN)
        return PlanArtifact(
            work_item_id=payload["work_item_id"],
            steps=["passo 1", "passo 2"],
            expected_files=list(state.plan_expected_files),
            test_plan="cobre o caminho feliz",
            risk_class=state.plan_risk_class,
        )

    async def run_tester_turn(payload: dict) -> CoderTurnResult:
        state.tester_calls += 1
        state.calls_log.append("run_tester_turn")
        _maybe_fail_closed(state, ACTIVITY_RUN_TESTER_TURN)
        return CoderTurnResult(
            sandbox_id=payload["sandbox_id"],
            diff_summary="fake tests",
            files_changed=["test_app.py"],
            cost_usd=state.tester_cost_usd,
        )

    async def run_l2_review(payload: dict) -> L2Verdict:
        state.l2_calls += 1
        state.calls_log.append("run_l2_review")
        state.last_l2_payload = dict(payload)
        _maybe_fail_closed(state, ACTIVITY_RUN_L2_REVIEW)
        if state.l2_fail_times > 0:
            state.l2_fail_times -= 1
            return L2Verdict(
                work_item_id=payload["work_item_id"], passed=False,
                objections=list(state.l2_objections), cost_usd=state.l2_cost_usd,
            )
        return L2Verdict(
            work_item_id=payload["work_item_id"], passed=True, objections=[],
            cost_usd=state.l2_cost_usd,
        )

    async def provision_sandbox(payload: dict) -> SandboxHandle:
        state.provision_calls += 1
        state.calls_log.append("provision_sandbox")
        _maybe_fail_closed(state, ACTIVITY_PROVISION_SANDBOX)
        return SandboxHandle(
            sandbox_id=f"sbx-{payload['work_item_id']}",
            work_item_id=payload["work_item_id"],
            tenant_id=payload["tenant_id"],
            branch=f"dse/{payload['work_item_id']}",
        )

    async def run_coder_turn(payload: dict) -> CoderTurnResult:
        state.coder_turn_calls += 1
        state.calls_log.append("run_coder_turn")
        _maybe_transient_fail(state, ACTIVITY_RUN_CODER_TURN)
        _maybe_fail_closed(state, ACTIVITY_RUN_CODER_TURN)
        if state.coder_turn_hang_event is not None:
            await state.coder_turn_hang_event.wait()
        return CoderTurnResult(
            sandbox_id=payload["sandbox_id"],
            diff_summary="fake diff",
            files_changed=["app.py"],
            cost_usd=state.coder_cost_usd,
            tokens_in=10,
            tokens_out=10,
        )

    async def checkpoint_sandbox(payload: dict) -> CheckpointRef:
        state.checkpoint_calls += 1
        state.calls_log.append("checkpoint_sandbox")
        if state.checkpoint_fail_times > 0:
            state.checkpoint_fail_times -= 1
            raise RuntimeError("simulated checkpoint failure")
        return CheckpointRef(
            work_item_id=payload["work_item_id"], git_ref="deadbeef", phase=payload["phase"]
        )

    async def rebuild_sandbox(payload: dict) -> SandboxHandle:
        state.rebuild_calls += 1
        state.calls_log.append("rebuild_sandbox")
        return SandboxHandle(
            sandbox_id=payload.get("sandbox_id") or f"sbx-{payload['work_item_id']}-rebuilt",
            work_item_id=payload["work_item_id"],
            tenant_id="t",
            branch="dse/rebuilt",
        )

    async def teardown_sandbox(payload: dict) -> None:
        state.teardown_calls += 1
        state.calls_log.append("teardown_sandbox")

    async def run_l1_pipeline(payload: dict) -> L1Result:
        state.l1_calls += 1
        state.calls_log.append("run_l1_pipeline")
        if state.l1_fail_times > 0:
            state.l1_fail_times -= 1
            return L1Result(
                work_item_id=payload["work_item_id"],
                passed=False,
                findings=[L1Finding(check="test", passed=False, detail="simulated failure")],
            )
        return L1Result(
            work_item_id=payload["work_item_id"],
            passed=True,
            findings=[L1Finding(check="test", passed=True)],
        )

    async def finalize_pr(payload: dict) -> PrRef:
        state.finalize_calls += 1
        state.calls_log.append("finalize_pr")
        existing = payload.get("existing_pr_number")
        pr_number = existing or state.pr_counter
        if not existing:
            state.pr_counter += 1
        return PrRef(
            work_item_id=payload["work_item_id"],
            pr_number=pr_number,
            url=f"https://github.com/x/y/pull/{pr_number}",
        )

    async def post_tracking_comment(payload: dict) -> None:
        state.calls_log.append("post_tracking_comment")

    async def consume_ci_status(payload: dict) -> CiStatusResult:
        state.calls_log.append("consume_ci_status")
        status = state.ci_sequence.pop(0) if state.ci_sequence else "green"
        return CiStatusResult(
            work_item_id=payload["work_item_id"], pr_number=payload["pr_number"], status=status
        )

    return [
        activity.defn(name=ACTIVITY_RUN_PLANNER_TURN)(run_planner_turn),
        activity.defn(name=ACTIVITY_RUN_TESTER_TURN)(run_tester_turn),
        activity.defn(name=ACTIVITY_RUN_L2_REVIEW)(run_l2_review),
        activity.defn(name=ACTIVITY_PROVISION_SANDBOX)(provision_sandbox),
        activity.defn(name=ACTIVITY_RUN_CODER_TURN)(run_coder_turn),
        activity.defn(name=ACTIVITY_CHECKPOINT_SANDBOX)(checkpoint_sandbox),
        activity.defn(name=ACTIVITY_REBUILD_SANDBOX)(rebuild_sandbox),
        activity.defn(name=ACTIVITY_TEARDOWN_SANDBOX)(teardown_sandbox),
        activity.defn(name=ACTIVITY_RUN_L1_PIPELINE)(run_l1_pipeline),
        activity.defn(name=ACTIVITY_FINALIZE_PR)(finalize_pr),
        activity.defn(name=ACTIVITY_POST_TRACKING_COMMENT)(post_tracking_comment),
        activity.defn(name=ACTIVITY_CONSUME_CI_STATUS)(consume_ci_status),
    ]
