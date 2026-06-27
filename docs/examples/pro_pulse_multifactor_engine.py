# ============================================================
# Professional Pulse Entry Engine (QuantDinger, Four-Way)
# ============================================================
#
# 设计目标:
# 1. 用多因素评分做“脉冲输入”，避免单一条件过于脆弱
# 2. 把入场拆成：特殊 K 振荡器 + 距离拉伸 + 反转评分
# 3. 再用 EMA / 零点 / HTF 三层过滤器约束方向质量
# 4. 使用四路显式信号，适配 QuantDinger IndicatorStrategy
#
# 说明:
# - 这是“评分驱动的入场引擎”，固定止损/止盈建议交给 engine
# - close_* 只做结构反转平仓，不在脚本里叠加窄 TP/SL
# - HTF 过滤使用“已确认高周期聚合 bar”，避免未来函数
#
# ============================================================

my_indicator_name = "专业脉冲输入引擎"
my_indicator_description = (
    "多因素评分系统：特殊K振荡器 + 距离拉伸 + 反转评分 + "
    "EMA/零点/HTF 过滤器。适合趋势回撤后的脉冲切入。"
)

# --- QuantDinger execution contract (v1) ---
# signal_form: four_way
# exit_owner: engine
# flip_mode: R2

# === 平台默认策略配置 ===
# @strategy stopLossPct 0.018
# @strategy takeProfitPct 0.05
# @strategy entryPct 0.2
# @strategy trailingEnabled true
# @strategy trailingStopPct 0.015
# @strategy trailingActivationPct 0.025
# @strategy tradeDirection both

# === 参数声明 ===
# @param ema_fast int 21 快速 EMA 周期
# @param ema_slow int 55 慢速 EMA 周期
# @param ema_basis int 34 拉伸参考 EMA 周期
# @param atr_period int 14 ATR 周期
# @param stretch_cap float 2.8 拉伸分数达到满分的 ATR 倍数
# @param score_threshold float 72 入场总分阈值
# @param exit_score_threshold float 56 失效总分阈值
# @param htf_factor int 6 高周期聚合倍数
# @param htf_fast int 8 HTF 快速 EMA 周期
# @param htf_slow int 21 HTF 慢速 EMA 周期
# @param osc_signal int 5 特殊 K 信号线周期
# @param osc_oversold float -18 特殊 K 超卖阈值
# @param osc_overbought float 18 特殊 K 超买阈值
# @param long_stretch_floor float 0.35 多头最小拉伸分数
# @param short_stretch_floor float 0.35 空头最小拉伸分数


def edge(s):
    s = s.fillna(False).astype(bool)
    return s & ~s.shift(1).fillna(False)


def clip01(s):
    return s.clip(lower=0.0, upper=1.0)


def normalize_score(s):
    return clip01(s) * 100.0


