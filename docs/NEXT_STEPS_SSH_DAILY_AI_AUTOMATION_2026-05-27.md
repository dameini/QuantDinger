# QuantDinger SSH 下一步操作文档：AI 自动回测、指标优化、策略重启、Bark 通知

生成时间：2026-05-27
项目目录：`/home/jianpeng/opt/QuantDinger`
适用场景：通过 SSH 在 Debian 服务器继续配置 daily 定时任务。

安全说明：本文不保存真实 Agent Token、Bark Key、交易所密钥或前端登录 JWT。所有敏感值都用占位符表示。

## 1. 当前确认状态

进入项目目录：

```bash
cd /home/jianpeng/opt/QuantDinger
```

检查 Docker 服务和后端健康状态：

```bash
docker compose ps
curl -m 10 -fsS http://127.0.0.1:5000/api/health
```

配置文件关系：

- 根目录 `.env`：Docker Compose 使用，控制镜像 tag、端口、数据库默认值等。
- 根目录 `.env.example`：根目录 `.env` 的模板，不参与运行。
- `backend_api_python/.env`：后端运行配置，容器挂载到 `/app/.env`。
- `backend_api_python/env.example`：后端 `.env` 模板。
- 根目录 `backend.env`：当前 `docker-compose.yml` 不使用，主要和 `docker-compose.ghcr.yml` 相关。

当前 Agent 令牌权限为 `R/B/W/N`：可以读取、回测、修改、通知；不能启动或停止实盘策略。

## 2. 本次策略/指标优化结论

目标策略：

- 策略 ID：`6`
- 交易对：`DOGE/USDT`
- 周期：`5m`

已修复问题：原策略在 `output["signals"]` 中放图表标记列表，导致回测引擎误读，报错 `signals must be a dict`。

正确规则：

- 回测/执行引擎读取 `df["buy"]` 和 `df["sell"]`。
- 前端图表需要买卖标记时，只在非回测环境输出 `output["signals"]`。
- 回测环境不要输出 `output["signals"]` 图表标记。
- 不要在代码或注释中出现 `globals()`。

推荐判断回测环境方式：

```python
try:
    backtest_params
    is_backtest_env = True
except NameError:
    is_backtest_env = False
```

优化前验证：

```text
version_mark: ai-test-try2-20260526-143032Z
job_id: 2339be589d1f4180b042d85004c3bb84
status: succeeded
totalReturn: -0.9
maxDrawdown: -14.22
sharpe: -0.17
winRate: 75.0
profitFactor: 1.16
trades: 12
```

优化后验证：

```text
version_mark: ai-opt-20260526-144706Z
job_id: f735744df3354490913861ddb153356c
status: succeeded
totalReturn: 5.15
maxDrawdown: -10.97
sharpe: 2.89
winRate: 66.67
profitFactor: 6.28
trades: 6
```

用户明确要求：备份指标，不是备份策略。

已确认：

```text
当前优化指标 id: 8
备份指标 id: 9
source_indicator_id: 8
backup_indicator_id: 9
version_mark: ai-opt-20260526-144706Z
```

检查指标备份：

```bash
docker compose exec -T postgres psql -U quantdinger -d quantdinger -c "select id, name, user_id, source_indicator_id, length(code) as code_len, updated_at from qd_indicator_codes where id in (8,9) order by id;"
```

## 3. daily 自动任务目标

目标流程：

```text
每天定时执行
  -> 读取当前策略和指标
  -> 备份当前指标到 qd_indicator_codes
  -> 生成 AI 优化候选
  -> 对候选逐个回测
  -> 选择评分最高版本
  -> 更新原指标代码
  -> 更新策略配置 version_mark / backup_indicator_id
  -> 再跑一次最终验证回测
  -> Bark 推送结果
  -> 可选：停止策略并重新启动策略
```

必须遵守：

- 备份对象是 `qd_indicator_codes` 指标代码。
- 不要复制出一条新的策略作为备份。
- 修改指标前必须先备份。
- 更新后必须跑最终验证回测。
- Bark 通知不要输出完整 token、交易所密钥等敏感信息。

