"""Trading (class T) — paper-only by default, hard-gated for live execution.

Live execution from agents requires *all* of the following:
  1. Token has scope `T`.
  2. Token has `paper_only=false` (operator must flip explicitly).
  3. Server-side env `AGENT_LIVE_TRADING_ENABLED=true` (deployment kill switch).

Until live is unlocked, this endpoint records orders to `qd_agent_paper_orders`
using the latest market price as the simulated fill — so AI workflows can
exercise the round trip without ever touching exchange credentials.
"""
from __future__ import annotations

import os
import time
import uuid
from typing import Any

from app.services.kline import KlineService
from app.utils.agent_auth import (
    SCOPE_T, agent_required, current_token, current_user_id,
    instrument_allowed, market_allowed, paper_only, with_idempotency,
)
from app.utils.agent_jobs import record_completed_job
from app.utils.db import get_db_connection
from app.utils.logger import get_logger
from flask import request

from . import agent_v1_bp
from ._helpers import envelope, error, get_json_or_400

logger = get_logger(__name__)
_kline = KlineService()


def _live_trading_kill_switch() -> bool:
    return os.getenv("AGENT_LIVE_TRADING_ENABLED", "false").lower() in ("1", "true", "yes")


def _last_price(market: str, symbol: str) -> float | None:
    try:
        rows = _kline.get_kline(market=market, symbol=symbol, timeframe="1m", limit=1) or []
        if not rows:
            return None
        last = rows[-1]
        if isinstance(last, dict):
            for k in ("close", "c", "Close"):
                v = last.get(k)
                if v is not None:
                    return float(v)
        return None
    except Exception as exc:
        logger.warning(f"agent_v1 quick_trade last_price failed: {exc}")
        return None


