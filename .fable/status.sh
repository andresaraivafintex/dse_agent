#!/bin/bash
# Live state from the VPS. Every loop iteration runs this FIRST and reads it
# instead of remembering. Cheap enough to run every time.
ssh -o BatchMode=yes -o ConnectTimeout=25 dse-vps 'bash -s' <<'REMOTE' 2>&1
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
Q(){ sudo k3s kubectl -n dse exec dse-dse-postgres-0 -- psql -U dse -d dse -At -F' | ' -c "$1" < /dev/null 2>/dev/null; }
echo "=== UTC $(date -u +%H:%M:%S) ==="
echo "--- versao em execucao ---"
sudo k3s kubectl -n dse get deploy -o jsonpath='{range .items[*]}{.spec.template.spec.containers[0].image}{"\n"}{end}' < /dev/null 2>/dev/null | grep -oE "rc\.[0-9]+" | sort | uniq -c | sed 's/^/  /'
echo "--- pods fora de Running ---"
sudo k3s kubectl -n dse get pods --no-headers < /dev/null 2>/dev/null | grep -vE "Completed|([0-9]+)/\1 +Running" | awk '{print "  "$1,$3}' | head -5
echo "--- sandboxes ativos ---"
sudo k3s kubectl -n dse-sandboxes get pods --no-headers < /dev/null 2>/dev/null | awk '{print "  "$1,$3,$5}'
echo "--- work items recentes ---"
Q "select id, status, coalesce(repo,'(sem repo)'), to_char(created_at,'HH24:MI') from work_items order by created_at desc limit 6" | sed 's/^/  /'
echo "--- PRs abertas pelo DSE ---"
Q "select work_item_id, pr_url from wse_pr_tracking order by created_at desc limit 6" | sed 's/^/  /'
echo "--- decisoes de roteamento ---"
Q "select work_item_id, left(details::text,150) from audit_log where action='repo_routing_decided' order by id desc limit 4" | sed 's/^/  /'
echo "--- bindings do canal (se apontar 1 repo, o roteador NAO roda) ---"
Q "select platform, binding_type, binding_value, repo from repo_bindings where tenant_id='fintex-poc' order by 1,2" | sed 's/^/  /'
echo "--- repo_profiles (o roteador le isto) ---"
Q "select repo, role from repo_profiles where tenant_id='fintex-poc' order by 1" | sed 's/^/  /'
echo "--- atividade em voo ---"
for W in $(Q "select id from work_items where status not in ('done','failed','escalated','awaiting_human_review') order by created_at desc limit 2"); do
  sudo k3s kubectl -n dse exec deploy/dse-dse-temporal -- temporal workflow describe \
    --address dse-dse-temporal:7233 --namespace default --workflow-id "$W" -o json < /dev/null 2>/dev/null \
   | python3 -c "
import sys,json,datetime
try: d=json.load(sys.stdin)
except Exception: raise SystemExit
now=datetime.datetime.now(datetime.timezone.utc)
st=str(d.get('workflowExecutionInfo',{}).get('status')).replace('WORKFLOW_EXECUTION_STATUS_','')
for p in d.get('pendingActivities',[]):
    hb=p.get('lastHeartbeatTime') or p.get('lastStartedTime') or ''
    try: age=int((now-datetime.datetime.fromisoformat(hb.replace('Z','+00:00'))).total_seconds())
    except Exception: age='?'
    print('  ', '$W'[:24], st, p.get('activityType',{}).get('name'), 'try', p.get('attempt'), 'hb', age,'s')
    break
else: print('  ', '$W'[:24], st, '(sem atividade pendente)')
"
done
REMOTE
