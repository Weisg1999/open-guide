# log/slog 实践指南（Go 标准库结构化日志）

> Go 1.21 起官方的结构化日志包：Logger / Handler / Record / Attr 四层抽象，零第三方依赖——把「怎么统一字段、怎么透传 trace_id、怎么接 Zap 与 OTel」讲清楚。
> 上游：<https://pkg.go.dev/log/slog>（Go 标准库）　|　适用版本：**Go 1.21+**（整理于 2026-11）　|　协议：Go BSD-3-Clause

---

## 1. 项目简介

[Zap 篇](../zap/zap-guide.md) 解决「极致性能 + 成熟生态」，本文解决「**不引第三方依赖时，怎么把日志做对**」。`log/slog` 是 Go 官方在 1.21 引入的结构化日志 API，设计上与 Zap 的 `zapcore` 分层很像，但抽象更克制：

```
Logger ──► 构造 Record（时间/级别/消息/调用点 + attrs）
              │
              ▼
         Handler ──► 编码（JSON / Text / 自定义）──► io.Writer
```

**Handler 是可插拔核心**：换输出格式、加脱敏、注入 `trace_id`、多目的地分流、采样，全部通过包装 Handler 完成，业务代码一行不改。

- **适用场景**：新项目不想引第三方日志库、库/框架需要可注入的日志接口、CLI 与工具、需要与标准库 `log` 平滑共存、团队想统一 `log/slog` 作为公共抽象再桥接到 Zap
- **不适合**：日志量极大且已压测证明 slog 是瓶颈（用 Zap）、需要成熟的采样 / Tee / Hook 生态（Zap 篇更全）、复杂多目的地分流（可自己写，但要评估成本）

> slog 与 Zap 不是二选一：常见做法是把 `slog` 作为**公共 API**（库代码接受 `*slog.Logger`），应用入口用 `zapslog` 桥接到 Zap 后端，兼顾标准化与性能。

### 最小可运行示例

```go
package main

import (
    "log/slog"
    "os"
    "time"
)

func main() {
    h := slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{
        Level:     slog.LevelDebug,
        AddSource: true,
    })
    logger := slog.New(h)
    slog.SetDefault(logger) // 让标准库 log.Printf 也走这里

    logger.Info("user login",
        slog.String("user", "alice"),
        slog.Int("attempt", 3),
        slog.Duration("latency", 42*time.Millisecond),
    )
    // {"time":"...","level":"INFO","source":{"function":"main.main","file":"...","line":17},
    //  "msg":"user login","user":"alice","attempt":3,"latency":0.042}
}
```

---

## 2. 功能特性总览

| 能力 | 说明 |
|---|---|
| 四层抽象 | `Logger` → `Record` → `Handler` → `io.Writer`；`Handler` 接口四个方法可自定义 |
| 级别 | `Debug` / `Info` / `Warn` / `Error`；`LevelVar` 支持运行时动态调整 |
| 内置 Handler | `JSONHandler`（生产）/ `TextHandler`（开发，key=value） |
| 结构化字段 | `slog.String` / `Int` / `Bool` / `Duration` / `Time` / `Group` / `Any`；强类型 `Attr` |
| 上下文 | `Logger.With(...)` 绑定公共字段、`WithGroup` 命名空间、`SetDefault` 全局默认 |
| 改写 | `HandlerOptions.ReplaceAttr` 统一改名、脱敏、格式转换 |
| 桥接 | `slog.SetDefault` + `log.SetOutput` 接管标准库 `log`；`NewLogLogger` 反向桥接 |
| 生态 | `sloglint` 静态检查、`tint` / `charmbracelet/log` 彩色 Handler、`zapslog` 接 Zap |
| 性能 | 内置 Handler 无反射；`slog.Any` 走反射，热路径应避免 |

---

## 3. 核心概念与原理

### 3.1 Handler 接口：四个方法都要实现

```go
type Handler interface {
    Enabled(context.Context, slog.Level) bool
    Handle(context.Context, slog.Record) error
    WithAttrs(attrs []slog.Attr) Handler
    WithGroup(name string) Handler
}
```

