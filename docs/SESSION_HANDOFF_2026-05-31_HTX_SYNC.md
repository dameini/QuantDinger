# 2026-05-31 会话交接文档：HTX 同步、前后端重启、交易记录展示

## 项目状态

- 工作目录：`/home/jianpeng/opt/QuantDinger`
- 主仓库 `origin`：`git@github.com:dameini/QuantDinger.git`
- 主仓库 `upstream`：`https://github.com/brokermr810/QuantDinger.git`
- 前端子仓库：`/home/jianpeng/opt/QuantDinger/QuantDinger-Vue`
- 前端远端：`https://github.com/brokermr810/QuantDinger-Vue`
- 当前运行版本：前端镜像 `ghcr.io/brokermr810/quantdinger-frontend:3.0.21`
- 当前服务状态：后端、前端、PostgreSQL、Redis 均已重启并显示 `healthy`
- 本轮没有推送远端，用户明确要求“先不进行推送”

## 上游同步状态

- 已从主仓库上游 `upstream/main` 拉取并合并。
- 合并后本地 `main` 领先 fork 的 `origin/main` 2 个提交。
- 前端仓库与其远端 `origin/main` 已同步，但存在本地未提交修改。
- 上游有 `v3.0.21` 标签，但主仓库上游 `VERSION` 和 `backend_api_python/app/_version.py` 仍显示 `3.0.20`；本地已手动统一改成 `3.0.21`。

## 文件备份

- 未跟踪文件 `backend.env` 已按用户要求暂时移除。
- 备份位置：`/tmp/QuantDinger-backend.env.backup-20260531-143644`
- 当前工作区不再显示 `backend.env` 未跟踪文件。

## 已完成的主要后端改动

### HTX 交易记录同步

- 新增同步服务：`backend_api_python/app/services/htx_trade_sync.py`
- 新增接口：`POST /api/strategies/trades/sync?id=<strategy_id>`
- 扩展 `GET /api/strategies/trades`：
  - 默认保持兼容。
  - `include_exchange=1` 时返回本地记录、HTX 已同步记录、HTX 未归属记录和未归属汇总。
- 扩展 `qd_strategy_trades` 字段：
  - `source`
  - `exchange_id`
  - `market_type`
  - `external_key`
  - `exchange_order_id`
  - `client_order_id`
  - `exchange_trade_id`
  - `attribution_status`
  - `raw_exchange_json`
- 去重键规则：
  - `external_key = htx:{market_type}:{symbol}:{order_id}:{client_order_id}:{trade_id}:{trade_time_ms}`
- 已调整逻辑：HTX 云端记录不再覆盖本地记录，而是作为 `source='htx'` 的独立记录写入。

### HTX 历史成交拉取

- 修改：`backend_api_python/app/services/live_trading/htx.py`
- 新增 `get_trade_history()`，支持：
  - 现货历史成交/订单接口。
  - USDT 永续 V5 历史成交接口。
  - 旧版 `linear-swap` 历史成交接口兜底。
- 增强成交规范化字段：
  - 支持 `created_time/updated_time`
  - 支持 `trade_volume/trade_avg_price/trade_turnover/fee/real_profit`

### 订单详情兜底同步

策略 6 的 HTX 历史成交接口返回 `fetched=0`，但 `pending_orders` 内已有真实 HTX 订单号和缓存订单详情。因此新增兜底逻辑：

- 如果 HTX 历史成交接口返回 0，读取 `pending_orders.exchange_order_id`。
- 优先按订单号调用 HTX 订单详情接口。
- 如果实时订单详情不可用，则使用 `pending_orders.exchange_response_json` 中缓存的 HTX 原始订单详情。
- 根据缓存字段生成云端交易记录，例如：
  - `order_id`
  - `trade_avg_price`
  - `trade_volume`
  - `trade_turnover`
  - `fee`
  - `real_profit`

注意：该补丁已写入代码并通过 Python 语法检查，随后已重新构建并重启前后端。

### 持仓同步修复

- 修复文件：`backend_api_python/app/services/pending_order_worker.py`
- 问题：策略 6 持仓同步时报错 `name 'strategy_symbol' is not defined`
- 修复：HTX `get_positions(symbol=...)` 改为使用当前策略允许交易的 symbol。
- 修复后日志显示策略 6 可从 HTX 读到实际持仓：
  - `DOGE/USDT long size=100.0 entry=0.101318`
- 本地 `qd_strategy_positions` 也曾核对到：
  - 策略 6：`DOGE/USDT long size=100.00000000 entry_price=0.10131800`

## 已完成的主要前端改动

### 交易记录页

- 修改文件：`QuantDinger-Vue/src/views/trading-assistant/components/TradingRecords.vue`
- 页面打开策略交易记录时自动调用 HTX 同步接口。
- 本地记录和 HTX 云端记录合并显示。
- 按交易时间倒序排列。
- 保留来源列，区分：
  - 本地
  - HTX同步
  - HTX未归属
- 移除了“对账/一致/差异/仅本地/仅云端”等前端展示信息。
- 增加来源筛选复选框，可单独显示本地、云端或同时显示。
- 来源标签改为高对比度配色。
- 表格列宽和 padding 调整得更紧凑。

### 当前持仓页

