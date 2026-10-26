# 普通事件、现货与永续的模拟交易

除原有 BTC 5 分钟、BTC 15 分钟、ETH 5 分钟决策台外，项目现在提供独立的公开行情与模拟交易模块。没有真实订单接口，也不需要交易所 API 密钥。

| 类型 | 选择方式 | 支持行为 |
| --- | --- | --- |
| Binance BTC/USDT 现货 | `binance_btc_spot` | 买入、卖出已有数量 |
| Binance ETH/USDT 现货 | `binance_eth_spot` | 买入、卖出已有数量 |
| Binance BTC/USDT 线性永续 | `binance_btc_perpetual` | 做多、做空、减仓、显式资金费记账 |
| Binance ETH/USDT 线性永续 | `binance_eth_perpetual` | 做多、做空、减仓、显式资金费记账 |
| Polymarket 普通二元事件 | `polymarket_event_<Gamma市场数字ID>_yes` 或 `_no` | 买卖选定结果份额、最终结算 |

事件选择使用 **Gamma 的 market ID**，不是 event ID、slug 或自行猜测的 token ID。适配器检查普通 Yes/No 结果与真实 token 的对应顺序，要求市场仍开放并接受 CLOB 订单，排除需要另行处理的 negative-risk 市场。到期后停止开单，最终结算另外核验。

## 页面与账户隔离

页面为每个交易品种使用独立账户名 `paper:<instrument_id>`。例如 BTC 现货是 `paper:binance_btc_spot`，ETH 永续是 `paper:binance_eth_perpetual`；事件 Yes 与 No 也各有账户。每个新账户首次成功下单时获得默认 1000 单位虚拟报价货币。USDT 与 USDC 不混用，不转换汇率，不给已有账户重复充值。

CLI 允许明确指定 `--portfolio`，用于隔离策略实验。模拟资金、持仓、已实现盈亏、费用、资金费、订单幂等键与事件审计存入 SQLite，重启后继续使用。Compose 设置 `PAPER_LEDGER_PATH=/data/paper-ledger.sqlite3`，文件保存在现有 `/data` 数据卷中。未设置环境变量时，CLI 使用当前目录的 `data/paper.db`；这与容器里的页面账本是不同文件。

在 WSL 项目目录可运行下面的命令。要操作页面同一份账本，请把命令中的 `prediction-paper` 替换成：

```bash
docker compose --env-file .env -f deploy/compose.yaml exec trader prediction-paper
```

本地开发环境尚未更新可执行入口时，可使用 `.venv/bin/python -m prediction_trader.trading.cli`。

## 查看支持品种与当前行情

```bash
cd ~/projects/prediction-trader
prediction-paper list
prediction-paper quote binance_btc_spot --portfolio paper:binance_btc_spot
prediction-paper quote binance_eth_perpetual --portfolio paper:binance_eth_perpetual
```

普通事件先从 Gamma 找到开放的数字 market ID，再选择 Yes 或 No，例如：

```bash
prediction-paper quote polymarket_event_2730675_yes --portfolio paper:polymarket_event_2730675_yes
```

该市场 ID 只是部署验收时的真实示例，后续可能到期；选择时以公开 API 当前状态为准。`quote` 输出买卖价、显示数量、时间依据，以及动态读取的最小数量、最小名义金额和精度。永续额外输出标记价格、资金费率与下一资金费时间。

## 显式模拟下单

下面的 `10 bps` 和 `5 bps` 是人为指定的**模拟费用假设**，不代表 Binance 或 Polymarket 的实际费用。每笔订单都必须指定费用，可明确填 `0`；程序不会静默认为交易免费。

```bash
# BTC 现货：买入 0.002 BTC，费用假设为成交名义金额的 0.10%。
prediction-paper paper-run binance_btc_spot \
  --portfolio paper:binance_btc_spot --side buy --quantity 0.002 \
  --fee-bps 10 --client-order-id btc-spot-buy-001

# 查看该账户与最近的记账事件。
prediction-paper account paper:binance_btc_spot

# 卖出已有数量；现货不允许裸卖空。
prediction-paper paper-run binance_btc_spot \
  --portfolio paper:binance_btc_spot --side sell --quantity 0.002 \
  --fee-bps 10 --client-order-id btc-spot-sell-001

# 永续：2 倍杠杆做空 0.002 BTC。
prediction-paper paper-run binance_btc_perpetual \
  --portfolio paper:binance_btc_perpetual --side sell --quantity 0.002 \
  --leverage 2 --fee-bps 5 --client-order-id btc-perp-short-001

# 买入平空，禁止扩大或反向开仓。持仓期间杠杆参数保持一致。
prediction-paper paper-run binance_btc_perpetual \
  --portfolio paper:binance_btc_perpetual --side buy --quantity 0.002 \
  --leverage 2 --reduce-only --fee-bps 5 --client-order-id btc-perp-close-001
```

