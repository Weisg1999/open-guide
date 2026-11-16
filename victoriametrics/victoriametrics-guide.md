# VictoriaMetrics / Thanos 技术介绍与使用指南

> Prometheus 长期存储与全局查询的两条路线：二者解决的是同一个问题——本地 TSDB 保留期有限、单机不可扩展、多集群没有统一视图；但架构完全不同，VictoriaMetrics 是自研一体化时序库直接承接写入与查询，Thanos 是把 Prometheus 原生 TSDB block 搬进对象存储再叠一层联邦查询层。
> 上游：<https://github.com/VictoriaMetrics/VictoriaMetrics> · <https://github.com/thanos-io/thanos>　|　主线：VictoriaMetrics **v1.156.0**（1.x，2026-11-09）/ Thanos **v0.42.5**（0.42.x，2026-10-27），整理于 2026-11　|　协议：社区版均为 Apache-2.0（VictoriaMetrics 企业版功能另需商业许可；Thanos 为 CNCF Incubating 项目）

---

## 1. 项目简介

[Prometheus 篇](../prometheus/prometheus-guide.md)讲清了「怎么拉、怎么存、怎么查、怎么告」，但它的本地 TSDB 有三条硬边界，正是二者存在的理由：**保留期有限**（默认 `--storage.tsdb.retention.time=15d`，只落本地盘，无廉价归档）；**单机无副本无分片**（重启期间不可查、盘坏即丢，HA 靠跑两个实例各拉一份但查询时数据重复）；**联邦不等于长期存储**（上层只拉下层算好的瞬时值，不做历史归档、跨集群去重与全局 PromQL）。

| 共同问题 | VictoriaMetrics 的答案 | Thanos 的答案 |
|---|---|---|
| 存得久、存得省 | 自研 TSDB + 高压缩比，本地盘即可，一个二进制起全套 | block 上传对象存储（S3/MinIO/COS），近乎无限保留 |
| 多集群统一查询 | 数据进同一套 vmselect，天然全局 | Query 通过 Store API 联邦 Sidecar 与 Store Gateway |
| HA 去重 | 写入侧打标签、查询侧 `dedup` | `--query.replica-label` 在查询时合并副本 |
| 接入方式 | remote write 接收为主，也能自己抓（vmagent） | 外挂 Sidecar（pull 不动采集链路）或 Receive（push 收 remote write） |

**定位差异一句话**：VictoriaMetrics 是「换一个更强的存储与查询引擎」，可替掉 Prometheus 的远端；Thanos 是「给已有 Prometheus 加一层」，采集链路原封不动。前者求极简运维与资源效率，后者求零侵入与无限容量。

- **适用**：跨集群/跨地域统一视图、年级容量与 SLO 趋势、HA 去重、降低单机资源占用、多租户隔离
- **不适合**：单集群且保留期几周内（直接调大 `retention.time`）、日志与单条请求上下文（用 [Loki 篇](../loki/loki-guide.md)）、SQL 明细分析（用 [ClickHouse 篇](../clickhouse/clickhouse-guide.md)）、业务时序与精确计费（用 [InfluxDB v3 篇](../influxdb-v3/influxdb-v3-guide.md)）

> 对象存储是 Thanos 的地基，自建参照 [MinIO 篇](../minio/minio-guide.md)：bucket 容量、请求次数、跨区流量直接决定账单与查询延迟，这在 VictoriaMetrics 路线上是不存在的成本项。

---

## 2. 功能特性总览

