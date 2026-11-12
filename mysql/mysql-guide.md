# MySQL 技术介绍与使用指南（Go 后端视角）

> 后端最核心的关系型数据库：B+ 树索引、MVCC 与事务隔离、锁机制、执行计划与主从复制——从 Go 应用视角讲清「为什么慢、为什么死锁、为什么读到了旧数据」。
> 上游：<https://github.com/mysql/mysql-server>　|　服务端主线：**8.0 / 8.4 LTS**（整理于 2026-11）　|　Go 驱动：[go-sql-driver/mysql](https://github.com/go-sql-driver/mysql) v1.x
> 协议：服务端 GPLv2（含 FOSS 例外）/ 驱动 MPL-2.0；具体版本以上游最新 release 为准。

---

## 1. 项目简介

[GORM 篇](../gorm/gorm-guide.md)讲的是**怎么用 ORM 写 DAO**，本文讲的是**数据库本身**：一条 SQL 进去之后索引怎么走、事务之间谁看得见谁、锁加在哪、为什么同一个查询昨天 5ms 今天 5s。这两件事的边界要分清——慢查询绝大多数不是 ORM 的问题，是索引、执行计划或事务没设计好。

MySQL 的核心定位：**支持事务的关系型数据库，默认 InnoDB 引擎**，靠 B+ 树索引做点查与范围扫描、靠 MVCC 做读写并发、靠 binlog 做复制与恢复。

- **适用场景**：有明确 schema 的业务主数据、需要 ACID 事务与复杂关联查询、订单/账户/库存这类一致性优先的数据
- **不适合**：高并发日志与时序数据（用 [InfluxDB](../influxdb-v2/influxdb-v2-guide.md) 或 Prometheus）、全文检索与模糊搜索（用 Elasticsearch）、超大宽表 OLAP（用 ClickHouse）、纯 KV 高频读写（用 [go-redis](../redis/redis-guide.md)）、schema 频繁变化的半结构化数据（用 MongoDB）

### 安装与连接

```bash
go get github.com/go-sql-driver/mysql
docker run --rm -d --name mysql-dev -p 3306:3306 \
  -e MYSQL_ROOT_PASSWORD=devpass -e MYSQL_DATABASE=app mysql:8.4
```

### 最小可运行示例

```go
package main

import (
    "context"
    "database/sql"
    "fmt"
    "time"

    _ "github.com/go-sql-driver/mysql"
)

func main() {
    dsn := "root:devpass@tcp(127.0.0.1:3306)/app" +
        "?charset=utf8mb4&parseTime=True&loc=Local&timeout=5s"
    db, err := sql.Open("mysql", dsn)
    if err != nil { panic(err) }
    defer db.Close()

    ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
    defer cancel()
    if err := db.PingContext(ctx); err != nil { panic(err) } // sql.Open 不建连，Ping 才真连

    var version string
    if err := db.QueryRowContext(ctx, "SELECT VERSION()").Scan(&version); err != nil {
        panic(err)
    }
    fmt.Println(version)
}
```

> `sql.Open` **只校验 DSN 格式、不建立连接**；真正建连发生在第一次 `Ping` / `Query`。所以「服务启动成功但一请求就报 connection refused」是没做启动探活。

---

## 2. 功能特性总览

| 能力 | 说明 |
|---|---|
| 存储引擎 | InnoDB（默认，事务/行锁/MVCC）；另有 Memory、Archive 等特殊用途引擎 |
| 事务 | ACID，四种隔离级别（默认 **REPEATABLE READ**），Savepoint，XA |
| 索引 | B+ 树聚簇索引 + 二级索引、联合索引、唯一索引、前缀索引、全文索引、降序索引、不可见索引 |
| 并发控制 | MVCC（undo log + ReadView）、共享/排他锁、意向锁、记录/间隙/临键锁、死锁自动检测 |
| 持久与恢复 | redo log（WAL）、undo log、binlog、两阶段提交、崩溃恢复 |
| 复制 | 基于 binlog 的异步 / 半同步复制、GTID、组复制（Group Replication）、读写分离 |
| SQL 能力 | JOIN、子查询、窗口函数、CTE、JSON 类型与函数、生成列、分区表 |
| 运维 | 在线 DDL、`EXPLAIN` / `EXPLAIN ANALYZE`、`performance_schema`、`sys` schema、慢查询日志 |
| Go 侧 | `database/sql` 连接池、Context 超时传播、预编译语句、驱动错误码（`*mysql.MySQLError`） |

