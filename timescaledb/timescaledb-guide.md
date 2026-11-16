# TimescaleDB 技术介绍与使用指南

> 长在 PostgreSQL 里的时序扩展：把「按时间自动分区 + 列式压缩 + 连续聚合 + 生命周期策略」做成原生 SQL，让时序数据与业务表同库 JOIN。
> 仓库：<https://github.com/timescale/timescaledb>　|　当前主线：**2.31.0**（2026-10-20 发布；Apache-2.0 核心 + Community/TSL 许可 + Tiger Cloud 云版；支持 PG 16 / 17 / 18）

---

## 1. 项目简介

TimescaleDB 不是独立进程，而是**装在 PostgreSQL 里的扩展**（C 编写，需放进 `shared_preload_libraries`）：一张普通 SQL 表经 `create_hypertable()` 变成超表，物理上由若干 chunk（真实子表）组成，再叠加连续聚合、列式压缩、保留与分层等后台作业。对应用而言，它仍是一张可 `JOIN`、可进事务、可 `pg_dump` 的 PG 表。

[PostgreSQL 篇](../postgresql/postgresql-guide.md) 已讲清 MVCC / VACUUM / 索引 / WAL / 连接池，本篇不重复底座，只讲**扩展层**：chunk 怎么切、时间范围查询为何变快、连续聚合如何增量刷新、列存怎么选键、数据怎么老化。与 [InfluxDB 篇](../influxdb-v2/influxdb-v2-guide.md) 的根本差别也在这里——后者另起一套存储与查询语言，TimescaleDB 则把时序模型嵌回关系模型。

- **核心价值**：一套 PG 运维栈同时承载业务表与时序表，遥测数据可与设备表、订单表直接 JOIN，不必 ETL 双写或维护第二套备份/权限体系。
- **适用场景**：IoT 与监控指标、金融 tick、应用遥测、事件流，以及时序数据必须和业务维度关联分析的场景。
- **代价**：写入上限受 PG 单节点 WAL 与进程模型约束；列存压缩后不适合高频行级更新；Community 版受 TSL 许可约束（第 5 章）。

> 一句话定位：**「让 PostgreSQL 学会时序」，而不是「换一个数据库」**。核心（hypertable / chunk / `time_bucket`）为 Apache-2.0；连续聚合、压缩与保留策略、后台作业、`time_bucket_gapfill` 属 Community(TSL)。

---

## 2. 功能特性总览

| 能力 | 说明 |
|---|---|
| 数据模型 | 普通 SQL 表 + `create_hypertable`；时序列 `TIMESTAMPTZ`，维度列可用任意 PG 类型 |
| 自动分区 | 按时间（+ 可选 space hash）**自动建 chunk**，无需预建；`chunk_time_interval` 控制跨度 |
| 查询加速 | chunk 排除（约束排除 + 规划/执行期裁剪）、chunk 本地索引、SkipScan、列存稀疏 min/max 索引 |
| 时间函数 | `time_bucket`、`time_bucket_gapfill` + `locf()` / `interpolate()`；超函数 `first` / `last` / `approx_percentile` |
| 连续聚合 | 增量刷新的物化视图、实时聚合、失效日志与 watermark、刷新策略、分层连续聚合 |
| 列式压缩 | columnstore（旧称 compression）：`segmentby` + `orderby`、`convert_to_columnstore` / `convert_to_rowstore`、`add_columnstore_policy` |
| 生命周期 | `add_retention_policy`（按 chunk 删）、`add_tiering_policy`（冷数据进对象存储）、统一后台作业 `add_job` / `alter_job` |
| 写入 | 标准 `INSERT` / `UPDATE` / `DELETE`、`COPY` / pgx `CopyFrom`；2.23+ 直写列存（Tech Preview） |
| 生态与许可 | 完整 SQL + PG 全套工具链（Grafana / 逻辑复制 / `pg_dump`）；Apache 2 / Community(TSL) / Tiger Cloud 三种发行版 |

> 所有 `add_*_policy` 都落在同一套后台作业框架上，可用 `timescaledb_information.jobs` 与 `job_stats` 统一观测、`alter_job` 改调度。

---

## 3. 核心概念与原理

### 3.1 超表与 chunk：自动分区的抽象