def _record_paper_order(*, body: dict, fill_price: float | None, status: str, note: str = "") -> dict:
    import uuid

    order_uid = uuid.uuid4().hex
    market = (body.get("market") or "").strip()
    symbol = (body.get("symbol") or "").strip()
    side = (body.get("side") or "").strip().lower()
    order_type = (body.get("order_type") or body.get("orderType") or "market").strip().lower()
    qty = float(body.get("qty") or body.get("quantity") or 0)
    limit_price = body.get("limit_price") or body.get("limitPrice")
    if limit_price is not None:
        limit_price = float(limit_price)

    fill_value = (fill_price * qty) if (fill_price is not None and qty) else None

    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            """
            INSERT INTO qd_agent_paper_orders
              (order_uid, user_id, agent_token_id, market, symbol, side, order_type,
               qty, limit_price, fill_price, fill_value, status, note)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                order_uid, current_user_id(), int(current_token().get("id") or 0),
                market, symbol, side, order_type,
                qty, limit_price, fill_price, fill_value, status, note,
            ),
        )
        db.commit()
        cur.close()

    return {
        "order_uid": order_uid,
        "market": market,
        "symbol": symbol,
        "side": side,
        "order_type": order_type,
        "qty": qty,
        "limit_price": limit_price,
        "fill_price": fill_price,
        "fill_value": fill_value,
        "status": status,
        "paper": True,
        "note": note,
    }


def _place_live_order(*, body: dict, user_id: int) -> dict:
    credential_id = int(body.get("credential_id") or body.get("credentialId") or 0)
    market = (body.get("market") or "").strip()
    symbol = (body.get("symbol") or "").strip()
    side = (body.get("side") or "").strip().lower()
    order_type = (body.get("order_type") or body.get("orderType") or "market").strip().lower()
    qty = float(body.get("qty") or body.get("quantity") or body.get("amount") or 0)
    limit_price = body.get("limit_price") or body.get("limitPrice") or body.get("price")
    limit_price_f = float(limit_price or 0)
    market_type = (body.get("market_type") or body.get("marketType") or "spot").strip().lower()
    leverage = int(body.get("leverage") or 1)
    margin_mode = (body.get("margin_mode") or body.get("marginMode") or "").strip().lower()
    if market_type in ("futures", "future", "perp", "perpetual"):
        market_type = "swap"
    if leverage > 1:
        market_type = "swap"
    if market_type not in ("spot", "swap"):
        market_type = "spot"
    if order_type == "limit" and limit_price_f <= 0:
        raise ValueError("limit_price is required for limit orders")
    if not credential_id:
        raise ValueError("credential_id is required for live agent trading")

    from app.routes.quick_trade import _record_quick_trade, _reject_quick_trade_if_desktop_broker
    from app.services.quick_trade.credentials import build_exchange_config, create_exchange_client
    from app.services.quick_trade.orders import enrich_fill, limit_order_kwargs

    cfg_overrides: dict[str, Any] = {"market_type": market_type}
    if margin_mode in ("cross", "crossed"):
        cfg_overrides["margin_mode"] = "cross"
        cfg_overrides["td_mode"] = "cross"
    elif margin_mode in ("iso", "isolated"):
        cfg_overrides["margin_mode"] = "isolated"
        cfg_overrides["td_mode"] = "isolated"

    exchange_config = build_exchange_config(credential_id, user_id, cfg_overrides)
    exchange_id = (exchange_config.get("exchange_id") or "").strip().lower()
    if not exchange_id:
        raise ValueError("Invalid credential: missing exchange_id")
    reject = _reject_quick_trade_if_desktop_broker(exchange_id)
    if reject is not None:
        raise ValueError("Quick Trade currently supports crypto exchange API keys only.")

    client = create_exchange_client(exchange_config, market_type=market_type)

    if market_type != "spot" and leverage > 1 and hasattr(client, "set_leverage"):
        try:
            client.set_leverage(symbol=symbol, leverage=leverage)
        except TypeError:
            try:
                client.set_leverage(symbol=symbol, lever=leverage)
            except Exception:
                pass
        except Exception as exc:
            logger.warning(f"agent quick_trade set_leverage failed (non-fatal): {exc}")

    client_order_id = f"qa{str(int(time.time()))[-6:]}{uuid.uuid4().hex[:8]}"
    if order_type == "market":
        from app.services.live_trading.execution import place_order_from_signal

        if market_type == "spot":
            signal_type = "open_long" if side == "buy" else "close_long"
        else:
            signal_type = "open_long" if side == "buy" else "open_short"
        result = place_order_from_signal(
            client=client,
            signal_type=signal_type,
            symbol=symbol,
            amount=qty,
            market_type=market_type,
            exchange_config=exchange_config,
            client_order_id=client_order_id,
        )
    else:
        result = client.place_limit_order(
            symbol=symbol,
            side=side.upper() if "binance" in exchange_id else side,
            **limit_order_kwargs(client, symbol, qty, limit_price_f, side, market_type, client_order_id),
        )

    exchange_order_id = str(getattr(result, "exchange_order_id", "") or "")
    filled = float(getattr(result, "filled", 0) or 0)
    avg_fill = float(getattr(result, "avg_price", 0) or 0)
    raw = getattr(result, "raw", {}) or {}
    commission = 0.0
    commission_ccy = ""
    if exchange_order_id:
        enrich = enrich_fill(client, order_id=exchange_order_id, symbol=symbol, market_type=market_type)
        if enrich.get("filled", 0.0) > 0:
            filled = float(enrich["filled"])
        if enrich.get("avg_price", 0.0) > 0:
            avg_fill = float(enrich["avg_price"])
        commission = float(enrich.get("fee") or 0.0)
        commission_ccy = str(enrich.get("fee_ccy") or "")

    trade_id = _record_quick_trade(
        user_id=user_id,
        credential_id=credential_id,
        exchange_id=exchange_id,
        symbol=symbol,
        side=side,
        order_type=order_type,
        amount=qty,
        price=limit_price_f if order_type == "limit" else avg_fill,
        leverage=leverage,
        market_type=market_type,
        tp_price=float(body.get("tp_price") or body.get("tpPrice") or 0),
        sl_price=float(body.get("sl_price") or body.get("slPrice") or 0),
        status="filled" if filled > 0 else "submitted",
        exchange_order_id=exchange_order_id,
        filled=filled,
        avg_price=avg_fill,
        error_msg="",
        source="agent_mcp",
        raw_result=raw,
        commission=commission,
        commission_ccy=commission_ccy,
    )

    return {
        "trade_id": trade_id,
        "exchange_order_id": exchange_order_id,
        "market": market,
        "symbol": symbol,
        "side": side,
        "order_type": order_type,
        "qty": qty,
        "limit_price": limit_price_f if order_type == "limit" else None,
        "filled": filled,
        "avg_price": avg_fill,
        "status": "filled" if filled > 0 else "submitted",
        "paper": False,
    }


@agent_v1_bp.route("/quick-trade/orders", methods=["POST"])
@agent_required(SCOPE_T)
def place_order():
    """Place an order. Paper-only unless explicitly unlocked (see module doc)."""
    body, err = get_json_or_400()
    if err:
        return err

    market = (body.get("market") or "").strip()
    symbol = (body.get("symbol") or "").strip()
    side = (body.get("side") or "").strip().lower()
    qty = body.get("qty") or body.get("quantity") or body.get("amount")

    if not market or not symbol:
        return error(400, "market and symbol are required")
    if side not in ("buy", "sell"):
        return error(400, "side must be 'buy' or 'sell'")
    try:
        qty_f = float(qty)
        if qty_f <= 0:
            raise ValueError
    except Exception:
        return error(400, "qty must be a positive number")

    if not market_allowed(market):
        return error(403, f"Market not allowed: {market}", http=403)
    if not instrument_allowed(symbol):
        return error(403, f"Instrument not allowed: {symbol}", http=403)

    with with_idempotency("quick_trade_order") as existing:
        if existing:
            return envelope({
                "duplicate": True,
                "previous": existing.get("result"),
            }, message="idempotent replay")

    # Live trading is hard-gated. Even with paper_only=false on the token, the
    # operator must enable AGENT_LIVE_TRADING_ENABLED to actually route to
    # exchange clients — keeping a final environment-level kill switch.
    if (not paper_only()) and _live_trading_kill_switch():
        try:
            result = _place_live_order(body=body, user_id=current_user_id())
        except ValueError as exc:
            return error(400, str(exc), http=400)
        except Exception as exc:
            logger.error(f"agent_v1 live quick_trade failed: {exc}", exc_info=True)
            return error(500, "live quick_trade failed", details=str(exc), http=500)
        record_completed_job(
            user_id=current_user_id(),
            agent_token_id=int(current_token().get("id") or 0),
            kind="quick_trade_order",
            request_payload=body,
            result=result,
            idempotency_key=request.headers.get("Idempotency-Key"),
        )
        return envelope(result, message="live-order")

    fill_price = _last_price(market, symbol)
    note = "" if fill_price is not None else "no last price available; recorded without fill"
    status = "filled" if fill_price is not None else "rejected"
    result = _record_paper_order(body=body, fill_price=fill_price, status=status, note=note)
    record_completed_job(
        user_id=current_user_id(),
        agent_token_id=int(current_token().get("id") or 0),
        kind="quick_trade_order",
        request_payload=body,
        result=result,
        idempotency_key=request.headers.get("Idempotency-Key"),
    )
    return envelope(result, message="paper-fill")


@agent_v1_bp.route("/quick-trade/kill-switch", methods=["POST"])
@agent_required(SCOPE_T)
def kill_switch():
    """Cancel all of the calling tenant's open paper orders.

    This intentionally limits scope to the agent's own surface; revoking live
    exchange orders requires the human admin path (separate, audited).
    """
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            """
            UPDATE qd_agent_paper_orders
            SET status = 'cancelled', note = COALESCE(note,'') || ' [kill_switch]'
            WHERE user_id = %s AND status NOT IN ('filled','cancelled','rejected')
            """,
            (current_user_id(),),
        )
        affected = cur.rowcount
        db.commit()
        cur.close()
    return envelope({"cancelled_open_paper_orders": int(affected or 0)})
