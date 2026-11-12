# OpenTelemetry 技术介绍与使用指南（Go 分布式追踪）

> 云原生可观测性的统一标准：一套 API/SDK 产出 Trace / Metrics / Logs，用 W3C TraceContext 把跨服务、跨消息队列的调用串成一条链。
> 上游：<https://github.com/open-telemetry/opentelemetry-go>　|　Go SDK 主线：**v1.x（API 稳定）**（整理于 2026-11）　|　传输协议：[OTLP](https://opentelemetry.io/docs/specs/otlp/)
> 协议：Apache-2.0；Collector 与后端另有各自版本，具体以上游最新 release 为准。

---

## 1. 项目简介

先厘清一个常见误解：**OpenTelemetry 不是监控系统，也不是数据库**。它是「**怎么产生、怎么描述、怎么传播、怎么导出遥测数据**」的标准与 SDK；存储、查询与可视化由 Jaeger / Tempo / Prometheus / Loki 或商业 APM 负责。

它解决的核心问题：一次用户请求穿过网关 → HTTP 服务 → gRPC 服务 → 异步任务 → 数据库，出问题时日志散落在五个地方，靠时间戳人肉拼接。OpenTelemetry 给每个请求分配一个全局 `trace_id`，让每一段工作都成为挂在同一个 Trace 下的 Span。

- **适用场景**：微服务排障、跨服务延迟归因、异步链路（MQ / 定时任务）追踪、统一多语言埋点、指标与链路关联（exemplar）
- **不适合**：存明细日志内容（用 [Zap + 日志系统](../zap/zap-guide.md)）、长期指标存储与告警（用 [Prometheus](../prometheus/prometheus-guide.md)）、替代业务审计日志（链路会被采样）

本仓库里它是「黏合剂」：[Gin](../gin/gin-guide.md) 中间件、[gRPC](../grpc/grpc-guide.md) 拦截器、[asynq](../asynq/asynq技术文档与使用手册.md) Handler 都能挂同一个 TracerProvider，Zap 日志里写 `trace_id` 后可直接从日志跳到链路。

### 安装

```bash
go get go.opentelemetry.io/otel \
       go.opentelemetry.io/otel/sdk \
       go.opentelemetry.io/otel/sdk/trace \
       go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp \
       go.opentelemetry.io/contrib/instrumentation/net/http/otelhttp
```

> `semconv` 包路径带版本（如 `go.opentelemetry.io/otel/semconv/v1.26.0`），**按所用 SDK 版本选择对应目录**；`otelhttp` / `otelgrpc` 自动埋点在独立的 `contrib` 模块里，不在主 SDK。

### 最小可运行示例（本地打印，无需后端）

```go
package main

import (
    "context"

    "go.opentelemetry.io/otel"
    "go.opentelemetry.io/otel/exporters/stdout/stdouttrace"
    sdktrace "go.opentelemetry.io/otel/sdk/trace"
)

func main() {
    exp, _ := stdouttrace.New(stdouttrace.WithPrettyPrint())
    tp := sdktrace.NewTracerProvider(sdktrace.WithSyncer(exp))
    defer tp.Shutdown(context.Background())
    otel.SetTracerProvider(tp)

    ctx, span := otel.Tracer("demo").Start(context.Background(), "hello")
    defer span.End()
    _ = ctx
}
```

---

## 2. 功能特性总览