| 维度 | VictoriaMetrics | Thanos |
|---|---|---|
| 核心思路 | 自研 TSDB 一体化替换/承接存储 | 复用 Prometheus TSDB block + 联邦查询层 |
| 部署形态 | 单节点 `victoria-metrics-prod` 单二进制；集群版 `vmstorage`/`vminsert`/`vmselect` | Sidecar / Store Gateway / Query / Query Frontend / Compactor / Ruler / Receive |
| 数据落点 | 本地盘或网络块存储（`-storageDataPath`） | 对象存储 bucket（TSDB block）+ 各组件本地缓存 |
| 写入协议 | remote write、Influx line、Graphite、OpenTSDB、OTLP、文本导入 | Sidecar 上传 block；Receive 收 remote write |
| 查询语言 | **MetricsQL**（PromQL 超集，兼容 Grafana） | 标准 **PromQL**（Prometheus Query API 兼容） |
| 保留期 | `-retentionPeriod`（如 `12months`），单机/集群统一语义 | Compactor 按分辨率设 raw/5m/1h 保留，理论无限 |
| 降采样 | 社区版无（企业版特性），靠高压缩比省空间 | **原生**：Compactor 生成 5m / 1h 降采样 block |
| HA 去重 | 写入侧标签 + 查询侧 `dedup` / 集群副本 | `--query.replica-label` 在 Query 层合并副本 |
| 多租户 | 集群版以 `accountID`/`projectID` 路径前缀隔离 | Receive + 租户标签 + hash 环，需自行规划 |
| 告警/预计算 | `vmalert`（规则 + Alertmanager） | `Ruler`（规则）+ Query Frontend（缓存/拆分） |
| 运维复杂度 | **低**：单节点零运维，集群版三类节点 | **高**：组件多 + bucket 生命周期 + compaction |
| 外部依赖 | 无（单节点）；集群版仅自身节点 | 必须有对象存储（可用 [MinIO](../minio/minio-guide.md)） |

---

## 3. 核心概念与原理

### 3.1 共同起点：Prometheus TSDB block

Thanos 搬运的就是 Prometheus 每 2 小时从内存 Head 落盘的不可变 block：

```text
<ULID>/
├── chunks/      # 压缩样本（XOR + 时间戳 delta-of-delta）
├── index        # 倒排索引：label=value -> series ID
├── meta.json    # ULID、[minTime,maxTime]、compaction 层级
└── tombstones   # 删除标记
```

block 自包含、不可变，天然适合整体上传与缓存——Thanos 全靠这一点；VictoriaMetrics 完全不复用 block，而把样本按自己的结构重排，故压缩率、查询路径、扩容方式从根上不同。

### 3.2 VictoriaMetrics：一体化存储与集群数据流

单节点把采集、存储、查询、UI（`vmui`）塞进一个二进制，靠 flag 配置；集群版拆成三类节点：

```text
 scrape / remote write ─► vmagent（抓取、relabel、WAL 队列）
                            │ remote write
                            ▼
 写入 ─► vminsert（无状态，按 accountID 一致性哈希选 vmstorage）
                            │
      ┌─────────────────────┼─────────────────────┐
      ▼                     ▼                     ▼
 vmstorage(shard)     vmstorage(shard)     vmstorage(shard) ← 唯一有状态、保存数据
      └─────────────────────┼─────────────────────┘
                            ▼
 查询 ─► vmselect（无状态，扇出、合并、去重）──► Grafana / vmalert
```

- `vmstorage` 有状态且不可随意替换，扩容要改 `-storageNode` 并做数据再平衡；`vminsert`/`vmselect`/`vmagent` 无状态，可随意水平扩。
- 多租户靠 URL 前缀：`/insert/42/prometheus/api/v1/write` 与 `/select/42/prometheus/api/v1/query` 中的 `42` 即 `accountID`；**集群只认数字 ID**，名称、鉴权、配额要放前置网关（vmauth）。
- `vmalert` 单独评估 recording/alerting rules 并回写结果、发 Alertmanager，相当于 Prometheus 的 rule evaluator + Thanos Ruler。

### 3.3 VictoriaMetrics：压缩、MetricsQL 与内存/磁盘取舍

高压缩比来自按 metricName 聚簇存储使重复标签字典化、时间戳与值分别 delta/XOR 变长编码、索引与数据按时间分区。代价是**用内存换磁盘**：活跃 series 索引、TSID 缓存、merge 缓冲区常驻内存，内存不足不会优雅降级而是 OOM。

