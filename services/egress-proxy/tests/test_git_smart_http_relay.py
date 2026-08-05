"""Cloning a PRIVATE repo without the token ever entering the sandbox.

The failure this closes was live on the VPS: a work item planned correctly, then
`provision_sandbox` retried eight times against

    fatal: could not read Username for 'https://github.com'

because over `https://` git asks the proxy for a CONNECT tunnel, and a tunnel is
opaque — the proxy can allow or deny it but cannot add an Authorization header.
A public repo does not notice. A private one cannot be cloned at all.

The relay makes the inbound leg plain HTTP so the proxy can terminate, inject,
and re-originate over TLS. What has to stay true, and is pinned here:

  - the credential goes out ONLY on a TLS leg — the whole reason the allowlist
    pinned github.com to :443 in the first place;
  - the sandbox never holds the token, it holds a placeholder header;
  - a chunked body survives the relay, because git sends one.
"""
from __future__ import annotations

import asyncio

import pytest

from egress_proxy.allowlist import Allowlist, AllowlistEntry
from egress_proxy.proxy import EgressProxy


class _Cred:
    def __init__(self):
        self.minted = 0

    def mint(self, *, work_item_id, repo, branch):
        self.minted += 1
        return type("C", (), {"token": "ghs_REAL_TOKEN", "credential_id": "cred-1"})()


def _proxy(allowlist=None):
    return EgressProxy(
        allowlist=allowlist or Allowlist.for_work_item(),
        tenant_id="t", work_item_id="wi-1", credential_broker=_Cred(),
    )


# ---------------------------------------------------------------------------
# The allowlist entry that governs the request
# ---------------------------------------------------------------------------

def test_github_has_two_entries_and_each_answers_for_its_own_port():
    """A host can hold several entries with different rules. The host-only
    lookup returns whichever was declared first, which answered the :443 policy
    for a :80 request — the relay would silently never upgrade."""
    a = Allowlist.for_work_item()

    assert a.entry_for("github.com", 443).tls_upgrade is False
    assert a.entry_for("github.com", 80).tls_upgrade is True


def test_a_wildcard_entry_never_beats_an_exact_port_match():
    a = Allowlist(entries=[
        AllowlistEntry(host="h", port=None, reason="any"),
        AllowlistEntry(host="h", port=80, reason="relay", tls_upgrade=True),
    ])
    assert a.entry_for("h", 80).tls_upgrade is True
    assert a.entry_for("h", 443).port is None


def test_only_the_repo_host_relays_plain_http():
    """The :80 entry exists for one purpose. A package registry must not become
    a credential-injecting relay by accident."""
    a = Allowlist.for_work_item()
    for reg in ("pypi.org", "registry.npmjs.org", "files.pythonhosted.org"):
        assert a.entry_for(reg, 80).tls_upgrade is False


# ---------------------------------------------------------------------------
# The invariant: a token never leaves on a cleartext hop
# ---------------------------------------------------------------------------

class _Writer:
    def __init__(self): self.buf = b""
    def write(self, d): self.buf += d
    async def drain(self): pass
    def close(self): pass


def _reader(data: bytes) -> asyncio.StreamReader:
    """Built inside a running loop — StreamReader binds to the current one."""
    r = asyncio.StreamReader()
    r.feed_data(data)
    r.feed_eof()
    return r


def test_injection_is_refused_when_the_outbound_leg_is_not_tls():
    """A host allowlisted on :80 WITHOUT tls_upgrade would send the token in the
    clear. It must be refused rather than downgraded — and refused loudly: a
    silent anonymous retry reads as 'the repo is public' and fails much later."""
    allow = Allowlist(entries=[
        AllowlistEntry(host="plain.example", port=80, reason="test", category="repo")
    ])
    p = _proxy(allow)
    w = _Writer()

    async def go():
        await p._handle_plain_http(
            "GET", "http://plain.example/x", {"x-dse-inject-credential": "github"},
            _reader(b""), w,
        )

    asyncio.run(go())

    assert b"403" in w.buf
    assert b"ghs_REAL_TOKEN" not in w.buf
    assert p.credential_broker.minted == 0, "a token was minted for a cleartext leg"


