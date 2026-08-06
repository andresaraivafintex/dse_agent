"""Which repositories a tenant has — asked identically everywhere.

Three services need this answer and each had its own copy of the query. They
drifted, and the drift routed a frontend request to the backend.

`repo_bindings` is NOT "the tenant's repositories": it has one row per BINDING,
so a repository nobody bound to a channel or a Jira project is invisible in it.
`repo_profiles` is the table that means "these are the repositories, and this is
what they are". The honest candidate set is the union.

The router's copy was fixed to use that union in #56. The INGEST GATEWAY's copy
was not, and it runs FIRST: `repo_resolver.resolve_repo` Rung 4 counted
`repo_bindings` alone, found exactly one row (a Jira binding to the backend),
concluded "this tenant has a single repository" and returned the backend for
"show a coloured badge on the reports dashboard" — a frontend-only request. The
router was never consulted, so there is no `repo_routing_decided` row to explain
it; the item just started implementing against the wrong repository.

The Slack adapter carried a third copy, whose docstring still said it "mirrors
the source that resolve_repo Rung 4/5 deemed ambiguous". It stopped mirroring it
the day the router was fixed, and nothing could have noticed.

So the SQL lives here, in the one package all three already depend on. Both
shapes are in this file deliberately: a change to what counts as a tenant's
repository has to be made once, not found in three places by a routing bug.
"""
from __future__ import annotations

#: Every repository the tenant has, one column, ordered so callers that render
#: a picker are deterministic. Takes a single named parameter `t` (tenant_id).
TENANT_REPOS_SQL = """
    SELECT DISTINCT repo
      FROM (
            SELECT repo FROM repo_bindings
             WHERE tenant_id = %(t)s AND repo IS NOT NULL
            UNION
            SELECT repo FROM repo_profiles
             WHERE tenant_id = %(t)s
           ) AS all_repos
     ORDER BY repo
"""

#: The same set, with what the router needs to tell the repositories apart.
#: `repo_bindings` carries no description, hence the empty literals and the
#: MAX() — a repo present in both tables keeps the profile's text.
TENANT_REPO_CATALOGUE_SQL = """
    SELECT repo,
           MAX(role) AS role, MAX(language) AS language, MAX(description) AS description
      FROM (
            SELECT repo, '' AS role, '' AS language, '' AS description
              FROM repo_bindings
             WHERE tenant_id = %(t)s AND repo IS NOT NULL
            UNION ALL
            SELECT repo, role, language, description
              FROM repo_profiles
             WHERE tenant_id = %(t)s
           ) AS all_repos
     GROUP BY repo
     ORDER BY repo
"""
