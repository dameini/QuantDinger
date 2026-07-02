"""Symbol search helpers for the market API."""

import re
import time
from typing import Iterable

from app.data.market_symbols_seed import (
    get_hot_symbols as seed_get_hot_symbols,
    search_symbols as seed_search_symbols,
)
from app.services.symbol_name import persist_seed_name
from app.utils.cache import CacheManager
from app.utils.logger import get_logger

logger = get_logger(__name__)

SYMBOL_SEARCH_CACHE_TTL_SEC = 21600
_market_cache = CacheManager()
_crypto_markets_cache: dict = {"data": None, "ts": 0}


def dedupe_symbol_results(items: Iterable[dict], limit: int) -> list:
    """Normalize, dedupe, and limit symbol search results."""
    out = []
    seen = set()
    for item in items or []:
        market = (item.get("market") or "").strip()
        symbol = (item.get("symbol") or "").strip().upper()
        name = (item.get("name") or "").strip()
        if not market or not symbol:
            continue
        key = (market, symbol)
        if key in seen:
            continue
        seen.add(key)
        out.append({"market": market, "symbol": symbol, "name": name})
        if len(out) >= limit:
            break
    return out


def search_market_symbols(market: str, keyword: str, limit: int = 20) -> list:
    """Search local seed first, then use market-specific external fallbacks."""
    market = (market or "").strip()
    keyword = (keyword or "").strip().upper()
    limit = max(1, int(limit or 20))
    if not market or not keyword:
        return []

    out = dedupe_symbol_results(
        seed_search_symbols(market=market, keyword=keyword, limit=limit),
        limit,
    )
    if out:
        return out

    existing = {r["symbol"] for r in out}
    if market == "Crypto":
        out.extend(_search_crypto_exchange(keyword, limit - len(out), existing))
    elif market in {"USStock", "CNStock", "HKStock"}:
        out.extend(_search_external_symbols(market, keyword, limit - len(out), existing))

    return dedupe_symbol_results(out, limit)


def find_market_symbol(market: str, symbol: str) -> dict | None:
    """Return an exact symbol match from local seed or supported external sources."""
    market = (market or "").strip()
    symbol = (symbol or "").strip().upper()
    if not market or not symbol:
        return None

    local = dedupe_symbol_results(
        seed_search_symbols(market=market, keyword=symbol, limit=10),
        10,
    )
    exact = _first_exact_match(local, market, symbol)
    if exact:
        return exact

    if market == "Crypto":
        rows = _search_crypto_exchange(symbol, 10, set())
    elif market in {"USStock", "CNStock", "HKStock"}:
        rows = _search_external_symbols(market, symbol, 10, set())
    else:
        rows = []

    return _first_exact_match(rows, market, symbol)


def get_hot_symbols(market: str, limit: int = 10) -> list:
    """Return curated hot symbols for a market."""
    return seed_get_hot_symbols(market=(market or "").strip(), limit=int(limit or 10))


def _first_exact_match(items: Iterable[dict], market: str, symbol: str) -> dict | None:
    want_market = (market or "").strip()
    want_symbol = (symbol or "").strip().upper()
    for item in items or []:
        item_market = (item.get("market") or "").strip()
        item_symbol = (item.get("symbol") or "").strip().upper()
        if item_market == want_market and item_symbol == want_symbol:
            return {
                "market": item_market,
                "symbol": item_symbol,
                "name": (item.get("name") or "").strip(),
            }
    return None


def _search_crypto_exchange(keyword: str, limit: int, existing: set) -> list:
    """Search exchange markets for active USDT crypto pairs."""
    if limit <= 0:
        return []
    try:
        import ccxt  # type: ignore
        from app.config.data_sources import CCXTConfig

        now = time.time()
        if _crypto_markets_cache["data"] and now - _crypto_markets_cache["ts"] < 14400:
            markets = _crypto_markets_cache["data"]
        else:
            exchange_cls = getattr(ccxt, CCXTConfig.DEFAULT_EXCHANGE, None) or ccxt.gate
            ex = exchange_cls()
            ex.load_markets()
            markets = []
            for sym, info in ex.markets.items():
                if not info.get("active") or info.get("quote", "") != "USDT":
                    continue
                markets.append({
                    "symbol": sym,
                    "base": info.get("base", ""),
                    "name": info.get("base", sym),
                })
            _crypto_markets_cache["data"] = markets
            _crypto_markets_cache["ts"] = now
            logger.info("Cached %d USDT crypto pairs from %s", len(markets), CCXTConfig.DEFAULT_EXCHANGE)

        kw = keyword.upper().replace("/USDT", "").replace("/", "")
        results = []
        for item in markets:
            symbol = item["symbol"]
            if symbol in existing:
                continue
            if kw in item["base"].upper() or kw in symbol.upper():
                results.append({"market": "Crypto", "symbol": symbol, "name": item["name"]})
                if len(results) >= limit:
                    break
        return results
    except Exception as exc:
        logger.debug("Crypto exchange symbol search failed: %s", exc)
        return []


