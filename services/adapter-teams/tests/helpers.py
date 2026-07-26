from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any


def teams_auth_header(secret_b64: str, body: bytes) -> str:
    """Reproduces the Teams outgoing webhook signature:
    `HMAC <base64(HMAC_SHA256(decoded_secret, body))>`."""
    key = base64.b64decode(secret_b64)
    digest = hmac.new(key, body, hashlib.sha256).digest()
    return f"HMAC {base64.b64encode(digest).decode()}"


def teams_activity(
    *,
    text: str = "<at>DSE Bot</at> please fix the login bug",
    activity_id: str = "act-0001",
    conversation_id: str = "19:channel_abc@thread.tacv2",
    from_id: str = "29:aad-user-jane",
    from_name: str = "Jane Doe",
    aad_object_id: str | None = "aad-obj-jane",
    tenant_guid: str = "aad-tenant-guid-xyz",
    service_url: str = "https://smba.trafficmanager.net/emea/",
    reply_to_id: str | None = None,
    with_mention_entity: bool = False,
) -> dict[str, Any]:
    activity: dict[str, Any] = {
        "type": "message",
        "id": activity_id,
        "text": text,
        "from": {"id": from_id, "name": from_name},
        "recipient": {"id": "28:bot-dse", "name": "DSE Bot"},
        "conversation": {"id": conversation_id, "conversationType": "channel"},
        "channelData": {"tenant": {"id": tenant_guid}},
        "serviceUrl": service_url,
    }
    if aad_object_id is not None:
        activity["from"]["aadObjectId"] = aad_object_id
    if reply_to_id is not None:
        activity["replyToId"] = reply_to_id
    if with_mention_entity:
        activity["entities"] = [{"type": "mention", "mentioned": {"id": "28:bot-dse"}}]
    return activity


def activity_body(**kwargs) -> bytes:
    return json.dumps(teams_activity(**kwargs)).encode()
