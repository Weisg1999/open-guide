# Grafana Loki 技术介绍与使用指南（Go 日志检索）

> 只索引标签、不索引正文的日志系统：用对象存储 + 轻量标签索引把日志成本压到 ES 的零头，靠 LogQL 与 Zap / OpenTelemetry 打通排障链路。
> 上游：<https://github.com/grafana/loki>　|　主线：**3.x**（整理于 2026-11）　|　采集端：Grafana Alloy / Fluent Bit / OTel Collector（**Promtail 已进入维护期**）
> 协议：AGPLv3；具体版本以上游最新 release 为准。

---

## 1. 项目简介

[Zap 篇](../zap/zap-guide.md) 解决「应用怎么输出结构化日志」，[Elasticsearch 篇](../elasticsearch/elasticsearch-guide.md) 解决「日志怎么被全文检索」，Loki 走的是第三条路：**不给日志正文建倒排索引，只给标签建索引**。

这个取舍换来的是成本：日志原始数据压缩后直接扔进对象存储，索引只有「哪些标签组合对应哪些 chunk」。代价是**按正文搜索是查询时的暴力扫描**，必须先靠标签把范围缩到足够小。

- **适用场景**：K8s / 容器日志集中检索、与 Prometheus + Grafana 组成统一可观测栈、日志量大但查询以「某个服务/某段时间」为主、成本敏感
- **不适合**：需要任意关键词的毫秒级全文检索（用 [ES](../elasticsearch/elasticsearch-guide.md)）、需要复杂相关性与排序、日志内容要做聚合分析报表（用 [ClickHouse](../clickhouse/clickhouse-guide.md)）、高基数字段（trace_id、user_id）必须作为检索维度

> 与 [Prometheus 篇](../prometheus/prometheus-guide.md) 是同一套标签哲学：指标用 `metric{labels}`，日志用 `{labels}`。**两者标签规范统一，`service.name` / `app` / `env` 一致，才能从告警曲线一键跳到日志**。

### 安装与本地起服务

```bash
docker run --rm -d --name loki -p 3100:3100 \
  -v "$(pwd)/loki-config.yaml:/etc/loki/local-config.yaml" \
  grafana/loki:3 make local-config
curl -G http://localhost:3100/ready
```

### 最小可运行示例

```go
// 应用侧唯一要做的事：把结构化日志写到 stdout，交给采集器
package main

import (
    "log/slog"
    "os"
)

func main() {
    h := slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: slog.LevelInfo})
    logger := slog.New(h).With(
        slog.String("service.name", "checkout-api"), // 与 Prometheus / OTel 统一
        slog.String("env", "prod"),
    )
    logger.Info("order created",
        slog.String("order_id", "A1001"),
        slog.String("trace_id", "4bf92f3577b34da6a3ce929d0e0e4736"),
        slog.Int("amount", 12800))
}
```

```logql
{app="checkout-api"} |= "order created" | json | amount > 10000
```

> ⚠️ **Loki 标签名只接受 `[a-zA-Z_][a-zA-Z0-9_]*`，不能含点**：OTel 语义约定里的 `service.name` 必须在采集端映射成 `app` / `service_name`，否则无法作为标签。正文里保留原名即可。
>
> 用 [Zap 篇](../zap/zap-guide.md) 的 `zap.NewProduction()` 或 [slog 篇](../slog/slog-guide.md) 都可以，**关键是输出 JSON 到 stdout**，别写文件——容器日志由采集器统一收走。

---

## 2. 功能特性总览

| 能力 | 说明 |
|---|---|
| 索引模型 | **只索引标签**；标签集唯一确定一条「流（stream）」，正文不建倒排 |
| 存储 | chunk（压缩的日志块）放对象存储（S3 / GCS / Azure / [MinIO](../minio/minio-guide.md)）；index（TSDB）记录 label → chunk 映射 |
| 写入路径 | Distributor → Ingester（内存 chunk + WAL）→ flush 到对象存储 |
| 查询路径 | Query Frontend / Querier：按标签定位 chunk → 下载 → 查询时过滤正文，可并行与分片 |
| 查询语言 | **LogQL**：流选择器 + 行过滤器 + 解析器 + 标签/行格式化；另有指标查询 |
| 解析器 | `json` / `logfmt` / `regexp` / `pattern` / `unpack`，在查询时把正文提取成临时标签 |
| 指标能力 | `rate` / `count_over_time` / `sum by(...)` 直接对日志算速率，可生成告警 |
| 生命周期 | compactor 合并索引、retention 按流/全局过期删除、schema 分版本演进 |
| 多租户 | `X-Scope-OrgID` 租户隔离；`auth_enabled: true` 时必填 |
| 部署形态 | 单二进制（all-in-one，开发）/ **可伸缩微服务模式**（生产） |
| 生态 | Grafana 数据源、logcli、Alertmanager 规则、原生 OTLP 日志摄入（3.x） |