## 4. 自动重启策略的限制

后端策略启动/停止接口：

```text
POST /api/strategies/stop?id=<strategy_id>
POST /api/strategies/start?id=<strategy_id>
```

这两个接口带 `@login_required`，属于前端登录态接口。

结论：

- 当前 `R/B/W/N` Agent 令牌不能重启策略。
- 不建议只改数据库 `status=running`，因为实际执行线程不会可靠启动。

可选方案：

- 方案 A：推荐。定时任务只自动回测、备份指标、修改指标、Bark 通知，通知后前端人工确认并手动重启。
- 方案 B：使用前端登录 JWT 自动重启。
- 方案 C：开启 `T` scope。需要把部署从 hosted/saas 模式调整为 self-host live trading 模式，并签发带 `T` 的 Agent 令牌。`T` 权限可以路由真实订单，风险最高。

使用前端 JWT 自动重启时，脚本需要调用：

```bash
curl -X POST "http://127.0.0.1:5000/api/strategies/stop?id=6" -H "Authorization: Bearer $QD_WEB_JWT"
curl -X POST "http://127.0.0.1:5000/api/strategies/start?id=6" -H "Authorization: Bearer $QD_WEB_JWT"
```

## 5. 配置 daily 任务

创建配置和日志目录：

```bash
cd /home/jianpeng/opt/QuantDinger
mkdir -p config logs
```

创建环境配置：

```bash
nano config/ai_daily.env
chmod 600 config/ai_daily.env
```

示例内容：

```bash
QD_BASE_URL=http://127.0.0.1:5000
QD_FRONTEND_URL=http://127.0.0.1:8888
QD_AGENT_TOKEN=替换为你的_R_B_W_N_令牌
QD_STRATEGY_IDS=6
QD_BACKTEST_DAYS=7
QD_DRY_RUN=true
BARK_URL=https://api.day.app/替换为你的BarkKey
QD_RESTART_MODE=manual
QD_WEB_JWT=
```

首次运行建议：

```bash
QD_DRY_RUN=true
QD_RESTART_MODE=manual
```

确认无误后再改：

```bash
QD_DRY_RUN=false
```

## 6. 正式自动优化脚本要求

当前项目里存在临时禁用脚本：

```text
scripts/ai_strategy_auto_optimize.py.disabled-codex
```

不要直接启用它，除非确认已经改成备份指标，而不是备份策略。

正式脚本目标路径：

```text
scripts/ai_strategy_auto_optimize.py
```

检查命令：

```bash
cd /home/jianpeng/opt/QuantDinger
ls -la scripts/ai_strategy_auto_optimize.py scripts/ai_strategy_auto_optimize.py.disabled-codex 2>/dev/null || true
```

脚本必须包含：

- 读取 `config/ai_daily.env`。
- 用 Agent API 跑回测。
- 从数据库读取当前策略关联的指标 ID。
- 修改前复制当前指标到 `qd_indicator_codes`，并设置 `source_indicator_id`。
- 更新原指标代码，而不是创建新策略。
- 更新策略配置中的 `backup_indicator_id` 和 `version_mark`。
- 最终验证回测成功后才发送成功通知。
- 失败时发送 Bark 失败通知并退出非 0 状态码。

## 7. 创建 daily 启动脚本

```bash
cd /home/jianpeng/opt/QuantDinger
nano scripts/run_ai_daily_optimize.sh
chmod +x scripts/run_ai_daily_optimize.sh
```

内容：

```bash
#!/usr/bin/env bash
set -euo pipefail

cd /home/jianpeng/opt/QuantDinger
mkdir -p logs

set -a
source config/ai_daily.env
set +a

if [ ! -f scripts/ai_strategy_auto_optimize.py ]; then
  echo "missing scripts/ai_strategy_auto_optimize.py" >&2
  exit 2
fi

docker cp scripts/ai_strategy_auto_optimize.py quantdinger-backend:/tmp/ai_strategy_auto_optimize.py

docker compose exec -T backend python /tmp/ai_strategy_auto_optimize.py
```

