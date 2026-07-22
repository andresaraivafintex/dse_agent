#!/usr/bin/env bash
# =============================================================================
#  Fintex DSE — start/stop/status de TODO o sistema local
#
#  Uso:
#    ./dse.sh start            sobe tudo (infra, migrations, serviços, cluster
#                              de preview, painel) — idempotente, pode re-rodar
#    ./dse.sh start --no-preview   pula o cluster k3d (previews desativados)
#    ./dse.sh start --no-console   pula o painel (API+UI)
#    ./dse.sh stop             para o painel + serviços (estado preservado)
#    ./dse.sh stop --all       idem + derruba containers (compose down) e o k3d
#    ./dse.sh status           saúde de cada peça
#    ./dse.sh logs [svc]       logs (compose) ou do painel (console-api|console-ui)
#    ./dse.sh secrets          cria/mostra o template do secrets.env
#
#  Secrets: o arquivo ./secrets.env (gitignored, chmod 600) é a fonte local.
#  Sem ele o sistema sobe em "modo local" (echo model, sem GitHub/Slack/Jira
#  reais) — o start avisa exatamente o que fica de fora. O Vault dev é
#  re-semeado a partir dele a cada start (dev-mode perde tudo em restart).
# =============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
CONSOLE_DIR="${DSE_CONSOLE_DIR:-$ROOT/../../GitHub/Fintex/dse_console_pane}"
CONSOLE_DIR="$(cd "$CONSOLE_DIR" 2>/dev/null && pwd || echo "$CONSOLE_DIR")"
CONSOLE_API_PORT="${DSE_CONSOLE_API_PORT:-4100}"
CONSOLE_UI_PORT="${DSE_CONSOLE_UI_PORT:-3000}"
SECRETS_FILE="$ROOT/secrets.env"
LOGDIR="$CONSOLE_DIR/.logs"

COMPOSE_FILES=$(ls docker-compose.yml docker-compose.ws*.yml 2>/dev/null)
COMPOSE_ARGS=""; for f in $COMPOSE_FILES; do COMPOSE_ARGS="$COMPOSE_ARGS -f $f"; done

G="\033[32m✓\033[0m"; R="\033[31m✗\033[0m"; Y="\033[33m•\033[0m"
say()  { printf "%b %s\n" "$1" "$2"; }
step() { printf "\n\033[1m==> %s\033[0m\n" "$1"; }

PY="python3"; [ -x "$ROOT/.venv/bin/python" ] && PY="$ROOT/.venv/bin/python"

# ----------------------------------------------------------------- secrets --
secrets_template() {
  cat > "$SECRETS_FILE" <<'EOF'
# Fintex DSE — secrets locais (NUNCA commitar; chmod 600).
# Preencha o que for usar; o que ficar vazio só desativa aquela integração.

# --- GitHub (fluxo real: issue -> PR -> merge) ---
GITHUB_APP_ID=
GITHUB_APP_INSTALLATION_ID=
# a chave privada .pem em UMA linha: awk 'BEGIN{ORS="\\n"}1' app.pem
GITHUB_APP_PRIVATE_KEY=
GITHUB_WEBHOOK_SECRET=

# --- Modelo real (Coder/Planner). Vazio = só o echo model local ---
ANTHROPIC_API_KEY=

# --- Slack (opcional) ---
SLACK_BOT_TOKEN=
SLACK_SIGNING_SECRET=

# --- Jira (opcional) ---
JIRA_API_TOKEN=
JIRA_WEBHOOK_SECRET=

# --- Teams (opcional) ---
TEAMS_APP_ID=
TEAMS_APP_PASSWORD=
TEAMS_OUTGOING_SECRET=
EOF
  chmod 600 "$SECRETS_FILE"
}

load_secrets() {
  SECRETS_LOADED=no
  if [ -f "$SECRETS_FILE" ]; then
    set -a; # shellcheck disable=SC1090
    source "$SECRETS_FILE"; set +a
    SECRETS_LOADED=yes
  fi
}

