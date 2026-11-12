# InfluxDB 2.x（OSS）技术介绍与使用指南

> 面向时序数据的开源数据库 v2 世代：TSM 存储引擎 + Flux 查询语言 + Bucket/Token 模型。
> 仓库：<https://github.com/influxdata/influxdb>　|　主线：2.7（维护期；后续演进为 3.x，见姊妹篇）

---

## 1. 项目简介

InfluxDB 是专门为**时序数据**（时间戳 + 指标 + 事件）设计的数据库，是 DevOps 监控、IoT 传感、金融tick 数据领域的老牌王者。v2（2020）相对 v1 的革新在于：

- **一体化平台**：数据库 + Web UI + 任务调度 + 告警（Alert）+ Dashboard 全部内置，单二进制；
- **Flux 查询语言**：函数式数据脚本语言，支持跨源 join、复杂转换；
- **Bucket / Org / Token 模型**：取代 v1 的 database/retention policy，权限以 API Token 精细化发放；
- 统一了 v1 时代 "influxdb + kapacitor + chronograf" 三件套的职责。

⚠️ **版本现状（重要）**：InfluxData 的开发重心已转向 v3（Rust 重写，见姊妹篇）。2.7 处于维护状态（安全与缺陷修复），Flux 语言在新版本中不再支持。**存量 2.x 集群仍大量运行于生产**（含云厂商托管版本），学习价值在于运维存量系统与理解时序模型；新项目建议评估 v3。

### 启动

```bash
docker run -d -p 8086:8086 \
  -v influxdb-data:/var/lib/influxdb2 \
  influxdb:2.7
# 打开 http://localhost:8086 完成初始化（org/bucket/admin token）
```

Go 客户端：`go get github.com/influxdata/influxdb-client-go/v2`

---

## 2. 数据模型

### 2.1 Line Protocol（行协议）

一切写入最终都是行协议文本：

```
measurement,tag1=v1,tag2=v2 field1=1.0,field2="x" 1735689600000000000
└─ 测量名     └─ 索引维度          └─ 数据值           └─ 纳秒时间戳(可省略)
```

- **measurement**：表概念的类比（如 `cpu`、`http_request`）；
- **tag**：字符串类型、**建立索引**，用于维度过滤（host、region、method）；
- **field**：实际数据值（float/int/string/bool），**不建索引**，是聚合计算的对象；
- **timestamp**：纳秒精度，默认取服务器时间。

### 2.2 Series 与基数（Cardinality）—— v2 的命门

```
Series Key = measurement + 所有 tag 的组合
基数 = 所有 series key 的去重总数
```

- 例：10 台主机 × 5 个 region × 60 个指标 = 3,000 series（健康）；
- 若把 `user_id`（100 万用户）当 tag → 基数爆炸，TSI 索引内存暴涨、写入变慢甚至 OOM——**v2 的"无限基数"做不到，高基数维度要么进 field 要么预先聚合**；
- OSS 2.x 无集群能力（单节点），企业版才有 HA——单机 + 高基数 = 最大风险组合。

### 2.3 Bucket / Organization / Token

- **Organization**：多租户顶层隔离；
- **Bucket**：数据库 + 保留策略合体（`my-bucket` 保留 30 天）；
- **Token**：按 org/bucket/读写粒度授权的 API 凭证，替代 v1 的用户名密码。

---

## 3. 存储与查询原理

### 3.1 TSM 引擎（Time-Structured Merge Tree）

```
写入 → WAL（写前日志）→ 内存 index(TSI) + cache
                          │ 定期 flush
                          ▼
              .tsm 文件（按时间分块、列式压缩，Level 层级 compaction）
```

- **写路径**：先进 WAL（顺序写保证持久性），随后 flush 成只读 `.tsm` 文件，后台按 level 合并压缩；
- **压缩算法**（时序数据的红利）：时间戳用 delta-of-delta、浮点用 Gorilla XOR 编码、字符串用 snappy——监控类数据常见 10x+ 压缩比；
- **TSI 索引**：倒排索引（tag → series），v2 内存占用的主要来源，也是基数敏感的根源；
- 查询：Flux/InfluxQL 编译为逻辑计划 → 按 series 定位 tsm 块 → 解压 → 聚合。

### 3.2 Flux 查询语言

