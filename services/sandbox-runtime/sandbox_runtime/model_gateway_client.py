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
        with httpx.Client(timeout=timeout_s) as client:
            resp = client.post(
                f"{base}/internal/virtual-keys",
                json=headers.model_dump(mode="json"),
                headers=headers.to_http_headers(),
            )
            resp.raise_for_status()
            data = resp.json()
        return VirtualKeyResult(
            virtual_key=data["virtual_key"],
            expires_at=datetime.fromisoformat(data["expires_at"]),
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