---

## 3. 核心概念与原理

### 3.1 只索引标签：为什么便宜

```
一行日志 = 时间戳 + 标签集 + 正文

{app="checkout", env="prod", level="info"}  → 一条 stream
{"msg":"order created","order_id":"A1001","trace_id":"..."}  → 正文（不建索引）

索引里只有：这组标签 → 哪些 chunk（含时间范围）
正文全部压缩进 chunk，躺在对象存储里
```

- **写放大极小**：不需要像 ES 那样为正文分词、构建倒排、维护 doc_values
- **存储成本低一个数量级**：压缩日志块 + 便宜的对象存储
- **代价**：查 `|= "error"` 时要把候选 chunk 拉出来做 grep 式过滤；没有标签剪枝就等于全量扫描

所以 Loki 的性能公式是：**候选 chunk 数量 ≈ 标签选择度 × 时间范围**。标签选得越宽、时间越长，查询越慢。

### 3.2 数据路径

```
采集器 (Alloy / Fluent Bit)
   │ 批量 push  /loki/api/v1/push
   ▼
Distributor  ──校验、按 stream 哈希、按复制因子分发──►
   ▼
Ingester     内存构建 chunk + WAL（防崩溃丢失）
   │ 达到 chunk 大小 / 超时 / flush 触发
   ▼
对象存储 (chunks) + Index (TSDB, 记录 label→chunk)
   │
   ▼
Compactor    合并索引、执行 retention 删除过期数据
```

### 3.3 查询路径与两段式 LogQL

```
Query Frontend（拆分时间片、缓存、并发限制）
   ▼
Querier  ── 用流选择器查 index，缩小到 chunk 集合 ──► 对象存储
   ▼ 下载 chunk，解压，逐行应用过滤器 / 解析器
结果返回
```

LogQL 查询由两段组成：

```logql
{app="checkout", env="prod"}          ← ① 流选择器：只用标签，走索引（最关键）
  |= "error" !~ "healthz"             ← ② 行过滤器：扫描正文，廉价但不过索引
  | json                              ← ③ 解析器：把 JSON 字段提取成临时标签
  | level="error" | duration_ms > 500 ← ④ 标签过滤（解析后才可用）
  | line_format "{{.msg}}"            ← ⑤ 输出格式化
```

**把能下推的条件尽量放进 ①**，是 Loki 查询优化的第一原则。解析器里的字段不能直接当流选择器用（它们不在索引里）。

### 3.4 与 Elasticsearch 的根本差异

| | Loki | Elasticsearch |
|---|---|---|
| 索引对象 | 标签（元数据） | 正文分词 + doc_values |
| 写入成本 | 低（追加压缩块） | 高（分词、构建倒排、refresh/merge） |
| 存储成本 | **低**（对象存储 + 压缩） | 高（多份索引结构 + JVM 堆） |
| 任意关键词检索 | 慢（查询时扫描） | **快** |
| 相关性排序 / 聚合 | 弱 | **强** |
| 运维复杂度 | 中（对象存储 + 多组件） | 高（JVM 调优、分片、ILM） |

一句话：**Loki 是「按服务/时间找日志」，ES 是「按内容找日志」**。多数排障场景属于前者，所以 Loki 在 K8s 日志场景性价比更高。

### 3.5 高基数：Loki 的头号事故

**每个唯一的标签组合 = 一条独立 stream**，每条 stream 都有自己的 chunk 序列与内存开销。把 `trace_id`、`user_id`、`request_id`、原始 URL 做成标签，会瞬间生成几百万条流：

- Ingester 内存被打满、chunk 切得极碎
- 索引膨胀、查询变慢
- 触发 `per_stream_rate_limit` / `max_streams_per_user` 后开始丢日志