hypertable 是**逻辑表**（可查询的父关系），chunk 是**物理表**（带时间 `CHECK` 约束的 PG 子表）。写请求按时间自动路由：命中已有 chunk 就追加，落在未覆盖范围就先自动建 chunk；应用永远只写父表名。

```text
hypertable: readings                       ← 应用只看到这一张表
   │  create_hypertable('readings','time', chunk_time_interval => INTERVAL '1 day')
   ▼
_hyper_1_11_chunk  CHECK (time >= '2026-11-12' AND time < '2026-11-13')
_hyper_1_12_chunk  CHECK (time >= '2026-11-13' AND time < '2026-11-14')  ← 活跃 chunk（行存）
_hyper_1_13_chunk  ...                                                    ← 已转列存
```

chunk 是真实表，可单独建索引、指定 tablespace、压缩、删除（`show_chunks()` / `timescaledb_information.chunks`）。默认只有时间维度，可加**空间维度** `by_hash('device_id', 4)` 把同设备哈希聚到一组 chunk，利于并行扫描。`chunk_time_interval` 是头号调参项：经验值是**让活跃 chunk 及其索引放进约 25% 内存**；太小则 chunk 爆炸、规划变慢、catalog 膨胀，太大则索引超出内存、裁剪粒度变粗。

`create_hypertable` 的硬约束：

1. **唯一索引 / 主键必须包含时间分区列**（用空间维度时须含所有分区列），否则建超表或后续 `CREATE UNIQUE INDEX` 直接报错——唯一性只能靠 chunk 内本地索引保证，没有跨 chunk 全局索引。典型主键是 `PRIMARY KEY (time, device_id)`。
2. **建好后不能更换分区维度**：时间列、空间分区数都不可原地改，`add_dimension()` 只能补空间维度且限制多；要改只能新建超表迁移。
3. **默认不接受非空表**：存量数据用 `migrate_data => TRUE`，它持 `ACCESS EXCLUSIVE` 锁逐行搬迁，须在停写窗口做。
4. 2.13+ 新写法 `create_hypertable('readings', by_range('time', INTERVAL '1 day'), by_hash('device_id', 4))`；2.23+ 可 `CREATE TABLE ... WITH (tsdb.hypertable)` 一步建表并默认开列存。老写法至今兼容、跨 2.x 最稳。

### 3.2 chunk 排除：时间范围查询为何变快，以及与原生声明式分区的差别

每个 chunk 的 `CHECK (time >= a AND time < b)` 是剪枝依据：规划器做**约束排除**删掉不相关 chunk，TimescaleDB 另有自定义 planner hook 做规划期裁剪（`Chunk Append`），2.31 起在嵌套循环参数化侧也支持执行期裁剪。`WHERE time > now() - INTERVAL '1 day'` 因此只扫 1~2 个 chunk。加速三重叠加：扫描量按时间收敛；每 chunk 的 B-tree 更小、更易常驻 `shared_buffers`；最近 chunk 保持行存可写，历史 chunk 转列存只读。`EXPLAIN (ANALYZE, BUFFERS)` 看 `Chunk Append` 下实际参与扫描的 chunk 数即可验证。**不带时间谓词会退化成全 chunk Append 扫描，比单表还慢**——这是时序模型的契约，不是优化器缺陷。

| 维度 | hypertable | 原生 PG 声明式分区 |
|---|---|---|
| 分区创建 | 写入时自动建 chunk | 需手工 / `pg_partman` 预建，缺分区落默认分区或写入失败 |
| 裁剪机制 | CHECK 约束 + 自定义 planner hook，规划期与执行期双层 | 原生 partition pruning |
| 内置策略 | 压缩 / 保留 / 分层 / 连续聚合 / 后台作业开箱即用 | 无，TTL 与降采样都自己写调度 |
| 分区键 | 时间必选 + 可选哈希空间维度 | RANGE / LIST / HASH 自由组合，多层需自己套娃 |
| 适用 | 持续写入的时序表 + 全生命周期治理 | 任意业务表按时间/租户归档，量不大不值得上扩展 |

> 二者不互斥：较新版本允许把 hypertable 叠加在原生分区之上（外层 LIST 做多租户隔离、内层 chunk 做时间切分）；但日常时序写入**不要手工预建分区**。

