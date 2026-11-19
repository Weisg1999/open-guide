# MySQL 技术介绍与使用指南（Go 后端视角）

> 后端最核心的关系型数据库：B+ 树索引、MVCC 与事务隔离、锁机制、执行计划与主从复制——从 Go 应用视角讲清「为什么慢、为什么死锁、为什么读到了旧数据」。
> 上游：<https://github.com/mysql/mysql-server>　|　服务端主线：**9.7 LTS / 26.10 Innovation**（8.0 已于 2026-04 EOL；整理于 2026-11）　|　Go 驱动：[go-sql-driver/mysql](https://github.com/go-sql-driver/mysql) v1.x
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

```text
表空间 (.ibd)
  └─ 段 (segment：叶子段 / 非叶子段 / 回滚段)
      └─ 区 (extent，1MB = 64 个连续页)
          └─ 页 (page，默认 16KB)  ← 磁盘与内存交换的最小单位
              └─ 行 (record)
```

页是 I/O 基本单位，这解释了两条工程经验：**主键尽量短且单调递增**（二级索引叶子节点存的是主键值，长主键会让所有二级索引膨胀）；**随机主键（UUID）会导致页分裂与碎片**，写放大明显。

### 3.2 B+ 树索引：聚簇与回表

```text
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

```text
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

### 3.8 索引设计：联合索引的顺序、覆盖与下推

**最左前缀的真实含义**：联合索引 `(a, b, c)` 的 B+ 树先按 `a` 排序、`a` 相同再按 `b`、再按 `c`。跳过 `a` 直接查 `b = ?`，相当于在一张「按 a 分区」的有序表里找一个 b 值——每个 a 分区里都可能有，只能全扫。不是优化器「不想用」，而是**索引结构上无法二分定位**。

**范围列会截断后续列**：

```sql
-- 索引 idx_user_status_time (user_id, status, created_at)
-- ✅ 两个等值列定位，最后一列做范围 + 排序
SELECT id, amount FROM orders
WHERE user_id = 42 AND status = 1 AND created_at >= '2026-01-01 00:00:00'
ORDER BY created_at DESC LIMIT 20;

-- ⚠️ 索引 idx_user_time_status (user_id, created_at, status)
-- user_id 定位 + created_at 范围之后，status 已无法参与定位
SELECT id, amount FROM orders
WHERE user_id = 42 AND created_at >= '2026-01-01 00:00:00' AND status = 1;
```

| 索引 | 同一查询 `user_id=? AND status=? AND created_at>=?` 用到的列 | 结果 |
|---|---|---|
| `(user_id, status, created_at)` | user_id 等值 + status 等值 + created_at 范围 | 三列全用上 |
| `(user_id, created_at, status)` | user_id 等值 + created_at 范围 | status 只能靠 ICP 过滤 |

**索引下推（ICP）**：范围列之后的条件虽然不能定位，但如果它也在索引里，存储引擎可以在**回表之前**先用它过滤，减少回表次数。判断依据是 `EXPLAIN` 的 `Extra` 出现 `Using index condition`；若同时出现 `Using where`，说明 server 层在回表后还要再过滤一遍，不代表没走 ICP。ICP 只在二级索引、且条件下推的列在索引内时生效；聚簇索引没有「回表」这一步，谈不上下推。

**覆盖索引是双刃剑**：把查询列都放进索引可以彻底避免回表（`Using index`），代价是索引变宽——叶子节点多存这些列，写入放大、buffer pool 有效容量下降。适合「高频、列少、稳定」的查询，不适合把 `SELECT *` 硬塞进索引。

**区分度（选择性）怎么算**：

```sql
SELECT
  COUNT(DISTINCT status)   / COUNT(*) AS sel_status,
  COUNT(DISTINCT user_id)  / COUNT(*) AS sel_user,
  COUNT(DISTINCT order_no) / COUNT(*) AS sel_order_no
FROM orders;
```

选择性越接近 1 越适合放索引左侧。`status` 只有几个枚举值，选择性接近 0，**单独建索引几乎没用**，必须与其他列组合；`order_no` 接近 1，适合做唯一索引。

三个真实场景的索引对照：

