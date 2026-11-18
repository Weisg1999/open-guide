# golang-migrate 技术指南

> 迁移工具的核心不是「执行 SQL」，而是版本表、幂等、并发锁与回滚边界：跑错一次，可能比不跑更糟。
> 上游：<https://github.com/golang-migrate/migrate> **v4.20.1**（2026-09，CLI + 库）　|　驱动：PostgreSQL / MySQL / SQLite / ClickHouse　|　许可：MIT

---

## 1. 项目简介

golang-migrate（下称 `migrate`）是一个**只做 schema 版本管理**的迁移工具：读一组按版本号排序的 SQL 文件，按顺序执行，并把「执行到哪一版」写进数据库里的 `schema_migrations` 表。它不解析 SQL 语义、不比对目标 schema、不生成 Go 代码——因此它和 [sqlc](../cobra-sqlc/cobra-sqlc-guide.md)、[GORM](../gorm/gorm-guide.md) 不是竞争关系，而是同一条流水线上游的一环。

设计取向非常明确：

- **迁移文件就是纯 SQL**，`.up.sql` / `.down.sql` 成对出现，DBA 可以直接 review，其他语言的服务也能复用同一套文件；
- **语言无关**：CLI 可以单独跑，Go 库只是把同一引擎嵌进进程；
- **source driver / database driver 双插件**：迁移从哪来（本地目录、embed、S3、GitHub）与打到哪去（PG、MySQL、SQLite……）互不影响；
- **v3/v4 API 已冻结**：官方明确不再对 v4 做破坏性改动，升级风险主要来自驱动与依赖，而不是接口。

代价也要说清楚：它**不理解你的 schema**，所以不能做「声明式 diff」，也不能保证你的 down 真的可逆；它**不自动包事务**，原子性取决于数据库本身；它**不记录校验和**，改动已发布的文件不会被发现。这三点是后面所有坑的源头。

