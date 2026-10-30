# 运行诊断、备份与恢复

## Docker 部署诊断

```bash
./pt doctor
./pt --mode portable doctor
```

`doctor` 依次检查 `.env`、Compose 插件、配置解析、Docker 守护进程，以及 trader、
PostgreSQL、Prometheus、Grafana 四个服务。会报告缺失、停止、重启中、不健康或尚在启动的容器。
trader 与 PostgreSQL 必须通过容器健康检查；另外两个服务当前只检查运行状态。

- 退出码 `0`：上述检查通过；`1`：存在需要处理的部署问题；`2`：命令参数错误。
- 检查只读取部署状态，不启动、停止或重建容器，不请求行情，也不输出解析后的环境变量。
- `doctor` 不检查行情新鲜度、账户收益或代理端点；诊断通过不代表行情或交易策略有效。
- `./pt check` 只检查配置；`./pt status` 默认只列出运行中的容器，已停止容器可能不显示。
  `doctor` 会包含停止的服务，并给出相应命令提示。

## 三种数据文件的区别

| 文件 | 包含什么 | 用途 |
| --- | --- | --- |
| CSV / JSONL 流水导出 | 单个账户的记账事件 | 分析、核对；不能恢复完整账本 |
| SQLite 账本备份 | 全部模拟账户、持仓、事件、订单幂等键和资金费记录 | 恢复模拟账本 |
| PostgreSQL dump | 行情、历史采集、回放等数据库表 | 单独恢复 PostgreSQL |

SQLite 备份不包含 PostgreSQL、Grafana、Prometheus 或 `.env`。
备份命令不修改账号密码，也不自动复制 `.env`；配置需要在仓库外另外妥善保存。

## 创建并校验模拟账本备份

先确认镜像包含当前代码。只构建、不启动服务：

```bash
./pt build trader
```

WSL 中继模式的构建仍需要该模式配置的 Windows 中继；通用模式不依赖它。
镜像已经构建后，以下维护命令不请求行情，也不启动应用主进程或 PostgreSQL：

```bash
./pt ledger backup /data/backups/paper-snapshot.sqlite3
./pt ledger verify-backup /data/backups/paper-snapshot.sqlite3
```

`ledger` 使用同一 Compose 项目的持久数据卷启动一次性容器，执行后删除该容器。
它不发布服务端口、不启动依赖或 Windows 中继，也不会启动采集或模拟下单服务。
只接受 `backup`、`verify-backup`、`restore` 三种操作。没有镜像时 Docker 仍可能尝试构建或
拉取镜像；准备好镜像后才可完全离线使用。现有 trader 容器运行或停止时都可以维护账本。

备份使用 SQLite 在线备份接口，包含已提交的 WAL 数据，不包含未提交事务。生成独立的
SQLite 文件后检查完整性、表结构及主键/唯一约束、账户可加载性、事件 JSON 和账户引用，
再公布最终文件名。
同名文件、符号链接或同名 SQLite 日志文件已存在时拒绝操作；换一个新文件名即可。

输出 JSON 包含 `verified`、账户数、事件数、文件字节数和 SHA-256。校验会扫描账户和事件，
大账本需要一定时间。`--timeout 60` 可调整 SQLite 复制阶段的秒数上限，不限制后续校验耗时。
在 Linux 文件系统上备份文件权限为 `0600`。普通报错或复制超时会清理临时文件；进程被强杀或
断电时可能残留 `.paper-backup-*`，不会作为成功备份使用。

### 保存到宿主机

`/data/backups/` 仍在同一个 Docker 数据卷里，不能防范整个卷被删除。使用当前项目已存在的
trader 容器把已完成的备份复制到宿主机，容器可以处于停止状态：

```bash
umask 077
mkdir -p backups
docker compose --env-file .env -f deploy/compose.yaml \
  cp trader:/data/backups/paper-snapshot.sqlite3 backups/paper-snapshot.sqlite3
chmod 600 backups/paper-snapshot.sqlite3
sha256sum backups/paper-snapshot.sqlite3
```

