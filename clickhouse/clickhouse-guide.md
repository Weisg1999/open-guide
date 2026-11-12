# ClickHouse 技术介绍与使用指南（Go OLAP 视角）

> 列式 OLAP 数据库：MergeTree 存储引擎、向量化执行、稀疏主键索引与数据跳过索引，把「亿级明细秒级聚合」变成常规操作。
> 上游：<https://github.com/ClickHouse/ClickHouse>　|　服务端主线：**25.x**（按年/月编号，月度发布；整理于 2026-11）　|　Go 客户端：[ClickHouse/clickhouse-go](https://github.com/ClickHouse/clickhouse-go) v2
> 协议：Apache-2.0；具体版本以上游最新 release 为准。

---

## 1. 项目简介

[MySQL 篇](../mysql/mysql-guide.md) 面向「一行一行地改」，[Elasticsearch 篇](../elasticsearch/elasticsearch-guide.md) 面向「一段文本怎么找到」，ClickHouse 面向的是第三种问题：**几亿行明细，按若干维度聚合出一张报表，还要够快**。它是 OLAP（分析型）数据库，不是 OLTP。

它快的三个根本原因：**列式存储**（只读取查询涉及的列，同类型数据压缩比极高）、**MergeTree 追加写 + 后台合并**（不做随机更新）、**向量化执行**（按列批量喂给 SIMD，而不是逐行解释）。

- **适用场景**：用户行为/埋点分析、日志与监控明细、BI 报表与即席查询、实时数仓、Trace 明细离线分析、大宽表聚合
- **不适合**：按主键频繁点查与更新（无事务、无行锁、UPDATE 是异步重写）、高并发小事务（用 [MySQL](../mysql/mysql-guide.md)）、全文相关性检索（用 [ES](../elasticsearch/elasticsearch-guide.md)）、纯指标监控告警（用 [Prometheus](../prometheus/prometheus-guide.md)）、高频 KV（用 [go-redis](../redis/redis-guide.md)）

> 与 [InfluxDB 3 篇](../influxdb-v3/influxdb-v3-guide.md) 对照读很有意思：两者都走「列存 + 对象存储 + 向量化」，只是 InfluxDB 3 选了 FDAP（Parquet + DataFusion + Arrow），ClickHouse 自研 MergeTree 原生格式。

### 安装与本地起服务

```bash
go get github.com/ClickHouse/clickhouse-go/v2
docker run --rm -d --name ch-dev \
  -p 9000:9000 -p 8123:8123 \
  --ulimit nofile=262144:262144 \
  clickhouse/clickhouse-server:25
curl 'http://localhost:8123/?query=SELECT%20version()'
```

> 9000 是原生 TCP 协议（Go 客户端用），8123 是 HTTP 接口。镜像标签 `25` 为大版本通配，生产请钉死具体版本。

### 最小可运行示例

```go
package main

import (
    "context"
    "fmt"

    "github.com/ClickHouse/clickhouse-go/v2"
)

func main() {
    conn, err := clickhouse.Open(&clickhouse.Options{
        Addr: []string{"localhost:9000"},
        Auth: clickhouse.Auth{Database: "default", Username: "default"},
    })
    if err != nil { panic(err) }
    ctx := context.Background()
    if err := conn.Ping(ctx); err != nil { panic(err) }

    if err := conn.Exec(ctx, `
        CREATE TABLE IF NOT EXISTS events (
            ts      DateTime64(3),
            user_id UInt64,
            event   LowCardinality(String),
            value   Float64
        ) ENGINE = MergeTree
        PARTITION BY toYYYYMM(ts)
        ORDER BY (event, user_id, ts)
    `); err != nil { panic(err) }

    batch, err := conn.PrepareBatch(ctx, "INSERT INTO events")
    if err != nil { panic(err) }
    for i := 0; i < 1000; i++ {
        if err := batch.Append(time.Now(), uint64(i%100), "view", 1.0); err != nil { panic(err) }
    }
    if err := batch.Send(); err != nil { panic(err) }

    var cnt uint64
    if err := conn.QueryRow(ctx, "SELECT count() FROM events").Scan(&cnt); err != nil {
        panic(err)
    }
    fmt.Println("rows:", cnt)
}
```

---

## 2. 功能特性总览