seed_vault() {
  # Vault dev é in-memory: re-semeia a cada start (fonte: secrets.env).
  docker exec dse_vault sh -c 'exit 0' 2>/dev/null || return 0
  local v="docker exec -e VAULT_ADDR=http://127.0.0.1:8200 -e VAULT_TOKEN=${VAULT_DEV_ROOT_TOKEN:-dse_dev_root} dse_vault vault"
  if [ -n "${GITHUB_APP_ID:-}" ]; then
    $v kv put secret/dse/github-app app_id="$GITHUB_APP_ID" \
      installation_id="${GITHUB_APP_INSTALLATION_ID:-}" \
      private_key="${GITHUB_APP_PRIVATE_KEY:-}" \
      webhook_secret="${GITHUB_WEBHOOK_SECRET:-}" >/dev/null && say "$G" "Vault: secret/dse/github-app semeado"
  fi
  if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
    $v kv put secret/dse/model-gateway/providers anthropic_api_key="$ANTHROPIC_API_KEY" >/dev/null \
      && say "$G" "Vault: secret/dse/model-gateway/providers semeado"
  fi
}

# ------------------------------------------------------------------- start --
cmd_start() {
  local WITH_PREVIEW=yes WITH_CONSOLE=yes
  for a in "$@"; do
    [ "$a" = "--no-preview" ] && WITH_PREVIEW=no
    [ "$a" = "--no-console" ] && WITH_CONSOLE=no
  done

  step "0/8 pré-requisitos"
  docker info >/dev/null 2>&1 || { say "$R" "Docker não está rodando — abra o Docker Desktop e re-rode."; exit 1; }
  say "$G" "docker ok"
  command -v k3d >/dev/null || { [ "$WITH_PREVIEW" = yes ] && { say "$Y" "k3d ausente — previews desativados"; WITH_PREVIEW=no; }; }
  command -v kubectl >/dev/null || { [ "$WITH_PREVIEW" = yes ] && { say "$Y" "kubectl ausente — previews desativados"; WITH_PREVIEW=no; }; }

  step "1/8 secrets"
  load_secrets
  if [ "$SECRETS_LOADED" = yes ]; then
    say "$G" "secrets.env carregado"
    [ -z "${GITHUB_APP_ID:-}" ]     && say "$Y" "GITHUB_APP_* vazio — PRs/comentários reais desativados"
    [ -z "${ANTHROPIC_API_KEY:-}" ] && say "$Y" "ANTHROPIC_API_KEY vazia — só o echo model (sem Coder real)"
  else
    secrets_template
    say "$Y" "secrets.env não existia — TEMPLATE criado em $SECRETS_FILE"
    say "$Y" "preencha-o e re-rode ./dse.sh start para o fluxo GitHub/modelo real"
  fi

  step "2/8 infra base (postgres, temporal, vault, redis)"
  docker network inspect dse_net >/dev/null 2>&1 || docker network create dse_net >/dev/null
  # shellcheck disable=SC2086
  docker compose $COMPOSE_ARGS up -d --quiet-pull 2>&1 | grep -vE "Running|Started|Created" || true
  until docker exec dse_postgres pg_isready -U dse -d dse >/dev/null 2>&1; do sleep 1; done
  say "$G" "postgres pronto"
  until curl -sf http://localhost:8200/v1/sys/health >/dev/null 2>&1; do sleep 1; done
  say "$G" "vault pronto"
  seed_vault

  step "3/8 migrations"
  if $PY -c "import psycopg2" 2>/dev/null; then
    $PY scripts/migrate.py | tail -1
  else
    say "$Y" "psycopg2 indisponível no host — aplicando via psql do container"
    for f in migrations/*.sql; do docker exec -i dse_postgres psql -U dse -d dse -q -f - < "$f" >/dev/null 2>&1 || true; done
    say "$G" "migrations aplicadas (idempotentes)"
  fi

  step "4/8 imagens auxiliares"
  docker image inspect dse-sandbox-base:wsc3 >/dev/null 2>&1 \
    || { say "$Y" "buildando dse-sandbox-base:wsc3…"; docker build -q -t dse-sandbox-base:wsc3 -f services/sandbox-runtime/docker/Dockerfile.sandbox-base services/sandbox-runtime >/dev/null; }
  say "$G" "sandbox base image ok"

  step "5/8 cluster de preview (k3d + Argo CD + Traefik + registry)"
  if [ "$WITH_PREVIEW" = yes ]; then
    if ! k3d cluster list 2>/dev/null | grep -q "^dse-preview\b"; then
      bash infra/k8s-local/setup-k3d-argocd.sh
    else
      # restart do Docker muda a porta do API server — reconcilia sempre
      k3d kubeconfig merge dse-preview --kubeconfig-merge-default --kubeconfig-switch-context >/dev/null 2>&1 || true
      say "$G" "cluster dse-preview já existe"
    fi
    bash infra/k8s-local/gen-kubeconfig-internal.sh >/dev/null
    say "$G" "kubeconfig interno gerado (worker → cluster)"
  else
    say "$Y" "previews pulados (--no-preview / k3d ausente)"
  fi

  step "6/8 serviços (adapters, orchestrator, gateway, projector…)"
  # shellcheck disable=SC2086
  { docker compose $COMPOSE_ARGS up -d 2>&1 | grep -cE "Started|Recreated" || true; } | xargs -I{} echo "   {} containers (re)iniciados"
  until curl -sf http://localhost:8900/health >/dev/null 2>&1; do sleep 1; done
  say "$G" "orchestrator worker no ar"
  until curl -sf http://localhost:4000/health/liveliness >/dev/null 2>&1; do sleep 1; done
  say "$G" "model-gateway no ar"

  step "7/8 painel (console API + admin-ui)"
  if [ "$WITH_CONSOLE" = yes ] && [ -d "$CONSOLE_DIR" ]; then
    mkdir -p "$LOGDIR"
    if ! curl -sf "http://localhost:${CONSOLE_API_PORT}/health/integrations" >/dev/null 2>&1; then
      ( cd "$CONSOLE_DIR/control-plane/api" \
        && DSE_DATA_SOURCE=controlplane PORT="$CONSOLE_API_PORT" \
           nohup npx tsx src/server.ts > "$LOGDIR/api.log" 2>&1 & echo $! > "$LOGDIR/api.pid" )
      until curl -sf "http://localhost:${CONSOLE_API_PORT}/health/integrations" >/dev/null 2>&1; do sleep 1; done
    fi
    say "$G" "console API :$CONSOLE_API_PORT"
    if ! lsof -nP -iTCP:"$CONSOLE_UI_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
      ( cd "$CONSOLE_DIR/control-plane/admin-ui" \
        && API_PROXY_TARGET="http://localhost:${CONSOLE_API_PORT}" \
           nohup npm run dev > "$LOGDIR/ui.log" 2>&1 & echo $! > "$LOGDIR/ui.pid" )
      until curl -so /dev/null "http://localhost:${CONSOLE_UI_PORT}/" 2>/dev/null; do sleep 1; done
    fi
    say "$G" "admin-ui :$CONSOLE_UI_PORT"
  else
    [ "$WITH_CONSOLE" = yes ] && say "$Y" "console não encontrado em $CONSOLE_DIR (defina DSE_CONSOLE_DIR)" \
                              || say "$Y" "painel pulado (--no-console)"
  fi

  step "8/8 pronto"
  cmd_status
  cat <<EOF

  Painel .......... http://localhost:${CONSOLE_UI_PORT}   (Basic auth: ver admin-ui/.env.local)
  Temporal UI ..... http://localhost:8088
  Previews ........ http://<ns>.preview.localhost:8081 (link aparece no PR)
  Logs painel ..... $LOGDIR/{api,ui}.log
EOF
  if [ "$SECRETS_LOADED" = no ] || [ -z "${GITHUB_APP_ID:-}" ]; then
    printf "\n  \033[33mLembrete:\033[0m preencha %s e re-rode ./dse.sh start\n" "$SECRETS_FILE"
    printf "  (sem ele: nada de PRs reais no GitHub — o resto funciona)\n"
  fi
}

# ------------------------------------------------------------------- status --
check() { # check <nome> <comando...>
  local name="$1"; shift
  if "$@" >/dev/null 2>&1; then say "$G" "$name"; else say "$R" "$name"; fi
}
cmd_status() {
  echo ""
  check "postgres"            docker exec dse_postgres pg_isready -U dse -d dse
  check "temporal"            sh -c 'docker ps --format "{{.Names}} {{.Status}}" | grep -q "dse_temporal Up"'
  check "vault"               curl -sf http://localhost:8200/v1/sys/health
  check "model-gateway :4000" curl -sf http://localhost:4000/health/liveliness
  check "orchestrator :8900"  curl -sf http://localhost:8900/health
  check "adapter-github"      sh -c 'docker ps --format "{{.Names}} {{.Status}}" | grep -q "dse_adapter_github Up"'
  check "adapter-slack"       sh -c 'docker ps --format "{{.Names}} {{.Status}}" | grep -q "dse_adapter_slack Up"'
  check "adapter-jira"        sh -c 'docker ps --format "{{.Names}} {{.Status}}" | grep -q "dse_adapter_jira Up"'
  check "ingest (gw+disp)"    sh -c 'docker ps --format "{{.Names}}" | grep -q dse_ingest_gateway && docker ps --format "{{.Names}}" | grep -q dse_ingest_dispatcher'
  check "console-projector"   sh -c 'docker ps --format "{{.Names}} {{.Status}}" | grep -q "dse_console_projector Up"'
  check "gitserver (GitOps)"  sh -c 'docker ps --format "{{.Names}} {{.Status}}" | grep -q "dse-wse-gitserver Up"'
  check "cluster k3d"         sh -c 'kubectl --context k3d-dse-preview get nodes 2>/dev/null | grep -q Ready'
  check "argo cd"             sh -c 'kubectl --context k3d-dse-preview -n argocd get deploy argocd-server -o jsonpath="{.status.availableReplicas}" 2>/dev/null | grep -qE "^[1-9]"'
  check "console API :${CONSOLE_API_PORT}" curl -sf "http://localhost:${CONSOLE_API_PORT}/health/integrations"
  check "admin-ui :${CONSOLE_UI_PORT}"     sh -c "curl -so /dev/null http://localhost:${CONSOLE_UI_PORT}/"
  # secrets nos containers (indicador, sem valores)
  if docker exec dse_orchestrator sh -c '[ -n "$GITHUB_APP_ID" ]' 2>/dev/null; then
    say "$G" "GitHub App creds injetadas"
  else
    say "$Y" "GitHub App creds AUSENTES (fluxo real de PR desativado — preencha secrets.env)"
  fi
}

# --------------------------------------------------------------------- stop --
cmd_stop() {
  local ALL=no; [ "${1:-}" = "--all" ] && ALL=yes
  step "parando painel"
  for p in api ui; do
    [ -f "$LOGDIR/$p.pid" ] && kill "$(cat "$LOGDIR/$p.pid")" 2>/dev/null && rm -f "$LOGDIR/$p.pid" || true
  done
  pkill -f "tsx src/server.ts" 2>/dev/null || true
  pkill -f "next dev -p ${CONSOLE_UI_PORT}" 2>/dev/null || true
  say "$G" "painel parado"
  step "parando serviços"
  if [ "$ALL" = yes ]; then
    # shellcheck disable=SC2086
    docker compose $COMPOSE_ARGS down
    command -v k3d >/dev/null && k3d cluster delete dse-preview 2>/dev/null || true
    say "$G" "tudo derrubado (compose down + cluster)"
  else
    # shellcheck disable=SC2086
    docker compose $COMPOSE_ARGS stop
    say "$G" "containers parados (estado preservado — ./dse.sh start religa)"
  fi
}

# --------------------------------------------------------------------- logs --
cmd_logs() {
  case "${1:-}" in
    console-api) tail -f "$LOGDIR/api.log" ;;
    console-ui)  tail -f "$LOGDIR/ui.log" ;;
    # shellcheck disable=SC2086
    "")          docker compose $COMPOSE_ARGS logs -f --tail 50 ;;
    # shellcheck disable=SC2086
    *)           docker compose $COMPOSE_ARGS logs -f --tail 100 "$1" ;;
  esac
}

case "${1:-}" in
  start)   shift; cmd_start "$@" ;;
  stop)    shift; cmd_stop "${1:-}" ;;
  status)  load_secrets; cmd_status ;;
  logs)    shift; cmd_logs "${1:-}" ;;
  secrets) [ -f "$SECRETS_FILE" ] && { echo "já existe: $SECRETS_FILE (chaves:)"; grep -oE "^[A-Z_]+" "$SECRETS_FILE"; } || { secrets_template; echo "template criado: $SECRETS_FILE"; } ;;
  *) sed -n '3,20p' "$0" ;;
esac