| 场景 | 典型查询 | 推荐索引 | 关键点 |
|---|---|---|---|
| 订单列表 | `user_id=? AND status=? ORDER BY created_at DESC LIMIT 20` | `(user_id, status, created_at)` | 等值列在前、排序列在后，排序由索引直接提供，避免 `Using filesort` |
| 消息未读红点 | `user_id=? AND is_read=0` 求 `COUNT(*)` | `(user_id, is_read)` | 计数完全走覆盖索引；`is_read` 选择性低，但作为第二列固定值仍然有效 |
| 软删除 + 时间范围 | `deleted_at IS NULL AND created_at > ?` | 优先 `(created_at)` + 历史归档/分区；数据分布特殊时再考虑 `(deleted_at, created_at)` | `deleted_at` 绝大多数为 NULL，放最左等于用索引扫全表；先看真实分布再决定 |

**索引越多写入越慢**：每多一个二级索引，INSERT 就要多维护一棵 B+ 树（随机页写入 + 可能的页分裂），UPDATE 改到索引列时还要删旧项插新项。一张有 8 个二级索引的表，一次插入约等于 9 次 B+ 树修改；索引还占 buffer pool，挤掉真正热的数据页。经验阈值：单表二级索引控制在 5 个以内，新增前先确认能否被更优的联合索引覆盖。

### 3.9 读写分离：读到旧数据的三种处理

3.6 讲了复制链路，这里讲**延迟从哪来**和**应用怎么兜**。延迟主要来自三处：

- **单线程 apply**：5.7+ 支持并行复制，但并行粒度受组提交限制，且**一个大事务只能由一个 worker 回放**——主库跑 10 分钟的批量 UPDATE，从库就得单线程追 10 分钟；
- **从库自身压力**：报表、备份、大查询抢走 CPU / IO，SQL thread 跟不上；
- **主库写入突增**：短时间大量小事务，回放速度追不上提交速度。

**`Seconds_Behind_Master` 为什么不可靠**：它算的是「SQL thread 正在回放的事件时间戳」与「IO thread 已拉到的最新事件时间戳」之差。主库长时间无写入时两个时间戳一起停住，显示 0 但数据并未同步；复制中断时可能返回 `NULL`；主从时钟不一致时直接失真。生产上应该用 **`pt-heartbeat`**：在主库定时写一行带时间戳的心跳，从库读这行与本地时间对比，得到端到端真实延迟。

三种处理策略，按代价从低到高：

1. **强制走主**：一致性要求高的读（支付结果、库存、刚写入的数据）直接读主库。最简单，代价是放弃这部分读扩展。
2. **会话粘滞（read-your-own-writes）**：写完 N 秒内该会话的读都路由到主库，用 Redis / Session 记录「最后写入时间」。适合「提交表单后跳转详情页」，成本低，但窗口要按实测的 P99 延迟设。
3. **半同步 + 等待位点**：半同步只保证「至少一个从库收到 binlog」，**收到 ≠ 回放完**；要读到最新数据还得在从库上等 GTID 执行到位：

```sql
-- 主库提交后取当前已执行的 GTID 集合（形如 3e11fa47-...:1-120）
SELECT @@GLOBAL.gtid_executed;

-- 在从库、同一个连接上阻塞等待；返回 0 = 已追上，1 = 超时
-- timeout 单位秒，0 表示一直等
SELECT WAIT_FOR_EXECUTED_GTID_SET('3e11fa47-...:1-120', 1);
```

> `WAIT_FOR_EXECUTED_GTID_SET` **必须在执行读的那条连接上调用**，连接池换一条连接就等于没等。它会阻塞连接，超时时间要远小于业务超时。

Go 侧用 **context 值 + 路由层**标记「必须读主」：

```go
type readPolicyKey struct{}

const readFromPrimary = "primary"

// 写操作成功后，把「读主」标记打到请求的 context 上
func WithPrimaryRead(ctx context.Context) context.Context {
    return context.WithValue(ctx, readPolicyKey{}, readFromPrimary)
}

func mustReadPrimary(ctx context.Context) bool {
    v, _ := ctx.Value(readPolicyKey{}).(string)
    return v == readFromPrimary
}

type Router struct{ primary, replica *sql.DB }

func (r *Router) reader(ctx context.Context) *sql.DB {
    if mustReadPrimary(ctx) {
        return r.primary
    }
    return r.replica
}
```

