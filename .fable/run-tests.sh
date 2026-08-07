#!/bin/bash
# Roda os tres testes EM SEQUENCIA, um por vez, e registra tudo no OVERNIGHT.md.
# Um item por vez e deliberado: a caixa tem 4 vCPU e dois itens tornam ambos
# mais lentos e os tempos ilegiveis.
CTRL=/Users/saraiva/Documents/DSE/fase1/.fable/OVERNIGHT.md
log(){ echo "[$(date -u +%H:%M:%S)] $*"; echo "- $(date -u +%H:%M) — $*" >> "$CTRL"; }

esperar() {  # esperar <workflow-id> <rotulo>
  local WI="$1" LBL="$2" i
  for i in $(seq 1 90); do
    S=$(ssh -o BatchMode=yes -o ConnectTimeout=25 dse-vps "bash -s" <<REMOTE 2>&1
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
sudo k3s kubectl -n dse exec deploy/dse-dse-temporal -- temporal workflow describe \
  --address dse-dse-temporal:7233 --namespace default --workflow-id $WI -o json < /dev/null 2>/dev/null \
 | python3 -c "
import sys,json,datetime
d=json.load(sys.stdin); now=datetime.datetime.now(datetime.timezone.utc)
print('WF='+str(d.get('workflowExecutionInfo',{}).get('status')).replace('WORKFLOW_EXECUTION_STATUS_',''))
for p in d.get('pendingActivities',[]):
    hb=p.get('lastHeartbeatTime') or p.get('lastStartedTime') or ''
    try: age=int((now-datetime.datetime.fromisoformat(hb.replace('Z','+00:00'))).total_seconds())
    except Exception: age=0
    print('ACT='+str(p.get('activityType',{}).get('name'))+' TRY='+str(p.get('attempt'))+' HB='+str(age))
    break
"
ST=\$(sudo k3s kubectl -n dse exec dse-dse-postgres-0 -- psql -U dse -d dse -At -c "select status from work_items where id='$WI'" < /dev/null 2>/dev/null)
echo "DB=\$ST"
REMOTE
)
    echo "  [$LBL] $(echo "$S" | tr '\n' ' ')"
    WF=$(echo "$S" | grep -o 'WF=[A-Z_]*' | cut -d= -f2)
    DB=$(echo "$S" | grep -o 'DB=[a-z_]*' | cut -d= -f2)
    # O status do WORKFLOW e a verdade. Um item iniciado direto pelo Temporal
    # nao cria linha em `work_items` — o UPDATE de projecao vira no-op — entao
    # `DB=` vem vazio e o laco antigo esperava 90 minutos por um estado que
    # nunca chegaria. Foi o que aconteceu com os testes 2 e 3.
    case "$WF" in
      COMPLETED)          log "$LBL: workflow COMPLETED"; return 0;;
      FAILED|TERMINATED|TIMED_OUT) log "$LBL: workflow $WF — investigar"; return 1;;
    esac
    TRY=$(echo "$S" | grep -o 'TRY=[0-9]*' | cut -d= -f2)
    HB=$(echo "$S" | grep -o 'HB=[0-9]*' | cut -d= -f2)
    case "$DB" in
      awaiting_human_review|done|review_ready) log "$LBL chegou a '$DB'"; return 0;;
      failed|escalated)                        log "$LBL terminou em '$DB' — investigar"; return 1;;
    esac
    [ "${TRY:-1}" -ge 4 ] 2>/dev/null && { log "$LBL em loop (tentativa $TRY) — abandonando"; return 1; }
    [ -n "$HB" ] && [ "$HB" -gt 500 ] 2>/dev/null && { log "$LBL sem heartbeat ha ${HB}s — abandonando"; return 1; }
    sleep 60
  done
  log "$LBL nao terminou em 90 min"; return 1
}

disparar() {  # disparar <prefixo> <frase>
  # A frase vai por arquivo, nao por argumento: ela tem travessao, aspas e
  # apostrofo, e cada nivel de aspas entre bash local, ssh e heredoc e uma
  # chance de mutilar o pedido que o modelo vai ler.
  local PFX="$1" FRASE="$2"
  printf '%s' "$FRASE" | ssh -o BatchMode=yes -o ConnectTimeout=25 dse-vps "cat > /tmp/dse-task.txt"
  ssh -o BatchMode=yes -o ConnectTimeout=25 dse-vps "bash -s" <<REMOTE 2>&1 | grep -oE 'wi_[a-z0-9]+-[a-f0-9]+' | head -1
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
WI="wi_${PFX}-\$(head -c4 /dev/urandom | od -An -tx1 | tr -d ' \n')"
INPUT=\$(WI_ID="\$WI" python3 -c "
import json, os, pathlib
print(json.dumps({
  'work_item_id': os.environ['WI_ID'],
  'tenant_id': 'fintex-poc',
  'requester': 'andre',
  'source': 'slack',
  'base_branch': 'main',
  'task_content': pathlib.Path('/tmp/dse-task.txt').read_text(),
}))")
sudo k3s kubectl -n dse exec deploy/dse-dse-temporal -- temporal workflow start \
  --address dse-dse-temporal:7233 --namespace default --task-queue dse-core-task-queue \
  --type WorkItemLifecycleWorkflow --workflow-id "\$WI" --input "\$INPUT" < /dev/null >/dev/null 2>&1
echo "\$WI"
REMOTE
}

T2="Calling the payout-levels API in the deployed container comes back as a 500 even though it works fine when I run the service from my IDE — fix that, and while you're in there let me fetch a single payout level by its id."
T3="Admins need to retire a payout level instead of deleting it, and retired levels must stop feeding advisor fee calculations."

log "sequencia iniciada"
esperar wi_0984351e80e97397d9c98c978c505e6129504ea64bf4374354eeed8bce6e9a0e "teste1"

W2=$(disparar t2 "$T2"); log "teste2 disparado: $W2"
esperar "$W2" "teste2"

W3=$(disparar t3 "$T3"); log "teste3 disparado: $W3"
esperar "$W3" "teste3"

log "sequencia concluida"
ssh -o BatchMode=yes -o ConnectTimeout=25 dse-vps 'export KUBECONFIG=/etc/rancher/k3s/k3s.yaml; sudo k3s kubectl -n dse exec dse-dse-postgres-0 -- psql -U dse -d dse -At -F" | " -c "select work_item_id, pr_url from wse_pr_tracking order by created_at desc limit 5" < /dev/null' 2>&1 | tail -6
