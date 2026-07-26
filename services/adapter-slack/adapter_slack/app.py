"""WSA-E3-T1/T2 — Slack adapter: inbound (Events API + Interactivity) and
outbound (a single status message, edited in-place). 100% stateless adapter:
no state lives in the process — everything (comment_ref, kill switch,
allowlist, work_items) is read from/written to the shared Postgres on every
request.

Inbound pipeline, in order (the WSA-E2 "4 defenses"):
  1. verify_slack_signature (HMAC + replay window)          -> 401 on failure
  2. content_snapshot frozen from the payload itself (TOCTOU) -> automatic
  3. sanitize_content (invisible unicode + secret redaction)
  4. idempotency: deterministic event_id -> dedup in admit_work_item/
     record_signal_event via a UNIQUE constraint
after that: correlate() decides Path A (new_task) vs Path B (signal) vs
unauthorized (steering allowlist).
"""
from __future__ import annotations

import json
import logging

from dse_audit import emit as audit_emit
from dse_contracts import mutable_comment
from dse_identity import resolve_principal
from fastapi import FastAPI, HTTPException, Request
from ingest_gateway import (
    AdmissionBlocked,
    admit_work_item,
    already_ingested,
    correlate,
    get_connection,
    is_authorized_to_steer,
    pending_reply_work_items,
    record_signal_event,
    resolve_tenant,
    resolve_repo,
    sanitize_content,
    verify_slack_signature,
)
from pydantic import BaseModel

from .backend import SlackCommentBackend, approval_blocks, build_real_slack_client, repo_select_blocks
from .comment_store import SURFACE, PgCommentStateStore
from .config import get_slack_bot_token, get_slack_signing_secret, get_tenant_id
from .events import (
    build_event_from_app_mention,
    build_event_from_block_action,
    build_event_from_thread_message,
    build_repo_select_signal_event,
    parse_slack_approval,
)

logger = logging.getLogger("adapter_slack")

app = FastAPI(title="dse-adapter-slack")


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "adapter-slack"}


def _reject(reason: str, *, surface: str) -> None:
    audit_emit(
        actor="system:adapter-slack",
        action="signature_rejected",
        tenant_id=get_tenant_id(),
        details={"reason": reason, "surface": surface},
    )
    raise HTTPException(status_code=401, detail=f"signature_verification_failed:{reason}")


def _resolve_tenant_for(team_id: str | None) -> str:
    """WSA-E1-T5 — resolves the tenant from the Slack workspace (`team_id`)
    via `tenant_platform_bindings`. A missing binding falls back to
    `DSE_TENANT_ID` with a warning audit row (documented single-tenant
    fallback)."""
    conn = get_connection()
    try:
        rt = resolve_tenant(conn, platform="slack", binding_key=team_id)
        conn.commit()
        return rt.tenant_id
    finally:
        conn.close()


def _distinct_repos_for_tenant(conn, tenant_id: str) -> list[str]:
    """Distinct repos of the tenant — mirrors the source that resolve_repo
    Rung 4/5 deemed ambiguous (same WHERE, no platform filter). Ordered ->
    deterministic Block Kit."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT repo FROM repo_bindings "
            "WHERE tenant_id = %s AND repo IS NOT NULL ORDER BY repo",
            (tenant_id,),
        )
        return [r[0] for r in cur.fetchall()]


def _base_branch_for_repo(conn, tenant_id: str, repo: str) -> str:
    """base_branch from the binding of the chosen repo (the ambiguous repo did
    not carry one). Defaults to 'main' (resolve_repo Rung 1 convention)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT base_branch FROM repo_bindings "
            "WHERE tenant_id = %s AND repo = %s AND base_branch IS NOT NULL LIMIT 1",
            (tenant_id, repo),
        )
        row = cur.fetchone()
    return row[0] if row and row[0] else "main"