- `Enabled` 在**构造字段之前**被调用，是级别过滤的快速路径；自定义 Handler 必须实现它，否则被禁用的日志仍会做完整编码
- `Handle` 负责输出；`Record` 是可读的（可 `r.AddAttrs(...)` 追加字段），但**不要在多个 goroutine 间共享 Record**
- `WithAttrs` / `WithGroup` 派生新 Handler——**这是自定义 Handler 最容易漏的两个方法**：漏实现会导致 `logger.With(...)` 的字段丢失

### 3.2 Attr 与 Value：避免反射

```go
slog.String("k", "v")   // 静态类型 → 编码时无需反射
slog.Int("n", 3)
slog.Duration("d", d)   // 输出为秒（float64），与 Prometheus 的 _seconds 单位一致
slog.Any("obj", bigStruct) // 走反射 + JSON 编码，大对象代价高
```

`Record.Attrs(func(a slog.Attr) bool)` 遍历字段；`Attr.Value.Resolve()` 处理 `LogValuer`（延迟求值的字段值，可用于避免昂贵计算）。

### 3.3 slog 不原生支持 Context

这是与 Zap 最大的心智差异：`slog.Info(...)` **没有 ctx 参数**（`slog.InfoContext(ctx, ...)` 才是带 ctx 的版本，且 `Handler.Handle` 能拿到它）。因此要自动带 `trace_id`，必须：

1. 业务侧统一调 `slog.InfoContext(ctx, ...)` 而不是 `slog.Info(...)` — 可用 `sloglint` 强制
2. 写一个自定义 Handler，在 `Handle` 里从 `ctx` 取值并 `r.AddAttrs(...)`

```go
// 推荐：用 sloglint 的 ctx-only 规则，强制所有日志调用走 *Context 变体
// slog.Info("...")  → 编译/lint 报错
// slog.InfoContext(ctx, "...") → 自动带 trace_id
```

### 3.4 LevelVar：运行时调级

```go
var level slog.LevelVar          // 零值 = Info
level.Set(slog.LevelDebug)        // 排障时临时开 Debug，无需重启
h := slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: &level})
```

`LevelVar` 是并发安全的，可挂到受控的 admin 接口或配置中心。注意 **Handler 每次都要读 `LevelVar`**，所以调级立即生效；用固定 `slog.LevelInfo` 常量则不行。

### 3.5 与 Prometheus / OTel 的职责边界

与 [Zap 篇](../zap/zap-guide.md) 一致：日志是「单条事件」，[指标](../prometheus/prometheus-guide.md) 是「一段时间趋势」，[链路](../opentelemetry/opentelemetry-guide.md) 是「跨服务调用路径」。三者的**唯一交汇点是 `trace_id` 与统一的 `service.name`**：日志里写 `trace_id`，指标上挂 exemplar，链路里用同一个 `service.name`。

---

## 4. 使用实战

### 4.1 生产配置

```go
package logging

import (
    "log/slog"
    "os"
    "strings"
)

var Level slog.LevelVar

func New(service, env string) *slog.Logger {
    Level.Set(slog.LevelInfo)
    h := slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{
        Level:     &Level,
        AddSource: true,
        ReplaceAttr: func(groups []string, a slog.Attr) slog.Attr {
            switch a.Key {
            case slog.MessageKey:
                a.Key = "msg"
            case slog.LevelKey:
                a.Key = "level"
                a.Value = slog.StringValue(strings.ToLower(a.Value.String()))
            }
            return a
        },
    })
    return slog.New(NewContextHandler(h)).With(
        slog.String("service.name", service),
        slog.String("env", env),
    )
}
```

> 容器里只写 **stdout**（见 [Docker/K8s 篇](../docker-k8s/docker-k8s-guide.md)），落盘与轮转交给采集器；`ReplaceAttr` 是统一字段名与大小写的最佳位置。

### 4.2 ContextHandler：自动注入 trace_id

