# QuantDinger 策略运行时与算法交易升级规划

**状态**：规划建议  
**适用范围**：ScriptStrategy、实盘策略运行时、订单执行、算法交易执行器  
**背景**：用户开始提出更复杂的实盘策略需求，例如 5 层分仓、每层 3 个马丁子单、均价止盈、多空模式和自定义杠杆。QuantDinger 需要从“能跑策略”升级为“可靠承载复杂策略和算法执行”的量化交易基础设施。

---

## 1. 结论

当前 ScriptStrategy 基座可以支持一类复杂策略：

- 有状态的分批开仓。
- 运行时参数化。
- 多次加仓、均价止盈。
- 只做多或只做空。
- K 线/实时 tick 循环驱动。
- 回测与实盘复用同一份脚本逻辑。

但对于专业级“阶梯分仓马丁 / basket martingale / layered DCA”这类策略，当前系统还缺少一些基础设施级边界：

- 独立 basket 状态持久化。
- 多空双向 basket 的独立状态。
- 重启恢复与成交回放。
- 部分成交、撤单、拒单后的确定性状态机。
- 实盘与回测在逐笔补仓、均价、手续费、滑点上的一致性校验。
- 策略级最大风险预算、爆仓距离、保证金占用和 kill switch。

因此：

- **只做多 / 只做空版本**：当前基座可以写出来，适合先落地。
- **多空双开版本**：建议先补强 hedge-mode 状态模型，不建议直接用单一 `ctx.position` 硬写。
- **专业级可售卖模板**：需要配套 basket runtime、订单生命周期和恢复机制后再推广。

---

## 2. 用户需求是否能由脚本策略表达

用户需求：

```text
5 个大分仓
每个分仓 3 个开单
单内开仓间距可调
每个子单按马丁倍数放大
均价止盈
前一个分仓没有盈利才开下一个分仓
交易品种自定义
只做多 / 只做空 / 多空双开
基础仓位是首单金额
合约按杠杆后的名义价值计算
马丁倍数可自定义
均价止盈可自定义
```

### 2.1 当前可直接表达的部分

ScriptStrategy 已经具备这些能力：

| 需求 | 当前支持情况 | 说明 |
| --- | --- | --- |
| 自定义交易品种 | 支持 | 由策略创建参数和运行配置选择 symbol |
| 参数可调 | 支持 | 使用 `ctx.param(...)` |
| 只做多 | 支持 | `ctx.open_long` / `ctx.add_long` / `ctx.close_position` |
| 只做空 | 支持 | `ctx.open_short` / `ctx.add_short` / `ctx.close_position` |
| 分批加仓 | 支持 | 脚本维护层级和触发价 |
| 马丁倍数 | 支持 | 通过脚本计算每次下单数量 |
| 均价止盈 | 支持 | 脚本维护或读取均价后触发平仓 |
| K 线或 tick 循环 | 支持 | 运行时按 bar/tick 调用脚本 |
| 回测 | 支持 | 可用脚本回测面板验证 |

### 2.2 当前不建议硬写的部分

| 需求 | 风险点 | 建议 |
| --- | --- | --- |
| 多空双开 | 需要 long basket 和 short basket 独立状态；交易所也必须是 hedge mode | 第一版拆成两个策略实例，后续补 `ctx.long_position` / `ctx.short_position` |
| 重启后继续运行 | 内存变量可能丢失，导致重复开单或错误止盈 | 必须有 basket 状态持久化和成交回放 |
| 部分成交 | 均价和层级不能只按“已发单”计算 | 必须按实际成交回填 |
| 拒单 / 超最小下单量 | 状态机可能误以为已开仓 | 下单确认必须驱动状态迁移 |
| 合约杠杆名义价值 | 不同交易所单位、面值、张数规则不同 | 统一 order sizing 层 |
| 爆仓/保证金风险 | 马丁连续补仓容易快速提高保证金占用 | 增加策略级风险预算和强制停止 |

---

## 3. 建议的脚本策略模型

这个用户需求应该抽象成 `LayeredMartingaleBasket`，不是普通网格，也不是简单 DCA。

### 3.1 参数结构

```text
symbol
direction: long | short | both
base_order_value
leverage
layers = 5
orders_per_layer = 3
martingale_multiplier = 2.0
intra_spacing_pct = [0.5, 0.8]
inter_spacing_pct = [1.2, 1.5, 1.8, 2.2]
take_profit_pct = 2.0
max_total_margin_pct
max_notional_pct
hard_stop_pct
cooldown_bars
restart_after_take_profit = true
```

