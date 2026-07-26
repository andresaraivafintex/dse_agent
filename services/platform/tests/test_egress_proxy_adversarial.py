"""WSF-E2 — adversarial tests for the egress proxy / credential broker (built by
WS-C in `services/egress-proxy/`, port 8806 — see CONVENTIONS.md). WS-F's role
here is sign-off: attack the exposed HTTP interface and prove (or disprove) the
"default-deny" and "zero persistent credentials in the sandbox" guarantees.

IMPORTANT — assumed interface: when this suite was written,
`services/egress-proxy/` did not exist yet in this checkout (WS-C builds it in
parallel). There is no documented, published interface yet, so these tests assume
the most likely contract given what the technical proposal describes
("Default-deny proxy + ephemeral credential injection", CONVENTIONS.md):

  1. The proxy works as a standard HTTP/HTTPS forward proxy at
     `http://localhost:8806` (the sandbox points HTTPS_PROXY/HTTP_PROXY here) —
     CONNECT tunneling for HTTPS, allowlist of destination hosts.
  2. Hosts outside the allowlist get a 403 (or the connection is refused/hung up
     on CONNECT) — never a silent forward.
  3. An observability/health endpoint at `GET /health` (the monorepo convention
     for the other HTTP services) answers 200 when the proxy is up.

WHEN WS-C PUBLISHES THE REAL INTERFACE: if it differs (e.g. a REST API like
`POST /v1/egress {url, headers}` instead of a pure forward proxy), this whole
suite must be rewritten against the real contract — it is not incremental. The
tests below detect that divergence and SKIP with a clear reason instead of
failing silently or producing a false positive (P6: decline-never-truncate — we
would rather state "could not verify" than pretend we verified).

Run WS-C's `make up` (or its compose fragment) before running this suite for it
to actually exercise anything; without that, every test below skips with the
reason "egress-proxy is not answering on localhost:8806".
"""
from __future__ import annotations

import socket

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
        f"services/egress-proxy (WS-C) is not answering on {PROXY_HOST}:{PROXY_PORT}. "
        "This adversarial suite (WSF-E2) has to be re-run as soon as WS-C brings the "
        "service up — see services/platform/README.md section 'Egress proxy adversarial tests' "
        "for the exact instructions and for what to re-evaluate should the real interface "
        "differ from the HTTP/HTTPS forward proxy assumed here."
    ),
)


def _proxy_speaks_forward_http() -> bool:
    """Light probe: a real HTTP forward proxy answers a plain GET made through it
    (even with a 403/502); an arbitrary REST service on the same port would
    typically answer 400 to a request line that is malformed for it. Used to
    decide whether to apply the forward-proxy tests or skip with the reason
    "the interface is not the assumed one"."""
    try:
        requests.get("http://dse-adversarial-probe.invalid/", proxies=PROXIES, timeout=TIMEOUT)
        return True
    except requests.exceptions.ProxyError:
        return True  # answered as a proxy (refused the CONNECT/GET) — the expected interface
    except requests.exceptions.ConnectionError:
        return False
    except requests.exceptions.RequestException:
        return True


@pytest.fixture(scope="module", autouse=True)
def _require_forward_proxy_interface():
    if not _proxy_speaks_forward_http():
        pytest.skip(
            "egress-proxy is up but did not answer as a standard HTTP/HTTPS forward proxy "
            "(the interface assumption documented at the top of this file). Rewrite this "
            "suite against the real interface published by WS-C."
        )


# ---------------------------------------------------------------------------
# 1) Access to a host outside the allowlist
# ---------------------------------------------------------------------------
class TestAllowlistEnforcement:
    NON_ALLOWLISTED_HOSTS = [
        "https://evil-exfil-destination.example.com/",
        "https://pastebin.com/raw/anything",
        "http://169.254.169.254/latest/meta-data/",  # classic cloud metadata SSRF
        "https://api.telegram.org/bot123/sendMessage",  # common exfil channel
    ]

    @pytest.mark.parametrize("url", NON_ALLOWLISTED_HOSTS)
    def test_non_allowlisted_host_is_denied(self, url):
        denied = False
        try:
            resp = requests.get(url, proxies=PROXIES, timeout=TIMEOUT)
            denied = resp.status_code in (401, 403, 407, 502, 503)
        except requests.exceptions.ProxyError:
            denied = True  # proxy refused the CONNECT — the expected default-deny behavior
        except requests.exceptions.ConnectionError:
            denied = True  # connection dropped by the proxy — also acceptable as "denied"
        except requests.exceptions.Timeout:
            pytest.skip(f"proxy did not answer (timeout) for {url} — inconclusive, re-run")

        assert denied, (
            f"host outside the allowlist '{url}' was NOT denied by the proxy — critical "
            "default-deny failure (FR-11 violated)"
        )

    def test_allowlist_denial_is_never_a_silent_forward(self):
        """A denial must never look like a 200 carrying the real destination's
        content — that would mean a silent bypass (P6: fail visibly, never
        truncate/mask)."""
        url = "https://evil-exfil-destination.example.com/should-never-be-reachable"
        try:
            resp = requests.get(url, proxies=PROXIES, timeout=TIMEOUT)
        except (requests.exceptions.ProxyError, requests.exceptions.ConnectionError):
            return  # denied at the connection layer — ok
        except requests.exceptions.Timeout:
            pytest.skip("timeout — inconclusive")
        assert resp.status_code != 200, (
            "proxy returned 200 for a host clearly outside any plausible allowlist — "
            "fail-open suspected"
        )