- 容量规划要按 **active series 数**估内存，而非磁盘容量；`-retentionPeriod=12months` 是全局滚动删除，**按整月分区、非精确到秒**，社区版只有单一保留期（多级保留/自动降采样是企业版能力）。
- **MetricsQL** 是 PromQL 超集：`WITH` 模板、`rollup_*` 系列、`label_match`/`label_mismatch`、`*_over_time` 扩展。关键行为差异：`increase()`/`rate()` **不做外推**，同一 counter 在 VM 与 Prometheus/Thanos 上可能差几个百分点——迁移 SLO 阈值必须重新校准。

```promql
# MetricsQL 专属：WITH 模板函数抽取公共聚合
WITH (common = {job="app", env="prod"}, rps(m) = sum(rate(m[5m])) by (cluster))
rps(app_http_requests_total{common})
# rollup_* 系列：在整个 lookbehind 窗口上聚合而非逐点计算
rollup_rate(app_http_requests_total[5m])
# 纯 PromQL 子集：两条路线都能跑，Grafana 面板直接复用
sum by (cluster) (rate(app_http_requests_total[5m]))
```

### 3.4 Thanos：block 上传与 Store API 联邦查询

Thanos 不改采集，外挂一组组件，组件间用 gRPC **Store API** 通信：

```text
 Prometheus ──(本地 TSDB)──► Sidecar ──上传 block──► 对象存储 bucket
      │ Sidecar 同时把「近 2h 热数据」通过 StoreAPI 暴露（否则刚产生的数据查不到）
      │                                  │ 读历史 block
      │                                  ▼
      │                        Store Gateway（索引缓存 + 惰性加载 chunks）
      │                                  │ Store API (gRPC)
      └──────────► Query / Query Frontend（扇出全部后端、合并、去重）──► Grafana
```

- **Query Frontend** 在 Query 前做查询拆分、步骤对齐与结果缓存，是长区间查询必配；Grafana 只当它是一个 Prometheus 数据源。
- **push 形态**：用 **Receive** 替代 Sidecar，直接收 remote write、本地攒成 block 再上传，适合不想每集群跑 Prometheus 或跨网络边界；代价是丢了拉取模型自带的 `up` 健康探测，且要自己设计 hash 环与副本。

### 3.5 Thanos：downsampling、副本去重与 Compactor

**Compactor** 串行处理单个 bucket：压缩小 block、应用保留策略、生成降采样数据。

```text
 raw block（原始精度）──保留 --retention.resolution-raw
      ├─► 5m 降采样 ──保留 resolution-5m
      └─► 1h 降采样 ──保留 resolution-1h
 查 90 天曲线自动选 1h 数据：扫描样本降到 1/120，这是「长区间不炸」的根因
```

降采样保留 `count`/`sum`/`min`/`max`，`rate`、`histogram_quantile` 仍可算但精度下降，**不能替代原始数据**。**副本去重**：HA 两份数据标签只差 `prometheus_replica`，Query 用 `--query.replica-label=prometheus_replica` 声明「该标签不参与 series 身份」，合并时折叠成一条；漏配则所有曲线数值翻倍、告警误报，配合 `external_labels`（`cluster`/`region`）标全局唯一来源才不会误伤不同集群。

### 3.6 两种架构的本质分野

| | VictoriaMetrics | Thanos |
|---|---|---|
| 查询放大 | 扇出到 N 个 vmstorage，无对象存储往返，延迟稳定 | 扇出到 N 个 Sidecar + M 个 Store Gateway，长区间天然放大，靠 downsampling + Query Frontend 压制 |
| 成本结构 | 本地盘 + 内存，数据不出口，成本可预测 | 容量便宜，但 PUT/GET 请求费、跨区流量、索引缓存是隐性成本 |
| 运维复杂度 | 单节点零运维；集群版 3 类节点 | 5~7 类组件 + bucket 生命周期 + Compactor 单例约束 |
| 与 Prometheus 关系 | 可替换/承接，采集可用 vmagent 重写 | 严格外挂，复用原生 TSDB 与规则语义 |
| PromQL 兼容性 | MetricsQL 超集，行为细节有差异 | 原生 PromQL 语义，迁移成本最低 |