### 3.3 time_bucket 与 time_bucket_gapfill

`time_bucket('5 minutes', time)` 把时间戳向下对齐到桶起点，语义类似 `date_trunc` 但支持任意桶宽与 `origin` / `timezone`，是降采样与连续聚合的基石。`time_bucket_gapfill` 进一步**补齐缺失桶**：设备离线期间没有点，普通 `GROUP BY` 会少行，gapfill 生成空桶再用 `locf()`（取前值，适合状态量）或 `interpolate()`（线性插值，适合数值量）填充。

```sql
SELECT time_bucket_gapfill(INTERVAL '5 minutes', time) AS bucket,
       locf(avg(temperature)) AS temp_locf, interpolate(avg(humidity)) AS humid_interp
FROM readings
WHERE time >= now() - INTERVAL '6 hours' AND time < now()   -- 必须有显式时间上下界
  AND device_id = 'dev-01'
GROUP BY bucket ORDER BY bucket;
```

三条硬规则：WHERE 必须给出**非易变**时间上下界（`now()` 可，预处理语句参数在部分版本会报错）；除 gapfill 桶外每个聚合列都必须包 `locf()` / `interpolate()`；两端填充值可能为 NULL，Go 侧须用 `*float64` 或 `COALESCE`。gapfill 属 Community(TSL) 特性。

### 3.4 连续聚合：增量物化、实时聚合与刷新策略

连续聚合是**增量刷新的物化视图**：内部用 materialization hypertable 存部分聚合状态、invalidation log 记录被写脏的时间区间、watermark 标记已物化边界，刷新只重算失效桶，不像 PG 原生 `REFRESH MATERIALIZED VIEW` 全量重算。创建形如 `CREATE MATERIALIZED VIEW ... WITH (timescaledb.continuous) AS SELECT time_bucket('1 hour', time) AS bucket, ... FROM readings GROUP BY bucket, device_id WITH NO DATA;`，再用 `add_continuous_aggregate_policy()` 定时刷新。

- **实时聚合**：用户视图是 `UNION ALL`——watermark 前的物化结果 + 最新原始数据实时计算，保证看板看到最新点。**2.13+ 起默认 `materialized_only = true`（实时聚合关闭）**，不显式写 `timescaledb.materialized_only = false` 就会「最新数据查不到」，是版本迁移头号暗坑；1.7~2.12 默认相反。
- **刷新策略**：`start_offset` 覆盖迟到/回填窗口，`end_offset` 留出仍在写入的区间避免反复重算，`schedule_interval` 与桶宽对齐；迟到数据由失效日志标记，下次刷新或 `CALL refresh_continuous_aggregate()` 手动重算。
- **限制与演进**：主分组必须含 `time_bucket()`，窗口函数实验性，两 hypertable 间 JOIN 不支持，2.9+ 支持分层连续聚合；2.23 去触发器失效追踪使 DML 吞吐提升约 10~20%，2.28 支持原地加列 / `ANALYZE` / `VACUUM` / 增量手动刷新，2.29 起增量并发刷新降低锁占用。

### 3.5 列式压缩 columnstore：segmentby / orderby 与压缩后 DML

压缩在 2.x 后期重命名为 **columnstore**（旧称 compression）：2.18 引入 hypercore 表访问方法、2.20 起文档统一用 columnstore、2.22 移除 TAM 后「压缩即转列存」。机制是把 chunk 按 `segmentby` 分组、组内按 `orderby` 排序后按列打包成 batch（约千行），各列用 delta-of-delta / Gorilla / 字典 / 整数编码独立压缩（2.31 预览 AIC 自适应整数压缩），并生成稀疏 min/max 索引；查询可先按元数据跳过整个 batch，`first()` / `last()` 与 `ORDER BY time DESC LIMIT 1` 甚至无需解压。

