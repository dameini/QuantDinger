import os
from typing import Any, Dict, Optional


def coerce_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on", "enabled"):
        return True
    if text in ("0", "false", "no", "off", "disabled", ""):
        return False
    return bool(default)


def kline_boundary_poll_offset_sec() -> float:
    try:
        return max(0.0, float(os.getenv("KLINE_BOUNDARY_POLL_OFFSET_SEC", "2")))
    except (TypeError, ValueError):
        return 2.0


def next_kline_boundary_poll_ts(
    now_ts: float,
    timeframe_seconds: int,
    offset_sec: Optional[float] = None,
) -> float:
    """Return the next wall-clock time to poll K-lines just after bar close."""
    timeframe = max(1, int(timeframe_seconds or 60))
    offset = kline_boundary_poll_offset_sec() if offset_sec is None else max(0.0, float(offset_sec))
    timestamp = float(now_ts)
    next_close = (int(timestamp) // timeframe + 1) * timeframe
    return next_close + offset


def normalize_trading_execution_modes(trading_config: Optional[Dict[str, Any]]) -> None:
    """Align live execution knobs with the UI strict_mode toggle."""
    if not isinstance(trading_config, dict):
        return
    tc = trading_config
    has_strict_toggle = "strict_mode" in tc or "strictMode" in tc
    strict = coerce_bool(tc.get("strict_mode", tc.get("strictMode")), default=True)
    tc["strict_mode"] = strict
    if has_strict_toggle:
        if strict:
            tc["signal_mode"] = "confirmed"
            tc["exit_signal_mode"] = "confirmed"
        else:
            tc["signal_mode"] = "aggressive"
            tc["exit_signal_mode"] = "aggressive"
            tc["entry_trigger_mode"] = "immediate"
    elif strict:
        tc.setdefault("exit_signal_mode", "confirmed")
        tc.setdefault("signal_mode", "confirmed")
    else:
        tc.setdefault("signal_mode", "aggressive")
        tc.setdefault("exit_signal_mode", "aggressive")
        tc.setdefault("entry_trigger_mode", "immediate")
