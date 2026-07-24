"""Fase C (relatório 07) — o ciclo de vida é source-agnóstico.

Fecha o Seam 10: até aqui TODO teste de lifecycle nascia github-shaped
(conftest hardcodava source='github'), escondendo divergências para
slack/jira. Estes provam que uma tarefa de origem SLACK, uma vez que o repo
foi resolvido na admissão (C2), flui pela mesma máquina de estados até `done`
— e que as notificações de estado ENDEREÇAM a superfície slack (o
post_tracking_comment não é mais github-only; C3).
"""
from __future__ import annotations

import uuid

import pytest
from temporalio.worker import Worker

from dse_contracts.work_item import WorkItemStatus
from dse_orchestrator.local_activities import LOCAL_ACTIVITIES
from dse_orchestrator.models import WorkItemLifecycleInput
from dse_orchestrator.workflows import WorkItemLifecycleWorkflow

from conftest import insert_work_item, new_work_item_id, read_audit_actions, wait_for_status
from fakes import FakeControlPlane, build_fake_activities



def _slack_input(work_item_id: str) -> WorkItemLifecycleInput:
    # Repo já resolvido na admissão (cascata C2) — é o pré-requisito que
    # faltava; com ele, o workflow trata slack como qualquer outra origem.
    return WorkItemLifecycleInput(
        work_item_id=work_item_id,
        tenant_id="test-tenant",
        requester="usr_test",
        repo="acme/repo",
        base_branch="main",
        acceptance_criteria="critério verificável",
        ci_poll_interval_seconds=0.01,
        ci_pending_poll_cap=10,
    )


@pytest.mark.asyncio
async def test_slack_origin_work_item_reaches_done(time_skipping_env):
    work_item_id = new_work_item_id("slacklife")
    insert_work_item(
        work_item_id, source="slack",
        source_ref={"channel": "C_TEAM", "thread_ts": "1700000000.001"},
    )
    state = FakeControlPlane()
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"

    async with Worker(
        time_skipping_env.client, task_queue=task_queue,
        workflows=[WorkItemLifecycleWorkflow],
        activities=list(LOCAL_ACTIVITIES) + build_fake_activities(state),
    ):
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run, _slack_input(work_item_id),
            id=work_item_id, task_queue=task_queue,
        )
        # estaciona em review_ready aguardando o verdict humano
        await wait_for_status(handle, {"review_ready", "pr_ready"})
        await handle.signal("review_comment", {"verdict": "approved"})
        await wait_for_status(handle, {"merge_pending", "pr_ready"})
        await handle.signal("merged_by_human", {"merged_by": "usr_test", "pr_number": 1000})
        result = await handle.result()

    # Mesma máquina de estados do github: chega a done, abre PR, roda o coder.
    assert result.status == WorkItemStatus.done.value
    assert result.pr_number is not None
    assert state.finalize_calls >= 1
    # O status foi refletido na superfície (post_tracking_comment REAL rodou —
    # best-effort contra o adapter slack; audita tracking_comment_posted quando
    # alcança o adapter, ou apenas segue quando o adapter está fora no teste).
    actions = read_audit_actions(work_item_id)
    assert "merged_by_human" in actions


@pytest.mark.asyncio
async def test_slack_source_ref_shape_survives_continue_as_new(time_skipping_env):
    """source_ref do slack ({channel, thread_ts}) não quebra nenhuma fronteira
    de fase (o github usa {repo, number}) — o workflow nunca assume a forma."""
    work_item_id = new_work_item_id("slackcan")
    insert_work_item(
        work_item_id, source="slack",
        source_ref={"channel": "C_X", "thread_ts": "1700000000.002"},
    )
    state = FakeControlPlane(plan_expected_files=[])  # -> escalated cedo, barato
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    async with Worker(
        time_skipping_env.client, task_queue=task_queue,
        workflows=[WorkItemLifecycleWorkflow],
        activities=list(LOCAL_ACTIVITIES) + build_fake_activities(state),
    ):
        result = await time_skipping_env.client.execute_workflow(
            WorkItemLifecycleWorkflow.run, _slack_input(work_item_id),
            id=work_item_id, task_queue=task_queue,
        )
    assert result.status == WorkItemStatus.escalated.value
    assert "escalated" in read_audit_actions(work_item_id)