> 与 **Grafana Mimir / Cortex** 的边界：二者同属「remote write + 对象存储」家族，Mimir 主打原生多租户（**AGPLv3**，嵌入商业产品需法务评估），Cortex 是其上游前身（Apache-2.0，CNCF Incubating）。Thanos 与它们的根本区别是**是否保留每集群独立 Prometheus + block 上传路径**；VictoriaMetrics 根本区别是**不依赖对象存储**。Prometheus + 联邦只是层级聚合，三者都不提供长期归档与全局去重，不在同一能力档位。

---

## 4. 使用实战

### 4.1 部署：VictoriaMetrics（单节点 / 集群 / vmagent）

```bash
# 单节点：一个二进制起全套；查询 /api/v1/query，remote write 接收 /api/v1/write，UI /vmui
docker run -d --name vmsingle -p 8428:8428 -v vmdata:/data \
  victoriametrics/victoria-metrics:v1.156.0 \
  -retentionPeriod=12months -storageDataPath=/data
# 集群版：vmstorage 有状态，vminsert/vmselect 无状态
docker run -d --name vmstorage-1 -p 8400:8400 -p 8401:8401 \
  victoriametrics/vmstorage:v1.156.0-cluster -retentionPeriod=12months
docker run -d --name vminsert -p 8480:8480 \
  victoriametrics/vminsert:v1.156.0-cluster -storageNode=vmstorage-1:8400
docker run -d --name vmselect -p 8481:8481 \
  victoriametrics/vmselect:v1.156.0-cluster -storageNode=vmstorage-1:8401
# 多租户路径：/insert/42/prometheus/api/v1/write、/select/42/.../query（42=accountID）
# vmagent：抓取 + relabel + 带 WAL 的可靠 remote write（-promscrape.config 兼容 Prometheus 语法）
docker run -d --name vmagent -p 8429:8429 \
  victoriametrics/vmagent:v1.156.0 -promscrape.config=/etc/vmagent/scrape.yml \
  -remoteWrite.url=http://vmsingle:8428/api/v1/write -remoteWrite.tmpDataPath=/tmp/vmagent
```

### 4.2 部署：Thanos（Sidecar / Store / Query / Compactor）

```bash
# 对象存储用 MinIO（见 ../minio/minio-guide.md）
docker run -d --name minio -p 9000:9000 \
  -e MINIO_ROOT_USER=minio -e MINIO_ROOT_PASSWORD=minio123 minio/minio server /data
mc alias set local http://localhost:9000 minio minio123 && mc mb local/thanos
# Sidecar 传冷 block + 暴露近 2h 热数据；Store 读历史；Query 联邦；Compactor 单例
docker run -d --name thanos-sidecar --network host \
  -v "$(pwd)/prometheus-data:/prometheus" -v "$(pwd)/bucket.yml:/etc/thanos/bucket.yml" \
  quay.io/thanos/thanos:v0.42.5 sidecar --tsdb.path=/prometheus \
  --prometheus.url=http://localhost:9090 --objstore.config-file=/etc/thanos/bucket.yml
docker run -d --name thanos-store --network host \
  -v "$(pwd)/bucket.yml:/etc/thanos/bucket.yml" \
  quay.io/thanos/thanos:v0.42.5 store --data-dir=/var/thanos/store \
  --objstore.config-file=/etc/thanos/bucket.yml
docker run -d --name thanos-query --network host \
  quay.io/thanos/thanos:v0.42.5 query --http-address=0.0.0.0:10902 \
  --store=localhost:10901 --query.replica-label=prometheus_replica
docker run -d --name thanos-compactor --network host \
  -v "$(pwd)/bucket.yml:/etc/thanos/bucket.yml" \
  quay.io/thanos/thanos:v0.42.5 compact --data-dir=/var/thanos/compact \
  --objstore.config-file=/etc/thanos/bucket.yml \
  --retention.resolution-raw=30d --retention.resolution-5m=180d --retention.resolution-1h=2y
```

