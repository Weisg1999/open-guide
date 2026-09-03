# InfluxDB 3（Core / Enterprise）技术介绍与使用指南

> Rust 重写的新世代时序平台：FDAP 架构（Flight + DataFusion + Arrow + Parquet）、SQL 查询、对象存储优先、近乎无限的基数支持。
> 仓库：<https://github.com/influxdata/influxdb>　|　GA：2025.4（Core 开源，MIT/Apache-2）

---

## 1. 项目简介

InfluxDB 3 是 InfluxData 历时四年多从零重写的产品，动机来自用户呼声最高的三个能力：

1. **无限基数（Infinite Cardinality）**：v2 的 TSI 索引在高基数下内存爆炸；v3 列式架构让 tag 组合数不再是性能枷锁；
2. **历史数据放对象存储**：冷数据沉到 S3 等廉价存储，同时保持可查询；
3. **标准 SQL**：弃用自研 Flux，查询语言回归工业标准。

两条产品线（同一内核）：

| 版本 | 定位 | 关键差异 |
|---|---|---|
| **Core（开源）** | 高速"近期数据引擎" | 单进程；内存中保留近期数据 + last cache；历史查询受 Parquet 文件数限制（默认 432 个文件 ≈ 72 小时 @ 默认 10 分钟文件粒度）；MIT/Apache-2 许可 |
| **Enterprise（商业）** | 完整平台 | Compactor 后台压缩、长期历史查询、HA、多节点、增强安全 |

典型场景：DevOps/IoT 监控、AI Agent 可观测、高频传感数据、需要 SQL 直接分析时序的场景。

### 启动

```bash
# 下载 influxdb3 二进制（或 docker run influxdb:3-core）
influxdb3 serve --node-id local01 \
  --object-store file \
  --data-dir ~/.influxdb3
# 默认 HTTP :8181；写入 /api/v3/write_lp，查询走 SQL
```

Go 客户端：`go get github.com/InfluxCommunity/influxdb3-go/v2`

---

## 2. 核心架构：FDAP 栈

```
F light      Apache Flight / Flight SQL —— 高性能列式传输协议（gRPC）
D ataFusion  Apache DataFusion —— Rust SQL 查询引擎（向量化执行）
A rrow       Apache Arrow —— 内存列式格式，组件间零拷贝共享
P arquet     Apache Parquet —— 磁盘/对象存储列式文件格式
```

v2 是"自研一切"（TSM 引擎 + Flux 语言），v3 是"站在开源分析生态上"：SQL、Flight SQL、与 pandas/Polars/DuckDB 的互操作几乎免费获得。

### 2.1 写入路径（diskless 架构）

```
Line Protocol 写入 → Router/Server
   → WAL（预写日志，本地盘，近实时查询可见）
   → 内存 write buffer（按时间分桶）
        │ 每 gen1 周期（默认 10 分钟）
        ▼
   Parquet 文件（对象存储 S3 / 本地 file）  ←→  Catalog（元数据：库表/文件清单）
```

- **近期数据**在 WAL 与内存 buffer 中，查询"最近几分钟"不碰对象存储——低延迟；
- 每 10 分钟把内存数据**整块转成 Parquet**（gen1 文件）写入对象存储；Catalog 记录"哪段时间在哪个文件"；
- **Enterprise 的 Compactor** 把小 gen1 文件合并成更大的 gen2/gen3 层，历史查询性能与成本的关键（Core 没有后台 Compactor，所以查询触碰的文件数受限）；
- "diskless"：持久状态可全部放对象存储，进程本身无状态化——K8s 友好（挂了重启即恢复）。

### 2.2 查询路径

```
SQL（Flight SQL / HTTP）→ DataFusion 生成计划
   → Catalog 定位涉及的 Parquet 文件 + 内存/WAL 近期数据
   → 向量化扫描（谓词下推、只读所需列）→ 聚合 → Arrow 结果返回
```

- SQL 完整语法：JOIN、窗口函数、CTE——v2 Flux 里费劲的 join 现在是一句 SQL；
- 兼容 **InfluxQL**（v1 语法子集）帮助迁移；**Flux 不支持**（迁移工具负责转换常见查询）；
- 多个查询引擎可经 Arrow Flight 直连 InfluxDB 3 的 Parquet 生态（DuckDB 查 S3 上的 InfluxDB Parquet 是官方演示场景）。

### 2.3 数据模型：与 v2 的对照

| v2 | v3 | 说明 |
|---|---|---|
| Organization | （服务级配置） | v3 简化 |
| Bucket | **Database** | 概念合并：一个 database 即一个库 |
| Measurement | **Table** | 表；tags/fields 即列 |
| Series/TSI 索引 | （无 Series 概念） | 列式 + Catalog，基数不再是索引负担 |

Line Protocol 仍是写入协议：`cpu,host=node1 usage_idle=91.2 1735689600000000000`，tag→普通字符串列、field→类型化数值列、timestamp→time 列。schema 可动态演化（宽稀疏表）。

### 2.4 Processing Engine：嵌入式 Python VM

v3 内嵌 Python 虚拟机，**插件在数据写入路径上零拷贝执行**（对 Arrow 批直接处理）：

- 触发器：WAL flush 事件 / 定时（cron）；
- 用途：降采样（替代 v2 的 Task）、异常检测、告警转发（ webhook/PagerDuty）、数据清洗；
- 插件就是 Python 文件（标准插件库 + 自定义），是 v2 Tasks 的官方继任者。

```python
# 处理引擎插件形态示意
def process_batch(batch, context):
    if batch["cpu"].mean() > 90:
        context.alert("cpu high")
```

---

## 3. 使用实战（Go）

### 3.1 写入与查询（influxdb3-go）

