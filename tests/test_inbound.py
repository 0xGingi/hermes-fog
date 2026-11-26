"""Inbound events -> MessageEvent, group gating, threads, commands, platform events."""
from __future__ import annotations

import asyncio

from fog_helpers import BOT_ID, OTHER_ID, USER_ID, message
from gateway.platforms.event import MessageType


async def test_dm_message_becomes_event(adapter, daemon):
    await adapter._dispatch(message("hello **bot**", reply_to={"id": "m0", "text": "earlier",
                                                                 "from": {"id": BOT_ID, "name": "Hermes"}}))
    assert len(adapter.handled) == 1
    ev = adapter.handled[0]
    assert ev.text == "hello **bot**"
    assert ev.message_type == MessageType.TEXT
    assert ev.message_id == "m1"
    assert ev.source.chat_id == USER_ID and ev.source.chat_type == "dm"
    assert ev.source.user_id == USER_ID and ev.source.user_name == "Ana"
    assert ev.reply_to_message_id == "m0" and ev.reply_to_text == "earlier"
    assert ev.reply_to_is_own_message is True
    await asyncio.sleep(0.05)
    assert daemon.calls("POST", f"/v1/chats/{USER_ID}/read"), "read receipt after dispatch"


async def test_duplicates_and_own_messages_are_dropped(adapter):
    await adapter._dispatch(message("one"))
    await adapter._dispatch(message("one"))  # redelivered (at-least-once queue)
    await adapter._dispatch(message("mine", sender=BOT_ID, msg_id="m2"))
    await adapter._dispatch(message("sys", msg_id="m3", system="joined"))
    assert [e.text for e in adapter.handled] == ["one"]


async def test_photo_and_voice_attachments_are_cached(adapter, daemon):
    daemon.files["fp"] = b"\x89PNG\r\n\x1a\n" + b"0" * 64
    daemon.files["fv"] = b"\x1aE\xdf\xa3voice"
    await adapter._dispatch(message("", attachment={"file_id": "fp", "kind": "photo", "mime": "image/png",
                                                    "name": "p.png", "size": 72}))
    await adapter._dispatch(message("", msg_id="m2", attachment={"file_id": "fv", "kind": "voice",
                                                                "mime": "audio/webm", "name": "voice-1.webm", "size": 9}))
    photo, voice = adapter.handled
    assert photo.message_type == MessageType.PHOTO and photo.media_urls and photo.media_types[0].startswith("image/")
    assert voice.message_type == MessageType.VOICE and voice.media_urls[0].endswith(".webm")
    assert voice.media_types == ["audio/webm"]


async def test_group_requires_mention(adapter):
    await adapter._dispatch(message("just chatting", chat_id="g:team", chat_type="group"))
    assert adapter.handled == []


async def test_group_mention_auto_threads(adapter, daemon):
    await adapter._dispatch(message("@hermes_test summarize the logs", chat_id="g:team", chat_type="group",
                                    mentions=[{"id": BOT_ID}], mentions_me=True))
    created = daemon.calls("POST", "/v1/chats/g%3Ateam/threads") or daemon.calls("POST", "/v1/chats/g:team/threads")
    assert created and created[0]["body"] == {"name": "summarize the logs", "message_id": "m1"}
    ev = adapter.handled[0]
    assert ev.text == "summarize the logs"
    assert ev.source.chat_type == "thread" and ev.source.thread_id == "t1" and ev.source.chat_id == "g:team"
    assert ev.source.auto_thread_created and ev.source.auto_thread_initial_name == "summarize the logs"
    # Follow-up inside the thread needs no mention (participation).
    await adapter._dispatch(message("and the errors?", chat_id="g:team", chat_type="group", msg_id="m2",
                                    thread={"id": "t1", "name": "summarize the logs", "message_id": "m1"}))
    assert len(adapter.handled) == 2 and adapter.handled[1].source.thread_id == "t1"
    # A thread the bot never joined still needs a mention.
    await adapter._dispatch(message("unrelated", chat_id="g:team", chat_type="group", msg_id="m3",
                                    thread={"id": "t9", "name": "x"}))
    assert len(adapter.handled) == 2


async def test_reply_to_bot_counts_as_mention_and_skips_auto_thread(adapter, daemon):
    await adapter._dispatch(message("why?", chat_id="g:team", chat_type="group",
                                    reply_to={"id": "m0", "text": "x", "from": {"id": BOT_ID}}))
    assert len(adapter.handled) == 1 and adapter.handled[0].source.chat_type == "group"
    assert not daemon.calls("POST", "/v1/chats/g:team/threads")


