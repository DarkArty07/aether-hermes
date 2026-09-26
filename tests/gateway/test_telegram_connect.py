"""Tests for Telegram connect() non-retryable fatal error on missing credentials.

When Telegram has no bot token or no python-telegram-bot installed, connect()
must set a non-retryable fatal error so the gateway does not queue it for
background reconnection (#31049).
"""

import sys
from types import ModuleType
from typing import Any
from unittest.mock import MagicMock

import pytest

from gateway.config import PlatformConfig


def _ensure_telegram_mock():
    if "telegram" in sys.modules and hasattr(sys.modules["telegram"], "__file__"):
        return

    telegram_mod = MagicMock()
    telegram_mod.ext.ContextTypes.DEFAULT_TYPE = type(None)
    telegram_mod.constants.ParseMode.MARKDOWN_V2 = "MarkdownV2"
    telegram_mod.constants.ChatType.GROUP = "group"
    telegram_mod.constants.ChatType.SUPERGROUP = "supergroup"
    telegram_mod.constants.ChatType.CHANNEL = "channel"
    telegram_mod.constants.ChatType.PRIVATE = "private"

    telegram_mod.error.NetworkError = type("NetworkError", (OSError,), {})
    telegram_mod.error.TimedOut = type("TimedOut", (OSError,), {})
    telegram_mod.error.BadRequest = type("BadRequest", (Exception,), {})

    for name in ("telegram", "telegram.ext", "telegram.constants", "telegram.request"):
        sys.modules.setdefault(name, telegram_mod)
    sys.modules.setdefault("telegram.error", telegram_mod.error)


_ensure_telegram_mock()

import plugins.platforms.telegram.adapter as telegram_mod  # noqa: E402
from plugins.platforms.telegram.adapter import TelegramAdapter  # noqa: E402


class TestTelegramUnconfiguredNonRetryable:
    """Verify that missing dependency/token sets a non-retryable fatal error."""

    @pytest.mark.asyncio
    async def test_no_telegram_lib_sets_non_retryable_fatal(self, monkeypatch):
        """connect() with python-telegram-bot unavailable → non-retryable fatal error."""
        adapter = TelegramAdapter(PlatformConfig(enabled=True, token="fake"))
        monkeypatch.setattr(telegram_mod, "TELEGRAM_AVAILABLE", False)
        result = await adapter.connect()
        assert result is False
        assert adapter.has_fatal_error is True
        assert adapter.fatal_error_retryable is False
        assert adapter.fatal_error_code == "missing_dependency"


def _lazy_telegram_sdk(monkeypatch):
    sdk = ModuleType("telegram")
    ext = ModuleType("telegram.ext")
    constants = ModuleType("telegram.constants")
    request = ModuleType("telegram.request")
    for name in ("Update", "Bot", "Message", "InlineKeyboardButton", "InlineKeyboardMarkup"):
        setattr(sdk, name, type(name, (), {}))
    setattr(sdk, "LinkPreviewOptions", None)
    for name in ("Application", "CommandHandler", "CallbackQueryHandler", "MessageHandler"):
        setattr(ext, name, MagicMock())
    setattr(ext, "ContextTypes", MagicMock())
    setattr(ext, "filters", MagicMock())
    setattr(constants, "ParseMode", MagicMock())
    setattr(constants, "ChatType", MagicMock())
    setattr(request, "HTTPXRequest", MagicMock())

    class FakeTypeHandler:
        def __init__(self, update_type, callback):
            self.update_type = update_type
            self.callback = callback

    setattr(ext, "TypeHandler", FakeTypeHandler)
    # Restore every global that the lazy-loader rebinds, not just the flag.
    for name in (
        "Update", "Bot", "Message", "InlineKeyboardButton", "InlineKeyboardMarkup",
        "LinkPreviewOptions", "Application", "CommandHandler", "CallbackQueryHandler",
        "TelegramMessageHandler", "TypeHandler", "ContextTypes", "filters", "ParseMode",
        "ChatType", "HTTPXRequest",
    ):
        monkeypatch.setattr(telegram_mod, name, getattr(telegram_mod, name))
    monkeypatch.setattr(telegram_mod, "TELEGRAM_AVAILABLE", False)
    monkeypatch.setattr(telegram_mod, "TypeHandler", Any)
    for name, module in (
        ("telegram", sdk), ("telegram.ext", ext),
        ("telegram.constants", constants), ("telegram.request", request),
    ):
        monkeypatch.setitem(sys.modules, name, module)
    ensure = MagicMock()
    monkeypatch.setattr("tools.lazy_deps.ensure", ensure)
    return sdk, ext, ensure


def test_lazy_install_rebinds_type_handler_before_registering_handlers(monkeypatch):
    sdk, ext, ensure = _lazy_telegram_sdk(monkeypatch)
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="fake"))
    assert telegram_mod.check_telegram_requirements() is True
    app = MagicMock()
    adapter._register_handlers(app)
    handler = app.add_handler.call_args.args[0]
    assert isinstance(handler, getattr(ext, "TypeHandler"))
    assert handler.update_type is getattr(sdk, "Update")
    assert app.add_handler.call_args.kwargs == {"group": 99}
    ensure.assert_called_once_with("platform.telegram", prompt=False)


def test_lazy_install_without_type_handler_stays_unavailable(monkeypatch):
    _, ext, _ = _lazy_telegram_sdk(monkeypatch)
    delattr(ext, "TypeHandler")
    assert telegram_mod.check_telegram_requirements() is False
    assert telegram_mod.TELEGRAM_AVAILABLE is False
    assert telegram_mod.TypeHandler is Any

