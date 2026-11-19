# Prometheus 技术介绍与使用指南（Go 可观测性）

> 云原生指标监控的事实标准：拉取模型 + 多维标签时序 + PromQL + Alertmanager，把「发生了什么」变成可查询、可聚合、可告警的数字。
> 上游：<https://github.com/prometheus/prometheus>　|　服务端主线：**3.x**（整理于 2026-11）　|　Go 客户端：[prometheus/client_golang](https://github.com/prometheus/client_golang) v1.x
> 协议：Apache-2.0；具体版本以上游最新 release 为准。

---

## 1. 项目简介

[Zap 篇](../zap/zap-guide.md)解决「单条事件的细节」，[OpenTelemetry 篇](../opentelemetry/opentelemetry-guide.md)解决「一次请求跨了哪些服务」，**Prometheus 解决「系统在一段时间内的整体趋势与是否该告警」**。三者是可观测性的三条支柱，不能互相替代——把「QPS 涨了三倍」写进日志再靠正则去数，是最常见的反模式。

Prometheus 的核心模型：**服务端主动按间隔拉取（pull）应用暴露的 `/metrics` 文本端点，把带标签的时间序列存进本地 TSDB，用 PromQL 查询，用 Alertmanager 发告警**。

- **适用场景**：服务健康度与 SLO、延迟/错误率/饱和度（RED / USE 方法）、容量趋势、基于阈值的告警、K8s 原生监控
- **不适合**：单条请求的详细上下文（用日志）、跨服务调用链（用链路追踪）、高基数明细（用户 ID、订单号、trace_id 不能做标签）、长期低成本归档（用 remote write 接 Thanos / Mimir / VictoriaMetrics）、事件型精确计费（拉取模型有采样间隔）

### 安装与本地起服务

```bash
go get github.com/prometheus/client_golang/prometheus
go get github.com/prometheus/client_golang/prometheus/promhttp
go get github.com/prometheus/client_golang/prometheus/collectors
```

```yaml
# prometheus.yml
global:
  scrape_interval: 15s
  evaluation_interval: 15s
scrape_configs:
  - job_name: "app"
    static_configs:
      - targets: ["host.docker.internal:8080"]
```

```bash
docker run --rm -p 9090:9090 \
  -v "$(pwd)/prometheus.yml:/etc/prometheus/prometheus.yml" \
  prom/prometheus
# 浏览器打开 http://localhost:9090 → Status → Targets 看是否 UP
```

### 最小可运行示例

```go
package main

import (
    "net/http"

    "github.com/prometheus/client_golang/prometheus/promhttp"
)

func main() {
    http.Handle("/metrics", promhttp.Handler()) // 默认注册表的 Go/进程指标
    http.ListenAndServe(":8080", nil)
}
```

---

## 2. 功能特性总览

| 能力 | 说明 |
|---|---|
| 数据模型 | `metric_name{label="value", ...} value timestamp`，标签是多维度的核心 |
| 四种指标类型 | Counter（单调累计）/ Gauge（可增可减）/ Histogram（服务端分桶）/ Summary（客户端分位数） |
| 采集方式 | 拉取为主；短生命周期任务用 Pushgateway；服务发现支持 K8s / Consul / DNS / 文件 |
| 查询 | PromQL：即时向量、区间向量、`rate` / `irate` / `increase`、聚合 `sum by` / `topk`、`histogram_quantile` |
| 规则 | Recording rules（预计算）、Alerting rules（条件 + `for` 持续时长） |
| 告警 | Alertmanager 负责去重、分组、静默、抑制、路由到邮件/企业微信/PagerDuty |
| 存储 | 本地 TSDB：WAL → Head block → 2h block → 压缩合并；倒排索引按标签定位 series |
| 扩展 | Remote write / read 接长期存储；联邦；Thanos / Mimir / VictoriaMetrics 做全局视图与 HA |
| 关联 | Exemplar 把 `trace_id` 挂到指标样本上，打通到链路追踪 |
| Go SDK | `Registry` / `Collector` / `Gatherer`、`promhttp` Handler、中间件生态、`testutil` 单测断言 |

---

## 3. 核心概念与原理

### 3.1 一条时间序列是怎么标识的

```text
http_requests_total{method="GET", route="/api/users", code="200"}  1024  @1730000000
└──── 指标名 ────┘ └──────────── 标签集（series 的身份）───────────┘ └值┘ └时间戳┘
```