- `segmentby` 选**低基数且在 WHERE / GROUP BY 高频等值过滤**的列（`device_id` / `tenant_id` / `symbol`）；选了 `user_id` / `trace_id` 这类高基数列，每批只剩一两行，压缩比与稀疏索引一起失效。
- `orderby` 几乎总是 `time DESC`（配合最近值查询），可叠加常做范围过滤的列；顺序影响排序游程与 min/max 跳过率。用 `chunk_compression_stats()` 核对实际压缩比，时序数据常见 10x~20x 降容。
- **压缩后 DML 限制**：早期压缩 chunk 只读，`INSERT` / `UPDATE` / `DELETE` 直接报错；2.11+ 允许 DML（后台解压缩→修改→重压缩），但写放大明显、部分约束支持有限；2.31 用向量化批量解压缩加速列存 UPDATE/DELETE；分层后的 chunk **完全只读**。故压缩窗口应设在「数据不再被修改」之后，批量修数先 `convert_to_rowstore` / `decompress_chunk`，冷层先 `untier_chunk`。
- **API 对照**：新 `ALTER TABLE ... SET (timescaledb.enable_columnstore = true, timescaledb.segmentby = ..., timescaledb.orderby = ...)` + `add_columnstore_policy()`；旧 `timescaledb.compress` / `compress_segmentby` / `compress_orderby` + `add_compression_policy()` 仍兼容，但二者不要混用。

### 3.6 生命周期：保留策略与分层存储

- **保留**：`add_retention_policy('readings', INTERVAL '90 days')` 注册后台作业，**按整块 chunk 删除**（`DROP` 子表）而非 `DELETE`，无 VACUUM 膨胀、速度极快；但边界按 chunk 的 `range_end` 判断，保留期须远大于 `chunk_time_interval`。删前先用 `show_chunks('readings', older_than => ...)` 预演，并把要长期保留的指标落进连续聚合。
- **分层**：`add_tiering_policy('readings', move_after => INTERVAL '30 days')` 把整块 chunk 冻结后异步上传对象存储（Parquet），上传期间仍可查询，本地副本在对象存储持久化后才删除；查询自动跨热/冷层，冷层只读、按原始未压缩大小计费（Tiger Cloud）。`timescaledb_osm.tiered_chunks` 看已分层块，回填需 `CALL untier_chunk(...)` 拉回。
- **组合**：热（行存、可改）→ 温（列存压缩、少改）→ 冷（对象存储、只读）→ 过期删除，四阶段各由一条策略驱动，统一落在 `add_job` 框架。注意保留/分层/列存策略均为 Community(TSL) 特性，Apache-2 版只能自己写定时脚本。

---

## 4. 使用实战

### 4.1 环境与依赖

```bash
go get github.com/jackc/pgx/v5/pgxpool
# 官方镜像已预置 shared_preload_libraries=timescaledb；PG 大版本按第 5 章矩阵选 16/17/18
docker run -d --name tsdb -p 5432:5432 \
  -e POSTGRES_PASSWORD=devpass -e POSTGRES_DB=metrics \
  timescale/timescaledb-ha:pg17
```

> 用原生 `postgres` 镜像自行编译安装必须加 `-c shared_preload_libraries=timescaledb` 并重启；2.31+ 与 `pgaudit` 共存时 timescaledb 要排前面。

### 4.2 建超表

```sql
CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE TABLE readings (
    time TIMESTAMPTZ NOT NULL, device_id TEXT NOT NULL,
    temperature DOUBLE PRECISION NOT NULL, humidity DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (time, device_id)                 -- 主键必须含时间列
);
SELECT create_hypertable('readings', 'time',
    chunk_time_interval => INTERVAL '1 day', if_not_exists => TRUE);
```

### 4.3 完整可运行 Go 示例（pgx v5）

建表、`CopyFrom` 批量写、`time_bucket` 聚合、`time_bucket_gapfill` 补点：

