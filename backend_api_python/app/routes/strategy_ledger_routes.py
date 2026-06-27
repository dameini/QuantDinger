"""Strategy ledger, equity curve, and performance routes."""
from datetime import datetime, timezone
import time
import traceback

from flask import g, jsonify, request

from app.routes.strategy_blueprint import strategy_blp
from app.routes.strategy_services import get_strategy_service
from app.utils.auth import login_required
from app.utils.db import get_db_connection
from app.utils.logger import get_logger
from app.utils.pnl import (
    calc_margin_notional,
    calc_notional_value,
    is_derivatives_market,
)


logger = get_logger(__name__)


def _normalize_trade_row_for_api(trade: dict, *, leverage: float = 1.0, market_type: str = "spot") -> dict:
    """Ensure numeric fields are JSON-friendly floats."""
    try:
        from decimal import Decimal
    except Exception:  # pragma: no cover
        Decimal = ()  # type: ignore
    out = dict(trade)
    for k in (
        "price",
        "amount",
        "value",
        "commission",
        "profit",
        "profit_gross",
        "net_pnl",
        "open_commission_allocated",
        "close_commission",
        "total_commission",
    ):
        v = out.get(k)
        if isinstance(v, Decimal):
            out[k] = float(v)
    try:
        price = float(out.get("price") or 0.0)
        amount = float(out.get("amount") or 0.0)
        value = float(out.get("value") or 0.0)
        if value <= 0 and price > 0 and amount > 0:
            value = calc_notional_value(price, amount)
            out["value"] = value
        out["notional_value"] = value
        out["margin_value"] = calc_margin_notional(value, leverage, market_type)
        profit = out.get("profit")
        if profit is not None:
            gross = out.get("profit_gross")
            if gross is None:
                gross = profit
            try:
                gross_f = float(gross)
            except Exception:
                gross_f = float(profit or 0.0)
            open_comm = float(out.get("open_commission_allocated") or 0.0)
            close_comm = float(
                out.get("close_commission")
                if out.get("close_commission") is not None
                else out.get("commission") or 0.0
            )
            net = float(profit)
            if out.get("net_pnl") is None:
                net = gross_f - close_comm - open_comm
                out["net_pnl"] = round(net, 8)
            if out.get("profit_gross") is None:
                out["profit_gross"] = gross_f
            if out.get("total_commission") is None:
                out["total_commission"] = round(close_comm + open_comm, 8)
            out["profit"] = round(net, 8)
            margin = float(out.get("margin_value") or 0.0)
            if margin > 0:
                out["profit_pct_on_margin"] = round(net / margin * 100.0, 4)
            else:
                out["profit_pct_on_margin"] = 0.0
            if value > 0:
                out["profit_pct_on_notional"] = round(net / value * 100.0, 4)
            else:
                out["profit_pct_on_notional"] = 0.0
    except Exception:
        pass
    return out