def _handle_conversation_event(conv_event, *, principal: str, tenant_id: str,
                               extra_payload: dict | None = None,
                               signal_only: bool = False) -> dict:
    channel = conv_event.source_ref["channel"]
    sanitized = sanitize_content(conv_event.content_snapshot)

    conn = get_connection()
    try:
        # Recovery sweeps re-read whole threads, so a task that is genuinely
        # waiting meets the same messages on every cycle. Recording dedupes on
        # `event_id`, but only after correlating and auditing — on Jira that
        # turned one stuck ticket into thousands of `signal_duplicate_ignored`
        # rows. Nothing below can change an outcome already reached.
        if already_ingested(conn, conv_event.event_id):
            return {"ok": True, "path": "already_ingested"}

        result = correlate(conn, tenant_id=tenant_id, event=conv_event, requester_principal=principal)

        if result.kind == "unauthorized":
            conn.commit()
            return {"ok": True, "path": "unauthorized"}

        # `signal_only` is the reconciler's leash (/internal/reconcile): that
        # caller recovers REPLIES to an existing task and must never manufacture
        # work. Without it, a thread that stopped correlating — the item raced
        # into a terminal status, or its source_ref does not match — would fall
        # into the Path A branch below and admit ONE NEW TASK PER MESSAGE in the
        # thread, turning a recovery sweep into a task storm. The webhook path
        # leaves this off: there, a message that correlates to nothing genuinely
        # is a new task.
        if signal_only and result.kind != "signal":
            conn.commit()
            return {"ok": True, "path": "not_correlated"}

        if result.kind == "signal":
            # `recorded` is False when the event_id already existed (dedup). The
            # reconciler needs the distinction to count/audit only what it truly
            # recovered — re-reading a thread every cycle must not inflate the
            # trail with replies that arrived normally.
            recorded = record_signal_event(
                conv_event,
                tenant_id=tenant_id,
                channel=channel,
                work_item_id=result.work_item_id,
                sanitized_content=sanitized,
                extra_payload=extra_payload,
                conn=conn,
            )
            return {"ok": True, "path": "signal", "work_item_id": result.work_item_id,
                    "recorded": recorded}

        # Path A: new_task — C2 (report 07): resolves the repo through the
        # cascade (explicit override in the text → channel binding → tenant
        # default). With no resolution, repo=None and the clarification gate
        # asks (it never guesses). The text used is the SANITIZED one (never
        # the raw one).
        repo, base_branch = resolve_repo(
            conn, tenant_id=tenant_id, platform="slack",
            signals={"text": sanitized, "channel": channel},
        )
        try:
            work_item_id = admit_work_item(
                conv_event,
                tenant_id=tenant_id,
                source="slack",
                channel=channel,
                repo=repo,
                base_branch=base_branch,
                requester_principal=principal,
                sanitized_content=sanitized,
                conn=conn,
            )
        except AdmissionBlocked:
            return {"ok": True, "path": "blocked_kill_switch"}

        if result.provenance_work_item_id:
            audit_emit(
                actor=principal,
                action="work_item_provenance_link",
                tenant_id=tenant_id,
                work_item_id=work_item_id,
                details={"previous_work_item_id": result.provenance_work_item_id},
            )

        return {"ok": True, "path": "new_task", "work_item_id": work_item_id}
    finally:
        conn.close()


@app.post("/slack/events")
async def slack_events(request: Request) -> dict:
    body = await request.body()
    timestamp = request.headers.get("X-Slack-Request-Timestamp")
    signature = request.headers.get("X-Slack-Signature")

    check = verify_slack_signature(
        signing_secret=get_slack_signing_secret(),
        timestamp_header=timestamp,
        body=body,
        signature_header=signature,
    )
    if not check.verified:
        _reject(check.reason, surface="slack_events")

    payload = json.loads(body)

    if payload.get("type") == "url_verification":
        return {"challenge": payload["challenge"]}

    if payload.get("type") != "event_callback":
        return {"ok": True}

    event = payload["event"]
    event_type = event.get("type")
    user_id = event.get("user")
    if not user_id:
        return {"ok": True}  # events with no user (e.g. bot_message) are ignored in Phase 1

    principal = resolve_principal("slack", user_id)
    tenant_id = _resolve_tenant_for(payload.get("team_id"))

    if event_type == "app_mention":
        conv_event = build_event_from_app_mention(event, resolved_principal=principal)
    elif event_type == "message" and not event.get("subtype") and event.get("thread_ts"):
        conv_event = build_event_from_thread_message(event, resolved_principal=principal)
    else:
        return {"ok": True}  # event type not covered in Phase 1

    return _handle_conversation_event(conv_event, principal=principal, tenant_id=tenant_id)


