"""Sends, streaming edits, notifications, media kinds, reactions, typing, commands, standalone, registration."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from fog_helpers import USER_ID
from gateway.platforms.event import MessageEvent, ProcessingOutcome


def _bodies(daemon, path):
    return [r["body"] for r in daemon.calls("POST", path)]


async def test_streaming_send_and_edits(adapter, daemon):
    first = await adapter.send(USER_ID, "Think", reply_to="m1", metadata={"expect_edits": True})
    assert first.success and first.message_id
    body = _bodies(daemon, f"/v1/chats/{USER_ID}/messages")[0]
    assert body == {"text": "Think", "reply_to": "m1", "live": True}  # the reply's first frame rings
    assert (await adapter.edit_message(USER_ID, first.message_id, "Thinking about it")).success
    assert (await adapter.edit_message(USER_ID, first.message_id, "Done.", finalize=True)).success
    edits = [r["body"] for r in daemon.calls("PATCH", f"/v1/chats/{USER_ID}/messages/{first.message_id}")]
    assert edits == [{"text": "Thinking about it"}, {"text": "Done.", "live": False}]
    assert adapter.REQUIRES_EDIT_FINALIZE is True


async def test_notification_policy(adapter, daemon, monkeypatch):
    await adapter.send(USER_ID, "tool progress")                                  # no notify -> silent
    await adapter.send(USER_ID, "final answer", metadata={"notify": True})        # final -> rings
    await adapter.send(USER_ID, "interim", metadata={"_interim_send": True})      # interim -> silent
    monkeypatch.setenv("FOG_NOTIFICATIONS", "all")
    await adapter.send(USER_ID, "everything rings")
    silent = [b.get("silent", False) for b in _bodies(daemon, f"/v1/chats/{USER_ID}/messages")]
    assert silent == [True, False, True, False]


async def test_thread_metadata_and_composite_targets(adapter, daemon):
    await adapter.send("g:team", "in thread", metadata={"thread_id": "t7", "notify": True})
    await adapter.send("g:team/t8", "home thread", metadata={"notify": True})
    bodies = _bodies(daemon, "/v1/chats/g:team/messages")
    assert bodies[0]["thread_id"] == "t7" and bodies[1]["thread_id"] == "t8"


async def test_long_messages_split(adapter, daemon):
    text = ("paragraph " * 400 + "\n\n") * 6  # ~24k chars
    result = await adapter.send(USER_ID, text, metadata={"notify": True})
    bodies = _bodies(daemon, f"/v1/chats/{USER_ID}/messages")
    assert result.success and len(bodies) == 2 and result.continuation_message_ids
    assert all(len(b["text"]) <= 16000 for b in bodies)


async def test_daemon_send_failure_is_not_retried(adapter):
    result = await adapter.send(USER_ID, "FAIL", metadata={"notify": True})
    assert not result.success and not result.retryable


async def test_media_upload_kinds(adapter, daemon, tmp_path):
    files = {}
    for name in ("a.ogg", "b.mp3", "c.png", "d.mp4", "e.pdf"):
        files[name] = tmp_path / name
        files[name].write_bytes(b"data-" + name.encode())
    assert (await adapter.send_voice(USER_ID, str(files["a.ogg"]))).success
    assert (await adapter.send_voice(USER_ID, str(files["b.mp3"]), is_voice=False)).success
    assert (await adapter.send_image_file(USER_ID, str(files["c.png"]), caption="look")).success
    assert (await adapter.send_video(USER_ID, str(files["d.mp4"]))).success
    assert (await adapter.send_document(USER_ID, str(files["e.pdf"]), file_name="report.pdf")).success
    uploads = daemon.calls("POST", "/v1/files")
    assert [u["query"]["kind"] for u in uploads] == ["voice", "audio", "photo", "video", "file"]
    assert uploads[0]["content_type"] == "audio/ogg" and uploads[2]["content_type"] == "image/png"
    assert uploads[4]["query"]["name"] == "report.pdf"
    assert uploads[0]["raw"] == b"data-a.ogg"
    sends = _bodies(daemon, f"/v1/chats/{USER_ID}/messages")
    assert all(b.get("file_id") for b in sends) and sends[2]["text"] == "look"
    missing = await adapter.send_document(USER_ID, str(tmp_path / "nope.txt"))
    assert not missing.success


async def test_multiple_images_go_one_per_message(adapter, daemon, tmp_path):
    paths = []
    for i in range(3):
        p = tmp_path / f"i{i}.png"
        p.write_bytes(b"png")
        paths.append(p)
    result = await adapter.send_multiple_images(USER_ID, [(f"file://{p}", "") for p in paths])
    assert result.success and len(daemon.calls("POST", f"/v1/chats/{USER_ID}/messages")) == 3


async def test_processing_reactions(adapter, daemon):
    ev = MessageEvent(text="x", message_id="m1", source=SimpleNamespace(chat_id=USER_ID, thread_id=None))
    await adapter.on_processing_start(ev)
    await adapter.on_processing_complete(ev, ProcessingOutcome.SUCCESS)
    await adapter.on_processing_complete(ev, ProcessingOutcome.CANCELLED)
    emojis = [r["body"]["emoji"] for r in daemon.calls("PUT", f"/v1/chats/{USER_ID}/messages/m1/reaction")]
    assert emojis == ["👀", "✅", None]


async def test_typing_is_throttled(adapter, daemon):
    await adapter.send_typing(USER_ID)
    await adapter.send_typing(USER_ID)
    assert len(daemon.calls("POST", f"/v1/chats/{USER_ID}/typing")) == 1


async def test_private_notice_uses_only_to_in_groups(adapter, daemon):
    await adapter.send_private_notice("g:team", USER_ID, "only you")
    await adapter.send_private_notice(USER_ID, USER_ID, "dm")
    assert _bodies(daemon, "/v1/chats/g:team/messages")[0]["only_to"] == USER_ID
    assert "only_to" not in _bodies(daemon, f"/v1/chats/{USER_ID}/messages")[0]


async def test_publish_commands(adapter, daemon, monkeypatch):
    import hermes_fog.adapter as mod
    monkeypatch.setattr(mod, "skill_catalog", lambda: [("summarize", "Summarize", "/summarize")])
    await adapter.publish_commands()
    commands = daemon.calls("PUT", "/v1/commands")[-1]["body"]["commands"]
    names = [c["name"] for c in commands]
    assert {"new", "stop", "model", "help"} <= set(names) and len(commands) <= 100
    skill = next(c for c in commands if c["name"] == "skill")
    assert skill["options"][0]["autocomplete"] is True
    assert "thread" in names
    assert adapter._skill_lookup == {"summarize": "/summarize"}
    reasoning = next((c for c in commands if c["name"] == "reasoning"), None)
    if reasoning:
        assert reasoning["options"][0]["choices"]
    assert (await adapter.refresh_skill_group())[0] == 1


async def test_allowlist_usernames_resolve(adapter, daemon, monkeypatch):
    monkeypatch.setenv("FOG_ALLOWED_USERS", "@ana, someid")
    await adapter._resolve_allowlist()
    assert adapter.resolved_allowlist_user_ids() == {USER_ID}
    assert adapter._env_allows(USER_ID) and adapter._env_allows("someid") and not adapter._env_allows("x")


async def test_get_chat_info(adapter):
    info = await adapter.get_chat_info("g:team")
    assert info == {"name": "Team", "type": "group", "chat_id": "g:team", "member_count": 2}


async def test_semantic_rename_after_first_turn(adapter, daemon, monkeypatch):
    import hermes_fog.adapter as mod
    real_sleep = asyncio.sleep
    monkeypatch.setattr(mod.asyncio, "sleep", lambda *_a, **_k: real_sleep(0))
    daemon.threads["g:team"] = [{"id": "t1", "name": "summarize the logs"}]
    adapter._auto_threads["g:team/t1"] = {"initial": "summarize the logs", "renamed": False}

    async def get_title(_sid):
        return "Log summary for Nov 25"
    adapter.gateway_runner = SimpleNamespace(persistence=SimpleNamespace(aio=SimpleNamespace(
        sessions=SimpleNamespace(get_title=get_title))))
    adapter._session_store = SimpleNamespace(peek_session_id=lambda key: "sess-1")
    adapter._event_session_key = lambda ev: "k"
    ev = MessageEvent(text="x", message_id="m1", source=SimpleNamespace(chat_id="g:team", thread_id="t1"))
    await adapter._semantic_rename(ev)
    assert daemon.threads["g:team"][0]["name"] == "Log summary for Nov 25"


async def test_standalone_send(daemon, tmp_path):
    from gateway.config import PlatformConfig

    from hermes_fog.standalone import standalone_send
    img = tmp_path / "chart.png"
    img.write_bytes(b"png")
    cfg = PlatformConfig(enabled=True, extra={"token": "t0k", "url": daemon.url})
    out = await standalone_send(cfg, "g:team/t4", "cron report", media_files=[(str(img), False)])
    assert out["success"]
    bodies = _bodies(daemon, "/v1/chats/g:team/messages")
    assert bodies[0] == {"thread_id": "t4", "text": "cron report"} and bodies[1]["file_id"]
    assert daemon.calls("POST", "/v1/files")[0]["query"]["kind"] == "photo"
    assert "error" in await standalone_send(PlatformConfig(enabled=True, extra={}), "x", "y")


def test_register_declares_the_platform():
    import hermes_fog
    captured = {}
    hermes_fog.register(SimpleNamespace(register_platform=lambda **kw: captured.update(kw)))
    assert captured["name"] == "fog" and captured["allowed_users_env"] == "FOG_ALLOWED_USERS"
    assert captured["cron_deliver_env_var"] == "FOG_HOME_CHANNEL" and captured["standalone_sender_fn"]
    assert captured.get("group_allowed_chats_env") == "FOG_GROUP_ALLOWED_CHATS"
    assert "Markdown" in captured["platform_hint"]
    parse = captured["parse_target_ref_fn"]
    assert parse("g:abc/t1") == ("g:abc", "t1") and parse("@ana") == ("@ana", None) and parse("#general") is None


def test_env_enablement(monkeypatch):
    from hermes_fog.register import _env_enablement
    assert _env_enablement() is None
    monkeypatch.setenv("FOG_BOT_TOKEN", "abc")
    monkeypatch.setenv("FOG_HOME_CHANNEL", "g:home")
    monkeypatch.setenv("FOG_REQUIRE_MENTION", "false")
    seed = _env_enablement()
    assert seed["token"] == "abc" and seed["require_mention"] is False
    assert seed["home_channel"]["chat_id"] == "g:home"


async def test_connect_polls_and_dispatches(daemon, monkeypatch):
    from gateway.config import PlatformConfig

    from fog_helpers import message
    from hermes_fog.adapter import FogAdapter
    import hermes_fog.adapter as mod
    monkeypatch.setattr(mod, "skill_catalog", lambda: [])
    daemon.events = [{**message("first"), "seq": 5},
                     {**message("second", msg_id="m2"), "seq": 6}]
    a = FogAdapter(PlatformConfig(enabled=True, extra={"token": "t0k", "url": daemon.url}))
    got = []

    async def capture(ev):
        got.append(ev.text)
    a.handle_message = capture
    assert await a.connect()
    try:
        for _ in range(50):
            if len(got) == 2 and daemon.calls("PUT", "/v1/commands"):
                break
            await asyncio.sleep(0.05)
        assert got == ["first", "second"]
        assert a._offset == 7
        assert any(r["query"].get("offset") == "7" for r in daemon.calls("GET", "/v1/events"))
        assert daemon.calls("PUT", "/v1/commands")
    finally:
        await a.disconnect()


async def test_connect_rejects_bad_token(daemon):
    from gateway.config import PlatformConfig

    from hermes_fog.adapter import FogAdapter
    a = FogAdapter(PlatformConfig(enabled=True, extra={"token": "wrong", "url": daemon.url}))
    assert not await a.connect()
    assert a._fatal_error_code == "auth"
