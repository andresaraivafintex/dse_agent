"""Beco 1 do mapa de alcançabilidade — `(tester, spec_tester, asserção)`,
medido DUAS vezes em produção antes disto existir:

  - wi_5eecf486 (run 6): a spec própria montou o MockStore sem
    `pagination.pageSize`; TypeError no detectChanges, três rodadas idênticas,
    morte no teto. Um humano resolveria em trinta segundos.
  - wi_32eb136f (rc.45): a spec própria afirma `severity === 'warning'`, que a
    union do p-tag proíbe — satisfazer a spec quebra o build e vice-versa.
    Morto à mão a caminho do teto.

A regra NÃO é um classificador (decidir se a asserção está errada é a
indecidibilidade da porta 2). É reconhecimento de EXAUSTÃO: fingerprint de
não-convergência apontando exclusivamente para specs do próprio Tester com
veredito presente = nenhum ator autorizado pode agir (Coder revertido, porta 5
não re-autora com veredito, porta 1 exclui por posse) — então parqueia para
humano com dossiê, na primitiva da porta 1, em vez de morrer mudo. Vermelho
antes do fix.
"""
from __future__ import annotations

import uuid

import psycopg2
import pytest
from temporalio.worker import Worker

from dse_contracts.work_item import WorkItemStatus
from dse_orchestrator.local_activities import LOCAL_ACTIVITIES
from dse_orchestrator.models import WorkItemLifecycleInput
from dse_orchestrator.workflows import WorkItemLifecycleWorkflow

from conftest import DSN, insert_work_item, new_work_item_id, read_audit_actions, wait_for_status
from fakes import FakeControlPlane, build_fake_activities

_BADGE_SPEC = "src/app/components/homepage/components/report-status-badge/report-status-badge.component.spec.ts"
_DSE_SPEC = "src/app/components/homepage/components/dashboard-list/dashboard-list.component-dse.spec.ts"
_CLIENT_SPEC = "src/app/components/homepage/homepage.component.spec.ts"

#: wi_32eb136f, verbatim (abreviado): asserção com veredito, esperado vs recebido.
_WARNING_DETAIL = f"""summary: 12 errors
--- the 2 line(s) this gate counted ---
FAIL {_BADGE_SPEC}
FAIL {_DSE_SPEC}
--- raw output (tail) ---
  ● ReportStatusBadgeComponent › severity › should return "warning" severity when status is in-progress

    expect(received).toBe(expected) // Object.is equality

    Expected: "warning"
    Received: "warn"

      at src/app/components/homepage/components/report-status-badge/report-status-badge.component.spec.ts:66:34
"""

#: wi_5eecf486, verbatim (abreviado): a spec executa e morre no render.
_PAGESIZE_DETAIL = f"""summary: 403 errors
--- the 2 line(s) this gate counted ---
FAIL {_DSE_SPEC} (7.1 s)
FAIL {_DSE_SPEC} (7.1 s)
--- raw output (tail) ---
    TypeError: Cannot read properties of undefined (reading 'pageSize')

      at DashboardListComponent_Conditional_4_Template (ng:/DashboardListComponent.js:319:25)
      at executeTemplate (node_modules/@angular/core/fesm2022/core.mjs:12429:9)
"""

_MIXED_DETAIL = f"""summary: 5 errors
--- the 2 line(s) this gate counted ---
FAIL {_DSE_SPEC}
FAIL {_CLIENT_SPEC}
--- raw output (tail) ---
    expect(received).toBe(expected)
"""


def _audit_details(work_item_id: str, action: str) -> list[dict]:
    conn = psycopg2.connect(DSN)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT details FROM audit_log WHERE work_item_id = %s AND action = %s ORDER BY id",
                (work_item_id, action),
            )
            return [row[0] for row in cur.fetchall()]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# O reconhecedor puro.
# ---------------------------------------------------------------------------


def test_recognizer_accepts_both_measured_scenarios():
    from dse_orchestrator.workflows import exclusively_tester_spec_failures

    warning = exclusively_tester_spec_failures(
        ["test"], test_detail=_WARNING_DETAIL, tester_owned=[_BADGE_SPEC, _DSE_SPEC],
    )
    assert warning == [_BADGE_SPEC, _DSE_SPEC]

    pagesize = exclusively_tester_spec_failures(
        ["test"], test_detail=_PAGESIZE_DETAIL, tester_owned=[_DSE_SPEC],
    )
    assert pagesize == [_DSE_SPEC]


