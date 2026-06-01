"""Tests for unified strategy auto-stop helpers."""

from app.services.strategy_lifecycle import is_fatal_exchange_error
from app.services.trading_executor import TradingExecutor


def test_binance_auth_fatal():
    assert is_fatal_exchange_error('Binance HTTP 401: {"code":-2015,"msg":"Invalid API-key"}')


def test_ibkr_connection_fatal():
    assert is_fatal_exchange_error("Connect call failed ('127.0.0.1', 7497)")


def test_bitget_ip_fatal():
    assert is_fatal_exchange_error("Bitget error 40018: Invalid IP")


def test_transient_not_fatal():
    assert not is_fatal_exchange_error("timeout waiting for response")


def test_exit_position_protection_action_defaults_to_notify(monkeypatch):
    monkeypatch.delenv("STRATEGY_EXIT_POSITION_PROTECTION", raising=False)
    executor = object.__new__(TradingExecutor)

    assert executor._exit_position_protection_action() == "notify"


def test_exit_position_protection_action_close(monkeypatch):
    monkeypatch.setenv("STRATEGY_EXIT_POSITION_PROTECTION", "close")
    executor = object.__new__(TradingExecutor)

    assert executor._exit_position_protection_action() == "close"


def test_format_position_summary():
    executor = object.__new__(TradingExecutor)

    summary = executor._format_position_summary([
        {
            "symbol": "LTC/USDT",
            "side": "long",
            "size": "2.0",
            "entry_price": "53.76",
            "current_price": "52.73",
        }
    ])

    assert "LTC/USDT long" in summary
    assert "size=2" in summary
    assert "entry=53.76" in summary


def test_okx_missing_credentials_fatal():
    assert is_fatal_exchange_error("Missing OKX api_key/secret_key/passphrase")


def test_maybe_auto_stop_fatal():
    from unittest.mock import patch

    from app.services.strategy_lifecycle import maybe_auto_stop_on_exchange_error

    with patch("app.services.strategy_lifecycle.auto_stop_live_strategy") as stop:
        assert maybe_auto_stop_on_exchange_error(1, "Missing OKX api_key/secret_key/passphrase", source="test")
        stop.assert_called_once()


def test_maybe_auto_stop_repeated():
    from unittest.mock import patch

    from app.services.strategy_lifecycle import maybe_auto_stop_on_exchange_error

    with patch("app.services.strategy_lifecycle.auto_stop_live_strategy") as stop:
        assert not maybe_auto_stop_on_exchange_error(1, "timeout", consecutive_failures=2, consecutive_threshold=5)
        stop.assert_not_called()
        assert maybe_auto_stop_on_exchange_error(1, "timeout", consecutive_failures=5, consecutive_threshold=5)
        stop.assert_called_once()