```go
package main

import (
    "context"
    "fmt"
    "log"
    "time"

    "github.com/jackc/pgx/v5"
    "github.com/jackc/pgx/v5/pgxpool"
)

const dsn = "postgres://postgres:devpass@localhost:5432/metrics?pool_max_conns=10"

type Reading struct {
    Time time.Time
    Device string
    Temp, Humid float64
}

func main() {
    ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
    defer cancel()
    pool, err := pgxpool.New(ctx, dsn)
    if err != nil { log.Fatal(err) }
    defer pool.Close()
    if err := pool.Ping(ctx); err != nil { log.Fatal(err) }

    ddl := []string{
        `CREATE EXTENSION IF NOT EXISTS timescaledb`,
        `CREATE TABLE IF NOT EXISTS readings (
             time TIMESTAMPTZ NOT NULL, device_id TEXT NOT NULL,
             temperature DOUBLE PRECISION NOT NULL, humidity DOUBLE PRECISION NOT NULL,
             PRIMARY KEY (time, device_id))`,
        `SELECT create_hypertable('readings','time',
             chunk_time_interval => INTERVAL '1 day', if_not_exists => TRUE)`,
    }
    for _, stmt := range ddl {
        if _, err := pool.Exec(ctx, stmt); err != nil { log.Fatal(err) }
    }

    base := time.Now().UTC().Add(-30 * time.Minute).Truncate(time.Second)
    devices := []string{"dev-01", "dev-02", "dev-03"}
    rows := make([]Reading, 3000)
    for i := range rows {
        rows[i] = Reading{base.Add(time.Duration(i) * time.Second), devices[i%3],
            20 + float64(i%17)*0.3, 40 + float64(i%11)*0.7}
    }
    n, err := pool.CopyFrom(ctx, pgx.Identifier{"readings"},
        []string{"time", "device_id", "temperature", "humidity"},
        pgx.CopyFromSlice(len(rows), func(i int) ([]any, error) {
            r := rows[i]
            return []any{r.Time, r.Device, r.Temp, r.Humid}, nil
        }))
    if err != nil { log.Fatal(err) }
    fmt.Println("CopyFrom 写入行数:", n)

    rs, err := pool.Query(ctx, `
        SELECT time_bucket(INTERVAL '5 minutes', time), device_id, avg(temperature), count(*)
        FROM readings WHERE time >= $1 AND device_id = ANY($2::text[])
        GROUP BY 1, 2 ORDER BY 1 DESC, 2 LIMIT 20`,
        time.Now().Add(-2*time.Hour), []string{"dev-01", "dev-02"})
    if err != nil { log.Fatal(err) }
    type Bucket struct { Bucket time.Time; Device string; Avg float64; N int64 }
    buckets, err := pgx.CollectRows(rs, func(row pgx.CollectableRow) (Bucket, error) {
        var b Bucket
        err := row.Scan(&b.Bucket, &b.Device, &b.Avg, &b.N)
        return b, err
    })
    if err != nil { log.Fatal(err) }
    for _, b := range buckets {
        fmt.Printf("bucket=%s device=%s avg=%.2f n=%d\n",
            b.Bucket.Format(time.RFC3339), b.Device, b.Avg, b.N)
    }
    // 需要补点时把 SQL 换成 3.3 的 time_bucket_gapfill；NULL 结果用 *float64 接
}
```

> `CopyFrom` 走 PG 的 `COPY` 协议，比循环 `INSERT` 快一个数量级，但**不支持 `ON CONFLICT`**；需要幂等时改写 staging 表再 `INSERT ... ON CONFLICT DO NOTHING`。`pgx.CollectRows` 自动 `Close()` 并检查 `rows.Err()`（手写 `rows.Next()` 循环则必须自己检查）。gapfill 的时间边界用字面 `now()` 而非参数，是为避开部分版本对参数化边界的限制。

### 4.4 策略 SQL：连续聚合、压缩、保留、分层