```js
from(bucket: "metrics")
  |> range(start: -1h)                          // 时间范围必须显式指定
  |> filter(fn: (r) => r._measurement == "cpu")
  |> filter(fn: (r) => r._field == "usage_idle")
  |> aggregateWindow(every: 5m, fn: mean)       // 降采样
  |> yield()
```

- 管道式（`|>`）数据流语法，类似 Rx/F# 风格；
- 支持跨 bucket join、HTTP/SQL 外部源、自定义函数——表达力强但**学习曲线陡、生态孤岛**（这是 v3 弃用它的直接原因）；
- 兼容模式：v2 仍可用 InfluxQL（`SELECT mean("value") FROM ...`，v1 语法子集）。

### 3.3 Tasks：内置任务调度

```js
option task = {name: "downsample-5m", every: 5m}

from(bucket: "raw")
  |> range(start: -task.every)
  |> aggregateWindow(every: 5m, fn: mean)
  |> to(bucket: "agg-5m", org: "my-org")        // 原始桶保留 1d，聚合桶保留 90d
```

典型的**降采样（downsampling）管道**：raw bucket 短保留 + 聚合 bucket 长保留，控制存储成本。任务也用于告警（to() 到 notification endpoint）与数据修整。

---

## 4. 使用实战（Go）

### 4.1 写入

```go
client := influxdb2.NewClient("http://localhost:8086", "my-token")
defer client.Close()

wb := client.WriteAPI("my-org", "my-bucket")
defer wb.Flush()

// 写点 1：行协议
wb.WriteRecord("cpu,host=node1,region=cn usage_idle=91.2,usage_user=6.1")

// 写点 2：结构化 Point
p := influxdb2.NewPoint("http_request",
    map[string]string{"method": "GET", "path": "/api/orders", "status": "200"}, // tags
    map[string]interface{}{"duration_ms": 45.2, "bytes_out": 1024},              // fields
    time.Now())
wb.WritePoint(p)
```

- WriteAPI 异步批量（攒批 + 定时 flush）；**务必消费 `wb.Errors()` channel**，否则失败静默丢弃：

```go
go func() {
    for err := range wb.Errors() {
        log.Printf("write failed: %v", err)  // 重试/落盘
    }
}()
```

### 4.2 查询（Flux）

```go
qAPI := client.QueryAPI("my-org")
result, _ := qAPI.Query(context.Background(), `
from(bucket: "my-bucket")
  |> range(start: -1h)
  |> filter(fn: (r) => r._measurement == "cpu" and r.host == "node1")
  |> aggregateWindow(every: 5m, fn: mean)`)

for result.Next() {
    fmt.Println(result.Record().Time(), result.Record().Value())
}
if result.Err() != nil { log.Fatal(result.Err()) }
```

### 4.3 生态工具

- **Telegraf**：300+ 插件的采集代理（Prometheus/MySQL/Redis/网卡/自定义脚本 → InfluxDB），是 v2 事实上的标准数据入口；
- **v1 兼容 API**：`/write`、`/query` 支持 v1 协议与用户名密码——老代码无痛接入；
- **Grafana**：InfluxQL/Flux 双数据源；
- **influx CLI**：`influx bucket create -n raw -r 1d -o my-org`、`influx query '...'`。

---

## 5. 运维要点

| 主题 | 实践 |
|---|---|
| 保留策略 | raw 桶 1~7d + 聚合桶 30~90d，用 Task 落盘 |
| 基数监控 | UI/`influx query` 观察 `cardinality`，超十万级就要治理 |
| 备份 | `influx backup`（全量/增量，目录可加时间戳），定期 + 异地 |
| 资源 | 写入高峰看 WAL flush 压力；内存大头是 TSI 索引 |
| 升级 | 2.0→2.7 可原地升；**v2→v3 无原生迁移**，需按 bucket 导出/重写 |

---

## 6. 常见坑与最佳实践