def test_loopback_is_the_one_cleartext_leg_a_credential_may_take():
    """Bytes to 127.0.0.1 never reach a network interface, and the proxy's own
    loopback is not reachable from the sandbox — it is a different Pod. Without
    this carve-out the rule would also forbid the local integration fixtures,
    which prove the swap end to end."""
    allow = Allowlist(entries=[
        AllowlistEntry(host="127.0.0.1", port=9, reason="test", category="repo")
    ])
    p = _proxy(allow)
    w = _Writer()

    async def go():
        await p._handle_plain_http(
            "GET", "http://127.0.0.1:9/x", {"x-dse-inject-credential": "github"},
            _reader(b""), w,
        )

    asyncio.run(go())

    # It gets past the injection gate (the connection then fails — port 9 is
    # discard — which is fine; what is pinned is that it was NOT refused at 403).
    assert b"403" not in w.buf
    assert p.credential_broker.minted == 1


# ---------------------------------------------------------------------------
# Chunked bodies — git sends them
# ---------------------------------------------------------------------------

def _body(headers, data):
    p = _proxy()

    async def go():
        return await p._read_body(headers, _reader(data))

    return asyncio.run(go())


def test_chunked_request_body_is_reassembled():
    assert _body({"transfer-encoding": "chunked"}, b"3\r\nwan\r\n2\r\nt \r\n0\r\n\r\n") == b"want "


def test_content_length_body_still_works():
    assert _body({"content-length": "5"}, b"hello") == b"hello"


def test_an_oversized_relayed_body_is_refused_rather_than_buffered():
    huge = str(EgressProxy._MAX_RELAY_BODY_BYTES + 1)
    with pytest.raises(ValueError, match="exceeds"):
        _body({"content-length": huge}, b"")