```sql
-- 连续聚合：2.13+ 默认 materialized_only=true，显式开启实时聚合
CREATE MATERIALIZED VIEW readings_hourly
WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS
SELECT time_bucket(INTERVAL '1 hour', time) AS bucket, device_id,
       avg(temperature) AS avg_temp, max(temperature) AS max_temp,
       first(temperature, time) AS first_temp, last(temperature, time) AS last_temp
FROM readings GROUP BY bucket, device_id WITH NO DATA;
SELECT add_continuous_aggregate_policy('readings_hourly', start_offset => INTERVAL '3 days', end_offset => INTERVAL '1 hour', schedule_interval => INTERVAL '1 hour');
CALL refresh_continuous_aggregate('readings_hourly', now() - INTERVAL '7 days', now() - INTERVAL '1 hour');

-- 列存：新命名（2.18+）；旧 API 为 timescaledb.compress / compress_segmentby / compress_orderby + add_compression_policy()
ALTER TABLE readings SET (timescaledb.enable_columnstore = true,
    timescaledb.segmentby = 'device_id', timescaledb.orderby = 'time DESC');
SELECT add_columnstore_policy('readings', INTERVAL '7 days');

-- 保留（按整块 chunk 删）与分层（冷数据进对象存储，落冷后只读）
SELECT show_chunks('readings', older_than => INTERVAL '90 days');
SELECT add_retention_policy('readings', INTERVAL '90 days');
SELECT add_tiering_policy('readings', move_after => INTERVAL '30 days', if_not_exists => true);
SELECT * FROM timescaledb_osm.tiered_chunks;

-- 观测：策略作业、压缩比、容量
SELECT * FROM timescaledb_information.jobs WHERE proc_name LIKE 'policy_%';
SELECT * FROM chunk_compression_stats('readings') ORDER BY chunk_name;
SELECT hypertable_detailed_size('readings');
```

---

## 5. 版本演进与要点

### 5.1 许可与发行版（2025-06-17 起 Tiger Data 更名后口径）

| 版本 | 许可 | 关键点 |
|---|---|---|
| Apache 2 Edition | **Apache-2.0** | 可自由修改、可作为托管服务出售；但不含连续聚合、保留/压缩/分层策略、后台作业、`time_bucket_gapfill`、多数超函数 |
| Community Edition | **TSL**（Timescale / Tiger Data License，2025-06-17 生效） | 源码可见、自建自用免费、有 right-to-repair；**禁止作为 DBaaS 出售**；默认安装即此版 |
| Tiger Cloud | 商业托管 | 原 Timescale Cloud，含分层存储等云侧能力 |

> `SHOW timescaledb.license;` 返回 `timescale`（Community）或 `apache`。**许可合规是选型硬门槛**：对外提供托管服务时，要么切 Apache-2 版接受功能裁剪，要么走商业授权。

### 5.2 PostgreSQL 版本支持矩阵（截至 2026-11 主线）

| TimescaleDB | PG 18 | PG 17 | PG 16 | PG 15 |
|---|---|---|---|---|
| 2.31.x / 2.30.x / 2.29.x | ✅ | ✅ | ✅ | ❌ |
| 2.28.x（最后支持 PG15 的小版本） | ✅ | ✅ | ✅ | ✅ |
| 2.23.x ~ 2.27.x | ✅ | ✅ | ✅ | ✅ |
| 2.22.x 及更早 | ❌ | ✅ | ✅ | ✅ |

- **2.29.0（2026-07-28）移除 PG 15 支持**；2.23（2025-10-29）首次完整支持 PG 18；当前主线只覆盖 PG 16 / 17 / 18。避开 PG 17.1 / 16.5 / 15.9 / 14.14 等引入破坏性二进制接口变更的小版本（已在 17.2 / 16.6 / 15.10 / 14.15 回滚），源码构建用 16.6+ 或 17.3+（2.31.0 起不再基于 17.2 构建），平台包与 Tiger Cloud 不受影响。
- 升级顺序固定：**先升 TimescaleDB 到当前 PG 支持的最后版本 → 再升 PG 大版本 → 再升 TimescaleDB**；跨多版本逐级过桥，且 `timescaledb_toolkit` 需先 ≥ 1.6.0。

### 5.3 版本演进要点

| 版本 | 关键变化与迁移注意点 |
|---|---|
| 1.x（2018）/ 2.0（2021） | 初代 hypertable / chunk；后台作业与策略框架（`add_job`）成型，多节点方案在 2.x 中逐步弱化 |
| 2.9 ~ 2.13 | 分层连续聚合、压缩 chunk 上 DML；**2.13 引入 `by_range`/`by_hash`，并把 `materialized_only` 默认改为 `true`** |
| 2.18 ~ 2.23 | hypercore 表访问方法 + 列存二级索引、SIMD 向量化聚合；文档以 columnstore 取代 compression、2.22 移除 TAM；**2.23（2025-10-29）完整支持 PG 18**、`WITH (tsdb.hypertable)` 一步建表、直写列存 INSERT（Tech Preview）、连续聚合去触发器失效追踪 |
| 2.28（2026-06-16） | 连续聚合原地加列、可直接 `ANALYZE`/`VACUUM`、增量手动刷新、`skip_cagg_invalidation` 加速批量导入；压缩 chunk 稀疏索引记录 first/last |
| 2.29（2026-07-28） | **移除 PG 15 支持**；连续聚合增量并发刷新、VACUUM 传播到压缩关系 |
| 2.31.0（2026-10-20） | 列存 UPDATE/DELETE 向量化批量解压缩、AIC 整数压缩预览、`move_to_columnstore()` 不停写压缩、gapfill 按 chunk 聚合加速 |

