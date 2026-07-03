"""Strategies CRUD (read = R, create/update = W).

Reuses StrategyService so behavior matches the human UI exactly. We only
expose a curated subset of fields to keep the agent contract stable.
"""
from __future__ import annotations

from typing import Any

from app.services.backtest_limits import validate_backtest_range
from app.services.strategy_snapshot import StrategySnapshotResolver
from app.services.strategy import StrategyService
from app.utils.agent_auth import (
    SCOPE_B, SCOPE_R, SCOPE_W, agent_required, current_user_id,
)
from app.utils.logger import get_logger
from flask import request

from . import agent_v1_bp
from ._helpers import clip_int, envelope, error, get_json_or_400
from ._security import redact_strategy_row

logger = get_logger(__name__)
_strategy_service = StrategyService()


_PUBLIC_FIELDS = (
    "id", "strategy_name", "strategy_type", "market_category",
    "symbol", "timeframe", "status", "initial_capital", "leverage",
    "market_type", "strategy_mode", "execution_mode",
    "created_at", "updated_at",
)


def _project(row: dict | None) -> dict | None:
    if not row:
        return None
    return {k: row.get(k) for k in _PUBLIC_FIELDS if k in row}


@agent_v1_bp.route("/strategies", methods=["GET"])
@agent_required(SCOPE_R)
def list_strategies():
    """List the calling tenant's strategies (compact projection)."""
    try:
        rows = _strategy_service.list_strategies(user_id=current_user_id()) or []
    except Exception as exc:
        logger.error(f"agent_v1/strategies list failed: {exc}", exc_info=True)
        return error(500, "list_strategies failed", details=str(exc), http=500)

    limit = clip_int(request.args.get("limit"), default=50, lo=1, hi=200)
    return envelope([_project(r) for r in rows[:limit]])


@agent_v1_bp.route("/strategies/<int:strategy_id>", methods=["GET"])
@agent_required(SCOPE_R)
def get_strategy(strategy_id: int):
    """Tenant-scoped strategy lookup (includes indicator_config snapshot)."""
    try:
        row = _strategy_service.get_strategy(strategy_id, user_id=current_user_id())
    except Exception as exc:
        logger.error(f"agent_v1/strategies get failed: {exc}", exc_info=True)
        return error(500, "get_strategy failed", details=str(exc), http=500)
    if not row:
        return error(404, "Strategy not found", http=404)
    return envelope(redact_strategy_row(row))


@agent_v1_bp.route("/strategies", methods=["POST"])
@agent_required(SCOPE_W)
def create_strategy():
    """Create a strategy on behalf of the calling tenant.

    Request body mirrors `StrategyService.create_strategy` payload, minus
    `user_id` (always overridden to the token's tenant for safety).
    """
    body, err = get_json_or_400()
    if err:
        return err

    name = (body.get("strategy_name") or "").strip()
    if not name:
        return error(400, "strategy_name is required")

    payload: dict[str, Any] = dict(body)
    payload["user_id"] = current_user_id()
    payload.setdefault("status", "stopped")  # never auto-start from agent path

    if (payload.get("strategy_type") or "IndicatorStrategy") == "IndicatorStrategy":
        from app.services.indicator_workspace import link_indicator_config
        ic = payload.get("indicator_config") or {}
        if isinstance(ic, dict) and (ic.get("indicator_code") or ic.get("code")):
            payload["indicator_config"] = link_indicator_config(
                current_user_id(),
                ic,
                auto_save=True,
            )

    try:
        new_id = _strategy_service.create_strategy(payload)
    except ValueError as ve:
        return error(400, str(ve))
    except Exception as exc:
        logger.error(f"agent_v1/strategies create failed: {exc}", exc_info=True)
        return error(500, "create_strategy failed", details=str(exc), http=500)

    row = _strategy_service.get_strategy(int(new_id), user_id=current_user_id())
    return envelope({"strategy_id": int(new_id), "strategy": _project(row)}, message="created")