> 同请求内的「写后读」用 context 传递最干净；跨请求的粘滞不能只靠 context（请求结束就丢了），要落到 Session / Redis 上带 TTL。别把「读主」做成全局开关——那等于放弃读写分离。

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

```text
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

### 4.7 EXPLAIN 逐列解读与反例

4.5 给了「看哪四列」，这里把每一列的**判读标准**和**常见误判**讲透。

`type` 的优先级（从好到坏）：

```text
system > const > eq_ref > ref > fulltext > ref_or_null > index_merge
       > unique_subquery > index_subquery > range > index > ALL
```

| type | 含义 | 可接受度 |
|---|---|---|
| `const` / `system` | 主键或唯一索引等值命中至多一行 | 最好 |
| `eq_ref` | 关联查询中被驱动表按主键/唯一索引命中一行 | 好 |
| `ref` | 普通二级索引等值命中多行 | 好，OLTP 常态 |
| `range` | 索引范围扫描 | 可接受，重点看 `rows` 规模 |
| `index` | 全索引扫描（走完整棵索引树） | 警戒，通常缺合适的索引 |
| `ALL` | 全表扫描 | 大表上必须消除 |

`key` 与 `key_len`：`key` 只说命中了哪个索引，**`key_len` 才告诉你用了联合索引的哪几列**。按列定义宽度累加即可推算：`INT` 占 4 字节、`INT NULL` 占 5 字节（多 1 字节空标志）、`VARCHAR(64)` utf8mb4 占 `64×4+2+1 = 259` 字节。于是 `(a INT NOT NULL, b INT NULL, c VARCHAR(64) NULL)` 的 key_len：只用 a = 4，用 a,b = 9，三列全用 = 268。它是按**定义长度**算的上限，不是实际字节数。

`rows` 是**估算值**，来自索引统计信息（8.0 默认 index dive，小范围查询会精确采样），不是真实扫描行数；要真实值用 `EXPLAIN ANALYZE`（8.0.18+）或 `EXPLAIN FORMAT=JSON` 中的 `rows_examined_per_scan`。`filtered` 是「经过 WHERE 后剩余行的百分比」，`rows × filtered / 100` 约等于传给下一步的行数；`rows` 大而 `filtered` 低（比如 5%）说明索引只筛掉一点，大部分靠回表后过滤——这是加联合索引的强信号。

`Extra` 关键项：

| Extra | 含义 | 处理方向 |
|---|---|---|
| `Using index` | 覆盖索引，不回表 | 理想状态 |
| `Using index condition` | 索引下推（ICP），回表前先过滤 | 正常且通常有益 |
| `Using where` | 取到行后在 Server 层再过滤 | 结合 `filtered`，低则考虑加索引 |
| `Using filesort` | 无法利用索引顺序，额外排序 | 把排序列加入索引 |
| `Using temporary` | 使用临时表（GROUP BY / DISTINCT / UNION） | 改写或调整索引顺序 |
| `Using MRR` | 多范围读，回表主键排序后批量读 | 通常有益，随机 IO 变顺序 IO |

三个典型反例：

```sql
-- 反例 1：隐式类型转换。phone 是 VARCHAR，传数字会让索引整体失效
SELECT * FROM users WHERE phone = 13800138000;    -- type=ALL
SELECT * FROM users WHERE phone = '13800138000';  -- type=ref

-- 反例 2：函数包裹索引列，索引无法用于定位
SELECT * FROM orders WHERE DATE(created_at) = '2026-11-19';       -- type=ALL
SELECT * FROM orders WHERE created_at >= '2026-11-19 00:00:00'
                       AND created_at <  '2026-11-20 00:00:00';   -- type=range

-- 反例 3：OR 连接了未索引列，整个条件退化为全表扫描
SELECT * FROM users WHERE email = 'a@b.com' OR nickname = 'neo';  -- type=ALL

