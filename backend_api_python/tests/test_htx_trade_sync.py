from app.services.htx_trade_sync import _parse_strategy_from_client_order_id, _trade_external_key
from app.services.live_trading.htx import HtxClient


def _client(market_type="swap"):
    return HtxClient(api_key="k", secret_key="s", market_type=market_type)


def test_parse_strategy_from_client_order_id_formats():
    assert _parse_strategy_from_client_order_id("qd_6_123_m", current_strategy_id=1) == 6
    assert _parse_strategy_from_client_order_id("qd6123m", current_strategy_id=6) == 6
    assert _parse_strategy_from_client_order_id("manual-123", current_strategy_id=6) is None


def test_trade_external_key_uses_symbol_order_client_trade_time():
    row = {
        "market_type": "swap",
        "symbol": "DOGE-USDT",
        "order_id": "o1",
        "client_order_id": "c1",
        "trade_id": "t1",
        "trade_time_ms": 123456,
    }
    assert _trade_external_key(row) == "htx:swap:DOGE/USDT:o1:c1:t1:123456"


def test_normalize_spot_history_fill():
    row = {
        "symbol": "dogeusdt",
        "order-id": "100",
        "match-id": "200",
        "type": "buy-market",
        "price": "0.10",
        "filled-amount": "12",
        "filled-fees": "0.001",
        "created-at": 1710000000000,
    }
    out = _client("spot")._normalize_history_fill(row, symbol="DOGE/USDT", market_type="spot")
    assert out["symbol"] == "DOGE/USDT"
    assert out["type"] == "buy"
    assert out["order_id"] == "100"
    assert out["trade_id"] == "200"
    assert out["amount"] == 12
    assert out["value"] == 1.2
    assert out["commission"] == 0.001


def test_normalize_swap_history_fill_converts_contracts(monkeypatch):
    client = _client("swap")
    monkeypatch.setattr(client, "get_contract_info", lambda symbol: {"contract_size": 100})
    row = {
        "contract_code": "DOGE-USDT",
        "order_id": "100",
        "trade_id": "200",
        "direction": "sell",
        "offset": "close",
        "trade_price": "0.10",
        "trade_volume": "2",
        "trade_fee": "0.003",
        "realized_pnl": "-0.5",
        "trade_time": 1710000000000,
    }
    out = client._normalize_history_fill(row, symbol="DOGE/USDT", market_type="swap")
    assert out["symbol"] == "DOGE/USDT"
    assert out["type"] == "close_long"
    assert out["amount"] == 200
    assert out["value"] == 20
    assert out["commission"] == 0.003
    assert out["profit"] == -0.5