@strategy_blp.route('/strategies/trades', methods=['GET'])
@login_required
def get_trades():
    """Get trade records for the current user's strategy."""
    try:
        user_id = g.user_id
        strategy_id = request.args.get('id', type=int)
        if not strategy_id:
            return jsonify({'code': 0, 'msg': 'Missing strategy id parameter', 'data': {'trades': [], 'items': []}}), 400

        st = get_strategy_service().get_strategy(strategy_id, user_id=user_id)
        if not st:
            return jsonify({'code': 0, 'msg': 'Strategy not found', 'data': {'trades': [], 'items': []}}), 404

        trading_config = st.get("trading_config") if isinstance(st.get("trading_config"), dict) else {}
        try:
            leverage = float(trading_config.get("leverage") or st.get("leverage") or 1.0)
        except Exception:
            leverage = 1.0
        if leverage <= 0:
            leverage = 1.0
        market_type = str(trading_config.get("market_type") or st.get("market_type") or "swap").strip().lower()
        if is_derivatives_market(market_type):
            market_type = "swap"

        from app.services.live_trading.records import ensure_strategy_trades_close_reason_column
        ensure_strategy_trades_close_reason_column()

        bot_type = str(trading_config.get("bot_type") or "").strip().lower()
        lang = str(request.args.get("lang") or request.headers.get("Accept-Language") or "zh")[:2].lower()
        if not lang.startswith("zh"):
            lang = "en"
        else:
            lang = "zh"
        include_exchange = str(request.args.get("include_exchange") or "").strip().lower() in ("1", "true", "yes")
        extra_cols = """
                       source, exchange_id, market_type, external_key,
                       exchange_order_id, client_order_id, exchange_trade_id,
                       attribution_status,
        """

        with get_db_connection() as db:
            cur = db.cursor()
            cur.execute(
                f"""
                SELECT id, strategy_id, symbol, type, price, amount, value,
                       commission, commission_ccy, profit, close_reason,
                       matched_entry_price, grid_matched_profit,
                       {extra_cols}
                       created_at
                FROM qd_strategy_trades
                WHERE strategy_id = ?
                ORDER BY id DESC
                """,
                (strategy_id,)
            )
            rows = cur.fetchall() or []
            cur.close()

        from app.utils.trade_close_reason import enrich_trade_row
        from app.utils.trade_net_pnl import enrich_trades_net_pnl
        from app.services.live_trading.records import normalize_strategy_symbol

        def _process_trade_rows(input_rows):
            processed_rows = []
            for row in input_rows:
                trade = dict(row)
                created_at = trade.get('created_at')
                if created_at:
                    if hasattr(created_at, 'timestamp'):
                        dt = created_at
                        if getattr(dt, 'tzinfo', None) is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        trade['created_at'] = int(dt.timestamp())
                    elif isinstance(created_at, str):
                        try:
                            dt = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
                            if getattr(dt, 'tzinfo', None) is None:
                                dt = dt.replace(tzinfo=timezone.utc)
                            trade['created_at'] = int(dt.timestamp())
                        except Exception:
                            pass

                trade = enrich_trade_row(trade, bot_type=bot_type, lang=lang)
                processed_rows.append(
                    _normalize_trade_row_for_api(trade, leverage=leverage, market_type=market_type)
                )
            return processed_rows

        def _trade_source(row):
            if str(row.get("attribution_status") or "") == "unassigned":
                return "htx_unassigned"
            return "htx" if str(row.get("source") or "local") == "htx" else "local"

        def _compare_trade_rows(all_rows):
            local_rows = [r for r in all_rows if _trade_source(r) == "local"]
            cloud_rows = [r for r in all_rows if _trade_source(r) in ("htx", "htx_unassigned")]
            used_cloud_ids = set()
            time_limit = 300
            for local in local_rows:
                best = None
                best_score = None
                for cloud in cloud_rows:
                    cid = cloud.get("id")
                    if cid in used_cloud_ids:
                        continue
                    if normalize_strategy_symbol(str(local.get("symbol") or "")) != normalize_strategy_symbol(str(cloud.get("symbol") or "")):
                        continue
                    if str(local.get("type") or "").lower() != str(cloud.get("type") or "").lower():
                        continue
                    try:
                        tdiff = abs(float(local.get("created_at") or 0) - float(cloud.get("created_at") or 0))
                        adiff = abs(float(local.get("amount") or 0) - float(cloud.get("amount") or 0))
                        pdiff = abs(float(local.get("price") or 0) - float(cloud.get("price") or 0))
                    except Exception:
                        continue
                    score = tdiff + adiff * 10_000 + pdiff * 10_000
                    if best is None or score < best_score:
                        best = (cloud, tdiff, adiff, pdiff)
                        best_score = score
                if not best:
                    local["compare_status"] = "local_only"
                    local["compare_note"] = "no_exchange_match"
                    continue
                cloud, tdiff, adiff, pdiff = best
                amount_tol = max(1e-8, abs(float(local.get("amount") or 0)) * 0.001)
                price_tol = max(1e-8, abs(float(local.get("price") or 0)) * 0.001)
                matched = tdiff <= time_limit and adiff <= amount_tol and pdiff <= price_tol
                status = "matched" if matched else "mismatch"
                note = f"time_diff={round(tdiff, 3)}s amount_diff={round(adiff, 10)} price_diff={round(pdiff, 10)}"
                local["compare_status"] = status
                local["compare_note"] = note
                local["matched_exchange_trade_id"] = cloud.get("id")
                cloud["compare_status"] = status
                cloud["compare_note"] = note
                cloud["matched_local_trade_id"] = local.get("id")
                used_cloud_ids.add(cloud.get("id"))
            for cloud in cloud_rows:
                if not cloud.get("compare_status"):
                    cloud["compare_status"] = "cloud_only"
                    cloud["compare_note"] = "no_local_match"

        processed_rows = _process_trade_rows(rows)
        unassigned_rows = []
        unassigned_summary = {"count": 0, "value": 0.0, "commission": 0.0, "profit": 0.0}
        if include_exchange:
            sync_symbol = normalize_strategy_symbol(str(st.get("symbol") or trading_config.get("symbol") or ""))
            with get_db_connection() as db:
                cur = db.cursor()
                cur.execute(
                    f"""
                    SELECT id, strategy_id, symbol, type, price, amount, value,
                           commission, commission_ccy, profit, close_reason,
                           matched_entry_price, grid_matched_profit,
                           {extra_cols}
                           created_at
                    FROM qd_strategy_trades
                    WHERE user_id = ?
                      AND strategy_id IS NULL
                      AND exchange_id = 'htx'
                      AND attribution_status = 'unassigned'
                      AND symbol = ?
                    ORDER BY id DESC
                    """,
                    (user_id, sync_symbol),
                )
                unassigned_raw = cur.fetchall() or []
                cur.close()
            unassigned_rows = _process_trade_rows(unassigned_raw)
            enrich_trades_net_pnl(processed_rows + unassigned_rows)
            _compare_trade_rows(processed_rows + unassigned_rows)
            for tr in unassigned_rows:
                unassigned_summary["count"] += 1
                unassigned_summary["value"] += float(tr.get("value") or 0.0)
                unassigned_summary["commission"] += float(tr.get("commission") or 0.0)
                unassigned_summary["profit"] += float(tr.get("profit") or 0.0)
            for key in ("value", "commission", "profit"):
                unassigned_summary[key] = round(float(unassigned_summary[key]), 8)
        else:
            enrich_trades_net_pnl(processed_rows)

        return jsonify({
            'code': 1,
            'msg': 'success',
            'data': {
                'trades': processed_rows,
                'items': processed_rows,
                'exchange_unassigned_trades': unassigned_rows,
                'exchange_unassigned_summary': unassigned_summary,
            }
        })
    except Exception as e:
        logger.error(f"get_trades failed: {str(e)}")
        logger.error(traceback.format_exc())
        return jsonify({'code': 0, 'msg': str(e), 'data': {'trades': [], 'items': []}}), 500


