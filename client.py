"""Small async client for the fog-bot daemon's local HTTP API (see fog/bot/README.md).

Shared by the live adapter and the out-of-process sender (cron / ``hermes send``).
"""
from __future__ import annotations

import asyncio
import ipaddress
import os
from typing import Any, AsyncIterator, Optional
from urllib.parse import quote, urlsplit

import httpx

DEFAULT_URL = "http://127.0.0.1:8790"
_UPLOAD_CHUNK = 1024 * 1024


class FogApiError(Exception):
    """A 4xx/5xx answer from the daemon: ``status`` and the daemon's ``code`` (``chat_not_found``...)."""

    def __init__(self, status: int, code: str, message: str = ""):
        super().__init__(f"{status} {code}: {message}" if message else f"{status} {code}")
        self.status, self.code, self.message = status, code, message


def split_chat(chat_id: str) -> tuple[str, Optional[str]]:
    """``"<chat>/<thread>"`` -> ``(chat, thread)``. Fog ids never contain ``/``, so a home channel or ``hermes send``
    target can name a thread this way."""
    chat, _, thread = str(chat_id or "").partition("/")
    return chat, (thread or None)


def check_url(url: str) -> None:
    """The token gives full control of the bot's account: only send it over HTTPS, or plain HTTP to this machine
    (fog-bot's default). Anything else raises ``ValueError``."""
    parts = urlsplit(url)
    if parts.scheme == "https":
        return
    if parts.scheme != "http":
        raise ValueError(f"FOG_BOT_URL must start with http:// or https:// (got {url!r})")
    host = (parts.hostname or "").lower()
    try:
        local = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        local = False
    if not local:
        raise ValueError(f"refusing to send the fog-bot token over plain HTTP to {host or url!r}: "
                         "use https:// (fog-bot run --tls-cert/--tls-key) or an SSH tunnel to 127.0.0.1")


def _q(value: str) -> str:
    return quote(str(value), safe="")


