"""model-gateway client (WS-D, `services/model-gateway/`, port 4000) for
minting the virtual key of a Coder session (WSC-E3-T1/T2).

Cross-workstream (see the task instructions): `run_coder_turn` calls
`mint_virtual_key(...)`, which WS-D is building in parallel right now. If the
real endpoint does not exist yet (or does not respond), it falls back to a
local fixture mode — CLEARLY flagged — which is enough for this workstream's
tests to run without depending on WS-D being ready at the same time. The real
integration happens in the cross-workstream integration phase.

Never call a provider SDK (OpenAI/Anthropic/Bedrock) directly from here — this
is the ONLY entry point for obtaining model credentials; the substrate (see
substrate.py) only ever receives the gateway's virtual key + base_url.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import httpx
from pydantic import BaseModel

from dse_contracts import GatewayCallHeaders

from .runtime_profile import model_gateway_fixture_allowed, validate_runtime_profile

DEFAULT_GATEWAY_URL = os.environ.get("DSE_MODEL_GATEWAY_URL", "http://localhost:4000")
# LiteLLM master key — used ONLY here, in the control plane (the
# run_coder_turn Activity runs in the orchestrator, which is trusted), to mint
# the per-task scoped virtual key via `/key/generate`. It NEVER enters the
# sandbox: the substrate only receives the short-lived virtual key + base_url
# (see substrate.py).
_MASTER_KEY = os.environ.get("DSE_LITELLM_MASTER_KEY", "sk-dse-local-dev-master-key")
# Model the virtual key is allowed to reach (minimal scope). The default is
# aligned with the alias registered for the Coder in litellm_config.yaml.
_CODER_MODEL = os.environ.get("DSE_CODER_MODEL", "anthropic/claude")


class VirtualKeyResult(BaseModel):
    virtual_key: str
    expires_at: datetime
    gateway_base_url: str
    fixture: bool = False  # True when it did NOT come from the real model-gateway


class ModelGatewayUnavailable(Exception):
    """Raised when the real gateway does not respond and fixture mode is
    disabled (`DSE_MODEL_GATEWAY_ALLOW_FIXTURE=0`) — clean failure (P6), never
    a silent downgrade to a direct provider call."""


def mint_virtual_key(
    headers: GatewayCallHeaders,
    *,
    gateway_base_url: str | None = None,
    timeout_s: float = 2.0,
    max_budget_usd: float | None = None,
) -> VirtualKeyResult:
    # In production, refuse before any I/O when the deployment still allows
    # fixture/in-process mode. The failure never turns into a local key.
    validate_runtime_profile(require_real_gateway=True)
    base = gateway_base_url or DEFAULT_GATEWAY_URL
    try:
        # REAL path (WS-D): LiteLLM's native `/key/generate` API, authenticated
        # with the master key (control plane only), scoped to the Coder model
        # and to the tenant/work_item pair in the metadata (for cost
        # attribution). The short lifetime (1h) mirrors the per-task GitHub
        # token pattern.
        with httpx.Client(timeout=timeout_s) as client:
            resp = client.post(
                f"{base}/key/generate",
                headers={"Authorization": f"Bearer {_MASTER_KEY}"},
                json={
                    "models": [_CODER_MODEL],
                    "duration": "1h",
                    # plano 08 §F (F2): HARD per-key backstop in the proxy. The
                    # fine-grained/dynamic enforcement is the pre-call hook (it
                    # reads the live control-plane budget); this cap is
                    # LiteLLM's static safety net, applied when the caller
                    # resolves a cap.
                    **({"max_budget": max_budget_usd} if max_budget_usd is not None else {}),
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
    except Exception as exc:  # noqa: BLE001 - we want to fall back to the fixture on any network/HTTP failure
        if not model_gateway_fixture_allowed():
            raise ModelGatewayUnavailable(
                f"model-gateway at {base} unavailable and fixture disabled: {exc}"
            ) from exc
        return VirtualKeyResult(
            virtual_key=f"fixture-vk-{headers.work_item_id}-{uuid.uuid4().hex[:8]}",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            gateway_base_url=base,
            fixture=True,
        )