@strategy_blp.route('/strategies/trades/sync', methods=['POST'])
@login_required
def sync_strategy_trades():
    """Manually sync HTX exchange trade history into strategy trade records."""
    try:
        user_id = g.user_id
        strategy_id = request.args.get('id', type=int)
        if not strategy_id:
            return jsonify({'code': 0, 'msg': 'Missing strategy id parameter', 'data': None}), 400

        st = get_strategy_service().get_strategy(strategy_id, user_id=user_id)
        if not st:
            return jsonify({'code': 0, 'msg': 'Strategy not found', 'data': None}), 404

        payload = request.get_json(silent=True) or {}
        from_ts = payload.get("from_ts") or request.args.get("from_ts")
        to_ts = payload.get("to_ts") or request.args.get("to_ts")
        from app.services.htx_trade_sync import HtxTradeSyncService

        result = HtxTradeSyncService().sync_strategy(
            user_id=int(user_id),
            strategy_id=int(strategy_id),
            from_ts=from_ts,
            to_ts=to_ts,
        )
        logger.info("sync_strategy_trades completed: user_id=%s strategy_id=%s result=%s", user_id, strategy_id, result)
        return jsonify({'code': 1, 'msg': 'success', 'data': result})
    except Exception as e:
        logger.error(f"sync_strategy_trades failed: {str(e)}")
        logger.error(traceback.format_exc())
        return jsonify({'code': 0, 'msg': str(e), 'data': None}), 500


def _trade_row_timestamp(row: dict) -> int:
    created_at = row.get("created_at")
    if created_at and hasattr(created_at, "timestamp"):
        dt = created_at
        if getattr(dt, "tzinfo", None) is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    if created_at:
        try:
            return int(created_at)
        except Exception:
            pass
    return int(time.time())


def _strategy_performance_summary(initial: float, curve: list) -> dict:
    """Unified KPI math for strategy detail header + performance tab."""
    init = float(initial or 0.0)
    if init <= 0:
        init = 1000.0
    latest = float(curve[-1].get("equity") or init) if curve else init
    total_return = latest - init
    total_return_pct = (total_return / init * 100.0) if init > 0 else 0.0

    peak = init
    max_drawdown = 0.0
    max_drawdown_pct = 0.0
    for pt in curve or []:
        eq = float(pt.get("equity") or 0.0)
        if eq > peak:
            peak = eq
        dd = peak - eq
        if dd > max_drawdown:
            max_drawdown = dd
        if peak > 0:
            dd_pct = dd / peak * 100.0
            if dd_pct > max_drawdown_pct:
                max_drawdown_pct = dd_pct

    return {
        "initial_equity": round(init, 2),
        "latest_equity": round(latest, 2),
        "total_return": round(total_return, 2),
        "total_return_pct": round(total_return_pct, 2),
        "max_drawdown": round(max_drawdown, 2),
        "max_drawdown_pct": round(max_drawdown_pct, 2),
    }


