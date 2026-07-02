"""Watchlist business logic."""

import re
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from typing import Optional

from app.data.market_symbols_seed import get_symbol_name as seed_get_symbol_name
from app.services.market.symbol_search import find_market_symbol
from app.services.symbol_name import normalize_crypto_symbol, persist_seed_name, resolve_symbol_name
from app.utils.db import get_db_connection
from app.utils.logger import get_logger

logger = get_logger(__name__)

VALID_MARKETS = frozenset({
    "Crypto", "USStock", "CNStock", "HKStock", "Forex", "Futures", "MOEX",
})
CN_A_SHARE_PATTERN = re.compile(r"^\d{6}$")

_name_resolve_executor = ThreadPoolExecutor(
    max_workers=4,
    thread_name_prefix="watchlist-name-resolve",
)


def normalize_symbol(symbol: str) -> str:
    return (symbol or "").strip().upper()


def validate_watchlist_pair(market: str, symbol: str) -> Optional[str]:
    """Validate a market/symbol pair before persisting it."""
    if market not in VALID_MARKETS:
        return f"Unsupported market '{market}'. Must be one of: {', '.join(sorted(VALID_MARKETS))}"
    if not symbol:
        return "Empty symbol"
    if CN_A_SHARE_PATTERN.match(symbol) and market != "CNStock":
        return f"Symbol '{symbol}' looks like a Chinese A-share code; market must be CNStock, not {market}"
    if symbol.endswith(".HK") and market != "HKStock":
        return f"Symbol '{symbol}' looks like a Hong Kong stock; market must be HKStock, not {market}"
    if market == "Crypto" and "/" not in symbol:
        return f"Crypto symbol '{symbol}' must be a BASE/QUOTE pair (e.g. BTC/USDT)."
    return None


def list_watchlist(user_id: int) -> list:
    """Return a user's watchlist and backfill missing display names."""
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            "SELECT id, market, symbol, name FROM qd_watchlist WHERE user_id = ? ORDER BY id DESC",
            (user_id,),
        )
        rows = cur.fetchall() or []
        for row in rows:
            _backfill_row_name(cur, user_id, row)
        db.commit()
        cur.close()
    return rows


def add_watchlist_item(user_id: int, market: str, raw_symbol: str, name_in: str = "") -> tuple[bool, str]:
    """Validate and persist a watchlist item."""
    market = (market or "").strip()
    symbol = normalize_symbol(raw_symbol)
    if not market or not symbol:
        return False, "Missing market or symbol"

    if market == "Crypto":
        symbol = normalize_crypto_symbol(symbol)

    validation_err = validate_watchlist_pair(market, symbol)
    if validation_err:
        logger.info("Rejecting watchlist add for user %s: %s", user_id, validation_err)
        return False, validation_err

    matched = find_market_symbol(market, symbol)
    if not matched:
        err = (
            f"Symbol '{symbol}' not found on {market}. "
            "Please verify the ticker and market, or pick from search results."
        )
        logger.info("Rejecting watchlist add for user %s: %s", user_id, err)
        return False, err

    resolved = (
        (matched.get("name") or "").strip()
        or resolve_symbol_name_bounded(market, symbol)
        or seed_get_symbol_name(market, symbol)
    )
    name = (name_in or "").strip() or resolved or symbol
    persist_seed_name(market, symbol, name)

    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            """
            INSERT INTO qd_watchlist (user_id, market, symbol, name, created_at, updated_at)
            VALUES (?, ?, ?, ?, NOW(), NOW())
            ON CONFLICT(user_id, market, symbol) DO UPDATE SET
                name = excluded.name,
                updated_at = NOW()
            """,
            (user_id, market, symbol, name),
        )
        db.commit()
        cur.close()
    return True, "success"


def remove_watchlist_item(user_id: int, market: str, raw_symbol: str) -> bool:
    """Remove a watchlist item, keeping legacy crypto rows removable."""
    market = (market or "").strip()
    raw_symbol = normalize_symbol(raw_symbol)
    canonical_symbol = normalize_crypto_symbol(raw_symbol) if market == "Crypto" else raw_symbol

    with get_db_connection() as db:
        cur = db.cursor()
        if market:
            cur.execute(
                "DELETE FROM qd_watchlist WHERE user_id = ? AND market = ? AND symbol = ?",
                (user_id, market, canonical_symbol),
            )
            deleted = cur.rowcount or 0
            if deleted == 0 and canonical_symbol != raw_symbol:
                cur.execute(
                    "DELETE FROM qd_watchlist WHERE user_id = ? AND market = ? AND symbol = ?",
                    (user_id, market, raw_symbol),
                )
        else:
            logger.info(
                "remove_watchlist called without market (user=%s, symbol=%s); using legacy symbol-only delete",
                user_id,
                raw_symbol,
            )
            cur.execute(
                "DELETE FROM qd_watchlist WHERE user_id = ? AND symbol = ?",
                (user_id, raw_symbol),
            )
        db.commit()
        cur.close()
    return True


def get_user_watchlist_pairs(user_id: int) -> list:
    """Return market/symbol rows for quote fetching."""
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            "SELECT market, symbol FROM qd_watchlist WHERE user_id = ?",
            (user_id,),
        )
        rows = cur.fetchall() or []
        cur.close()
    return [
        {"market": (r.get("market") or "").strip(), "symbol": (r.get("symbol") or "").strip()}
        for r in rows
    ]


def resolve_symbol_name_bounded(market: str, symbol: str, timeout_sec: float = 4.0) -> Optional[str]:
    """Resolve a display name with a hard wall-clock cap."""
    try:
        future = _name_resolve_executor.submit(resolve_symbol_name, market, symbol)
        return future.result(timeout=timeout_sec)
    except FuturesTimeoutError:
        logger.info(
            "Symbol name resolve timed out after %.1fs for %s:%s",
            timeout_sec,
            market,
            symbol,
        )
        return None
    except Exception as exc:
        logger.debug("Symbol name resolve raised for %s:%s: %s", market, symbol, exc)
        return None


def _backfill_row_name(cur, user_id: int, row: dict) -> None:
    try:
        market = row.get("market")
        symbol = row.get("symbol")
        current_name = (row.get("name") or "").strip()
        if not market or not symbol or (current_name and current_name != symbol):
            return
        resolved = resolve_symbol_name(market, symbol) or seed_get_symbol_name(market, symbol)
        if resolved and resolved != current_name:
            row["name"] = resolved
            cur.execute(
                "UPDATE qd_watchlist SET name = ?, updated_at = NOW() WHERE user_id = ? AND market = ? AND symbol = ?",
                (resolved, user_id, market, symbol),
            )
    except Exception:
        return

