# 本机部署验收：2026-09-19

部署已完成，服务运行在原四个容器中。决策台：http://localhost:8080 ；
Grafana：http://localhost:3000 。启动和操作命令见 [玩法与操作](market-profiles.md)。

## 当前镜像与持久化

- 镜像：`prediction-trader-trader:local`，另标记 `multi-market-20260919`。
- 镜像 SHA-256：`09b862bf98654b7d144ca57fd1a25cda9f4d8cfa11a393cc8acf4f61228ca748`。
- PostgreSQL 17、Prometheus v3.5.0、Grafana 12.1.0 保持原有数据卷。
- 新增历史费率观察、结算接收时间、独立报告表；五个在线指标查询索引已应用。
- 原 `market_data` 卷目录权限调整为 trader 可写，新增镜像也预设该权限。
- 普通用户模拟账本：`/data/paper-ledger.sqlite3`；验收使用独立
  `/data/deployment-check.sqlite3` 和 `verification20260919:*` 命名空间。
- 数据库结构备份：WSL `/tmp/prediction-trader-schema-before-multi.sql`。
  本轮迁移为追加结构，没有删除原市场或行情数据。
- Windows 公共 HTTPS 中继独立监听127.0.0.1:18866；`./pt start` 确保其运行。
  原有 localhost 页面转发及 TradingAgents 服务未改动。

## 验收结果

1. **418 项测试通过**：包含实际 PostgreSQL 隔离测试和 Windows Chrome 浏览器交互；
   Ruff 全仓检查及 Mypy 63 个源码文件通过，容器 `pip check` 通过。
   临时测试 PostgreSQL 容器已经清理。
2. 三玩法真实 Gamma 发现成功；一个 CLOB 连接订阅12个 token；
   BTC/ETH 各一个 Binance 和一个 Chainlink TWAP 连接，12个必需工作线程正常，
   `prediction_trader_ready=1`。
3. 三玩法 HTTP state/profile/report 隔离正确。最后运行检查时三者行情质量均为 ok；
   后续遇到安静盘口、断线或跨窗口时仍可能按规则降级，不能把此快照当永久保证。
4. 真实浏览器验证3玩法、3报告、4 Binance 品种及事件2730675 YES；
   无 JavaScript 异常、无浏览器下单，账户未被测试创建。
5. 独立账本使用真实公共报价完成14笔模拟成交：两种现货往返、两种永续多/空往返，
   以及普通事件往返；全部平仓，费用扣除正确。重复订单不重复成交。
   曾遇到过时报价，系统按预期拒绝；刷新为合格报价后才完成模拟成交。
6. trader 受控重启后，14笔验收成交、现金、费用及平仓状态完全保留；
   采集、报告和网页恢复正常。
7. 三种回放报告均已生成。最近一次验收的市场/预测数为 BTC5=6/4、
   BTC15=2/1、ETH5=3/1；BTC5已有2个已结算预测，其余仍等待更多历史。
   当时均无回放成交，不据此宣称策略收益或胜率改善。
8. 已读取隔壁真实研究目录：BTC 返回 `missing_evidence_timestamp`，ETH 返回
   `missing`。缺少证据截止时间的报告不会冒充实时研究；接口保持只读，
   未调用模型、未修改隔壁输出。

浏览器截图和机器可读记录位于 Windows：
`outputs\prediction-trader-qa\`，
文件为 `production-analysis.png`、`production-trading.png`、
`production-browser-qa.json`。

## 可重复检查

```bash
cd /path/to/prediction-trader
./pt status
docker exec -i prediction-trader-trader-1 python - < deploy/verify_runtime.py
docker exec -i prediction-trader-trader-1 python - --verify-only < deploy/smoke_paper_roundtrip.py
```

`smoke_public_quotes.py` 仅访问公共行情；`smoke_paper_roundtrip.py` 非 verify 模式会
明确生成隔离账本的模拟成交，普通用户无需运行。标记的事件ID和行情时间会随市场变化，
历史验收结果不代表该事件将来仍开放交易。

真实下单、自动策略执行、真实强平与自动资金费计提尚未启用。
研究接口是背景展示，当前上游仍需提供真实证据时间后才能产生有效上下文。

