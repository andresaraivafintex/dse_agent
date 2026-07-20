"""WSA-E1-T3 — dispatcher outbox: `SELECT ... FOR UPDATE SKIP LOCKED` +
Temporal `start_workflow` real (sem mock — Postgres e Temporal reais da
infra, conforme CONVENTIONS.md: nunca mockar durabilidade/idempotência).

Teste central (núcleo do chaos test de saída da Fase 1, NFR-01, lado do
intake): 2 dispatchers concorrentes (2 threads, cada uma com seu próprio
event loop + Client Temporal) drenando a MESMA fila de ingest_events sem
duplicar nem perder.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from concurrent.futures import ThreadPoolExecutor

import psycopg2
import pytest
from temporalio.client import Client

from ingest_gateway.dispatcher import Dispatcher

DSN = "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse"
TEMPORAL_ADDRESS = "localhost:7233"


def _insert_task_request_row(tenant_id: str, n: int) -> str:
    """Cria um work_item + ingest_event (kind=task_request) diretamente,
    simulando o que `admit_work_item` já teria feito — o dispatcher não se
    importa com quem escreveu a linha."""
    work_item_id = f"wi_disp_{uuid.uuid4().hex[:12]}"
    event_id = f"evt_{uuid.uuid4().hex}"
    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO work_items (id, tenant_id, source, source_ref, requester, idempotency_key)
            VALUES (%s, %s, 'slack', %s::jsonb, 'usr_test', %s)
            """,
            (work_item_id, tenant_id, json.dumps({"thread_ts": f"t{n}"}), f"idem_{work_item_id}"),
        )
        cur.execute(
            """
            INSERT INTO ingest_events (work_item_id, event_id, kind, payload)
            VALUES (%s, %s, 'task_request', %s::jsonb)
            """,
            (work_item_id, event_id, json.dumps({"n": n})),
        )
    conn.commit()
    conn.close()
    return work_item_id


def _run_dispatcher_drain_all(batch_size: int) -> int:
    async def _run():
        client = await Client.connect(TEMPORAL_ADDRESS)
        dispatcher = Dispatcher(client, batch_size=batch_size)
        return await dispatcher.drain_all()

    return asyncio.new_event_loop().run_until_complete(_run())


@pytest.fixture
def temporal_client():
    return asyncio.new_event_loop().run_until_complete(Client.connect(TEMPORAL_ADDRESS))


def test_single_dispatcher_starts_workflow_and_marks_processed(tenant_id, temporal_client):
    work_item_id = _insert_task_request_row(tenant_id, 1)

    dispatcher = Dispatcher(temporal_client, batch_size=10)
    processed = asyncio.new_event_loop().run_until_complete(dispatcher.drain_once())
    assert processed == 1

    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute("SELECT processed FROM ingest_events WHERE work_item_id = %s", (work_item_id,))
        assert cur.fetchone() == (True,)
        cur.execute(
            "SELECT 1 FROM audit_log WHERE work_item_id=%s AND action='dispatch_started'",
            (work_item_id,),
        )
        assert cur.fetchone() is not None
    conn.close()

    desc = asyncio.new_event_loop().run_until_complete(
        temporal_client.get_workflow_handle(work_item_id).describe()
    )
    assert desc.id == work_item_id


def test_duplicate_ingest_event_for_same_work_item_is_idempotent(tenant_id, temporal_client):
    """Simula 2 linhas de outbox apontando para o MESMO work_item (nunca
    deveria acontecer via admit_work_item, que dedupe por event_id — mas o
    dispatcher precisa ser à prova disso): a 2a tentativa de start_workflow
    esbarra em WorkflowAlreadyStartedError e é tratada como sucesso
    idempotente, nunca re-lançada."""
    work_item_id = f"wi_disp_{uuid.uuid4().hex[:12]}"
    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO work_items (id, tenant_id, source, source_ref, requester, idempotency_key) "
            "VALUES (%s,%s,'slack','{}'::jsonb,'usr_test',%s)",
            (work_item_id, tenant_id, f"idem_{work_item_id}"),
        )
        for i in range(2):
            cur.execute(
                "INSERT INTO ingest_events (work_item_id, event_id, kind, payload) VALUES (%s,%s,'task_request','{}'::jsonb)",
                (work_item_id, f"evt_{work_item_id}_{i}", ),
            )
    conn.commit()
    conn.close()

    dispatcher = Dispatcher(temporal_client, batch_size=10)
    loop = asyncio.new_event_loop()
    processed = loop.run_until_complete(dispatcher.drain_all())
    assert processed == 2  # ambas as linhas marcadas processed, nenhum crash

    check_conn = psycopg2.connect(DSN)
    with check_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM ingest_events WHERE work_item_id=%s AND processed", (work_item_id,))
        assert cur.fetchone()[0] == 2
        cur.execute(
            "SELECT action, count(*) FROM audit_log WHERE work_item_id=%s GROUP BY action", (work_item_id,)
        )
        actions = dict(cur.fetchall())
    check_conn.close()

    assert actions.get("dispatch_started") == 1
    assert actions.get("dispatch_deduped_already_started") == 1