def _df_records(df) -> list:
    if df is None:
        return []
    try:
        return df.to_dict("records")
    except Exception:
        return []


def _search_cn_akshare(keyword: str, limit: int) -> list:
    if limit <= 0:
        return []
    try:
        import akshare as ak  # type: ignore

        rows = _df_records(ak.stock_info_a_code_name())
        kw = keyword.strip().upper()
        out = []
        for row in rows:
            symbol = str(row.get("code") or row.get("代码") or "").strip().upper()
            name = str(row.get("name") or row.get("名称") or "").strip()
            if not symbol or not name:
                continue
            if kw in symbol or kw in name.upper():
                out.append({"market": "CNStock", "symbol": symbol, "name": name})
                persist_seed_name("CNStock", symbol, name)
                if len(out) >= limit:
                    break
        return out
    except Exception as exc:
        logger.debug("CN AkShare symbol search failed: %s", exc)
        return []


def _search_hk_akshare(keyword: str, limit: int) -> list:
    if limit <= 0:
        return []
    try:
        import akshare as ak  # type: ignore

        rows = _df_records(ak.stock_hk_spot_em())
        kw = keyword.strip().upper()
        out = []
        for row in rows:
            raw_symbol = str(row.get("代码") or row.get("code") or row.get("symbol") or "").strip().upper()
            name = str(row.get("名称") or row.get("name") or "").strip()
            if not raw_symbol or not name:
                continue
            symbol = re.sub(r"[^0-9]", "", raw_symbol).zfill(5)
            if kw in raw_symbol or kw in symbol or kw in name.upper():
                out.append({"market": "HKStock", "symbol": symbol, "name": name})
                persist_seed_name("HKStock", symbol, name)
                if len(out) >= limit:
                    break
        return out
    except Exception as exc:
        logger.debug("HK AkShare symbol search failed: %s", exc)
        return []


def _search_us_yahoo(keyword: str, limit: int) -> list:
    if limit <= 0:
        return []
    try:
        import requests

        resp = requests.get(
            "https://query2.finance.yahoo.com/v1/finance/search",
            params={"q": keyword, "quotesCount": limit, "newsCount": 0},
            timeout=6,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        if resp.status_code != 200:
            return []
        out = []
        for quote in (resp.json() or {}).get("quotes") or []:
            quote_type = str(quote.get("quoteType") or "").upper()
            if quote_type not in {"EQUITY", "ETF"}:
                continue
            symbol = str(quote.get("symbol") or "").strip().upper()
            name = str(quote.get("shortname") or quote.get("longname") or quote.get("name") or "").strip()
            exchange = str(quote.get("exchange") or "").upper()
            if not symbol or not name:
                continue
            if exchange in {"HKG", "SHH", "SHZ"} or symbol.endswith((".HK", ".SS", ".SZ")):
                continue
            out.append({"market": "USStock", "symbol": symbol, "name": name})
            persist_seed_name("USStock", symbol, name)
            if len(out) >= limit:
                break
        return out
    except Exception as exc:
        logger.debug("Yahoo symbol search failed: %s", exc)
        return []


def _search_external_symbols(market: str, keyword: str, limit: int, existing: set) -> list:
    cache_key = f"symbol_search:{market}:{keyword.strip().upper()}:{limit}"
    cached = _market_cache.get(cache_key)
    if isinstance(cached, list):
        return [r for r in cached if r.get("symbol") not in existing][:limit]

    if market == "CNStock":
        rows = _search_cn_akshare(keyword, limit)
    elif market == "HKStock":
        rows = _search_hk_akshare(keyword, limit)
    elif market == "USStock":
        rows = _search_us_yahoo(keyword, limit)
    else:
        rows = []

    rows = dedupe_symbol_results(rows, limit)
    if rows:
        _market_cache.set(cache_key, rows, SYMBOL_SEARCH_CACHE_TTL_SEC)
    return [r for r in rows if r.get("symbol") not in existing][:limit]

