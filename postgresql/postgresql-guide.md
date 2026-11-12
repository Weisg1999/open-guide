# PostgreSQL 技术介绍与使用指南（Go 后端视角）

> 功能最完整的开源关系型数据库：MVCC 与快照隔离、B-tree / GIN / GiST / BRIN 索引、`jsonb`、CTE 与窗口函数、WAL 逻辑复制——从 Go 应用视角讲清「为什么它和 MySQL 的并发假设不一样」。
> 上游：<https://github.com/postgres/postgres>　|　服务端主线：**16 / 17 / 18**（整理于 2026-11）　|　Go 驱动：[jackc/pgx](https://github.com/jackc/pgx) v5
> 协议：PostgreSQL License；具体版本以上游最新 release 为准。

---

## 1. 项目简介

[MySQL 篇](../mysql/mysql-guide.md) 是行锁 + undo log + 默认 `REPEATABLE READ`；PostgreSQL 是**元组多版本 + VACUUM + 默认 `READ COMMITTED`**。这个差异不是配置细节，而是并发模型的根本不同——从 MySQL 迁过来的代码，如果依赖「同一事务里两次读结果一致」，在 PG 上会直接出问题。

PG 的定位是「**能扛复杂查询的关系型数据库**」：标准 SQL 支持最完整、索引类型最丰富、`jsonb` 让半结构化数据不必另起一个库、扩展机制（PostGIS / pg_trgm / TimescaleDB）覆盖面极广。