def _selected_repo_from_state(payload: dict, block_id: str, work_item_id: str) -> str | None:
    """Repo chosen in the static_select, read from the message `state`.

    On the confirm-button click the `action` describes the BUTTON — it does not
    carry `selected_option`. Slack ships along the current state of the
    message's stateful elements in `state.values`, indexed by
    block_id -> action_id (documented for MESSAGE `block_actions` since 2020,
    not just for modals). That is where the choice comes from — which is what
    makes the select+confirm pair possible without keeping a pending selection
    server-side. A button is stateless: it does not show up here and therefore
    does not pollute the read.

    `state` is opportunistic, not durable: it may arrive absent, without the
    block, or with `selected_option: null` (deselection, or a message re-render
    that wipes the choice). All three cases return None — it never guesses a
    repo; the caller warns the human instead of failing mute.

    The safety net for a click with no `block_id` matches on the
    `:<work_item_id>` SUFFIX instead of scanning everything: if the message
    ever carries two selectors, the choice is never paired with the wrong work
    item."""
    values = (payload.get("state") or {}).get("values") or {}
    if block_id in values:
        blocks = [values[block_id]]
    else:
        blocks = [v for k, v in values.items() if work_item_id and k.endswith(f":{work_item_id}")]
    for block in blocks:
        selected = ((block or {}).get("dse_repo_select") or {}).get("selected_option") or {}
        if selected.get("value"):
            return selected["value"]
    return None


def _notify_ephemeral(channel: str, user_id: str, text: str) -> None:
    """Notice visible only to whoever clicked (`chat.postEphemeral`).

    Without this, a Confirm with no selection — or with the selection lost
    because the message was re-rendered — fails in ABSOLUTE silence: the human
    clicks, nothing happens, and there is no hint whatsoever as to why.
    Best-effort on purpose: a Slack failure here must not take down the
    interaction nor undo the signal that was already recorded."""
    try:
        build_real_slack_client(get_slack_bot_token()).chat_postEphemeral(
            channel=channel, user=user_id, text=text
        )
    except Exception:  # noqa: BLE001 — feedback is ancillary, never fatal
        logger.warning("chat_postEphemeral failed (repo selector feedback)", exc_info=True)


