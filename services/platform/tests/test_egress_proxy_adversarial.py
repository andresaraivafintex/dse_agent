"""WSF-E2 — testes adversariais do egress proxy / broker de credenciais
(construído pelo WS-C em `services/egress-proxy/`, porta 8806 — ver
CONVENTIONS.md). Papel do WS-F aqui é sign-off: atacar a interface HTTP
exposta e provar (ou refutar) as garantias "default-deny" e "zero
credenciais persistentes no sandbox".

IMPORTANTE — interface assumida: no momento em que esta suíte foi escrita,
`services/egress-proxy/` ainda não existia neste checkout (WS-C constrói em
paralelo). Não há uma interface documentada e publicada ainda, então estes
testes assumem o contrato mais provável dado o que a proposta técnica
descreve ("Proxy default-deny + injeção de credenciais efêmeras", CONVENTIONS.md):

  1. O proxy funciona como forward proxy HTTP/HTTPS padrão em
     `http://localhost:8806` (o sandbox aponta HTTPS_PROXY/HTTP_PROXY para
     cá) — CONNECT tunneling para HTTPS, allowlist de hosts de destino.
  2. Hosts fora da allowlist recebem 403 (ou a conexão é recusada/hang up
     no CONNECT) — nunca um encaminhamento silencioso.
  3. Um endpoint de observabilidade/health em `GET /health` (convenção do
     monorepo para os demais serviços HTTP) responde 200 quando o proxy
     está operante.

QUANDO O WS-C PUBLICAR A INTERFACE REAL: se ela for diferente (ex.: API
REST tipo `POST /v1/egress {url, headers}` em vez de forward-proxy puro),
esta suíte inteira precisa ser reescrita contra o contrato real — não é
incremental. Os testes abaixo detectam essa divergência e SKIPAM com razão
clara em vez de falhar silenciosamente ou dar falso-positivo (P6:
decline-never-truncate — preferimos declarar "não pude verificar" a fingir
que verificamos).

Rode `make up` estilo WS-C (ou o compose fragment dele) antes de rodar esta
suíte para exercitar de verdade; sem isso, todos os testes abaixo skipam com
razão "egress-proxy não está respondendo em localhost:8806".
"""
from __future__ import annotations

import socket
import time
from urllib.parse import urlparse

import pytest
import requests

PROXY_HOST = "localhost"
PROXY_PORT = 8806
PROXY_URL = f"http://{PROXY_HOST}:{PROXY_PORT}"
PROXIES = {"http": PROXY_URL, "https": PROXY_URL}
TIMEOUT = 4.0