手动测试：

```bash
cd /home/jianpeng/opt/QuantDinger
QD_DRY_RUN=true scripts/run_ai_daily_optimize.sh
```

## 8. 配置 cron 每天执行

编辑 crontab：

```bash
crontab -e
```

加入每天凌晨 2:30 执行：

```cron
30 2 * * * flock -n /tmp/quantdinger-ai-daily.lock /home/jianpeng/opt/QuantDinger/scripts/run_ai_daily_optimize.sh >> /home/jianpeng/opt/QuantDinger/logs/ai_daily_cron.log 2>&1
```

查看 cron：

```bash
crontab -l
```

查看日志：

```bash
tail -100 /home/jianpeng/opt/QuantDinger/logs/ai_daily_cron.log
```

## 9. 前端查看优化结果

1. 打开 QuantDinger 前端。
2. 进入策略列表。
3. 找到策略 ID `6` 或名称包含 `AI优化版` / `ai-opt` 的策略。
4. 查看策略配置中的 `version_mark`。
5. 查看关联指标代码是否已更新。
6. 查看最新回测记录，对比 `totalReturn`、`maxDrawdown`、`sharpeRatio`、`winRate`、`profitFactor`、`totalTrades`。
7. 如果 `QD_RESTART_MODE=manual`，前端确认后手动停止并启动策略，让执行器加载新指标。

## 10. 下一步操作顺序

第一步，确认正式脚本是否存在：

```bash
cd /home/jianpeng/opt/QuantDinger
ls -la scripts/ai_strategy_auto_optimize.py scripts/ai_strategy_auto_optimize.py.disabled-codex 2>/dev/null || true
```

第二步，如果没有正式脚本，先不要配置 cron，先补齐 `scripts/ai_strategy_auto_optimize.py`。

第三步，配置 `config/ai_daily.env`，并先使用：

```bash
QD_DRY_RUN=true
QD_RESTART_MODE=manual
```

第四步，手动 dry-run：

```bash
scripts/run_ai_daily_optimize.sh
```

第五步，确认 dry-run 成功后正式执行：

```bash
nano config/ai_daily.env
# 改 QD_DRY_RUN=false
scripts/run_ai_daily_optimize.sh
```

第六步，确认 Bark 收到通知。

第七步，配置 cron daily。

## 11. 常用排错命令

后端健康检查：

```bash
curl -m 10 -fsS http://127.0.0.1:5000/api/health
```

Docker 服务状态：

```bash
docker compose ps
```

后端日志：

```bash
docker compose logs --tail=200 backend
```

cron 日志：

```bash
tail -200 /home/jianpeng/opt/QuantDinger/logs/ai_daily_cron.log
```

检查最近指标：

```bash
docker compose exec -T postgres psql -U quantdinger -d quantdinger -c "select id, name, source_indicator_id, length(code) as code_len, updated_at from qd_indicator_codes order by id desc limit 10;"
```

检查策略配置：

```bash
docker compose exec -T postgres psql -U quantdinger -d quantdinger -c "select id, name, status, symbol, timeframe, indicator_id, config from qd_strategies where id = 6;"
```

## 12. 风险提醒

- 自动优化不能只看单次 7 天回测，交易次数太少容易过拟合。
- 评分函数应惩罚低交易次数、高回撤、过低 profitFactor。
- daily 任务默认不要自动实盘重启，先 Bark 通知人工确认。
- 如果启用自动重启，建议先用小资金或模拟盘验证。
- Token、Bark Key、交易所 API Key 不要写进 git。
- 如果真实 token 曾暴露在聊天或日志中，建议轮换。

## 13. 本文档位置

```text
/home/jianpeng/opt/QuantDinger/docs/NEXT_STEPS_SSH_DAILY_AI_AUTOMATION_2026-05-27.md
```

SSH 登录后查看：

```bash
cd /home/jianpeng/opt/QuantDinger
less docs/NEXT_STEPS_SSH_DAILY_AI_AUTOMATION_2026-05-27.md
```