def test_recognizer_refuses_anything_not_exclusively_tester_owned():
    from dse_orchestrator.workflows import exclusively_tester_spec_failures

    # spec do cliente na lista FAIL -> não é o beco 1
    assert exclusively_tester_spec_failures(
        ["test"], test_detail=_MIXED_DETAIL, tester_owned=[_DSE_SPEC],
    ) == []
    # outro gate reprovando junto -> o Coder ainda tem o que fazer
    assert exclusively_tester_spec_failures(
        ["test", "build"], test_detail=_WARNING_DETAIL, tester_owned=[_BADGE_SPEC, _DSE_SPEC],
    ) == []
    # sem veredito (carga) -> território da porta 5, nunca deste parque
    zero = f"FAIL {_DSE_SPEC}\n  ● Test suite failed to run\n    Cannot find module 'x'\n"
    assert exclusively_tester_spec_failures(
        ["test"], test_detail=zero, tester_owned=[_DSE_SPEC],
    ) == []


# ---------------------------------------------------------------------------
# Control-plane: parquear, não morrer no teto.
# ---------------------------------------------------------------------------


async def _start(state: FakeControlPlane, work_item_id: str, env):
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    activities = list(LOCAL_ACTIVITIES) + build_fake_activities(state)
    worker = Worker(
        env.client, task_queue=task_queue,
        workflows=[WorkItemLifecycleWorkflow], activities=activities,
    )
    wf_input = WorkItemLifecycleInput(
        work_item_id=work_item_id, tenant_id="test-tenant", requester="usr_test",
        repo="acme/repo", base_branch="main", acceptance_criteria="crit",
    )
    handle = await env.client.start_workflow(
        WorkItemLifecycleWorkflow.run, wf_input, id=work_item_id, task_queue=task_queue)
    return worker, handle


async def _drive_to_park_and_finish(env, state, work_item_id, *, expect_expected_received):
    worker, handle = await _start(state, work_item_id, env)
    async with worker:
        await wait_for_status(handle, {"spec_conflict"})
        assert state.coder_turn_calls == 2, "parqueia na 2ª falha idêntica, sem comprar a 3ª"

        detected = _audit_details(work_item_id, "spec_conflict_detected")
        assert detected, "o dossiê é auditável"
        d = detected[0]
        assert d.get("reason") == "tester_spec_exhaustion"
        assert d["specs"], "quais specs"
        assert "expect(" in d["assertions"] or "TypeError" in d["assertions"], "quais asserções"
        if expect_expected_received:
            joined = " ".join(d.get("expected_vs_received") or [])
            assert "warning" in joined and "warn" in joined, "esperado vs recebido"
        assert d["diff_files"], "o diff do Coder"

        await handle.signal(
            "spec_conflict_resolution", {"verdict": "retry", "actor": "usr_test"},
        )
        await wait_for_status(handle, {"review_ready"})
        await handle.signal("review_comment", {"verdict": "approved"})
        await handle.signal("merged_by_human", {"merged_by": "usr_test", "pr_number": 1000})
        result = await handle.result()
    assert result.status == WorkItemStatus.done.value
    actions = read_audit_actions(work_item_id)
    assert "coder_retry_cap_exhausted" not in actions, "parquear, não morrer no teto"


@pytest.mark.asyncio
async def test_the_warning_scenario_parks_instead_of_dying(time_skipping_env):
    work_item_id = new_work_item_id("exh-warn")
    insert_work_item(work_item_id)
    state = FakeControlPlane(
        plan_risk_class="low",
        l1_fail_times=2,
        l1_fail_detail=_WARNING_DETAIL,
        coder_files_changed=["src/app/components/homepage/components/report-status-badge/report-status-badge.component.ts"],
        tester_test_files=[_BADGE_SPEC, _DSE_SPEC],
    )
    await _drive_to_park_and_finish(
        time_skipping_env, state, work_item_id, expect_expected_received=True,
    )