**指标名 + 标签集的每一种组合 = 一条独立 series**。这是理解 Prometheus 的第一原则，也是头号事故的来源：

```text
用户 ID 做标签 → 100 万用户 × 10 条指标 = 1000 万 series → 内存与索引爆炸
```

series 数量 = 各标签基数的**笛卡尔积**。标签必须是**有界、低基数**的枚举值（method、route 模板、status code、实例名），不能是 `user_id`、原始 URL、`trace_id`、时间戳。

### 3.2 为什么是拉取（pull），不是推送（push）

| | 拉取（Prometheus） | 推送（StatsD 等） |
|---|---|---|
| 应用 | 只需暴露一个 HTTP 端点，不感知监控后端地址 | 需配置后端地址、处理失败与背压 |
| 健康探测 | `up{job=...}` 天然是「目标是否活着」 | 需额外心跳 |
| 流量控制 | 服务端集中控制采集频率、可随时调 | 客户端决定，故障时易形成风暴 |
| 短板 | 短生命周期任务、NAT/防火墙后的目标 | — |

短任务（cron job、批处理）用 **Pushgateway** 补，但它不是「代理」：Pushgateway 会一直保留最后一次推送的值，任务结束**必须显式删除对应分组**，否则会永远上报一个僵死的指标。

### 3.3 四种指标类型与 `rate()`

| 类型 | 语义 | 典型用途 | 注意 |
|---|---|---|---|
| Counter | 只增不减（重启归零） | 请求总数、错误总数、处理字节数 | **永远不要直接画原始值**，用 `rate()`；Prometheus 能识别重启归零并补偿 |
| Gauge | 可增可减 | 在途请求数、队列深度、内存占用、温度 | 采集间隔内的突变会被漏掉 |
| Histogram | 按预设桶累计计数 + `_sum` + `_count` | 延迟分布、P99、SLO | 精度由桶边界决定；**可跨实例聚合** |
| Summary | 客户端直接算分位数 | 单实例精确分位数 | **分位数不可跨实例聚合**，多副本场景基本不用 |

```promql
# Counter 的正确用法：算每秒速率，再按维度聚合
sum by (route) (rate(http_requests_total[5m]))

# Histogram 估算 P99（桶边界决定误差，必须保留 le 标签）
histogram_quantile(0.99,
  sum by (le, route) (rate(http_request_duration_seconds_bucket[5m]))
)
```

> `[5m]` 是**区间向量**，窗口至少要覆盖 4 个采集间隔（15s 间隔时 5m 很合适）。窗口太短会因样本不足导致 `rate` 抖动甚至无数据。

### 3.4 TSDB 内部：为什么标签查询这么快

```text
写入 ──► WAL（防崩溃丢失）──► Head（内存 chunk，约 2h）
                                  │ 每 2h 落盘为一个 block
                                  ▼
                     block: chunks + 倒排索引(label→series) + tombstones
                                  │ 后台 compaction 合并小 block
                                  ▼
                        更大的 block（可配置保留期）
```

- 样本值用 XOR + 变长编码压缩，时间戳用 delta-of-delta；单样本约 1~2 字节
- **倒排索引**把 `label="value"` 映射到 series ID 集合，所以 `sum(rate(...{code="500"}))` 不用扫全量数据
- 代价是：series 越多，倒排索引与内存 Head 越大 → 高基数直接拖垮 TSDB

### 3.5 staleness 与采样间隔

目标抓取失败或实例下线后，Prometheus 会在 5 分钟（staleness marker）后认为该 series 停止，图线中断而不是沿用旧值。这解释了「Pod 重启后曲线出现断口」和「`rate()` 在刚启动时样本不足返回空」两个常见现象。

### 3.6 高基数：是什么、怎么打爆、怎么发现

**基数（cardinality）= 某个标签不同取值的数量**；一个指标的 series 数 = 各标签基数的**乘积**。`method`（4）× `route`（50）× `code`（10）= 2000 条，完全可控；一旦混进 `user_id`（100 万），立刻变成 20 亿条。