async def test_free_response_and_no_mention_config(adapter, monkeypatch):
    monkeypatch.setenv("FOG_FREE_RESPONSE_CHATS", "g:free")
    await adapter._dispatch(message("hi all", chat_id="g:free", chat_type="group"))
    assert adapter.handled[-1].source.chat_type == "group"
    monkeypatch.setenv("FOG_REQUIRE_MENTION", "false")
    await adapter._dispatch(message("anyone?", chat_id="g:other", chat_type="group", msg_id="m2"))
    assert len(adapter.handled) == 2


async def test_history_backfill_in_groups(adapter, daemon):
    daemon.history = [
        {"id": "h0", "text": "old answer", "from": {"id": BOT_ID, "name": "Hermes", "is_bot": True}},
        {"id": "h1", "text": "deploy failed again", "from": {"id": OTHER_ID, "name": "Bo"}},
    ]
    await adapter._dispatch(message("why?", chat_id="g:team", chat_type="group",
                                    reply_to={"id": "h0", "text": "old answer", "from": {"id": BOT_ID}}))
    ctx = adapter.handled[0].channel_context
    assert "[Recent channel messages]" in ctx and "[Bo] deploy failed again" in ctx and "old answer" not in ctx


async def test_skill_command_is_rewritten(adapter):
    adapter._skill_lookup = {"summarize": "/summarize"}
    await adapter._dispatch(message("/skill summarize the README"))
    ev = adapter.handled[0]
    assert ev.text == "/summarize the README" and ev.message_type == MessageType.COMMAND


async def test_unknown_skill_gets_private_notice(adapter, daemon):
    await adapter._dispatch(message("@hermes_test /skill nope", chat_id="g:team", chat_type="group", mentions_me=True))
    assert adapter.handled == []
    sent = daemon.calls("POST", "/v1/chats/g:team/messages")
    assert sent and sent[0]["body"]["only_to"] == USER_ID


async def test_thread_command_starts_a_session(adapter, daemon):
    adapter.set_authorization_check(lambda user, chat_type, chat_id, **kw: user == USER_ID)
    await adapter._dispatch(message("/thread Nope", sender=OTHER_ID, msg_id="m0"))
    assert not daemon.calls("POST", f"/v1/chats/{USER_ID}/threads")  # strangers can't open threads
    await adapter._dispatch(message("/thread Release plan | draft the checklist"))
    created = daemon.calls("POST", f"/v1/chats/{USER_ID}/threads")
    assert created and created[0]["body"] == {"name": "Release plan"}
    ev = adapter.handled[0]
    assert ev.text == "draft the checklist" and ev.source.thread_id == "t1" and ev.source.chat_type == "thread"


async def test_handoff_thread(adapter, daemon):
    assert await adapter.create_handoff_thread("g:team", "CLI handoff") == "t1"
    assert "g:team/t1" in adapter._threads


async def test_platform_events(adapter):
    seen = []

    async def handler(envelope, source):
        seen.append((envelope, source))

    adapter._platform_event_handler = handler
    chat = {"id": "g:team", "type": "group", "title": "Team"}
    who = {"id": USER_ID, "name": "Ana"}
    await adapter._dispatch({"type": "reaction", "seq": 2, "chat": chat, "message_id": "m5", "from": who, "emoji": "👍"})
    await adapter._dispatch({"type": "deleted", "seq": 3, "chat": chat, "message_id": "m5", "from": who})
    await adapter._dispatch({"type": "edited", "seq": 4, "message": {**message("new text")["message"]}})
    await adapter._dispatch({"type": "thread", "seq": 5, "chat": chat, "from": who, "created": True,
                             "thread": {"id": "t3", "name": "Ideas"}})
    kinds = [e["event_type"] for e, _ in seen]
    assert kinds == ["reaction", "message_deleted", "message_edited", "thread_created"]
    assert seen[0][0]["payload"]["emojis"] == ["👍"] and seen[0][1].user_id == USER_ID
    assert seen[2][0]["payload"]["text"] == "new text"


async def test_autocomplete_filters_skills_and_refuses_strangers(adapter, daemon):
    adapter._skill_entries = [("summarize", "Summarize a document", "/summarize"), ("deploy", "Ship it", "/deploy")]
    adapter.set_authorization_check(lambda user, chat_type, chat_id, **kw: user == USER_ID)
    ev = {"type": "autocomplete", "query_id": "a1", "chat": {"id": USER_ID, "type": "dm"}, "from": {"id": USER_ID},
          "command": "skill", "option": "name", "value": "sum"}
    await adapter._dispatch(ev)
    await adapter._dispatch({**ev, "query_id": "a2", "from": {"id": OTHER_ID}})
    answers = daemon.calls("POST", "/v1/autocomplete/")
    assert answers[0]["body"]["choices"] == [{"label": "summarize — Summarize a document", "value": "summarize"}]
    assert answers[1]["body"]["choices"] == []
