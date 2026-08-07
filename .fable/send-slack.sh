#!/bin/bash
# Envia um pedido pelo CAMINHO REAL do Slack — intake, nao Temporal.
#
#   bash .fable/send-slack.sh "a frase do pedido"
#
# POR QUE ISSO EXISTE. Os tres testes vinham sendo iniciados com
# `temporal workflow start`, o que pula o intake inteiro. O intake e quem CRIA
# A LINHA em `work_items`, e sem ela:
#
#   - o fan-out multi-repo morre com "primary work item '<id>' not found"
#     (foi exatamente assim que o teste 3 falhou, 5,1 s depois de o roteador
#     ter acertado OS DOIS repositorios);
#   - qualquer espera baseada em `work_items.status` trava para sempre.
#
# Medido: todo item vindo do Slack real TEM linha em `work_items`; os tres itens
# `wi_t*` iniciados por mim tinham ZERO. O defeito era do meu arranjo de teste,
# nunca do produto — e custou tres rodadas de "a feature multi-repo falhou".
#
# A assinatura HMAC e calculada DENTRO do pod, lendo o signing secret do proprio
# processo. O segredo nunca sai da maquina.
#
# A frase viaja em base64: ela atravessa dois heredocs e um `python -c`, e
# aspas/acentos/apostrofos quebram qualquer outra forma de citacao.
set -u
FRASE="${1:?uso: send-slack.sh \"a frase\"}"
CANAL="${CANAL:-C0BKA7TMMEY}"     # o canal que o Andre usa
USUARIO="${USUARIO:-U0BJWSGNA20}" # -> usr_9c50c8496cd14260
B64=$(printf '%s' "$FRASE" | base64 | tr -d '\n')

ssh dse-vps 'bash -s' <<REMOTE
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
POD=\$(sudo k3s kubectl -n dse get pods -o name | grep adapter-slack | head -1)
sudo k3s kubectl -n dse exec "\$POD" -- python -c '
import base64, hmac, hashlib, json, time, sys, urllib.request
from adapter_slack.config import get_slack_signing_secret

frase   = base64.b64decode(sys.argv[1]).decode()
canal   = sys.argv[2]
usuario = sys.argv[3]
ts      = "%.6f" % time.time()

payload = {
    "type": "event_callback",
    "team_id": "T0BJR6TQ5V8",
    "event_id": "Ev" + ts.replace(".", ""),
    "event": {
        "type": "app_mention",
        "user": usuario,
        "text": frase,
        "channel": canal,
        "ts": ts,
    },
}
body = json.dumps(payload).encode()
stamp = str(int(time.time()))
sig = "v0=" + hmac.new(
    get_slack_signing_secret().encode(),
    b"v0:" + stamp.encode() + b":" + body,
    hashlib.sha256,
).hexdigest()

req = urllib.request.Request(
    "http://127.0.0.1:8801/slack/events",
    data=body,
    headers={
        "Content-Type": "application/json",
        "X-Slack-Request-Timestamp": stamp,
        "X-Slack-Signature": sig,
    },
)
try:
    print(urllib.request.urlopen(req, timeout=60).read().decode())
except Exception as exc:
    detail = getattr(exc, "read", lambda: b"")()
    print("ERRO:", type(exc).__name__, getattr(exc, "code", ""), detail.decode()[:300])
' '$B64' '$CANAL' '$USUARIO'
REMOTE