@pytest.mark.asyncio
async def test_the_pagesize_scenario_parks_instead_of_dying(time_skipping_env):
    work_item_id = new_work_item_id("exh-page")
    insert_work_item(work_item_id)
    state = FakeControlPlane(
        plan_risk_class="low",
        l1_fail_times=2,
        l1_fail_detail=_PAGESIZE_DETAIL,
        coder_files_changed=["src/app/components/homepage/components/dashboard-list/dashboard-list.component.ts"],
        tester_test_files=[_DSE_SPEC],
    )
    await _drive_to_park_and_finish(
        time_skipping_env, state, work_item_id, expect_expected_received=False,
    )


# ---------------------------------------------------------------------------
# O veredito `reauthor`: humano autoriza, agente executa. A spec do Tester
# vive no Pod (commit sem push até o finalize) — não existe caminho out-of-band
# para o humano corrigi-la, então `retry` religa um laço em que ninguém pode
# agir. O veredito novo carrega a ORDEM: o julgamento de "a asserção está
# errada" é do humano; a reescrita, in-place e gateada por posse, é do Tester.
# ---------------------------------------------------------------------------


async def _wait_until(cond, *, attempts: int = 120, sleep_s: float = 0.25) -> None:
    """Espelho do wait_for_status para condições fora do status (ex.: linhas de
    auditoria) — mesmo intervalo de 250ms que preserva a janela do time-skip."""
    import asyncio
    for _ in range(attempts):
        if cond():
            return
        await asyncio.sleep(sleep_s)
    raise AssertionError("condição não alcançada no teto do helper")


@pytest.mark.asyncio
async def test_retry_reparks_because_no_actor_in_the_loop_can_move(time_skipping_env):
    """A resposta executável de "o retry basta?": não. O retry religa o laço,
    o Coder não pode tocar a spec (revert determinístico), o Tester reusa o
    alvo byte-idêntico e a porta 5 não age com veredito presente — o mesmo
    vermelho volta e o item RE-PARQUEIA, uma rodada inteira mais pobre. Só a
    ordem de re-autoria sai do ciclo."""
    work_item_id = new_work_item_id("exh-retry")
    insert_work_item(work_item_id)
    state = FakeControlPlane(
        plan_risk_class="low",
        l1_fail_times=3,
        l1_fail_detail=_WARNING_DETAIL,
        coder_files_changed=["src/app/components/homepage/components/report-status-badge/report-status-badge.component.ts"],
        tester_test_files=[_BADGE_SPEC, _DSE_SPEC],
    )
    worker, handle = await _start(state, work_item_id, time_skipping_env)
    async with worker:
        await wait_for_status(handle, {"spec_conflict"})
        await handle.signal(
            "spec_conflict_resolution", {"verdict": "retry", "actor": "usr_test"},
        )
        await _wait_until(
            lambda: len(_audit_details(work_item_id, "spec_conflict_detected")) >= 2
        )
        await wait_for_status(handle, {"spec_conflict"})
        assert state.coder_turn_calls == 3, "o retry comprou exatamente uma rodada, e nada mudou"
        detected = _audit_details(work_item_id, "spec_conflict_detected")
        assert [d.get("reason") for d in detected] == ["tester_spec_exhaustion"] * 2

        await handle.signal(
            "spec_conflict_resolution", {"verdict": "reauthor", "actor": "usr_test"},
        )
        await wait_for_status(handle, {"review_ready"})
        await handle.signal("review_comment", {"verdict": "approved"})
        await handle.signal("merged_by_human", {"merged_by": "usr_test", "pr_number": 1000})
        result = await handle.result()
    assert result.status == WorkItemStatus.done.value


