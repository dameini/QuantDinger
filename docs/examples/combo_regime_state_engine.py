# ============================================================
# 通用组合状态机引擎
# 趋势突破模块 + 震荡回归模块，根据市场状态切换
# ============================================================

my_indicator_name = "通用组合状态机引擎"
my_indicator_description = "趋势突破模块 + 震荡回归模块，根据市场状态切换"

# signal_form: four_way
# exit_owner: engine
# flip_mode: R2

# @strategy stopLossPct 0.5
# @strategy takeProfitPct 0.0
# @strategy entryPct 0.15
# @strategy trailingEnabled false
# @strategy trailingStopPct 0.0
# @strategy trailingActivationPct 0.0
# @strategy tradeDirection both

# @param ema_fast int 21 快速EMA
# @param ema_slow int 55 慢速EMA
# @param atr_period int 14 ATR周期
# @param bb_period int 20 布林周期
# @param bb_mult float 1.8 布林倍数
# @param vol_period int 20 成交量均线
# @param vol_mult float 0.9 趋势量能倍数
# @param breakout_lookback int 24 趋势突破回看
# @param breakout_buffer float 0.001 趋势突破缓冲
# @param regime_threshold float 0.014 状态阈值
# @param rsi_period int 14 RSI周期
# @param rsi_trend_long float 55 趋势多阈值
# @param rsi_trend_short float 45 趋势空阈值
# @param rsi_revert_long float 34 回归多阈值
# @param rsi_revert_short float 66 回归空阈值
# @param rsi_exit_mid float 50 RSI中轴离场
# @param trend_trail_atr float 1.8 趋势ATR退出倍数
# @param revert_target_frac float 0.35 回归离场比例
# @param cooldown_bars int 2 冷却K数
# @param max_trend_bars int 40 趋势最大持仓
# @param max_revert_bars int 16 回归最大持仓
# @param htf_factor int 4 HTF聚合倍数
# @param htf_fast int 21 HTF快EMA
# @param htf_slow int 55 HTF慢EMA
# @param er_period int 16 效率比周期
# @param hysteresis float 0.003 状态切换滞后


def rsi(series, period):
    delta = series.diff()
    up = delta.clip(lower=0.0)
    down = -delta.clip(upper=0.0)
    avg_up = up.ewm(alpha=1.0 / max(period, 1), adjust=False).mean()
    avg_down = down.ewm(alpha=1.0 / max(period, 1), adjust=False).mean()
    rs = avg_up / avg_down.replace(0, np.nan)
    return (100.0 - (100.0 / (1.0 + rs))).fillna(50.0)


def efficiency_ratio(series, period):
    change = (series - series.shift(period)).abs()
    volatility = series.diff().abs().rolling(period).sum()
    return (change / volatility.replace(0, np.nan)).fillna(0.0).clip(0.0, 1.0)


