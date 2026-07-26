#!/usr/bin/env python3
"""Convenience CLI for WSD-E3-T2: prints the cost aggregation by
tenant/task-class/stage from the spans recorded IN THE CURRENT PROCESS.

Important note: because the span recorder is in memory per process, running
this script as a separate process after another process already made the model
calls will show NOTHING (the buffer is empty here). This is only for
demonstration/manual testing (e.g. running inside the same process that made
the calls, or using `model_gateway_client.export_api` as an HTTP endpoint
attached to the process that actually serves the calls).

Production: swap `cost_export._iter_spans()` to read from WS-F's OTel collector
backend (see cost_export.py's docstring) — then it really works as a standalone
process/CLI, because the data source stops being one specific process's
in-memory buffer.
"""
from __future__ import annotations

import argparse
import json
import sys

from model_gateway_client.cost_export import aggregate_cost, aggregate_cost_by_tenant


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id", default=None)
    parser.add_argument("--by-tenant-only", action="store_true")
    args = parser.parse_args()

    if args.by_tenant_only:
        print(json.dumps(aggregate_cost_by_tenant(), indent=2, sort_keys=True))
    else:
        print(json.dumps(aggregate_cost(tenant_id=args.tenant_id), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
