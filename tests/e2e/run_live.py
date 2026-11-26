"""Live end-to-end run: the real FogAdapter against a running fog-bot daemon, with a scripted Fog user.

    FOG_BOT_URL=http://127.0.0.1:8790 FOG_BOT_TOKEN=... FOG_REPO=~/dev/fog FOG_SERVER=http://localhost:8787 \\
    FOG_E2E_WORKDIR=<dir to run the user in> python tests/e2e/run_live.py

No gateway and no LLM: ``handle_message`` is captured, and the bot side of the conversation is played here
through the adapter's own send/edit/approval methods. Exits non-zero when a step fails.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
os.environ.setdefault("HERMES_HOME", tempfile.mkdtemp(prefix="hermes-fog-e2e-"))


def _seed_skill() -> None:
    """One skill in the throwaway HERMES_HOME so /skill and its autocomplete have something to list."""
    skill = Path(os.environ["HERMES_HOME"]) / "skills" / "e2e-demo" / "SKILL.md"
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text("---\nname: e2e-demo\ndescription: A demo skill for the Fog end-to-end test\n---\n\nSay hi.\n")


def _load_plugin():
    if "hermes_fog" not in sys.modules:
        spec = importlib.util.spec_from_file_location("hermes_fog", ROOT / "__init__.py",
                                                      submodule_search_locations=[str(ROOT)])
        mod = importlib.util.module_from_spec(spec)
        sys.modules["hermes_fog"] = mod
        spec.loader.exec_module(mod)
    from gateway.platform_registry import PlatformEntry, platform_registry
    sys.modules["hermes_fog"].register(type("Ctx", (), {"register_platform": staticmethod(
        lambda **kw: platform_registry.register(PlatformEntry(source="plugin", **kw)))})())


async def _wait(pred, timeout=45.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        value = pred()
        if value:
            return value
        await asyncio.sleep(0.1)
    return None


async def main() -> int:
    _seed_skill()
    _load_plugin()
    from gateway.config import PlatformConfig
    from gateway.platforms.base import ExecApprovalPrompt

    from hermes_fog.adapter import FogAdapter

    adapter = FogAdapter(PlatformConfig(enabled=True, extra={
        "token": os.environ["FOG_BOT_TOKEN"], "url": os.environ.get("FOG_BOT_URL", "http://127.0.0.1:8790")}))
    handled: list = []

    async def capture(event):
        handled.append(event)
    adapter.handle_message = capture
    # Stands in for the gateway runner's admission chain (allowlists/pairing), which admits the test user.
    adapter.set_authorization_check(lambda user_id, chat_type, chat_id, **kw: True)

    resolved: list = []
    import tools.approval
    tools.approval.resolve_gateway_approval_card = lambda sk, choice, rid, **kw: resolved.append((sk, choice, rid)) or 1
    import gateway.approval_actor
    gateway.approval_actor.approval_refusal = lambda *a, **kw: None

    assert await adapter.connect(), "adapter.connect() failed"
    print("adapter connected; draining backlog", flush=True)
    await asyncio.sleep(3)  # earlier runs' events drain first
    handled.clear()

    user = await asyncio.create_subprocess_exec(
        "bun", str(ROOT / "tests" / "e2e" / "fog_user.ts"), cwd=os.environ.get("FOG_E2E_WORKDIR", "."),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env={**os.environ})
    steps: dict[str, dict] = {}
    user_id = None

    async def read_user():
        nonlocal user_id
        async for raw in user.stdout:
            line = raw.decode().strip()
            if not line.startswith("{"):
                continue
            step = json.loads(line)
            steps[step["step"]] = step
            print("user:", line, flush=True)
            if step["step"] == "ready":
                user_id = step["user"]
    reader = asyncio.create_task(read_user())
    results: dict[str, bool] = {}
    try:
        # 1. inbound DM reaches handle_message with the right fields
        ev = await _wait(lambda: next((e for e in handled if e.text == "hello from e2e"), None))
        results["inbound"] = bool(ev) and ev.source.chat_type == "dm" and ev.source.user_id == user_id
        if not ev:
            raise SystemExit("no inbound event")
        chat = ev.source.chat_id
        # 2. streamed reply: live send, live edits, final edit clears live
        first = await adapter.send(chat, "Working", reply_to=ev.message_id, metadata={"expect_edits": True})
        await asyncio.sleep(0.4)
        await adapter.edit_message(chat, first.message_id, "Working on it")
        await asyncio.sleep(0.4)
        final = await adapter.edit_message(chat, first.message_id, "Done **e2e**", finalize=True)
        results["stream_sent"] = first.success and final.success
        # 3. approval card; the user taps "Allow once"
        card = await adapter._send_exec_approval_prompt(ExecApprovalPrompt(
            chat_id=chat, session_key="agent:main:fog:dm:e2e", text="Run `rm -rf build`?",
            actions=[("Allow Once", "once", "primary"), ("Deny", "deny", "danger")], command="rm -rf build",
            description="dangerous", smart_denied=False, request_id="req-e2e"))
        results["approval_sent"] = card.success
        results["approval_resolved"] = bool(await _wait(lambda: resolved)) and resolved[0][1:] == ("once", "req-e2e")
        # 4. group mention -> auto-thread -> reply in the thread
        tev = await _wait(lambda: next((e for e in handled if e.source.chat_type == "thread"
                                        and e.text == "check the build"), None), 60)
        results["auto_thread"] = bool(tev) and bool(tev.source.auto_thread_created)
        if tev:
            await adapter.send(tev.source.chat_id, "thread reply", metadata={"thread_id": tev.source.thread_id, "notify": True})
            fev = await _wait(lambda: next((e for e in handled if e.text == "and the tests?"), None), 45)
            results["thread_participation"] = bool(fev) and fev.source.thread_id == tev.source.thread_id
            if fev:
                await adapter.send(fev.source.chat_id, "follow-up seen", metadata={"thread_id": fev.source.thread_id})
        await asyncio.wait_for(user.wait(), 90)
        await reader
    finally:
        if user.returncode is None:
            user.kill()
        err = (await user.stderr.read()).decode()
        if err.strip():
            print("user stderr:", err[-2000:], flush=True)
        await adapter.disconnect()
    for name in ("stream", "approval", "commands", "thread", "thread_followup"):
        results[f"user:{name}"] = bool(steps.get(name, {}).get("ok"))
    width = max(map(len, results))
    for name, ok in results.items():
        print(f"{'PASS' if ok else 'FAIL'}  {name.ljust(width)}", flush=True)
    passed = sum(results.values())
    print(f"{passed}/{len(results)} checks passed", flush=True)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
