# 安装、升级与排障

## 环境要求

- Linux / macOS / WSL 中的 Bash、Git、Docker 和 Docker Compose 插件。
- Windows 使用 WSL 2，并在 Docker Desktop 中启用对应发行版的集成。
- 仅使用 Docker 时不需要宿主机 Python；开发、运行测试需要 Python 3.12。
- 可连接 Polymarket 和 Binance 的公开数据端点。无需交易所 API 密钥。
- 可选的 `wsl-relay` 模式需要 Compose **2.24.4+**，因为覆盖端口使用了 `!override`。

```bash
docker version
docker compose version
```

建议把 WSL 项目放在 Linux 文件系统，例如 `~/projects/prediction-trader`。

## 首次安装：通用模式

```bash
git clone https://github.com/000xiaoming/prediction-trader.git
cd prediction-trader
./pt init
```

`init` 以仅当前用户可读写的权限创建 `.env`，已有文件不会被覆盖。编辑本地配置：

- 保持 `TRADING_MODE=paper`，Compose 也会强制使用 paper 模式。
- `GRAFANA_ADMIN_USER` 和 `GRAFANA_ADMIN_PASSWORD` 是 Grafana 初始账号。
- `POSTGRES_PASSWORD` 和 `DATABASE_URL` 中的密码必须一致；URL 中的特殊字符需要编码。
- `INDICATOR_PUBLISHED_PORT` / `GRAFANA_PUBLISHED_PORT` 默认是 8080 / 3000。
- 市场集合通过 `TRADING_PROFILE` 和 `ENABLED_TRADING_PROFILES` 设置。

```bash
./pt check
./pt start
./pt status
./pt logs -f trader
```

`check` 只验证 Compose 配置，不启动服务、不输出解析后的密码。
默认部署启动 trader、PostgreSQL、Prometheus 和 Grafana。两个网页端口只绑定
`127.0.0.1`；数据库、Prometheus 没有宿主机端口。打开：

- http://127.0.0.1:8080：分析页面和模拟交易。
- http://127.0.0.1:3000：Grafana。

页面等待市场或完整参考窗口时，先检查日志和数据新鲜度。首次安装没有历史数据，
回放报告可能暂时为空。项目不会用缺失或过期数据强行生成概率。

## 日常操作与升级

```bash
./pt status
./pt logs -f trader
./pt stop
./pt start
```

升级前先备份 PostgreSQL、模拟账本和本地 `.env`。确认本地改动已妥善保存后：

```bash
git pull --ff-only
./pt check
./pt start
```

数据保存在 Docker 命名卷中。`stop` 保留容器和数据；`down` 默认保留数据卷。
`down -v` 会删除持久数据，不应用作普通重启或网络排障步骤。

需要留存可读的模拟流水时，可以在停止服务前运行：

```bash
mkdir -p exports
./pt paper export paper:binance_btc_spot > exports/btc-spot.csv
```

此命令完整导出指定账户的流水，不请求行情。导出不包含可恢复的全部账户状态，
不能代替前述数据库备份；格式和筛选见[流水说明](paper-trading.md#查询和导出流水)。

不要在已有数据库上仅修改 `.env` 的密码来尝试重置账号；PostgreSQL 的初始化变量只对
新数据库生效。已有实例需要正常的数据库账号变更流程。

## 可选：原有 WSL 工作站模式

只有已经配置 Windows HTTPS 中继和页面转发的工作站才需要此模式。
一般的新用户先使用通用模式。

```bash
./pt --mode wsl-relay check
./pt --mode wsl-relay start
./pt --mode wsl-relay status
```

此模式合并 `compose.yaml`、`compose.local.yaml` 和 `compose.research.yaml`，
在启动前检查 Windows 的公共 HTTPS 中继，并创建 `data/research/`。
存在 `../TradingAgents/data/paper/research` 时，再加入只读研究目录覆盖文件。

- 中继监听 Windows `127.0.0.1:18866`，容器通过 `host.docker.internal:18866` 使用它。
- 页面默认发布到 `198.18.0.1:13080`，Grafana 默认发布到 `198.18.0.1:13000`。
- `198.18.0.1` 是原工作站的虚拟网卡地址，不是所有 WSL 环境都会拥有的地址。
- Windows localhost 的 8080 / 3000 转发需由工作站预先配置；启动器不会修改全局代理、
  防火墙或系统转发规则。
- 可在 `.env` 中设置 `WSL_RELAY_ADDRESS`、`WSL_INDICATOR_PORT` 和 `WSL_GRAFANA_PORT`。
- 中继需要 Windows Python 3。启动脚本优先查找 `py.exe`，其次查找实际的 `python.exe`。
  也可以在 WSL 中导出 `PT_WINDOWS_PYTHON`，值为 Windows Python 的完整路径。
- 可通过 `PT_WINDOWS_STATE_DIRECTORY` 指定 Windows 中继的状态目录；默认位于
  `%LOCALAPPDATA%\PredictionTrader\market-proxy`。

模式选择顺序是 `--mode`、环境变量 `PT_DEPLOYMENT_MODE`、本地 `.pt-mode` 文件、
最后为 `portable`。原工作站需要保留旧的 `./pt start` 行为时，可以在项目目录执行：

```bash
printf '%s\n' wsl-relay > .pt-mode
```

`.pt-mode` 不会被提交，也不会被复制进镜像。随时可以使用
`./pt --mode portable check` 明确选择通用模式。

## 可选：只读研究文件

通用模式默认不挂载其他项目。要提供研究 JSON，先创建目录，再明确加入覆盖文件：

```bash
mkdir -p data/research
docker compose --env-file .env \
  -f deploy/compose.yaml -f deploy/compose.research.yaml up --build -d
```

文件时间和格式要求见[研究集成说明](tradingagents-integration.md)。无效研究文件会显示为
不可用背景信息，不会改变模拟订单的执行权限。

## 常见问题

| 现象 | 检查方法 |
| --- | --- |
| 找不到 `.env` | 在项目目录运行 `./pt init`，已有配置不会覆盖 |
| Docker 命令不可用 | 启动 Docker；WSL 用户检查 Docker Desktop 的发行版集成 |
| 端口被占用 | 修改 `.env` 中两个 `*_PUBLISHED_PORT`，再执行 `./pt start` |
| 无法绑定 `198.18.0.1` | 当前模式不适用于该机器；使用 `--mode portable` |
| 镜像或 Python 包下载失败 | 分别检查 Docker 构建网络与宿主机代理，二者不自动共享配置 |
| GitHub 能访问，但行情失败 | 检查容器内的数据端点连接；宿主机 Git 代理不等于容器代理 |
| 页面等待数据 | 检查 `./pt logs -f trader`，等待完整窗口并确认行情来源可达 |
| 流水为空 | 确认所选标的、事件类型和账户；只有成功记账的操作才有记录，CLI 默认账本与容器账本不同 |
| CSV 只有一页 | 页面下载按当前页导出；全部历史使用 `./pt paper export` |
| Grafana 密码修改未生效 | `.env` 初始化变量不会重置已有 Grafana 数据卷中的账号 |

不要通过关闭 TLS 验证来处理连接问题。页面和模拟订单 API 没有用户认证；需要远程访问时，
先增加独立的认证和访问控制，不要直接把默认端口暴露到公网。