| 标签 | 基数 | 可用 | 说明 |
|---|---|---|---|
| `method` / `code` | 个位数 ~ 几十 | ✅ | 有界枚举 |
| `route`（模板） | 几十 ~ 几百 | ✅ | 必须是 `/users/:id`，不是 `/users/123` |
| `instance` | 与副本数同阶 | ✅ | 有界，但弹性伸缩会让它持续增长 |
| 原始 URL / 带 query | 无界 | ❌ | `?utm_source=...` 每个来源一条 series |
| `user_id` / `order_id` | 无界 | ❌ | 明细属于日志 / 链路 / OLAP |
| `trace_id` / `request_id` | 无界 | ❌ | 用 exemplar 关联，不做标签 |

**四个典型爆炸源**，事故基本都能归到其中之一：

1. **把业务主键当标签**：「加个 `user_id` 方便按用户查」——这一句话能让 TSDB 在几小时内 OOM。按用户维度的分析是 OLAP 的活，不是 Prometheus 的活。
2. **`trace_id` / `request_id` 做标签**：每请求一条 series，写完即成垃圾，倒排索引却永久膨胀。
3. **URL 未模板化**：`path` 直接取 `r.URL.Path`，`/users/123`、`/users/124` 各算一条。
4. **错误信息当标签**：`err.Error()` 值域无限，且天然带随机 ID。

**怎么发现**——先在表达式浏览器里按指标名数 series：

```promql
# 每个指标名有多少条 series，取前 20
topk(20, count by (__name__) ({__name__=~".+"}))

# 某个可疑指标内部，哪个 user_id 贡献了最多 series
topk(10, count by (user_id) (http_requests_total))
```

再查 TSDB 自带的统计接口，不用装任何插件：

```bash
curl -s localhost:9090/api/v1/status/tsdb | jq '.data.headStats.numSeries'
curl -s localhost:9090/api/v1/status/tsdb | jq '.data.seriesCountByMetricName[:5]'
```

`numSeries` 就是 Head 中的活跃 series 数，**内存与它近似线性**：每条 1~3 KB（chunk + 索引 + 标签符号表 + 抓取缓冲），100 万条约 2~3 GB 常驻，再叠加查询临时内存与 compaction 峰值。它是容量规划的第一指标。

**已经进去了怎么救**：只能在抓取入库前丢，事后删不掉历史索引。

```yaml
scrape_configs:
  - job_name: "app"
    metric_relabel_configs:
      - source_labels: [__name__]
        regex: "debug_.*"
        action: drop            # 整条指标不要
      - regex: "user_id|request_id|trace_id"
        action: labeldrop       # 只摘标签，指标保留
      - source_labels: [path]
        regex: "/users/[0-9]+"
        target_label: path
        replacement: "/users/:id"
```

`metric_relabel_configs` 在**入库前**执行，是唯一真正能减少 series 的地方；`labeldrop` 会让原本不同的 series 合并成一条，但这不是「相加」而是**丢弃**：同一时间点标签集相同的样本只保留第一条，其余静默丢弃（计入 `prometheus_target_scrapes_sample_duplicate_timestamp_total`）。counter 的 reset 语义随之被破坏，`rate()` 会给出偏高的荒谬值——所以这是纯风险，不是收益。

---

## 4. 使用实战

### 4.1 自定义 Registry（推荐做法）

```go
package metrics

import (
    "net/http"

    "github.com/prometheus/client_golang/prometheus"
    "github.com/prometheus/client_golang/prometheus/collectors"
    "github.com/prometheus/client_golang/prometheus/promhttp"
)

type Metrics struct {
    Requests *prometheus.CounterVec
    Duration *prometheus.HistogramVec
    InFlight prometheus.Gauge
}

func New(reg *prometheus.Registry) *Metrics {
    m := &Metrics{
        Requests: prometheus.NewCounterVec(prometheus.CounterOpts{
            Name: "http_requests_total",
            Help: "Total number of HTTP requests.",
        }, []string{"method", "route", "code"}),
        Duration: prometheus.NewHistogramVec(prometheus.HistogramOpts{
            Name:    "http_request_duration_seconds",
            Help:    "HTTP request latency in seconds.",
            Buckets: []float64{.005, .01, .025, .05, .1, .25, .5, 1, 2.5, 5},
        }, []string{"method", "route"}),
        InFlight: prometheus.NewGauge(prometheus.GaugeOpts{
            Name: "http_requests_in_flight",
            Help: "Current number of in-flight HTTP requests.",
        }),
    }
    reg.MustRegister(
        m.Requests, m.Duration, m.InFlight,
        collectors.NewGoCollector(),                       // GC / goroutine / 内存
        collectors.NewProcessCollector(collectors.ProcessCollectorOpts{}),
    )
    return m
}

func Handler(reg *prometheus.Registry) http.Handler {
    return promhttp.HandlerFor(reg, promhttp.HandlerOpts{
        EnableOpenMetrics: true, // 支持 exemplar
    })
}
```