```go
package logging

import (
    "context"
    "log/slog"

    "go.opentelemetry.io/otel/trace"
)

type ContextHandler struct{ slog.Handler }

func NewContextHandler(h slog.Handler) ContextHandler { return ContextHandler{h} }

func (h ContextHandler) Enabled(ctx context.Context, l slog.Level) bool {
    return h.Handler.Enabled(ctx, l)
}

func (h ContextHandler) Handle(ctx context.Context, r slog.Record) error {
    sc := trace.SpanFromContext(ctx).SpanContext()
    if sc.IsValid() {
        r.AddAttrs(
            slog.String("trace_id", sc.TraceID().String()),
            slog.String("span_id", sc.SpanID().String()),
        )
    }
    if tid, ok := ctx.Value(requestIDKey{}).(string); ok {
        r.AddAttrs(slog.String("request_id", tid))
    }
    return h.Handler.Handle(ctx, r)
}

// 关键：包装 Handler 必须转发 WithAttrs / WithGroup，否则 logger.With 的字段会丢
func (h ContextHandler) WithAttrs(attrs []slog.Attr) slog.Handler {
    return ContextHandler{h.Handler.WithAttrs(attrs)}
}
func (h ContextHandler) WithGroup(name string) slog.Handler {
    return ContextHandler{h.Handler.WithGroup(name)}
}

type requestIDKey struct{}
// 请求入口写入：ctx = context.WithValue(ctx, requestIDKey{}, rid)
```

> 这段是 slog 工程化最有价值的一块：业务代码只要 `slog.InfoContext(ctx, "order created")`，日志就自动带上 `trace_id`，可直接跳到 [OpenTelemetry](../opentelemetry/opentelemetry-guide.md) 链路或 [Loki](../loki/loki-guide.md) 检索。

### 4.3 脱敏：ReplaceAttr

```go
var sensitive = map[string]bool{
    "password": true, "token": true, "authorization": true,
    "card_no": true, "id_card": true,
}

opts := &slog.HandlerOptions{
    ReplaceAttr: func(groups []string, a slog.Attr) slog.Attr {
        if sensitive[strings.ToLower(a.Key)] {
            return slog.String(a.Key, "[REDACTED]")
        }
        return a
    },
}
```

> `groups` 参数可用于区分 `WithGroup("http")` 下的同名字段。脱敏放在 Handler 层比靠 code review 可靠，但**无法覆盖把敏感信息拼进 `msg` 本身**的情况——那要靠规范与 lint。

### 4.4 多目的地 Fanout Handler

```go
type fanout []slog.Handler

func (f fanout) Enabled(ctx context.Context, l slog.Level) bool {
    for _, h := range f {
        if h.Enabled(ctx, l) { return true }
    }
    return false
}
func (f fanout) Handle(ctx context.Context, r slog.Record) error {
    for _, h := range f {
        if !h.Enabled(ctx, r.Level) { continue }
        // Record.Clone 需 Go 1.23+；低版本可构造新 Record 或让各 Handler 只读
        if err := h.Handle(ctx, r.Clone()); err != nil { return err }
    }
    return nil
}
func (f fanout) WithAttrs(attrs []slog.Attr) slog.Handler {
    out := make(fanout, len(f))
    for i, h := range f { out[i] = h.WithAttrs(attrs) }
    return out
}
func (f fanout) WithGroup(name string) slog.Handler {
    out := make(fanout, len(f))
    for i, h := range f { out[i] = h.WithGroup(name) }
    return out
}
```

```go
// 全量 → stdout；Error 以上 → 单独文件（便于告警系统只 tail 该文件）
all := slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: slog.LevelDebug})
errFile, _ := os.OpenFile("/var/log/app/error.log", os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0644)
errs := slog.NewJSONHandler(errFile, &slog.HandlerOptions{Level: slog.LevelError})
logger := slog.New(fanout{all, errs})
```

> `Record` 被多个 Handler 消费时用 `r.Clone()`，避免互相干扰；`os.File` 本身是并发安全的，但自定义 writer 要自行加锁（同 [Zap 篇](../zap/zap-guide.md) 的 `zapcore.Lock`）。

### 4.5 Gin 中间件：请求日志

```go
func Logging(base *slog.Logger) gin.HandlerFunc {
    return func(c *gin.Context) {
        start := time.Now()
        c.Next()
        level := slog.LevelInfo
        if c.Writer.Status() >= 500 { level = slog.LevelError }
        log := base.With(
            slog.String("http.method", c.Request.Method),
            slog.String("http.route", c.FullPath()),   // 模板，不是原始 URL
            slog.Int("http.status_code", c.Writer.Status()),
        )
        log.LogAttrs(c.Request.Context(), level, "http request",
            slog.Duration("duration", time.Since(start)),
            slog.String("remote_ip", c.ClientIP()),
        )
    }
}
```