### 3.2 状态结构

```text
basket_id
side
status: idle | opening | active | closing | closed | failed
current_layer
current_order_in_layer
filled_orders
total_qty
total_notional
avg_entry_price
next_entry_trigger
take_profit_price
last_signal_ts
last_order_id
error_count
```

### 3.3 执行流程

```text
if basket is idle:
    open layer 1 order 1

if price moves adverse by intra/inter spacing:
    open next child order

if weighted average reaches take-profit:
    close basket
    reset state

if max layer/order reached:
    stop adding
    wait for TP or trigger risk exit

if hard risk limit reached:
    close basket or stop strategy
```

---

## 4. ScriptStrategy 基座需要补齐的边界

### 4.1 Basket state persistence

复杂策略不能只依赖脚本内存变量。建议新增 basket 状态表或 runtime state 标准：

```text
qd_strategy_baskets
qd_strategy_basket_orders
```

核心能力：

- 每个 basket 有唯一 ID。
- 每个子单有独立状态。
- 状态由成交事件驱动，而不是由“发单成功”驱动。
- 后端重启后可以恢复当前层级、均价和未完成订单。

#### 4.1.1 脚本线程与持久化边界

脚本策略通常跑在线程或任务循环里，但线程内变量只能作为运行期缓存，不能作为真实状态来源。建议把策略状态拆成三层：

| 层级 | 用途 | 建议存储 | 是否可信 |
| --- | --- | --- | --- |
| 线程内存 | 当前循环临时计算、减少查询 | Python object | 不可信，重启即丢 |
| Redis / cache | 分布式锁、短期心跳、运行中标记、限频 | Redis，可选 | 半可信，可重建 |
| 数据库 | basket、订单意图、成交、风险状态、恢复检查点 | PostgreSQL / SQLite / MySQL | 可信状态源 |

原则是：脚本可以读写 `ctx.state`，但 `ctx.state` 背后必须由运行时托管并定期落库；策略代码不应该直接操作数据库连接，也不应该自己决定恢复逻辑。

建议提供运行时接口：

```python
ctx.state.get("current_layer", 0)
ctx.state.set("current_layer", 2)
ctx.state.flush()

ctx.basket("long").open_child_order(...)
ctx.basket("long").checkpoint()
```

其中：

- `ctx.state` 保存轻量策略状态，例如当前层级、最近触发价格、冷却计数。
- `ctx.basket` 保存交易相关状态，例如子单、成交、均价、止盈价、未完成订单。
- `flush()` 由运行时批量落库，也可以在发单前后强制 checkpoint。
- 真实下单前必须先写入 `order_intent`，拿到幂等键，再提交交易所订单。

#### 4.1.2 数据库与 Redis 的分工

数据库是恢复源，Redis 只做加速和协调：

- 数据库负责：策略实例、basket、子订单、订单意图、交易所订单 ID、成交回报、风险状态、最后 checkpoint。
- Redis 负责：策略运行锁、线程心跳、短期去重键、任务队列、行情订阅状态。
- 如果没有 Redis，单机版本也能工作，只是不能很好地做多进程调度和快速故障切换。
- 如果 Redis 数据丢失，不应该影响真实持仓恢复；最多影响 UI 的“运行中”即时状态。

推荐表结构方向：

```text
strategy_runtime_state
strategy_baskets
strategy_basket_orders
strategy_order_intents
strategy_order_fills
strategy_recovery_events
```

#### 4.1.3 重启与容灾流程

后端重启、线程崩溃或容器迁移后，不应该直接从脚本第一行重新开始发单。恢复流程应由运行时统一执行：

1. 标记策略实例为 `recovering`，禁止脚本继续发新单。
2. 读取数据库中的最后 checkpoint、basket、未完成 order intent。
3. 拉交易所当前持仓、未成交订单、最近成交。
4. 用交易所事实修正本地状态：成交数量、均价、未完成订单、已关闭订单。
5. 对没有交易所订单 ID 的 intent 做幂等判定：确认未提交才允许重试。
6. 重建 `ctx.state` 和 `ctx.basket`，恢复到线程内存。
7. 写入 recovery event，标记为 `running`。
8. 下一轮策略循环只允许基于恢复后的状态继续运行。

关键点是：恢复优先相信交易所事实，其次相信数据库 checkpoint，最后才相信脚本内存。

