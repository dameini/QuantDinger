"""Async task and in-flight helpers for fast analysis routes."""

import threading
import time

from app.services.analysis_memory import get_analysis_memory
from app.services.billing_service import get_billing_service
from app.services.fast_analysis import get_fast_analysis_service
from app.utils.logger import get_logger

logger = get_logger(__name__)

_analysis_inflight_lock = threading.Lock()
_analysis_inflight: dict[str, float] = {}


def build_inflight_key(user_id: int, market: str, symbol: str, timeframe: str) -> str:
    return (
        f"{int(user_id)}|{str(market or '').strip().upper()}|"
        f"{str(symbol or '').strip().upper()}|{str(timeframe or '').strip().upper()}"
    )


def acquire_inflight(key: str, ttl_sec: int = 90) -> bool:
    now = time.time()
    with _analysis_inflight_lock:
        stale = [k for k, exp in _analysis_inflight.items() if float(exp) <= now]
        for stale_key in stale[:1024]:
            _analysis_inflight.pop(stale_key, None)
        if key in _analysis_inflight and float(_analysis_inflight.get(key) or 0) > now:
            return False
        _analysis_inflight[key] = now + int(ttl_sec)
        return True


def release_inflight(key: str) -> None:
    with _analysis_inflight_lock:
        _analysis_inflight.pop(key, None)


def try_refund_credits(user_id: int, amount: int, remark: str) -> None:
    """Best-effort refund when analysis fails after a pre-charge."""
    try:
        if int(amount or 0) <= 0:
            return
        get_billing_service().add_credits(
            user_id=int(user_id),
            amount=int(amount),
            action="refund",
            remark=remark,
        )
    except Exception as exc:
        logger.error("Async auto refund failed: %s", exc, exc_info=True)


def run_async_analysis_task(
    task_memory_id: int,
    market: str,
    symbol: str,
    language: str,
    model: str,
    timeframe: str,
    user_id: int,
    inflight_key: str,
    credits_charged: int = 0,
) -> None:
    """Execute analysis in a background worker and finalize pending history."""
    try:
        service = get_fast_analysis_service()
        memory = get_analysis_memory()
        result = service.analyze(
            market=market,
            symbol=symbol,
            language=language,
            model=model,
            timeframe=timeframe,
            user_id=user_id,
        )
        memory.finalize_pending_task(task_memory_id, result)
        if result.get("error"):
            try_refund_credits(
                user_id=int(user_id),
                amount=int(credits_charged or 0),
                remark=f"Auto refund: async fast-analysis failed ({market}:{symbol}:{timeframe})",
            )

        auto_memory_id = result.get("memory_id")
        if auto_memory_id and int(auto_memory_id) != int(task_memory_id):
            try:
                memory.delete_history(int(auto_memory_id), user_id=user_id)
            except Exception:
                pass
    except Exception as exc:
        logger.error("Async analysis task failed: %s", exc, exc_info=True)
        try_refund_credits(
            user_id=int(user_id),
            amount=int(credits_charged or 0),
            remark=f"Auto refund: async fast-analysis exception ({market}:{symbol}:{timeframe})",
        )
        try:
            get_analysis_memory().fail_pending_task(task_memory_id, str(exc))
        except Exception:
            pass
    finally:
        try:
            release_inflight(inflight_key)
        except Exception:
            pass


def start_async_analysis_task(*args, **kwargs) -> threading.Thread:
    thread = threading.Thread(target=run_async_analysis_task, args=args, kwargs=kwargs, daemon=True)
    thread.start()
    return thread

