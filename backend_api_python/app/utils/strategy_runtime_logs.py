"""Persist strategy runtime lines for the strategy management UI (`qd_strategy_logs`)."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Dict, Tuple

from app.utils.db import get_db_connection
from app.utils.logger import get_logger

logger = get_logger(__name__)

_BARK_ERROR_DEDUP: Dict[Tuple[int, str], float] = {}
_BARK_ERROR_DEDUP_TTL_SEC = 120.0


def _notify_bark_for_strategy_error(strategy_id: int, message: str) -> None:
    """Best-effort Bark push for strategy error logs, scoped by user settings."""
    try:
        sid = int(strategy_id)
        msg = str(message or "").strip()
        if not msg:
            return

        dedup_key = (sid, msg)
        now = time.monotonic()
        last_sent = _BARK_ERROR_DEDUP.get(dedup_key, 0.0)
        if now - last_sent < _BARK_ERROR_DEDUP_TTL_SEC:
            return

        with get_db_connection() as db:
            cur = db.cursor()
            cur.execute(
                """
                SELECT
                    s.strategy_name,
                    s.symbol,
                    u.notification_settings
                FROM qd_strategies_trading s
                LEFT JOIN qd_users u ON u.id = s.user_id
                WHERE s.id = ?
                """,
                (sid,),
            )
            row = cur.fetchone() or {}
            cur.close()

        settings_raw = row.get("notification_settings") or ""
        settings = {}
        if settings_raw:
            try:
                parsed = json.loads(settings_raw)
                if isinstance(parsed, dict):
                    settings = parsed
            except Exception:
                settings = {}

        bark_url = str(settings.get("webhook_url") or "").strip()
        if "api.day.app" not in bark_url.lower():
            return

        from app.services.signal_notifier import SignalNotifier

        strategy_name = str(row.get("strategy_name") or f"Strategy {sid}").strip()
        symbol = str(row.get("symbol") or "").strip()
        title = f"QD Strategy Error | {strategy_name}"
        body = f"{symbol} | {msg}".strip(" |")
        payload = {
            "event": "qd.strategy_error",
            "title": title,
            "message": body,
            "strategy": {"id": sid, "name": strategy_name},
            "instrument": {"symbol": symbol},
            "level": "error",
            "timestamp_iso": datetime.now(timezone.utc).isoformat(),
        }

        notifier = SignalNotifier()
        ok, err = notifier._notify_webhook(
            url=bark_url,
            payload=payload,
            token_override=str(settings.get("webhook_token") or "").strip() or None,
            signing_secret_override=str(settings.get("webhook_signing_secret") or "").strip() or None,
        )
        if ok:
            _BARK_ERROR_DEDUP[dedup_key] = now
        else:
            logger.debug("strategy error bark notify skipped/failed: strategy_id=%s err=%s", sid, err)
    except Exception as e:
        logger.debug("strategy error bark notify failed: %s", e)


def append_strategy_log(strategy_id: int, level: str, message: str) -> None:
    """Best-effort insert; never raises to caller."""
    try:
        sid = int(strategy_id)
        lv = (level or "info").strip().lower()[:20]
        msg = str(message or "").strip()
        if not msg:
            return
        msg = msg[:8000]
        with get_db_connection() as db:
            cur = db.cursor()
            cur.execute(
                """
                INSERT INTO qd_strategy_logs (strategy_id, level, message, timestamp)
                VALUES (?, ?, ?, ?)
                """,
                (sid, lv, msg, datetime.now(timezone.utc)),
            )
            db.commit()
            cur.close()
        if lv == "error":
            _notify_bark_for_strategy_error(sid, msg)
    except Exception as e:
        logger.debug("append_strategy_log skip: %s", e)
