"""Ids and event builders shared by the tests."""
from __future__ import annotations

BOT_ID = "B" * 43
USER_ID = "U" * 43
OTHER_ID = "O" * 43


def message(text="hi", *, chat_id=USER_ID, chat_type="dm", sender=USER_ID, msg_id="m1", **extra) -> dict:
    return {"type": "message", "seq": 1, "message": {
        "id": msg_id, "date": 1764000000000, "text": text,
        "chat": {"id": chat_id, "type": chat_type, "title": "Ana" if chat_type == "dm" else "Team"},
        "from": {"id": sender, "username": "ana", "name": "Ana", "is_bot": False}, **extra}}


def tap(data, *, message_id="m-card", sender=USER_ID, chat_id=USER_ID, chat_type="dm", **extra) -> dict:
    return {"type": "button", "query_id": "q1", "chat": {"id": chat_id, "type": chat_type, "title": "Ana"},
            "message_id": message_id, "from": {"id": sender, "username": "ana", "name": "Ana", "is_bot": False},
            "data": data, **extra}
