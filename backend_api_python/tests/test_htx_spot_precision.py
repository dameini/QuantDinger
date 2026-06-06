from decimal import Decimal

from app.services.live_trading.htx import HtxClient


def _client():
    return HtxClient(api_key="k", secret_key="s", market_type="spot")


def test_normalize_spot_quantity_respects_amount_precision(monkeypatch):
    client = _client()
    monkeypatch.setattr(
        client,
        "_get_spot_symbol_info",
        lambda symbol: {"symbol": "dogeusdt", "amount-precision": 2},
    )

    dec, prec = client._normalize_quantity(symbol="DOGE/USDT", quantity=28.29315837, for_market=True)

    assert prec == 2
    assert dec == Decimal("28.29")


def test_place_market_sell_uses_precision_normalized_amount(monkeypatch):
    client = _client()
    monkeypatch.setattr(client, "_get_spot_account_id", lambda: "123")
    monkeypatch.setattr(
        client,
        "_get_spot_symbol_info",
        lambda symbol: {"symbol": "dogeusdt", "amount-precision": 2},
    )

    captured = {}

    def _fake_spot_private_request(method, path, *, params=None, json_body=None):
        captured["json_body"] = dict(json_body or {})
        return {"status": "ok", "data": "order-1"}

    monkeypatch.setattr(client, "_spot_private_request", _fake_spot_private_request)

    out = client.place_market_order(symbol="DOGE/USDT", side="sell", qty=28.29315837)

    assert out.exchange_order_id == "order-1"
    assert captured["json_body"]["amount"] == "28.29"
