#!/usr/bin/env bash
# PostToolUse — devolve erro de sintaxe/lint/tipo ao agente no MESMO turno.
#
# O `exit 2` e a razao de existir deste hook: com exit 0 o stderr vai apenas
# para o log de debug e o agente nunca ve. Com exit 2 o stderr chega a ele como
# mensagem de erro, e ele corrige antes de seguir.
#
# Medido neste repo (2026-08-06): py_compile 0.40s + ruff 0.16s + mypy nos
# pacotes do ratchet, cache quente, 0.58s. Menos de 1s por edicao.

set -uo pipefail

REPO="/Users/saraiva/Documents/DSE/fase1"

# A unica coisa entre este repo e "make lint: command not found": nada ativa o
# venv, e ruff/mypy/pytest existem SO dentro dele. Um agente que roda `ruff` no
# shell nu recebe "command not found", conclui que o lint nao se aplica, e seg.
export PATH="$REPO/.venv/bin:$PATH"

INPUT=$(cat)
FILE=$(printf '%s' "$INPUT" | jq -r '.tool_input.file_path // empty')

[[ -z "$FILE" ]] && exit 0
[[ "$FILE" == *.py ]] || exit 0
[[ -f "$FILE" ]] || exit 0

# venv, caches e o clone de preview do cliente nao sao nosso codigo
case "$FILE" in
  */.venv*|*__pycache__*|*/preview_repo/*|*/node_modules/*) exit 0 ;;
esac

fail() { printf '%s\n' "$1" | head -40 >&2; exit 2; }

OUT=$(python -m py_compile "$FILE" 2>&1) || fail "$OUT"
OUT=$(ruff check "$FILE" 2>&1)          || fail "$OUT"

# mypy so nos tres pacotes que o ratchet do Makefile ja gateia. Em
# services/orchestrator o baseline e de 426 erros (Makefile: lint), entao rodar
# mypy la devolveria divida herdada como se fosse erro desta edicao — e um gate
# que grita por coisa que nao e sua deixa de ser lido.
case "$FILE" in
  "$REPO"/packages/contracts/dse_contracts/*|\
  "$REPO"/packages/dse_audit/dse_audit/*|\
  "$REPO"/packages/dse_identity/dse_identity/*)
    OUT=$(cd "$REPO" && mypy "$FILE" --ignore-missing-imports 2>&1) || fail "$OUT"
    ;;
esac

exit 0
