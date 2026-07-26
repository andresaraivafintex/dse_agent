"""PR title presentation: source-platform markup must not leak into GitHub.

The summary is the task text exactly as a human typed it on the origin platform,
so it carries markup that only renders there. What lands in a PR title has to
read like something a person wrote — the title is the first (often only) thing a
reviewer sees in a PR list.
"""
from __future__ import annotations

from dse_validation.github.pr_finalizer import (
    PR_TITLE_TEMPLATE,
    clean_summary,
    short_work_item_id,
)

WORK_ITEM_ID = "wi_624f69e53efdc13d62df42cd9721f32c891e0bf06e64a3ef76f13c92278b28be"


def test_slack_mention_never_reaches_the_pr_title():
    """Regression: PR #27 shipped titled
    `[DSE wi_624f69e5...278b28be] <@U0BJR6U90UF> please change the title...` —
    a 64-char hash plus a raw Slack id that resolves to nothing on GitHub."""
    title = PR_TITLE_TEMPLATE.format(
        work_item_id=short_work_item_id(WORK_ITEM_ID),
        summary=clean_summary('<@U0BJR6U90UF> please change the title to "Demo Example"'),
    )
    assert title == '[DSE wi_624f69e5] please change the title to "Demo Example"'


def test_slack_links_keep_their_label_not_their_markup():
    assert clean_summary("fix <https://example.com/x|the docs page>") == "fix the docs page"
    assert clean_summary("see <https://example.com/a>") == "see https://example.com/a"


def test_channel_references_are_stripped():
    assert clean_summary("ping <#C0BKA7TMMEY|test-dse> about it") == "ping about it"


def test_summary_made_only_of_markup_still_yields_a_title():
    """GitHub rejects an empty PR title, so cleaning must never empty the
    summary — an ugly title beats a failed PR at the end of a green run."""
    assert clean_summary("<@U123ABC>") == "<@U123ABC>"


def test_short_id_only_shortens_real_work_item_ids():
    assert short_work_item_id(WORK_ITEM_ID) == "wi_624f69e5"
    assert short_work_item_id("legacy-123") == "legacy-123"