```yaml
# bucket.yml —— 所有访问对象存储的 Thanos 组件共用
type: S3
config:
  bucket: thanos
  endpoint: localhost:9000
  access_key: minio
  secret_key: minio123
  insecure: true
```

### 4.3 采集端：Prometheus 配置与 Go 应用埋点

```yaml
# prometheus.yml —— external_labels 必须全局唯一，否则 Thanos 分不清来源
global:
  scrape_interval: 15s
  external_labels: { cluster: cn-shanghai-a, prometheus_replica: replica-0 }
scrape_configs:
  - job_name: "app"
    static_configs:
      - targets: ["localhost:8080"]
remote_write:
  - url: http://vmsingle:8428/api/v1/write   # 路线 A：推给 vmsingle / vminsert
    queue_config: { max_samples_per_send: 10000, capacity: 20000 }
  # - url: http://localhost:10908/api/v1/receive  # 路线 B：推给 Thanos Receive（见 4.5）
```

```go
package main

import (
    "log"
    "net/http"

    "github.com/prometheus/client_golang/prometheus"
    "github.com/prometheus/client_golang/prometheus/collectors"
    "github.com/prometheus/client_golang/prometheus/promhttp"
)

func main() {
    reg := prometheus.NewRegistry()
    reg.MustRegister(collectors.NewGoCollector(),
        collectors.NewProcessCollector(collectors.ProcessCollectorOpts{}))
    requests := prometheus.NewCounterVec(prometheus.CounterOpts{
        Name: "app_http_requests_total",
        Help: "Total HTTP requests handled, labelled by route and status.",
    }, []string{"route", "code"})
    reg.MustRegister(requests)
    mux := http.NewServeMux()
    mux.HandleFunc("/api/users", func(w http.ResponseWriter, r *http.Request) {
        requests.WithLabelValues("/api/users", "200").Inc() // 用路由模板，勿用 r.URL.Path
        w.WriteHeader(http.StatusOK)
        _, _ = w.Write([]byte("ok"))
    })
    mux.Handle("/metrics", promhttp.HandlerFor(reg, promhttp.HandlerOpts{}))
    log.Println("listening on :8080, metrics at /metrics")
    log.Fatal(http.ListenAndServe(":8080", mux))
}
```

> 用独立 `Registry` 而非默认注册表避免重复注册 panic；标签只放有界枚举，见 [Prometheus 篇](../prometheus/prometheus-guide.md)。批处理补数可走 VM 的 `/api/v1/import/prometheus` 文本导入端点，无需 protobuf/snappy。

### 4.4 Go：一套代码查询两条路线的全局视图

```go
package main

import (
    "fmt"
    "io"
    "log"
    "net/http"
    "net/url"
)

// vmsingle / vmselect / Thanos Query 都兼容 Prometheus /api/v1/query，
// 因此同一段代码可无缝切换后端，拿到跨集群的全局结果。
func query(endpoint, expr string) (string, error) {
    u := endpoint + "/api/v1/query?query=" + url.QueryEscape(expr)
    resp, err := http.Get(u)
    if err != nil {
        return "", err
    }
    defer resp.Body.Close()
    b, err := io.ReadAll(resp.Body)
    return fmt.Sprintf("%s %s", resp.Status, b), err
}

func main() {
    expr := `sum by (cluster) (rate(app_http_requests_total[5m]))`
    for _, ep := range []string{"http://localhost:8428", "http://localhost:10902"} {
        out, err := query(ep, expr)
        if err != nil {
            log.Fatal(err)
        }
        fmt.Println(ep, out)
    }
}
```

### 4.5 Thanos Receive（push 形态）与告警组件