def build_confirmed_htf_ema(source_df, factor, fast_span, slow_span):
    groups = pd.Series(np.arange(len(source_df)) // factor, index=source_df.index)
    htf = pd.DataFrame(
        {
            "open": source_df["open"].groupby(groups).first(),
            "high": source_df["high"].groupby(groups).max(),
            "low": source_df["low"].groupby(groups).min(),
            "close": source_df["close"].groupby(groups).last(),
            "volume": source_df["volume"].groupby(groups).sum(),
        }
    )
    htf_fast = htf["close"].ewm(span=max(fast_span, 1), adjust=False).mean()
    htf_slow = htf["close"].ewm(span=max(slow_span, 1), adjust=False).mean()
    confirmed_groups = (groups - 1).astype(int)
    return (
        confirmed_groups.map(htf_fast.to_dict()).astype(float),
        confirmed_groups.map(htf_slow.to_dict()).astype(float),
    )


ema_fast_period = int(params.get("ema_fast", 21))
ema_slow_period = int(params.get("ema_slow", 55))
atr_period = int(params.get("atr_period", 14))
bb_period = int(params.get("bb_period", 20))
bb_mult = float(params.get("bb_mult", 1.8))
vol_period = int(params.get("vol_period", 20))
vol_mult = float(params.get("vol_mult", 0.9))
breakout_lookback = int(params.get("breakout_lookback", 24))
breakout_buffer = float(params.get("breakout_buffer", 0.001))
regime_threshold = float(params.get("regime_threshold", 0.014))
rsi_period = int(params.get("rsi_period", 14))
rsi_trend_long = float(params.get("rsi_trend_long", 55))
rsi_trend_short = float(params.get("rsi_trend_short", 45))
rsi_revert_long = float(params.get("rsi_revert_long", 34))
rsi_revert_short = float(params.get("rsi_revert_short", 66))
rsi_exit_mid = float(params.get("rsi_exit_mid", 50))
trend_trail_atr = float(params.get("trend_trail_atr", 1.8))
revert_target_frac = float(params.get("revert_target_frac", 0.35))
cooldown_bars = int(params.get("cooldown_bars", 2))
max_trend_bars = int(params.get("max_trend_bars", 40))
max_revert_bars = int(params.get("max_revert_bars", 16))
htf_factor = max(int(params.get("htf_factor", 4)), 2)
htf_fast_period = int(params.get("htf_fast", 21))
htf_slow_period = int(params.get("htf_slow", 55))
er_period = int(params.get("er_period", 16))
hysteresis = float(params.get("hysteresis", 0.003))

df = df.copy()
prev_close = df["close"].shift(1)
tr = pd.concat(
    [
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ],
    axis=1,
).max(axis=1)
atr = tr.rolling(atr_period).mean().bfill()
ema_fast = df["close"].ewm(span=ema_fast_period, adjust=False).mean()
ema_slow = df["close"].ewm(span=ema_slow_period, adjust=False).mean()
basis = df["close"].rolling(bb_period).mean().bfill()
stdev = df["close"].rolling(bb_period).std().fillna(0.0)
upper = basis + stdev * bb_mult
lower = basis - stdev * bb_mult
bb_width = ((upper - lower) / basis.replace(0, np.nan)).fillna(0.0)
er = efficiency_ratio(df["close"], er_period)
rsi_val = rsi(df["close"], rsi_period)
vol_ma = df["volume"].rolling(vol_period).mean().bfill()
htf_fast, htf_slow = build_confirmed_htf_ema(
    df, htf_factor, htf_fast_period, htf_slow_period
)

trend_bias_up = (ema_fast > ema_slow) & (htf_fast > htf_slow).fillna(False)
trend_bias_down = (ema_fast < ema_slow) & (htf_fast < htf_slow).fillna(False)
regime_score = 0.55 * bb_width + 0.45 * er
trend_on = regime_score > regime_threshold
trend_off = regime_score < max(0.0, regime_threshold - hysteresis)
high_ref = df["high"].rolling(max(breakout_lookback, 2)).max().shift(1)
low_ref = df["low"].rolling(max(breakout_lookback, 2)).min().shift(1)
volume_ok = df["volume"] > (vol_ma * vol_mult)

trend_long_entry = (
    trend_bias_up
    & trend_on
    & volume_ok
    & (rsi_val > rsi_trend_long)
    & (df["close"] > high_ref * (1.0 + breakout_buffer))
)
trend_short_entry = (
    trend_bias_down
    & trend_on
    & volume_ok
    & (rsi_val < rsi_trend_short)
    & (df["close"] < low_ref * (1.0 - breakout_buffer))
)
revert_long_entry = (
    trend_off
    & (df["low"] < lower)
    & (rsi_val < rsi_revert_long)
    & (df["close"] > df["open"])
)
revert_short_entry = (
    trend_off
    & (df["high"] > upper)
    & (rsi_val > rsi_revert_short)
    & (df["close"] < df["open"])
)

n = len(df)
open_long = np.zeros(n, dtype=bool)
close_long = np.zeros(n, dtype=bool)
open_short = np.zeros(n, dtype=bool)
close_short = np.zeros(n, dtype=bool)

state = "flat"
mode = None
highest = 0.0
lowest = 0.0
bars_in_pos = 0
cooldown = 999999

for i in range(n):
    if (
        not np.isfinite(ema_slow.iloc[i])
        or not np.isfinite(atr.iloc[i])
        or not np.isfinite(basis.iloc[i])
    ):
        cooldown += 1
        continue

    c = float(df["close"].iloc[i])
    h = float(df["high"].iloc[i])
    l = float(df["low"].iloc[i])
    atrv = float(atr.iloc[i])
    b = float(basis.iloc[i])
    up = float(upper.iloc[i])
    lo = float(lower.iloc[i])

    if state == "flat":
        cooldown += 1
        if cooldown >= cooldown_bars:
            want_long = bool(trend_long_entry.iloc[i] or revert_long_entry.iloc[i])
            want_short = bool(trend_short_entry.iloc[i] or revert_short_entry.iloc[i])
            if want_long and not want_short:
                open_long[i] = True
                state = "long"
                mode = "trend" if bool(trend_long_entry.iloc[i]) else "revert"
                highest = h
                bars_in_pos = 0
                cooldown = 0
            elif want_short and not want_long:
                open_short[i] = True
                state = "short"
                mode = "trend" if bool(trend_short_entry.iloc[i]) else "revert"
                lowest = l
                bars_in_pos = 0
                cooldown = 0
    elif state == "long":
        bars_in_pos += 1
        highest = max(highest, h)
        if mode == "trend":
            trail_stop = highest - atrv * trend_trail_atr
            exit_now = (
                (c < trail_stop)
                or bool(trend_bias_down.iloc[i])
                or (rsi_val.iloc[i] < rsi_exit_mid - 4)
                or (bars_in_pos >= max_trend_bars)
            )
        else:
            revert_target = b + (up - b) * revert_target_frac
            exit_now = (
                (c >= revert_target)
                or (rsi_val.iloc[i] > rsi_exit_mid)
                or bool(trend_short_entry.iloc[i])
                or (bars_in_pos >= max_revert_bars)
            )
        flip_now = bool(trend_short_entry.iloc[i] or revert_short_entry.iloc[i])
        if exit_now:
            close_long[i] = True
            state = "flat"
            mode = None
            highest = 0.0
            bars_in_pos = 0
            cooldown = 0
        elif flip_now:
            close_long[i] = True
            open_short[i] = True
            state = "short"
            mode = "trend" if bool(trend_short_entry.iloc[i]) else "revert"
            lowest = l
            highest = 0.0
            bars_in_pos = 0
            cooldown = 0
    elif state == "short":
        bars_in_pos += 1
        lowest = min(lowest, l)
        if mode == "trend":
            trail_stop = lowest + atrv * trend_trail_atr
            exit_now = (
                (c > trail_stop)
                or bool(trend_bias_up.iloc[i])
                or (rsi_val.iloc[i] > rsi_exit_mid + 4)
                or (bars_in_pos >= max_trend_bars)
            )
        else:
            revert_target = b - (b - lo) * revert_target_frac
            exit_now = (
                (c <= revert_target)
                or (rsi_val.iloc[i] < rsi_exit_mid)
                or bool(trend_long_entry.iloc[i])
                or (bars_in_pos >= max_revert_bars)
            )
        flip_now = bool(trend_long_entry.iloc[i] or revert_long_entry.iloc[i])
        if exit_now:
            close_short[i] = True
            state = "flat"
            mode = None
            lowest = 0.0
            bars_in_pos = 0
            cooldown = 0
        elif flip_now:
            close_short[i] = True
            open_long[i] = True
            state = "long"
            mode = "trend" if bool(trend_long_entry.iloc[i]) else "revert"
            highest = h
            lowest = 0.0
            bars_in_pos = 0
            cooldown = 0

df["open_long"] = pd.Series(open_long, index=df.index)
df["close_long"] = pd.Series(close_long, index=df.index)
df["open_short"] = pd.Series(open_short, index=df.index)
df["close_short"] = pd.Series(close_short, index=df.index)

output = {
    "name": my_indicator_name,
    "plots": [],
    "signals": [
        {
            "type": "buy",
            "text": "L",
            "data": [
                df["low"].iloc[i] * 0.996 if bool(df["open_long"].iloc[i]) else None
                for i in range(n)
            ],
            "color": "#00e676",
        },
        {
            "type": "sell",
            "text": "S",
            "data": [
                df["high"].iloc[i] * 1.004 if bool(df["open_short"].iloc[i]) else None
                for i in range(n)
            ],
            "color": "#ff5252",
        },
    ],
}
