"""Test setup: an isolated HERMES_HOME, the plugin importable as ``hermes_fog`` without installing it, and a fake
fog-bot daemon (aiohttp) that records every request."""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("HERMES_HOME", tempfile.mkdtemp(prefix="hermes-fog-test-"))

if "hermes_fog" not in sys.modules:
    try:
        import hermes_fog  # noqa: F401  (pip-installed)
    except ImportError:
        spec = importlib.util.spec_from_file_location("hermes_fog", ROOT / "__init__.py",
                                                      submodule_search_locations=[str(ROOT)])
        module = importlib.util.module_from_spec(spec)
        sys.modules["hermes_fog"] = module
        spec.loader.exec_module(module)

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_daemon import FakeDaemon  # noqa: E402

from fog_helpers import BOT_ID  # noqa: E402


def _register_platform() -> None:
    """Register through the real registry, as the plugin loader's ``ctx.register_platform`` does."""
    from gateway.platform_registry import PlatformEntry, platform_registry

    import hermes_fog

    hermes_fog.register(type("Ctx", (), {"register_platform": staticmethod(
        lambda **kw: platform_registry.register(PlatformEntry(source="plugin", plugin_name="hermes-fog", **kw)))})())


_register_platform()


@pytest.fixture
async def daemon():
    d = FakeDaemon(bot_id=BOT_ID)
    await d.start()
    yield d
    await d.stop()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in list(os.environ):
        if name.startswith("FOG_"):
            monkeypatch.delenv(name, raising=False)


@pytest.fixture
async def adapter(daemon):
    from gateway.config import PlatformConfig

    from hermes_fog.adapter import FogAdapter
    from hermes_fog.client import FogClient

    a = FogAdapter(PlatformConfig(enabled=True, extra={"token": "t0k", "url": daemon.url}))
    a._client = FogClient(daemon.url, "t0k")
    a._me = {"id": BOT_ID, "username": "hermes_test", "name": "Hermes"}
    a._threads.clear()
    a.handled = []

    async def capture(event):
        a.handled.append(event)

    a.handle_message = capture
    yield a
    await a._client.close()