正确做法：**高基数字段留在正文**（用 `| json | trace_id="..."` 在查询时过滤），标签只放低基数枚举（`app`、`env`、`level`、`cluster`、`namespace`、`pod` 的模板名）。

### 3.6 结构化元数据（structured metadata）

3.x 支持把 `trace_id` 这类高基数字段作为「结构化元数据」随 chunk 存储但**不进索引**——既能随日志保留、可在查询里过滤，又不会造成流爆炸。这是替代「正文解析」与「高基数标签」的第三条路，也是接 [OpenTelemetry 篇](../opentelemetry/opentelemetry-guide.md) 的推荐方式。

---

## 4. 使用实战

### 4.1 本地配置（开发用）

```yaml
# loki-config.yaml
auth_enabled: false
server:
  http_listen_port: 3100
common:
  path_prefix: /loki
  storage:
    filesystem:
      chunks_directory: /loki/chunks
      rules_directory: /loki/rules
  replication_factor: 1
  ring:
    kvstore:
      store: inmemory
schema_config:
  configs:
    - from: 2024-01-01
      store: tsdb
      object_store: filesystem      # 生产改 s3 / s3-compatible
      schema: v13
      index:
        prefix: index_
        period: 24h
limits_config:
  retention_period: 168h           # 7 天；不设会无限增长
compactor:
  working_directory: /loki/compactor
  retention_enabled: true
```

> 生产必须改三处：`object_store` 换成 S3 / [MinIO](../minio/minio-guide.md)、`replication_factor: 3`、`ring.kvstore` 换 consul/etcd 并启用可伸缩部署模式。`filesystem + inmemory ring` 只能用于本地。

### 4.2 采集：Grafana Alloy（Promtail 的继任者）

```alloy
discovery.docker "containers" {
  host = "unix:///var/run/docker.sock"
}

loki.source.docker "default" {
  host       = "unix:///var/run/docker.sock"
  targets    = discovery.docker.containers.targets
  forward_to = [loki.process.json.receiver]
}

loki.process "json" {
  stage.json {
    // 左边是 Loki 合法标签名，右边是日志正文里的字段路径
    expressions = { level = "level", msg = "msg", app = "service.name" }
  }
  stage.labels {
    values = { level = "", app = "" }       // 只提升低基数字段为标签
  }
  forward_to = [loki.write.local.receiver]
}

loki.write "local" {
  endpoint {
    url = "http://loki:3100/loki/api/v1/push"
  }
}
```

> K8s 环境通常用 Alloy 的 `discovery.kubernetes` + `loki.source.kubernetes` 自动附加 `namespace` / `pod` / `container` 标签；**注意 pod 名带随机后缀时基数会随发布次数增长**，用 `pod` 的 owner（Deployment 名）更稳。多行日志（Java 异常栈）要在采集端配置 `stage.multiline` 合并。

### 4.3 LogQL 常用查询速查

```logql
# 某服务最近的错误（先用标签剪枝，再过滤正文）
{app="checkout-api", env="prod"} |= "error" !~ "healthz"

# 解析 JSON 后按字段过滤（字段在正文，不在索引）
{app="checkout-api"} | json | level="error" | duration_ms > 500

# 提取指定 trace_id 的全链路日志（呼应 OpenTelemetry 篇）
{app=~"checkout-api|payment-api"} | json | trace_id="4bf92f3577b34da6a3ce929d0e0e4736"

# 从日志生成指标：每分钟 5xx 错误数
sum(count_over_time({app="checkout-api"} | json | status >= 500 [1m])) by (app)

# 错误的速率（可直接做告警规则）
sum(rate({app="checkout-api"} |= "level=error" [5m])) by (app)

# 正则提取 + 格式化输出
{app="nginx"} | regexp `(?P<ip>\S+) .* (?P<status>\d{3})` | status >= 500
              | line_format "{{.ip}} -> {{.status}}"
```

### 4.4 告警规则

```yaml
# rules/fake/rules.yaml
groups:
  - name: app-logs
    rules:
      - alert: HighErrorLogRate
        expr: sum(rate({app="checkout-api"} | json | level="error" [5m])) > 1
        for: 10m
        labels: {severity: warning}
        annotations:
          summary: "checkout-api error logs > 1/s for 10m"
          dashboard_url: "http://grafana/explore?..."   # 跳回日志
```

