"""Approvals, clarify, slash confirm, /update, model + choice pickers and the vault code form."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from fog_helpers import OTHER_ID, USER_ID, tap
from gateway.platforms.base import ExecApprovalPrompt


def _last_post(daemon, path):
    return daemon.calls("POST", path)[-1]["body"]


def _answers(daemon):
    return [r["body"] for r in daemon.calls("POST", "/v1/buttons/")]


def _patches(daemon, chat=USER_ID):
    return [r["body"] for r in daemon.calls("PATCH", f"/v1/chats/{chat}/messages/")]


@pytest.fixture
def allow_user(adapter):
    adapter.set_authorization_check(lambda user, chat_type, chat_id, **kw: user in (USER_ID, OTHER_ID))
    return adapter


def _approval(actions=(("Allow Once", "once", "primary"), ("Allow Session", "session", ""),
                       ("Always Allow", "always", ""), ("Deny", "deny", "danger"))):
    return ExecApprovalPrompt(chat_id=USER_ID, session_key="agent:main:fog:dm:x", text="⚠️ run `rm -rf build`?",
                              actions=list(actions), command="rm -rf build", description="dangerous",
                              smart_denied=False, request_id="req-1")


async def test_approval_card_and_resolution(allow_user, daemon, monkeypatch):
    adapter = allow_user
    calls = []
    monkeypatch.setattr("tools.approval.resolve_gateway_approval_card",
                        lambda sk, choice, rid, **kw: calls.append((sk, choice, rid, kw.get("actor_id"))) or 1)
    monkeypatch.setattr("gateway.approval_actor.approval_refusal", lambda *a, **kw: None)
    result = await adapter._send_exec_approval_prompt(_approval())
    assert result.success
    body = _last_post(daemon, f"/v1/chats/{USER_ID}/messages")
    assert body["card"]["title"].startswith("⚠️") and "silent" not in body
    flat = [b for row in body["buttons"] for b in row]
    assert [b["data"].split(":")[1] for b in flat] == ["once", "session", "always", "deny"]
    assert flat[0]["style"] == "primary" and flat[-1]["style"] == "danger"
    pid = flat[0]["data"].split(":")[2]
    await adapter._handle_button(tap(f"ea:session:{pid}", message_id=result.message_id))
    assert calls == [("agent:main:fog:dm:x", "session", "req-1", USER_ID)]
    assert _answers(daemon)[-1]["text"].startswith("✅")
    edit = _patches(daemon)[-1]
    assert edit["buttons"] is None and edit["card"]["title"].startswith("✅")
    # A second tap finds nothing pending.
    await adapter._handle_button(tap(f"ea:once:{pid}"))
    assert "already been resolved" in _answers(daemon)[-1]["text"] and len(calls) == 1


async def test_approval_refusals(allow_user, daemon, monkeypatch):
    adapter = allow_user
    monkeypatch.setattr("tools.approval.resolve_gateway_approval_card", lambda *a, **kw: pytest.fail("resolved"))
    monkeypatch.setattr("gateway.approval_actor.approval_refusal", lambda *a, **kw: "Ana")
    await adapter._send_exec_approval_prompt(_approval())
    pid = _last_post(daemon, f"/v1/chats/{USER_ID}/messages")["buttons"][0][0]["data"].split(":")[2]
    await adapter._handle_button(tap(f"ea:once:{pid}", sender=OTHER_ID))
    answer = _answers(daemon)[-1]
    assert answer["alert"] is True and "Ana" in answer["text"]
    # Someone the gateway doesn't admit at all.
    await adapter._handle_button(tap(f"ea:once:{pid}", sender="Z" * 43))
    assert _answers(daemon)[-1]["alert"] is True
    assert pid in adapter._approvals  # still pending


async def test_clarify_buttons_other_and_multiselect(allow_user, daemon, monkeypatch):
    adapter = allow_user
    import tools.clarify_gateway as cg
    resolved, awaiting = [], []
    entry = SimpleNamespace(choices=["Postgres", "SQLite"], multi_select=False)
    monkeypatch.setattr(cg, "get_pending", lambda cid: entry)
    monkeypatch.setattr(cg, "resolve_gateway_clarify", lambda cid, text: resolved.append((cid, text)) or True)
    monkeypatch.setattr(cg, "mark_awaiting_text", lambda cid: awaiting.append(cid) or True)
    monkeypatch.setattr("gateway.approval_actor.clarify_refusal", lambda *a, **kw: None)
    await adapter.send_clarify(USER_ID, "Which database?", ["Postgres", "SQLite"], "c1", "sk")
    body = _last_post(daemon, f"/v1/chats/{USER_ID}/messages")
    datas = [b["data"] for row in body["buttons"] for b in row]
    assert datas == ["cl:c1:0", "cl:c1:1", "cl:c1:other"] and "1. Postgres" in body["text"]
    await adapter._handle_button(tap("cl:c1:1"))
    assert resolved == [("c1", "SQLite")] and _patches(daemon)[-1]["buttons"] is None
    await adapter.send_clarify(USER_ID, "Anything else?", ["Yes", "No"], "c2", "sk")
    await adapter._handle_button(tap("cl:c2:other"))
    assert awaiting == ["c2"]
    entry.multi_select, entry.choices = True, ["a", "b", "c"]
    await adapter.send_clarify(USER_ID, "Pick", ["a", "b", "c"], "c3", "sk")
    select = _last_post(daemon, f"/v1/chats/{USER_ID}/messages")["buttons"][0]["select"]
    assert select["id"] == "cl:c3" and select["max"] == 3
    await adapter._handle_button(tap("cl:c3", values=["0", "2"]))
    assert resolved[-1] == ("c3", "a, c")


async def test_slash_confirm(allow_user, daemon, monkeypatch):
    adapter = allow_user
    import tools.slash_confirm as sc
    got = []

    async def resolve(session_key, confirm_id, choice, **kw):
        got.append((session_key, confirm_id, choice))
        return "MCP servers reloaded."
    monkeypatch.setattr(sc, "resolve", resolve)
    monkeypatch.setattr("gateway.approval_actor.slash_confirm_refusal", lambda *a, **kw: None)
    await adapter.send_slash_confirm(USER_ID, "Reload MCP?", "This restarts tools.", "sk", "conf1")
    datas = [b["data"] for row in _last_post(daemon, f"/v1/chats/{USER_ID}/messages")["buttons"] for b in row]
    assert datas == ["sc:once:conf1", "sc:always:conf1", "sc:cancel:conf1"]
    await adapter._handle_button(tap("sc:always:conf1"))
    assert got == [("sk", "conf1", "always")]
    assert _last_post(daemon, f"/v1/chats/{USER_ID}/messages")["text"] == "MCP servers reloaded."


async def test_update_prompt(allow_user, daemon, monkeypatch, tmp_path):
    adapter = allow_user
    monkeypatch.setattr("gateway.approval_actor.update_prompt_refusal", lambda *a, **kw: None)
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)
    await adapter.send_update_prompt(USER_ID, "Restart now?", default="y")
    data = _last_post(daemon, f"/v1/chats/{USER_ID}/messages")["buttons"][0][0]["data"]
    await adapter._handle_button(tap(data))
    assert (tmp_path / ".update_response").read_text() == "y"
    await adapter._handle_button(tap(data))
    assert "Already answered" in _answers(daemon)[-1]["text"]


async def test_model_picker_flow(allow_user, daemon, monkeypatch):
    adapter = allow_user
    monkeypatch.setattr("hermes_cli.model_selection_guards.combined_selection_warning", lambda *a, **kw: None)
    switched = []

    async def on_selected(chat_id, model_id, provider):
        switched.append((chat_id, model_id, provider))
        return f"Switched to {model_id}"
    providers = [{"slug": "anthropic", "name": "Anthropic", "models": ["claude-opus", "claude-haiku"], "is_current": True},
                 {"slug": "openai", "name": "OpenAI", "models": [f"gpt-{i}" for i in range(30)]}]
    sent = await adapter.send_model_picker(USER_ID, providers, "claude-opus", "anthropic", "sk", on_selected)
    body = _last_post(daemon, f"/v1/chats/{USER_ID}/messages")
    select = body["buttons"][0]["select"]
    pid = select["id"].split(":")[1]
    assert [o["value"] for o in select["options"]] == ["anthropic", "openai"]
    await adapter._handle_button(tap(f"mp:{pid}", values=["openai"], message_id=sent.message_id))
    rows = _patches(daemon)[-1]["buttons"]
    assert [r["select"]["id"] for r in rows[:2]] == [f"mm:{pid}", f"mm:{pid}"]  # 30 models -> two dropdowns
    await adapter._handle_button(tap(f"mm:{pid}", values=["gpt-7"], message_id=sent.message_id))
    assert switched == [(USER_ID, "gpt-7", "openai")]
    final = _patches(daemon)[-1]
    assert final["text"] == "Switched to gpt-7" and final["buttons"] is None
    await adapter._handle_button(tap(f"mm:{pid}", values=["gpt-1"], message_id=sent.message_id))
    assert "expired" in _answers(daemon)[-1]["text"]


async def test_model_picker_refuses_non_opener(allow_user, daemon):
    adapter = allow_user

    async def on_selected(*_a):
        raise AssertionError("should not switch")
    on_selected.prompt_authority = {"requester_id": USER_ID, "requester_name": "Ana", "chat_type": "group", "approvers": []}
    sent = await adapter.send_model_picker(USER_ID, [{"slug": "a", "name": "A", "models": ["m"]}], "m", "a", "sk", on_selected)
    pid = _last_post(daemon, f"/v1/chats/{USER_ID}/messages")["buttons"][0]["select"]["id"].split(":")[1]
    await adapter._handle_button(tap(f"mp:{pid}", values=["a"], sender=OTHER_ID, message_id=sent.message_id))
    assert _answers(daemon)[-1]["alert"] is True


async def test_choice_picker(allow_user, daemon):
    adapter = allow_user
    picked = []

    async def on_choice(chat_id, value):
        picked.append(value)
        return f"Reasoning set to {value}"
    sent = await adapter.send_choice_picker(USER_ID, "Reasoning effort\nCurrent: medium", [
        {"value": "low", "label": "Low"}, {"value": "high", "label": "High", "is_current": True}], "sk", on_choice)
    body = _last_post(daemon, f"/v1/chats/{USER_ID}/messages")
    select = body["buttons"][0]["select"]
    assert select["options"][1]["default"] is True and body["card"]["title"] == "⚙ Reasoning effort"
    await adapter._handle_button(tap(select["id"], values=["low"], message_id="stale"))
    assert "expired" in _answers(daemon)[-1]["text"] and picked == []  # a tap on another message
    await adapter._handle_button(tap(select["id"], values=["low"], message_id=sent.message_id))
    assert picked == ["low"] and _patches(daemon)[-1]["text"] == "Reasoning set to low"


async def test_vault_code_form(allow_user, daemon):
    adapter = allow_user
    codes = []
    await adapter.send_vault_code_prompt(USER_ID, "github.com", "", user_id=USER_ID,
                                         submit=lambda code: codes.append(code) or True, timeout_s=60)
    data = _last_post(daemon, f"/v1/chats/{USER_ID}/messages")["buttons"][0][0]["data"]
    await adapter._handle_button(tap(data, sender=OTHER_ID))
    assert _answers(daemon)[-1]["alert"] is True  # only the turn's author
    await adapter._handle_button(tap(data))
    form = _answers(daemon)[-1]["form"]
    assert form["fields"][0]["secret"] is True and form["fields"][0]["id"] == "code"
    await adapter._handle_button(tap(form["id"], fields={"code": "123456"}))
    assert codes == ["123456"]
    assert _patches(daemon)[-1]["buttons"] is None
