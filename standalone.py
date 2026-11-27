"""Out-of-process Fog send (cron without a co-resident gateway, ``hermes send``): ``standalone_sender_fn``."""
from __future__ import annotations

import logging
import mimetypes
import os
from typing import Optional

from gateway.platforms.base import BasePlatformAdapter, utf16_len

from .client import FogClient, split_chat

logger = logging.getLogger(__name__)

_IMAGE = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
_VIDEO = {".mp4", ".mov", ".webm", ".mkv", ".avi", ".3gp"}
_AUDIO = {".ogg", ".opus", ".mp3", ".m4a", ".wav", ".flac", ".aac"}


def _kind(path: str, is_voice: bool, force_document: bool) -> str:
    ext = os.path.splitext(path)[1].lower()
    if force_document:
        return "file"
    if ext in _AUDIO:
        return "voice" if is_voice else "audio"
    return "photo" if ext in _IMAGE else "video" if ext in _VIDEO else "file"


async def standalone_send(pconfig, chat_id: str, message: str, *, thread_id: Optional[str] = None,
                          media_files: Optional[list] = None, force_document: bool = False, notify: bool = True) -> dict:
    from .adapter import MAX_MESSAGE_LENGTH, fog_ca, fog_token, fog_url
    token = fog_token(pconfig)
    if not token:
        return {"error": "FOG_BOT_TOKEN is not set"}
    try:
        client = FogClient(fog_url(pconfig), token, ca=fog_ca(pconfig))
    except ValueError as exc:
        return {"error": str(exc)}
    base: dict = {}
    chat_id, target_thread = split_chat(chat_id)
    thread_id = thread_id or target_thread
    if thread_id:
        base["thread_id"] = str(thread_id)
    if not notify:
        base["silent"] = True
    last_id = None
    warnings: list[str] = []
    try:
        text = message or ""
        chunks = ([text] if utf16_len(text) <= MAX_MESSAGE_LENGTH
                  else BasePlatformAdapter.truncate_message(text, MAX_MESSAGE_LENGTH, utf16_len)) if text.strip() else []
        for chunk in chunks:
            data = await client.send_message(chat_id, {**base, "text": chunk})
            last_id = (data.get("message") or {}).get("id") or last_id
        for item in media_files or []:
            path, is_voice = (item if isinstance(item, (tuple, list)) else (item, False))[:2]
            if not os.path.isfile(path):
                warnings.append(f"missing file skipped: {os.path.basename(path)}")
                continue
            uploaded = await client.upload_file(path, kind=_kind(path, bool(is_voice), force_document),
                                                mime=mimetypes.guess_type(path)[0] or "application/octet-stream")
            data = await client.send_message(chat_id, {**base, "file_id": uploaded.get("file_id")})
            last_id = (data.get("message") or {}).get("id") or last_id
        if last_id is None:
            return {"error": "nothing to send"}
        out = {"success": True, "platform": "fog", "chat_id": chat_id, "message_id": last_id}
        if warnings:
            out["warnings"] = warnings
        return out
    except Exception as exc:
        logger.warning("Fog standalone send failed: %s", exc)
        return {"error": f"Fog send failed: {exc}"}
    finally:
        await client.close()
