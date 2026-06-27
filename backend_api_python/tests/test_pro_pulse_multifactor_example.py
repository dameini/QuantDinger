"""Execution contract test for the professional pulse entry example."""

from pathlib import Path

from app.services.indicator_code_quality import analyze_indicator_code_quality
from app.utils.safe_exec import build_safe_builtins, safe_exec_with_validation

import numpy as np
import pandas as pd


def _load_example_code() -> str:
    root = Path(__file__).resolve().parents[2]
    return (root / "docs" / "examples" / "pro_pulse_multifactor_engine.py").read_text()


def _mock_df(length: int = 240) -> pd.DataFrame:
    base = 100 * np.exp(np.cumsum(np.random.normal(0, 0.01, length)))
    return pd.DataFrame(
        {
            "open": base * (1 + np.random.normal(0, 0.001, length)),
            "high": base * (1 + np.abs(np.random.normal(0.002, 0.001, length))),
            "low": base * (1 - np.abs(np.random.normal(0.002, 0.001, length))),
            "close": base,
            "volume": np.random.uniform(500, 5000, length),
        }
    )


def test_pro_pulse_example_passes_quality_hints():
    code = _load_example_code()
    hints = analyze_indicator_code_quality(code)
    codes = {hint["code"] for hint in hints}
    assert "MISSING_OUTPUT" not in codes
    assert "MISSING_BUY_SELL_COLUMNS" not in codes
    assert "DECLARED_PARAMS_NOT_READ_VIA_PARAMS_GET" not in codes


def test_pro_pulse_example_executes_in_sandbox():
    code = _load_example_code()
    df = _mock_df()
    env = {
        "df": df.copy(),
        "pd": pd,
        "np": np,
        "params": {},
        "output": None,
        "__builtins__": build_safe_builtins(),
    }
    result = safe_exec_with_validation(code=code, exec_globals=env, exec_locals=env, timeout=15)
    assert result.get("success"), result.get("error")
    out = env.get("output")
    assert isinstance(out, dict)
    assert out.get("plots")
    executed = env["df"]
    for col in ("open_long", "close_long", "open_short", "close_short"):
        assert col in executed.columns
        assert len(executed[col]) == len(df)
