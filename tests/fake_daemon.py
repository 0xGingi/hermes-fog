"""A fake fog-bot daemon: the HTTP surface of fog/bot/main.ts, recording requests for assertions."""
from __future__ import annotations

import asyncio
import json
from typing import Any, Optional

from aiohttp import web


class FakeDaemon:
    def __init__(self, bot_id: str):
        self.bot_id = bot_id
        self.requests: list[dict] = []
        self.events: list[dict] = []
        self.files: dict[str, bytes] = {}
        self.threads: dict[str, list] = {}
        self.history: list[dict] = []
        self.users = {"@ana": {"id": "U" * 43, "username": "ana", "name": "Ana", "is_bot": False}}
        self._n = 0
        self._runner: Optional[web.AppRunner] = None
        self.url = ""

    def _id(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}{self._n}"

    def calls(self, method: str, path_prefix: str) -> list[dict]:
        return [r for r in self.requests if r["method"] == method and r["path"].startswith(path_prefix)]

    async def start(self) -> None:
        app = web.Application(client_max_size=1024 ** 3)
        app.router.add_route("*", "/{tail:.*}", self._handle)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()

    async def _handle(self, request: web.Request) -> web.Response:
        path, method = request.path, request.method
        raw = await request.read()
        body: Any = None
        if raw and request.content_type == "application/json":
            body = json.loads(raw)
        self.requests.append({"method": method, "path": path, "query": dict(request.query), "body": body,
                              "raw": raw if request.content_type != "application/json" else None,
                              "content_type": request.content_type})
        if path == "/health":
            return web.json_response({"ok": True, "connected": True})
        if request.headers.get("Authorization") != "Bearer t0k":
            return web.json_response({"error": "unauthorized"}, status=401)
        parts = path.strip("/").split("/")
        if path == "/v1/me":
            return web.json_response({"id": self.bot_id, "username": "hermes_test", "name": "Hermes", "is_bot": True})
        if path == "/v1/commands":
            return web.json_response({"ok": True, "count": len((body or {}).get("commands", []))})
        if path == "/v1/events":
            offset = int(request.query.get("offset", 0))
            pending = [e for e in self.events if e["seq"] >= offset]
            if not pending:
                await asyncio.sleep(min(float(request.query.get("timeout", 0)), 0.2))
            return web.json_response({"events": pending[: int(request.query.get("limit", 100))]})
        if path == "/v1/files" and method == "POST":
            fid = self._id("f")
            self.files[fid] = raw
            return web.json_response({"file": {"file_id": fid, "kind": request.query.get("kind"),
                                               "name": request.query.get("name"), "size": len(raw)}})
        if parts[:2] == ["v1", "files"] and method == "GET":
            return web.Response(body=self.files.get(parts[2], b""), content_type="application/octet-stream")
        if parts[:2] == ["v1", "users"]:
            user = self.users.get(parts[2])
            return web.json_response(user) if user else web.json_response({"error": "user_not_found"}, status=404)
        if parts[:2] in (["v1", "buttons"], ["v1", "autocomplete"]):
            return web.json_response({"ok": True})
        if parts[:2] == ["v1", "chats"]:
            chat = parts[2]
            rest = parts[3:]
            if not rest and method == "GET":
                return web.json_response({"id": chat, "type": "group" if chat.startswith("g:") else "dm",
                                          "title": "Team", "members": [{"id": "U" * 43}, {"id": self.bot_id}]})
            if rest == ["messages"] and method == "POST":
                if (body or {}).get("text") == "FAIL":
                    return web.json_response({"error": "send_failed", "message": "x"}, status=502)
                mid = self._id("m")
                msg = {"id": mid, "chat": {"id": chat}, "text": body.get("text", "")}
                return web.json_response({"message": msg})
            if rest == ["messages"] and method == "GET":
                return web.json_response({"messages": self.history})
            if rest and rest[0] == "messages":
                return web.json_response({"message": {"id": rest[1]}, "ok": True})
            if rest == ["threads"] and method == "POST":
                tid = self._id("t")
                thread = {"id": tid, "name": body.get("name"), **({"message_id": body["message_id"]} if body.get("message_id") else {})}
                self.threads.setdefault(chat, []).append(thread)
                return web.json_response({"thread": thread})
            if rest == ["threads"] and method == "GET":
                return web.json_response({"threads": self.threads.get(chat, [])})
            if rest[:1] == ["threads"] and method == "PATCH":
                for t in self.threads.get(chat, []):
                    if t["id"] == rest[1]:
                        t["name"] = body["name"]
                        return web.json_response({"thread": t})
                return web.json_response({"error": "thread_not_found"}, status=404)
            return web.json_response({"ok": True})
        return web.json_response({"error": "not_found"}, status=404)