| 能力 | 说明 |
|---|---|
| 三类信号 | Trace（稳定）/ Metrics（稳定）/ Logs（逐步稳定），共用 Resource 与 Context |
| 核心对象 | `TracerProvider`、`Tracer`、`Span`、`SpanContext`、`MeterProvider`、`LoggerProvider` |
| Span 语义 | 名称、Kind、Attributes、Events、Links、Status、Exception、起止时间 |
| 上下文传播 | 进程内 `context.Context`；跨进程 W3C `traceparent` / `tracestate` / `baggage` |
| 采样 | `AlwaysSample` / `NeverSample` / `TraceIDRatioBased` / `ParentBased`，Collector 可做尾部采样 |
| 处理管线 | `SpanProcessor`：`SimpleSpanProcessor`（同步）/ `BatchSpanProcessor`（生产默认） |
| 导出 | OTLP over gRPC(4317) / HTTP(4318)、stdout、Jaeger / Zipkin 兼容端点 |
| Resource | `service.name` / `service.version` / `deployment.environment` —— 三支柱关联的统一身份 |
| 语义约定 | Semantic Conventions：`http.route`、`db.system`、`rpc.method` 等统一字段命名 |
| 自动埋点 | `otelhttp`、`otelgrpc`、数据库与主流框架 instrumentation（contrib 仓库） |
| Collector | receiver → processor（batch / tail_sampling / attributes）→ exporter，与语言无关 |

---

## 3. 核心概念与原理

### 3.1 Trace 是一棵 Span 树

```
Trace (trace_id = 16 字节，全局唯一)
└── Span A  [HTTP GET /orders]  span_id=8字节, parent=无
    ├── Span B  [gRPC GetUser]        parent=A
    │   └── Span C  [DB SELECT]       parent=B
    └── Span D  [asynq send_email]    parent=A  ← 通过消息队列传播
```

每个 Span 的 `SpanContext` 包含 `trace_id`、`span_id`、trace flags（采样位）与 tracestate。**父子关系不靠推断，靠显式把父 Span 放进 `context.Context`**——这是 Go 埋点最容易出错的地方。

### 3.2 跨进程传播：traceparent

```
traceparent: 00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01
             │  └──────── trace_id ────────┘ └─ parent span_id ─┘ └flags
             └─ 版本                                              01 = sampled
```

流程是 **inject / extract**：客户端把当前 SpanContext 注入 carrier（HTTP Header、gRPC Metadata、Kafka Header、asynq Task payload），服务端从 carrier 提取并作为新 Span 的父。中间件只负责搬运，**不会自动创建 Trace**——如果入口没配置 propagator，链路会在第一个网络边界断掉。

```
进程内:  ctx = context.WithValue(ctx, span)   ← otel.Tracer(...).Start(ctx, ...)
跨进程:  Inject(ctx, HeaderCarrier)  ──网络──►  Extract(ctx, HeaderCarrier)
```

### 3.3 SDK 管线

```
Instrumentation (手动 Start / otelhttp 自动)
   │
   ▼
TracerProvider ──► Sampler（决定是否记录、是否采样）
   │
   ▼ (仅被采样的 Span)
SpanProcessor ──► Batch：队列 + 定时/定量批 ──► Exporter ──► Collector
                                                                │
                                              receiver → processor → exporter
                                                                ▼
                                                    Jaeger / Tempo / 商业 APM
```

- `SimpleSpanProcessor` 每个 Span 结束就同步导出，只适合本地调试
- `BatchSpanProcessor` 异步批量，生产必用；队列满时**丢弃并计数**，需要监控 `otelcol_exporter_dropped_spans` 一类指标
- **Collector 不是必需的**（应用可直连后端），但生产强烈建议：解耦后端、统一采样与脱敏、多语言汇聚

### 3.4 采样：头部 vs 尾部

| | 头部采样（SDK） | 尾部采样（Collector） |
|---|---|---|
| 决策时机 | 请求入口，基于 trace_id 比例 | 整条 Trace 收齐后 |
| 优点 | 省资源、下游零开销 | 可「只保留出错 / 慢的链路」 |
| 缺点 | 会漏掉低概率错误 | 所有 Span 都要先传到 Collector |

生产常见组合：SDK 用 `ParentBased(TraceIDRatioBased(0.1))` 保底（**ParentBased 保证同一条 Trace 内决策一致，否则链路会残缺**），Collector 再对 `status=ERROR` 或高延迟链路做尾部补采。