#### 4.1.4 防重复下单机制

复杂马丁策略最怕重启后重复补仓。每次下单必须有稳定幂等键：

```text
strategy_id + basket_id + side + layer_index + order_index + action
```

例如：

```text
strategy-12:basket-20260629-long:L2:O3:open
```

运行时提交订单前先创建 `order_intent`：

- 如果同一幂等键已有 `submitted / accepted / partially_filled / filled`，禁止再次提交。
- 如果是 `rejected / expired`，根据策略配置决定是否重试。
- 如果是 `unknown`，必须先向交易所对账，不允许盲目重发。

这样即使线程在“写库成功、发单超时、交易所实际已接收”的中间状态崩溃，也可以通过交易所订单查询恢复，而不是重复开单。

### 4.2 Position model upgrade

当前脚本更适合单方向持仓。后续建议提供清晰接口：

```python
ctx.position
ctx.long_position
ctx.short_position
ctx.basket(side="long")
ctx.basket(side="short")
```

这样多空双开不会挤在一个 `ctx.position` 语义里。

### 4.3 Order lifecycle standard

复杂策略必须清楚区分：

- intent created
- order submitted
- exchange accepted
- partially filled
- filled
- rejected
- cancelled
- expired
- reconciled

状态迁移必须幂等，避免重启后重复下单。

### 4.4 Fill-driven average price

均价止盈必须基于实际成交：

- 不能只按脚本计划价格。
- 不能只按信号价格。
- 必须考虑部分成交、手续费、合约面值和滑点。

### 4.5 Restart recovery

策略恢复时应执行：

1. 读取本地 basket 状态。
2. 拉交易所持仓和未成交订单。
3. 对账本地订单与交易所订单。
4. 重建均价、层级和风险状态。
5. 再允许继续发新单。

这一节是恢复行为摘要，详细线程、数据库、Redis 与幂等设计见 `4.1.1` 到 `4.1.4`。

### 4.6 Risk guard

马丁类策略必须内置硬风险边界：

- 最大层数。
- 最大子单数。
- 最大名义价值。
- 最大保证金占用。
- 最大账户权益占比。
- 最大连续亏损次数。
- 最大浮亏。
- 距离爆仓价最小安全距离。
- 交易所错误过多自动停止。

### 4.7 Backtest/live consistency

复杂加仓策略需要增强回测：

- 支持逐层成交记录。
- 支持手续费和滑点模型。
- 支持最小下单量和价格步进。
- 支持合约张数换算。
- 支持“下根 K 成交”和“tick 触发”的差异报告。
- 支持实盘偏差报告：计划成交 vs 实际成交。

### 4.8 Strategy run identity

每一次策略启动都必须有独立 `strategy_run_id`。不要只用 `strategy_id` 表示一次运行，因为同一策略可能多次启动、暂停、恢复、修改参数。

建议记录：

```text
strategy_id
strategy_run_id
source_version_id
parameter_snapshot
account_id
exchange
symbol
market_type
position_mode
started_at
stopped_at
stop_reason
runtime_status
```

这样可以回答：

- 这笔订单属于哪一次运行。
- 当时使用的是哪一版脚本代码。
- 当时参数是什么。
- 是正常停止、用户停止、风控停止，还是异常崩溃后恢复。

### 4.9 Single writer and lock model

同一个策略实例同一时间只能有一个运行时写状态和发单。否则多线程、多进程或容器重启时容易重复补仓。

建议：

- 以 `strategy_id + account_id + symbol + side` 作为运行锁范围。
- 单机版可以用数据库锁。
- 多实例部署建议用 Redis lock + 数据库 fencing token。
- 每次写入订单状态时检查 `runtime_epoch` 或 `fencing_token`，旧线程失去锁后不能继续写库。
- UI 上的“启动策略”如果发现已有活动 run，应提示用户恢复、接管或强制停止。

### 4.10 Event ledger

除了保存当前状态，还应该保存事件流水。当前状态用于快速恢复，事件流水用于审计和重放。

建议事件类型：

```text
strategy_started
signal_generated
order_intent_created
order_submitted
order_accepted
order_partially_filled
order_filled
order_rejected
order_cancelled
basket_checkpointed
risk_guard_triggered
recovery_started
recovery_completed
strategy_stopped
manual_override
```

有了 event ledger，后续才能做：

- 策略事故复盘。
- 回测与实盘偏差报告。
- 用户投诉时还原执行过程。
- 版本升级后的兼容性检查。

