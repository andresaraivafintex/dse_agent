"""WSC-E1-T3: prova que `provision_sandbox`/`checkpoint_sandbox`/
`rebuild_sandbox`/`teardown_sandbox`/`run_coder_turn` são Activities de
verdade do Temporal Python SDK — não só funções assíncronas coincidentemente
com a mesma assinatura.

Usa `temporalio.testing.ActivityEnvironment`, o harness OFICIAL do SDK para
executar uma Activity isoladamente (fora de um Workflow/Worker completo) —
não é um mock: é o mesmo código de execução de Activity do SDK real, só sem
precisar de um servidor Temporal de pé. Nota de ambiente: o container
`dse_temporal` desta sessão de desenvolvimento paralela está com o schema
dynamic config ausente na imagem (`temporalio/auto-setup:1.24`) e saiu
(`docker ps -a` mostra `Exited (1)`); isso é um problema de infra da
fundação (docker-compose.yml, fora do escopo de edição do WS-C) — não afeta
a validade deste teste, que não depende do servidor Temporal, só do SDK.

Os testes de idempotência/chaos/scoped-git já chamam essas mesmas funções
diretamente via `asyncio.run(...)` contra Docker/Postgres/git reais — este
arquivo cobre especificamente a integração com o SDK do Temporal em si
(registro via `@activity.defn(name=...)`, execução via `ActivityEnvironment`).
"""
from __future__ import annotations

import asyncio

from temporalio.testing import ActivityEnvironment

from dse_contracts import (
    ACTIVITY_CHECKPOINT_SANDBOX,
    ACTIVITY_PROVISION_SANDBOX,
    ACTIVITY_REBUILD_SANDBOX,
    ACTIVITY_RUN_CODER_TURN,
    ACTIVITY_TEARDOWN_SANDBOX,
)
from sandbox_runtime.activities import (
    ProvisionSandboxInput,
    TeardownSandboxInput,
    checkpoint_sandbox,
    provision_sandbox,
    rebuild_sandbox,
    run_coder_turn,
    teardown_sandbox,
)


def test_activity_names_match_the_contract():
    assert provision_sandbox.__temporal_activity_definition.name == ACTIVITY_PROVISION_SANDBOX
    assert checkpoint_sandbox.__temporal_activity_definition.name == ACTIVITY_CHECKPOINT_SANDBOX
    assert rebuild_sandbox.__temporal_activity_definition.name == ACTIVITY_REBUILD_SANDBOX
    assert teardown_sandbox.__temporal_activity_definition.name == ACTIVITY_TEARDOWN_SANDBOX
    assert run_coder_turn.__temporal_activity_definition.name == ACTIVITY_RUN_CODER_TURN


def test_provision_and_teardown_run_through_real_temporal_activity_environment(work_item_id, state_dir):
    """Executa a Activity através do harness real do Temporal SDK
    (`ActivityEnvironment.run`), não só como função Python solta."""
    env = ActivityEnvironment()

    handle = asyncio.run(
        env.run(provision_sandbox, ProvisionSandboxInput(work_item_id=work_item_id, tenant_id="tenant-a"))
    )
    assert handle.container_id
    assert handle.work_item_id == work_item_id

    asyncio.run(env.run(teardown_sandbox, TeardownSandboxInput(work_item_id=work_item_id, tenant_id="tenant-a")))