@app.post("/slack/interactions")
async def slack_interactions(request: Request) -> dict:
    body = await request.body()
    timestamp = request.headers.get("X-Slack-Request-Timestamp")
    signature = request.headers.get("X-Slack-Signature")

    check = verify_slack_signature(
        signing_secret=get_slack_signing_secret(),
        timestamp_header=timestamp,
        body=body,
        signature_header=signature,
    )
    if not check.verified:
        _reject(check.reason, surface="slack_interactions")

    form = await request.form()
    payload = json.loads(form["payload"])

    if payload.get("type") != "block_actions":
        return {"ok": True}

    user_id = payload["user"]["id"]
    principal = resolve_principal("slack", user_id)
    team_id = (payload.get("team") or {}).get("id") or payload.get("user", {}).get("team_id")
    tenant_id = _resolve_tenant_for(team_id)
    action = payload["actions"][0]

    # TWO-step repo selection. Slack fires `block_actions` as soon as the
    # static_select is picked; treating that as the decision would make the
    # first click irreversible — getting the repo wrong would fire an agent turn
    # against the wrong repo. So the select merely stages (the choice sits in
    # the message `state`) and only the button promotes it to a signal.
    if action.get("action_id") == "dse_repo_select":
        # No-op ack: an empty 200 keeps the Slack client from flagging the
        # interaction as failed, and nothing is recorded until the Confirm.
        return {"ok": True, "path": "repo_select_staged"}

    # Repo confirmation (ambiguous-repo clarification): this is NOT an approval.
    # Addressed by the work_item_id in the block_id (not by correlation — the
    # status-comment is posted OUTSIDE the thread). The repo+base_branch become
    # the `repo=X branch=Y` marker in the content -> the dispatcher extracts it
    # (C4 regex) -> clarification_answer SIGNAL -> the workflow refills
    # input.repo/base_branch. Identical effect to typing
    # `repo=org/x branch=main` in the thread.
    if action.get("action_id") == "dse_repo_confirm":
        block_id = action.get("block_id", "")
        work_item_id = block_id.split(":", 1)[1] if ":" in block_id else action.get("value", "")
        channel = payload["channel"]["id"]
        repo = _selected_repo_from_state(payload, block_id, work_item_id)
        if not work_item_id or not repo:
            # Confirm with no valid choice: either nothing was selected, or the
            # message `state` was lost. Nothing is recorded — but the human
            # NEEDS to know, otherwise they keep clicking a mute button with no
            # idea why.
            _notify_ephemeral(
                channel, user_id,
                "Pick a repository from the menu, then hit *Confirm*.",
            )
            return {"ok": True, "path": "repo_select_noop"}
        # Security parity with correlate's clarification_answer gate (steering
        # allowlist). Without this, anyone in the channel could pick the repo.
        if not is_authorized_to_steer(tenant_id, principal):
            audit_emit(actor=principal, action="steering_rejected_unauthorized",
                       tenant_id=tenant_id,
                       details={"kind": "repo_select", "work_item_id": work_item_id})
            # The gate denies by default; whoever clicked has to know they were
            # refused, not that the button is broken. This leaks nothing: the
            # person is already in the channel and already saw the message.
            _notify_ephemeral(
                channel, user_id,
                "You don't have permission to choose the repository for this task.",
            )
            return {"ok": True, "path": "unauthorized"}
        conn = get_connection()
        try:
            # The repo arrives via the message `state`; confining it to the
            # tenant's list guarantees that only a repo WE offered can become the
            # target of an agent turn — the handler never accepts a repo it did
            # not offer.
            if repo not in _distinct_repos_for_tenant(conn, tenant_id):
                audit_emit(actor=principal, action="repo_select_rejected_unknown_repo",
                           tenant_id=tenant_id,
                           details={"work_item_id": work_item_id, "repo": repo})
                _notify_ephemeral(
                    channel, user_id,
                    f"`{repo}` isn't a repository registered in this workspace.",
                )
                return {"ok": True, "path": "unknown_repo"}
            content = f"repo={repo} branch={_base_branch_for_repo(conn, tenant_id, repo)}"
            conv_event = build_repo_select_signal_event(
                payload, action, resolved_principal=principal, content=content
            )
            record_signal_event(
                conv_event, tenant_id=tenant_id, channel=channel,
                work_item_id=work_item_id, sanitized_content=content, conn=conn,
            )
            conn.commit()  # persists the ingest_event for the dispatcher to drain
        except AdmissionBlocked:
            return {"ok": True, "path": "blocked_kill_switch"}
        finally:
            conn.close()
        # Acknowledges receipt to whoever clicked. Beyond courtesy, this is what
        # prevents repeated clicks from someone who got no visual feedback —
        # every extra Confirm would become another clarification_answer on the
        # same work item.
        _notify_ephemeral(channel, user_id, f"✅ Using *{repo}* — starting work now.")
        return {"ok": True, "path": "repo_selected", "work_item_id": work_item_id, "repo": repo}

    conv_event = build_event_from_block_action(payload, resolved_principal=principal)

    # C1 (report 07): derives the button's verdict/route into DETERMINISTIC
    # markers — without this the dispatcher defaults to `approved` and a
    # "reject" would silently approve the plan (gate security bug).
    verdict, route = parse_slack_approval(action.get("action_id", ""), action.get("value", ""))
    extra_payload: dict = {"approval_verdict": verdict}
    if route:
        extra_payload["approval_route"] = route

    return _handle_conversation_event(
        conv_event, principal=principal, tenant_id=tenant_id, extra_payload=extra_payload
    )


class StatusCommentRequest(BaseModel):
    work_item_id: str
    channel: str
    body: str
    actor: str  # resolved principal of who/what triggered the update (e.g. "system:orchestrator")
    status: str | None = None  # Phase B: when 'awaiting_plan_approval', builds the buttons