-- 改写：拆成两段 UNION ALL，各自走各自的索引
SELECT * FROM users WHERE email = 'a@b.com'
UNION ALL
SELECT * FROM users WHERE nickname = 'neo' AND email <> 'a@b.com';
```

> 隐式转换还有一个隐蔽版本：JOIN 两边字段类型不一致（`varchar` 对 `bigint`），被驱动表无法用索引，表现为「单表很快，一 JOIN 就慢」。

### 4.8 在线 DDL：怎么改表不锁业务

`ALTER TABLE` 的代价由两个参数决定：`ALGORITHM`（怎么改）和 `LOCK`（改的时候让不让读写）。

- `ALGORITHM=COPY`：建临时表 → 逐行拷贝 → 改名。全程阻塞写入，大表上等于停服；
- `ALGORITHM=INPLACE`：在**存储引擎层**完成，不经过 Server 层逐行拷贝，通常快得多，但部分操作仍会**重建表**并短暂阻塞写入；
- `LOCK=NONE`：允许并发读写，才是真正「不锁业务」；`LOCK=SHARED` 只允许读，`LOCK=EXCLUSIVE` 读写全禁。

能不能用，先让 MySQL 自己回答——把 `ALGORITHM` / `LOCK` 显式写进语句，不支持会直接报错，而不是偷偷降级：

```sql
ALTER TABLE orders ADD COLUMN remark VARCHAR(255) DEFAULT NULL,
  ALGORITHM=INPLACE, LOCK=NONE;
-- 若报 ERROR 1846 (0A000): ALGORITHM=INPLACE is not supported. Reason: ...
-- 说明该操作只能退到 COPY，需要换 gh-ost / pt-osc 方案
```

四类常见操作的行为矩阵（MySQL 8.0+）：

| 操作 | 默认算法 | 是否重建表 | 能否 `LOCK=NONE` |
|---|---|---|---|
| `ADD COLUMN`（末尾、可空或有默认值） | **INSTANT**（8.0.12+） | 否，只改元数据 | 是，秒级 |
| `ADD COLUMN ... AFTER x` / `DROP COLUMN` | **INSTANT**（8.0.29+） | 否 | 是 |
| `MODIFY COLUMN` 改类型 / 长度 | COPY 或 INPLACE 重建 | 是 | 改类型通常不行 |
| `ADD INDEX` / `DROP INDEX` | INPLACE | 否（在线建索引） | 是 |
| `CONVERT TO CHARACTER SET` | COPY | 是 | 否，全程阻塞写入 |

> INSTANT 加列有边界：列必须可空或带默认值、不能是 `AUTO_INCREMENT`、不能是生成列；8.0.12~8.0.28 只能加在**最后一列**，8.0.29 起支持任意位置。反复 INSTANT 加/删列会让行格式元数据变复杂，最终仍可能触发一次真实重建。

超出在线 DDL 能力时（改字符集、改主键、超大表重建），用外部工具：

```bash
# gh-ost：读 binlog 追增量，不建触发器，可随时暂停 / 按延迟限流
gh-ost \
  --host=127.0.0.1 --port=3306 --user=ghost --password='***' \
  --database=app --table=orders --alter="MODIFY COLUMN remark VARCHAR(512) DEFAULT NULL" \
  --max-lag-millis=1500 --allow-on-master \
  --execute
```

`pt-online-schema-change` 思路类似，但用**触发器**同步增量，对写入有额外开销，且表上已有触发器时会拒绝执行。两者共同代价：**影子表占一倍磁盘**、双写带来额外 IO、cut-over 瞬间需要短暂表锁。

DDL 执行期间的监控项：

```sql
-- 谁在等元数据锁：DDL 本身不可怕，可怕的是它后面排队的业务查询
SELECT OBJECT_SCHEMA, OBJECT_NAME, LOCK_TYPE, LOCK_STATUS, OWNER_THREAD_ID
FROM performance_schema.metadata_locks
WHERE OBJECT_TYPE = 'TABLE' AND OBJECT_SCHEMA = 'app';