> 升级后执行 `ALTER EXTENSION timescaledb UPDATE;` 并核对 `timescaledb_information.jobs` 策略作业仍在；注意 backward-incompatible 变更（如 2.31 把 `enable_columnarscan` 改名为 `debug_enable_columnarscan`）。

---

## 6. 常见坑与最佳实践

1. **主键 / 唯一索引不含时间列** → `create_hypertable` 直接报错。对策：改成 `PRIMARY KEY (time, device_id)`，业务唯一性交应用层或接受「同设备同时刻唯一」语义。
2. **上线后才想改分区维度 / `chunk_time_interval` 设太小** → 维度不可原地改，chunk 成千上万会让规划变慢、catalog 膨胀。对策：压测期先定 `chunk_time_interval`（按「活跃 chunk + 索引放进约 25% 内存」反推）与 `by_hash` 分片数；确需变更就新建超表 + 双写 / 逻辑复制迁移，并监控 `num_chunks`。
3. **查询不带时间谓词** → chunk 排除失效，全 chunk Append 扫描。对策：强制带 `time` 范围、看板默认给时间窗、`statement_timeout` 兜底、用 `EXPLAIN` 数参与扫描的 chunk。
4. **升到 2.13+ 后「最新数据查不到」** → 连续聚合默认 `materialized_only = true`。对策：创建时写 `WITH (timescaledb.materialized_only = false)`，或 `ALTER MATERIALIZED VIEW ... SET (... = false)`。
5. **刷新 `end_offset` 为 0 / 迟到数据打进很老的 chunk** → 反复重算仍在写入的桶、后台作业吃 IO，并触发失效日志与连续聚合重算。对策：`end_offset` 至少留一个桶宽、`start_offset` 覆盖最大迟到窗口、`schedule_interval` 与桶宽对齐；大批量回填用 2.28+ 的 `skip_cagg_invalidation` 后手动 `refresh_continuous_aggregate`。
6. **`segmentby` 选了高基数列** → 每 batch 一两行，压缩比与稀疏索引双双失效。对策：只用 `device_id` / `tenant_id` 等低基数高频过滤列，`orderby` 固定 `time DESC`，用 `chunk_compression_stats()` 核对 batch 行数。
7. **压缩窗口设在仍需改数的时间里** → 压缩 chunk 上 DML 需解/重压缩，写放大明显，早期版本直接报错。对策：`after` 设在数据不再修改之后（如 7 天）；批量修数先 `convert_to_rowstore`，冷层先 `untier_chunk`。
8. **用 `CopyFrom` 做幂等写入 / gapfill 结果扫进 `float64`** → `COPY` 不支持 `ON CONFLICT`；`locf` / `interpolate` 两端还可能返回 NULL。对策：纯追加用 `CopyFrom`，需 upsert 先写 staging 表再 `INSERT ... ON CONFLICT DO NOTHING`；Go 侧用 `*float64` / `pgtype.Float8` 或 SQL 里 `COALESCE`。
9. **把 retention 当 `DELETE` 理解** → 按整块 chunk 删、边界取 `range_end`，保留期接近 chunk 跨度会误删或删不干净。对策：保留期远大于 `chunk_time_interval`，先 `show_chunks(... older_than => ...)` 预演，重要指标先落连续聚合。
10. **以为 Apache-2.0 版功能齐全 / 漏配 `shared_preload_libraries` / 升级顺序错** → 连续聚合、保留/压缩/分层策略、后台作业、gapfill 都是 Community(TSL) 特性，`license = apache` 时调用报错；`CREATE EXTENSION` 还会因未预加载而失败。对策：自建自用用 Community 版、做 DBaaS 切 Apache-2 版或谈商业授权，部署后 `SHOW timescaledb.license;` 校验；用官方镜像，升级严格走「扩展 → PG → 扩展」三段式并查第 5 章矩阵。

