from app.services.trading_executor import TradingExecutor


def test_normalize_signal_for_state_maps_open_long_to_add_long_when_already_long():
    signal = {"type": "open_long", "trigger_price": 1.23, "timestamp": 123}

    normalized = TradingExecutor._normalize_signal_for_state(signal, "long")

    assert normalized is not signal
    assert normalized["type"] == "add_long"
    assert normalized["trigger_price"] == signal["trigger_price"]
    assert normalized["timestamp"] == signal["timestamp"]


def test_normalize_signal_for_state_keeps_both_mode_flip_semantics():
    signal = {"type": "open_long", "trigger_price": 1.23, "timestamp": 123}

    normalized = TradingExecutor._normalize_signal_for_state(
        signal,
        "short",
        indicator_both_mode=True,
    )

    assert normalized is signal
    assert normalized["type"] == "open_long"
