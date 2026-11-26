"""Fog command menu: Hermes' gateway commands, plugin commands, ``/skill`` (autocomplete) and ``/thread``.

The menu travels inside the bot's encrypted profile (``PUT /v1/commands``). A picked command arrives as an
ordinary message (``/model opus``), so dispatch needs nothing here except the ``/skill`` rewrite.
"""
from __future__ import annotations

import logging
import re
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

MAX_COMMANDS = 100
MAX_CHOICES = 25
_NAME_RE = re.compile(r"[^a-z0-9_-]")
_DESC_LIMIT = 100

SKILL_COMMAND = "skill"
THREAD_COMMAND = "thread"


def _clip(text: str, limit: int = _DESC_LIMIT) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def _fog_name(raw: str) -> str:
    """Fog command names are ``[a-z0-9_-]``; anything else would never match what the client sends."""
    name = str(raw or "").strip().lstrip("/").lower()
    return "" if _NAME_RE.search(name) else name[:32]


def _options_for(args_hint: str, subcommands: Iterable[str] = ()) -> list:
    """One positional option per command: a fixed choice list from ``subcommands``, else free text."""
    hint = str(args_hint or "").strip()
    if not hint and not subcommands:
        return []
    option: dict = {"name": "args", "description": _clip(hint or "arguments"), "required": hint.startswith("<")}
    subs = [s for s in subcommands if s][:MAX_CHOICES]
    if subs:
        option["name"] = "action"
        option["choices"] = [{"label": s, "value": s} for s in subs]
    return [option]


def _core_commands() -> list[tuple[str, str, str, tuple]]:
    from hermes_cli.commands import COMMAND_REGISTRY, _is_gateway_available, _resolve_config_gates
    gates = _resolve_config_gates()
    return [(c.name, c.describe() if hasattr(c, "describe") else c.description, c.args_hint, tuple(c.subcommands))
            for c in COMMAND_REGISTRY if _is_gateway_available(c, gates)]


def _plugin_commands() -> list[tuple[str, str, str]]:
    try:
        from hermes_cli.commands import _iter_plugin_command_entries
        return list(_iter_plugin_command_entries())
    except Exception:
        logger.debug("Fog: plugin command listing failed", exc_info=True)
        return []


def skill_catalog(reserved: Iterable[str] = ()) -> list[tuple[str, str, str]]:
    """``(name, description, cmd_key)`` for every gateway-visible skill and bundle, alphabetical."""
    try:
        from hermes_cli.commands_platforms import discord_skill_commands_by_category
        categories, uncategorized, _hidden = discord_skill_commands_by_category(set(reserved))
    except Exception:
        logger.debug("Fog: skill catalog scan failed", exc_info=True)
        return []
    entries = [e for group in categories.values() for e in group] + list(uncategorized)
    return sorted(entries, key=lambda e: e[0])


def build_menu(skills_available: bool = True) -> list[dict]:
    """The Fog ``commands`` payload: built-ins first, then plugin commands, then ``/skill`` and ``/thread``."""
    menu: list[dict] = []
    seen: set[str] = set()
    reserved_tail = [SKILL_COMMAND, THREAD_COMMAND]

    def add(raw_name: str, description: str, args_hint: str = "", subcommands: Iterable[str] = ()) -> None:
        name = _fog_name(raw_name)
        if not name or name in seen or name in reserved_tail or len(menu) >= MAX_COMMANDS - len(reserved_tail):
            return
        seen.add(name)
        entry: dict = {"name": name, "description": _clip(description or f"/{name}")}
        options = _options_for(args_hint, subcommands)
        if options:
            entry["options"] = options
        menu.append(entry)

    try:
        for name, desc, hint, subs in _core_commands():
            add(name, desc, hint, subs)
    except Exception:
        logger.warning("Fog: could not read Hermes' command registry; publishing a minimal menu", exc_info=True)
        for name, desc in (("new", "Start a new session"), ("stop", "Stop the running task"),
                           ("model", "Switch the model"), ("help", "Show commands")):
            add(name, desc)
    for name, desc, hint in _plugin_commands():
        add(name, desc, hint)
    if skills_available:
        menu.append({"name": SKILL_COMMAND, "description": "Run a skill",
                     "options": [{"name": "name", "description": "Skill name", "required": True, "autocomplete": True},
                                 {"name": "args", "description": "Arguments for the skill", "required": False}]})
    menu.append({"name": THREAD_COMMAND, "description": "Start a thread with its own session",
                 "options": [{"name": "name", "description": "Thread name (add | and a first message to start it)",
                              "required": True}]})
    return menu


def filter_skills(entries: list[tuple[str, str, str]], query: str, limit: int = MAX_CHOICES) -> list[dict]:
    """Autocomplete choices: substring match on name or description (Discord's rule), at most ``limit``."""
    q = (query or "").strip().lower()
    out: list[dict] = []
    for name, desc, _key in entries:
        if q and q not in name.lower() and not (desc and q in desc.lower()):
            continue
        label = f"{name} — {desc}" if desc else name
        out.append({"label": _clip(label), "value": name})
        if len(out) >= limit:
            break
    return out


def rewrite_skill_command(text: str, lookup: dict[str, str]) -> Optional[str]:
    """``/skill <name> [args]`` -> ``/<cmd_key> [args]``; ``None`` when the skill is unknown/missing."""
    parts = (text or "").strip().split(maxsplit=2)
    if len(parts) < 2:
        return None
    key = lookup.get(parts[1].strip().lower())
    if not key:
        return None
    return f"{key} {parts[2]}".strip() if len(parts) > 2 else key


def parse_thread_command(text: str) -> tuple[str, str]:
    """``/thread Name | first message`` -> ``(name, message)``."""
    rest = (text or "").strip().split(maxsplit=1)
    body = rest[1] if len(rest) > 1 else ""
    name, _, message = body.partition("|")
    return name.strip(), message.strip()