---

## 7. 选型对比

| 维度 | TimescaleDB | InfluxDB 2.x / 3.x | ClickHouse | Prometheus / VictoriaMetrics | 原生 PG 声明式分区 |
|---|---|---|---|---|---|
| 数据模型 | 关系表 + hypertable / chunk | Measurement + tag/field；3.x 列式表 | MergeTree 列式大宽表 | 指标 + label | PG 分区表 |
| SQL 表达力 | **完整 SQL + 超函数**，可与业务表 JOIN、进事务 | 2.x Flux（已弃用）/ 3.x SQL(DataFusion)，无 JOIN/事务 | 完整 SQL，JOIN/事务弱 | PromQL / MetricsQL，专用 | **完整 SQL**，同 PG |
| 写入吞吐 | 中高（受 PG WAL 限制；`CopyFrom` / 直写列存可提升） | **高**（行协议批量写） | **最高**（批量 INSERT） | 中（scrape / remote write），VM 更高 | 中（同 PG 单行事务开销） |
| 压缩比 | 列存 10x~20x | 2.x TSM、3.x Parquet，10x+ | **最高**（列存 + 编码） | 中（本地 TSDB / VM） | 基本无（行存，靠 BRIN 减索引） |
| 高基数维度 | 较好（B-tree；segmentby 需低基数） | 2.x 受 TSI 限制易膨胀，3.x 目标无限基数 | **强项** | 同样怕高基数 | 好（B-tree） |
| 生命周期治理 | **内置**压缩/保留/分层/连续聚合策略 | 2.x Bucket + Task；3.x 较弱 | TTL + 手工降采样 | 本地保留 + 远程写 | 全靠自己写脚本 |
| 集群与 HA | 依赖 PG 生态（流复制 / 逻辑复制） | 2.x OSS 单节点；3.x Enterprise 多节点 | 原生分布式 | Prometheus 单机 / VM 可集群 | 依赖 PG 生态 |
| 生态 | PG 全套工具链 + Grafana | Telegraf / Grafana；2.x 一体化 UI 告警 | OLAP / 日志生态 | **云原生监控告警事实标准** | PG 全套 |
| 典型场景 | 已有 PG 栈的时序、需 JOIN 的监控/IoT | 专用采集与监控平台 | 日志、用户行为、大宽表分析 | K8s 指标与告警 | 按时间归档的普通业务表 |

一句话决策：**团队已在 PostgreSQL 栈、时序数据要与业务表 JOIN、想要全套 SQL 与一套运维 → TimescaleDB**；纯 K8s scrape 告警 → Prometheus/VictoriaMetrics；日志与大宽表 OLAP → ClickHouse；专用高吞吐采集且不需关联查询 → InfluxDB 3；只需按时间归档的普通业务表 → 原生 PG 分区。

**何时不该用 TimescaleDB**：

- 数据量不大、只需按时间归档冷数据：原生声明式分区 + `pg_partman` 更轻，省掉扩展、后台作业与 TSL 合规负担；
- 纯 Kubernetes 指标监控与告警：不要用它替换 scrape / Alertmanager / PromQL 生态；
- 超大规模日志与用户行为即席分析、写入峰值极高且不需事务/JOIN：ClickHouse 更省成本；
- 要对外提供托管 DBaaS 且不接受 TSL 限制（Community 版禁止作为服务出售、Apache-2 版又缺连续聚合与压缩策略）；或需要原生分布式多机分片、强依赖刚发布而扩展尚未适配的 PG 新大版本、要求对历史数据高频行级 UPDATE 且不允许压缩窗口：都与「热可改 → 温压缩 → 冷只读」模型及第 5 章支持矩阵冲突。

参考：<https://docs.timescale.com/>　<https://github.com/timescale/timescaledb/releases>　<https://www.timescale.com/legal/licenses>　<https://www.timescale.com/docs/get-started/timescaledb-supported-platforms>　（版本与许可信息截至 2026-11-13 核对）