> 用 `c.FullPath()` 而非 `c.Request.URL.Path`：否则 `/users/123` 每个 ID 都是不同值，既不利于检索也容易撞上 [Prometheus 篇](../prometheus/prometheus-guide.md) 的高基数问题。所有调用走 `*Context` 变体，`trace_id` 才会自动带上。

### 4.6 与 Zap 互操作

```go
import (
    "go.uber.org/zap"
    "go.uber.org/zap/exp/zapslog"
)

// 把 Zap Core 包装成 slog.Handler，库代码用 slog，底层仍是 Zap 的性能
zl, err := zap.NewProduction()
if err != nil { return err }
logger := slog.New(zapslog.NewHandler(zl.Core()))
```

> `zapslog` 位于 `exp/`（实验包），API 可能随版本变化，升级前核对所用 Zap 版本。反向桥接（slog → 标准 log）用 `slog.NewLogLogger(h, slog.LevelInfo)`。

### 4.7 单测断言

```go
func TestOrderLog(t *testing.T) {
    var buf bytes.Buffer
    logger := slog.New(slog.NewJSONHandler(&buf, nil))
    emitOrderCreated(logger, "A1001") // 待测的业务函数，接收 *slog.Logger

    var got map[string]any
    if err := json.Unmarshal(buf.Bytes(), &got); err != nil { t.Fatal(err) }
    if got["msg"] != "order created" { t.Fatalf("msg = %v", got["msg"]) }
    if got["order_id"] != "A1001" { t.Fatalf("order_id = %v", got["order_id"]) }
}
```

### 4.8 静态检查

```bash
go install github.com/vearutop/sloglint/cmd/sloglint@latest
sloglint ./...
```

在配置里打开三类检查（**具体 flag 名以所用 sloglint 版本的 README 为准**）：强制只用 `InfoContext` 等带 ctx 的调用、禁止把裸 key-value 与 `slog.Attr` 混用、要求键名遵循统一命名规范。这三条能把「字段名不统一 / 忘传 ctx / 用 `slog.Any` 打大对象」拦在 CI 阶段。

---

## 5. 版本演进速览

| 阶段 | 关键变化与迁移注意点 |
|---|---|
| Go 1.21 | **`log/slog` 引入**：Logger / Handler / Record / Attr、`JSONHandler` / `TextHandler`、`LevelVar`、`SetDefault` |
| Go 1.22 | `slog.SetLogLoggerLevel` 便于接管标准库 `log`；细节行为与性能优化 |
| Go 1.23+ | 生态完善（`sloglint`、`zapslog`、彩色 Handler）、`Record.Clone` 等便利 API、性能优化持续 |

**注意**：

- slog 是**标准库**，版本跟随 Go 工具链；升级 Go 时留意 `slog` 的行为变更（尤其 `SetDefault` 与 `log` 包联动）
- **设置 `slog.SetDefault` 会影响进程内所有用 `log.Printf` 的代码**（包括第三方库），这是优点也是副作用：格式突变可能让依赖文本日志的采集器失配
- 库代码**不要调用 `SetDefault`**，应由应用入口装配；库函数接受 `*slog.Logger` 或 `slog.Handler` 注入
- `zapslog` 等桥接包在 `exp/` 下，不承诺稳定；生产锁定版本

---

## 6. 常见坑与最佳实践

