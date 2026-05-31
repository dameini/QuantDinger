"""HTX trade history synchronization for strategy trade records."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.services.exchange_execution import load_strategy_configs, resolve_exchange_config
from app.services.live_trading.factory import create_client
from app.services.live_trading.htx import HtxClient
from app.services.live_trading.records import ensure_strategy_trades_close_reason_column, normalize_strategy_symbol
from app.utils.db import get_db_connection
from app.utils.logger import get_logger


logger = get_logger(__name__)


def _history_first_value(row: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in row and row.get(key) not in (None, ""):
            return row.get(key)
    return None


def _dt_to_ms(value: Any) -> int:
    if not value:
        return 0
    if isinstance(value, (int, float)):
        v = float(value)
        return int(v * 1000) if v < 10_000_000_000 else int(v)
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except Exception:
        return 0


def _ms_to_db_dt(ms: int) -> datetime:
    if not ms:
        return datetime.now(timezone.utc).replace(tzinfo=None)
    return datetime.fromtimestamp(int(ms) / 1000.0, tz=timezone.utc).replace(tzinfo=None)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except Exception:
        return default


def _normalize_cached_order_fill(row: Dict[str, Any], *, symbol: str, market_type: str) -> Dict[str, Any]:
    raw_symbol = str(_history_first_value(row, "symbol", "contract_code", "contractCode") or symbol or "").strip()
    hb_symbol = raw_symbol.upper().replace("-SWAP", "").replace("-", "/")
    if "/" not in hb_symbol and hb_symbol.endswith("USDT") and len(hb_symbol) > 4:
        hb_symbol = f"{hb_symbol[:-4]}/USDT"

    order_id = str(_history_first_value(row, "order_id", "order-id", "orderId", "order_id_str", "id") or "").strip()
    client_order_id = str(
        _history_first_value(row, "client_order_id", "client-order-id", "clientOrderId", "clientOrderID", "client_oid") or ""
    ).strip()
    trade_id = str(_history_first_value(row, "trade_id", "trade-id", "match_id", "match-id", "id") or "").strip()
    ts_ms = _dt_to_ms(
        _history_first_value(row, "created-at", "created_at", "createdAt", "created_time", "createdTime", "updated_time", "updatedTime", "time", "ts")
    )
    side_raw = str(_history_first_value(row, "side", "direction", "type", "order_type", "orderType") or "").lower()
    signal_type = str(row.get("_pending_signal_type") or "").lower()
    reduce_only = str(_history_first_value(row, "reduce_only", "reduceOnly") or "").lower() in ("1", "true", "yes")
    if signal_type in ("open_long", "add_long", "close_short"):
        side = "buy"
    elif signal_type in ("open_short", "add_short", "close_long"):
        side = "sell"
    elif "buy" in side_raw:
        side = "buy"
    elif "sell" in side_raw:
        side = "sell"
    else:
        side = side_raw

    if market_type == "spot":
        trade_type = "buy" if side == "buy" else "sell"
    elif signal_type:
        trade_type = signal_type
    elif side == "buy" and reduce_only:
        trade_type = "close_short"
    elif side == "sell" and reduce_only:
        trade_type = "close_long"
    elif side == "sell":
        trade_type = "open_short"
    else:
        trade_type = "open_long"

    price = _safe_float(_history_first_value(row, "trade_avg_price", "avg_price", "price", "trade_price", "tradePrice", "match_price", "matchPrice"))
    qty = _safe_float(_history_first_value(row, "trade_volume", "tradeVolume", "filled", "filled_amount", "filled-amount", "amount", "qty", "quantity", "volume"))
    value = _safe_float(_history_first_value(row, "trade_turnover", "tradeTurnover", "value", "filled_cash_amount", "filled-cash-amount"))
    if market_type == "swap" and value > 0 and price > 0:
        qty = value / price
    if market_type == "swap" and qty > 0:
        contract_size = _safe_float(row.get("_contract_size"), 1.0)
        if contract_size > 0:
            qty *= contract_size
    if value <= 0 and price > 0 and qty > 0:
        value = price * qty
    fee = abs(_safe_float(_history_first_value(row, "fee", "trade_fee", "tradeFee", "filled_fees", "filled-fees", "commission")))
    fee_ccy = str(_history_first_value(row, "fee_currency", "feeCurrency", "fee_asset", "feeAsset", "commissionAsset") or "USDT").upper()
    profit = _safe_float(_history_first_value(row, "real_profit", "realProfit", "profit", "realized_pnl", "realizedPnl", "trade_profit", "tradeProfit"), 0.0)
    return {
        "symbol": hb_symbol,
        "market_type": market_type,
        "order_id": order_id,
        "client_order_id": client_order_id,
        "trade_id": trade_id,
        "trade_time_ms": ts_ms,
        "type": trade_type,
        "price": price,
        "amount": qty,
        "value": value,
        "commission": fee,
        "commission_ccy": fee_ccy,
        "profit": profit,
        "raw": row,
    }


def _parse_strategy_from_client_order_id(client_order_id: str, *, current_strategy_id: int) -> Optional[int]:
    coid = str(client_order_id or "").strip()
    if not coid:
        return None
    m = re.match(r"^qd_(\d+)_\d+", coid)
    if m:
        return int(m.group(1))
    compact_prefix = f"qd{int(current_strategy_id)}"
    if coid.startswith(compact_prefix):
        return int(current_strategy_id)
    return None


def _trade_external_key(row: Dict[str, Any]) -> str:
    symbol = normalize_strategy_symbol(str(row.get("symbol") or ""))
    return "htx:{market_type}:{symbol}:{order_id}:{client_order_id}:{trade_id}:{trade_time_ms}".format(
        market_type=str(row.get("market_type") or ""),
        symbol=symbol,
        order_id=str(row.get("order_id") or ""),
        client_order_id=str(row.get("client_order_id") or ""),
        trade_id=str(row.get("trade_id") or ""),
        trade_time_ms=int(row.get("trade_time_ms") or 0),
    )


def _strategy_sync_start_ms(strategy_id: int) -> int:
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            """
            SELECT MIN(COALESCE(executed_at, created_at)) AS t
            FROM pending_orders
            WHERE strategy_id = %s
            """,
            (int(strategy_id),),
        )
        row = cur.fetchone() or {}
        start_ms = _dt_to_ms(row.get("t"))
        if not start_ms:
            cur.execute(
                """
                SELECT MIN(created_at) AS t
                FROM qd_strategy_trades
                WHERE strategy_id = %s
                """,
                (int(strategy_id),),
            )
            row = cur.fetchone() or {}
            start_ms = _dt_to_ms(row.get("t"))
        if not start_ms:
            cur.execute("SELECT created_at FROM qd_strategies_trading WHERE id = %s", (int(strategy_id),))
            row = cur.fetchone() or {}
            start_ms = _dt_to_ms(row.get("created_at"))
        cur.close()
    return start_ms


def _lookup_strategy_by_exchange_order(user_id: int, exchange_order_id: str) -> Optional[int]:
    if not exchange_order_id:
        return None
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            """
            SELECT strategy_id
            FROM pending_orders
            WHERE user_id = %s AND exchange_order_id = %s AND strategy_id IS NOT NULL
            ORDER BY id DESC
            LIMIT 1
            """,
            (int(user_id), str(exchange_order_id)),
        )
        row = cur.fetchone() or {}
        cur.close()
    try:
        return int(row.get("strategy_id")) if row.get("strategy_id") else None
    except Exception:
        return None


def _find_local_trade_for_update(*, user_id: int, strategy_id: int, row: Dict[str, Any]) -> int:
    trade_time = _ms_to_db_dt(int(row.get("trade_time_ms") or 0))
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            """
            SELECT id
            FROM qd_strategy_trades
            WHERE user_id = %s
              AND strategy_id = %s
              AND COALESCE(source, 'local') = 'local'
              AND COALESCE(external_key, '') = ''
              AND symbol = %s
              AND type = %s
              AND created_at BETWEEN %s - INTERVAL '1 day' AND %s + INTERVAL '1 day'
            ORDER BY ABS(EXTRACT(EPOCH FROM (created_at - %s))) ASC, id DESC
            LIMIT 1
            """,
            (
                int(user_id),
                int(strategy_id),
                normalize_strategy_symbol(str(row.get("symbol") or "")),
                str(row.get("type") or ""),
                trade_time,
                trade_time,
                trade_time,
            ),
        )
        found = cur.fetchone() or {}
        cur.close()
    try:
        return int(found.get("id") or 0)
    except Exception:
        return 0


def _pending_order_detail_rows(
    *,
    user_id: int,
    strategy_id: int,
    market_type: str,
    start_ms: int,
    end_ms: int,
) -> List[Dict[str, Any]]:
    start_dt = _ms_to_db_dt(start_ms) if start_ms else None
    end_dt = _ms_to_db_dt(end_ms) if end_ms else None
    params: List[Any] = [int(user_id), int(strategy_id), str(market_type)]
    where = """
        user_id = %s
        AND strategy_id = %s
        AND COALESCE(market_type, '') = %s
        AND COALESCE(exchange_id, '') IN ('htx', 'huobi')
        AND COALESCE(exchange_order_id, '') <> ''
    """
    if start_dt:
        where += " AND COALESCE(executed_at, created_at) >= %s"
        params.append(start_dt)
    if end_dt:
        where += " AND COALESCE(executed_at, created_at) <= %s"
        params.append(end_dt)
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            f"""
            SELECT id, symbol, signal_type, amount, price, exchange_order_id,
                   exchange_response_json, executed_at, created_at
            FROM pending_orders
            WHERE {where}
            ORDER BY COALESCE(executed_at, created_at) ASC, id ASC
            LIMIT 200
            """,
            tuple(params),
        )
        rows = cur.fetchall() or []
        cur.close()
    return [dict(r) for r in rows]


def _extract_cached_order_detail(pending_row: Dict[str, Any]) -> Dict[str, Any]:
    raw_text = pending_row.get("exchange_response_json") or ""
    if not raw_text:
        return {}
    try:
        obj = json.loads(raw_text) if isinstance(raw_text, str) else raw_text
    except Exception:
        return {}
    if not isinstance(obj, dict):
        return {}
    phases = obj.get("phases") if isinstance(obj.get("phases"), dict) else {}
    market_query = phases.get("market_query") if isinstance(phases.get("market_query"), dict) else {}
    order = market_query.get("order") if isinstance(market_query.get("order"), dict) else {}
    if order:
        detail = dict(order)
    else:
        detail = obj if obj else {}
    if detail:
        detail.setdefault("_pending_signal_type", pending_row.get("signal_type") or "")
        detail.setdefault("order_id", pending_row.get("exchange_order_id") or "")
        detail.setdefault("symbol", pending_row.get("symbol") or "")
        detail.setdefault("created_time", _dt_to_ms(pending_row.get("executed_at") or pending_row.get("created_at")))
    return detail


def _cached_pending_fills(
    *,
    user_id: int,
    strategy_id: int,
    market_type: str,
    start_ms: int,
    end_ms: int,
    symbol: str,
) -> List[Dict[str, Any]]:
    pending_rows = _pending_order_detail_rows(
        user_id=int(user_id),
        strategy_id=int(strategy_id),
        market_type=market_type,
        start_ms=start_ms,
        end_ms=end_ms,
    )
    fallback_rows = []
    for pending in pending_rows:
        detail = _extract_cached_order_detail(pending)
        if not detail:
            continue
        fallback_rows.append(_normalize_cached_order_fill(detail, symbol=symbol, market_type=market_type))
    return [r for r in fallback_rows if r.get("symbol") and float(r.get("amount") or 0.0) > 0]


def _upsert_exchange_trade(
    *,
    user_id: int,
    current_strategy_id: int,
    row: Dict[str, Any],
) -> Tuple[str, str]:
    external_key = _trade_external_key(row)
    symbol = normalize_strategy_symbol(str(row.get("symbol") or ""))
    exchange_order_id = str(row.get("order_id") or "")
    client_order_id = str(row.get("client_order_id") or "")
    attributed_sid = _parse_strategy_from_client_order_id(client_order_id, current_strategy_id=current_strategy_id)
    if attributed_sid is None:
        attributed_sid = _lookup_strategy_by_exchange_order(user_id, exchange_order_id)

    attribution_status = "strategy" if attributed_sid == int(current_strategy_id) else "unassigned"
    strategy_id_for_db = int(current_strategy_id) if attribution_status == "strategy" else None
    raw_json = json.dumps(row.get("raw") or {}, ensure_ascii=False, default=str)
    created_at = _ms_to_db_dt(int(row.get("trade_time_ms") or 0))

    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            "SELECT id FROM qd_strategy_trades WHERE user_id = %s AND external_key = %s",
            (int(user_id), external_key),
        )
        existing = cur.fetchone() or {}
        existing_id = int(existing.get("id") or 0)
        if existing_id:
            cur.execute(
                """
                UPDATE qd_strategy_trades
                SET strategy_id = %s,
                    symbol = %s,
                    type = %s,
                    price = %s,
                    amount = %s,
                    value = %s,
                    commission = %s,
                    commission_ccy = %s,
                    profit = %s,
                    source = 'htx',
                    exchange_id = 'htx',
                    market_type = %s,
                    external_key = %s,
                    exchange_order_id = %s,
                    client_order_id = %s,
                    exchange_trade_id = %s,
                    attribution_status = %s,
                    raw_exchange_json = %s,
                    created_at = %s
                WHERE id = %s
                """,
                (
                    strategy_id_for_db,
                    symbol,
                    str(row.get("type") or ""),
                    _safe_float(row.get("price")),
                    _safe_float(row.get("amount")),
                    _safe_float(row.get("value")),
                    _safe_float(row.get("commission")),
                    str(row.get("commission_ccy") or ""),
                    _safe_float(row.get("profit")),
                    str(row.get("market_type") or ""),
                    external_key,
                    exchange_order_id,
                    client_order_id,
                    str(row.get("trade_id") or ""),
                    attribution_status,
                    raw_json,
                    created_at,
                    int(existing_id),
                ),
            )
            db.commit()
            cur.close()
            return "updated", attribution_status

        cur.execute(
            """
            INSERT INTO qd_strategy_trades
            (user_id, strategy_id, symbol, type, price, amount, value, commission,
             commission_ccy, profit, close_reason, matched_entry_price, grid_matched_profit,
             source, exchange_id, market_type, external_key, exchange_order_id, client_order_id,
             exchange_trade_id, attribution_status, raw_exchange_json, created_at)
            VALUES
            (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                int(user_id),
                strategy_id_for_db,
                symbol,
                str(row.get("type") or ""),
                _safe_float(row.get("price")),
                _safe_float(row.get("amount")),
                _safe_float(row.get("value")),
                _safe_float(row.get("commission")),
                str(row.get("commission_ccy") or ""),
                _safe_float(row.get("profit")),
                "",
                0.0,
                _safe_float(row.get("profit")),
                "htx",
                "htx",
                str(row.get("market_type") or ""),
                external_key,
                exchange_order_id,
                client_order_id,
                str(row.get("trade_id") or ""),
                attribution_status,
                raw_json,
                created_at,
            ),
        )
        db.commit()
        cur.close()
    return "inserted", attribution_status


