"""WS-A Microsoft Teams adapter — PROVISIONED (Phase 4, orphan scope).

Mirrors adapter-slack/adapter-github/adapter-jira: inbound (Teams
message/mention -> ConversationEvent through the 4 intake defenses) and outbound
(a single status message, edited in-place, via `MutableCommentWriter` with the
Teams backend).

NOT ACTIVATED: turning Teams on is a business/roadmap decision (Phase 4+). The
only code-level blocker for activation is the foundation (`Platform`/platform
CHECKs in packages/contracts + migrations 0001), which this workstream does not
edit in this session. See README.md and activation.sql."""