---

## 3. 核心概念与原理

### 3.1 InnoDB 的物理结构

```
表空间 (.ibd)
  └─ 段 (segment：叶子段 / 非叶子段 / 回滚段)
      └─ 区 (extent，1MB = 64 个连续页)
          └─ 页 (page，默认 16KB)  ← 磁盘与内存交换的最小单位
              └─ 行 (record)
```

页是 I/O 基本单位，这解释了两条工程经验：**主键尽量短且单调递增**（二级索引叶子节点存的是主键值，长主键会让所有二级索引膨胀）；**随机主键（UUID）会导致页分裂与碎片**，写放大明显。

### 3.2 B+ 树索引：聚簇与回表

```
聚簇索引（主键）              二级索引 idx_name
      [P1|P2|P3]                  [P1|P2|P3]
       /   |   \                   /  |  \
  叶子页(整行数据)            叶子页(name, 主键ID)
                                    │
                                    └──► 拿主键回聚簇索引取其余列 = 回表
```

- **聚簇索引的叶子节点就是整行数据**，所以主键查询最快；
- 二级索引叶子只存「索引列 + 主键」，查非索引列必须**回表**，回表是随机 I/O，代价可能比扫描还高；
- 若查询所需列都在索引里 → **覆盖索引**，直接返回，`EXPLAIN` 显示 `Using index`；
- **最左前缀原则**：联合索引 `(a, b, c)` 能用于 `a`、`a,b`、`a,b,c`，不能跳过 `a` 直接用 `b,c`；范围查询之后的列不再用于定位；
- **索引下推（ICP）**：5.6 起把部分 `WHERE` 条件下推到存储引擎层过滤，减少回表次数（`Using index condition`）。

### 3.3 MVCC：读不加锁是怎么做到的

每行有隐藏列 `trx_id`（最后修改的事务 ID）与 `roll_pointer`（指向 undo log）。修改时**把旧版本写进 undo log 并串成版本链**；读事务启动时生成 **ReadView**（记录当前活跃事务集合），沿版本链找到第一个对自己可见的版本。

| 隔离级别 | ReadView 生成时机 | 现象 |
|---|---|---|
| READ UNCOMMITTED | 不生成，直接读最新 | 脏读 |
| READ COMMITTED | **每条 SELECT 重新生成** | 不可重复读 |
| **REPEATABLE READ**（默认） | **事务内第一次 SELECT 生成一次** | 同一事务多次读一致；靠间隙锁抑制幻读 |
| SERIALIZABLE | 读也加锁 | 无并发 |

> 关键差异：RC 每次语句新建 ReadView，RR 整个事务共用一个。这就是「同一个事务里两次读，RC 能看到别人刚提交的修改，RR 看不到」的根因。

### 3.4 锁：为什么 RR 更容易死锁

- **记录锁（Record Lock）**：锁单行索引记录
- **间隙锁（Gap Lock）**：锁索引记录之间的「空隙」，防止插入 → 抑制幻读
- **临键锁（Next-Key Lock）**：记录锁 + 间隙锁，**RR 下的默认行锁**

```sql
-- RR 下，若 age 无索引，这条 UPDATE 会退化成锁全表所有记录与间隙
UPDATE users SET status = 1 WHERE age > 20;
```

间隙锁之间**不互斥**，但「间隙锁 + 插入意向锁」冲突，因此两个事务交叉更新时极易死锁。InnoDB 会自动检测并回滚代价较小的事务（错误码 `1213`），应用侧必须能重试。

### 3.5 redo / undo / binlog 与两阶段提交

| 日志 | 归属 | 作用 |
|---|---|---|
| redo log | InnoDB | 物理日志，WAL，保证崩溃后已提交事务不丢（**crash-safe**） |
| undo log | InnoDB | 逻辑日志，版本链（MVCC）与事务回滚 |
| binlog | Server 层 | 逻辑日志，主从复制与按时间点恢复 |

一次 `COMMIT` 走**两阶段提交**：redo prepare → 写 binlog → redo commit。任何一步崩溃都能靠「redo 已 prepare 且 binlog 完整则提交，否则回滚」保证主库与从库数据一致。

### 3.6 主从复制与延迟

```
主库: 写 binlog ──► dump thread ──► 从库 IO thread ──► relay log ──► SQL thread 重放
```