- 修改文件：`QuantDinger-Vue/src/views/trading-assistant/components/PositionRecords.vue`
- 打开当前持仓时拉取本地持仓和 HTX 云端持仓。
- 增加来源筛选复选框。
- 移除了“对账”列，只保留来源区分。
- 为避免频繁打 HTX API，云端持仓同步最多每 30 秒触发一次；普通轮询仍为 5 秒。

### 前端 API

- 修改文件：`QuantDinger-Vue/src/api/strategy.js`
- `getStrategyPositions(id)` 改为支持额外参数：
  - `include_exchange`
  - `sync_exchange`
- `getStrategyTrades(id, params)` 支持 `include_exchange=1`。
- 新增/使用 `syncStrategyTrades(id)`。

### 国际化

- 修改文件：
  - `QuantDinger-Vue/src/locales/lang/zh-CN.js`
  - `QuantDinger-Vue/src/locales/lang/en-US.js`
- 新增/调整文案：
  - 来源
  - 本地
  - HTX同步
  - HTX未归属
  - 同步火币记录
  - 同步结果计数

## 关键排查结论

### 策略 6 持仓同步

- 已成功。
- 之前错误为 `strategy_symbol` 未定义。
- 修复后不再出现该错误。
- 后端日志能看到 HTX 返回实际持仓。

### 策略 6 交易记录同步

- 最初同步接口成功返回 `200`，但结果为：
  - `spot fetched=0`
  - `swap fetched=0`
  - `inserted=0`
  - `updated=0`
- 数据库中策略 6 当时只有本地记录：
  - `source=local`
  - `count=7`
- 原因不是前端筛选，也不是数据库查询，而是 HTX 历史成交接口返回空。
- 后续已补订单详情兜底同步，使用 `pending_orders.exchange_order_id` 和 `exchange_response_json` 恢复云端成交记录。

## 当前验证情况

已执行并通过：

- Python 编译检查：
  - `backend_api_python/app/routes/strategy.py`
  - `backend_api_python/app/services/htx_trade_sync.py`
  - `backend_api_python/app/services/live_trading/htx.py`
- 前端 JS/脚本语法检查：
  - `QuantDinger-Vue/src/api/strategy.js`
  - `QuantDinger-Vue/src/locales/lang/zh-CN.js`
  - `QuantDinger-Vue/src/locales/lang/en-US.js`
  - `TradingRecords.vue` 和 `PositionRecords.vue` 的 `<script>` 部分
- `git diff --check`
- `git -C QuantDinger-Vue diff --check`
- Docker 构建和重启
- `docker compose ps` 显示服务健康
- 最近错误日志筛选未发现：
  - `ERROR`
  - `WARNING`
  - `Traceback`
  - `Exception`
  - `UndefinedColumn`
  - `strategy_symbol`
  - `failed`

构建时仍有历史 CSS `/deep/` 和 chunk 体积警告，不影响启动。

## 当前未提交修改概览

主仓库存在未提交改动，包含：

- `VERSION`
- `backend_api_python/app/_version.py`
- `backend_api_python/app/routes/strategy.py`
- `backend_api_python/app/services/htx_trade_sync.py`
- `backend_api_python/app/services/live_trading/htx.py`
- `backend_api_python/app/services/live_trading/records.py`
- `backend_api_python/app/services/pending_order_worker.py`
- `backend_api_python/migrations/init.sql`
- `backend_api_python/tests/test_htx_trade_sync.py`

前端仓库存在未提交改动，包含：

- `QuantDinger-Vue/package.json`
- `QuantDinger-Vue/src/api/strategy.js`
- `QuantDinger-Vue/src/config/defaultSettings.js`
- `QuantDinger-Vue/src/locales/lang/en-US.js`
- `QuantDinger-Vue/src/locales/lang/zh-CN.js`
- `QuantDinger-Vue/src/views/trading-assistant/components/PositionRecords.vue`
- `QuantDinger-Vue/src/views/trading-assistant/components/TradingRecords.vue`

## 下一个对话建议

1. 先确认当前交易记录页面是否能看到策略 6 的 HTX 云端记录。
2. 如果仍未显示，优先查：
   - `docker compose logs --since=30m backend | grep -E "HTX trade sync|sync_strategy_trades|cached pending"`
   - `qd_strategy_trades` 中是否出现 `source='htx'`
3. 若云端记录已正常显示，再决定是否提交本地改动。
4. 用户当前不希望推送远端，除非用户明确要求，否则不要 `git push`。
5. 如果需要恢复 `backend.env`，从备份复制回来：

```bash
cp /tmp/QuantDinger-backend.env.backup-20260531-143644 /home/jianpeng/opt/QuantDinger/backend.env
```

## 常用检查命令

```bash
docker compose ps
docker compose logs --since=10m backend frontend | grep -E "ERROR|WARNING|Traceback|Exception|UndefinedColumn|strategy_symbol|failed"
docker compose exec -T postgres psql -U quantdinger -d quantdinger -c "SELECT strategy_id, source, attribution_status, market_type, COUNT(*) FROM qd_strategy_trades WHERE strategy_id=6 OR exchange_id='htx' GROUP BY strategy_id, source, attribution_status, market_type ORDER BY strategy_id NULLS LAST;"
git status --short --branch
git -C QuantDinger-Vue status --short --branch
```
