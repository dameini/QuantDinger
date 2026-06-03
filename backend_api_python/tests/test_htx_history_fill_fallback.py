from app.services.live_trading.htx import HtxClient


def _client():
    return HtxClient(api_key="k", secret_key="s", market_type="swap")


def test_normalize_swap_order_detail_fallback_keeps_close_long(monkeypatch):
    client = _client()
    monkeypatch.setattr(client, "get_contract_info", lambda symbol: {"contract_size": 100})
    row = {
        "_pending_signal_type": "close_long",
        "contract_code": "DOGE-USDT",
        "order_id": "1511173803730726912",
        "client_order_id": "6248",
        "side": "sell",
        "type": "market",
        "position_side": "long",
        "reduce_only": True,
        "trade_avg_price": "0.099927",
        "trade_volume": "1",
        "trade_turnover": "9.9927",
        "fee_currency": "USDT",
        "fee": "0.00599562",
        "real_profit": "-0.0413",
        "created_time": "1780333511060",
    }

    out = client._normalize_history_fill(row, symbol="DOGE/USDT", market_type="swap")

    assert out["symbol"] == "DOGE/USDT"
    assert out["type"] == "close_long"
    assert out["amount"] == 100
    assert out["value"] == 9.9927
    assert out["commission"] == 0.00599562
    assert out["profit"] == -0.0413