| 能力 | 说明 |
|---|---|
| 存储引擎 | `MergeTree` 家族：`ReplacingMergeTree`（去重）/ `SummingMergeTree`（预聚合）/ `AggregatingMergeTree` / `CollapsingMergeTree` / `Replicated*` 副本 |
| 列式存储 | 每列独立文件 + 压缩（LZ4 / ZSTD）、只读涉及列、向量化执行与 SIMD |
| 索引 | **稀疏主键索引**（granule + mark）、数据跳过索引（minmax / set / bloom_filter / tokenbf_v1）、Projection |
| 分区与生命周期 | `PARTITION BY`（通常按月）、TTL 自动过期、分区级 DROP / MOVE 到冷热存储 |
| 预计算 | 物化视图（写入时增量聚合）、Projection、`AggregatingMergeTree` |
| 分布式 | `Distributed` 表引擎 + 本地表、分片键、副本与 ClickHouse Keeper（替代 ZooKeeper） |
| 数据摄入 | 原生批量 INSERT、`async_insert`、Kafka / S3 / HDFS 表引擎、Parquet/CSV/JSON 文件导入 |
| 查询 | 类 SQL 方言、`PREWHERE`、`SAMPLE` 近似查询、数组/Map/JSON 类型、窗口函数、CTE |
| Go SDK | `clickhouse-go/v2`：`PrepareBatch` 批量写、`database/sql` 兼容接口、结构体反射扫描、异步插入 |
| 可运维 | `system.parts` / `system.query_log` / `system.merges`、`EXPLAIN`、查询配额与资源组 |

---

## 3. 核心概念与原理

### 3.1 列存为什么适合分析

```
行存 (MySQL):  [id|name|age|city] [id|name|age|city] ...
               → 查询 SELECT avg(age) 必须把整行读进内存

列存 (CH):     [id|id|id...] [name|name|...] [age|age|age...] [city|...]
               → 只读 age 这一段连续数据；同类型值压缩比可达 10~20 倍
               → 连续数组可直接喂 SIMD，一次处理数百个值
```

代价也很直接：**按主键取一整行很慢**（要拼多个列文件），**更新一行几乎不可能**（要重写整个 part）。所以 ClickHouse 的设计前提就是「追加写、批量读」。

### 3.2 MergeTree：part、granule 与合并

```
INSERT 批次
   │ 按 PARTITION BY 分区，按 ORDER BY 排序
   ▼
生成一个不可变的 part（每个 part 内每列一个 .bin + .mrk 标记文件）
   │
   ├─► 稀疏主键索引：每 8192 行（granule）记一个排序键的值 + mark
   │      → 索引极小，可常驻内存；定位到 granule 后再顺序扫描 8192 行
   ▼
后台 merge：把多个小 part 合并成大 part（同时执行 Replacing 去重、TTL 清理）
```

关键推论：

- **ORDER BY 是最重要的设计决策**：它决定数据物理排序、稀疏索引能否生效、以及 `ReplacingMergeTree` 按什么键去重
- 排序键应把**高频过滤条件放前面、低基数字段优先**（`ORDER BY (event, user_id, ts)` 而不是反过来），`ts` 这类高基数时间字段通常放最后
- **part 是不可变的**，所以 `UPDATE` / `DELETE`（mutation）是异步重写整个 part，代价高、不保证立即可见

### 3.3 数据跳过索引

稀疏主键索引只对排序键前缀有效。对其他列可用跳过索引在读取前跳过整个 granule：

| 类型 | 适用 |
|---|---|
| `minmax` | 数值/时间范围（最常用） |
| `set(n)` | 低基数枚举 |
| `bloom_filter` | 高基数等值判断、字符串包含 |
| `tokenbf_v1` | 文本 token 检索 |

```sql
ALTER TABLE events ADD INDEX idx_uid user_id TYPE bloom_filter(0.01) GRANULARITY 4;
ALTER TABLE events MATERIALIZE INDEX idx_uid;  -- 只对新 part 生效，历史需 MATERIALIZE
```

### 3.4 ReplacingMergeTree 的「最终一致去重」

```sql
CREATE TABLE users (...) ENGINE = ReplacingMergeTree(ver) ORDER BY user_id;
```

去重**只在后台 merge 时发生**，且只保证同一 part 内、同一分片内。因此刚写入后 `SELECT count()` 可能看到重复行。三种解法：查询时加 `FINAL`（简单但会读放大）、用 `argMax(col, ver) GROUP BY key`（推荐）、或接受近似去重。

> 这是把 ClickHouse 当「有主键的表」用时最常踩的坑：它**没有唯一约束**。

### 3.5 分布式查询模型