@agent_v1_bp.route("/strategies/<int:strategy_id>", methods=["PATCH"])
@agent_required(SCOPE_W)
def update_strategy(strategy_id: int):
    """Tenant-scoped patch.  Status changes that flip a strategy to `running`
    are rejected unless the token also has T scope; agents must explicitly
    request live execution scope to start strategies.
    """
    body, err = get_json_or_400()
    if err:
        return err

    new_status = (body.get("status") or "").strip().lower()
    if new_status and new_status not in {"running", "stopped"}:
        return error(400, "status must be running or stopped")

    if new_status in {"running", "stopped"}:
        from app.utils.agent_auth import current_token, parse_scopes
        if "T" not in parse_scopes(current_token().get("scopes")):
            return error(
                403,
                "Changing strategy runtime status requires T (trading) scope on this token",
                http=403,
            )

    config_body = {k: v for k, v in body.items() if k != "status"}
    try:
        if config_body:
            ok = _strategy_service.update_strategy(strategy_id, config_body, user_id=current_user_id())
        else:
            ok = bool(_strategy_service.get_strategy(strategy_id, user_id=current_user_id()))

        if ok and new_status:
            ok = _strategy_service.update_strategy_status(
                strategy_id,
                new_status,
                user_id=current_user_id(),
            )
    except Exception as exc:
        logger.error(f"agent_v1/strategies update failed: {exc}", exc_info=True)
        return error(500, "update_strategy failed", details=str(exc), http=500)

    if not ok:
        return error(404, "Strategy not found or no fields updated", http=404)

    row = _strategy_service.get_strategy(strategy_id, user_id=current_user_id())
    return envelope(_project(row), message="updated")


@agent_v1_bp.route("/strategies/<int:strategy_id>/backtest", methods=["POST"])
@agent_required(SCOPE_B)
def backtest_strategy(strategy_id: int):
    """Backtest an existing tenant strategy snapshot.

    This covers both IndicatorStrategy and ScriptStrategy records and mirrors
    the human `/api/strategies/backtest` path without requiring a browser JWT.
    """
    from datetime import datetime
    from app.services.backtest import BacktestService

    body, err = get_json_or_400()
    if err:
        return err

    start_text = str(body.get("start_date") or body.get("startDate") or "").strip()
    end_text = str(body.get("end_date") or body.get("endDate") or "").strip()
    if not start_text or not end_text:
        return error(400, "start_date and end_date are required (YYYY-MM-DD)")

    try:
        start_date = datetime.strptime(start_text, "%Y-%m-%d")
        end_date = datetime.strptime(end_text, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
    except ValueError:
        return error(400, "Invalid date. Use YYYY-MM-DD.")

    user_id = current_user_id()
    strategy = _strategy_service.get_strategy(strategy_id, user_id=user_id)
    if not strategy:
        return error(404, "Strategy not found", http=404)

    override_config = body.get("override_config") or body.get("overrideConfig") or {}
    if not isinstance(override_config, dict):
        return error(400, "override_config must be an object")

    try:
        snapshot = StrategySnapshotResolver(user_id=user_id).resolve(strategy, override_config)
        snapshot["user_id"] = user_id
        svc = BacktestService()
        warmup_bars = svc._estimate_warmup_bars(
            snapshot.get("code") or "",
            (snapshot.get("strategy_config") or {}).get("indicator_params")
            if isinstance(snapshot.get("strategy_config"), dict)
            else None,
        )
        range_error = validate_backtest_range(
            market=snapshot.get("market") or "",
            symbol=snapshot.get("symbol") or "",
            timeframe=snapshot.get("timeframe") or "1D",
            start_date=start_date,
            end_date=end_date,
            warmup_bars=warmup_bars,
        )
        if range_error:
            return error(400, range_error.get("msg") or "Invalid backtest range", details=range_error)
        result = svc.run_strategy_snapshot(snapshot, start_date=start_date, end_date=end_date)
    except ValueError as exc:
        return error(400, str(exc))
    except Exception as exc:
        logger.error(f"agent_v1/strategies backtest failed: {exc}", exc_info=True)
        return error(500, "strategy backtest failed", details=str(exc), http=500)

    return envelope({"strategy_id": strategy_id, "result": result}, message="backtested")
