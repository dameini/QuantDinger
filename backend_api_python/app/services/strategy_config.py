from typing import Any, Dict, List, Optional

from app.services.symbol_name import normalize_crypto_symbol


def normalize_cross_sectional_symbol_list(symbol_list: List[Any], market_category: str) -> List[str]:
    """Normalize universe entries to ``Market:SYMBOL`` values."""
    out: List[str] = []
    default_market = (market_category or "Crypto").strip() or "Crypto"
    for entry in symbol_list or []:
        raw = str(entry or "").strip()
        if not raw:
            continue
        if ":" in raw:
            market, symbol = raw.split(":", 1)
            market = (market or default_market).strip() or default_market
        else:
            market, symbol = default_market, raw
        symbol = symbol.strip()
        if not symbol:
            continue
        if market == "Crypto":
            symbol = normalize_crypto_symbol(symbol)
        out.append(f"{market}:{symbol}")
    return out


def apply_cross_sectional_trading_config(
    trading_config: Dict[str, Any],
    *,
    cs_strategy_type: str,
    symbol_list: List[Any],
    portfolio_size: Any,
    long_ratio: Any,
    rebalance_frequency: Any,
    market_category: str,
    market_type: str,
) -> Dict[str, Any]:
    """Validate and persist cross-sectional fields inside ``trading_config``."""
    config = dict(trading_config or {})
    cs_type = (cs_strategy_type or "single").strip().lower()
    if cs_type != "cross_sectional":
        config["cs_strategy_type"] = "single"
        return config

    normalized = normalize_cross_sectional_symbol_list(symbol_list, market_category)
    if len(normalized) < 2:
        raise ValueError("cross_sectional requires at least 2 symbols in symbol_list")

    try:
        portfolio_size_int = int(portfolio_size or 10)
    except (TypeError, ValueError):
        portfolio_size_int = 10
    if portfolio_size_int < 1:
        raise ValueError("portfolio_size must be >= 1")
    if portfolio_size_int > len(normalized):
        raise ValueError("portfolio_size cannot exceed the number of symbols in symbol_list")

    try:
        long_ratio_float = float(long_ratio if long_ratio is not None else 0.5)
    except (TypeError, ValueError):
        long_ratio_float = 0.5
    long_ratio_float = max(0.0, min(1.0, long_ratio_float))
    normalized_market_type = (market_type or "swap").strip().lower()
    if normalized_market_type == "spot" and long_ratio_float < 1.0:
        long_ratio_float = 1.0

    frequency = (rebalance_frequency or "daily").strip().lower()
    if frequency not in ("daily", "weekly", "monthly"):
        frequency = "daily"

    config["cs_strategy_type"] = "cross_sectional"
    config["symbol_list"] = normalized
    config["portfolio_size"] = portfolio_size_int
    config["long_ratio"] = long_ratio_float
    config["rebalance_frequency"] = frequency
    config["strategy_type"] = "cross_sectional"
    config["symbol"] = normalized[0].split(":", 1)[-1]
    return config


def apply_default_strict_mode(trading_config: Dict[str, Any]) -> Dict[str, Any]:
    """Default new strategies to strict, backtest-aligned live execution."""
    config = dict(trading_config or {})
    if "strict_mode" not in config and "strictMode" not in config:
        config["strict_mode"] = True
    return config


def strip_legacy_risk_pct_basis(trading_config: Dict[str, Any]) -> Dict[str, Any]:
    """Drop the legacy risk-basis toggle from incoming payloads."""
    config = dict(trading_config or {})
    config.pop("risk_pct_basis", None)
    config.pop("riskPctBasis", None)
    return config


def apply_risk_flat_from_indicator_code(
    trading_config: Dict[str, Any],
    indicator_config: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Persist flat risk fields declared by ``@strategy`` annotations."""
    config = indicator_config if isinstance(indicator_config, dict) else {}
    code = config.get("indicator_code") or ""
    if not str(code).strip():
        return dict(trading_config or {})

    from app.services.indicator_params import StrategyConfigParser

    flat = StrategyConfigParser.to_trading_config_risk_flat(str(code))
    if not flat:
        return dict(trading_config or {})
    trading = dict(trading_config or {})
    explicit_trade_direction = trading.get("trade_direction")
    trading.update(flat)
    if explicit_trade_direction:
        trading["trade_direction"] = explicit_trade_direction
    return trading
