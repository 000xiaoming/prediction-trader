# 玩法选择与操作

同一进程支持 BTC 5分钟、BTC 15分钟、ETH 5分钟并行采集。页面切换只改变当前视图，
不会重启进程或停止其他玩法。另有现货、永续和普通二元事件的模拟交易区。

| 玩法 ID | 分析与回放 | 数据源 |
|---|---|---|
| polymarket_btc_5m | BTC 300秒 Up/Down | Polymarket CLOB、BTC Chainlink TWAP / Binance |
| polymarket_btc_15m | BTC 900秒 Up/Down | 共享 BTC 参考源，独立合约窗口 |
| polymarket_eth_5m | ETH 300秒 Up/Down | Polymarket CLOB、ETH Chainlink TWAP / Binance |

## 本机启动

WSL 中：

```bash
cd /path/to/prediction-trader
./pt start
./pt status
./pt logs trader
```

Windows PowerShell 中：

```powershell
wsl -d Ubuntu-24.04 -- /path/to/prediction-trader/pt start
```

决策台：http://localhost:8080 。Grafana：http://localhost:3000 。

`pt start` 启动本项目独立的 Windows HTTPS 中继，使用 base + local + research 三份
Compose 配置构建并启动；检测到隔壁研究目录时再加 tradingagents 只读覆盖文件。
中继只监听 127.0.0.1:18866；保持目标 HTTPS 加密，限定公共
行情与 Python 包域名。它解决当前 WSL/Docker 经现有 TUN 路径连接超时的问题。
Windows 重启后重新执行启动命令；没有改全局代理、现有转发服务或隔壁 TradingAgents。

`./pt stop` 停止本项目容器，保留数据。不要用 `down -v` 清理持久数据。

## 启用集合与主视图

基本配置从根目录 `.env` 读取：

```dotenv
TRADING_PROFILE=polymarket_btc_5m
ENABLED_TRADING_PROFILES=polymarket_btc_5m,polymarket_btc_15m,polymarket_eth_5m
PROFILE_BACKTESTS_ENABLED=true
PROFILE_BACKTEST_INTERVAL_SECONDS=300
PROFILE_BACKTEST_LOOKBACK_HOURS=24
```

空的 ENABLED_TRADING_PROFILES 表示仅运行主玩法。主玩法始终自动加入，重复 ID 去重，
未知 ID 拒绝启动。配置对象不可变；各玩法得到独立校验的配置视图。

**本机 local overlay 显式启用以上三个玩法及定时回放**，不修改原 `.env`。
要改变本机启用集合，编辑 `deploy/compose.local.yaml` 后执行 `./pt start`。
页面选择则立即生效并记在浏览器中。

`TRADING_PROFILE` 的资产、slug、Chainlink/Binance 符号由注册表派生，旧的单独 BTC
字段不能覆盖 ETH 选择。可用 `./pt profiles list`、`./pt profiles show` 检查。
Docker Compose 的 `--profile` 是选择容器组，和这里的市场玩法无关。

API：

- `GET /api/profiles`：active 主玩法、enabled 启用集合、profiles 注册表、switching=view_only。
- `GET /api/state?profile=polymarket_eth_5m`：独立行情/概率快照。
- `GET /api/reports?profile=polymarket_eth_5m`：独立最近回放报告；尚未生成时 report=null。
- `GET /api/instruments`、`GET /api/trading/state?instrument=binance_btc_spot`：其他交易类型。
- `POST /api/paper/orders`：显式模拟订单；详见 [模拟交易](paper-trading.md)。

## 共享与隔离

三个玩法使用 3 个市场发现器、1 个合并 CLOB 连接、2 个 Binance 连接和 2 个 Chainlink
连接。BTC5/BTC15 共享 BTC 源，但各自选择开盘和到期；ETH 不会回退到 BTC。
发现只关注相邻窗口，盘口只订阅启用玩法当前及下一窗口的 token 联集。
旧订阅的盘口缓存与节流状态会清理。

每个玩法有自己的快照缓存、窗口边界、研究标的和回放报告。数据库查询为同一
REPEATABLE READ 事务中的 7 次 SELECT；并发读共享一次在途加载。
必需线程退出会撤销 readiness 并停止进程，由 Compose 重启。

TWAP 30/60 秒是参考价格平均窗口，5/15 分钟是合约周期，不能互换。
缺开盘观测、参考过期或断档时不显示概率；中途启动可能等到下一完整窗口。
开盘参考仍是本地近似值，不是已核验的官方结算基准；模型未分别校准。

## 独立回放

```bash
./pt backtest --profile polymarket_btc_5m
./pt backtest --profile polymarket_btc_15m
./pt backtest --profile polymarket_eth_5m --latency-ms 500 --slippage-bps 10
```

默认最近24小时，最多7天/5000个窗口；可通过 --start/--end/--as-of 指定带时区时间。
每个窗口只在固定决策时点评估一次。只读当时已接收的参考数据和市场费率快照，
延迟后必须等新报价才能成交；缺费用记录不会假设免费。成交为顶档价格模拟，
不模拟真实排队、冲击或跨窗口资本分配。

报告包含覆盖率、跳过原因、已结算 Brier score/校准分箱、费用后已实现盈亏和回撤。
未结算持仓单独列出，不记为零收益或已赚取收益。历史数据不足会如实降低覆盖率。
新采集元数据与结算观察时间不会伪造历史可得性；旧行缺失时保守拒绝。

旧 `prediction-replay` / `prediction-execution-replay` 保持 BTC5 专用，
和新多玩法回放分别保存，不混报结果。

## 研究与扩展

`pt` 挂载本项目 `data/research/` 为只读 `/research`，按 BTC-USD/ETH-USD 读取。
研究文件不存在或过期只影响背景展示，不影响本地行情或概率；不会映射成买卖概率。
导出命令和生产者时间契约见 [TradingAgents 集成](tradingagents-integration.md)。

相同 Up/Down 结构扩展在 `profiles.py` 注册，经真实来源、窗口和混合数据库测试。
模型通过 `strategies/indicator_models.py` 替换。其他交易类型通过
`trading/models.py` 的 InstrumentSpec/Quote/PaperOrder/Fill/Position 契约接入，
各自验证精度、成本、结算、保证金和持仓语义；不能只添加一个名字就宣称支持。

股票尚未实现。自动策略下单未接入运行主循环；目前为分析/回放和用户明确触发的模拟订单。