```go
client, err := influxdb3.New(influxdb3.ClientConfig{
    Host:       "http://localhost:8181",
    Token:      "my-token",
    Database:   "metrics",
})
defer client.Close()

// 写入 Line Protocol
points := []string{
    "cpu,host=node1,region=cn usage_idle=91.2,usage_user=6.1",
    "cpu,host=node2,region=cn usage_idle=88.7,usage_user=7.9",
}
err = client.Write(context.Background(), points)

// SQL 查询
query := `SELECT host, DATE_BIN(INTERVAL '5 minutes', time, TIMESTAMP '1970-01-01T00:00:00Z')
                 AS window, AVG(usage_idle) AS idle
          FROM cpu WHERE time > NOW() - INTERVAL '1 hour'
          GROUP BY host, window ORDER BY window`
it, err := client.Query(context.Background(), query)
for it.Next() {
    row := it.Value()
    fmt.Println(row["host"], row["window"], row["idle"])
}
```

### 3.2 HTTP API（任何语言零依赖接入）

```bash
# 写入
curl -X POST "http://localhost:8181/api/v3/write_lp?db=metrics&precision=nanosecond" \
  -H "Authorization: Bearer $TOKEN" \
  --data-binary 'cpu,host=node1 usage_idle=91.2'

# SQL 查询（JSON / Arrow / CSV / Parquet 格式）
curl -X POST "http://localhost:8181/api/v3/query_sql" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"db":"metrics","q":"SELECT host, AVG(usage_idle) FROM cpu WHERE time > NOW() - INTERVAL 1 HOUR GROUP BY host"}'
```

### 3.3 管理

```bash
# CLI
influxdb3 create database iot --retention-period 30d
influxdb3 create token --all-databases
influxdb3 query "SELECT * FROM cpu LIMIT 5" --database metrics

# 处理引擎插件
influxdb3 enable --plugin downsample.py --trigger tables --database metrics
influxdb3 plugin list
```

---

## 4. Core vs Enterprise：选型必读

| 能力 | Core（开源） | Enterprise |
|---|---|---|
| 近期数据查询（WAL/内存/last cache） | ✅ 低延迟 | ✅ |
| 历史查询范围 | 受 `--query-file-limit`（默认 432 个 Parquet 文件，约 3 天@10min 粒度，可调大但吃内存） | ✅ Compactor 合并 + 完整历史 |
| 高可用 / 多节点 | ❌ 单进程 | ✅ |
| 后台 Compaction | ❌ | ✅ |
| 安全增强（审计等） | 基础 | ✅ |

> Core 的"72 小时"限制本质是**单次查询可触碰的 Parquet 文件数**（432），不是时间硬编码；写入持续、查询范围小的时候存得远不止 72h 的数据，只是查询受限。需要长期廉价保留 + 全量历史查询 → Enterprise 或自行把 Parquet 交给 DuckDB/Spark 生态。

---

## 5. 版本演进速览

- **2020–2024**：IOx 项目验证 Arrow/Parquet 路线；
- **2025.1** Alpha、**2025.3** Beta、**2025.4 GA**（Core + Enterprise）；
- 2025 内核迭代：last cache 优化、InfluxQL 兼容扩展、处理引擎插件丰富；
- **2026（Enterprise）**：新一代升级存储引擎（`.pt` 文件格式、column families、Gorilla/delta-delta 压缩回归时序特化路线，`--upgrade-pacha-tree` 平滑迁移）——解决 Parquet 通用格式在单序列高频查询上的短板；
- **Flux 于 3.x 正式终止**；v2→v3 迁移以 bucket→database 重写为主（官方迁移工具覆盖常见 Flux 查询）。

---

## 6. 常见坑与最佳实践

1. **Core 别当长期库用**：Core 定位是"最近数据引擎"，把 90 天查询需求压给 Core 必然撞文件数限制——要么 Enterprise，要么 Parquet 出湖给分析引擎；
2. **tag/field 选型习惯保留**：过滤列（tag）仍决定谓词下推效率；高基数不再是禁区但会放大存储；
3. **写入仍走 Line Protocol 批量化**：单点 HTTP 写入的性能浪费与 v2 时代相同；
4. **时间谓词必须带**：无 time 过滤的查询会扫全部文件——SQL 方言没变，代价还在；
5. **对象存储选型**：S3/R2/MinIO 均可（file 模式仅适合试验）；MinIO 是自托管常见组合；
6. **内存管理**：write buffer 与 last cache 常驻内存，容量规划按写入速率 × gen1 周期估算；
7. **Arrow 生态红利**：分析场景直接 DuckDB 查 Parquet、Python 用 Flight SQL 拿 Arrow 批——比 REST JSON 快一个量级；
8. **迁移策略**：双写并行（v2 + v3）观察查询等价性，Flux 查询用官方转换工具过一遍再人工修；
9. **版本跟进**：3.x 迭代快，升级前读 changelog（Core 的配置项偶有更名）。

---

## 7. 选型小结

| 场景 | 建议 |
|---|---|
| 新监控/可观测项目、想用 SQL | InfluxDB 3 Core 起步 |
| 高基数（成千上万维度组合）事件数据 | v3（v2 请直接跳过） |
| 长保留 + 全量历史 + HA | InfluxDB 3 Enterprise；或 Core + Parquet 外部分析 |
| 已有大量 Flux 资产 | 先评估迁移成本；v2.7 维护期内规划 |
| 纯指标采集 + PromQL | Prometheus/VictoriaMetrics 更对口（v3 不是 PromQL 替代品） |

一句话：**v3 用"开放格式 + 标准SQL"换掉了 v2 的"自研孤岛"，代价是近期/历史的分层架构需要你理解 Core 与 Enterprise 的边界**。