适用场景：SQL 优先的团队、需要 DBA 参与评审、多语言共用一个库、容器化部署（迁移作为独立 Job）。**不适合**：想要「改 struct 就自动同步表结构」的原型阶段（用 GORM AutoMigrate）、需要自动生成可回滚 DDL 的大型团队（用 [Atlas](https://atlasgo.io/)）。

### 安装与快速开始

```bash
# CLI：用 build tag 选择编译进去的驱动，全编会让二进制变大
go install -tags 'postgres' github.com/golang-migrate/migrate/v4/cmd/migrate@v4.20.1
docker run --rm -v "$PWD/db/migrations:/migrations" --network host \
  migrate/migrate:v4.20.1 -path=/migrations -database "$DATABASE_URL" up
go get github.com/golang-migrate/migrate/v4@v4.20.1      # 库

migrate create -ext sql -dir db/migrations -seq create_users
# → 000001_create_users.up.sql / 000001_create_users.down.sql
migrate -path db/migrations -database "$DATABASE_URL" up
```

---

## 2. 能力总览

| 能力 | 说明 |
|---|---|
| 迁移源 | `file://`（本地目录）、`iofs`（`embed.FS`）、`http(s)://`、`github://`、`gitlab://`、`s3://`、`gcs://`、`go-bindata` |
| 数据库驱动 | PostgreSQL / `pgx` / MySQL / MariaDB / SQLite3 / ClickHouse / SQL Server / MongoDB / CockroachDB / Redshift |
| 版本表 | `schema_migrations(version BIGINT, dirty BOOLEAN)`，只有两列，无校验和、无时间戳 |
| 版本号 | 文件名前缀，必须是**整数**；`-seq` 生成 6 位自增，`-format` 可改成时间戳 |
| 命令 | `create` / `up` / `down` / `steps` / `goto` / `force` / `version` / `drop` |
| 幂等 | `up` 只跑大于当前版本的；没有新迁移返回 `ErrNoChange` |
| 并发 | PG 用 `pg_advisory_lock`、MySQL 用 `GET_LOCK`、CockroachDB 用锁表；等待可能无限长 |
| 事务 | **不自动包**；PG 下「一个文件一条 query」由服务端隐式事务兜底，MySQL DDL 直接隐式提交 |
| 回滚 | 执行对应的 `.down.sql`；不校验是否真的可逆 |
| Go 库 | `migrate.New` / `NewWithSourceInstance` / `Up` / `Steps` / `Force` / `Version` / `GracefulStop` |
| 错误语义 | `migrate.ErrNoChange`、`migrate.ErrNilVersion`、`migrate.ErrDirty` |

---

## 3. 核心概念与原理

### 3.1 文件命名：版本号必须是整数

```text
db/migrations/
├── 000001_create_users.up.sql
├── 000001_create_users.down.sql
├── 000002_add_users_status.up.sql
├── 000002_add_users_status.down.sql
└── 20260901120000_add_orders_index.up.sql   # -format 时间戳，与上面的 6 位序号不能混用
```

解析规则只有一条：文件名去掉后缀后，按第一个 `_` 切出**版本号**，剩下的当名字。版本号用 `ParseUint` 解析，所以：

- `000001`、`20260901120000` 合法；`v1_init.sql`、`000001.1_x.sql`、`fix-typo.sql` 直接报错或不被识别；
- 排序按**数值**而非字典序（`9` 在 `10` 之前），位数不齐也不会错序，但团队内应统一位数；
- 同一版本必须成对出现 up/down；`up` 缺 down 可以跑，但 `down` 到这一版时找不到文件会失败——**能接受不可回滚，就不要留一个假装可用的 down**。

命名用动词开头的短句：`create_users`、`add_orders_index`、`drop_legacy_flag`。不要用 `update_db`、`fix`、`migration_3`——半年后没人知道它做了什么。

### 3.2 `schema_migrations`：两列就决定了全部语义

```sql
CREATE TABLE IF NOT EXISTS schema_migrations (
    version BIGINT  NOT NULL,
    dirty   BOOLEAN NOT NULL
);
```

表里**通常只有一行**：当前已完整应用到的版本，加上一个布尔标记。没有 `name`、没有 `applied_at`、没有 `checksum`。三条推论：

1. **改名/改内容不会被发现**：已发布的迁移文件被编辑，工具只会看版本号，各环境的 schema 就会静默分叉；
2. **只有「当前版本」没有历史**：想看某一版是谁什么时候上的，得靠 git 记录或另外的审计表；
3. **`dirty` 是唯一的中断信号**：`dirty=true` 意味着「这一版的 SQL 已经开始执行，但没走完，且工具不敢猜现在的状态」。

`dirty=true` 的成因：执行 `.up.sql` 时报错（语法、约束冲突、锁超时、连接断开）。此时 `migrate` **把版本写成该版并置 dirty**，然后所有后续命令都返回 `ErrDirty`，拒绝在未知状态上继续叠加。这是刻意的设计——**脏标记不是 bug，是刹车**。

安全恢复顺序（不要跳步）：

```bash
migrate -path db/migrations -database "$DATABASE_URL" version
# 20260901120000 true   ← 输出是「版本号 + dirty」，记住版本号 V

# 2 连库确认「到底执行到哪一步」：表建了没？索引在不在？列加了几何？
#    PG: \d+ users / SELECT indexname FROM pg_indexes WHERE tablename='users';
#    MySQL: SHOW CREATE TABLE users; SHOW INDEX FROM users;

# 3 手工补完剩余语句（或手工回退已执行的部分），把它当成一次线上故障处理，
#    这段补偿 SQL 放 runbook，不要塞回迁移目录

# 4 把版本表强制指向「真实已生效的版本」——补完了就是 V，回退了就是 V-1
migrate -path db/migrations -database "$DATABASE_URL" force 20260901120000

# 5 修正迁移文件（改成幂等写法），或干脆新开一版重做
```

`force N` 的动作只有一句：写 `schema_migrations`（有行就 `UPDATE ... SET version=N, dirty=false`，空表则 `INSERT`）。**它不执行任何 SQL，也不检查 N 对应的 schema 是否真的存在**。所以：

- force 到**高于真实状态**的版本 → 后续迁移被当成「已应用」跳过，schema 永久缺一段；
- force 到**低于真实状态**的版本 → 下次 `up` 重跑已应用的 SQL，报「already exists」，可能又脏一次；
- 直接 `UPDATE schema_migrations SET dirty=false` 只是把刹车片拆了，风险比 force 更大（至少 force 强制你想清楚版本号）。

### 3.3 执行模型：一次 Exec、无自动事务

`database.Driver.Run(io.Reader)` 的默认实现是把**整个文件当成一条 query** 交给驱动执行。于是同一个文件在不同库上的原子性完全不同：

| | PostgreSQL | MySQL（InnoDB） |
|---|---|---|
| 一个文件多条语句 | 单次 query 内的多语句由服务端包进**隐式事务**，任一失败整体回滚 | 每条 DDL 前**隐式提交**，失败时前面的已生效 |
| 失败后状态 | 表结构回到执行前，但版本表被打脏 | 一半语句已落地，版本表被打脏 |
| `BEGIN; ... COMMIT;` | 可以显式写，DDL 可回滚 | 只能保护 DML；DDL 依然隐式提交 |
| `CREATE INDEX CONCURRENTLY` | 不能和别的语句同文件（隐式事务会报错） | 不支持该语法 |

```sql
-- 000007_add_users_status.up.sql   —— 一件事一个文件
ALTER TABLE users ADD COLUMN status text NOT NULL DEFAULT 'active';

-- 000008_users_status_idx.up.sql   —— CONCURRENTLY 必须独占文件
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_users_status ON users (status);
```

把这两条塞进同一个文件，PG 会因隐式事务报 `cannot run inside a transaction block` 并把版本打脏。要同文件只能开 `x-multi-statement=true`：按分号切分逐条发送，字符串里的分号会切错，且失去文件级原子性——用得越少越好。

与 goose 的关键差异：goose 默认给每个文件包事务，并允许 `-- +goose NO TRANSACTION` 标注例外；`migrate` **没有这类标注**，只能靠拆文件、`x-multi-statement` 或手写 `BEGIN/COMMIT` 来调节。所以拿 goose 的写法直接搬到 `migrate`，最容易在 `CONCURRENTLY` 上翻车。

### 3.4 并发：谁会阻塞，锁在哪里

多个副本同时启动并调用 `up` 时，行为由驱动的锁决定：A 拿到锁执行，B 在 `Lock()` 上阻塞，A 释放后 B 读到「无新迁移」返回 `ErrNoChange`。所以不会有两次并发写 schema，但会有长时间的静默等待。

- **PostgreSQL / pgx**：`SELECT pg_advisory_lock($1)`，锁 ID 由数据库名 + schema + 迁移表名派生；注释写得很直白——**会无限期等待**。所以 B 不会失败，但也可能卡几分钟，健康检查会先挂。
- **MySQL**：`GET_LOCK()` / `RELEASE_LOCK()`，同样是连接级会话锁；`?x-no-lock=true` 可以关掉（多主架构下必须只让一台机器迁移）。**经过 ProxySQL / PgBouncer 的 transaction pooling 时，会话锁可能落到不同后端连接上而失效**，这时锁等于没有。
- **CockroachDB**：没有 advisory lock，退化成锁表方案。

工程结论：

1. **推荐把迁移做成独立步骤**（K8s `Job` / `initContainer` / 发布流水线里的一个 stage），应用只负责启动，不在 `main` 里抢锁；
2. 若坚持启动时自动迁移，必须加**超时**（见 4.2），并在迁移 Job 与副本之间只允许一个写入者；
3. 迁移耗时超过探针阈值时，把 `initialDelaySeconds` 让位于「迁移 Job 完成」这个前置条件，而不是加大探针超时。

### 3.5 source 与 database：两侧都是可替换插件

```text
source driver（迁移从哪来）        migrate 引擎            database driver（打到哪去）
file:// / iofs / s3://  ──►  版本排序、加锁、          ──►  postgres:// / mysql://
github:// / http(s)://        执行、写 schema_migrations      sqlite3:// / clickhouse://
```

- source 侧用 `iofs` 最稳：迁移文件编进二进制，**不再依赖进程工作目录**，容器里 `WORKDIR` 改了也不会找不到文件；
- database 侧用 URL scheme 选驱动，**驱动必须被空导入**才能注册：`_ "github.com/golang-migrate/migrate/v4/database/postgres"`；CLI 则靠编译期 `-tags`；
- 常用 URL 参数：`x-migrations-table`（换表名，多模块共库时用）、`x-statement-timeout`（毫秒，PG/MySQL）、`x-migrations-table-quoted`（PG schema 限定时用）、`x-no-lock`（MySQL）；
- 已有 `*sql.DB` 连接池时用 `database.WithInstance(db, &Config{})` 复用，避免一次迁移多开一堆连接。

### 3.6 不可逆迁移：别假装 down 能还原数据

`down` 只是「执行另一段 SQL」，不存在魔法。分类看：

| 变更 | down 能否真还原 |
|---|---|
| `CREATE TABLE` / `ADD COLUMN`（可空或无默认值） | 能，删回去即可 |
| `ADD COLUMN NOT NULL DEFAULT` | 能删列，但默认值写进去的数据没了 |
| `CREATE INDEX` | 能，`DROP INDEX` 即可 |
| `DROP COLUMN` / `DROP TABLE` | **不能**，down 只能重建空结构，行数据永久丢失 |
| 改类型（`text` → `int`） | 单向有损，反向转换不保证等值 |
| 数据回填 / 拆列合并 | 一般不可逆，除非同时保留旧列 |

因此：

- **破坏性变更用 expand-contract 两阶段**：先加新列并双写 → 回填 → 切读 → 观察一两个版本 → 再单独一版删旧列。删列那一版可以写「down = 重新加回空列」，并在文件头注明**数据不可恢复**；
- **不可逆的 down 要显式声明**，而不是留一个空文件——空 down 会让 `down` 成功退出并把版本退回去，制造「版本说回滚了、schema 没回滚」的假象；
- 生产环境的基本原则：`down` 只用于**发布前**的验证与本地开发；线上回滚靠「前向修复 + 备份恢复」，不靠 `migrate down`。

---

## 4. 使用实战

### 4.1 CLI 全流程

```bash
# 生成：-seq 自增（团队协作易撞号），-format 时间戳（推荐多人并行）
migrate create -ext sql -dir db/migrations -seq create_orders
migrate create -ext sql -dir db/migrations -format 20060102150405 add_orders_paid_at

DSN='postgres://app:secret@db:5432/app?sslmode=disable&x-migrations-table=schema_migrations'

migrate -path db/migrations -database "$DSN" up          # 全部前滚
migrate -path db/migrations -database "$DSN" down 1      # 回滚 1 个
migrate -path db/migrations -database "$DSN" steps 2     # 前滚 2 个
migrate -path db/migrations -database "$DSN" steps -1    # 回滚 1 个
migrate -path db/migrations -database "$DSN" goto 20260901120000
migrate -path db/migrations -database "$DSN" version     # 打印 version 与 dirty
migrate -path db/migrations -database "$DSN" force 20260901120000
migrate -path db/migrations -database "$DSN" drop -f     # 删库级对象，仅开发环境
```

命令语义速查：

| 命令 | 语义 | 注意 |
|---|---|---|
| `up [N]` | 前滚全部或 N 个 | 没有新迁移 → `no change`，退出码 0 |
| `down [N]` | 回滚 N 个（默认 1） | 到 0 之后再 `down` 返回 `ErrNilVersion` |
| `steps N` | N>0 前滚、N<0 回滚 | 与 `up/down N` 等价，符号更直观 |
| `goto V` | 走到指定版本（目标高于当前则前滚，反之回滚） | 会连续执行多个文件，中途失败即 dirty |
| `version` | 打印版本与 dirty | 部署前必跑 |
| `force V` | 只改版本表，不清洗 schema | 见 3.2，慎用 |
| `drop -f` | 删掉 schema 内所有对象含版本表 | 生产禁用 |

### 4.2 Go 库：`embed.FS` + `iofs` + 超时

```go
// internal/db/migrate.go
package db

import (
	"context"
	"embed"
	"errors"
	"fmt"

	"github.com/golang-migrate/migrate/v4"
	_ "github.com/golang-migrate/migrate/v4/database/postgres" // 注册 database driver
	"github.com/golang-migrate/migrate/v4/source/iofs"
)

//go:embed migrations/*.sql
var migrationsFS embed.FS

// Up 把 schema 推到最新版本；ctx 超时会打断阻塞中的锁等待。
func Up(ctx context.Context, dsn string) error {
	src, err := iofs.New(migrationsFS, "migrations") // 第二个参数是 embed 内的相对目录
	if err != nil {
		return fmt.Errorf("加载内嵌迁移: %w", err)
	}
	m, err := migrate.NewWithSourceInstance("iofs", src, dsn)
	if err != nil {
		return fmt.Errorf("构造 migrator: %w", err)
	}
	defer func() { _, _ = m.Close() }() // 关闭 source 与 database 两侧

	// dirty 时不要硬跑：交给发布流程里的人工确认 + force
	if v, dirty, verr := m.Version(); verr == nil && dirty {
		return fmt.Errorf("数据库 dirty（version=%d），先修复并 force 再启动", v)
	} else if verr != nil && !errors.Is(verr, migrate.ErrNilVersion) {
		return fmt.Errorf("读取版本: %w", verr)
	}

	done := make(chan error, 1)
	go func() { done <- m.Up() }() // Up 内部先拿 advisory lock，可能长时间阻塞

	select {
	case err := <-done:
		if errors.Is(err, migrate.ErrNoChange) {
			return nil // 已是最新，属正常路径
		}
		return err
	case <-ctx.Done():
		m.GracefulStop() // 打断等待中的锁/语句，避免副本启动无限挂住
		return fmt.Errorf("迁移超时: %w", ctx.Err())
	}
}
```

调用侧（`main` 里显式给预算，失败就别起服务）：

```go
ctx, cancel := context.WithTimeout(context.Background(), 90*time.Second)
defer cancel()
if err := db.Up(ctx, cfg.DSN); err != nil {
	return fmt.Errorf("迁移失败，拒绝启动: %w", err)
}
```

其他库 API 的用法要点：

```go
m, _ := migrate.NewWithSourceInstance("iofs", src, dsn)
m.Steps(-1)                  // 回滚一版，等价 CLI 的 down 1
m.Force(20260901120000)      // 只在确认 schema 与版本一致后调用
v, dirty, err := m.Version() // err 可能是 ErrNilVersion（空库）
srcErr, dbErr := m.Close()   // 两个错误要分开处理，别只判一个
```

复用已有连接池：`postgres.WithInstance(sqlDB, &postgres.Config{MigrationsTable: "schema_migrations"})` 拿到 `database.Driver`，再 `migrate.NewWithInstance("iofs", src, "postgres", drv)`，避免为迁移单开连接。

### 4.3 迁移文件示例（PG 与 MySQL 的差异）

```sql
-- 000001_create_orders.up.sql（PostgreSQL）
CREATE TABLE orders (
    id          bigserial PRIMARY KEY,
    user_id     bigint      NOT NULL REFERENCES users (id),
    amount      numeric(12,2) NOT NULL CHECK (amount >= 0),
    status      text        NOT NULL DEFAULT 'pending',
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_orders_user_created ON orders (user_id, created_at DESC);
```

```sql
-- 000001_create_orders.down.sql —— 与库无关，顺序与 up 相反
DROP TABLE IF EXISTS orders;
```

MySQL 8 的等价写法要换成 `BIGINT UNSIGNED AUTO_INCREMENT`、`DATETIME(3)`、表级 `KEY` 与 `ENGINE=InnoDB`。多语句文件必须在 DSN 上开 `multiStatements=true`，否则驱动会拒绝整段文本（报语法错误，而不是「只执行了第一条」）：

```text
mysql://app:secret@tcp(db:3306)/app?multiStatements=true&x-migrations-table=schema_migrations
```

开了多语句之后，**部分成功**是常态：`CREATE TABLE` 成功、`ALTER TABLE` 失败，前面的表已经提交，只能靠 3.2 的流程收拾。所以 MySQL 上的硬规则是：**一个文件一条 DDL**。

### 4.4 CI 里验证「可逆」

```bash
set -euo pipefail
migrate -path db/migrations -database "$TEST_DSN" up
migrate -path db/migrations -database "$TEST_DSN" down 1
migrate -path db/migrations -database "$TEST_DSN" up   # 第二次 up 必须干净通过
migrate -path db/migrations -database "$EMPTY_DSN" up  # 再从空库全跑一遍
```

`up → down 1 → up` 是最小成本的反向验证：down 少删东西会在第二次 up 报「already exists」，down 多删东西会把后续版本依赖的对象删掉。测试库的**服务端大版本要与生产一致**——PG 16 能过的 DDL，PG 18 上未必同价。

### 4.5 容器与 K8s：迁移做成独立 Job

```dockerfile
FROM migrate/migrate:v4.20.1 AS migrator
COPY db/migrations /migrations
ENTRYPOINT ["/usr/local/bin/migrate", "-path=/migrations"]
```

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: app-migrate
spec:
  backoffLimit: 1                 # 迁移不是幂等重试友好，失败就停
  activeDeadlineSeconds: 600      # 大表 DDL 需要时间，但必须有上限
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: migrate
          image: registry.example.com/app-migrator:v1.42.0  # 与本次发布同版本
          args: ["-database", "$(DATABASE_URL)", "up"]
          envFrom: [{ secretRef: { name: app-db } }]
```

部署顺序：`Job` 成功 → 再滚动更新应用。这样「多个副本同时启动」的问题根本不会出现，也避免长迁移把 `livenessProbe` 拖死。若用 `initContainer`，要意识到**每个 Pod 都会跑一次**，只能靠 advisory lock 串行，且 `restartPolicy` 与超时都要单独设。

---

## 5. 版本演进

| 版本 | 时间 | 要点 |
|---|---|---|
| v1 → v4 | 2018 起 | 仓库迁到 golang-migrate 组织；v3/v4 API 声明**稳定冻结**，迁移文件与版本表结构自此未变；驱动拆成独立子包，CLI 用 build tag 裁剪 |
| v4.18.3 | 2025-04-24 | 补丁维护：驱动修复与依赖跟进 |
| v4.19.0 / v4.19.1 | 2025-08-29 / 2025-11 | 常规维护，构建与测试矩阵更新 |
| **v4.20.1** | **2026-09-09** | 当前最新（截至 2026-11）；v4.20.0 的 CI/GoReleaser 发布失败，4.20.1 是立即修复版，命令与 API 无变化 |

升级提示：

- **v4 内部升级风险主要来自驱动**：PG 驱动在 `postgres://` 与 `pgx5://` 之间行为有细微差别（多语句、锁实现），换驱动等于换执行语义，先在测试库跑一遍 `up/down/up`；
- **CLI 的 build tag 必须与 DSN scheme 匹配**：镜像里只编了 `postgres`，却把 DSN 写成 `mysql://`，报的是「unknown driver」而不是连接错误；
- **库版本与 CLI 版本保持一致**：两者共用 `schema_migrations` 语义，混用不同大版本的工具去操作同一个库属于自找麻烦；
- v3 已停止支持，`import` 路径仍是 `github.com/golang-migrate/migrate/v4`，没有 v5。

---

## 6. 常见坑与最佳实践

1. **down 写成「删表」当成回滚**：`DROP TABLE users` 在 down 里执行成功、退出码 0，数据没了。down 的职责是「撤销 up 的结构变更」，涉及删除的 up 必须显式标注不可逆，并把 down 限制在开发环境使用。
2. **dirty 不看就 force**：`force` 只改版本表。正确顺序永远是「查 version → 连库看真实状态 → 手工补/退 → force 到真实版本」，目标是**真实已生效的版本**，不是失败的那一版，也不是下一版。直接 `UPDATE schema_migrations SET dirty=false` 更糟：它拆掉了唯一的刹车，把「已知的不确定」变成「未知的错误」。
3. **多人同时提 PR 用同一个 `-seq` 号**：两个分支各自生成 `000007_*`，合并后同版本两个文件，谁先跑谁赢，另一个被静默跳过。→ 多人协作用 `-format 20060102150405`（时间戳），并在 CI 检查「版本号唯一且严格递增」。
4. **改已发布的迁移文件**：没有 checksum，改了也不会报错，结果是各环境 schema 悄悄分叉。→ 已合并到主干的迁移视为**不可变**，修正一律新开一版。
5. **把数据修复塞进 schema 迁移**：`UPDATE ... WHERE` 百万行、或依赖当时的业务状态，重跑一次结果就不同；放在迁移里还会长时间持锁，卡住整个发布。→ 回填走应用侧分批任务或独立的一次性脚本（可重入、有进度、可中断）。
6. **`embed` 路径写错**：`//go:embed migrations/*.sql` + `iofs.New(fs, "migrations")` 是配套的；写成 `iofs.New(fs, ".")` 会找不到文件，写成 `embed migrations`（无通配符）会漏掉子目录里的文件（需要 `all:migrations`）。启动时立刻报错，别等到首次发布才发现。
7. **多副本同时启动各跑一次迁移**：有 advisory lock 时只有一个真正执行，其余阻塞等待——但等待是**无限期**的，探针先超时；经过 transaction-mode pooler 时锁还可能失效。→ 迁移做成独立 Job，应用启动只校验版本是否满足最低要求。
8. **在启动路径上跑大表 DDL**：`ALTER TABLE ... ADD COLUMN NOT NULL DEFAULT` 在 PG 11 之前会重写全表，几分钟内启动失败、滚动发布卡死。→ 大表变更单独走运维窗口，迁移 Job 的 `activeDeadlineSeconds` 要按最坏耗时设。
9. **`CREATE INDEX CONCURRENTLY` 和多条语句同文件**：默认整文件一条 query → 隐式事务 → 报 `cannot run inside a transaction block`，并且把版本打脏。→ 独占一个文件；确实要同文件就开 `x-multi-statement=true` 并接受失去原子性。
10. **版本号不是整数**：`v1_init.sql`、`0001.5_fix.sql`、`2026-09-01_x.sql` 都无法解析。→ 只用纯数字前缀，时间戳不要带分隔符。
11. **MySQL 忘了 `multiStatements=true`**：整个文件被当成一条语句，报语法错误；有人误以为「工具只执行了第一条」。→ MySQL 上一个文件一条 DDL，或明确开多语句并接受部分成功。
12. **`-path` 依赖当前工作目录**：本地 `go run ./cmd/migrate` 能跑，容器里 `WORKDIR` 变了就 `file does not exist`。→ 一律 `embed` + `iofs`，或把路径解析成绝对路径。
13. **两套迁移工具并存**：一半迁移在 `goose_db_version`，一半在 `schema_migrations`，谁都不知道库到底到哪一版。→ 一个库只留一套版本表；从 goose 迁过来时，用 `force` 对齐当前版本后**删掉**旧表，写进 runbook。
14. **把 `down` 当成线上回滚方案**：发布出问题时先 `down 1`，结果 down 本身有 bug，故障扩大。→ 线上回滚优先「回滚应用版本 + 前向修复迁移」；`down` 只在发布前验证。
15. **不测 `up → down → up`**：down 少写一句 `DROP INDEX`，第二次 up 直接失败，而这个问题在本地从没暴露过。→ 把它做成 CI 的必过项，跑在空库 + 与生产同版本的服务端上。

---

## 7. 选型对比

| 维度 | golang-migrate | goose | Atlas | Flyway | GORM AutoMigrate |
|---|---|---|---|---|---|
| 定位 | 版本化 SQL 执行器 | 版本化迁移 + Go migration | schema 管理平台（声明式 + 版本式） | 企业级迁移工具 | ORM 附带的开发期同步 |
| 迁移载体 | `.up/.down.sql` | SQL 或 `.go` 文件 | HCL/SQL，可生成 | SQL（Java 生态） | Go struct |
| 版本号 | 整数前缀（序号或时间戳） | 序号或时间戳 | 时间戳目录 + `atlas.sum` 校验 | `V1__x.sql` 约定 | 无版本概念 |
| 变更检测 | 无 | 无 | **声明式 diff + lint** | 无 | 反射 struct 差异 |
| 事务 | 不自动包 | **默认包**，可 `NO TRANSACTION` | 按语句/文件策略 | 每迁移一事务（可关） | 由 ORM 决定 |
| 并发锁 | PG advisory / MySQL `GET_LOCK` | 有（含表级 Locker） | 有 | 有 | 无 |
| 回滚 | 执行 down（不校验可逆性） | 执行 down | 生成 down，可校验 | 支持 undo（社区版受限） | 不支持删除/收窄 |
| 校验和 | **无** | 无 | **有（`atlas.sum`）** | 有 | 不适用 |
| 许可 | MIT | MIT | Apache-2.0（部分能力商业） | Apache-2.0（社区版功能受限） | MIT |

分工建议，一句话版：**migrate 管「版本与执行」，sqlc 管「从 schema 生成类型安全代码」，goose 是 migrate 的替代品而不是补充，Atlas 负责「diff 与治理」，AutoMigrate 只配开发环境。**

- **和 sqlc 的分工**：sqlc 把 `db/migrations` 当 catalog 读，**只读不写**；它不会执行迁移，也不会改版本表。所以 CI 顺序必须是「迁移目录变更 → 空库 `up` → `sqlc generate` → `git diff --exit-code`」，否则生成代码会基于一份不存在的 schema。
- **和 goose 的分工**：两者解决同一个问题，选一个即可。需要 Go 里写迁移（复杂数据变换、调用外部服务）、需要文件级 `NO TRANSACTION` 标注、想要默认事务保护 → goose（[cobra-sqlc 篇](../cobra-sqlc/cobra-sqlc-guide.md) 里就是这套）；团队 SQL 优先、要 CLI 与语言无关、要 S3/GitHub 远程源 → migrate。
- **和 Atlas 的关系**：Atlas 能直接消费 golang-migrate 目录（`--dir-format golang-migrate`），常见组合是「Atlas 做 lint/diff 生成 SQL，migrate 负责在各环境执行」；如果已经痛在「手写 down 不可靠」，该换的是 Atlas，而不是给 migrate 加包装。
- **和 GORM AutoMigrate 的边界**：AutoMigrate 只加不减，删列/改类型/收窄长度都不做，也没有版本记录与回滚。开发期图快可以用，只要一旦有第二个环境，就必须切到版本化迁移——两套同时开着，schema 会被两边拉扯。

参考：[golang-migrate 仓库与 FAQ](https://github.com/golang-migrate/migrate)、[GETTING_STARTED](https://github.com/golang-migrate/migrate/blob/master/GETTING_STARTED.md)、[database/postgres 驱动说明](https://github.com/golang-migrate/migrate/tree/master/database/postgres)、[PostgreSQL 事务性 DDL](https://www.postgresql.org/docs/current/transaction-iso.html)。