> 用独立 `Registry` 而不是 `prometheus.DefaultRegisterer`：避免第三方库重复注册导致 `MustRegister` panic，也便于单测隔离。

### 4.2 埋点中间件（接 [Gin 篇](../gin/gin-guide.md)）

```go
import "github.com/gin-gonic/gin"

func Instrument(m *Metrics) gin.HandlerFunc {
    return func(c *gin.Context) {
        start := time.Now()
        m.InFlight.Inc()
        defer m.InFlight.Dec()

        c.Next()

        // 关键：用路由模板而不是 Request.URL.Path，
        // 否则 /users/123 每个用户 ID 都会生成一条 series
        route := c.FullPath() // "/users/:id"
        if route == "" { route = "unmatched" }
        code := strconv.Itoa(c.Writer.Status())
        m.Requests.WithLabelValues(c.Request.Method, route, code).Inc()
        m.Duration.WithLabelValues(c.Request.Method, route).
            Observe(time.Since(start).Seconds())
    }
}
```

### 4.3 自定义 Collector（从业务状态实时取值）

```go
type QueueCollector struct {
    depth *prometheus.Desc
    q     *Queue
}

func NewQueueCollector(q *Queue) *QueueCollector {
    return &QueueCollector{
        depth: prometheus.NewDesc("app_queue_depth", "Pending jobs.",
            []string{"queue"}, prometheus.Labels{"service": "worker"}),
        q: q,
    }
}
func (c *QueueCollector) Describe(ch chan<- *prometheus.Desc) {
    ch <- c.depth
}
func (c *QueueCollector) Collect(ch chan<- prometheus.Metric) {
    for name, n := range c.q.Depths() {
        ch <- prometheus.MustNewConstMetric(c.depth, prometheus.GaugeValue,
            float64(n), name)
    }
}
```

> `Collect` 在每次抓取时同步调用，**不能做慢查询**；耗时超过 `scrape_timeout` 会导致整个 target 抓取失败。

### 4.4 常用 PromQL 速查

```promql
# QPS（按路由）
sum by (route) (rate(http_requests_total[5m]))

# 5xx 错误率
sum(rate(http_requests_total{code=~"5.."}[5m]))
  / sum(rate(http_requests_total[5m]))

# P99 延迟
histogram_quantile(0.99, sum by (le) (rate(http_request_duration_seconds_bucket[5m])))

# 错误最多的 5 个路由
topk(5, sum by (route) (rate(http_requests_total{code=~"5.."}[5m])))

# 实例是否存活
up{job="app"} == 0
```

### 4.5 告警规则与 Alertmanager

```yaml
# rules.yml
groups:
  - name: app
    rules:
      - record: job:http_requests:rate5m   # Recording rule：预计算降本
        expr: sum by (job) (rate(http_requests_total[5m]))
      - alert: HighErrorRate
        expr: |
          sum(rate(http_requests_total{code=~"5.."}[5m]))
            / sum(rate(http_requests_total[5m])) > 0.05
        for: 10m                            # 持续 10 分钟才触发，避免抖动误报
        labels: {severity: warning}
        annotations:
          summary: "5xx ratio above 5% for 10 minutes"
```

```yaml
# prometheus.yml 追加
rule_files: ["rules.yml"]
alerting:
  alertmanagers:
    - static_configs:
        - targets: ["localhost:9093"]
```

### 4.6 单测断言

```go
func TestCounter(t *testing.T) {
    reg := prometheus.NewRegistry()
    m := New(reg)
    m.Requests.WithLabelValues("GET", "/ping", "200").Inc()

    err := testutil.GatherAndCompare(reg, strings.NewReader(`
# HELP http_requests_total Total number of HTTP requests.
# TYPE http_requests_total counter
http_requests_total{code="200",method="GET",route="/ping"} 1
`), "http_requests_total")
    if err != nil {
        t.Fatalf("metric mismatch: %v", err)
    }
}
```