-- 复制延迟：DDL 期间从库回放压力会上升
SHOW REPLICA STATUS\G  -- 看 Replica_SQL_Running / Seconds_Behind_Source
```

> 最常见的生产事故不是 DDL 慢，而是 **DDL 等 MDL、业务查询再等 DDL**：一条长事务不提交，DDL 拿不到排他 MDL 就排队，后面所有访问该表的查询全部堵死。执行前先用 `SELECT * FROM information_schema.innodb_trx` 确认没有长事务。

### 4.9 慢查询治理闭环

慢查询治理不是「看到慢就加索引」，而是**采集 → 聚合 → 归因 → 修复 → 回归**的闭环。

开启与配置（`my.cnf`）：

```ini
[mysqld]
slow_query_log                = ON
slow_query_log_file           = /var/log/mysql/slow.log
long_query_time               = 0.2    # 秒；OLTP 建议 0.1~0.5，别用默认的 10
min_examined_row_limit        = 100    # 扫描行数很少的语句不记，压掉噪声
log_queries_not_using_indexes = OFF    # 打开会被小表全扫刷屏，按需临时开
log_slow_replica_statements   = ON     # 从库回放产生的慢查询也要记录
```

日志是逐条的，直接看没有意义，先聚合：

```bash
pt-query-digest /var/log/mysql/slow.log > slow-report.txt
```

输出头部按**总耗时**排序，读三个字段就够：

```text
# Rank Query ID          Response time   Calls  R/Call   V/M   Item
# ==== ================= =============== ====== ======== ===== ============
#    1 0x8F3A2B...       120.5s  60.2%     2400  50.2ms   8.1  SELECT orders
#    2 0x2C91D4...        45.1s  22.5%      900  50.1ms   3.2  UPDATE messages
```

- `Response time` 占比：**先修占 60% 的那一条**，而不是先修单次最慢的；
- `R/Call`：平均耗时，判断是否已经影响用户体验；
- `V/M`（方差 / 均值）：大于 1 说明耗时极不稳定，通常是**锁等待或缓存未命中**，而非单纯扫描量大。

把「慢」拆成四类，对症下药：

| 类型 | 判据 | 处理 |
|---|---|---|
| 扫得多 | `rows` 大、`Handler_read_next` / `Handler_read_rnd_next` 高 | 加/改联合索引、覆盖索引、去掉 `SELECT *` |
| 排序多 | `Using filesort`、`Sort_merge_passes` 增长 | 让排序列进索引、减少排序数据量 |
| 锁等待 | `Innodb_row_lock_time` 高、1213/1205 频发 | 缩短事务、按固定顺序更新、必要时降到 RC |
| 网络回传 | `Rows_sent` 大、返回字节多 | 分页、只取必要列、大字段拆分或归档 |

治理顺序：**先改 SQL / 加索引，再谈改表结构，最后才考虑分库分表**。前两者分钟级可回滚，后者是天级、不可逆的架构变更。也不要一上来加机器——慢 SQL 加机器只是把雪崩推迟。

上线后做回归验证，对比状态变量的**差值**：

```sql
-- 上线前后各采一次，看差值而不是绝对值（实例重启会清零）
SHOW GLOBAL STATUS WHERE Variable_name IN (
  'Handler_read_key',       -- 走索引点查次数，应上升
  'Handler_read_next',      -- 索引范围扫描行数
  'Handler_read_rnd_next',  -- 全表扫描行数，应显著下降
  'Created_tmp_disk_tables' -- 磁盘临时表，应下降
);
```

> 判断「有没有变好」的硬指标是**扫描行数 / 返回行数**的比值，而不是单次耗时——耗时受缓存与并发影响波动大，扫描量才是索引设计的直接结果。

---

## 5. 版本演进速览

| 版本 | 关键变化与迁移注意点 |
|---|---|
| 5.7 | 广泛使用的旧主线；默认 `utf8`（实为 utf8mb3）、查询缓存仍存在；**已停止官方支持** |
| **8.0** | 默认字符集改 `utf8mb4`；默认认证插件改 `caching_sha2_password`（**老驱动连不上**）；**移除查询缓存**；引入窗口函数、CTE、降序索引、不可见索引、原子 DDL、JSON 增强、角色管理；**已于 2026-04 EOL** |
| **8.4 LTS** | 长期支持版本，收紧废弃参数、调整 InnoDB 默认值与复制相关术语；仍在维护，适合存量系统升级 |
| **9.7 LTS** | 2026.04 起的新 LTS 线：复制可观测性、Group Replication 治理、JSON Duality Views 的 DML、Hypergraph Optimizer 进入社区版；**新项目首选** |
| **26.x（创新版）** | 2026.07 起改用日历版本号（YY.MM）季度发布，新特性先行，**不建议生产长期跟踪**；生产选 LTS |

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