默认**异步复制**：主库提交不等从库，因此从库可能落后几十毫秒到数秒。「写主库立刻读从库读到旧数据」是经典事故。解法按代价排序：关键读走主库 → 会话级「写后一段时间内读主」 → 半同步复制 → 等 GTID（`WAIT_FOR_EXECUTED_GTID_SET`）。

### 3.7 Go 侧连接池

`database/sql` 的 `DB` 是**连接池**，不是单连接，并发安全、应全局复用。四个参数与 MySQL 的 `max_connections`、`wait_timeout` 必须配套：

| Go 参数 | 含义 | 常见坑 |
|---|---|---|
| `SetMaxOpenConns` | 最大打开连接数 | 超过 MySQL `max_connections` 会报 `Too many connections`；多实例时要按总副本数分摊 |
| `SetMaxIdleConns` | 空闲连接数 | 设太小会频繁建连，设太大浪费服务端资源 |
| `SetConnMaxLifetime` | 连接最长存活 | 应**小于** MySQL `wait_timeout` 或代理（如 ProxySQL）的空闲超时，否则拿到已断的连接 |
| `SetConnMaxIdleTime` | 空闲多久回收 | 防止长空闲连接被中间件静默断开 |

---

## 4. 使用实战

### 4.1 生产级初始化

```go
package db

import (
    "database/sql"
    "time"

    _ "github.com/go-sql-driver/mysql"
)

func Open(dsn string) (*sql.DB, error) {
    db, err := sql.Open("mysql", dsn)
    if err != nil { return nil, err }
    db.SetMaxOpenConns(50)
    db.SetMaxIdleConns(20)
    db.SetConnMaxLifetime(30 * time.Minute)
    db.SetConnMaxIdleTime(5 * time.Minute)
    return db, db.Ping()
}
```

DSN 关键参数：

```
user:pass@tcp(host:3306)/db
  ?charset=utf8mb4          # 一定要 utf8mb4，否则 emoji / 4 字节字符写不进
  &parseTime=True           # 否则 DATETIME/TIMESTAMP 只能 Scan 成 []byte
  &loc=Local                # 必须和 Go 进程时区一致，否则时间差 8 小时
  &timeout=5s               # 建连超时
  &readTimeout=5s           # 读超时
  &writeTimeout=5s          # 写超时
  &interpolateParams=true   # 客户端预拼参数，省一次 prepare 往返（注意仍会转义）
```

### 4.2 参数化查询（唯一的防注入写法）

```go
// 正确：占位符
row := db.QueryRowContext(ctx,
    "SELECT id, name, created_at FROM users WHERE email = ?", email)

// 错误：字符串拼接 —— 注入入口
// row := db.QueryRowContext(ctx, "SELECT ... WHERE email = '"+email+"'")
```

> 表名、列名、排序方向**不能**用占位符，只能白名单校验后拼接。动态条件用 `sqlx.In` 或自己拼 `?` 个数。

### 4.3 事务：模板与常见错误

```go
func Transfer(ctx context.Context, db *sql.DB, from, to int64, amount int64) error {
    tx, err := db.BeginTx(ctx, &sql.TxOptions{Isolation: sql.LevelReadCommitted})
    if err != nil { return err }
    defer tx.Rollback() // Commit 之后 Rollback 返回 ErrTxDone，可安全忽略

    var balance int64
    if err := tx.QueryRowContext(ctx,
        "SELECT balance FROM accounts WHERE id = ? FOR UPDATE", from,
    ).Scan(&balance); err != nil { return err }
    if balance < amount { return errors.New("insufficient balance") }

    if _, err := tx.ExecContext(ctx,
        "UPDATE accounts SET balance = balance - ? WHERE id = ?", amount, from,
    ); err != nil { return err }
    if _, err := tx.ExecContext(ctx,
        "UPDATE accounts SET balance = balance + ? WHERE id = ?", amount, to,
    ); err != nil { return err }

    return tx.Commit()
}
```

三条铁律：**所有操作都用 `tx.` 而不是 `db.`**（混用会从池里另取连接，既不在事务内又容易与 `FOR UPDATE` 死锁）；**事务里不放 RPC / 文件 IO**（长事务撑大 undo、拉长锁持有时间）；**`defer Rollback()` 紧跟 `BeginTx`**。

### 4.4 死锁 / 可重试错误的处理

```go
func WithRetry(ctx context.Context, max int, fn func() error) error {
    var err error
    for i := 0; i < max; i++ {
        err = fn()
        if err == nil { return nil }
        var me *mysql.MySQLError
        if !(errors.As(err, &me) && (me.Number == 1213 || me.Number == 1205)) {
            return err // 1213 死锁 / 1205 锁等待超时，其余不重试
        }
        time.Sleep(time.Duration(i+1) * 20 * time.Millisecond) // 加抖动
    }
    return err
}
```

