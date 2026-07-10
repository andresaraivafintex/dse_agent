"""Entrypoint standalone do egress-proxy — usado tanto por
`docker-compose.wsc.yml` (serviço `egress-proxy`, porta 8806) quanto pelo
container "pelado" (`python:3.11-slim` + bind mount, sem pip install) usado
no teste de isolamento de rede (`tests/test_network_isolation.py`).

Depende só de stdlib + o pacote `egress_proxy` em si (que por sua vez tem
import opcional de `dse_audit` — ver `proxy.py`). Configuração 100% via env
var para poder rodar num container sem argumentos de CLI:

  DSE_EGRESS_PORT                 porta de escuta (default 8806)
  DSE_EGRESS_TENANT_ID            tenant_id para audit
  DSE_EGRESS_WORK_ITEM_ID         work_item_id para audit (opcional)
  DSE_EGRESS_ALLOW_HOSTS          lista "host[:port],host2[:port2],..." extra
                                  além do allowlist padrão derivado do WorkItem
  DSE_EGRESS_MODEL_GATEWAY_HOST   default "model-gateway"
  DSE_EGRESS_MODEL_GATEWAY_PORT   default 4000
  DSE_EGRESS_REPO_HOST            default "github.com"
"""
from __future__ import annotations

import asyncio
import logging
import os

from .allowlist import Allowlist, AllowlistEntry
from .proxy import EgressProxy


def _build_allowlist_from_env() -> Allowlist:
    allowlist = Allowlist.for_work_item(
        repo_host=os.environ.get("DSE_EGRESS_REPO_HOST", "github.com"),
        model_gateway_host=os.environ.get("DSE_EGRESS_MODEL_GATEWAY_HOST", "model-gateway"),
        model_gateway_port=int(os.environ.get("DSE_EGRESS_MODEL_GATEWAY_PORT", "4000")),
    )
    extra = os.environ.get("DSE_EGRESS_ALLOW_HOSTS", "")
    for raw in filter(None, (h.strip() for h in extra.split(","))):
        host, _, port = raw.partition(":")
        allowlist.entries.append(
            AllowlistEntry(host=host, port=int(port) if port else None, reason="extra via env", category="generic")
        )
    return allowlist


async def _amain() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    port = int(os.environ.get("DSE_EGRESS_PORT", "8806"))
    tenant_id = os.environ.get("DSE_EGRESS_TENANT_ID", "unknown")
    work_item_id = os.environ.get("DSE_EGRESS_WORK_ITEM_ID")

    proxy = EgressProxy(_build_allowlist_from_env(), tenant_id=tenant_id, work_item_id=work_item_id)
    server = await proxy.start(host="0.0.0.0", port=port)
    logging.getLogger("egress_proxy").info("egress-proxy escutando em 0.0.0.0:%s", port)
    async with server:
        await server.serve_forever()


def main() -> None:
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
