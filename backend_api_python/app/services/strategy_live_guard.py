from typing import Any, Dict, Optional, Tuple

from app.routes.strategy_services import get_strategy_service
from app.utils.db import get_db_connection
from app.utils.logger import get_logger

logger = get_logger(__name__)


def strategy_live_lock_key(strategy: Dict[str, Any], user_id: int) -> Optional[Tuple[Any, ...]]:
    """Return the account/symbol key that cannot run twice for live strategies."""
    execution_mode = str(strategy.get("execution_mode") or "signal").strip().lower()
    if execution_mode != "live":
        return None

    trading_config = strategy.get("trading_config") if isinstance(strategy.get("trading_config"), dict) else {}
    exchange_config = strategy.get("exchange_config") if isinstance(strategy.get("exchange_config"), dict) else {}

    try:
        from app.services.exchange_execution import resolve_exchange_config
        from app.services.live_trading.leg_context import credential_id_from_exchange_config
        from app.services.live_trading.records import normalize_strategy_symbol

        resolved_exchange = resolve_exchange_config(exchange_config, user_id=int(user_id or strategy.get("user_id") or 1))
        exchange_id = str(
            resolved_exchange.get("exchange_id")
            or exchange_config.get("exchange_id")
            or ""
        ).strip().lower()
        if not exchange_id:
            return None

        credential_id = int(
            credential_id_from_exchange_config(resolved_exchange)
            or credential_id_from_exchange_config(exchange_config)
            or 0
        )
        credential_key: Any = credential_id if credential_id > 0 else f"inline:{exchange_id}"

        market_type = str(
            trading_config.get("market_type")
            or strategy.get("market_type")
            or resolved_exchange.get("market_type")
            or "swap"
        ).strip().lower()
        if market_type in ("futures", "future", "perp", "perpetual"):
            market_type = "swap"

        symbol = strategy.get("symbol") or trading_config.get("symbol") or ""
        symbol = normalize_strategy_symbol(str(symbol or "").strip()).upper()
        if not symbol:
            return None

        return (int(user_id or strategy.get("user_id") or 0), credential_key, exchange_id, market_type, symbol)
    except Exception as exc:
        logger.warning("strategy live lock key failed for strategy %s: %s", strategy.get("id"), exc)
        return None


def find_live_strategy_conflict(strategy: Dict[str, Any], user_id: int) -> Optional[Dict[str, Any]]:
    """Find another running live strategy using the same account, market and symbol."""
    key = strategy_live_lock_key(strategy, user_id)
    if not key:
        return None

    strategy_id = int(strategy.get("id") or 0)
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            """
            SELECT id
            FROM qd_strategies_trading
            WHERE user_id = ? AND status = 'running' AND execution_mode = 'live' AND id <> ?
            """,
            (int(user_id), strategy_id),
        )
        rows = cur.fetchall() or []
        cur.close()

    service = get_strategy_service()
    for row in rows:
        other_id = int(row.get("id") or 0)
        other = service.get_strategy(other_id, user_id=user_id)
        if not other:
            continue
        if strategy_live_lock_key(other, user_id) == key:
            return {
                "strategy_id": other_id,
                "strategy_name": other.get("strategy_name") or other.get("name") or str(other_id),
                "symbol": key[-1],
                "market_type": key[-2],
                "exchange_id": key[-3],
            }
    return None


def live_conflict_message(conflict: Dict[str, Any]) -> str:
    return (
        "Live strategy conflict: another running strategy already uses the same "
        f"API key/exchange/market/symbol ({conflict.get('exchange_id')} "
        f"{conflict.get('market_type')} {conflict.get('symbol')}). "
        f"Please stop strategy {conflict.get('strategy_id')} "
        f"({conflict.get('strategy_name')}) first."
    )