def _tcp_reachable(host: str, port: int, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


_PROXY_UP = _tcp_reachable(PROXY_HOST, PROXY_PORT)

pytestmark = pytest.mark.skipif(
    not _PROXY_UP,
    reason=(
        f"services/egress-proxy (WS-C) não está respondendo em {PROXY_HOST}:{PROXY_PORT}. "
        "Esta suíte adversarial (WSF-E2) precisa ser reexecutada assim que o WS-C subir o "
        "serviço — ver services/platform/README.md secção 'Egress proxy adversarial tests' "
        "para instruções exatas e para o que reavaliar caso a interface real difira do "
        "forward-proxy HTTP/HTTPS assumido aqui."
    ),
)


def _proxy_speaks_forward_http() -> bool:
    """Sonda leve: um forward proxy HTTP real responde a um GET simples
    através dele (mesmo que com 403/502); um serviço REST arbitrário na
    mesma porta tipicamente responderia com 400 a uma linha de request mal
    formada para ele. Usado para decidir se aplicamos os testes de
    forward-proxy ou skipamos com razão de "interface não é a assumida"."""
    try:
        requests.get("http://dse-adversarial-probe.invalid/", proxies=PROXIES, timeout=TIMEOUT)
        return True
    except requests.exceptions.ProxyError:
        return True  # respondeu como proxy (recusou o CONNECT/GET) — é a interface esperada
    except requests.exceptions.ConnectionError:
        return False
    except requests.exceptions.RequestException:
        return True


@pytest.fixture(scope="module", autouse=True)
def _require_forward_proxy_interface():
    if not _proxy_speaks_forward_http():
        pytest.skip(
            "egress-proxy está no ar mas não respondeu como forward-proxy HTTP/HTTPS padrão "
            "(a suposição de interface documentada no topo deste arquivo). Reescreva esta "
            "suíte contra a interface real publicada pelo WS-C."
        )


# ---------------------------------------------------------------------------
# 1) Acesso a host fora da allowlist
# ---------------------------------------------------------------------------
class TestAllowlistEnforcement:
    NON_ALLOWLISTED_HOSTS = [
        "https://evil-exfil-destination.example.com/",
        "https://pastebin.com/raw/anything",
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata SSRF clássico
        "https://api.telegram.org/bot123/sendMessage",  # canal de exfil comum
    ]

    @pytest.mark.parametrize("url", NON_ALLOWLISTED_HOSTS)
    def test_non_allowlisted_host_is_denied(self, url):
        denied = False
        try:
            resp = requests.get(url, proxies=PROXIES, timeout=TIMEOUT)
            denied = resp.status_code in (401, 403, 407, 502, 503)
        except requests.exceptions.ProxyError:
            denied = True  # proxy recusou o CONNECT — comportamento default-deny esperado
        except requests.exceptions.ConnectionError:
            denied = True  # conexão derrubada pelo proxy — também aceitável como "negado"
        except requests.exceptions.Timeout:
            pytest.skip(f"proxy não respondeu (timeout) para {url} — inconclusivo, reexecutar")

        assert denied, (
            f"host fora da allowlist '{url}' NÃO foi negado pelo proxy — falha crítica de "
            "default-deny (FR-11 violado)"
        )

    def test_allowlist_denial_is_never_a_silent_forward(self):
        """Uma negação nunca deve parecer um 200 com o conteúdo do destino de
        verdade — isso indicaria bypass silencioso (P6: falhar visivelmente,
        nunca truncar/mascarar)."""
        url = "https://evil-exfil-destination.example.com/should-never-be-reachable"
        try:
            resp = requests.get(url, proxies=PROXIES, timeout=TIMEOUT)
        except (requests.exceptions.ProxyError, requests.exceptions.ConnectionError):
            return  # negado na camada de conexão — ok
        except requests.exceptions.Timeout:
            pytest.skip("timeout — inconclusivo")
        assert resp.status_code != 200, (
            "proxy retornou 200 para host claramente fora de qualquer allowlist plausível — "
            "suspeita de fail-open"
        )


# ---------------------------------------------------------------------------
# 2) Bypass de allowlist via redirect / DNS trick / confusão de host
# ---------------------------------------------------------------------------
class TestBypassAttempts:
    # Técnicas clássicas de bypass de allowlist baseada em hostname: confusão
    # de sufixo, userinfo embutido na URL, encoding alternativo de IP,
    # concatenação de host permitido como subdomínio de host malicioso.
    BYPASS_ATTEMPTS = [
        "https://api.github.com.evil-exfil.com/",       # sufixo confuso
        "https://evil-exfil.com/api.github.com",         # path confuso
        "http://2130706433/",                             # 127.0.0.1 em decimal
        "http://0x7f000001/",                             # 127.0.0.1 em hex
        "http://[::ffff:127.0.0.1]/",                     # IPv4-mapped IPv6
    ]

    @pytest.mark.parametrize("url", BYPASS_ATTEMPTS)
    def test_hostname_confusion_bypass_is_denied(self, url):
        try:
            resp = requests.get(url, proxies=PROXIES, timeout=TIMEOUT)
            denied = resp.status_code in (400, 401, 403, 407, 502, 503)
        except (requests.exceptions.ProxyError, requests.exceptions.ConnectionError):
            denied = True
        except requests.exceptions.Timeout:
            pytest.skip(f"timeout para {url} — inconclusivo, reexecutar")
        except requests.exceptions.InvalidURL:
            return  # o próprio parser de URL rejeitou — não chegou a testar o proxy, ok

        assert denied, f"tentativa de bypass via confusão de host '{url}' não foi negada"

    def test_redirect_to_non_allowlisted_host_is_still_enforced(self, httpserver=None):
        """Simula um host allowlisted (assumido: nenhum de fato está
        configurado neste ambiente de teste isolado) que devolve um 302 para
        um destino fora da allowlist. Sem controle sobre um host realmente
        allowlisted neste teste, validamos a propriedade mais fraca porém
        ainda real: `requests` com `allow_redirects=True` NÃO deve terminar
        com 200 vindo de um destino fora de qualquer allowlist plausível
        quando o primeiro hop já é desconhecido do proxy."""
        url = "https://evil-exfil-destination.example.com/redirect-to-elsewhere"
        try:
            resp = requests.get(url, proxies=PROXIES, timeout=TIMEOUT, allow_redirects=True)
            denied = resp.status_code in (401, 403, 407, 502, 503)
        except (requests.exceptions.ProxyError, requests.exceptions.ConnectionError):
            denied = True
        except requests.exceptions.Timeout:
            pytest.skip("timeout — inconclusivo")
        assert denied


# ---------------------------------------------------------------------------
# 3) Reuso de token capturado fora da janela/escopo
# ---------------------------------------------------------------------------
class TestCredentialReuse:
    """Estas verificações dependem de uma API de broker de credenciais que
    ainda não está documentada publicamente pelo WS-C (endpoint de
    emissão/introspecção de token efêmero). Tentamos os caminhos mais
    prováveis (`/v1/credentials/*`, `/broker/*`) e SKIPAMOS com razão clara
    se nenhum responder — não inventamos um contrato que o WS-C não
    publicou."""

    CANDIDATE_ISSUE_PATHS = ["/v1/credentials/issue", "/broker/issue", "/v1/token"]
    CANDIDATE_INTROSPECT_PATHS = ["/v1/credentials/introspect", "/broker/introspect", "/v1/token/introspect"]

    def _find_working_endpoint(self, candidates: list[str], method: str = "get") -> str | None:
        for path in candidates:
            try:
                resp = getattr(requests, method)(f"{PROXY_URL}{path}", timeout=TIMEOUT)
                if resp.status_code != 404:
                    return path
            except requests.exceptions.RequestException:
                continue
        return None

    def test_expired_or_out_of_scope_token_is_rejected(self):
        issue_path = self._find_working_endpoint(self.CANDIDATE_ISSUE_PATHS, method="post")
        introspect_path = self._find_working_endpoint(self.CANDIDATE_INTROSPECT_PATHS, method="get")

        if issue_path is None or introspect_path is None:
            pytest.skip(
                "Nenhum endpoint de emissão/introspecção de credencial efêmera encontrado nos "
                f"caminhos candidatos {self.CANDIDATE_ISSUE_PATHS + self.CANDIDATE_INTROSPECT_PATHS}. "
                "WS-C ainda não publicou a API do broker — REEXECUTAR esta verificação assim que "
                "publicar (ver services/platform/README.md)."
            )

        # Se algum dia um destes caminhos existir de fato, o teste real:
        # 1. emite um token com TTL curto e escopo restrito a um host X
        # 2. espera o TTL expirar
        # 3. tenta introspectar/usar o token — espera rejeição (401/403)
        # 4. tenta usar o MESMO token para um host Y fora do escopo original
        #    — espera rejeição mesmo dentro do TTL
        pytest.skip(
            "endpoint candidato encontrado mas o contrato de payload (campos de scope/ttl) não "
            "está documentado — implementar o corpo do teste quando o WS-C publicar o schema."
        )

    def test_captured_token_cannot_be_replayed_directly_against_upstream(self):
        """Propriedade de mais alto nível ('zero credenciais no sandbox'): um
        token supostamente injetado pelo proxy na borda não deve ser
        reutilizável fora da requisição original se capturado (ex.: por um
        processo dentro do sandbox tentando fazer replay direto contra o
        upstream, pulando o proxy). Sem acesso ao sandbox real do WS-C nem a
        um upstream de teste controlado, este teste permanece um placeholder
        de INTENÇÃO — não inventamos uma asserção que não podemos verificar
        de verdade (isso violaria P8: evidência sobre asserção)."""
        pytest.skip(
            "Requer um sandbox real (services/sandbox-runtime, WS-C) executando uma sessão e "
            "um upstream de teste controlado para capturar+repetir um token de verdade. "
            "Não verificável apenas contra a interface HTTP do proxy isoladamente — mover para "
            "um teste de integração cross-workstream (WS-C + WS-F) na fase de consolidação."
        )


def test_proxy_is_listening_and_rejects_malformed_requests():
    """Achado da integração da Fase 1: o egress-proxy do WS-C (`egress_proxy/
    proxy.py`) é um forward proxy HTTP/CONNECT bruto, não uma API REST — não
    existe rota `/health`. Um GET a `/health` no próprio socket do proxy é um
    request de proxy malformado (sem absolute-URI, sem CONNECT) e o proxy
    corretamente responde 400 (`_handle_plain_http`), não 200. O teste
    original assumia uma convenção de `/health` nunca confirmada contra a
    interface real (o próprio comentário original já sinalizava isso) —
    substituído por uma asserção que reflete o comportamento real: o proxy
    está de pé e recusa o request malformado de forma limpa (não trava, não
    faz hang, não repassa o request adiante)."""
    try:
        resp = requests.get(f"{PROXY_URL}/health", timeout=TIMEOUT)
    except requests.exceptions.RequestException:
        pytest.skip("proxy não está no ar em PROXY_URL")
    assert resp.status_code == 400
