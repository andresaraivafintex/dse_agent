"""WSE-E2 — camada L2 (Reviewer de contexto fresco) do WS-E.

A SESSÃO Reviewer em si (a chamada de modelo com contexto fresco) é construída
pelo WS-C (WSC-E3-T5) e registrada como a Activity `ACTIVITY_RUN_L2_REVIEW`.
Este pacote é a ORQUESTRAÇÃO que o WS-E é dono (adendo Fase 2, WSE-E2-T4/T5):

  - `session`   — Protocol da sessão L2 + fake determinístico + resolução
                  defensiva da implementação do WS-C (que roda em paralelo).
  - `l2_review` — orquestra 1 turno L2 (cheapest-first: só depois do L1 verde,
                  antes do CI — P5), registra veredito + custo (P8).
  - `fix_loop`  — lógica determinística do loop bounded L2->Coder (P1/P6):
                  quando reenviar ao Coder, quando escalar a operador.
"""
