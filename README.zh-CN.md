# Prediction Trader

[English](README.md) | 简体中文

[![CI](https://github.com/000xiaoming/prediction-trader/actions/workflows/ci.yml/badge.svg)](https://github.com/000xiaoming/prediction-trader/actions/workflows/ci.yml)

一个用于公开行情分析、历史回放和模拟交易的 Python 项目。支持 Polymarket 的
BTC 5 分钟、BTC 15 分钟、ETH 5 分钟 Up/Down 市场，并提供 Binance 现货、永续以及
普通 Polymarket Yes/No 事件的独立模拟账户。

**当前仅支持 paper 模式，没有启用真实下单。** 不需要钱包私钥或交易所 API 密钥。
页面上的概率和回放结果属于研究输出，不能据此推断未来收益。

## 可以做什么

- 同时采集多个市场玩法，在页面中切换分析视图和独立回放报告。
- 查看公开报价、Chainlink 参考价格、行情新鲜度、盘口和实验性概率。
- 手动触发模拟买卖，记录费用、现金、持仓和订单幂等键。
- 按时间点回放历史数据，并明确报告缺失数据、未结算持仓和模拟限制。
- 使用 Prometheus / Grafana 查看运行状态；可选接入只读研究 JSON。

## 快速启动

需要 Docker 和 Docker Compose 插件。Windows 用户请在 WSL 中操作，并启用
Docker Desktop 对该 WSL 发行版的集成。宿主机不必安装 Python，容器镜像自带运行环境。

```bash
git clone https://github.com/000xiaoming/prediction-trader.git
cd prediction-trader
./pt init
```

查看生成的 `.env`，按需修改本地数据库和 Grafana 密码。修改数据库密码时，
`POSTGRES_PASSWORD` 与 `DATABASE_URL` 中的密码必须一致。然后运行：

```bash
./pt check
./pt start
./pt status
```

| 页面 | 默认地址 |
| --- | --- |
| 行情分析和模拟交易 | http://127.0.0.1:8080 |
| Grafana | http://127.0.0.1:3000 |

Grafana 的账号、密码来自 `.env`。首次采集需要时间；等待市场、数据过期或回放样本不足
都是可能出现的状态，不代表系统已经具备可用的交易信号。

新克隆的项目默认使用通用部署，不依赖个人目录、Windows 代理或隔壁的其他项目。
需要 WSL 专用代理、已有工作站迁移或端口排查时，查看[安装与排障](docs/installation.md)。

## 常用命令

```bash
./pt logs -f trader                 # 查看运行日志
./pt profiles list                 # 查看市场玩法
./pt backtest --profile polymarket_btc_5m
./pt paper list                    # 查看模拟交易品种
./pt stop                          # 停止服务，保留数据
./pt --help
```

模拟交易的具体命令见[模拟交易指南](docs/paper-trading.md)，多玩法设置见
[玩法与操作](docs/market-profiles.md)。不要把真实密钥、个人运行数据或 `.env` 提交到 Git。
页面和模拟订单 API 没有用户认证，默认只绑定本机；公开代码仓库不等于可以公开部署该 API。

## 开发和检查

使用 Python 3.12：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
make check
make build
```

GitHub Actions 会运行 Ruff、Mypy、单元测试、独立 PostgreSQL 集成测试、浏览器交互测试、
首次安装配置检查和 Python 包构建。如何在本地开启可选测试见[贡献指南](CONTRIBUTING.md)。

## 文档索引

- [安装、升级和网络排障](docs/installation.md)
- [多市场玩法](docs/market-profiles.md)
- [模拟交易](docs/paper-trading.md)
- [系统架构](docs/architecture.md)
- [研究数据集成](docs/tradingagents-integration.md)
- [安全边界](docs/security.md)
- [后续计划](docs/roadmap.md)

仓库目前未指定开源许可证。