> 重试的前提是**业务幂等**（或整个事务可安全重放）。扣款、发券这类副作用操作必须带业务流水号 + 唯一索引兜底。

### 4.5 用 EXPLAIN 定位慢查询

```sql
EXPLAIN SELECT id, name FROM users WHERE email = 'a@b.com';
```

重点看四列：

| 列 | 好 | 坏 |
|---|---|---|
| `type` | `const` / `eq_ref` / `ref` / `range` | `index`（全索引扫）→ `ALL`（全表扫） |
| `key` | 命中预期索引 | `NULL` |
| `rows` | 接近实际返回行数 | 远大于返回行数 |
| `Extra` | `Using index`（覆盖） | `Using filesort` / `Using temporary` / `Using where; Using join buffer` |

```sql
-- 深分页优化：先查主键再回表，避免 OFFSET 扫描前 100000 行
SELECT u.* FROM users u
JOIN (SELECT id FROM users WHERE status = 1 ORDER BY id LIMIT 100000, 20) t
  ON u.id = t.id;

-- 更推荐游标式（记住上一页最大 id）
SELECT * FROM users WHERE status = 1 AND id > ? ORDER BY id LIMIT 20;
```

### 4.6 批量写入与迁移

```go
func BulkInsert(ctx context.Context, db *sql.DB, rows []User) error {
    const batch = 500
    for start := 0; start < len(rows); start += batch {
        end := min(start+batch, len(rows))
        q := "INSERT INTO users (name, email) VALUES "
        args := make([]any, 0, (end-start)*2)
        for i := start; i < end; i++ {
            if i > start { q += "," }
            q += "(?,?)"
            args = append(args, rows[i].Name, rows[i].Email)
        }
        if _, err := db.ExecContext(ctx, q, args...); err != nil { return err }
    }
    return nil
}
```

> 单条 INSERT 循环是最常见的性能反模式。批量大小控制在 500~1000，避免超过 `max_allowed_packet` 与单事务过大。表结构变更用 `golang-migrate` / `goose` 管理版本化迁移；大表加字段用 `gh-ost` / `pt-online-schema-change` 在线变更，不要直接 `ALTER TABLE` 锁表。

---

## 5. 版本演进速览

| 版本 | 关键变化与迁移注意点 |
|---|---|
| 5.7 | 广泛使用的旧主线；默认 `utf8`（实为 utf8mb3）、查询缓存仍存在；**已停止官方支持** |
| **8.0** | 默认字符集改 `utf8mb4`；默认认证插件改 `caching_sha2_password`（**老驱动连不上**）；**移除查询缓存**；引入窗口函数、CTE、降序索引、不可见索引、原子 DDL、JSON 增强、角色管理 |
| **8.4 LTS** | 长期支持版本，收紧废弃参数、调整 InnoDB 默认值与复制相关术语；适合新项目与 5.7 迁移终点 |
| 9.x（创新版） | 快速迭代分支，新特性先行，**不建议生产长期跟踪**；生产选 LTS |

**Go 驱动侧**：v1.x 长期稳定；8.0 之后必须确认驱动版本支持 `caching_sha2_password`（较新的 v1.x 均已支持），否则报 `Authentication plugin 'caching_sha2_password' cannot be loaded`。`allowNativePasswords=false` 与 TLS 配置在多版本间行为有差异，升级前读 CHANGELOG。

> 服务端版本比 Go 库更容易漂移：文首固定「服务端 + 驱动」两个版本，压测与结论才有意义。

---

## 6. 常见坑与最佳实践

