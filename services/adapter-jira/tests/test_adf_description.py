"""The Jira description must survive ingestion.

Jira Cloud's REST v3 returns `description` as ADF (a dict), never a plain
string. The adapter used to keep it only `if isinstance(description, str)` —
which is never true against the real API — so every task arrived carrying just
its summary.

That is where the acceptance criteria live. Dropping the description made the
completeness gate ask for criteria on a ticket that already stated them, and on
BD-40 that question was then read back as its own answer, so the Coder changed
nothing, the Tester failed a correct test, and the run died at the retry cap.
One dropped field, four stages of damage.
"""
from __future__ import annotations

from adapter_jira import events

ADF = {
    "type": "doc",
    "version": 1,
    "content": [
        {
            "type": "paragraph",
            "content": [{"type": "text", "text": "The background must be yellow on every page."}],
        }
    ],
}


def _issue(description):
    return {"fields": {"summary": "Change BG to yellow", "description": description}}


def test_adf_description_is_flattened_not_dropped():
    content = events._issue_content(_issue(ADF))
    assert "The background must be yellow on every page." in content
    assert content.startswith("Change BG to yellow")


def test_plain_string_description_still_works():
    """The webhook payload can carry plain text — both shapes must survive."""
    assert "plain text body" in events._issue_content(_issue("plain text body"))


def test_missing_description_leaves_just_the_summary():
    assert events._issue_content(_issue(None)) == "Change BG to yellow"


def test_flattened_description_clears_the_completeness_bar():
    """The gate treats a body under 40 chars as "no acceptance criteria". A
    summary alone is almost always under it — which is exactly why the dropped
    description turned every ticket into a clarification request."""
    content = events._issue_content(_issue(ADF))
    assert len(content) >= 40, content
    assert len(events._issue_content(_issue(None))) < 40
