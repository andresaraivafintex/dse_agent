#!/usr/bin/env bash
# Stop hook — impede o agente de encerrar o turno com a suíte vermelha.
#
# A guarda `stop_hook_active` é OBRIGATÓRIA. Sem ela: teste falha -> hook
# bloqueia -> agente tenta de novo -> Stop dispara -> loop infinito.
# Essa guarda nativa cobre apenas o evento Stop. Se você gatear
# TeammateIdle ou TaskCompleted, implemente a proteção de loop você mesmo.
#
# INSTALAÇÃO
#   cp gate-stop.sh .claude/hooks/ && chmod +x .claude/hooks/gate-stop.sh
#
# ==== EDITE ESTE BLOCO PARA O SEU PROJETO ====
TEST_CMD="npm test --silent"
# Exemplos:
#   Go:     TEST_CMD="go test ./..."
#   Rust:   TEST_CMD="cargo test --quiet"
#   Python: TEST_CMD="pytest -q"
# =============================================

set -uo pipefail

INPUT=$(cat)

# Guarda de loop: se já estamos num Stop bloqueado, deixe passar.
if [[ "$(printf '%s' "$INPUT" | jq -r '.stop_hook_active')" == "true" ]]; then
  exit 0
fi

if ! OUT=$(eval "$TEST_CMD" 2>&1); then
  printf '%s\n' "$OUT" | tail -40 >&2
  printf '\nA suite falhou. Corrija antes de encerrar, ou reporte BLOCKED com este output.\n' >&2
  exit 2
fi

exit 0