- **适用场景**：复杂关联与报表查询、需要 `jsonb` / 数组 / 范围类型、GIS、全文检索、作为 [TimescaleDB 类时序方案](https://github.com/timescale/timescaledb) 的底座、强 SQL 标准的系统
- **不适合**：极简高频点查且不需要复杂查询（MySQL 运维更轻）、纯 KV（用 [go-redis](../redis/redis-guide.md)）、超大规模 OLAP（用 [ClickHouse](../clickhouse/clickhouse-guide.md)）、需要成熟分库分表中间件生态的场景

> 与 [GORM 篇](../gorm/gorm-guide.md) 的关系：GORM 通过 `gorm.io/driver/postgres` 同样能操作 PG，但本文讲**数据库本身**与原生 `pgx` 驱动，两边不重复。

### 安装与本地起服务

```bash
go get github.com/jackc/pgx/v5/pgxpool
docker run --rm -d --name pg-dev -p 5432:5432 \
  -e POSTGRES_PASSWORD=devpass -e POSTGRES_DB=app postgres:17
```

### 最小可运行示例

```go
package main

import (
    "context"
    "fmt"

    "github.com/jackc/pgx/v5/pgxpool"
)

func main() {
    ctx := context.Background()
    pool, err := pgxpool.New(ctx,
        "postgres://postgres:devpass@localhost:5432/app?pool_max_conns=20")
    if err != nil { panic(err) }
    defer pool.Close()
    if err := pool.Ping(ctx); err != nil { panic(err) }

    // 注意：PG 用 $1 占位符，不是 MySQL 的 ?
    if _, err := pool.Exec(ctx, `
        CREATE TABLE IF NOT EXISTS users (
            id BIGSERIAL PRIMARY KEY,
            email TEXT NOT NULL UNIQUE,
            profile JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )`); err != nil { panic(err) }

    if _, err := pool.Exec(ctx,
        `INSERT INTO users (email, profile) VALUES ($1, $2)
         ON CONFLICT (email) DO NOTHING`,
        "alice@example.com", `{"city":"Shenzhen"}`); err != nil { panic(err) }

    var email string
    if err := pool.QueryRow(ctx,
        `SELECT email FROM users WHERE email = $1`, "alice@example.com",
    ).Scan(&email); err != nil { panic(err) }
    fmt.Println(email)
}
```

---

## 2. 功能特性总览

| 能力 | 说明 |
|---|---|
| 并发控制 | **MVCC 元组多版本**（`xmin` / `xmax`）、VACUUM 回收、HOT 更新、事务 ID 回卷防护 |
| 隔离级别 | 默认 **READ COMMITTED**；REPEATABLE READ 为快照隔离；SERIALIZABLE 用 **SSI**（可能报 `40001` 需重试） |
| 索引 | B-tree（默认）、**GIN**（jsonb / 数组 / 全文）、**GiST**（地理 / 范围）、**BRIN**（大表时序）、SP-GiST、Hash；部分/表达式/覆盖索引 |
| 数据类型 | `jsonb`、数组、`hstore`、`range` / `multirange`、`uuid`、`inet`、`enum`、自定义类型、域 |
| SQL 能力 | CTE（含递归）、窗口函数、`MERGE`、`DISTINCT ON`、`LATERAL`、`FILTER`、并行查询、表函数 |
| 分区 | 声明式 RANGE / LIST / HASH 分区、分区裁剪、默认分区 |
| 复制与恢复 | WAL 预写日志、流复制（物理）、**逻辑复制**（publication / subscription）、PITR 时间点恢复 |
| 扩展 | PostGIS、`pg_trgm`（模糊匹配）、`pg_stat_statements`、TimescaleDB、`uuid-ossp` |
| 全文检索 | `tsvector` / `tsquery` + GIN，可满足中等规模检索需求 |
| Go 驱动 | `pgx/v5` 原生接口、`pgxpool` 连接池、`CopyFrom` 批量写、`pgtype` 类型系统、`LISTEN/NOTIFY` |

---

## 3. 核心概念与原理

### 3.1 进程模型：连接很贵

```
postmaster（主进程）
   ├─ backend process 1  ← 每个客户端连接一个独立 OS 进程
   ├─ backend process 2     （MySQL 是线程模型，连接便宜得多）
   └─ 后台进程：checkpointer / walwriter / autovacuum / bgwriter
```

**一个连接 = 一个进程 ≈ 数 MB 内存**。所以 PG 不能像 MySQL 那样开几百个空闲连接，必须用连接池：`pgxpool`（应用内）或 **PgBouncer**（独立代理，transaction pooling 模式）。这是 PG 部署与 MySQL 最大的运维差异。

### 3.2 MVCC：元组多版本与 VACUUM

```
UPDATE users SET email='new' WHERE id=1;

旧元组: (xmin=100, xmax=200, email='old')   ← 对事务 200 之后不可见，但物理还在
新元组: (xmin=200, xmax=0,   email='new')
```

- **每次 UPDATE 都插入新版本**，旧版本靠 `xmax` 标记，不进 undo log（与 MySQL 相反）
- 旧元组由 **VACUUM** 回收；`autovacuum` 自动触发，但**长事务会阻止它清理**，导致表膨胀
- 只改非索引列且页内有空间时可走 **HOT update**，避免更新所有索引
- 事务 ID 是 32 位，需要 **freeze** 防止回卷（wraparound）；autovacuum 卡住是严重的运维故障

> 工程含义：PG 里「频繁更新同一行」比 MySQL 更容易产生膨胀，需要关注 `pg_stat_user_tables.n_dead_tup` 与 autovacuum 是否跟得上。

### 3.3 隔离级别：与 MySQL 的关键差异

| | PostgreSQL（默认 **RC**） | MySQL InnoDB（默认 **RR**） |
|---|---|---|
| 读一致性 | 每条语句新快照 | 事务内第一次读建立 ReadView |
| 不可重复读 | **会发生** | 不会发生 |
| 幻读 / 间隙锁 | RR 用快照隔离，无间隙锁 | RR 用临键锁抑制幻读 |
| 可串行化 | **SSI**，冲突时抛 `40001 serialization_failure` | SERIALIZABLE 退化成加锁读 |

```go
// 需要事务内一致读时，显式指定 RepeatableRead
tx, err := pool.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.RepeatableRead})
```

> **迁移第一检查项**：把 MySQL 的 RR 假设直接带到 PG 的 RC，会出现「同一事务两次读不一致」。要么显式提升隔离级别，要么把读合并成一条语句 / 用 `SELECT ... FOR UPDATE`。

用 `SERIALIZABLE` 时必须能重试整个事务（`40001` 是**预期内**的错误，不是故障）。

### 3.4 索引：选对类型比加索引更重要

| 索引 | 适用 | 例子 |
|---|---|---|
| B-tree | 等值、范围、排序、最左前缀 | 默认，`CREATE INDEX ON orders (tenant_id, created_at DESC)` |
| **GIN** | 包含性查询：jsonb、数组、全文 | `CREATE INDEX ON users USING GIN (profile jsonb_path_ops)` |
| **GiST** | 几何、范围、最近邻、模糊匹配 | PostGIS 地理位置、`pg_trgm` |
| **BRIN** | 超大表且物理顺序与查询列相关（时序） | 按时间追加的日志表，索引极小 |
| 部分索引 | 只索引关心的子集 | `WHERE status = 'active'` |
| 表达式索引 | 函数条件 | `LOWER(email)` |
| 覆盖索引 | 避免回表 | `INCLUDE (name, email)` |

`jsonb` 有两种 GIN 操作符类：默认 `jsonb_ops` 支持 `?` / `?|` / `@>` 全算子但索引大；`jsonb_path_ops` 只支持 `@>` 但更小更快。按查询形态选。

### 3.5 WAL 与逻辑复制

```
写请求 ──► 先写 WAL（顺序追加，保证 crash-safe）──► 再改 shared_buffers 中的页
                 │
                 ├─► 流复制：把 WAL 字节流发给备库（整库物理复制）
                 └─► 逻辑复制：解析 WAL 为行级变更，按 publication 发给订阅端（可跨版本 / 选表）
```

逻辑复制是做 **CDC / 异构同步 / 零停机大版本升级** 的基础，但需要建 **replication slot**；订阅端停了槽不消费，**WAL 会一直堆积直到打满磁盘**——这是生产常见事故。

### 3.6 执行计划

```sql
EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT)
SELECT * FROM orders WHERE tenant_id = 42 AND created_at > now() - interval '7 days';
```

重点看：`Seq Scan` vs `Index Scan` vs `Bitmap Heap Scan`、预估行数 `rows` 与实际行数是否量级一致（不一致 → 统计信息过期，跑 `ANALYZE`）、`Buffers: shared hit/read`（缓存命中）、Join 策略（Nested Loop / Hash / Merge）。PG 的类型检查比 MySQL 严格，隐式转换少，因此统计信息更准，但也更容易因参数类型不匹配而报错。

---

## 4. 使用实战

### 4.1 连接池配置

```go
cfg, err := pgxpool.ParseConfig(dsn)
if err != nil { return err }
cfg.MaxConns = 20                        // 必须按 (总连接预算 / 副本数) 反推
cfg.MinConns = 5
cfg.MaxConnLifetime = 30 * time.Minute
cfg.MaxConnIdleTime = 5 * time.Minute
cfg.HealthCheckPeriod = 30 * time.Second
cfg.ConnConfig.RuntimeParams["statement_timeout"] = "5000" // 5s，防慢查询拖死池
cfg.ConnConfig.RuntimeParams["application_name"] = "checkout-api"

pool, err := pgxpool.NewWithConfig(ctx, cfg)
```

> 所有查询传带超时的 `ctx`；`statement_timeout` 是第二道保险。`pgxpool.Pool` 并发安全，全局复用。

### 4.2 查询与扫描

```go
type User struct {
    ID        int64
    Email     string
    City      string
    CreatedAt time.Time
}

rows, err := pool.Query(ctx,
    `SELECT id, email, profile->>'city', created_at
     FROM users WHERE created_at > $1 ORDER BY id LIMIT $2`, since, 100)
if err != nil { return err }
defer rows.Close()

users, err := pgx.CollectRows(rows, func(row pgx.CollectableRow) (User, error) {
    var u User
    err := row.Scan(&u.ID, &u.Email, &u.City, &u.CreatedAt)
    return u, err
})
if err != nil { return err }
```

> `pgx.CollectRows` 会自动处理 `rows.Close()` 与 `rows.Err()`；手写循环时**必须检查 `rows.Err()`**，否则迭代中的错误被吞掉。`profile->>'city'` 返回 text，若为 NULL 会 Scan 失败——用 `COALESCE(profile->>'city','')` 或 `pgtype.Text`。

### 4.3 事务、错误码与可串行化重试

```go
func Transfer(ctx context.Context, pool *pgxpool.Pool, from, to int64, amount int64) error {
    return withRetry(ctx, 3, func() error {
        tx, err := pool.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.Serializable})
        if err != nil { return err }
        defer tx.Rollback(ctx) // Commit 后返回 ErrTxClosed，可忽略

        var balance int64
        if err := tx.QueryRow(ctx,
            `SELECT balance FROM accounts WHERE id = $1 FOR UPDATE`, from,
        ).Scan(&balance); err != nil { return err }
        if balance < amount { return errors.New("insufficient balance") }

        if _, err := tx.Exec(ctx,
            `UPDATE accounts SET balance = balance - $1 WHERE id = $2`, amount, from,
        ); err != nil { return err }
        if _, err := tx.Exec(ctx,
            `UPDATE accounts SET balance = balance + $1 WHERE id = $2`, amount, to,
        ); err != nil { return err }
        return tx.Commit(ctx)
    })
}

func isRetryable(err error) bool {
    var pgErr *pgconn.PgError
    return errors.As(err, &pgErr) &&
        (pgErr.Code == "40001" || pgErr.Code == "40P01") // 序列化失败 / 死锁
}

func withRetry(ctx context.Context, max int, fn func() error) error {
    var err error
    for i := 0; i < max; i++ {
        err = fn()
        if err == nil || !isRetryable(err) { return err }
        select {
        case <-ctx.Done(): return ctx.Err()
        case <-time.After(time.Duration(i+1) * 20 * time.Millisecond):
        }
    }
    return err
}
```

常见 SQLSTATE：`23505` 唯一键冲突、`23503` 外键冲突、`40001` serialization_failure、`40P01` deadlock_detected、`57014` 取消语句（超时）。

### 4.4 批量写入：CopyFrom

```go
_, err := pool.CopyFrom(ctx,
    pgx.Identifier{"events"},
    []string{"ts", "user_id", "event"},
    pgx.CopyFromSlice(len(events), func(i int) ([]any, error) {
        return []any{events[i].Ts, events[i].UserID, events[i].Event}, nil
    }),
)
```

> `CopyFrom` 走 PostgreSQL 的 `COPY` 协议，比循环 INSERT 快一个数量级，是批量导入的正确姿势。与 [ClickHouse 篇](../clickhouse/clickhouse-guide.md) 的 `PrepareBatch` 异曲同工。

### 4.5 jsonb、数组与 LISTEN/NOTIFY

```go
// jsonb 包含查询（需 GIN 索引）
var ids []int64
rows, _ := pool.Query(ctx,
    `SELECT id FROM users WHERE profile @> $1::jsonb`, `{"city":"Shenzhen"}`)

// 数组参数：pgx 会自动把 Go slice 编码为 PG array
rows, _ = pool.Query(ctx, `SELECT id FROM users WHERE id = ANY($1)`, []int64{1, 2, 3})

// LISTEN/NOTIFY：轻量变更通知（不保证可靠投递，关键链路要用逻辑复制或 outbox）
conn, err := pool.Acquire(ctx)
if err != nil { return err }
defer conn.Release()
if _, err := conn.Exec(ctx, "LISTEN order_events"); err != nil { return err }
notif, err := conn.Conn().WaitForNotification(ctx)
```

### 4.6 运维速查

```sql
SELECT * FROM pg_stat_activity WHERE state <> 'idle';       -- 长事务 / 阻塞
SELECT relname, n_dead_tup, last_autovacuum FROM pg_stat_user_tables
  ORDER BY n_dead_tup DESC LIMIT 10;                       -- 膨胀与 VACUUM
SELECT * FROM pg_stat_statements ORDER BY total_exec_time DESC LIMIT 10;  -- 慢 SQL
SELECT slot_name, pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn))
  FROM pg_replication_slots;                              -- 复制槽 WAL 堆积
```

```bash
pg_dump -Fc app > app.dump        # 逻辑备份
pg_restore -d app app.dump
```

---

## 5. 版本演进速览

| 版本 | 关键变化与迁移注意点 |
|---|---|
| 9.4 / 9.6 | `jsonb` 落地、并行查询起步 |
| **10** | **声明式分区**、逻辑复制（publication/subscription）、身份列 `GENERATED ... AS IDENTITY` |
| 11 / 12 | 分区增强（默认分区、外键）、JIT 编译、CTE 内联优化、生成列 |
| 13 / 14 | 并行 VACUUM、增量排序、`jsonb` 下标语法、逻辑复制支持大事务 |
| 15 | `MERGE`、逻辑复制行过滤/列过滤、`security_invoker` 视图 |
| 16 | 逻辑复制可从备库、并行 FULL/RIGHT JOIN、`pg_stat_io` |
| 17 | VACUUM 内存管理改进、增量备份、逻辑复制故障转移槽 |
| 18 | 异步 I/O、跳过扫描（skip scan）等性能优化 |

**迁移注意**：

- 大版本升级用 `pg_upgrade`（或逻辑复制做近零停机），**不能跨多版本直接跳**，先读目标版本的 release notes
- `pgx` **v4 → v5** 是破坏性升级：`pgxpool`、`pgtype` 与 `QueryRow` 的错误处理有变化，`database/sql` 兼容接口路径也不同
- `jackc/pgx` 与 `lib/pq` 不同：后者已归档，新项目一律 pgx
- 服务端与驱动版本不必严格一致，但新类型/新语法需要较新的驱动

---

## 6. 常见坑与最佳实践

1. **把 MySQL 的隔离级别假设带过来**：PG 默认 RC，同一事务两次读可能不一致。显式指定 `RepeatableRead`，或让单条 SQL 完成读-改-写。
2. **连接数开太大**：每连接一个进程，`max_connections` 打满直接拒绝服务。用 `pgxpool` 限流 + PgBouncer，按 `总预算 / 副本数` 反推 `MaxConns`。
3. **长事务阻塞 VACUUM**：事务开着不提交（或忘了 `COMMIT`），死元组无法回收 → 表膨胀、XID 回卷风险。监控 `pg_stat_activity` 与 `n_dead_tup`。
4. **用 `?` 占位符**：PG 是 `$1`、`$2`。这是从 MySQL 迁来时最先撞到的语法错误。
5. **`SERIALIZABLE` 不重试 `40001`**：SSI 冲突是预期行为，必须整体重试事务。
6. **标识符大小写**：未加引号的标识符会被折叠成小写；`CREATE TABLE "User"` 之后 `SELECT * FROM user` 查不到。一律用小写 snake_case，不加引号。
7. **隐式类型转换**：PG 比 MySQL 严格，`WHERE varchar_col = 123` 会报错；传参类型要明确（`$1::jsonb`、`$1::int`）。
8. **SELECT \* + 按位置 Scan**：表加列或改列顺序会静默错位。明确列清单，或用 `pgx.RowToStructByName` 按名映射。
9. **NULL 扫描失败**：`profile->>'city'` 为 NULL 时扫进 `string` 会报错。用 `COALESCE` 或 `pgtype.Text` / `*string`。
10. **索引失效 / 统计信息过期**：数据大幅变更后不 `ANALYZE`，优化器选错计划。看 `EXPLAIN (ANALYZE, BUFFERS)` 的预估 vs 实际行数。
11. **`jsonb` 索引选错**：默认 `jsonb_ops` 索引大；只做 `@>` 包含查询时用 `jsonb_path_ops`。
12. **深分页 `OFFSET`**：与 MySQL 同理，改 keyset 分页（`WHERE id > $1 ORDER BY id LIMIT n`）。
13. **时间类型混用**：`TIMESTAMPTZ`（带时区，推荐）vs `TIMESTAMP`（不带）。Go 的 `time.Time` 与 `timestamptz` 搭配最省心，统一 UTC 存储。
14. **复制槽不消费**：逻辑复制订阅端停机，WAL 无限堆积打满磁盘。监控 `pg_replication_slots` 的 lag，必要时删除失效槽。
15. **序列不回滚**：`BIGSERIAL` / `IDENTITY` 的值在事务回滚后不会归还，ID 有空洞属正常，不要依赖连续性。
16. **不设超时**：慢查询占满连接池。用带 deadline 的 `ctx` + `statement_timeout` 双保险。
17. **在库代码里建连接池**：`pgxpool` 由应用入口装配并注入，库函数接收接口，便于测试与替换。

---

## 7. 选型对比

| 维度 | PostgreSQL | MySQL 8.x | SQLite | CockroachDB / YugabyteDB | TimescaleDB | ClickHouse |
|---|---|---|---|---|---|---|
| 定位 | 通用 + 扩展型 OLTP | 通用 OLTP | 嵌入式 | 分布式 SQL | PG 上的时序扩展 | 列存 OLAP |
| 默认隔离 | **READ COMMITTED** | REPEATABLE READ | SERIALIZABLE | SERIALIZABLE | 同 PG | 无事务 |
| JSON | **jsonb + GIN 强项** | 可用但较弱 | 有限 | 有 | 同 PG | 有限 |
| 索引类型 | **最丰富** | B+ 树为主 | B-tree | 分布式索引 | 同 PG + 时序 | 稀疏/跳数 |
| 复杂查询 | **CTE / 窗口 / LATERAL 最全** | 8.0 起补全 | 有限 | 完整 SQL | 完整 SQL | 完整 SQL |
| 水平扩展 | 需分区 / Citus | 需分库分表 | 不适用 | **原生** | 依赖 PG | 原生 |
| 运维成本 | 中 | **低** | 极低 | 高 | 中 | 中高 |
| 典型场景 | 复杂业务、GIS、混合数据 | 常规业务主数据 | 本地工具 / 测试 | 全球多活 | 监控 / IoT 时序 | 分析报表 |

一句话决策：

- **默认推荐 PostgreSQL**：SQL 标准、`jsonb`、索引类型与扩展性最全面，很多「本来要上第二个数据库」的需求（半结构化、GIS、全文、时序）用 PG 扩展就能收进一个库。
- **团队对 MySQL 更熟、以简单 OLTP 为主、追求运维最省** → [MySQL](../mysql/mysql-guide.md)，生态与资料最丰富。
- **单机工具、CLI、单元测试** → SQLite，零运维。
- **需要多region 强一致与原生水平扩展** → CockroachDB / YugabyteDB，但要接受分布式事务的延迟成本。
- **监控与 IoT 时序、且团队已在 PG 栈** → TimescaleDB（补上[时序缺口](https://github.com/timescale/timescaledb)）；**分析型大宽表** → [ClickHouse](../clickhouse/clickhouse-guide.md)。

> 从 MySQL 迁到 PG 时，按这个顺序排查：占位符 → 隔离级别 → 标识符大小写 → 隐式类型转换 → NULL 扫描 → 自增序列语义。**前两条会造成静默的数据正确性问题，后四条只会报错。**