@app.post("/internal/status-comment")
def upsert_status_comment(req: StatusCommentRequest) -> dict:
    """WSA-E3-T2: exactly 1 status message per WorkItem, edited in-place —
    called by the orchestrator (WS-B) on every relevant state transition.
    Uses the shared `MutableCommentWriter` (dse_contracts).

    Phase B (report 07): on status `awaiting_plan_approval` the message goes
    out with Block Kit (Approve/Reject buttons) — the same mutable message,
    only interactive. The clicks come back via /slack/interactions (verdict via
    C1)."""
    client = build_real_slack_client(get_slack_bot_token())
    backend = SlackCommentBackend(client)
    store = PgCommentStateStore()
    writer = mutable_comment.MutableCommentWriter(backend, store, SURFACE)

    surface_ref = {"channel": req.channel}
    if req.status == "awaiting_plan_approval":
        surface_ref["blocks"] = approval_blocks(req.body)
    elif req.status == "awaiting_repo_selection":
        # Ambiguous repo: offer a static_select with the tenant's repos. With < 2
        # repos it degrades to plain text (nothing to pick -> just the text
        # question).
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT tenant_id FROM work_items WHERE id = %s", (req.work_item_id,))
                row = cur.fetchone()
            tenant_id = row[0] if row else get_tenant_id()
            repos = _distinct_repos_for_tenant(conn, tenant_id)
            conn.commit()
        finally:
            conn.close()
        if len(repos) >= 2:
            surface_ref["blocks"] = repo_select_blocks(req.work_item_id, repos, req.body)
    comment_ref = writer.upsert(req.work_item_id, surface_ref, req.body)

    audit_emit(
        actor=req.actor,
        action="status_comment_upserted",
        tenant_id=get_tenant_id(),
        work_item_id=req.work_item_id,
        details={"surface": SURFACE, "channel": req.channel},
    )
    return {"ok": True, "comment_ref": comment_ref}


_RECONCILER_ACTOR = "system:adapter-slack-reconciler"


def _is_recoverable_reply(message: dict, thread_ts: str) -> bool:
    """True for a thread message the WEBHOOK path would have ingested.

    Parity with /slack/events is the entire rule: the reconciler exists to make
    up for a lost delivery, not to widen what counts as an answer. So it drops
    exactly what the webhook drops — messages from the bot itself (`bot_id`, or
    no `user` at all, which is how Slack shapes bot/system messages) and
    anything carrying a `subtype` (channel joins, file shares, message-changed).

    The thread ROOT is dropped on top of that: it is the original task_request,
    already ingested when the task was created. Re-reading it as a reply would
    feed the task its own opening line back as an answer."""
    if message.get("bot_id") or not message.get("user"):
        return False
    if message.get("subtype"):
        return False
    ts = message.get("ts")
    return bool(ts) and ts != thread_ts


def _recover_missed_replies(client, item: dict, *, tenant_id: str) -> int:
    """Re-reads ONE blocked thread and ingests whatever never arrived.

    Every recovered message goes through `_handle_conversation_event` — the very
    function the webhook calls — so sanitization, correlation, the steering gate
    and the `record_signal_event` outbox write are the same code, not a parallel
    copy that will drift.

    Idempotency is free and deliberate: `event_id` is derived from
    platform+thread+message ts, identical to what the webhook would have
    produced, so a reply that DID arrive collides on the UNIQUE constraint and is
    dropped by the existing dedup. That is why re-reading the whole thread on
    every cycle is correct rather than merely tolerable — and why only
    `recorded=True` counts as a recovery.

    Re-reading is the operation the TOCTOU defense (WSA-E2-T2) forbids for
    APPROVALS, and this path never touches one: the caller only ever gets work
    items blocked on a clarification reply, and the events built here are
    `clarification_answer` by construction (`build_event_from_thread_message`)."""
    source_ref = item.get("source_ref") or {}
    channel, thread_ts = source_ref.get("channel"), source_ref.get("thread_ts")
    if not channel or not thread_ts:
        return 0  # nothing to re-read: this item was never anchored to a thread

    messages = client.conversations_replies(channel=channel, ts=thread_ts).get("messages") or []
    recovered = 0
    for message in messages:
        if not _is_recoverable_reply(message, thread_ts):
            continue
        principal = resolve_principal("slack", message["user"])
        conv_event = build_event_from_thread_message(
            {
                "channel": channel,
                "ts": message["ts"],
                "thread_ts": thread_ts,
                "user": message["user"],
                "text": message.get("text", ""),
            },
            resolved_principal=principal,
        )
        result = _handle_conversation_event(
            conv_event, principal=principal, tenant_id=tenant_id, signal_only=True
        )
        if not result.get("recorded"):
            continue  # already ingested, refused by the steering gate, or no longer correlated
        recovered += 1
        # The trail has to say "this came in through recovery, not through a
        # signed webhook" — an event ingested from a re-read message is a
        # different provenance claim, and an auditor must be able to tell them
        # apart without inferring it from timestamps. The ACTOR is the
        # reconciler, not the human: they wrote the reply, they did not trigger
        # the sweep. Who wrote it stays in `author` (and in the event itself).
        audit_emit(
            actor=_RECONCILER_ACTOR,
            action="reply_recovered",
            tenant_id=tenant_id,
            work_item_id=result["work_item_id"],
            details={
                "surface": "slack",
                "channel": channel,
                "thread_ts": thread_ts,
                "message_ts": message["ts"],
                "event_id": conv_event.event_id,
                "author": principal,
                "blocked_status": item.get("status"),
            },
        )
    return recovered