```
Distributed 表（逻辑表，不存数据）
   │ 查询发到各分片的本地表 → 各分片并行聚合中间态 → 发起节点汇总
   ▼
本地表 (*_local, ReplicatedMergeTree)
```

- `Distributed` 表负责分发与二次聚合；**写入 Distributed 表会先落本地缓冲再异步转发**，批量写入建议直写本地表并自己控制分片键
- `GLOBAL JOIN / GLOBAL IN` 用于右表需要广播的场景，否则每个分片各自执行子查询
- 副本一致性由 `Replicated*MergeTree` + Keeper 保证，DDL 用 `ON CLUSTER` 广播

### 3.6 与 InfluxDB 3 / Prometheus 的边界

| | ClickHouse | InfluxDB 3 | Prometheus |
|---|---|---|---|
| 存什么 | 任意明细事件 | 时序指标 + 事件 | 近期监控指标 |
| 查询 | 完整 SQL | SQL + InfluxQL | PromQL |
| 保留 | 月/年级，TTL | 对象存储长期 | 默认短期，remote write 归档 |
| 典型 | 行为分析、BI、日志明细 | 设备时序 | SLO 与告警 |

---

## 4. 使用实战

### 4.1 客户端初始化（生产配置）

```go
func Open() (driver.Conn, error) {
    return clickhouse.Open(&clickhouse.Options{
        Addr: []string{"ch-1:9000", "ch-2:9000"}, // 多节点自动负载均衡
        Auth: clickhouse.Auth{
            Database: "analytics", Username: "app", Password: os.Getenv("CH_PASS"),
        },
        DialTimeout:      5 * time.Second,
        MaxOpenConns:     10,
        MaxIdleConns:     5,
        ConnMaxLifetime:  time.Hour,
        Compression:      &clickhouse.Compression{Method: clickhouse.CompressionLZ4},
        Settings: clickhouse.Settings{
            "max_execution_time":        60,
            "max_memory_usage":          4_000_000_000,
            "max_bytes_before_external_group_by": 2_000_000_000, // 聚合溢写磁盘
        },
    })
}
```

> 也提供 `clickhouse.OpenDB(...)` 返回 `*sql.DB` 以复用 `database/sql` 生态，但**批量写入必须用原生 `PrepareBatch`**。

### 4.2 批量写入（唯一正确的写入姿势）

```go
type Event struct {
    Ts     time.Time
    UserID uint64
    Event  string
    Value  float64
}

func InsertBatch(ctx context.Context, conn driver.Conn, events []Event) error {
    batch, err := conn.PrepareBatch(ctx, "INSERT INTO events")
    if err != nil { return err }
    for _, e := range events {
        if err := batch.Append(e.Ts, e.UserID, e.Event, e.Value); err != nil {
            _ = batch.Abort()
            return err
        }
    }
    return batch.Send() // 一次 RPC 提交整批
}
```

> **绝对不要循环 `INSERT INTO ... VALUES (?)`**：每次 INSERT 都会生成一个 part，后台合并跟不上会触发 `Too many parts` 报错。单批建议 1 万～10 万行或 10~100MB，攒批间隔 1~10s。低频小批量可用 `async_insert=1` 让服务端攒批。

### 4.3 参数化查询与聚合

```go
rows, err := conn.Query(ctx, `
    SELECT event, count() AS cnt, round(avg(value), 2) AS avg_val
    FROM events
    WHERE ts >= ? AND ts < ? AND user_id IN ?
    GROUP BY event
    ORDER BY cnt DESC
    LIMIT 20`, from, to, userIDs)
if err != nil { return err }
defer rows.Close()

for rows.Next() {
    var ev string
    var cnt uint64
    var avg float64
    if err := rows.Scan(&ev, &cnt, &avg); err != nil { return err }
    fmt.Printf("%s: %d / %.2f\n", ev, cnt, avg)
}
return rows.Err()
```

> `IN ?` 可直接传切片，驱动会展开；字符串用参数化而非拼接。`PREWHERE` 可让过滤在读取其他列之前发生，对宽表收益明显。

### 4.4 物化视图：把聚合前移到写入时

```sql
CREATE MATERIALIZED VIEW events_daily_mv
TO events_daily AS
SELECT
    toDate(ts)      AS day,
    event,
    count()         AS cnt,
    sum(value)      AS total
FROM events
GROUP BY day, event;

-- 目标表用 SummingMergeTree 承接增量
CREATE TABLE events_daily (
    day Date, event LowCardinality(String), cnt UInt64, total Float64
) ENGINE = SummingMergeTree ORDER BY (day, event);
```