```bash
# Receive 直接收 remote write 替代 Sidecar（hash 环见 receive.yml）
docker run -d --name thanos-receive --network host \
  -v "$(pwd)/bucket.yml:/etc/thanos/bucket.yml" -v "$(pwd)/receive.yml:/etc/thanos/receive.yml" \
  quay.io/thanos/thanos:v0.42.5 receive --tsdb.path=/var/thanos/receive \
  --grpc-address=0.0.0.0:10907 --http-address=0.0.0.0:10908 \
  --remote-write.address=0.0.0.0:10908/api/v1/receive \
  --objstore.config-file=/etc/thanos/bucket.yml \
  --receive.replication-factor=1 --receive.hashrings-file=/etc/thanos/receive.yml
# 规则评估：vmalert（VM）/ Ruler（Thanos），规则语法与 Prometheus 一致（recording/alerting + for）
docker run -d --name vmalert --network host \
  -v "$(pwd)/rules.yml:/etc/vmalert/rules.yml" \
  victoriametrics/vmalert:v1.156.0 -datasource.url=http://localhost:8428 \
  -notifier.url=http://localhost:9093 -rule=/etc/vmalert/rules.yml
docker run -d --name thanos-ruler --network host \
  -v "$(pwd)/rules.yml:/etc/thanos/rules.yml" \
  quay.io/thanos/thanos:v0.42.5 rule --data-dir=/var/thanos/rule \
  --rule-file=/etc/thanos/rules.yml --alertmanagers.url=http://localhost:9093 \
  --query=localhost:10902 --label=cluster="cn-shanghai-a"
curl -s 'http://localhost:8428/api/v1/query?query=up'        # 查询
thanos tools bucket inspect --objstore.config-file=bucket.yml # block/降采样/保留状态
thanos tools bucket verify  --objstore.config-file=bucket.yml # 重叠/损坏 block
```

```yaml
# receive.yml —— hash 环按 tenant 标签路由
- tenant: default
  endpoints: [localhost:10907]
```

---

## 5. 版本演进与要点

| 组件 | 阶段 | 关键变化与迁移注意点 |
|---|---|---|
| VictoriaMetrics | 1.0x ~ 1.4x | 单节点成熟、集群版三节点定形；MetricsQL、vmagent、vmalert、vmbackup 工具链补齐 |
| **VictoriaMetrics** | **1.15x（2026）** | 主线 **v1.156.0**（2026-11-09）：企业版改为 `-eula` 接受协议、`-license*` 废弃；vmagent 支持按 `-remoteWrite.url` 独立持久化队列；修复 stream aggregation 乱序、cgroup v1 资源探测 |
| Thanos | 0.2x ~ 0.3x | Sidecar + Store + Query 基本盘；Compactor 引入 5m/1h 降采样；Query Frontend 查询拆分 |
| **Thanos** | **0.4x（2026）** | 主线 **v0.42.5**（2026-10-27）：0.42 重点在 Receive 多租户写入、按 endpoint 配置、租户生命周期；Query Frontend 复用低步长缓存；修复 exemplar 跨层 external label 丢失与 gRPC 安全公告 |

**主线版本与许可（2026-11 核对）**：VictoriaMetrics 主线 **1.x**，最新稳定 **v1.156.0**；Thanos 主线 **0.x**，最新稳定 **v0.42.5**，仍未发布 1.0，属 CNCF Incubating。两者社区版均 **Apache-2.0**，Thanos/Cortex 亦为 Apache-2.0，而 **Grafana Mimir 为 AGPLv3**，嵌入对外产品需法务评估。VictoriaMetrics **企业版专有**：多级保留期、原生降采样、异常检测、自动备份、LTS，需商业许可，切勿写进社区版方案。升级要点：VM 各组件版本必须配套；Thanos 所有组件必须同一版本；remote write 2.0 规范 2026 年仍为实验性、跨后端支持不一致，生产以 1.0 协议为准。若上述版本查不到，一律以「截至 2026-11 主线」表述，不臆造版本号。

---

## 6. 常见坑与最佳实践