1. **tag 与 field 混淆**：要过滤的维度用 tag（索引），要算的数值用 field；把高基数值（userId、traceId）放 tag 是 v2 头号事故来源；
2. **field 类型不可变**：同一 field 先写 float 再写 string 会报 field type conflict——上线前定好 schema；
3. **重复时间戳静默覆盖**：同 series 同 field 同时刻的写入后到者覆盖先到者；
4. **批量写**：单条 HTTP 写入既慢又费——用客户端攒批（默认 5000 点/批）；
5. **range 必写**：Flux 查询不写 range 会报错（防全表扫描，是保护不是缺陷）；
6. **单点风险**：OSS 2.x 无副本，机器挂了数据从最近 backup 恢复——重要监控建议双写（v1 兼容 API + Prometheus 远程写双通道）；
7. **Flux 的长期风险**：新代码尽量用 InfluxQL 子集或考虑直接上 v3，减少 Flux 资产沉淀。

---

## 7. v2 → v3 演进对照

| 维度 | v2.7 OSS | v3 Core/Enterprise |
|---|---|---|
| 语言实现 | Go | Rust |
| 存储引擎 | TSM（行式，自研） | 列式：WAL→内存→**Parquet**（对象存储） |
| 查询语言 | Flux + InfluxQL | **SQL**（DataFusion）+ InfluxQL；**Flux 已弃用** |
| 基数 | 受 TSI 限制 | 设计目标"无限基数" |
| 组织模型 | Org/Bucket/Token | Database（bucket 概念合并） |
| 部署 | 单二进制（OSS 单节点） | 单进程 Core + 多节点 Enterprise，S3 优先 |
| 定位 | 监控一体化平台 | 时序分析 + 事件数据平台 |

一句话：**存量 2.x 继续运维、逐步治理 Flux 资产；新时序项目从 v3 起步**。

---

## 8. 选型对比

v2 与 v3 的取舍已在上一节给出，这里把视角放宽到整个时序/监控数据选型。关键判断不是「谁更快」，而是**查询形态（标签过滤 + 时间窗口聚合 vs 通用分析）、基数规模、以及是否已有 Flux 资产**。

| 维度 | InfluxDB 2.7 OSS | InfluxDB 3 Core | Prometheus / VictoriaMetrics | TimescaleDB | ClickHouse |
|---|---|---|---|---|---|
| 数据模型 | Measurement + tag/field | Database + 列式表 | 指标 + label | 关系表 + hypertable | 列式大宽表 |
| 查询语言 | **Flux**（+ InfluxQL） | **SQL**（+ InfluxQL） | PromQL / MetricsQL | 完整 SQL | 完整 SQL |
| 写入协议 | Line Protocol / HTTP | Line Protocol v2 + Flight | 拉取（scrape）/ remote write | SQL INSERT | 批量 INSERT |
| 高基数（tag/label） | 受 TSI 限制，易膨胀 | 设计目标「无限基数」 | 同样怕高基数 | 较好（B-tree 索引） | **强项** |
| 长期存储 | 本地 TSM，单机 | Parquet + 对象存储 | 本地 + 可选远程 | 本地/分布式 | 本地/对象存储 |
| 集群与高可用 | **OSS 单节点** | Core 单进程 / Enterprise 多节点 | Prometheus 单机，VM 可集群 | 依赖 PG 生态 | 原生分布式 |
| 生态定位 | 监控一体化（含 Task/告警/UI） | 时序分析平台 | 云原生监控告警事实标准 | 已有 PG 栈的时序扩展 | 通用 OLAP / 日志分析 |
| 学习成本 | Flux 较陡且已弃用 | SQL 低 | PromQL 中等 | SQL 低 | SQL 低 |

一句话决策：

- **已有 2.x 存量且 Flux 任务/看板很多** → 继续运维 2.7，但**停止新增 Flux 资产**，按第 7 节规划迁移；
- **新项目、时序 + 高基数 + 对象存储优先** → [InfluxDB 3](../influxdb-v3/influxdb-v3-guide.md)；
- **纯 K8s 指标监控与告警** → Prometheus / VictoriaMetrics，不要拿 InfluxDB 去替代 scrape 生态；
- **团队已在 PostgreSQL 上、需要时序与业务表 JOIN** → TimescaleDB；
- **日志、用户行为、超大规模即席分析** → ClickHouse（它是 OLAP，不是时序数据库，但常被拿来兜底）。

> 选型时最容易踩的一条：把 InfluxDB 2.x 当「带 UI 的通用数据库」用。它是为「时间戳 + 标签过滤 + 窗口聚合」特化的；一旦出现多表 JOIN、事务、复杂关联查询，应尽早换关系型/OLAP 方案。