核对输出 SHA-256 与创建时的值一致，并把副本保存到数据卷之外的可靠位置。
复制目标请选择未使用的文件名；`compose cp` 不提供备份命令的拒绝覆盖保护。
这里使用默认 Compose 项目名；若自行指定过 `COMPOSE_PROJECT_NAME`，应保持一致。
`backups/` 已被 Git 和 Docker 构建忽略。若尚未创建主服务容器，可先使用本地 Python 命令，
或根据 Docker 的数据卷管理流程提取文件，不需要为了导出备份启动行情采集。

## 恢复到新账本

恢复始终写入一个**尚不存在的新文件**，不覆盖现有账本，不自动切换应用配置。
先恢复并检查，再决定是否让应用使用它：

```bash
./pt ledger restore /data/backups/paper-snapshot.sqlite3 --output /data/paper-restored.sqlite3
./pt ledger verify-backup /data/paper-restored.sqlite3
```

也可以直接从宿主机输入备份，由非 root 的维护容器校验并写入新账本，无需手动修改数据卷属主：

```bash
./pt ledger restore - --output /data/paper-restored.sqlite3 < backups/paper-snapshot.sqlite3
```

两种恢复方法任选其一，并使用尚不存在的目标文件。截断或不完整的输入不会公布为恢复完成的
文件。只恢复来自可信来源、经过核对的备份；SHA-256 用于比对文件，不是来源认证。

切换时先执行 `./pt stop trader`，在本地 `.env` 增加或修改：

```dotenv
PAPER_LEDGER_CONTAINER_PATH=/data/paper-restored.sqlite3
```

然后执行 `./pt check` 和 `./pt start`。保留旧账本；回退时停止 trader，再把该变量改回旧路径。
恢复后的数据只包含备份时的状态，之后的新交易不会自动合并。
不要同时让两个应用实例对同一恢复账本写入。

容器路径必须放在持久化的 `/data/` 内。宿主机开发使用的 `PAPER_LEDGER_PATH` 与 Docker 的
`PAPER_LEDGER_CONTAINER_PATH` 分开，新增设置不会把容器账本意外切换到宿主机相对路径。
应用重新打开恢复文件后会启用 WAL；此时请先创建新备份，再对备份运行 `verify-backup`。

### 本地 Python 用法

```bash
prediction-paper backup backups/paper-snapshot.sqlite3 --db data/paper.db
prediction-paper verify-backup backups/paper-snapshot.sqlite3
prediction-paper restore backups/paper-snapshot.sqlite3 --output data/paper-restored.db
prediction-paper account my-portfolio --db data/paper-restored.db
```

`verify-backup` 检查存储和可加载性，不证明策略表现或金融模型正确。
空账本可以备份；源文件不存在、文件损坏、结构不受支持、账户状态或事件无法读取时会报错。

## PostgreSQL 单独备份与演练

以下命令在 WSL / Bash 中执行，需要 PostgreSQL 服务运行。示例使用仓库默认数据库和角色名。
如需 SQLite 与 PostgreSQL 停在同一业务时点，先停止 trader，并确认没有其他写入者，
再分别备份；这两个工具本身不提供跨数据库的原子快照。

```bash
umask 077
mkdir -p backups
(set -o noclobber
 docker compose --env-file .env -f deploy/compose.yaml exec -T postgres \
   pg_dump -U prediction -d prediction -Fc > backups/postgres-snapshot.dump)
```

必须确认命令成功退出；失败可能留下不完整文件，不应作为成功备份使用。
已有同名文件时改用新名称。恢复演练使用新数据库，不覆盖业务数据库：

```bash
docker compose --env-file .env -f deploy/compose.yaml exec -T postgres \
  createdb -U prediction -T template0 prediction_restore_check &&
docker compose --env-file .env -f deploy/compose.yaml exec -T postgres \
  pg_restore -U prediction -d prediction_restore_check --exit-on-error --single-transaction \
  < backups/postgres-snapshot.dump
```

检查恢复库的表、行数和回放结果后再规划业务切换。上述 PostgreSQL 步骤为手动运维流程，
没有接入 `./pt ledger`，也不会由本项目自动替换运行中的数据库。

参考：[SQLite 在线备份](https://docs.python.org/3.12/library/sqlite3.html#sqlite3.Connection.backup)、
[Compose 一次性任务](https://docs.docker.com/reference/cli/docker/compose/run/)、
[PostgreSQL dump 与恢复](https://www.postgresql.org/docs/17/backup-dump.html)。
