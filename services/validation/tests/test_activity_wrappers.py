"""Boundary wrapper→core (achado do disparo real 2026-07-22): os testes chamam
o core direto; quando a assinatura do core muda, o wrapper da Activity fica
defasado e SÓ quebra em produção (L1 ficou em retry infinito com
"unexpected keyword argument 'base_branch'"). Este teste trava o boundary."""
from __future__ import annotations

import inspect


def test_l1_wrapper_matches_core_signature():
    from dse_validation.l1.pipeline import run_l1_pipeline_core
    sig = inspect.signature(run_l1_pipeline_core)
    # exatamente os kwargs que _run_l1_pipeline monta (activities.py) —
    # se o core mudar sem o wrapper (ou vice-versa), o bind explode AQUI.
    sig.bind(
        executor=object(),
        work_item_id="wi",
        tenant_id="t",
        plan=object(),
        base_sha="a" * 8,
        head_sha="b" * 8,
        target_dir=".",

    )