class ReconcileRequest(BaseModel):
    tenant_id: str | None = None  # defaults to the adapter's tenant (Phase 1: single tenant)
    limit: int = 50  # blast-radius guard: crawl instead of stampeding the Slack API


@app.post("/internal/reconcile")
def reconcile_missed_replies(req: ReconcileRequest | None = None) -> dict:
    """Recovers clarification replies whose delivery to this adapter was lost.

    Observed twice in one afternoon (BD-40, BD-41): a human answers the
    question, the webhook never lands (adapter down, delivery failed), and the
    task sits in `needs_clarification` FOREVER in complete silence — both times
    unblocked by hand with an UPDATE on the database. Delivery is not something
    to keep betting on: for the handful of items blocked waiting on a human,
    this re-reads the thread and ingests what was missed.

    Deliberately NOT recovered: plan approvals. `pending_reply_work_items` only
    returns reply-blocked statuses and excludes `awaiting_plan_approval`,
    because a recovered approval is a decision manufactured from text nobody
    signed — the exact attack the TOCTOU defense exists to stop (post something
    benign, get it approved, edit afterwards). A lost approval stays lost and a
    human re-approves; that is the correct failure mode.

    Best-effort end to end: an unreadable thread (Slack error, deleted channel,
    malformed row) is logged and skipped so it cannot blind the rest of the
    sweep. Nothing here answers 5xx either — a caller on a timer would only
    retry into the same failure, and `ok: False` says more than a stack trace at
    the other end. Same contract as the GitHub adapter's reconciler, so one
    scheduled caller can read both the same way."""
    tenant_id = (req.tenant_id if req else None) or get_tenant_id()
    limit = req.limit if req else 50

    try:
        conn = get_connection()
        try:
            items = pending_reply_work_items(
                conn, tenant_id=tenant_id, source="slack", limit=limit
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — a broken sweep must not become a 5xx loop
        logger.exception("reconcile: could not list the work items awaiting a reply")
        return {"ok": False, "checked": 0, "recovered": 0}

    if not items:  # nothing blocked -> no reason to hold a Slack token, let alone call the API
        return {"ok": True, "checked": 0, "recovered": 0}

    try:
        client = build_real_slack_client(get_slack_bot_token())
    except Exception:  # noqa: BLE001
        logger.exception("reconcile: could not build the Slack client")
        return {"ok": False, "checked": 0, "recovered": 0}

    recovered = 0
    for item in items:
        try:
            recovered += _recover_missed_replies(client, item, tenant_id=tenant_id)
        except Exception:  # noqa: BLE001 — one bad item must not abort the sweep
            logger.exception(
                "reconcile: could not recover %s; continuing the sweep",
                item.get("work_item_id"),
            )

    return {"ok": True, "checked": len(items), "recovered": recovered}