### 4.7 PromQL 进阶：子查询、`@` 与聚合陷阱

**子查询 `[range:step]`** 是「对一个表达式的计算结果再做区间运算」。它很贵：外层每个 step 都要把内层整个窗口重算一遍。

```promql
# 最近 1 小时内，5xx 比例的 5 分钟滑动最大值
max_over_time(
  (sum(rate(http_requests_total{code=~"5.."}[5m]))
   / sum(rate(http_requests_total[5m])))[1h:1m]
)
```

`[1h:1m]` = 过去 1 小时、每 1 分钟取一个点，内层表达式会被算 60 次。成本随 `窗口/步长` 线性增长，**告警规则里出现子查询通常意味着该上 Recording rule 了**。

**`@` 与 `offset`**：`@` 把求值时刻钉死在某个 Unix 时间戳，`offset` 是相对偏移，可叠加。

```promql
# 与当前值对比 1 小时前
sum(rate(http_requests_total[5m])) - sum(rate(http_requests_total[5m] offset 1h))

# 钉住结束时刻再往前挪一天 = 昨天同一时刻的同一段窗口
sum(rate(http_requests_total[5m] @ end() offset 1d))
```

注意 `@` 修饰的是**求值时刻**而非样本时间，`offset` 在它之上生效；顺序反过来含义就变了。

**`rate` vs `irate`**：前者用窗口首尾两点做外推，后者只看最后两个点。

| | `rate(d[5m])` | `irate(d[5m])` |
|---|---|---|
| 用几个点 | 窗口内全部（首尾外推） | 最后两个 |
| 平滑度 | 平滑，适合画图与告警 | 灵敏，看瞬时尖刺 |
| 风险 | 窗口内 counter reset 会低估 | 抓取抖动被放大成假尖峰 |
| 告警 | ✅ | ❌ 值会跳，容易误报 |

一次慢抓取就能让 `irate` 翻倍。**任何带 `for` 的告警都不该用 `irate`。**

**`sum` 与 `rate` 的顺序**：永远先 `rate` 再 `sum`。

```promql
# 正确：每条 series 各自求速率，再按维度求和
sum by (route) (rate(http_requests_total[5m]))

# 错误：多条 counter 先相加再求速率
rate(sum by (route) (http_requests_total)[5m:1m])
```

多条 counter 相加后，任意一条 reset 都会在合并曲线上造成下跳，`rate` 会把它当作一次 counter reset 补偿进去，结果系统性偏大（`rate` 从不返回负值）。先 `rate` 时每条 series 的 reset 由 Prometheus 单独补偿，结果才正确。

**缺失告警：`absent()` 与 `unless`**：阈值只能发现「值不对」，发现不了「数据没了」。

```promql
# 整个 job 一条 series 都没有 → 返回 1
absent(up{job="app"})

# 用 unless 表达「现在有、5 分钟前没有」
sum(up{job="app"}) unless sum(up{job="app"} offset 5m)
```

`absent()` 对「部分实例消失」无能——只要还剩一条 series 就不为空，这种情况要靠 `up == 0` 或 `count` 兜底。

**`histogram_quantile` 必须保留 `le`**：`le` 是桶边界，丢了它分位数无从计算。

```promql
# 正确
histogram_quantile(0.99, sum by (le, route) (rate(http_request_duration_seconds_bucket[5m])))

# 错误：sum by (route) 把 le 抹平，输出无意义
histogram_quantile(0.99, sum by (route) (rate(http_request_duration_seconds_bucket[5m])))
```

分位数也**不能先算再平均**：`avg(histogram_quantile(...))` 不是「全局 P99」。要全局值就把所有实例的桶加在一起再算。

**`topk` 不能直接做告警**：榜上成员随时换人，超标的那个今天在、明天掉出去，告警就会「自己好了」。`topk` 只用于看板展示；告警要固定维度写在 `by` 里。

### 4.8 告警规则设计：`for` 的误用与分级

**`for` 不是「持续多久」，而是「连续多少次求值为真」**。每个 `evaluation_interval` 评估一次，中间任何一次为假就清零重来。

| 现象 | 原因 |
|---|---|
| 配 `for: 5m` 却十几分钟才响 | 表达式在 true/false 之间跳，计时反复重置 |
| `for: 10s` 形同虚设 | 小于 2× `evaluation_interval`，最坏只评估一次 |
| 反复 firing / resolved | 值在阈值附近抖动，`for` 抵不住 |

