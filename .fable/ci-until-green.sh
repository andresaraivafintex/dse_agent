#!/bin/bash
# Insiste no CI enquanto a falha for INFRA do GitHub, com recuo.
# O incidente "Failed to resolve action download info" derruba jobs que nunca
# executaram um passo — isso nao e sinal sobre o codigo, e re-disparar e a
# resposta certa. Uma falha de TESTE aborta na hora.
set -u
RUN="${1:?uso: ci-until-green.sh <run-id> <pr>}"
PR="${2:?uso: ci-until-green.sh <run-id> <pr>}"
cd /Users/saraiva/Documents/DSE/fase1
for tent in 1 2 3 4 5 6; do
  for i in $(seq 1 80); do
    ST=$(gh run view "$RUN" --json status --jq .status 2>/dev/null)
    [ "$ST" = "completed" ] && break
    sleep 40
  done
  CONC=$(gh run view "$RUN" --json conclusion --jq .conclusion 2>/dev/null)
  echo "[$(date -u +%H:%M:%S)] tentativa $tent -> $CONC"
  [ "$CONC" = "success" ] && { echo "CI VERDE"; exit 0; }

  # Classificar CADA job vermelho lendo o log. Sem `paste`/`bc`: a versao
  # anterior deste script usou `paste -sd+` sem argumento de arquivo, o que no
  # macOS falha, zerou a contagem e me fez concluir "falha real" sem medir.
  gh api "repos/fintexinc/dse_agent/actions/runs/$RUN/jobs" > /tmp/ci-jobs.json 2>/dev/null
  REAIS=0; INFRA=0
  # Um job so fala sobre o CODIGO se EXECUTOU PASSOS. Zero passos executados
  # significa que ele morreu no setup (incidente do GitHub) ou foi cancelado na
  # fila — em nenhum dos dois casos ele viu uma linha do diff. A versao anterior
  # so procurava a string "Failed to resolve action download info" no log, e um
  # job cancelado na fila nao tem log nenhum (215 bytes), entao virava "falha
  # real" e abortava a noite por nada.
  CLASSES=$(python3 -c "
import json
d=json.load(open('/tmp/ci-jobs.json'))
for j in d['jobs']:
    if j.get('conclusion') not in ('failure','cancelled'):
        continue
    ran=sum(1 for s in j.get('steps',[]) if s.get('conclusion') not in (None,'skipped'))
    # o setup ('Set up job') conta como 1 e nao roda teste nenhum
    print(('INFRA' if ran<=1 else 'REAL')+' '+j['name'])
")
  while read -r CLASSE NOME; do
    [ -z "${CLASSE:-}" ] && continue
    echo "   $CLASSE :: $NOME"
    if [ "$CLASSE" = "INFRA" ]; then INFRA=$((INFRA+1)); else REAIS=$((REAIS+1)); fi
  done <<< "$CLASSES"
  echo "   jobs vermelhos: $INFRA de infra, $REAIS reais"
  [ "$REAIS" -gt 0 ] && { echo "FALHA REAL — nao insisto"; exit 1; }
  ESPERA=$((tent*180))
  echo "   incidente do GitHub — re-disparando em ${ESPERA}s"
  sleep "$ESPERA"
  gh run rerun "$RUN" --failed 2>&1 | tail -1
  sleep 30
done
echo "esgotado"; exit 1
