"""Quick trade order helper functions."""

from __future__ import annotations

from typing import Any, Dict

from app.utils.logger import get_logger

logger = get_logger(__name__)


def enrich_fill(
    client: Any,
    *,
    order_id: str,
    symbol: str,
    market_type: str,
    max_wait_sec: float = 8.0,
) -> Dict[str, Any]:
    """Best-effort post-place fill enrichment for a Quick Trade order."""
    out = {"filled": 0.0, "avg_price": 0.0, "fee": 0.0, "fee_ccy": ""}
    oid = str(order_id or "").strip()
    if not oid:
        return out
    sym = str(symbol or "")
    market = (market_type or "swap").strip().lower()
    try:
        from app.services.live_trading.gate import GateSpotClient, GateUsdtFuturesClient
        from app.services.live_trading.okx import OkxClient
        from app.services.live_trading.symbols import (
            to_gate_currency_pair,
            to_okx_spot_inst_id,
            to_okx_swap_inst_id,
        )

        fill_result: Dict[str, Any] = {}
        if isinstance(client, OkxClient):
            inst_id = to_okx_spot_inst_id(sym) if market == "spot" else to_okx_swap_inst_id(sym)
            inst_type = "SPOT" if market == "spot" else "SWAP"
            fill_result = client.wait_for_fill(
                inst_id=inst_id,
                ord_id=oid,
                market_type=market,
                inst_type=inst_type,
                max_wait_sec=max_wait_sec,
            )
        elif isinstance(client, GateSpotClient):
            fill_result = client.wait_for_fill(order_id=oid, max_wait_sec=max_wait_sec)
        elif isinstance(client, GateUsdtFuturesClient):
            fill_result = client.wait_for_fill(
                order_id=oid,
                contract=to_gate_currency_pair(sym),
                max_wait_sec=max_wait_sec,
            )
        elif hasattr(client, "wait_for_fill"):
            try:
                fill_result = client.wait_for_fill(order_id=oid, max_wait_sec=max_wait_sec)
            except TypeError:
                try:
                    fill_result = client.wait_for_fill(symbol=sym, order_id=oid, max_wait_sec=max_wait_sec)
                except Exception as exc:
                    logger.info(
                        "enrich_fill: client %s wait_for_fill failed: %s",
                        type(client).__name__,
                        exc,
                    )
                    return out
        else:
            return out

        if isinstance(fill_result, dict):
            try:
                out["filled"] = float(fill_result.get("filled") or 0.0)
            except Exception:
                out["filled"] = 0.0
            try:
                out["avg_price"] = float(fill_result.get("avg_price") or 0.0)
            except Exception:
                out["avg_price"] = 0.0
            try:
                out["fee"] = abs(float(fill_result.get("fee") or 0.0))
            except Exception:
                out["fee"] = 0.0
            out["fee_ccy"] = str(fill_result.get("fee_ccy") or "").strip()
    except Exception as exc:
        logger.info("enrich_fill skipped: %s", exc)
    return out


def limit_order_kwargs(client, symbol, amount, price, side, market_type, client_order_id):
    """Build kwargs compatible with any exchange client's place_limit_order."""
    from app.services.live_trading.binance import BinanceFuturesClient
    from app.services.live_trading.binance_spot import BinanceSpotClient
    from app.services.live_trading.bybit import BybitClient
    from app.services.live_trading.okx import OkxClient

    if isinstance(client, (BinanceFuturesClient, BinanceSpotClient)):
        return {"quantity": amount, "price": price, "client_order_id": client_order_id}
    if isinstance(client, OkxClient):
        kwargs = {
            "market_type": market_type,
            "size": amount,
            "price": price,
            "client_order_id": client_order_id,
        }
        if market_type and market_type.strip().lower() != "spot":
            kwargs["pos_side"] = "long" if side.lower() == "buy" else "short"
        return kwargs
    if isinstance(client, BybitClient):
        return {"qty": amount, "price": price, "client_order_id": client_order_id}
    return {"size": amount, "price": price, "client_order_id": client_order_id}
