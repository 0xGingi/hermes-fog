"""Plugin registration: ``register(ctx)`` plus the config/env hooks Hermes calls before the adapter exists."""
from __future__ import annotations

import dataclasses
import logging
import re
from typing import Optional

from gateway.platforms._shared import apply_yaml_bridge, env_is_connected, get_scoped_secret, seed_extra_from_env

from .adapter import MAX_MESSAGE_LENGTH, PLATFORM_NAME, FogAdapter, check_fog_requirements, fog_token
from .client import split_chat
from .hint import PLATFORM_HINT, PLATFORM_HINT_TOOL_NOTES
from .standalone import standalone_send

logger = logging.getLogger(__name__)

_TRUTHY = {"1", "true", "yes", "on"}

# (ENV_VAR, extra key, converter) seeded into PlatformConfig.extra for env-only setups.
_ENV_SEED = (
    ("FOG_BOT_TOKEN", "token", None),
    ("FOG_BOT_URL", "url", None),
    ("FOG_BOT_CA", "ca", None),
    ("FOG_REQUIRE_MENTION", "require_mention", lambda v: v.lower() in _TRUTHY),
    ("FOG_AUTO_THREAD", "auto_thread", lambda v: v.lower() in _TRUTHY),
    ("FOG_REACTIONS", "reactions", lambda v: v.lower() in _TRUTHY),
    ("FOG_HISTORY_BACKFILL", "history_backfill", lambda v: v.lower() in _TRUTHY),
    ("FOG_FREE_RESPONSE_CHATS", "free_response_chats", None),
    ("FOG_NOTIFICATIONS", "notifications", None),
)

# ``fog:`` keys in config.yaml -> env (env wins) and PlatformConfig.extra.
_YAML_BRIDGE = (
    ("require_mention", "FOG_REQUIRE_MENTION", "lower"),
    ("auto_thread", "FOG_AUTO_THREAD", "lower"),
    ("reactions", "FOG_REACTIONS", "lower"),
    ("history_backfill", "FOG_HISTORY_BACKFILL", "lower"),
    ("free_response_chats", "FOG_FREE_RESPONSE_CHATS", "csv"),
    ("notifications", "FOG_NOTIFICATIONS", "str"),
    ("url", "FOG_BOT_URL", "str"),
    ("ca", "FOG_BOT_CA", "str"),
)

_ACCOUNT_ID = re.compile(r"^[A-Za-z0-9_-]{43}$")


def _env_enablement() -> Optional[dict]:
    if not str(get_scoped_secret("FOG_BOT_TOKEN", "") or "").strip():
        return None
    return seed_extra_from_env(_ENV_SEED, home_env="FOG_HOME_CHANNEL")


def _apply_yaml_config(yaml_cfg: dict, platform_cfg: dict) -> Optional[dict]:
    return apply_yaml_bridge(platform_cfg or {}, _YAML_BRIDGE)


def _validate_config(config) -> bool:
    if fog_token(config):
        return True
    logger.warning("Fog: FOG_BOT_TOKEN not configured (run `fog-bot token` on the bot's machine)")
    return False


def _config_connected(config) -> bool:
    return bool(fog_token(config))


def _parse_target(ref: str):
    """Native ``send`` targets: ``g:<group>``, ``@username``, an account id, each optionally ``/<thread id>``."""
    chat, thread = split_chat((ref or "").strip())
    if chat.startswith("g:") or chat.startswith("@") or _ACCOUNT_ID.match(chat):
        return chat, (thread or None)
    return None


def register(ctx) -> None:
    """Plugin entry point, called by the Hermes plugin system."""
    kwargs = dict(
        name=PLATFORM_NAME, label="Fog", adapter_factory=FogAdapter,
        check_fn=check_fog_requirements, validate_config=_validate_config,
        is_connected=env_is_connected("FOG_BOT_TOKEN"), required_env=["FOG_BOT_TOKEN"],
        install_hint="Install and start fog-bot (`fog-bot init`, `fog-bot run`), then set FOG_BOT_TOKEN.",
        env_enablement_fn=_env_enablement, apply_yaml_config_fn=_apply_yaml_config,
        cron_deliver_env_var="FOG_HOME_CHANNEL", standalone_sender_fn=standalone_send,
        parse_target_ref_fn=_parse_target,
        allowed_users_env="FOG_ALLOWED_USERS", allow_all_env="FOG_ALLOW_ALL_USERS",
        max_message_length=MAX_MESSAGE_LENGTH, emoji="🌫️", allow_update_command=True, pii_safe=False,
        platform_hint=PLATFORM_HINT,
    )
    try:  # fields newer cores added; older ones reject unknown kwargs
        from gateway.platform_registry import PlatformEntry
        known = {f.name for f in dataclasses.fields(PlatformEntry)}
    except Exception:
        known = set()
    for key, value in (
        ("platform_hint_tool_notes", PLATFORM_HINT_TOOL_NOTES),
        ("group_allowed_chats_env", "FOG_GROUP_ALLOWED_CHATS"),
        ("allow_bots_env", "FOG_ALLOW_BOTS"),
        ("allowlist_suppresses_pairing", True),
        ("config_connected_fn", _config_connected),
        ("display_tier", "high"),
        ("dashboard_description", "End-to-end encrypted messenger, through a local fog-bot daemon."),
        ("docs_url", "https://github.com/0xGingi/hermes-fog"),
        ("dashboard_env_vars", ("FOG_BOT_TOKEN", "FOG_BOT_URL", "FOG_ALLOWED_USERS", "FOG_HOME_CHANNEL")),
    ):
        if key in known:
            kwargs[key] = value
    ctx.register_platform(**kwargs)
