#!/usr/bin/env bash
# PostToolUse hook — devolve erro de compilação/tipo ao agente no MESMO turno.
#
# O que faz este hook funcionar é o `exit 2`: com exit 0 o stderr vai apenas
# para o log de debug e o agente nunca vê. Com exit 2 o stderr é entregue a ele
# como mensagem de erro, e ele corrige antes de seguir.
#
# INSTALAÇÃO
#   cp verify-edit.sh .claude/hooks/ && chmod +x .claude/hooks/verify-edit.sh
#
# ==== EDITE ESTE BLOCO PARA O SEU PROJETO ====
CHECK_CMD="npx --no-install tsc --noEmit --incremental"
CHECK_EXT_REGEX='\.(ts|tsx)$'
# Exemplos:
#   Go:      CHECK_CMD="go build ./..."            CHECK_EXT_REGEX='\.go$'
#   Rust:    CHECK_CMD="cargo check --quiet"       CHECK_EXT_REGEX='\.rs$'
#   Python:  CHECK_CMD="mypy ."                    CHECK_EXT_REGEX='\.py$'
# =============================================

set -uo pipefail

INPUT=$(cat)
FILE=$(printf '%s' "$INPUT" | jq -r '.tool_input.file_path // empty')

# Sem arquivo, ou arquivo de outro tipo: não é problema deste hook.
[[ -z "$FILE" ]] && exit 0
printf '%s' "$FILE" | grep -qE "$CHECK_EXT_REGEX" || exit 0

if ! OUT=$(eval "$CHECK_CMD" 2>&1); then
  # 40 linhas: erro suficiente para agir, sem inundar o contexto.
  printf '%s\n' "$OUT" | head -40 >&2
  exit 2
fi

exit 0
