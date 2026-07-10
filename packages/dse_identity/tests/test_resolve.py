"""Requer `make up && make migrate` rodando (Postgres real)."""
import uuid

from dse_identity import resolve_principal


def test_resolve_is_idempotent_for_same_platform_user():
    uid = f"U{uuid.uuid4().hex[:10]}"
    p1 = resolve_principal("slack", uid, display_name="Ada")
    p2 = resolve_principal("slack", uid, display_name="Ada")
    assert p1 == p2


def test_different_platforms_same_raw_id_get_distinct_principals():
    raw_id = uuid.uuid4().hex[:10]
    p_slack = resolve_principal("slack", raw_id)
    p_github = resolve_principal("github", raw_id)
    assert p_slack != p_github
