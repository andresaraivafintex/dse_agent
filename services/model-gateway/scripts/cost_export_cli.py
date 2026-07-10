#!/usr/bin/env python3
"""CLI de conveniência para WSD-E3-T2: imprime a agregação de custo por
tenant/task-class/stage dos spans registrados NO PROCESSO ATUAL.

Nota importante: como o recorder de spans é em memória por processo, rodar
este script como um processo separado depois que outro processo já fez as
chamadas de modelo NÃO mostrará nada (o buffer está vazio aqui). Isto é
só para demonstração/teste manual (ex.: rodar dentro do mesmo processo que
fez as chamadas, ou usar via `model_gateway_client.export_api` como um
endpoint HTTP acoplado ao processo que efetivamente serve as chamadas).

Produção: troca `cost_export._iter_spans()` para ler do backend do OTel
collector do WS-F (ver docstring de cost_export.py) — aí sim funciona como
processo/CLI standalone de verdade, porque a fonte de dados deixa de ser
o buffer em memória de um processo específico.
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