def build_confirmed_htf_ema(source_df, factor, fast_span, slow_span):
    groups = pd.Series(np.arange(len(source_df)) // factor, index=source_df.index)
    htf = pd.DataFrame(
        {
            "open": source_df["open"].groupby(groups).first(),
            "high": source_df["high"].groupby(groups).max(),
            "low": source_df["low"].groupby(groups).min(),
            "close": source_df["close"].groupby(groups).last(),
        }
    )
    htf_fast = htf["close"].ewm(span=fast_span, adjust=False).mean()
    htf_slow = htf["close"].ewm(span=slow_span, adjust=False).mean()
    confirmed_groups = (groups - 1).astype(int)
    return (
        confirmed_groups.map(htf_fast.to_dict()).astype(float),
        confirmed_groups.map(htf_slow.to_dict()).astype(float),
    )


ema_fast_period = int(params.get("ema_fast", 21))
ema_slow_period = int(params.get("ema_slow", 55))
ema_basis_period = int(params.get("ema_basis", 34))
atr_period = int(params.get("atr_period", 14))
stretch_cap = float(params.get("stretch_cap", 2.8))
score_threshold = float(params.get("score_threshold", 72.0))
exit_score_threshold = float(params.get("exit_score_threshold", 56.0))
htf_factor = max(int(params.get("htf_factor", 6)), 2)
htf_fast_period = int(params.get("htf_fast", 8))
htf_slow_period = int(params.get("htf_slow", 21))
osc_signal_period = int(params.get("osc_signal", 5))
osc_oversold = float(params.get("osc_oversold", -18.0))
osc_overbought = float(params.get("osc_overbought", 18.0))
long_stretch_floor = float(params.get("long_stretch_floor", 0.35))
short_stretch_floor = float(params.get("short_stretch_floor", 0.35))

df = df.copy()

range_raw = (df["high"] - df["low"]).replace(0, np.nan)
prev_close = df["close"].shift(1)
tr = pd.concat(
    [
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ],
    axis=1,
).max(axis=1)
atr = tr.rolling(atr_period).mean()

ema_fast = df["close"].ewm(span=ema_fast_period, adjust=False).mean()
ema_slow = df["close"].ewm(span=ema_slow_period, adjust=False).mean()
ema_basis = df["close"].ewm(span=ema_basis_period, adjust=False).mean()

body = df["close"] - df["open"]
body_ratio = (body / range_raw).clip(-1.0, 1.0).fillna(0.0)
close_location = (
    ((df["close"] - df["low"]) - (df["high"] - df["close"])) / range_raw
).clip(-1.0, 1.0).fillna(0.0)
impulse = np.tanh((df["close"].diff() / atr.replace(0, np.nan)).fillna(0.0) * 1.7)

# 特殊 K 振荡器: 把 K 线位置、实体力度和 ATR 归一化脉冲组合到一个零轴振荡器里。
special_k = 100.0 * (0.42 * close_location + 0.33 * body_ratio + 0.25 * impulse)
special_k_signal = special_k.ewm(span=osc_signal_period, adjust=False).mean()
special_k_hist = special_k - special_k_signal

dist_atr = ((df["close"] - ema_basis) / atr.replace(0, np.nan)).fillna(0.0)
stretch_long_score = normalize_score((-dist_atr / max(stretch_cap, 0.5)).clip(0.0, 1.0))
stretch_short_score = normalize_score((dist_atr / max(stretch_cap, 0.5)).clip(0.0, 1.0))

lower_wick = ((np.minimum(df["open"], df["close"]) - df["low"]) / range_raw).clip(0.0, 1.0).fillna(0.0)
upper_wick = ((df["high"] - np.maximum(df["open"], df["close"])) / range_raw).clip(0.0, 1.0).fillna(0.0)
reclaim_up = ((df["close"] - df["low"]) / range_raw).clip(0.0, 1.0).fillna(0.0)
reclaim_down = ((df["high"] - df["close"]) / range_raw).clip(0.0, 1.0).fillna(0.0)
osc_turn_up = clip01((special_k_hist - special_k_hist.shift(1)).fillna(0.0) / 12.0 + 0.5)
osc_turn_down = clip01((special_k_hist.shift(1) - special_k_hist).fillna(0.0) / 12.0 + 0.5)

reversal_long_score = normalize_score(
    0.40 * lower_wick + 0.35 * reclaim_up + 0.25 * osc_turn_up
)
reversal_short_score = normalize_score(
    0.40 * upper_wick + 0.35 * reclaim_down + 0.25 * osc_turn_down
)

osc_long_score = normalize_score(
    clip01((osc_oversold - special_k) / max(abs(osc_oversold), 1.0))
    * clip01((special_k_hist + 12.0) / 24.0)
)
osc_short_score = normalize_score(
    clip01((special_k - osc_overbought) / max(abs(osc_overbought), 1.0))
    * clip01((12.0 - special_k_hist) / 24.0)
)

long_score = 0.36 * osc_long_score + 0.34 * stretch_long_score + 0.30 * reversal_long_score
short_score = 0.36 * osc_short_score + 0.34 * stretch_short_score + 0.30 * reversal_short_score

htf_ema_fast, htf_ema_slow = build_confirmed_htf_ema(
    df, htf_factor, htf_fast_period, htf_slow_period
)

ema_filter_long = (ema_fast > ema_slow) & (ema_fast.diff() > 0)
ema_filter_short = (ema_fast < ema_slow) & (ema_fast.diff() < 0)
zero_filter_long = (special_k_hist > 0) & (special_k > special_k_signal)
zero_filter_short = (special_k_hist < 0) & (special_k < special_k_signal)
htf_filter_long = (htf_ema_fast > htf_ema_slow).fillna(False)
htf_filter_short = (htf_ema_fast < htf_ema_slow).fillna(False)

long_ready = (
    (long_score >= score_threshold)
    & (stretch_long_score >= long_stretch_floor * 100.0)
    & ema_filter_long.fillna(False)
    & zero_filter_long.fillna(False)
    & htf_filter_long
)
short_ready = (
    (short_score >= score_threshold)
    & (stretch_short_score >= short_stretch_floor * 100.0)
    & ema_filter_short.fillna(False)
    & zero_filter_short.fillna(False)
    & htf_filter_short
)

long_fail = (
    (long_score <= exit_score_threshold)
    | ((special_k_hist < 0) & (special_k < special_k_signal))
    | (~htf_filter_long)
)
short_fail = (
    (short_score <= exit_score_threshold)
    | ((special_k_hist > 0) & (special_k > special_k_signal))
    | (~htf_filter_short)
)

raw_open_long = long_ready & (~short_ready)
raw_open_short = short_ready & (~long_ready)
raw_close_long = raw_open_short | long_fail
raw_close_short = raw_open_long | short_fail

df["open_long"] = edge(raw_open_long)
df["open_short"] = edge(raw_open_short)
df["close_long"] = edge(raw_close_long)
df["close_short"] = edge(raw_close_short)

n = len(df)
open_long_marks = [
    df["low"].iloc[i] * 0.996 if bool(df["open_long"].iloc[i]) else None for i in range(n)
]
open_short_marks = [
    df["high"].iloc[i] * 1.004 if bool(df["open_short"].iloc[i]) else None for i in range(n)
]

output = {
    "name": my_indicator_name,
    "plots": [
        {"name": f"EMA{ema_fast_period}", "data": ema_fast.fillna(0).tolist(), "color": "#FF9800", "overlay": True},
        {"name": f"EMA{ema_slow_period}", "data": ema_slow.fillna(0).tolist(), "color": "#1E88E5", "overlay": True},
        {"name": f"HTF_EMA{htf_fast_period}", "data": htf_ema_fast.fillna(0).tolist(), "color": "#8E24AA", "overlay": True},
        {"name": f"HTF_EMA{htf_slow_period}", "data": htf_ema_slow.fillna(0).tolist(), "color": "#5E35B1", "overlay": True},
        {"name": "SpecialK", "data": special_k.fillna(0).tolist(), "color": "#00897B", "overlay": False},
        {"name": "SpecialKHist", "data": special_k_hist.fillna(0).tolist(), "color": "#26A69A", "overlay": False},
        {"name": "LongScore", "data": long_score.fillna(0).tolist(), "color": "#43A047", "overlay": False},
        {"name": "ShortScore", "data": short_score.fillna(0).tolist(), "color": "#E53935", "overlay": False},
    ],
    "signals": [
        {"type": "buy", "text": "L", "data": open_long_marks, "color": "#00E676"},
        {"type": "sell", "text": "S", "data": open_short_marks, "color": "#FF5252"},
    ],
}
