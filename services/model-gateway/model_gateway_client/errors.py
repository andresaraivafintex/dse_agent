"""Exceções do model_gateway_client. P6 decline-never-truncate: qualquer
falha (chamada admin do LiteLLM, chamada de modelo, lookup de virtual key)
propaga como exceção clara em fronteira — nunca é engolida ou "consertada"
silenciosamente."""
from __future__ import annotations

from typing import Any


class ModelGatewayError(Exception):
    """Erro genérico ao falar com o model-gateway (admin API ou chamada de modelo)."""


class VirtualKeyNotFoundError(ModelGatewayError):
    """`revoke_virtual_key` chamado com uma key que este processo/tabela
    `virtual_keys` não reconhece — não adivinhamos, falhamos limpo."""


class GatewayCallError(ModelGatewayError):
    """O gateway respondeu com erro (policy denial / budget exhausted /
    model unavailable — `dse_contracts.gateway_contract.GatewayErrorResponse`
    — ou qualquer HTTP não-2xx). Contém o corpo original para quem for tratar
    o erro decidir o que fazer (nunca decidido por um LLM — P1)."""

    def __init__(self, status_code: int, body: dict[str, Any]):
        self.status_code = status_code
        self.body = body
        super().__init__(f"model-gateway call failed: HTTP {status_code}: {body}")