### 3.5 Resource 与三支柱关联

`service.name` 是**最关键的一个属性**：Zap 日志、Prometheus 指标、OTel Trace 都用同一个值，排障时才能三套系统对得上。关联方式：

- 日志 → 链路：日志里写 `trace_id` / `span_id`
- 指标 → 链路：Prometheus **exemplar** 把某个样本的 `trace_id` 挂到 Histogram 桶上
- 链路 → 指标：Span 属性与指标标签共用 `http.route` 等语义约定

### 3.6 与 Prometheus 的职责边界

OTel 的 `MeterProvider` 负责**在进程内生成指标**，Prometheus 负责**拉取、存储、查询、告警**。两者可通过 `otel/exporters/prometheus` 打通（OTel 指标以 Prometheus 格式暴露），但**不要指望 OTel 替代 TSDB 与 PromQL**。

---

## 4. 使用实战

### 4.1 生产级初始化

```go
package telemetry

import (
    "context"
    "time"

    "go.opentelemetry.io/otel"
    "go.opentelemetry.io/otel/attribute"
    "go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp"
    "go.opentelemetry.io/otel/propagation"
    "go.opentelemetry.io/otel/sdk/resource"
    sdktrace "go.opentelemetry.io/otel/sdk/trace"
    semconv "go.opentelemetry.io/otel/semconv/v1.26.0"
)

func Setup(ctx context.Context, service, version, env string) (func(context.Context) error, error) {
    res, err := resource.Merge(resource.Default(),
        resource.NewWithAttributes(semconv.SchemaURL,
            semconv.ServiceName(service),
            attribute.String("service.version", version),
            attribute.String("deployment.environment.name", env),
        ))
    if err != nil { return nil, err }

    exp, err := otlptracehttp.New(ctx,
        otlptracehttp.WithEndpoint("localhost:4318"),
        otlptracehttp.WithInsecure(), // 生产走 TLS，去掉此项并配证书
    )
    if err != nil { return nil, err }

    tp := sdktrace.NewTracerProvider(
        sdktrace.WithResource(res),
        sdktrace.WithBatcher(exp,
            sdktrace.WithMaxQueueSize(2048),
            sdktrace.WithBatchTimeout(5*time.Second),
            sdktrace.WithExportTimeout(10*time.Second),
        ),
        sdktrace.WithSampler(
            sdktrace.ParentBased(sdktrace.TraceIDRatioBased(0.1))),
    )
    otel.SetTracerProvider(tp)
    // 漏掉这一行 = 跨服务链路必断
    otel.SetTextMapPropagator(propagation.NewCompositeTextMapPropagator(
        propagation.TraceContext{}, propagation.Baggage{},
    ))
    return tp.Shutdown, nil
}
```

```go
// main.go
shutdown, err := telemetry.Setup(ctx, "checkout-api", "1.2.0", "prod")
if err != nil { log.Fatal(err) }
defer func() {
    c, cancel := context.WithTimeout(context.Background(), 5*time.Second)
    defer cancel()
    _ = shutdown(c) // 不 Shutdown 会丢最后一批 Span
}()
```

### 4.2 手动埋点

```go
func CreateOrder(ctx context.Context, id string) error {
    ctx, span := otel.Tracer("checkout").Start(ctx, "CreateOrder",
        trace.WithSpanKind(trace.SpanKindInternal),
        trace.WithAttributes(attribute.String("order.id", id)),
    )
    defer span.End()

    if err := reserveStock(ctx, id); err != nil {
        span.RecordError(err)
        span.SetStatus(codes.Error, err.Error())
        return err
    }
    span.AddEvent("stock.reserved",
        trace.WithAttributes(attribute.Int("items", 3)))
    span.SetStatus(codes.Ok, "")
    return nil
}
```