数量还必须满足当时的交易规则、显示深度与可用模拟资金。`--initial-cash 2000` 只影响首次成功创建的账户，不能重置已有账户。可以用 `--stake 100` 替代 `--quantity`，表示最多约 100 单位报价货币的成交名义金额，数量向下按步长取整，**费用另计**；取整后不满足最小金额会拒绝。

`client-order-id` 在同一账户内唯一。同一 ID、同一订单参数重试只返回原成交；改变数量、方向、费用、杠杆或 reduce-only 会报冲突。使用明确 `--quantity` 的 CLI 重试和网页订单重试可直接从账本返回原成交，不依赖网络。CLI 使用 `--stake` 时数量取决于当前报价，因此需要稳定重试时请使用首次返回的明确 `--quantity`。不同账户可使用相同 ID。所有通过公开行情的下单都只修改本地模拟账本。

## 更新估值、资金费与事件结算

账户输出是最后观察到的报价估值，不是后台自动实时权益。页面读取会刷新已存在账户的估值；CLI 可以主动刷新：

```bash
prediction-paper mark binance_btc_perpetual --portfolio paper:binance_btc_perpetual
prediction-paper account paper:binance_btc_perpetual
```

资金费只在显式调用时入账，公开接口返回的费率不会自动重复扣款。下面在当前标记价、当前持仓上注入一次模拟资金费事件，`0.0001` 表示 0.01%。这是操作者指定的模拟情景，不是交易所实际资金费账单或历史结算证明。

```bash
prediction-paper funding binance_btc_perpetual \
  --portfolio paper:binance_btc_perpetual \
  --payment-id btc-perp-funding-001 --rate 0.0001
```

正费率时多头付费、空头收款，负费率反向。相同 payment ID、相同品种与费率的 CLI 重试返回原记录，不再次收费。Python 账本 API 为 `ledger.funding(payment_id, quote, rate)`，要求重放同一事件时保留该事件的原始报价和时间。

Polymarket 事件到期后，可对已有份额查询 Gamma 并申请本地模拟结算：

```bash
prediction-paper settle polymarket_event_2730675_yes \
  --portfolio paper:polymarket_event_2730675_yes
```

程序要求 market/condition/token 对应一致、`closed=true`、`umaResolutionStatus=resolved`，且结果价格严格为 0/1，并校验结算时间。未最终解决或存在争议时拒绝结算；重复调用不会重复支付。该动作不调用链上赎回。

## 记账与明确限制

- 所有价格、数量、费用和盈亏使用 `Decimal`。买单按 ask、卖单按 bid 全量成交，超过该侧显示数量就拒绝。同一账户不能用重复的同一报价观察无限重复消耗显示深度。没有模拟排队、部分成交或真实市场冲击。
- 报价默认最多 10 秒，拒绝明显未来时间、倒退的观察时间、错误品种/账户、交叉盘口、不合精度的价格或数量、过小金额及不足的现金/保证金。Binance 现货 REST 没有交易所报价时间，以请求开始时间记录；其他接口保留交易所时间依据。
- 现货和事件买入直接扣现金，卖出按平均成本计算已实现盈亏。卖空和杠杆被禁用。事件胜出份额最终按 1 支付，失败份额按 0 支付。
- USDT 线性永续的多空数量带符号，交易不扣整个名义本金；开仓计保证金、减仓实现盈亏、费用直接扣现金、资金费单独记账。反向交易先结旧仓，剩余数量建立相反方向仓位。现货资产不作为永续抵押品。
- 永续的模拟杠杆上限固定为 3，维护保证金率为 1%，初始占用按开仓价与标记价较高者计算。它们是项目的保守模拟规则，不是交易所账户的杠杆档位或精确强平规则。维护保证金不足或现金为负时账户停止接收新订单并要求人工复核；不会伪造强平成交，也没有自动解除停止状态。
- 费用模型是显式固定 bps × 成交名义金额。没有模拟交易所 VIP 费率、返佣、资产折扣，或 Polymarket 特有的非线性费用规则；不要把当前收益直接视为可实现净收益。
- 公开行情更新不会自动跑策略、自动下单或自动结算资金费；当前接口用于只读研究与明确触发的模拟执行。日级 TradingAgents 报告也不会直接变成短周期下单指令。

## 模块入口与依据

`trading/models.py` 定义不可变品种与报价/订单契约；`catalog.py` 管理允许品种与事件 token 映射；`adapters.py` 负责公开数据；`engine.py` 负责纯模拟记账；`ledger.py` 提供 SQLite 事务与审计；`cli.py` 提供命令行入口。网页通过 `services/paper_trading.py` 复用同一组接口。

公开接口路径与字段参考 [Binance 现货官方 REST 文档](https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md)、[Binance USDT 永续市场数据文档](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data)、[Polymarket 市场发现文档](https://docs.polymarket.com/market-data/discover-markets)及其[官方 CLOB 客户端类型定义](https://github.com/Polymarket/py-clob-client/blob/main/py_clob_client/clob_types.py)。公共规则刷新只用于提高模拟输入质量，不使本地引擎成为交易所精确复刻。