> 物化视图**只对创建之后写入的数据生效**；历史数据要 `INSERT INTO events_daily SELECT ... FROM events WHERE ...` 回填。查询时仍建议 `sum(cnt)` 而非直接读 `cnt`（合并尚未完成时是分片累加值）。

### 4.5 排查慢查询

```sql
EXPLAIN indexes = 1 SELECT ...;              -- 看跳过了多少 granule
SELECT query, read_rows, read_bytes, query_duration_ms, memory_usage
FROM system.query_log WHERE type = 'QueryFinish' ORDER BY query_duration_ms DESC LIMIT 10;
SELECT partition, name, rows, bytes_on_disk
FROM system.parts WHERE table = 'events' AND active ORDER BY bytes_on_disk DESC;
SELECT * FROM system.merges WHERE table = 'events';
```

```bash
# 文件导入 / 导出
clickhouse-client --query "INSERT INTO events FORMAT CSV" < events.csv
clickhouse-client --query "SELECT * FROM events FORMAT Parquet" > events.parquet
```

### 4.6 从 Kafka 摄取（接 [Kafka 篇](../kafka/kafka-guide.md)）

```sql
CREATE TABLE events_queue (ts DateTime64(3), user_id UInt64, event String, value Float64)
ENGINE = Kafka
SETTINGS kafka_broker_list = 'kafka:9092',
         kafka_topic_list = 'events',
         kafka_group_name = 'ch_events',
         kafka_format = 'JSONEachRow';

CREATE MATERIALIZED VIEW events_consumer TO events AS
SELECT * FROM events_queue;
```

> Kafka 引擎表是「消费者」，读一次就消费掉；数据落在 `TO` 指定的目标表。生产要监控消费 lag，且注意 **Kafka 表引擎本身不可直接 SELECT 排查**（会消费数据）。

---

## 5. 版本演进速览

| 阶段 | 关键变化与迁移注意点 |
|---|---|
| 早期（20.x 前） | MergeTree 成熟，ZooKeeper 承担副本协调；版本号改为「年.月」式月度发布 |
| 21~23.x | 投影（Projection）、窗口函数、轻量 `DELETE`、异步插入、ClickHouse Keeper 逐步替代 ZooKeeper |
| 24.x | JSON 类型与动态子列、Parquet/数据湖集成增强、倒排/全文索引实验、查询并行度优化 |
| 25.x | 继续强化 JOIN / 聚合性能、对象存储与存算分离、可观测性系统表、共享 MergeTree 等能力 |

**迁移注意**：

- ClickHouse 是**月度发布**，小版本迭代快、废弃项多。生产钉死具体版本，升级前逐条读 CHANGELOG 的 Breaking Changes
- **Keeper 替代 ZooKeeper**：新集群直接用 ClickHouse Keeper，老集群迁移需按官方步骤
- `clickhouse-go` **v1 → v2** 是破坏性升级：包路径改 `/v2`、原生 `driver.Conn` API 与 `database/sql` 接口分离、`PrepareBatch` 用法变化
- 服务端与客户端大版本不必严格一致，但新语法（如参数化数组、JSON 类型）需要较新的客户端

---

## 6. 常见坑与最佳实践