class HtxTradeSyncService:
    """Synchronize HTX exchange fills into qd_strategy_trades."""

    def sync_strategy(
        self,
        *,
        user_id: int,
        strategy_id: int,
        from_ts: Optional[Any] = None,
        to_ts: Optional[Any] = None,
    ) -> Dict[str, Any]:
        ensure_strategy_trades_close_reason_column()
        sc = load_strategy_configs(int(strategy_id))
        exchange_config = resolve_exchange_config(sc.get("exchange_config") or {}, user_id=int(user_id))
        exchange_id = str(exchange_config.get("exchange_id") or "").strip().lower()
        if exchange_id not in ("htx", "huobi"):
            raise ValueError(f"strategy exchange is not HTX: {exchange_id or 'empty'}")

        symbol = normalize_strategy_symbol(str(sc.get("symbol") or (sc.get("trading_config") or {}).get("symbol") or ""))
        if not symbol:
            raise ValueError("strategy symbol is empty")

        start_ms = _dt_to_ms(from_ts) if from_ts else _strategy_sync_start_ms(int(strategy_id))
        end_ms = _dt_to_ms(to_ts) if to_ts else int(datetime.now(timezone.utc).timestamp() * 1000)
        counts = {"inserted": 0, "updated": 0, "unassigned": 0, "skipped": 0}
        synced: List[Dict[str, Any]] = []
        market_results: List[Dict[str, Any]] = []

        for market_type in ("spot", "swap"):
            market_result = {
                "market_type": market_type,
                "fetched": 0,
                "inserted": 0,
                "updated": 0,
                "unassigned": 0,
                "skipped": 0,
                "error": "",
            }
            try:
                cfg = dict(exchange_config)
                cfg["exchange_id"] = "htx"
                client = create_client(cfg, market_type=market_type)
                if not isinstance(client, HtxClient):
                    counts["skipped"] += 1
                    market_result["skipped"] += 1
                    market_result["error"] = "created client is not HtxClient"
                    market_results.append(market_result)
                    continue
                rows = client.get_trade_history(symbol=symbol, start_ms=start_ms, end_ms=end_ms, limit=100)
                market_result["fetched"] = len(rows or [])
                if not rows:
                    fallback_rows = []
                    pending_rows = _pending_order_detail_rows(
                        user_id=int(user_id),
                        strategy_id=int(strategy_id),
                        market_type=market_type,
                        start_ms=start_ms,
                        end_ms=end_ms,
                    )
                    for pending in pending_rows:
                        order_id = str(pending.get("exchange_order_id") or "")
                        detail: Dict[str, Any] = {}
                        try:
                            detail = client.get_order(symbol=str(pending.get("symbol") or symbol), order_id=order_id)
                        except Exception as detail_err:
                            logger.info(
                                "HTX trade sync order detail fallback using cached response: strategy_id=%s market=%s order_id=%s err=%s",
                                strategy_id,
                                market_type,
                                order_id,
                                detail_err,
                            )
                            detail = _extract_cached_order_detail(pending)
                        if not detail:
                            continue
                        detail.setdefault("order_id", order_id)
                        detail.setdefault("symbol", pending.get("symbol") or symbol)
                        detail.setdefault("type", pending.get("signal_type") or "")
                        detail.setdefault("created_time", _dt_to_ms(pending.get("executed_at") or pending.get("created_at")))
                        fallback_rows.append(detail)
                    if fallback_rows:
                        rows = [
                            client._normalize_history_fill(r, symbol=symbol, market_type=market_type)
                            for r in fallback_rows
                        ]
                        rows = [r for r in rows if r.get("symbol") and float(r.get("amount") or 0.0) > 0]
                        market_result["fetched"] = len(rows)
                        market_result["source"] = "order_detail_fallback"
            except Exception as e:
                rows = _cached_pending_fills(
                    user_id=int(user_id),
                    strategy_id=int(strategy_id),
                    market_type=market_type,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    symbol=symbol,
                )
                if rows:
                    market_result["fetched"] = len(rows)
                    market_result["source"] = "cached_pending_order"
                    market_result["error"] = str(e)
                    logger.info(
                        "HTX trade sync used cached pending orders: strategy_id=%s market=%s symbol=%s rows=%s err=%s",
                        strategy_id,
                        market_type,
                        symbol,
                        len(rows),
                        e,
                    )
                else:
                    logger.warning(
                        "HTX trade sync failed: strategy_id=%s market=%s symbol=%s err=%s",
                        strategy_id,
                        market_type,
                        symbol,
                        e,
                    )
                    counts["skipped"] += 1
                    market_result["skipped"] += 1
                    market_result["error"] = str(e)
                    market_results.append(market_result)
                    continue

            for row in rows:
                try:
                    action, attribution = _upsert_exchange_trade(
                        user_id=int(user_id),
                        current_strategy_id=int(strategy_id),
                        row=row,
                    )
                    counts[action] += 1
                    market_result[action] += 1
                    if attribution == "unassigned":
                        counts["unassigned"] += 1
                        market_result["unassigned"] += 1
                    synced.append({"external_key": _trade_external_key(row), "attribution_status": attribution})
                except Exception as e:
                    logger.warning("HTX trade sync row skipped: strategy_id=%s row=%s err=%s", strategy_id, row, e)
                    counts["skipped"] += 1
                    market_result["skipped"] += 1

            market_results.append(market_result)

        logger.info(
            "HTX trade sync completed: strategy_id=%s symbol=%s from_ts=%s to_ts=%s counts=%s markets=%s",
            strategy_id,
            symbol,
            start_ms,
            end_ms,
            counts,
            market_results,
        )

        return {
            "strategy_id": int(strategy_id),
            "symbol": symbol,
            "from_ts": start_ms,
            "to_ts": end_ms,
            **counts,
            "market_results": market_results,
            "synced": synced[:50],
        }