1. **单机够用却硬上分布式**：单集群、周级保留直接调大 `--storage.tsdb.retention.time` 最省事。**对策**：先确认真的需要全局视图/年计保留再引入。
2. **`-retentionPeriod` 单位写错**：写成 `12` 或 `12d` 语义都不对。**对策**：用 `12months`/`2y` 带单位写法，并知道删除按整月分区、非精确到秒。
3. **Thanos 漏配唯一 `external_labels`**：两个集群都叫 `cluster=prod` 会被当成同一来源。**对策**：每个 Prometheus 写全局 `external_labels`，上线前 `thanos tools bucket inspect` 核对。
4. **HA 双副本数据翻倍**：漏了 `--query.replica-label`，`rate()` 直接翻倍、告警全误报。**对策**：所有副本用同一 replica 标签名，Query 与 Ruler 都声明，用 `count(up) by (cluster)` 验证。
5. **长区间查询直接打 Query**：一年曲线把请求放大到全部 block，拖垮 Store Gateway。**对策**：必配 Compactor 降采样 + Query Frontend，面板限制最大时间范围，raw 只用于短窗口排障。
6. **只算容量不算请求费与流量**：Store Gateway 反复拉索引/chunks 产生大量 GET。**对策**：留足本地缓存盘、监控 bucket 操作数与跨区流量、同地域部署（见 [MinIO 篇](../minio/minio-guide.md)）。
7. **把 VM 当成「省内存」方案**：高压缩比是**用内存换磁盘**，索引与缓存常驻内存。**对策**：按活跃 series 数估内存，监控 `vm_*` 自监控指标，OOM 前先扩内存或减基数。
8. **高基数标签照搬**：`user_id`/`trace_id`/原始 URL 做标签，两条路线都会爆。**对策**：标签只放有界枚举，明细进日志/链路（[OpenTelemetry 篇](../opentelemetry/opentelemetry-guide.md)），查询侧用 `-search.maxSeries` 限流兜底。
9. **单节点与集群端点混用**：集群版必须带 `/insert/<accountID>/...` 前缀，直接 POST `/api/v1/write` 会 404。**对策**：配置时明确区分 vmsingle 与 vminsert/vmselect 路径，accountID 由网关注入。
10. **多租户只靠路径、无鉴权**：集群只认数字 `accountID` 不校验身份。**对策**：前置 vmauth/API 网关做认证、配额与租户映射，别把 vmselect 直接暴露给租户。
11. **把 MetricsQL 搬到 Thanos/Grafana**：`WITH`、`rollup_*`、`label_match` 在标准 PromQL 直接语法错。**对策**：公共面板坚持用 PromQL 子集，VM 专属扩展只放 VM 数据源并注明不可移植。
12. **`increase`/`rate` 跨路线对不上**：MetricsQL 不外推，同一 counter 差几个百分点。**对策**：迁移 SLO 阈值重新校准，别把告警表达式在两条路线间原样复制。
13. **remote write 无队列、抖动丢数据**：不落盘即内存排队。**对策**：开启 WAL/持久化队列（`-remoteWrite.tmpDataPath`、`queue_config`），监控 `prometheus_remote_storage_*` 失败与积压。
14. **Compactor 多实例抢同一 bucket**：会损坏 block；Sidecar 跨集群写同一 bucket 也会重叠。**对策**：每 bucket 只跑一个 Compactor，用 relabel 或独立 bucket 隔离租户，定期 `bucket verify`。
15. **拿它存日志/明细/精确计费**：时序库为聚合监控设计。**对策**：日志走 [Loki 篇](../loki/loki-guide.md)、明细走 [ClickHouse 篇](../clickhouse/clickhouse-guide.md)、业务时序走 [InfluxDB v3 篇](../influxdb-v3/influxdb-v3-guide.md)。
16. **不看自监控**：查询变慢、block 上传滞后、去重失效都不会主动报错。**对策**：给各组件配抓取与告警（compactor 进度、store 延迟、bucket 错误率、query 并发），共用 Alertmanager。

---

## 7. 选型对比