Loki Ruler 把结果发给 Alertmanager，与 [Prometheus 篇](../prometheus/prometheus-guide.md) 的告警共用同一套分组/静默策略。

### 4.5 Go 侧：查询 Loki（标准库即可）

```go
package logs

import (
    "context"
    "encoding/json"
    "fmt"
    "net/http"
    "net/url"
    "time"
)

type Result struct {
    Data struct {
        Result []struct {
            Stream map[string]string `json:"stream"`
            Values [][]string        `json:"values"` // [ts, line]
        } `json:"result"`
    } `json:"data"`
}

func Query(ctx context.Context, lokiURL, query string, since time.Duration) (*Result, error) {
    q := url.Values{}
    q.Set("query", query)
    q.Set("limit", "100")
    q.Set("since", since.String())
    req, err := http.NewRequestWithContext(ctx, http.MethodGet,
        lokiURL+"/loki/api/v1/query_range?"+q.Encode(), nil)
    if err != nil { return nil, err }
    resp, err := http.DefaultClient.Do(req)
    if err != nil { return nil, err }
    defer resp.Body.Close()
    if resp.StatusCode != http.StatusOK {
        return nil, fmt.Errorf("loki query: %s", resp.Status)
    }
    var out Result
    return &out, json.NewDecoder(resp.Body).Decode(&out)
}
```

> 应用一般**不要直接向 Loki 推日志**（会让 Loki 成为业务链路的同步依赖）。正确做法是 stdout + 采集器异步上报，与 [Docker/K8s 篇](../docker-k8s/docker-k8s-guide.md) 的「容器只写 stdout」一致。

### 4.6 从日志跳到链路

在 Grafana 里给 Loki 数据源配置 **derived field**：从 `trace_id` 正则提取值，指向 Tempo / Jaeger 数据源。这样在日志行上点一下就能打开 [OpenTelemetry](../opentelemetry/opentelemetry-guide.md) 的完整调用链——三支柱闭环的最后一步。

```bash
# 命令行排查
logcli query '{app="checkout-api"} |= "error"' --since=1h --limit=50
logcli labels app
```

---

## 5. 版本演进速览

| 阶段 | 关键变化与迁移注意点 |
|---|---|
| 2.0~2.8 | 索引从 BoltDB Shipper 迁到 **TSDB**、`schema v12/v13`；LogQL 增加 `pattern` / `unpack` 解析器；结构化元数据引入；查询并行化 |
| **3.x** | **原生 OTLP 日志摄入**（可由 OTel Collector 直推）、查询性能与缓存改进、结构化元数据进 GA、规则与 retention 增强 |
| 采集端 | **Promtail 进入维护期**，官方推荐 **Grafana Alloy**；存量 Promtail 配置需迁移到 Alloy 的 River 语法 |

**升级注意**：

- `schema_config` 的 `store` / `schema` / `object_store` 变更需要新增一段 `from` 日期配置，**不能直接改历史段**，否则旧数据查不到
- Promtail → Alloy 不是改镜像名，配置文件语法完全不同
- 大版本升级前读官方 upgrade guide，尤其涉及 index schema 与 compactor 行为的部分
- 采集端、Loki、Grafana 版本建议同代，跨太多版本可能出现 LogQL 语法或 API 不兼容

---

## 6. 常见坑与最佳实践