> 三条铁律：**`Start` 传入上游 `ctx`（不是 `context.Background()`）**；**`defer span.End()`**；**子函数接收 `ctx` 并继续 `Start`**，否则 Span 树会断成一片平铺的孤岛。

### 4.3 HTTP 自动埋点与跨服务传播

```go
// 服务端：otelhttp 自动 Extract + 建 SERVER Span
handler := otelhttp.NewHandler(mux, "checkout-api")

// 客户端：自动 Inject + 建 CLIENT Span，必须把 ctx 传下去
req, _ := http.NewRequestWithContext(ctx, "GET", url, nil)
resp, err := otelhttp.Get(ctx, url) // 或自定义 client.Transport = otelhttp.NewTransport(...)
```

手动传播（自己写 Gin 中间件时）：

```go
func Tracing() gin.HandlerFunc {
    return func(c *gin.Context) {
        ctx := otel.GetTextMapPropagator().Extract(
            c.Request.Context(), propagation.HeaderCarrier(c.Request.Header))
        ctx, span := otel.Tracer("http").Start(ctx, c.FullPath(),
            trace.WithSpanKind(trace.SpanKindServer))
        defer span.End()
        c.Request = c.Request.WithContext(ctx) // 关键：让后续 handler 拿到同一个 ctx
        c.Next()
        if len(c.Errors) > 0 {
            span.SetStatus(codes.Error, c.Errors.String())
        }
    }
}
```

### 4.4 gRPC 拦截器（接 [gRPC 篇](../grpc/grpc-guide.md)）

```go
import "go.opentelemetry.io/contrib/instrumentation/google.golang.org/grpc/otelgrpc"

s := grpc.NewServer(
    grpc.ChainUnaryInterceptor(otelgrpc.UnaryServerInterceptor(), LoggingUnary()),
)
conn, _ := grpc.NewClient(addr,
    grpc.WithTransportCredentials(insecure.NewCredentials()),
    grpc.WithChainUnaryInterceptor(otelgrpc.UnaryClientInterceptor()),
)
```

> 客户端拦截器负责把 `traceparent` 写进 gRPC Metadata，服务端拦截器负责 Extract。两边都要配，缺一侧链路就断。

### 4.5 把 trace_id 写进日志（接 [Zap 篇](../zap/zap-guide.md)）

```go
func LoggerFromContext(ctx context.Context, base *zap.Logger) *zap.Logger {
    sc := trace.SpanFromContext(ctx).SpanContext()
    if !sc.IsValid() { return base }
    return base.With(
        zap.String("trace_id", sc.TraceID().String()),
        zap.String("span_id", sc.SpanID().String()),
    )
}
```

配好之后，日志检索里拿到 `trace_id` 就能直接跳到 Jaeger/Tempo 看完整调用链——这是可观测性三支柱真正闭环的一步。

### 4.6 异步任务传播（接 [asynq 篇](../asynq/asynq技术文档与使用手册.md)）

消息队列不会自动带 HTTP Header，需要手动 inject/extract：

```go
// 入队：把当前 SpanContext 写进 payload
carrier := propagation.MapCarrier{}
otel.GetTextMapPropagator().Inject(ctx, carrier)
payload := map[string]any{"order_id": id, "traceparent": carrier.Get("traceparent")}

// Handler：取出并作为父 Span
func HandleEmail(ctx context.Context, t *asynq.Task) error {
    var p struct {
        OrderID     string `json:"order_id"`
        TraceParent string `json:"traceparent"`
    }
    _ = json.Unmarshal(t.Payload(), &p)
    ctx = otel.GetTextMapPropagator().Extract(ctx,
        propagation.MapCarrier{"traceparent": p.TraceParent})
    ctx, span := otel.Tracer("worker").Start(ctx, "send_email")
    defer span.End()
    return send(ctx, p.OrderID)
}
```

### 4.7 Baggage（跨服务透传业务上下文）