def _build_strategy_equity_curve(user_id: int, strategy_id: int):
    st = get_strategy_service().get_strategy(strategy_id, user_id=user_id) or {}
    if not st:
        return None, 'Strategy not found'

    initial = float(st.get('initial_capital') or (st.get('trading_config') or {}).get('initial_capital') or 0)
    if initial <= 0:
        initial = 1000.0

    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            """
            SELECT created_at, profit, commission
            FROM qd_strategy_trades
            WHERE strategy_id = ?
            ORDER BY created_at ASC
            """,
            (strategy_id,)
        )
        rows = cur.fetchall() or []
        cur.execute(
            """
            SELECT COALESCE(SUM(unrealized_pnl), 0) AS u
            FROM qd_strategy_positions
            WHERE strategy_id = ?
            """,
            (strategy_id,),
        )
        prow = cur.fetchone() or {}
        cur.close()

    equity = initial
    curve = []
    if rows:
        anchor_ts = _trade_row_timestamp(rows[0])
        curve.append({"time": anchor_ts, "equity": round(initial, 2)})

    from app.utils.trade_net_pnl import enrich_trades_net_pnl, net_pnl_for_equity_step

    trade_rows = [dict(r) for r in rows]
    enrich_trades_net_pnl(trade_rows)
    for r in trade_rows:
        try:
            equity += float(net_pnl_for_equity_step(r))
        except Exception:
            pass
        curve.append({'time': _trade_row_timestamp(r), 'equity': round(equity, 2)})

    try:
        unreal = float(prow.get('u') or prow.get('U') or 0)
    except Exception:
        unreal = 0.0
    live_equity = float(equity) + unreal
    now_ts = int(time.time())
    if abs(unreal) > 1e-12 or not curve:
        curve.append({'time': now_ts, 'equity': round(live_equity, 2)})

    return curve, None


@strategy_blp.route('/strategies/equityCurve', methods=['GET'])
@login_required
def get_equity_curve():
    """Get equity curve for the current user's strategy."""
    try:
        user_id = g.user_id
        strategy_id = request.args.get('id', type=int)
        if not strategy_id:
            return jsonify({'code': 0, 'msg': 'Missing strategy id parameter', 'data': []}), 400

        curve, error = _build_strategy_equity_curve(user_id, strategy_id)
        if error:
            return jsonify({'code': 0, 'msg': error, 'data': []}), 404

        return jsonify({'code': 1, 'msg': 'success', 'data': curve})
    except Exception as e:
        logger.error(f"get_equity_curve failed: {str(e)}")
        logger.error(traceback.format_exc())
        return jsonify({'code': 0, 'msg': str(e), 'data': []}), 500


@strategy_blp.route('/strategies/performance', methods=['GET'])
@login_required
def get_strategy_performance():
    """Get strategy performance metrics."""
    try:
        user_id = g.user_id
        strategy_id = request.args.get('id', type=int)
        if not strategy_id:
            return jsonify({'code': 0, 'msg': 'Strategy ID required'})

        equity_data, error = _build_strategy_equity_curve(user_id, strategy_id)
        if error:
            return jsonify({'code': 0, 'msg': error, 'data': None}), 404

        st = get_strategy_service().get_strategy(strategy_id, user_id=user_id) or {}
        initial = float(st.get('initial_capital') or (st.get('trading_config') or {}).get('initial_capital') or 0)
        summary = _strategy_performance_summary(initial, equity_data)
        return jsonify({
            'code': 1,
            'msg': 'success',
            'data': {
                'equity_curve': equity_data,
                'latest_equity': summary['latest_equity'],
                'initial_equity': summary['initial_equity'],
                'total_return': summary['total_return'],
                'total_return_pct': summary['total_return_pct'],
                'max_drawdown': summary['max_drawdown'],
                'max_drawdown_pct': summary['max_drawdown_pct'],
                'points': len(equity_data),
            }
        })
    except Exception as e:
        logger.error(f"get_strategy_performance failed: {str(e)}")
        return jsonify({'code': 0, 'msg': str(e)}), 500