1. **高基数标签**：把 `trace_id` / `user_id` / `request_id` / 原始 URL 做标签 → 流爆炸、内存打满、丢日志。高基数字段放正文或结构化元数据，标签只放低基数枚举。
2. **正文当标签**：`stage.labels` 里提升动态字段，等于把每条日志变成一条新流。采集端要显式白名单。
3. **拿 Loki 做全文搜索引擎**：`{app=~".*"} |= "xxx"` 是全量扫描。先用标签把范围缩到单个服务 + 短时间窗。
4. **查询必带流选择器**：LogQL 要求至少一个标签匹配器；`{}` 或只写 `|=` 会报错或被拒。
5. **时间范围过大 / 无 `limit`**：一次拉 30 天日志会打爆 Querier。拆分时间片、加 `limit`、用 Query Frontend 的并发与缓存限制。
6. **保留策略没配**：不设 `retention_period` + 不开 `compactor.retention_enabled`，对象存储无限增长。
7. **本地 filesystem 后端上生产**：单点、无法水平扩展。用 S3 / [MinIO](../minio/minio-guide.md) 等对象存储 + 可伸缩部署模式。
8. **单二进制模式上生产**：`replication_factor: 1` + `inmemory ring` 无高可用。生产用微服务模式 + 3 副本 + 外部 KV。
9. **乱序写入 / 时间戳错误**：采集器时间未同步或解析了错误的时间字段，会触发 `entry out of order` 丢弃。统一 NTP，多行日志正确设置时间戳。
10. **多行日志没合并**：异常栈被拆成几十条独立日志，检索与计数全错。在采集端配 `stage.multiline`。
11. **单行过大 / 单行超长**：超 `max_line_size` 会被截断或丢弃；大 payload 应只记 ID 与摘要，别把整个请求体写进日志（参见 [Zap 篇](../zap/zap-guide.md)）。
12. **ingestion rate limit 误配**：`ingestion_rate_mb` / `per_stream_rate_limit` 过小会在流量高峰静默丢日志。压测并监控 `loki_discarded_samples_total`。
13. **日志不是结构化 JSON**：纯文本导致每次查询都要 `regexp` 解析，慢且易错。应用侧输出 JSON，字段名与 [slog 篇](../slog/slog-guide.md) 的词典统一。
14. **标签规范与 Prometheus 不一致**：指标叫 `service_name`、日志叫 `app`，Grafana 里无法联动。给 OTel 的 `service.name` 定一条固定映射（如统一映射为 `app`），并让指标、日志、链路三套系统用同一套 `app` / `env` / `cluster` 值。
15. **采集器不带 K8s 元数据**：查不到「哪个 Pod / 哪个 namespace」，排障价值大减。用 `discovery.kubernetes` 自动附加。
16. **敏感信息进日志**：Loki 长期留存且检索方便，密码/token/身份证必须脱敏（采集端 `stage.replace` 或应用侧过滤）。
17. **把 Loki 当指标或链路存储**：趋势与告警用 [Prometheus](../prometheus/prometheus-guide.md)，跨服务调用链用 [OpenTelemetry](../opentelemetry/opentelemetry-guide.md)；Loki 的 metric query 只适合从日志临时算速率。

---

## 7. 选型对比

| 维度 | Loki | Elasticsearch / OpenSearch | ClickHouse（日志） | Graylog | 云日志服务 |
|---|---|---|---|---|---|
| 索引策略 | **只索引标签** | 全文倒排 + doc_values | 列存 + 跳过索引 | ES 之上封装 | 托管 |
| 存储成本 | **最低**（对象存储 + 压缩） | 高 | 低～中 | 高 | 按量计费 |
| 写入吞吐 | 高 | 高 | **极高** | 中 | 高 |
| 全文检索 | 弱（查询时扫描） | **最强** | 中（SQL like / tokenbf） | 强 | 中～强 |
| 聚合分析 | 弱（LogQL 指标查询） | 强 | **最强（SQL）** | 强 | 中 |
| 运维复杂度 | 中 | 高（JVM） | 中 | 中高 | **极低** |
| 生态联动 | **Grafana / Prometheus 原生** | Kibana / OpenSearch Dashboards | 自建看板 | 自带 | 厂商生态 |
| 典型场景 | K8s 日志、成本敏感 | 业务搜索 + 日志检索 | 日志分析报表 | 传统日志平台 | 不想自建 |

一句话决策：

- **已用 Prometheus + Grafana、日志主要按服务/时间查、想省钱** → **Loki**，与现有栈几乎零摩擦。
- **既要业务全文检索又要日志、需要相关性与复杂聚合** → [Elasticsearch](../elasticsearch/elasticsearch-guide.md)；许可要求纯 Apache-2.0 → OpenSearch。
- **日志主要做 SQL 聚合报表、量级极大** → [ClickHouse](../clickhouse/clickhouse-guide.md)。
- **团队小、完全不想运维** → 云日志服务，用成本换人力。

> 落地顺序建议：先统一应用侧的 JSON 字段词典（[Zap](../zap/zap-guide.md) / [slog](../slog/slog-guide.md)）→ 定死低基数标签规范 → 配好采集器与多行合并 → 再开 retention 与告警。**标签规范一旦定错，后面迁移历史数据的代价极高。**