```go
m, _ := baggage.NewMember("tenant", "acme")
bag, _ := baggage.New(m)
ctx = baggage.ContextWithBaggage(ctx, bag)
// 下游：bag := baggage.FromContext(ctx); member := bag.Member("tenant")
```

> Baggage 会随**每个**下游请求传播并可能写进日志，只放租户/灰度标这类小体积、非敏感信息；不要放 PII，也不要当 Span Attribute 用。

### 4.8 Collector 配置与本地起服务

```yaml
# otel-collector-config.yaml
receivers:
  otlp:
    protocols:
      grpc: {endpoint: 0.0.0.0:4317}
      http: {endpoint: 0.0.0.0:4318}
processors:
  batch: {timeout: 5s, send_batch_size: 512}
exporters:
  debug: {verbosity: detailed}
service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [batch]
      exporters: [debug]
```

```bash
docker run --rm -p 4317:4317 -p 4318:4318 \
  -v "$(pwd)/otel-collector-config.yaml:/etc/otelcol-contrib/config.yaml" \
  otel/opentelemetry-collector-contrib:latest
```

把 `debug` 换成 `otlp/jaeger` 或 `otlphttp/tempo` 即可接可视化后端。

### 4.9 单测

```go
sr := tracetest.NewSpanRecorder()
tp := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(sr))
otel.SetTracerProvider(tp)

CreateOrder(context.Background(), "A1001")

spans := sr.Ended()
require.Len(t, spans, 1)
require.Equal(t, "CreateOrder", spans[0].Name())
```

---

## 5. 版本演进速览

| 阶段 | 关键变化与迁移注意点 |
|---|---|
| 2019 | OpenTracing 与 OpenCensus 合并为 OpenTelemetry，统一 API / SDK / OTLP 协议 |
| 2021 | **Go Tracing API 达到 1.0 稳定**，官方承诺长期向后兼容；Metrics 随后稳定 |
| 近年 v1.x | Logs 逐步稳定；Semantic Conventions 持续演进（`http.*` → `http.request.*` 等迁移）；OTLP 成为默认导出协议 |
| 生态 | Jaeger 的 Thrift 协议退役，Go 侧旧的 Jaeger exporter 移除，**统一走 OTLP**；`otelhttp` / `otelgrpc` 迁至 `contrib` 独立模块 |

**迁移注意**：

- `semconv` 按版本切分包路径（`v1.24.0` / `v1.26.0` …），升 SDK 时要么同步换 import，要么显式锁定旧版语义约定
- 属性命名会随 Semantic Conventions 演进，看板与告警若依赖属性名，升级前先核对
- 旧的 `otelhttp` 在 `go.opentelemetry.io/otel/instrumentation/...`，现位于 `go.opentelemetry.io/contrib/instrumentation/...`
- **SDK 版本、Collector 版本、后端版本三者要分别标注**：协议由 OTLP 保证兼容，但语义与 UI 行为各自演进

---

## 6. 常见坑与最佳实践

