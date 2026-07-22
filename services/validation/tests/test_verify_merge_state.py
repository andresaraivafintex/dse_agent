"""Plano 08 §F (F1) — verificação do merge contra a API do GitHub.

Lógica pura de `_verify_merge_state` com um cliente GitHub falso (sem rede):
um webhook forjado (PR não merged / inexistente / sha divergente) NÃO é
verificado; um PR de fato merged é. Fail-safe: erro de API => verified=False."""
from __future__ import annotations

from dse_contracts.activities import VerifyMergeInput
from dse_validation.activities import _verify_merge_state


class _FakeClient:
    def __init__(self, pr):
        self._pr = pr

    def get_pull_request(self, repo, pr_number):
        if callable(self._pr):
            return self._pr(repo, pr_number)
        return self._pr


def _inp(**kw):
    base = dict(work_item_id="wi1", tenant_id="t1", repo="acme/app", pr_number=7)
    base.update(kw)
    return VerifyMergeInput(**base)


def test_real_merged_pr_is_verified():
    client = _FakeClient({"number": 7, "state": "closed", "merged": True,
                          "merged_by": "alice", "merge_commit_sha": "abc", "head_sha": "h1"})
    v = _verify_merge_state(_inp(expected_head_sha="h1"), github_client=client)
    assert v.verified and v.merged and v.merged_by == "alice"


def test_forged_webhook_pr_not_merged_is_rejected():
    # o PR existe mas NÃO está merged — webhook forjado não conclui a tarefa
    client = _FakeClient({"number": 7, "state": "open", "merged": False,
                          "merged_by": None, "merge_commit_sha": None, "head_sha": "h1"})
    v = _verify_merge_state(_inp(), github_client=client)
    assert not v.verified and v.exists and v.reason.startswith("not_merged")


def test_pr_not_found_is_rejected():
    client = _FakeClient(None)  # 404 → None
    v = _verify_merge_state(_inp(), github_client=client)
    assert not v.verified and not v.exists and v.reason == "pr_not_found"


def test_head_sha_mismatch_is_rejected():
    # merged, mas de um SHA diferente do que o workflow validou (replay/forja)
    client = _FakeClient({"number": 7, "state": "closed", "merged": True,
                          "merged_by": "alice", "merge_commit_sha": "abc", "head_sha": "OTHER"})
    v = _verify_merge_state(_inp(expected_head_sha="h1"), github_client=client)
    assert not v.verified and v.reason == "head_sha_mismatch"


def test_api_error_is_failsafe_unverified():
    def _boom(repo, pr_number):
        raise RuntimeError("network down")
    v = _verify_merge_state(_inp(), github_client=_FakeClient(_boom))
    assert not v.verified and v.reason.startswith("api_error")