### 4.11 Code and parameter snapshot

实盘运行不能只引用“当前脚本代码”。用户可能在策略运行中修改脚本，如果没有快照，会出现无法复现的问题。

建议：

- 每次启动实盘时固定 `source_version_id`。
- 保存代码 hash、参数快照、运行配置、交易账户、交易所模式。
- 修改代码后不影响正在运行的 run，除非用户明确点击“应用并重启”。
- 回测记录、实盘记录和市场模板都引用同一套版本快照。

### 4.12 Manual intervention and degraded mode

恢复失败、交易所状态不一致或订单状态未知时，不应该让策略继续自动补仓。

建议增加状态：

```text
running
recovering
paused
needs_review
stopping
stopped
failed
```

进入 `needs_review` 时：

- 禁止发新开仓单。
- 允许只减仓或撤单。
- UI 显示差异：本地 basket、交易所持仓、未成交订单。
- 用户可以选择：同步交易所状态、关闭 basket、继续观察、强制停止。

---

## 5. 算法交易能力缺口

目前 QuantDinger 已有基础下单与策略运行能力，但还不是完整 AlgoTrading 执行平台。

### 5.1 当前已有能力

- 市价单。
- 限价单。
- 基础取消订单。
- 策略信号触发下单。
- 止损、止盈、追踪止损等策略级风控。
- 网格 resting limit orders 的部分执行基础。
- 多交易所适配框架。

### 5.2 需要补齐的算法交易模块

建议新增独立域：

```text
app/services/algo_trading/
  order_intent.py
  scheduler.py
  child_order.py
  execution_algorithms/
    twap.py
    iceberg.py
    best_limit.py
    sniper.py
    stop.py
  order_state.py
  reconciliation.py
  risk.py
```

### 5.3 第一阶段建议实现

| 算法 | 优先级 | 原因 |
| --- | --- | --- |
| TWAP | P0 | 最容易解释，最适合大额分批成交 |
| BestLimit | P0 | 基于盘口最优价挂单，适合低滑点执行 |
| Stop / StopLimit | P0 | 用户理解成本低，交易刚需 |
| Iceberg | P1 | 大单隐藏，依赖交易所支持或本地拆单 |
| Sniper | P1 | 需要盘口、成交量、触发条件和撤单速度 |
| VWAP / POV | P2 | 需要可靠成交量曲线和市场深度数据 |

---

## 6. 算法交易必须补齐的边界

### 6.1 Unified order intent

所有策略和手动交易都应先生成统一订单意图：

```text
side
symbol
market_type
position_side
reduce_only
notional
quantity
limit_price
time_in_force
execution_algo
risk_budget
client_order_id
strategy_id
basket_id
```

### 6.2 Child order scheduler

TWAP/Iceberg/Sniper 都需要子单调度：

- 分片数量。
- 分片间隔。
- 每片最大数量。
- 撤单重挂。
- 超时处理。
- 最大滑点。
- 成交不足补单。

### 6.3 Market microstructure data

算法交易不能只靠 K 线。至少需要：

- best bid/ask。
- order book top levels。
- recent trades。
- spread。
- depth。
- volatility。
- exchange rate limit。

### 6.4 Reconciliation

执行器必须定期对账：

- 本地订单状态。
- 交易所订单状态。
- 实际成交。
- 当前持仓。
- 手续费。
- 平均成交价。

### 6.5 Kill switch

算法交易需要全局和策略级开关：

- 全局暂停发单。
- 单策略暂停。
- 单交易所暂停。
- 最大错误次数自动停止。
- 最大滑点自动停止。
- 仓位异常自动停止。
- API key 异常自动停止。

### 6.6 Observability

需要能回答：

- 为什么下了这笔单？
- 计划成交多少？
- 实际成交多少？
- 滑点多少？
- 哪个子单失败？
- 是策略原因、交易所原因还是风控原因？

建议新增：

```text
algo_order_logs
algo_child_order_logs
execution_trace_id
strategy_run_id
```

### 6.7 Exchange capability matrix

算法交易不能假设每个交易所能力一致。需要维护交易所能力矩阵：

```text
exchange
market_type
supports_hedge_mode
supports_reduce_only
supports_post_only
supports_stop_order
supports_iceberg
supports_client_order_id
min_notional
min_quantity
price_tick
quantity_step
rate_limit
order_book_depth
```