1. **忘记 `SetTextMapPropagator`**：进程内 Span 正常，一跨服务就断链。这是排查「链路只有一段」的第一检查项。
2. **忘记 `Shutdown`**：`BatchSpanProcessor` 有缓冲，进程退出丢最后一批 Span，恰好常是出错的那批。
3. **用 `context.Background()` 起 Span**：父子关系全断，看板上是一堆孤立 Span。所有函数签名都要传 `ctx`。
4. **不 `defer span.End()` 或提前 End**：Span 时长错误，甚至在 goroutine 里使用已结束的 Span。异步任务要显式管理生命周期。
5. **每请求创建 `TracerProvider`**：初始化极重且会泄漏 exporter。`TracerProvider` 全局单例，`Tracer` 可按模块获取。
6. **库代码直接调 `otel.Tracer()` 全局**：应接收 `trace.Tracer` 作为依赖注入，便于单测与替换；只有应用入口装配全局 Provider。
7. **高基数 Attribute**：`user_id`、原始 URL、SQL 全文、大 payload。Span 属性同样有成本，且后端按属性建索引。用 `http.route` 模板而非具体路径。
8. **Span 里记敏感信息**：密码、token、身份证、完整请求体。OTLP 是明文或 TLS，但后端长期留存，脱敏要在 SDK 或 Collector `attributes` processor 做。
9. **采样率不用 `ParentBased`**：各服务各自按 10% 采样，一条 Trace 大概率只剩残缺片段。
10. **`SimpleSpanProcessor` 上生产**：每个 Span 同步网络导出，直接把延迟打到业务路径上。生产一律 `BatchSpanProcessor`。
11. **Batch 队列打满静默丢 Span**：按 QPS 调 `MaxQueueSize` / `BatchTimeout`，并监控 Collector 的 dropped 指标。
12. **Resource 不设 `service.name`**：后端显示 `unknown_service`，多服务混在一起无法区分。
13. **属性命名各自为政**：`userID` / `user_id` / `User.Id` 混用，看板聚合失效。遵循 Semantic Conventions。
14. **异步链路不传播**：Kafka / asynq / 定时任务默认断链，必须手动 Inject/Extract 到消息头或 payload。
15. **把 Trace 当日志/指标用**：在 Span 里塞长文本、拿 Span 数量算 QPS。明细用[日志](../zap/zap-guide.md)，趋势用[指标](../prometheus/prometheus-guide.md)。
16. **上来就全量采样**：链路数据量大、后端成本高。从 1%~10% + 错误/慢请求尾部采样起步。

---

## 7. 选型对比

| 维度 | OpenTelemetry | Jaeger / Zipkin（老一代） | SkyWalking | Prometheus | 商业 APM |
|---|---|---|---|---|---|
| 定位 | **标准 + SDK + 协议**（不存储） | 追踪后端（含 SDK） | 一体化 APM（Agent） | 指标监控 | 托管全栈 |
| 信号 | Trace + Metrics + Logs | 仅 Trace | Trace + Metrics + 日志 | 仅 Metrics | 全部 |
| 语言 / 生态 | **CNCF 标准，多语言最全** | 逐渐转为 OTLP 接收端 | Java 生态最强 | 多语言 client | 多语言 Agent |
| 侵入性 | 需埋点或引入 instrumentation | 需 SDK | 低（字节码增强） | 暴露端点 | 低 |
| 存储 / 查询 | 交给后端 | 自带 | 自带 | 自带 TSDB + PromQL | 托管 |
| 长期趋势 | **主流，vendor 中立** | 作为后端继续存在，SDK 被 OTel 取代 | 特定生态 | 指标事实标准 | 成本与锁定 |

一句话决策：

- **新项目一律用 OpenTelemetry 做埋点与传播标准**：vendor 中立，换后端不用改业务代码；Collector 把「采集」与「存储」解耦。
- **后端怎么选**：轻量自建用 Jaeger（走 OTLP 接收）、已上 Grafana 栈用 Tempo、要日志指标链路一体化看板用 Grafana Cloud / 商业 APM。
- **Jaeger / Zipkin 客户端 SDK 不要再新引入**：它们本身已转向接收 OTLP，先用原生 SDK 等于给自己留一次迁移。
- **Java 为主且希望零侵入** → SkyWalking 仍有优势；**混合多语言** → OTel 更合适。
- **指标与告警** → [Prometheus](../prometheus/prometheus-guide.md)，OTel Metrics 可通过 exporter 接入；**日志** → [Zap](../zap/zap-guide.md) + Loki/ES。

> 落地顺序建议：先统一 `service.name` 与语义约定 → 接入 HTTP/gRPC 自动埋点 → 把 `trace_id` 写进 Zap 日志 → 配 Collector 与采样 → 最后补异步链路与 exemplar。**不要一上来就追求 100% 覆盖，先把最关键的几条请求链路打通。**