1. **逐条 INSERT**：每次生成一个 part → `Too many parts (300)` 报错、合并线程打满。批量 1 万行起，客户端攒批。
2. **ORDER BY 选错**：把高基数 `ts` 放首位、或把查询几乎不用的字段放首位，稀疏索引形同虚设。按「高频过滤、低基数优先」设计，并与实际查询一起压测。
3. **把 `ReplacingMergeTree` 当唯一约束**：去重发生在后台合并、且只在分片内。用 `argMax` / `GROUP BY` 或查询加 `FINAL`（评估读放大）。
4. **滥用 `FINAL`**：它会在查询时合并所有 part，大表上可能慢一个数量级。优先改查询写法。
5. **用 UPDATE/DELETE 做业务更新**：mutation 是异步重写整个 part，无法回滚、不即时可见。ClickHouse 是追加写系统，要改数据就重新导入分区。
6. **分区粒度过细**：`PARTITION BY toYYYYMMDDhh(ts)` 按小时分区会产生海量分区，元数据与合并成本爆炸。通常按月（数据量极大才按天）。
7. **`Nullable` 滥用**：每列多一个 null mask 文件、有额外开销。能用默认值（`''` / `0`）表达就不要用 Nullable。
8. **低基数字段用 String**：`event`、`status` 这类用 `LowCardinality(String)` 或 `Enum`，压缩与过滤收益明显。
9. **高基数 GROUP BY OOM**：设 `max_bytes_before_external_group_by` 允许溢写磁盘，或用物化视图预聚合。
10. **JOIN 右表过大**：ClickHouse 默认把右表载入内存构建 hash。用维度表 `dictionary`、预聚合或 `join_algorithm` 调优。
11. **`SELECT *`**：列存下虽然会跳过无关列文件，但宽表上仍会读回大量无用数据。明确列清单。
12. **无查询配额**：一个即席查询吃光内存拖垮集群。按用户/角色设 `max_memory_usage`、`max_execution_time`、并发限制。
13. **物化视图不回填历史**：只对创建后的写入生效，且 `SummingMergeTree` 查询仍需 `sum()`。
14. **副本与 DDL 不同步**：单机 `CREATE TABLE` 只在一个节点生效；集群环境用 `ON CLUSTER`，并确认 Keeper 健康。
15. **时区与精度**：`DateTime` 秒级、`DateTime64(3)` 毫秒级；Go `time.Time` 与服务端时区不一致会让分区落错月份。统一 UTC 并在写入侧显式转换。
16. **拿它当 OLTP / 缓存 / 搜索引擎**：无事务、无点查优势、无相关性排序。分别用 [MySQL](../mysql/mysql-guide.md) / [go-redis](../redis/redis-guide.md) / [ES](../elasticsearch/elasticsearch-guide.md)。
17. **不监控 parts 与 merges**：`system.parts` 的 part 数、`system.merges` 的积压是写入健康的第一指标，比 CPU 更早报警。

---

## 7. 选型对比

| 维度 | ClickHouse | Elasticsearch | Apache Druid / Pinot | Snowflake / BigQuery | DuckDB | InfluxDB 3 |
|---|---|---|---|---|---|---|
| 定位 | 通用 OLAP 明细分析 | 检索 + 日志 | 实时 OLAP 多维 | 云数仓 | 嵌入式/单机分析 | 时序数据 |
| 存储 | 自研 MergeTree 列存 | 倒排 + doc_values | 列存 + 预聚合 | 对象存储列存 | 本地列存 | Parquet + DataFusion |
| 查询 | **完整 SQL** | Query DSL / ES SQL | SQL（有限） | 完整 SQL | 完整 SQL | SQL + InfluxQL |
| JOIN | 支持（需调优） | 不支持 | 弱 | **强** | 强 | 有限 |
| 全文检索 | 弱（倒排索引实验性） | **强** | 弱 | 弱 | 弱 | 弱 |
| 写入 | **批量极高** | 高 | 极高（实时摄取） | 批式为主 | 单进程 | 高 |
| 运维 | 中（自建）/ 低（Cloud） | 高（JVM） | 高（多元件） | **极低（托管）** | 极低 | 中 |
| 典型 | 行为分析、日志明细、BI | 搜索、日志检索 | 实时看板 | 企业数仓 | 本地分析 / ETL | 监控与 IoT 时序 |

一句话决策：

- **自建、要完整 SQL、亿级明细秒级聚合、成本敏感** → **ClickHouse**，当前性能和生态综合最均衡。
- **主要需求是全文检索与相关性排序** → [Elasticsearch](../elasticsearch/elasticsearch-guide.md)；日志只做标签过滤与查看 → Loki 更省。
- **超大规模实时多维看板、预聚合模型固定** → Druid / Pinot，但组件更多、运维更重。
- **不想运维、预算充足、已有云数仓** → Snowflake / BigQuery，用钱换人力。
- **本地/单进程分析、ETL 中间步骤、单元测试** → DuckDB，零运维，SQL 兼容性极好。
- **纯时序指标与监控** → [InfluxDB 3](../influxdb-v3/influxdb-v3-guide.md) 或 [Prometheus](../prometheus/prometheus-guide.md)；**主数据事务** → [MySQL](../mysql/mysql-guide.md)。

> 最后一句经验：ClickHouse 的性能几乎完全由「**排序键 + 分区 + 批量写入**」这三件事决定，而这三件事都在建表那一刻就定死了。上线前用真实量级的数据跑一遍 `EXPLAIN indexes = 1` 与 `system.query_log`，比事后加机器有效得多。
