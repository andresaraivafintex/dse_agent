"""S7 (Fase 5) — boundary test do par workflow -> checkpoint/rebuild.

Os call sites destas Activities vivem no workflow (WS-B, `_checkpoint_or_rebuild`);
os modelos de input vivem aqui (WS-C). Os fakes de teste do WS-B eram lenientes
e escondiam divergencias (o workflow chamava `checkpoint_sandbox` sem `tenant_id`
e `rebuild_sandbox` sem `tenant_id`/`checkpoint_ref` -> `Failed decoding arguments`
so em runtime real). Estes testes validam o PAYLOAD LITERAL que o workflow monta
contra o modelo pydantic, para essa classe de bug falhar no CI, nao em producao.

Se o workflow mudar o shape do payload, ATUALIZE os literais aqui de proposito.
"""
from __future__ import annotations

from sandbox_runtime.activities import CheckpointSandboxInput, RebuildSandboxInput
from dse_contracts import CheckpointRef


def test_checkpoint_input_accepts_exact_workflow_payload():
    # literal de workflows.py::_checkpoint_or_rebuild (call site do checkpoint).
    payload = {
        "sandbox_id": "sbx-123",  # extra, ignorado pelo modelo
        "work_item_id": "wi-1",
        "tenant_id": "tnt-1",
        "phase": "implementing",
    }
    inp = CheckpointSandboxInput(**payload)
    assert inp.work_item_id == "wi-1"
    assert inp.tenant_id == "tnt-1"
    assert inp.phase == "implementing"


def test_rebuild_input_accepts_exact_workflow_payload():
    # o workflow passa o CheckpointRef de um checkpoint bem-sucedido (dict, pois
    # execute_activity sem result_type retorna o payload cru desserializado).
    checkpoint_ref = CheckpointRef(
        work_item_id="wi-1", git_ref="abc123", phase="implementing"
    ).model_dump()
    payload = {
        "work_item_id": "wi-1",
        "tenant_id": "tnt-1",
        "checkpoint_ref": checkpoint_ref,  # dict -> coagido a CheckpointRef
    }
    inp = RebuildSandboxInput(**payload)
    assert inp.checkpoint_ref.git_ref == "abc123"
    assert inp.tenant_id == "tnt-1"
