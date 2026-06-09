import pandas as pd

from app.services.trading_executor import TradingExecutor


LEGACY_LONG_STATE_INDICATOR = """
# @param stop_loss_pct float 0.88 stop line
stop_loss_pct = float(params.get("stop_loss_pct", 0.88))
df = df.copy()

position_qty = 0
avg_price = 0.0
last_buy_price = 0.0
current_price = float(df["close"].iloc[-1])
do_close_long = bool(position_qty > 0 and current_price <= avg_price * stop_loss_pct)

df["open_long"] = False
df["close_long"] = do_close_long
df["open_short"] = False
df["close_short"] = False
df["signal_type"] = "STOP" if do_close_long else ""
df["debug_position_qty"] = position_qty
df["debug_avg_price"] = avg_price if position_qty > 0 else None
df["debug_take_profit_line"] = None
df["debug_stop_loss_line"] = avg_price * stop_loss_pct if position_qty > 0 else None
"""


def _make_executor() -> TradingExecutor:
    return TradingExecutor.__new__(TradingExecutor)


def _single_bar_df(close_price: float) -> pd.DataFrame:
    ts = pd.DatetimeIndex([pd.Timestamp("2026-06-06T08:25:00Z")])
    return pd.DataFrame(
        {
            "open": [close_price],
            "high": [close_price],
            "low": [close_price],
            "close": [close_price],
            "volume": [1.0],
        },
        index=ts,
    )


def test_legacy_indicator_hydrates_real_position_before_evaluating_stop() -> None:
    ex = _make_executor()
    df, _ = ex._execute_indicator_df(
        LEGACY_LONG_STATE_INDICATOR,
        _single_bar_df(0.0825),
        trading_config={"indicator_params": {"stop_loss_pct": 0.5}, "leverage": 2, "initial_capital": 20},
        initial_position=1,
        initial_avg_entry_price=0.10034,
        initial_position_count=1,
        initial_last_add_price=0.10034,
    )

    row = df.iloc[0]
    assert bool(row["open_long"]) is False
    assert bool(row["close_long"]) is False
    assert row["signal_type"] == ""
    assert row["debug_position_qty"] == 1
    assert row["debug_avg_price"] == 0.10034
    assert row["debug_stop_loss_line"] == 0.10034 * 0.5


def test_legacy_indicator_default_stop_still_triggers_when_price_crosses_line() -> None:
    ex = _make_executor()
    df, _ = ex._execute_indicator_df(
        LEGACY_LONG_STATE_INDICATOR,
        _single_bar_df(0.08235),
        trading_config={"indicator_params": {}, "leverage": 2, "initial_capital": 20},
        initial_position=1,
        initial_avg_entry_price=0.10034,
        initial_position_count=1,
        initial_last_add_price=0.10034,
    )

    row = df.iloc[0]
    assert bool(row["close_long"]) is True
    assert row["signal_type"] == "STOP"