经验值：**`for` ≥ 3~4 倍 `evaluation_interval`**，且表达式自身先用较长的 `rate` 窗口平滑。`interval: 1m` + `for: 1m` 基本等于没有。

```yaml
groups:
  - name: app-slo
    interval: 30s                     # 覆盖全局，评估更密
    rules:
      - alert: HighErrorRate
        expr: |
          sum(rate(http_requests_total{code=~"5.."}[5m]))
            / sum(rate(http_requests_total[5m])) > 0.05
        for: 5m                       # 连续 10 次为真（30s × 10）
        keep_firing_for: 5m           # 2.42+：恢复后仍保持 firing，压抖动
        labels:
          severity: page
        annotations:
          summary: "{{ $labels.job }} 5xx 比例 {{ $value | humanizePercentage }}"
          runbook_url: "https://runbook.internal/alert/HighErrorRate"
          dashboard_url: "https://grafana.internal/d/app?var-job={{ $labels.job }}"
```

`keep_firing_for`（Prometheus 2.42+）解决的是另一半问题：值刚跌破阈值就 resolved、一分钟后又 firing，通知渠道被刷屏。它要求**持续恢复**一段时间才真正 resolved。

**分级**：`severity` 不是装饰，它直接决定通知渠道与响应时限。

| 级别 | 含义 | 通知渠道 | 响应 | 典型规则 |
|---|---|---|---|---|
| `page` | 用户已受影响 / SLO 在烧 | 电话、PagerDuty、值班群 @人 | 立即 | 错误率 > 5% 持续 5m、核心接口全挂 |
| `ticket` | 有风险但用户无感 | 工单、邮件 | 一个工作日 | 磁盘 80%、副本不足、证书 30 天到期 |
| `warning` | 趋势 / 容量类 | 群消息，不 @人 | 观察 | QPS 同比翻倍、P99 缓慢上升 |

原则：**`page` 必须少**。一个 page 告警若经常被静默，说明阈值或分级错了，而不是值班的人不上心。

**`expr` 里要避开的写法**：

- **不要 `topk`**：成员会换，告警会自己 resolved。要监控维度超标就把维度写进 `by`。
- **不要裸 `absent`**：粒度太粗容易整片误报，用 `absent(metric{job="app"})` 并配合 `unless`。
- **不要塞子查询**：每次评估全量重算，几十条规则叠加足以拖垮 Prometheus。

**Recording rule 是告警的成本开关**：被多个面板 / 多条告警复用的表达式，录成指标后只读结果。

```yaml
- record: job:http_requests_error_ratio:rate5m
  expr: |
    sum by (job) (rate(http_requests_total{code=~"5.."}[5m]))
      / sum by (job) (rate(http_requests_total[5m]))
```

**注解里必须有 runbook**：告警只说「出了什么事」，runbook 说「怎么办」。没有 runbook 的 page 级告警，等于把压力原样丢给凌晨三点的人。

### 4.9 容量与性能：series 估算、抓取调优与何时换后端

**第一步是数 series**，而不是拍脑袋加内存：

```bash
curl -s localhost:9090/api/v1/status/tsdb | jq '.data.headStats.numSeries'
```

```promql
prometheus_tsdb_head_series                                 # 当前活跃 series
sum(rate(prometheus_tsdb_head_samples_appended_total[5m]))   # 每秒写入样本数
prometheus_tsdb_storage_blocks_bytes / 1024 / 1024 / 1024    # 落盘 block 大小（GB）
```

**内存换算**：Head 中每条 series 约 **1~3 KB**（chunk + 索引 + 标签符号表），按 3 KB 做上限估算：

| series 数 | Head 内存（≈3KB/series） | 结论 |
|---|---|---|
| 10 万 | ~300 MB | 单机轻松 |
| 100 万 | ~3 GB | 4~8 GB 堆，需盯 GC |
| 500 万 | ~15 GB | 接近单机上限，考虑分片 |
| > 1000 万 | > 30 GB | 单 Prometheus 不划算，上 VM / Mimir |

这是稳态占用；查询临时内存、WAL replay、compaction 峰值会再叠一层，**规划时至少留 2 倍余量**。

