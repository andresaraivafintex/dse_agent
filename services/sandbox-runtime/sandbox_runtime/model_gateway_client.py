"""Cliente do model-gateway (WS-D, `services/model-gateway/`, porta 4000)
para mintar a virtual key de uma sessão Coder (WSC-E3-T1/T2).

Cross-workstream (ver instruções do task): `run_coder_turn` chama
`mint_virtual_key(...)`, que o WS-D está construindo em paralelo agora. Se o
endpoint real ainda não existir (ou não responder), cai para um modo fixture
local — CLARAMENTE marcado — que basta para os testes deste workstream
rodarem sem depender do WS-D estar pronto ao mesmo tempo. A integração real
acontece na fase de integração entre workstreams.

Nunca chame um SDK de provider (OpenAI/Anthropic/Bedrock) diretamente daqui
— esta é a ÚNICA porta de entrada para conseguir credenciais de modelo; o
substrato (ver substrate.py) só recebe a virtual key + base_url do gateway.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import httpx
from pydantic import BaseModel

from dse_contracts import GatewayCallHeaders

DEFAULT_GATEWAY_URL = os.environ.get("DSE_MODEL_GATEWAY_URL", "http://localhost:4000")
_FIXTURE_ENV_VAR = "DSE_MODEL_GATEWAY_ALLOW_FIXTURE"
# Master key do LiteLLM — usada SÓ aqui, no control plane (a Activity
# run_coder_turn roda no orchestrator, confiável), para mintar a virtual key
# escopada por tarefa via `/key/generate`. NUNCA entra no sandbox: o substrato
# recebe apenas a virtual key de curta duração + base_url (ver substrate.py).
_MASTER_KEY = os.environ.get("DSE_LITELLM_MASTER_KEY", "sk-dse-local-dev-master-key")
# Modelo que a virtual key pode acessar (escopo mínimo). Default alinhado ao
# alias registrado no litellm_config.yaml para o Coder.
_CODER_MODEL = os.environ.get("DSE_CODER_MODEL", "anthropic/claude")


class VirtualKeyResult(BaseModel):
    virtual_key: str
    expires_at: datetime
    gateway_base_url: str
    fixture: bool = False  # True quando NÃO veio do model-gateway real


class ModelGatewayUnavailable(Exception):
    """Levantado quando o gateway real não responde e o modo fixture está
    desabilitado (`DSE_MODEL_GATEWAY_ALLOW_FIXTURE=0`) — falha limpa (P6),
    nunca degrada silenciosamente para uma chamada direta a um provider."""


def mint_virtual_key(
    headers: GatewayCallHeaders,
    *,
    gateway_base_url: str | None = None,
    timeout_s: float = 2.0,
) -> VirtualKeyResult:
    base = gateway_base_url or DEFAULT_GATEWAY_URL
    try:
        # Via REAL (WS-D): a API nativa do LiteLLM `/key/generate`, autenticada
        # com a master key (só no control plane), escopada ao modelo do Coder
        # e ao par tenant/work_item nos metadados (para atribuição de custo).
        # Duração curta (1h) espelha o padrão dos tokens GitHub por tarefa.
        with httpx.Client(timeout=timeout_s) as client:
            resp = client.post(
                f"{base}/key/generate",
                headers={"Authorization": f"Bearer {_MASTER_KEY}"},
                json={
                    "models": [_CODER_MODEL],
                    "duration": "1h",
                    "metadata": {
                        "tenant_id": headers.tenant_id,
                        "work_item_id": headers.work_item_id,
                        "stage": headers.stage.value if hasattr(headers.stage, "value") else headers.stage,
                    },
                },
            )
            resp.raise_for_status()
            data = resp.json()
        return VirtualKeyResult(
            virtual_key=data["key"],
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            gateway_base_url=base,
            fixture=False,
        )
    except Exception as exc:  # noqa: BLE001 - queremos cair pro fixture por qualquer falha de rede/HTTP
        if os.environ.get(_FIXTURE_ENV_VAR, "1") != "1":
            raise ModelGatewayUnavailable(
                f"model-gateway em {base} indisponível e fixture desabilitado: {exc}"
            ) from exc
        return VirtualKeyResult(
            virtual_key=f"fixture-vk-{headers.work_item_id}-{uuid.uuid4().hex[:8]}",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            gateway_base_url=base,
            fixture=True,
        )