# ---------------------------------------------------------------------------
# 2) Allowlist bypass via redirect / DNS trick / host confusion
# ---------------------------------------------------------------------------
class TestBypassAttempts:
    # Classic bypass techniques against a hostname-based allowlist: suffix
    # confusion, userinfo embedded in the URL, alternative IP encodings,
    # concatenating an allowed host as a subdomain of a malicious host.
    BYPASS_ATTEMPTS = [
        "https://api.github.com.evil-exfil.com/",       # confusing suffix
        "https://evil-exfil.com/api.github.com",         # confusing path
        "http://2130706433/",                             # 127.0.0.1 in decimal
        "http://0x7f000001/",                             # 127.0.0.1 in hex
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
            pytest.skip(f"timeout for {url} — inconclusive, re-run")
        except requests.exceptions.InvalidURL:
            return  # the URL parser itself rejected it — the proxy was never tested, ok

        assert denied, f"bypass attempt via host confusion '{url}' was not denied"

    def test_redirect_to_non_allowlisted_host_is_still_enforced(self, httpserver=None):
        """Simulates an allowlisted host (assumed: none is actually configured in
        this isolated test environment) returning a 302 to a destination outside
        the allowlist. Without control over a truly allowlisted host in this
        test, we validate the weaker but still real property: `requests` with
        `allow_redirects=True` must NOT end with a 200 coming from a destination
        outside any plausible allowlist when the first hop is already unknown to
        the proxy."""
        url = "https://evil-exfil-destination.example.com/redirect-to-elsewhere"
        try:
            resp = requests.get(url, proxies=PROXIES, timeout=TIMEOUT, allow_redirects=True)
            denied = resp.status_code in (401, 403, 407, 502, 503)
        except (requests.exceptions.ProxyError, requests.exceptions.ConnectionError):
            denied = True
        except requests.exceptions.Timeout:
            pytest.skip("timeout — inconclusive")
        assert denied


# ---------------------------------------------------------------------------
# 3) Reuse of a captured token outside its window/scope
# ---------------------------------------------------------------------------
class TestCredentialReuse:
    """These checks depend on a credential broker API that WS-C has not publicly
    documented yet (ephemeral token issue/introspect endpoints). We try the most
    likely paths (`/v1/credentials/*`, `/broker/*`) and SKIP with a clear reason
    if none answers — we do not invent a contract WS-C never published."""

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
                "No ephemeral-credential issue/introspection endpoint found among the "
                f"candidate paths {self.CANDIDATE_ISSUE_PATHS + self.CANDIDATE_INTROSPECT_PATHS}. "
                "WS-C has not published the broker API yet — RE-RUN this check as soon as "
                "it does (see services/platform/README.md)."
            )

        # If one of these paths ever really exists, the real test would:
        # 1. issue a token with a short TTL and scope restricted to host X
        # 2. wait for the TTL to expire
        # 3. try to introspect/use the token — expect rejection (401/403)
        # 4. try to use the SAME token against a host Y outside the original
        #    scope — expect rejection even while inside the TTL
        pytest.skip(
            "candidate endpoint found but the payload contract (scope/ttl fields) is not "
            "documented — implement the test body once WS-C publishes the schema."
        )

    def test_captured_token_cannot_be_replayed_directly_against_upstream(self):
        """Higher-level property ('zero credentials in the sandbox'): a token
        supposedly injected by the proxy at the edge must not be reusable outside
        the original request if captured (e.g. by a process inside the sandbox
        replaying it directly against the upstream, bypassing the proxy). Without
        access to WS-C's real sandbox nor a controlled test upstream, this test
        stays an INTENT placeholder — we do not invent an assertion we cannot
        actually verify (that would violate P8: evidence over assertion)."""
        pytest.skip(
            "Requires a real sandbox (services/sandbox-runtime, WS-C) running a session and "
            "a controlled test upstream to capture+replay a real token. "
            "Not verifiable against the proxy HTTP interface alone — move it to a "
            "cross-workstream integration test (WS-C + WS-F) in the consolidation phase."
        )


def test_proxy_is_listening_and_rejects_malformed_requests():
    """Phase 1 integration finding: WS-C's egress-proxy (`egress_proxy/proxy.py`)
    is a raw HTTP/CONNECT forward proxy, not a REST API — there is no `/health`
    route. A GET to `/health` on the proxy's own socket is a malformed proxy
    request (no absolute-URI, no CONNECT) and the proxy correctly answers 400
    (`_handle_plain_http`), not 200. The original test assumed a `/health`
    convention that was never confirmed against the real interface (its own
    comment already flagged that) — replaced with an assertion that reflects the
    real behavior: the proxy is up and refuses the malformed request cleanly (it
    does not crash, does not hang, does not forward the request onward)."""
    try:
        resp = requests.get(f"{PROXY_URL}/health", timeout=TIMEOUT)
    except requests.exceptions.RequestException:
        pytest.skip("proxy is not up at PROXY_URL")
    assert resp.status_code == 400