**抓取参数三件套**：

| 参数 | 作用 | 约束 |
|---|---|---|
| `scrape_interval` | 抓取周期，决定采样密度 | 直接决定 `samples/s = series / interval` |
| `scrape_timeout` | 单次抓取超时 | **必须 < `scrape_interval`**，否则会层层叠加超时 |
| `evaluation_interval` | 规则评估周期 | 与 `for` 配合，见 4.8 |

100 万 series、15s 间隔 ≈ 6.7 万 samples/s，这个量级已接近单机可持续上限；把间隔缩到 5s 就是三倍压力。**缩间隔前先算这笔账。**

**保留期与磁盘**：

```bash
--storage.tsdb.retention.time=15d
--storage.tsdb.retention.size=500GB   # 双保险，先到先触发
```

磁盘 ≈ `samples/s × 86400 × 保留天数 × 1~2 字节`（压缩后），6.7 万 samples/s × 15 天约 100 GB 量级。**只配 `retention.time` 不够**：磁盘会先满，然后 Prometheus 崩溃。

**remote write 的背压**：接长期存储后，队列落后是第一报警信号。

```promql
prometheus_remote_storage_highest_timestamp_in_seconds
  - prometheus_remote_storage_queue_highest_sent_timestamp_seconds   # 落后秒数
rate(prometheus_remote_storage_samples_dropped_total[5m])            # > 0 即事故
rate(prometheus_remote_storage_samples_failed_total[5m])             # 发送失败
```

落后持续增大且 `dropped` 上升，说明后端消费不动，Prometheus 会丢样本保本地。此时该扩后端或加 shard，而不是盲目调大 `max_shards` 硬顶。

**什么时候该换 VictoriaMetrics**：单机 series 上千万、保留期要几个月、remote write 长期追不上——这些是单进程 Prometheus 的结构性瓶颈。VM 兼容 PromQL（MetricsQL 是超集），同等数据量下内存与磁盘通常只有几分之一，参见 [VictoriaMetrics 篇](../victoriametrics/victoriametrics-guide.md)。但**别一上来就换**：先清高基数标签、补 Recording rule，多数「Prometheus 撑不住」其实是基数问题，不是引擎问题。

---

## 5. 版本演进速览

| 阶段 | 关键变化与迁移注意点 |
|---|---|
| 1.x | 早期版本，存储为自研 v1 TSDB，大规模下内存与性能吃紧 |
| **2.x** | **TSDB v2 重写**（WAL + block + 倒排索引），性能与压缩比数量级提升；新增 Recording rules 优化、`subquery`、远程读写接口。2.x 是长期主流 |
| **3.x** | 支持 **UTF-8 指标名/标签名**（旧版强制 `[a-zA-Z_:][a-zA-Z0-9_:]*`）；OTLP 接收与 remote write 2.0 增强；部分 flag、API 与默认行为调整 |

**升级注意**：

- 2.x → 3.x 前**务必读官方迁移指南**，逐个核对已废弃的命令行参数与 API；UTF-8 名称放宽是「允许」而非「推荐」，命名仍应遵守语义约定
- `client_golang` v1.x API 长期稳定，但 `collectors` 子包、`promhttp` 的 `HandlerOpts` 字段随小版本演进；升级时一起升
- **服务端版本与 Go 客户端版本要分别标注**：指标语义由客户端决定，查询与告警行为由服务端决定

---

## 6. 常见坑与最佳实践