class FogClient:
    """One ``httpx.AsyncClient`` against fog-bot (``http://127.0.0.1:8790`` by default) with the bearer token.
    ``ca``: a CA bundle (or fog-bot's own certificate) to trust for a remote ``https://`` fog-bot.
    Raises ``ValueError`` for a URL the token mustn't go to (see ``check_url``)."""

    def __init__(self, base_url: str, token: str, *, timeout: float = 30.0, ca: Optional[str] = None):
        self.base_url = (base_url or DEFAULT_URL).rstrip("/")
        check_url(self.base_url)
        self._http = httpx.AsyncClient(
            base_url=self.base_url, timeout=httpx.Timeout(timeout, connect=10.0),
            headers={"Authorization": f"Bearer {token}", "User-Agent": "hermes-fog"},
            verify=ca or True,
            # Straight to fog-bot; never route it (and the token) through an HTTP(S)_PROXY.
            trust_env=False)

    async def close(self) -> None:
        await self._http.aclose()

    async def request(self, method: str, path: str, *, json: Any = None, params: Optional[dict] = None,
                      content: Any = None, headers: Optional[dict] = None, timeout: Any = None) -> Any:
        kwargs: dict[str, Any] = {}
        if json is not None:
            kwargs["json"] = json
        if params:
            kwargs["params"] = {k: v for k, v in params.items() if v is not None}
        if content is not None:
            kwargs["content"] = content
        if headers:
            kwargs["headers"] = headers
        if timeout is not None:
            kwargs["timeout"] = timeout
        resp = await self._http.request(method, path, **kwargs)
        if resp.status_code >= 400:
            try:
                body = resp.json()
            except ValueError:
                body = {}
            raise FogApiError(resp.status_code, str(body.get("error") or "http_error"), str(body.get("message") or ""))
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError:
            return {}

    # --- identity / health -------------------------------------------------------------------

    async def health(self) -> dict:
        resp = await self._http.get("/health")
        resp.raise_for_status()
        return resp.json()

    async def me(self) -> dict:
        return await self.request("GET", "/v1/me")

    async def set_commands(self, commands: list) -> dict:
        return await self.request("PUT", "/v1/commands", json={"commands": commands})

    async def user(self, ref: str) -> dict:
        return await self.request("GET", f"/v1/users/{_q(ref)}")

    # --- events ------------------------------------------------------------------------------

    async def events(self, offset: int, timeout: int = 30, limit: int = 100) -> list:
        data = await self.request("GET", "/v1/events", params={"offset": offset, "timeout": timeout, "limit": limit},
                                  timeout=httpx.Timeout(timeout + 15.0, connect=10.0))
        return list(data.get("events") or [])

    # --- messages ----------------------------------------------------------------------------

    async def send_message(self, chat: str, body: dict) -> dict:
        return await self.request("POST", f"/v1/chats/{_q(chat)}/messages", json=body,
                                  timeout=httpx.Timeout(90.0, connect=10.0))

    async def edit_message(self, chat: str, message_id: str, body: dict) -> dict:
        return await self.request("PATCH", f"/v1/chats/{_q(chat)}/messages/{_q(message_id)}", json=body)

    async def delete_message(self, chat: str, message_id: str) -> dict:
        return await self.request("DELETE", f"/v1/chats/{_q(chat)}/messages/{_q(message_id)}")

    async def react(self, chat: str, message_id: str, emoji: Optional[str]) -> dict:
        return await self.request("PUT", f"/v1/chats/{_q(chat)}/messages/{_q(message_id)}/reaction",
                                  json={"emoji": emoji})

    async def typing(self, chat: str) -> dict:
        return await self.request("POST", f"/v1/chats/{_q(chat)}/typing")

    async def read(self, chat: str) -> dict:
        return await self.request("POST", f"/v1/chats/{_q(chat)}/read")

    async def history(self, chat: str, *, limit: int = 50, before: Optional[str] = None,
                      thread_id: Optional[str] = None) -> list:
        data = await self.request("GET", f"/v1/chats/{_q(chat)}/messages",
                                  params={"limit": limit, "before": before, "thread_id": thread_id})
        return list(data.get("messages") or [])

    async def chat(self, chat: str) -> dict:
        return await self.request("GET", f"/v1/chats/{_q(chat)}")

    # --- threads -----------------------------------------------------------------------------

    async def threads(self, chat: str) -> list:
        data = await self.request("GET", f"/v1/chats/{_q(chat)}/threads")
        return list(data.get("threads") or [])

    async def create_thread(self, chat: str, name: str, message_id: Optional[str] = None) -> dict:
        body: dict[str, Any] = {"name": name}
        if message_id:
            body["message_id"] = message_id
        data = await self.request("POST", f"/v1/chats/{_q(chat)}/threads", json=body)
        return data.get("thread") or {}

    async def rename_thread(self, chat: str, thread_id: str, name: str) -> dict:
        data = await self.request("PATCH", f"/v1/chats/{_q(chat)}/threads/{_q(thread_id)}", json={"name": name})
        return data.get("thread") or {}

    # --- taps / suggestions ------------------------------------------------------------------

    async def answer_button(self, query_id: str, *, text: Optional[str] = None, alert: bool = False,
                            form: Optional[dict] = None) -> dict:
        body: dict[str, Any] = {}
        if text:
            body["text"] = text
        if alert:
            body["alert"] = True
        if form:
            body["form"] = form
        return await self.request("POST", f"/v1/buttons/{_q(query_id)}/answer", json=body)

    async def answer_autocomplete(self, query_id: str, choices: list) -> dict:
        return await self.request("POST", f"/v1/autocomplete/{_q(query_id)}/answer", json={"choices": choices})

    # --- files -------------------------------------------------------------------------------

    async def upload_file(self, path: str, *, kind: Optional[str] = None, mime: str = "application/octet-stream",
                          name: Optional[str] = None, duration: Optional[float] = None) -> dict:
        """Stream a local file to ``POST /v1/files`` (up to 2 GB) and return its ``file`` record."""
        params = {"name": name or os.path.basename(path), "kind": kind,
                  "duration": f"{duration:.2f}" if duration else None}

        async def chunks() -> AsyncIterator[bytes]:
            fh = await asyncio.to_thread(open, path, "rb")
            try:
                while True:
                    block = await asyncio.to_thread(fh.read, _UPLOAD_CHUNK)
                    if not block:
                        break
                    yield block
            finally:
                await asyncio.to_thread(fh.close)

        size = await asyncio.to_thread(os.path.getsize, path)
        data = await self.request("POST", "/v1/files", params=params, content=chunks(),
                                  headers={"Content-Type": mime, "Content-Length": str(size)},
                                  timeout=httpx.Timeout(max(120.0, size / 1_000_000), connect=10.0))
        return data.get("file") or {}

    async def download_file(self, file_id: str, *, max_bytes: Optional[int] = None) -> bytes:
        async with self._http.stream("GET", f"/v1/files/{_q(file_id)}",
                                     timeout=httpx.Timeout(300.0, connect=10.0)) as resp:
            if resp.status_code >= 400:
                await resp.aread()
                try:
                    body = resp.json()
                except ValueError:
                    body = {}
                raise FogApiError(resp.status_code, str(body.get("error") or "http_error"), str(body.get("message") or ""))
            buf = bytearray()
            async for block in resp.aiter_bytes():
                buf.extend(block)
                if max_bytes and len(buf) > max_bytes:
                    raise FogApiError(413, "file_too_large", f"attachment exceeds {max_bytes} bytes")
            return bytes(buf)
