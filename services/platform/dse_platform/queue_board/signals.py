"""WSF-E6-T2 — envio de signals Temporal a partir do queue board.

O board não fala Temporal diretamente no corpo dos handlers; usa a interface
`SignalSender`. Duas implementações:
  - `TemporalSignalSender`: real, usa `temporalio.client.Client` para conectar
    ao cluster (`localhost:7233`) e chamar `handle.signal(name, arg)` no
    workflow cujo `workflow_id == work_item_id` (contrato da fundação:
    StartWorkflow(workflow_id = work_item_id)).
  - `FakeSignalSender`: grava as chamadas em memória — usado nos testes do
    operador (o audit é o mesmo caminho real; só o transporte do signal é
    fake, para não exigir um workflow vivo por teste). CLARAMENTE MARCADO.

Os nomes dos signals batem com os `@workflow.signal` do
`services/orchestrator/.../workflows.py` (WS-B): pause, resume, cancel,
retry_from_checkpoint, force_clarification, escalate, reassign_model,
reassign_runtime. Nomes centralizados aqui como constantes.
"""
from __future__ import annotations

import abc

SIGNAL_PAUSE = "pause"
SIGNAL_RESUME = "resume"
SIGNAL_CANCEL = "cancel"
SIGNAL_RETRY_FROM_CHECKPOINT = "retry_from_checkpoint"
SIGNAL_FORCE_CLARIFICATION = "force_clarification"
SIGNAL_ESCALATE = "escalate"
SIGNAL_REASSIGN_MODEL = "reassign_model"
SIGNAL_REASSIGN_RUNTIME = "reassign_runtime"

VALID_SIGNALS = {
    SIGNAL_PAUSE, SIGNAL_RESUME, SIGNAL_CANCEL, SIGNAL_RETRY_FROM_CHECKPOINT,
    SIGNAL_FORCE_CLARIFICATION, SIGNAL_ESCALATE, SIGNAL_REASSIGN_MODEL, SIGNAL_REASSIGN_RUNTIME,
}


class SignalSender(abc.ABC):
    @abc.abstractmethod
    def signal(self, workflow_id: str, signal_name: str, arg=None) -> None:
        ...


class TemporalSignalSender(SignalSender):
    """Real. Conecta sob demanda (uma conexão por sender, reaproveitada). Importa
    `temporalio` de forma preguiçosa para não exigir a dependência em ambientes
    que só usam o FakeSignalSender."""

    def __init__(self, target: str = "localhost:7233", namespace: str = "default"):
        self._target = target
        self._namespace = namespace
        self._client = None

    async def _client_async(self):
        from temporalio.client import Client

        if self._client is None:
            self._client = await Client.connect(self._target, namespace=self._namespace)
        return self._client

    def signal(self, workflow_id: str, signal_name: str, arg=None) -> None:
        if signal_name not in VALID_SIGNALS:
            raise ValueError(f"signal desconhecido: {signal_name!r}")
        import asyncio

        async def _do():
            client = await self._client_async()
            handle = client.get_workflow_handle(workflow_id)
            if arg is None:
                await handle.signal(signal_name)
            else:
                await handle.signal(signal_name, arg)

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop and loop.is_running():
            # chamado de dentro de um loop async (ex.: handler FastAPI async):
            # o caller deve preferir a variante async; aqui agendamos e esperamos.
            raise RuntimeError("use signal_async dentro de um event loop em execução")
        asyncio.run(_do())

    async def signal_async(self, workflow_id: str, signal_name: str, arg=None) -> None:
        if signal_name not in VALID_SIGNALS:
            raise ValueError(f"signal desconhecido: {signal_name!r}")
        client = await self._client_async()
        handle = client.get_workflow_handle(workflow_id)
        if arg is None:
            await handle.signal(signal_name)
        else:
            await handle.signal(signal_name, arg)


class FakeSignalSender(SignalSender):
    """FIXTURE de teste (marcado). Grava as chamadas de signal em `.sent` em vez
    de enviar ao Temporal — para exercitar o caminho de operador (validação +
    audit) sem exigir um workflow vivo. `raises_for` permite simular um signal
    que falha (ex.: workflow inexistente) para testar o fail-closed."""

    def __init__(self, raises_for: set[str] | None = None):
        self.sent: list[tuple[str, str, object]] = []
        self._raises_for = raises_for or set()

    def signal(self, workflow_id: str, signal_name: str, arg=None) -> None:
        if signal_name not in VALID_SIGNALS:
            raise ValueError(f"signal desconhecido: {signal_name!r}")
        if workflow_id in self._raises_for:
            raise RuntimeError(f"workflow {workflow_id} não encontrado (simulado)")
        self.sent.append((workflow_id, signal_name, arg))