| 维度 | VictoriaMetrics | Thanos | Grafana Mimir | Cortex | Prometheus + 联邦 |
|---|---|---|---|---|---|
| 架构 | 一体化自研 TSDB | block 上传 + StoreAPI 联邦 | remote write + 对象存储 | remote write + 对象存储 | 层级聚合，无长期存储 |
| 采集链路 | vmagent 可重写 | 沿用原生 Prometheus | 沿用原生 Prometheus | 沿用原生 Prometheus | 原生 |
| 对象存储 | **不需要** | **必须** | 必须 | 必须 | 不需要 |
| 降采样 | 企业版 | **原生 5m/1h** | 无 | 无 | 无 |
| 查询语言 | MetricsQL | 标准 PromQL | PromQL / MQE | PromQL | PromQL |
| 多租户 | 集群版 accountID 原生 | Receive + 标签规划 | **原生最完整** | 原生 | 无 |
| 运维复杂度 | **最低** | 高 | 高 | 高 | 低 |
| 许可 | Apache-2.0（企业版另计） | Apache-2.0 | AGPLv3 | Apache-2.0 | Apache-2.0 |

按决策维度落点：

| 决策维度 | 倾向 | 理由 |
|---|---|---|
| **单机够用**（单集群、周级保留） | 都别上，调大 `retention.time` | 引入分布式系统是无谓成本 |
| **需要全局视图 + 想少运维** | **VictoriaMetrics** | 无对象存储，单二进制/vmselect 即全局查询 |
| **已有对象存储 / 已在跑 Prometheus 且不想改采集** | **Thanos** | Sidecar 外挂零侵入，block 归档无限保留 |
| **已有对象存储 + 强多租户硬隔离** | Grafana Mimir（注意 AGPLv3） | 租户、配额、限流最完整 |
| **多租户但想避开 AGPLv3** | VM 集群版 / Thanos Receive + 前置网关 | Apache-2.0，租户映射自己掌控 |
| **极致压缩比、内存充足、成本敏感** | **VictoriaMetrics** | 高压缩比，数据不出本地盘 |
| **超长区间（年级）历史查询** | **Thanos** | 唯一原生降采样，长区间扫描成本最低 |

一句话决策结论：**想用最少组件、最低资源把 Prometheus 的存储和全局查询一次做好 → VictoriaMetrics；已重度依赖 Prometheus + 对象存储、要保持采集链路与 PromQL 语义原封不动 → Thanos**。二者不是「谁更好」，而是「换引擎」与「加一层」两条方向相反的路。

何时不该用：

- **单机就能满足**（单集群、保留期 < 2 个月、无跨集群查询）：直接调大 Prometheus 本地保留期，别引入任何一套。
- **只有对象存储、却没有跨集群/长期需求**：Thanos 的组件与 bucket 治理成本收不回来。
- **要 SQL 分析、明细事件、日志检索或精确计费**：四条路线都是聚合型时序监控，明细分别走 [ClickHouse](../clickhouse/clickhouse-guide.md)、[Loki](../loki/loki-guide.md)、[InfluxDB v3](../influxdb-v3/influxdb-v3-guide.md)。
- **对 AGPLv3 敏感且要把监控系统嵌入对外产品**：避开 Mimir，回到 Apache-2.0 的 VM / Thanos / Cortex 并让法务确认。
- **没有对象存储运维能力、也不想自建**（[MinIO 篇](../minio/minio-guide.md)）：优先 VM 单节点或托管云监控，别硬扛 bucket 生命周期。

> 落地顺序：先统一 `cluster`/`replica` 标签规范与 [OpenTelemetry](../opentelemetry/opentelemetry-guide.md) Resource → 打通 remote write → 配好去重与降采样 → 再上全局面板与告警。**先看标签与保留期，再选引擎**，绝大多数「查询慢/数据翻倍/账单爆炸」都是这两件事没定好。

参考：<https://docs.victoriametrics.com/> · <https://thanos.io/> · <https://github.com/VictoriaMetrics/VictoriaMetrics/releases> · <https://github.com/thanos-io/thanos/releases> · 版本与许可核对截至 2026-11-13，具体以上游最新 release 为准。