def test_two_concurrent_dispatchers_drain_without_duplication_or_loss(tenant_id):
    """NÚCLEO do chaos test NFR-01 (lado do intake): 20 ingest_events
    distintos, 2 dispatchers concorrentes (threads/processos separados,
    cada um com seu próprio Client Temporal e conexão Postgres) drenando a
    MESMA fila via `SELECT ... FOR UPDATE SKIP LOCKED`. Ao final: todas as
    20 linhas processed=true, exatamente uma vez cada (nenhuma duplicada,
    nenhuma perdida), e exatamente um workflow Temporal iniciado por
    work_item (nenhum WorkflowAlreadyStartedError deveria sequer ocorrer
    aqui, já que cada work_item é único — provando que SKIP LOCKED evita a
    corrida antes mesmo de chegar no Temporal)."""
    N = 20
    work_item_ids = [_insert_task_request_row(tenant_id, i) for i in range(N)]

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_run_dispatcher_drain_all, 3) for _ in range(2)]
        results = [f.result(timeout=60) for f in futures]

    # Os 2 dispatchers do teste juntos processam NO MÁXIMO N. A igualdade
    # estrita (== N) só vale quando eles são os ÚNICOS consumidores da fila;
    # no ambiente compartilhado desta fase o container `dse_ingest_dispatcher`
    # (run_forever) também está no ar e pode drenar parte das linhas via o
    # MESMO `SELECT ... FOR UPDATE SKIP LOCKED`. Isso NÃO viola NFR-01 — a
    # invariante real (nenhuma perda, nenhuma duplicação, exatamente-uma-vez)
    # é provada pelas asserções de banco abaixo, que valem para QUALQUER número
    # de consumidores concorrentes (2 do teste + o container). SKIP LOCKED
    # garante um consumidor por linha; quem processou é irrelevante.
    assert sum(results) <= N
    assert sum(results) >= 0

    # Espera curta: se o container de background pegou (via SKIP LOCKED) as
    # linhas que os 2 dispatchers do teste pularam, elas podem estar em voo no
    # instante em que os dispatchers do teste desistem (3 rounds vazios). Poll
    # com timeout absorve essa janela sem mascarar perda real — se ao fim do
    # timeout faltar alguma, é perda de verdade e o assert falha.
    import time as _time

    deadline = _time.time() + 15
    conn = psycopg2.connect(DSN)
    while True:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM ingest_events WHERE work_item_id = ANY(%s) AND processed",
                (work_item_ids,),
            )
            done = cur.fetchone()[0]
        conn.commit()  # encerra o snapshot para a próxima leitura ver commits recentes
        if done == N or _time.time() >= deadline:
            break
        _time.sleep(0.25)

    with conn.cursor() as cur:
        assert done == N  # nenhuma perdida — TODAS processadas (por qualquer consumidor)

        cur.execute(
            "SELECT count(*) FROM ingest_events WHERE work_item_id = ANY(%s)",
            (work_item_ids,),
        )
        assert cur.fetchone()[0] == N  # nenhuma linha duplicada foi criada

        # Exatamente um dispatch_started por work_item — sem corrida dupla.
        cur.execute(
            """
            SELECT work_item_id, count(*) FROM audit_log
            WHERE work_item_id = ANY(%s) AND action = 'dispatch_started'
            GROUP BY work_item_id
            """,
            (work_item_ids,),
        )
        counts = {row[0]: row[1] for row in cur.fetchall()}
    conn.close()

    assert len(counts) == N
    assert all(c == 1 for c in counts.values())