1. **`sql.Open` 不当建连**：不做 `Ping` 探活，服务启动「成功」但首个请求全失败。
2. **索引失效**：对索引列用函数/运算（`WHERE DATE(created_at)=?`）、隐式类型转换（字符串列传数字）、前导模糊 `LIKE '%x'`、`OR` 混用未索引列、联合索引不满足最左前缀。一律用 `EXPLAIN` 验证，不要凭感觉。
3. **`SELECT *`**：无法走覆盖索引、多回表、网络传输放大，且表加字段后业务代码可能意外依赖新列。明确列清单。
4. **深分页 `LIMIT 100000, 20`**：MySQL 要先扫描并丢弃前 10 万行。改游标分页或延迟关联。
5. **长事务**：事务里做 HTTP 调用、等用户输入、大批量循环不提交 → undo log 膨胀、锁长期不释放、主从延迟。把事务缩到纯 DB 操作。
6. **RR 下间隙锁引发死锁**：`WHERE` 条件没走索引会锁大量间隙；并发更新多行时**按固定主键顺序**更新；捕获 1213 并重试。
7. **混用 `db.` 与 `tx.`**：代码看似在事务里，实际部分操作另取连接，破坏原子性甚至自锁。
8. **DSN 漏 `parseTime` / `loc`**：时间字段 Scan 成 `[]byte` 报错，或时间整体偏移 8 小时。
9. **连接池参数与服务端不匹配**：`MaxOpenConns × 副本数 > max_connections`；`ConnMaxLifetime > wait_timeout` 导致偶发 `invalid connection`。
10. **所有查询不带超时**：慢 SQL 打满连接池 → 整个服务雪崩。所有 DB 调用走 `Context` 并设置合理 deadline。
11. **主从延迟下读从库**：写后立刻读拿到旧值。关键读走主库，或用 [GORM 篇](../gorm/gorm-guide.md) 的 `dbresolver` 按会话强制主库。
12. **动态拼 SQL**：字符串拼接是注入入口；占位符只覆盖「值」，表名/列名/排序方向走白名单。
13. **唯一索引与 NULL**：MySQL 唯一索引允许多个 NULL，「唯一约束」因此可能失效；用 `NOT NULL DEFAULT ''` 或生成列兜底。
14. **大表直接 DDL / 无备份改数据**：`ALTER TABLE`、无 `WHERE` 的 `UPDATE` 是生产事故高发区。先备份、先在从库演练、用在线变更工具。
15. **把 MySQL 当队列 / 缓存 / 搜索引擎**：轮询 `SELECT ... WHERE status=0` 做任务队列会锁竞争（用 [asynq](../asynq/asynq技术文档与使用手册.md)）；高频 KV 用 [go-redis](../redis/redis-guide.md)；模糊搜索用 Elasticsearch。

---

## 7. 选型对比

| 维度 | MySQL 8.x | PostgreSQL | SQLite | TiDB / OceanBase | ClickHouse | MongoDB |
|---|---|---|---|---|---|---|
| 定位 | 通用 OLTP | 通用 + 扩展型 OLTP | 嵌入式单机 | 分布式 NewSQL | 列存 OLAP | 文档型 |
| 事务 | ACID（RR 默认） | ACID（RC 默认） | ACID（单写） | 分布式事务 | 弱（无多行事务） | 多文档事务 |
| 索引 | B+ 树聚簇 | B-tree / GIN / GiST / BRIN | B-tree | 分布式 KV + 索引 | 稀疏主键 + 跳数 | B-tree / 文本 / 地理 |
| JSON | 有，能用但非强项 | **jsonb 强项** | 有限 | 有 | 弱 | **原生** |
| 水平扩展 | 需分库分表 / 中间件 | 需分区 / Citus | 不适用 | **原生** | 原生分布式 | 原生分片 |
| 运维成本 | 低（生态最成熟） | 中 | 极低 | 高 | 中高 | 中 |
| 典型场景 | 业务主数据、订单账户 | 复杂查询、GIS、分析混合 | 本地工具、测试 | 超大规模 OLTP | 日志/行为分析 | 半结构化、快速迭代 |

一句话决策：

- **默认选 MySQL**：事务型业务主数据、团队熟悉度最高、云厂商与中间件生态最全；新项目直接上 8.4 LTS。
- **复杂查询 / JSON 重度 / GIS / 需要丰富索引类型** → PostgreSQL；注意它的默认隔离级别是 RC，从 MySQL 迁移时要复核并发假设。
- **单机工具、CLI、单元测试** → SQLite，零运维。
- **单表数据量到了分库分表维护不动的程度** → 先评估是否只是索引和历史归档没做好；确需分布式再上 TiDB / OceanBase，别过早引入。
- **分析型大宽表、日志聚合** → ClickHouse；**schema 频繁变化、文档嵌套** → MongoDB；**高频 KV 缓存** → [go-redis](../redis/redis-guide.md)。

> 最后一条经验：多数「MySQL 撑不住」的案例，根因是缺失索引、长事务或把分析查询跑在主库上，而不是该换数据库。先 `EXPLAIN`、先看慢查询日志，再谈选型。