@pytest.mark.asyncio
async def test_reauthor_order_reaches_the_tester_and_the_warning_converges(time_skipping_env):
    """DoD: o cenário do 'warning' converge com a ordem. O veredito `reauthor`
    NÃO compra turno de Coder (não há o que codar — o código está certo); o
    turno seguinte é do Tester, com os caminhos parqueados na ordem, one-shot."""
    work_item_id = new_work_item_id("exh-reauth")
    insert_work_item(work_item_id)
    state = FakeControlPlane(
        plan_risk_class="low",
        l1_fail_times=2,
        l1_fail_detail=_WARNING_DETAIL,
        coder_files_changed=["src/app/components/homepage/components/report-status-badge/report-status-badge.component.ts"],
        tester_test_files=[_BADGE_SPEC, _DSE_SPEC],
    )
    worker, handle = await _start(state, work_item_id, time_skipping_env)
    async with worker:
        await wait_for_status(handle, {"spec_conflict"})
        coder_turns_at_park = state.coder_turn_calls
        await handle.signal(
            "spec_conflict_resolution", {"verdict": "reauthor", "actor": "usr_test"},
        )
        await wait_for_status(handle, {"review_ready"})
        assert state.coder_turn_calls == coder_turns_at_park, (
            "a rodada da ordem é do Tester; um turno de Coder aqui perseguiria "
            "a asserção que o humano acabou de julgar errada"
        )
        assert state.tester_reauthor_orders, "o Tester recebeu a ordem"
        assert state.tester_reauthor_orders[-1] == [_BADGE_SPEC, _DSE_SPEC]
        assert all(o == [] for o in state.tester_reauthor_orders[:-1]), (
            "a ordem é one-shot, não um estado que vaza para turnos futuros"
        )
        ordered = _audit_details(work_item_id, "tester_reauthor_ordered")
        assert ordered and ordered[0]["specs"] == [_BADGE_SPEC, _DSE_SPEC]
        await handle.signal("review_comment", {"verdict": "approved"})
        await handle.signal("merged_by_human", {"merged_by": "usr_test", "pr_number": 1000})
        result = await handle.result()
    assert result.status == WorkItemStatus.done.value
    actions = read_audit_actions(work_item_id)
    assert "coder_retry_cap_exhausted" not in actions


@pytest.mark.asyncio
async def test_reauthor_is_refused_on_a_client_spec_park(time_skipping_env):
    """`reauthor` só existe para o parque de exaustão de spec PRÓPRIA. Num
    parque da porta 1 (spec do CLIENTE quebrada pelo diff), a ordem não
    autoriza ninguém — o DSE nunca reescreve spec de cliente, nem por ordem —
    e o item escala para humano em vez de resumir."""
    client_subject = "src/app/components/homepage/homepage.component.ts"
    detail = (
        "summary: 5 errors\n"
        "--- the 1 line(s) this gate counted ---\n"
        f"FAIL {_CLIENT_SPEC}\n"
        "--- raw output (tail) ---\n"
        "expect(received).toBe(expected)\n"
    )
    work_item_id = new_work_item_id("exh-guard")
    insert_work_item(work_item_id)
    state = FakeControlPlane(
        plan_risk_class="low",
        # v3 da porta 1: a 1ª falha é a chance do Coder; o parque (onde este
        # guard vive) vem na reincidência.
        l1_fail_times=2,
        l1_fail_detail=detail,
        coder_files_changed=[client_subject],
        tester_test_files=[_DSE_SPEC],
    )
    worker, handle = await _start(state, work_item_id, time_skipping_env)
    async with worker:
        await wait_for_status(handle, {"spec_conflict"})
        await handle.signal(
            "spec_conflict_resolution", {"verdict": "reauthor", "actor": "usr_test"},
        )
        await wait_for_status(handle, {"escalated"})
        result = await handle.result()
    assert result.status == WorkItemStatus.escalated.value
    assert "tester_reauthor_ordered" not in read_audit_actions(work_item_id)


@pytest.mark.asyncio
async def test_a_mixed_failure_stays_in_the_normal_flow(time_skipping_env):
    """DoD 3: FAIL que inclui spec fora da posse do Tester não é o beco 1 —
    segue o laço normal (retry) e completa quando o L1 passa."""
    work_item_id = new_work_item_id("exh-mix")
    insert_work_item(work_item_id)
    state = FakeControlPlane(
        plan_risk_class="low",
        l1_fail_times=2,
        l1_fail_detail=_MIXED_DETAIL,
        coder_files_changed=["app.py"],
        tester_test_files=[_DSE_SPEC],
    )
    worker, handle = await _start(state, work_item_id, time_skipping_env)
    async with worker:
        await wait_for_status(handle, {"review_ready"})
        actions = read_audit_actions(work_item_id)
        assert "l1_failed_retrying" in actions
        assert "spec_conflict_detected" not in actions
        await handle.signal("review_comment", {"verdict": "approved"})
        await handle.signal("merged_by_human", {"merged_by": "usr_test", "pr_number": 1000})
        result = await handle.result()
    assert result.status == WorkItemStatus.done.value