1. **高基数标签**：把 `user_id` / `trace_id` / 原始 URL / 订单号 / 时间戳当标签，是 Prometheus 头号事故。标签只放有界枚举；明细进日志与链路。
2. **Counter 直接画原始值**：它是累计值，重启归零会形成断崖。一律 `rate()` / `increase()`；聚合顺序是**先 rate 再 sum**。
3. **Histogram 桶边界拍脑袋**：默认桶（最大 10s）对毫秒级 RPC 太粗，P99 会失真。按实际 P99 量级设计桶，且桶数别超过 ~20。
4. **用 Summary 做多副本聚合**：Summary 的分位数在客户端算好，**无法跨实例求 P99**。多副本一律用 Histogram。
5. **单位与命名不规范**：时长用 `_seconds`、字节用 `_bytes`、累计量加 `_total`；不要 `latency_ms` 和 `duration_seconds` 混用。遵循官方命名与 [semantic conventions](https://opentelemetry.io/docs/specs/semconv/)。
6. **路由未模板化**：`/users/123` 每条 URL 一条 series。用 `chi.RoutePattern()` / Gin 的 `FullPath()` 取 `/users/:id`。
7. **`MustRegister` 重复注册 panic**：库代码别直接注册到全局默认 Registry；用自定义 Registry 或 `Register` + 处理 `AlreadyRegisteredError`。
8. **`Collect` 里做慢查询**：抓取是同步的，慢 Collector 会拖垮整个 `/metrics` 乃至 target 超时。异步刷新缓存，Collect 只读内存。
9. **`/metrics` 暴露到公网**：泄露内部路径、实例信息与业务量。放在内网端口或加认证/网络策略。
10. **告警不带 `for`**：瞬时抖动狂轰滥炸。配 `for` + 用 `rate` 窗口，并在 Alertmanager 配置分组、静默与抑制。
11. **`scrape_interval` 与突发流量不匹配**：15s 间隔会漏掉秒级尖刺；关键 SLO 可缩到 5s，但要评估 TSDB 写入压力。
12. **拿 Prometheus 存日志/长期明细**：本地 TSDB 是为近期监控设计的。长期归档走 remote write 到 Thanos / Mimir / VictoriaMetrics；日志走 [Zap](../zap/zap-guide.md) + Loki/ES。
13. **Pushgateway 当代理用**：短任务结束后不 `Delete` 分组，指标会永久停留在最后值，产生假告警。
14. **指标与日志/链路各自为政**：`service.name` 不统一、`trace_id` 不写进日志、指标上不挂 exemplar，排障时三套系统对不上。用 [OpenTelemetry](../opentelemetry/opentelemetry-guide.md) 统一 Resource，并用 exemplar 把指标样本连到 trace。

---

## 7. 选型对比

| 维度 | Prometheus | InfluxDB / VictoriaMetrics | StatsD / Graphite | OpenTelemetry Metrics | 云监控（云厂商） |
|---|---|---|---|---|---|
| 采集模型 | **拉取 + 服务发现** | 推送为主 | 推送（UDP） | 生成并导出（SDK） | Agent 推送 |
| 数据模型 | 多维标签时序 | 标签时序 | 点分隔的层级名 | OTLP 时序 | 各异 |
| 查询 | **PromQL（强）** | Flux / InfluxQL / MetricsQL | 弱 | 无（交给后端） | 厂商控制台 |
| 告警 | 规则 + Alertmanager | 内建 / 外接 | 需外接 | 不负责 | 内建 |
| 长期存储 | 本地有限，靠 remote write | **强项**（尤其 VM） | 弱 | 不存储 | 托管 |
| K8s 生态 | **事实标准** | 良好 | 一般 | CNCF 标准 | 良好 |
| 定位 | 监控告警 | 时序/长期存储 | 简单计数 | **遥测数据标准** | 托管运维 |

一句话决策：

- **云原生服务监控告警默认 Prometheus**，PromQL + Alertmanager + Grafana 是事实标准组合；单集群单 Prometheus 足够，多集群/长期再叠 Thanos / Mimir。
- **要存几年、要存业务时序数据**（非纯监控指标）→ [InfluxDB](../influxdb-v2/influxdb-v2-guide.md) 或 ClickHouse；**觉得 Prometheus 太吃资源** → VictoriaMetrics 兼容 PromQL 且更省。
- **埋点标准与跨语言统一** → OpenTelemetry SDK 生成数据，再导出到 Prometheus / OTLP 后端。**OTel 与 Prometheus 是上下游关系，不是二选一**：OTel 负责「怎么产生、怎么传播」，Prometheus 负责「怎么拉、怎么存、怎么查、怎么告」。
- **已经在云上且不想运维监控系统** → 云监控，但要接受查询能力与厂商锁定。
- **日志内容、单条请求上下文** → [Zap](../zap/zap-guide.md) + 日志系统；**跨服务调用链** → [OpenTelemetry](../opentelemetry/opentelemetry-guide.md)。

> 落地顺序建议：先定 `service.name` 与标签规范 → 暴露 `/metrics` → 写 3~5 条核心 SLO 的 Recording/Alerting rules → 最后才扩指标数量。**指标不是越多越好，每加一个标签都要先问基数是多少。**