运行时根据能力矩阵决定：

- 能否启动某个策略。
- 是否需要本地模拟 stop / iceberg。
- 下单数量和价格如何 round。
- 策略模板是否适合某个交易所。

### 6.8 Account, permission, and secret boundary

算法交易会放大账户风险，必须把账户权限纳入运行前检查：

- API key 是否具备交易权限。
- 是否禁止提现权限。
- 是否支持读取订单和成交历史。
- 是否配置 IP 白名单。
- 是否允许合约交易。
- 是否设置账户级最大风险预算。

策略启动前应做 preflight check，失败时明确告诉用户缺哪个权限或配置，而不是启动后才报错。

### 6.9 Test and certification suite

如果要把 QuantDinger 做成基础设施，需要有策略运行时验收测试：

- 重启恢复测试：发单中途杀进程，恢复后不重复下单。
- 部分成交测试：均价和 TP 按真实成交更新。
- 拒单测试：最小下单量、余额不足、API 错误后不误改 basket。
- 断网测试：状态进入 `needs_review`，不继续补仓。
- 多空 hedge 测试：long basket 与 short basket 独立。
- 回测/实盘一致性测试：同一信号路径能对齐计划成交点。
- 交易所精度测试：价格 tick、数量 step、最小名义金额都正确处理。

---

## 7. 产品分期路线

### Phase 1：ScriptStrategy 专业化

目标：让复杂状态机策略可稳定运行。

- Basket runtime state。
- 多空独立 position/basket 接口。
- 成交驱动均价。
- 重启恢复。
- 策略运行 ID、代码版本快照和参数快照。
- 事件流水和恢复审计。
- 单写入者锁，避免重复运行。
- 复杂策略回测报告。
- Layered Martingale Basket 官方模板。

### Phase 2：基础 AlgoTrading

目标：让用户可以选择执行算法，而不只是市价/限价。

- Unified order intent。
- TWAP。
- BestLimit。
- Stop / StopLimit。
- 子单状态表。
- 执行日志和偏差分析。
- 交易所能力矩阵。
- 账户权限 preflight check。

### Phase 3：高级执行

目标：降低大额交易滑点，支持专业交易执行。

- Iceberg。
- Sniper。
- VWAP。
- POV。
- 盘口深度驱动。
- 智能撤单重挂。

### Phase 4：基础设施化

目标：QuantDinger 成为策略、执行、风控、监控一体化基础设施。

- 统一策略运行 ID。
- 全链路 execution trace。
- 交易所能力矩阵。
- 多账户组合执行。
- 回测/模拟盘/实盘一致性报告。
- 策略市场模板准入审核。
- 风险沙盒和资金预算模拟器。
- 运行时验收测试套件。

---

## 8. 推荐落地顺序

最推荐的实际落地顺序：

1. 先做 `LayeredMartingaleBasket` 官方脚本模板，只支持 `long` / `short`。
2. 增加 `strategy_run_id`、代码快照、参数快照，保证可追溯。
3. 增加 basket 状态持久化和事件流水，解决重启恢复。
4. 增加单写入者锁和幂等下单，解决重复运行和重复补仓。
5. 增加成交驱动均价，解决部分成交和真实 TP。
6. 增加 `long_basket` / `short_basket`，再开放多空双开。
7. 增加交易所能力矩阵和启动前 preflight check。
8. 增加 TWAP / BestLimit，作为算法交易第一版。
9. 再做 Iceberg / Sniper。

这样既能尽快满足用户需求，又不会把复杂风险压到脚本作者身上。

---

## 9. 对外表述建议

当前阶段建议谨慎表述：

```text
QuantDinger supports stateful ScriptStrategy workflows for scale-in, DCA,
martingale-style baskets, average-cost exits, backtesting, and live execution.
Advanced multi-leg basket persistence, hedge-mode basket accounting, and
algorithmic execution orders such as TWAP, Iceberg, Sniper, and BestLimit are
planned infrastructure upgrades.
```

中文：

```text
QuantDinger 当前支持有状态脚本策略，可实现分批加仓、DCA、马丁篮子、均价止盈、
回测和实盘执行。多腿篮子持久化、多空独立篮子核算，以及 TWAP、Iceberg、
Sniper、BestLimit 等算法订单执行能力，将作为后续基础设施升级重点。
```

不要过早宣传“完整算法交易平台”，应先宣传“可扩展的策略运行时 + 正在建设中的算法执行基础设施”。
