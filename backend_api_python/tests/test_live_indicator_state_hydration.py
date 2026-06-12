import pandas as pd

from app.services import trading_executor as trading_executor_module
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


LEGACY_GRID_STATE_INDICATOR = """
import pandas as pd

df = df.copy()
raw_buy = df["close"] <= 0.0844
raw_sell = df["close"] >= 0.0851

position_qty = 0
avg_price = 0.0
last_buy_price = 0.0

open_long_signals = []
close_long_signals = []
signal_types = []
debug_position_qty = []
debug_avg_price = []
debug_take_profit_line = []

for i in range(len(df)):
    current_price = float(df["close"].iloc[i])
    is_raw_buy = bool(raw_buy.iloc[i])
    is_raw_sell = bool(raw_sell.iloc[i])

    do_open_long = False
    do_close_long = False
    mark_type = ""

    if is_raw_buy and position_qty < 6:
        if position_qty == 0:
            do_open_long = True
            avg_price = current_price
            last_buy_price = current_price
            position_qty += 1
        elif current_price <= last_buy_price * (1 - 0.006):
            do_open_long = True
            avg_price = ((avg_price * position_qty) + current_price) / (position_qty + 1)
            last_buy_price = current_price
            position_qty += 1
    elif position_qty > 0 and is_raw_sell and current_price >= avg_price * (1 + 0.005):
        do_close_long = True
        mark_type = "S"
        position_qty = 0
        avg_price = 0.0
        last_buy_price = 0.0

    open_long_signals.append(do_open_long)
    close_long_signals.append(do_close_long)
    signal_types.append(mark_type)
    debug_position_qty.append(position_qty)
    debug_avg_price.append(avg_price if position_qty > 0 else None)
    debug_take_profit_line.append(avg_price * (1 + 0.005) if position_qty > 0 else None)

df["open_long"] = pd.Series(open_long_signals, index=df.index).fillna(False).astype(bool)
df["close_long"] = pd.Series(close_long_signals, index=df.index).fillna(False).astype(bool)
df["open_short"] = False
df["close_short"] = False
df["buy"] = df["open_long"]
df["sell"] = df["close_long"]
df["signal_type"] = signal_types
df["debug_position_qty"] = debug_position_qty
df["debug_avg_price"] = debug_avg_price
df["debug_take_profit_line"] = debug_take_profit_line
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


def _multi_bar_df(closes: list[float]) -> pd.DataFrame:
    ts = pd.date_range("2026-06-10T00:00:00Z", periods=len(closes), freq="5min")
    return pd.DataFrame(
        {
            "open": closes,
            "high": closes,
            "low": closes,
            "close": closes,
            "volume": [1.0] * len(closes),
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


def test_legacy_indicator_reseeds_live_state_only_on_tail_window() -> None:
    ex = _make_executor()
    df, _ = ex._execute_indicator_df(
        LEGACY_GRID_STATE_INDICATOR,
        _multi_bar_df([0.0844, 0.0838, 0.0831, 0.0851, 0.0850]),
        trading_config={"indicator_params": {}, "leverage": 2, "initial_capital": 20},
        initial_position=1,
        initial_avg_entry_price=0.08568375,
        initial_position_count=2,
        initial_last_add_price=0.085014,
    )

    decision_bar = df.iloc[-2]
    assert bool(decision_bar["close_long"]) is False
    assert decision_bar["debug_position_qty"] == 2
    assert decision_bar["debug_avg_price"] == 0.08568375
    assert decision_bar["debug_take_profit_line"] == 0.08568375 * 1.005


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, query, args=None):
        return None

    def fetchall(self):
        return self._rows

    def close(self):
        return None


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows

    def cursor(self):
        return _FakeCursor(self._rows)


class _FakeDbCtx:
    def __init__(self, rows):
        self._rows = rows

    def __enter__(self):
        return _FakeConn(self._rows)

    def __exit__(self, exc_type, exc, tb):
        return False


def test_live_position_snapshot_uses_real_open_leg_entries(monkeypatch) -> None:
    ex = _make_executor()
    monkeypatch.setattr(
        ex,
        "_get_current_positions",
        lambda strategy_id, symbol: [
            {
                "symbol": "DOGE/USDT",
                "side": "long",
                "size": 400.0,
                "entry_price": 0.08568375,
                "highest_price": 0.0,
            }
        ],
    )
    rows = [
        {
            "created_at": "2026-06-10T13:45:07Z",
            "symbol": "DOGE/USDT",
            "type": "close_long",
            "amount": 400.0,
            "price": 0.085255,
            "source": "local",
        },
        {
            "created_at": "2026-06-09T14:25:09Z",
            "symbol": "DOGE/USDT",
            "type": "add_long",
            "amount": 100.0,
            "price": 0.085014,
            "source": "local",
        },
        {
            "created_at": "2026-06-09T09:30:07Z",
            "symbol": "DOGE/USDT",
            "type": "open_long",
            "amount": 300.0,
            "price": 0.085907,
            "source": "local",
        },
        {
            "created_at": "2026-06-09T04:35:05Z",
            "symbol": "DOGE/USDT",
            "type": "close_long",
            "amount": 400.0,
            "price": 0.086114,
            "source": "local",
        },
    ]
    monkeypatch.setattr(
        trading_executor_module,
        "get_db_connection",
        lambda: _FakeDbCtx(rows),
    )

    snapshot = ex._get_live_indicator_position_snapshot(strategy_id=6, symbol="DOGE/USDT")
    assert snapshot["initial_position"] == 1
    assert snapshot["initial_avg_entry_price"] == 0.08568375
    assert snapshot["initial_position_count"] == 2
    assert snapshot["initial_last_add_price"] == 0.085014