def test_chunked_body_is_bounded_too():
    """The header-declared length is easy to bound; the chunked path has to
    count as it goes, or the bound is decorative."""
    chunk = b"f" * 65536
    frame = b"%x\r\n%s\r\n" % (len(chunk), chunk)
    n = (EgressProxy._MAX_RELAY_BODY_BYTES // len(chunk)) + 2
    with pytest.raises(ValueError, match="exceeds"):
        _body({"transfer-encoding": "chunked"}, frame * n)


# ---------------------------------------------------------------------------
# What the adversarial pass found: three ways this shipped inert or unsafe
# ---------------------------------------------------------------------------

def test_a_credential_only_goes_to_the_repo_host():
    """Gating on transport alone meant EVERY allowlisted :443 host —
    api.anthropic.com, slack.com, the Jira site, login.microsoftonline.com —
    would receive a real GitHub installation token if the sandbox simply
    addressed it with the header set. The token is scoped to one host; so is
    its use."""
    allow = Allowlist.for_work_item()
    allow.entries.append(AllowlistEntry(host="api.anthropic.com", port=443, reason="x"))
    p = _proxy(allow)
    w = _Writer()

    async def go():
        await p._handle_plain_http(
            "GET", "http://api.anthropic.com:443/v1", {"x-dse-inject-credential": "github"},
            _reader(b""), w,
        )

    asyncio.run(go())

    assert b"403" in w.buf
    assert p.credential_broker.minted == 0
    assert "api.anthropic.com" not in p.credential_hosts
    assert "github.com" in p.credential_hosts


def test_a_fixture_token_is_refused_rather_than_sent():
    """The broker falls back to `fixture-ghtoken-…` when the App credentials are
    absent, and GitHub answers 401 to a bogus credential where it answers 200 to
    an anonymous read. Sending it would break the public-repo clone that works
    today, and say nothing about why."""
    class _Fixture:
        def __init__(self): self.minted = 0
        def mint(self, **_):
            self.minted += 1
            return type("C", (), {"token": "fixture-ghtoken-abc", "credential_id": "c"})()

    p = EgressProxy(
        allowlist=Allowlist.for_work_item(), tenant_id="t",
        work_item_id="wi", credential_broker=_Fixture(),
    )
    w = _Writer()

    async def go():
        await p._handle_plain_http(
            "GET", "http://github.com/a/b.git/info/refs",
            {"x-dse-inject-credential": "github"}, _reader(b""), w,
        )

    asyncio.run(go())

    assert b"403" in w.buf
    assert b"fixture-ghtoken" not in w.buf


def test_the_token_is_sent_as_basic_not_as_the_rest_scheme():
    """`Authorization: token <t>` is the REST API's scheme; github.com's git
    smart-HTTP endpoint answers 401 to it and 200 to Basic with the token as the
    password. Pinned at the header level so a refactor cannot quietly revert to
    the scheme that does not work."""
    import base64 as b64
    import inspect

    src = inspect.getsource(EgressProxy._handle_plain_http)
    assert 'f"Basic {basic}"' in src
    assert 'f"token {cred.token}"' not in src
    # and the encoding is the one GitHub expects
    assert b64.b64encode(b"x-access-token:T").decode() == "eC1hY2Nlc3MtdG9rZW46VA=="


# ---------------------------------------------------------------------------
# Second adversarial pass: two blockers the first fix set introduced
# ---------------------------------------------------------------------------

def test_a_public_repo_still_clones_when_no_token_can_be_minted():
    """Refusing with 403 broke every PUBLIC repo in any environment without
    GitHub App credentials — repos that clone fine without this branch at all.
    An anonymous relay succeeds for a public repo and lets GitHub answer 401
    for a private one, which is a message the operator can act on."""
    class _NoCreds:
        minted = 0
        def mint(self, **_):
            return type("C", (), {"token": "fixture-ghtoken-x", "credential_id": "c"})()

    p = EgressProxy(
        allowlist=Allowlist.for_work_item(), tenant_id="t",
        work_item_id="wi", credential_broker=_NoCreds(),
    )
    w = _Writer()

    async def boom(*_a, **_k):
        raise OSError("no network in this test")

    async def go(monkey):
        monkey.setattr(asyncio, "open_connection", boom)
        await p._handle_plain_http(
            "GET", "http://github.com/psf/requests.git/info/refs",
            {"x-dse-inject-credential": "github"}, _reader(b""), w,
        )

    mp = pytest.MonkeyPatch()
    try:
        asyncio.run(go(mp))
    finally:
        mp.undo()

    # 502 means it got PAST the injection gate and tried to reach the upstream.
    # Our refusal would have been a bare 403 with no attempt at all.
    assert b"502" in w.buf
    assert b"403" not in w.buf
    assert b"fixture-ghtoken" not in w.buf


def test_the_token_is_scoped_to_the_repo_actually_being_fetched():
    """The scope used to come from `X-Dse-Repo`, a header the untrusted sandbox
    writes — so it could hold a token for one repository while fetching another.
    The request path is the authoritative statement of what is being fetched."""
    from egress_proxy.proxy import _repo_from_git_path

    assert _repo_from_git_path("/acme/api.git/info/refs?service=git-upload-pack") == "acme/api"
    assert _repo_from_git_path("/acme/api/git-upload-pack") == "acme/api"
    assert _repo_from_git_path("/not/a/git/path") is None


def test_internal_headers_do_not_reach_the_upstream():
    """Only the placeholder was stripped, so x-dse-repo, x-dse-branch and
    x-dse-credential-id were shipped to GitHub alongside the token."""
    import inspect
    src = inspect.getsource(EgressProxy._handle_plain_http)
    for h in ("x-dse-repo", "x-dse-branch", "x-dse-credential-id"):
        assert f'"{h}"' in src.split("for internal in")[1].split(")")[0]


def test_the_broker_kill_switch_is_actually_read():
    """`DSE_EGRESS_CREDENTIAL_BROKER_ENABLED` is rendered into the chart's
    ConfigMap and was read by nothing — a switch that did not switch."""
    import os
    from egress_proxy.credentials import CredentialBroker

    prev = os.environ.get("DSE_EGRESS_CREDENTIAL_BROKER_ENABLED")
    try:
        os.environ["DSE_EGRESS_CREDENTIAL_BROKER_ENABLED"] = "false"
        assert CredentialBroker.enabled() is False
        os.environ["DSE_EGRESS_CREDENTIAL_BROKER_ENABLED"] = "true"
        assert CredentialBroker.enabled() is True
    finally:
        if prev is None:
            os.environ.pop("DSE_EGRESS_CREDENTIAL_BROKER_ENABLED", None)
        else:
            os.environ["DSE_EGRESS_CREDENTIAL_BROKER_ENABLED"] = prev


def test_the_lease_records_the_permission_that_was_actually_requested():
    """The mint asked for contents:read while the ledger recorded
    contents:write — an audit trail that overstates what was handed out."""
    import inspect
    from egress_proxy import credentials

    src = inspect.getsource(credentials)
    assert '"contents": "read"' in src
    assert 'frozenset({"contents:write"})' not in src


def test_rs256_is_actually_available_to_the_broker():
    """A GitHub App JWT can only be RS256, and RS256 lives in `cryptography` —
    an EXTRA of PyJWT, not part of it. The image declared bare `PyJWT`, so
    `jwt.encode(..., algorithm="RS256")` raised NotImplementedError inside the
    running Pod, the broker swallowed it into a fixture token, and the relay
    went out anonymous: a private repo stayed unclonable with nothing in the
    logs but a generic 401.

    Found by minting a token inside the deployed container. Nothing in the
    source, the manifest or the unit suite could have shown it, because the
    dev venv happens to have `cryptography` pulled in by something else."""
    import jwt

    jwt.get_algorithm_by_name("RS256")  # raises NotImplementedError if absent
