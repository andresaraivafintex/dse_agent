"""Standalone entrypoint for the egress-proxy — used both by
`docker-compose.wsc.yml` (service `egress-proxy`, port 8806) and by the "bare"
container (`python:3.11-slim` + bind mount, no pip install) used in the network
isolation test (`tests/test_network_isolation.py`).

Depends only on stdlib + the `egress_proxy` package itself (which in turn has an
optional import of `dse_audit` — see `proxy.py`). Configuration is 100% via env
vars so it can run in a container with no CLI arguments:

  DSE_EGRESS_PORT                 listen port (default 8806)
  DSE_EGRESS_TENANT_ID            tenant_id for audit
  DSE_EGRESS_WORK_ITEM_ID         work_item_id for audit (optional)
  DSE_EGRESS_ALLOW_HOSTS          extra "host[:port],host2[:port2],..." list on
                                  top of the default WorkItem-derived allowlist
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
    logging.getLogger("egress_proxy").info("egress-proxy listening on 0.0.0.0:%s", port)
    async with server:
        await server.serve_forever()


def main() -> None:
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