1. **用 `fmt.Sprintf` 拼消息**：`slog.Info(fmt.Sprintf("user %s login", u))` 把结构化字段压回字符串，既慢又不可检索。改成 `slog.String("user", u)`。
2. **热路径滥用 `slog.Any`**：大对象走反射与 JSON 编码，可能成为 CPU 与 GC 热点。用具体类型 Field，或只挑关键字段。
3. **自定义 Handler 漏实现 `WithAttrs` / `WithGroup`**：`logger.With("service", ...)` 的字段静默丢失，是最难查的一类 bug。包装 Handler 时必须转发这两个方法（并同样转发 `Enabled`）。
4. **只用 `slog.Info` 不传 ctx**：自定义 ContextHandler 拿不到 ctx，`trace_id` 永远为空。用 `InfoContext` + `sloglint -ctx-only` 强制。
5. **库代码里调 `SetDefault`**：污染宿主应用的日志配置。库应接收 `*slog.Logger` 注入，或使用 `slog.Default()` 但由宿主决定。
6. **忘记 `SetDefault`**：标准库 `log.Printf` 与部分第三方库仍走旧格式，日志两套并存。应用入口统一设置。
7. **字段名不统一**：`userID` / `user_id` / `UserID` 混用，下游聚合要写三套查询。制定字段词典并用 `sloglint -enforce-keys` 约束。
8. **`Error` 级别滥用**：用户输入不合法打成 Error 会让告警全是噪音。约定 Error = 需人介入的系统故障，Warn = 可自愈，Info = 关键业务事件。
9. **敏感信息进 attrs**：密码、token、身份证。用 `ReplaceAttr` 集中脱敏；同时禁止把敏感值拼进 `msg`。
10. **每请求新建 Logger / Handler**：`slog.New` 与 `NewJSONHandler` 有构造成本。全局建一次，用 `logger.With(...)` 派生轻量子 Logger。
11. **不设 `AddSource` 或 caller 层级错**：封装一层后 `source` 指向封装文件。自己封装时用 `runtime.Callers` 调整，或干脆在字段里显式写 `component`。
12. **同步写文件不加锁**：多 goroutine 写同一个 `io.Writer` 会行交错、JSON 被撕坏。用 `os.File`（并发安全）或 `sync.Mutex` 包装；容器优先 stdout。
13. **把日志当指标或链路**：`duration` 字段可以辅助排障，但 SLO 交给 [Prometheus](../prometheus/prometheus-guide.md)，跨服务调用链交给 [OpenTelemetry](../opentelemetry/opentelemetry-guide.md)。
14. **不设采样**：下游故障时循环重试能瞬间写满磁盘。slog 没有内建采样，需自己写 `SamplingHandler`（按消息+级别在窗口内限流）或依赖采集端限流。
15. **时间格式 / 单位不一致**：`slog.Duration` 输出秒（float64），与 Prometheus 的 `_seconds` 一致；别再自定义一个 `duration_ms` 混用。
16. **Handler 里做慢操作**：`Handle` 在调用方 goroutine 同步执行，慢 writer（网络、无缓冲文件）会直接拖慢业务。需要异步落盘时自己加有界队列并处理丢弃策略。
17. **忽略 `Enabled` 快速路径**：自定义 Handler 不做级别判断，被禁用的 Debug 日志仍会完整编码，零成本过滤失效。

---

## 7. 选型对比

| 维度 | log/slog（标准库） | Zap | zerolog | logrus | 彩色 Handler（tint 等） |
|---|---|---|---|---|---|
| 依赖 | **零** | 第三方 | 第三方 | 第三方（维护模式） | 第三方 |
| 性能 | 中～高 | **极高**（零分配热路径） | 极高 | 低 | 中 |
| API 风格 | `slog.Attr` + Handler | 强类型 `Field` | 链式 | `WithFields` | slog Handler 兼容 |
| Context 透传 | 需自定义 Handler | 需自己封装 | 需自己封装 | 弱 | 同 slog |
| 采样 / 多目的地 | 需自己写 | **内建** | 需自实现 | 无 | 同 slog |
| 动态级别 | `LevelVar` | `AtomicLevel` | 全局 Level | SetLevel | 同 slog |
| 生态成熟度 | 官方，快速成长 | **最成熟** | 活跃 | 存量项目多 | 小众 |
| 典型场景 | 新项目 / 库 / CLI | 高性能在线服务 | 追求极简链式 | 存量 logrus | 本地开发终端 |

一句话决策：

- **新项目、想少一个依赖、日志不是瓶颈** → **log/slog**，配一个 `ContextHandler` + `sloglint` 就能覆盖绝大多数需求。
- **高性能在线服务、需要采样与 Tee 分流、已有可观测体系** → [Zap](../zap/zap-guide.md)；两者可用 `zapslog` 桥接，把 slog 作为公共 API、Zap 作为后端。
- **追求极简链式写法与最小分配** → zerolog；生态与分流能力不如 Zap。
- **存量 logrus 项目** → 不必强迁；可通过 `slog.Handler` 做渐进替换。
- **CLI / 本地开发想看彩色输出** → `tint` 等 slog Handler，不引入完整日志框架。

> 和 [Zap 篇](../zap/zap-guide.md) 的结论一样：真正决定可观测性的是**字段规范、trace_id 全链路透传、级别语义约定、以及日志/指标/链路三者的职责边界**。选标准库还是 Zap 只影响「写得快不快」，规范才决定「查得到查不到」。
