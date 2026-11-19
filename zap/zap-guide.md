# Zap 技术介绍与使用指南

> Uber 开源的 Go 结构化日志库：零分配热路径、`Logger` / `SugaredLogger` 双 API、可组合的 `zapcore` 管线，兼顾极致性能与工程可控性。
> 仓库：<https://github.com/uber-go/zap>　|　主线版本：**v1.28.x**（API 稳定；整理于 2026-11）　|　协议：MIT

---

## 1. 项目简介

日志库看起来简单，难在**高频路径上的分配与锁**：一次请求打十几条日志，每条都 `fmt.Sprintf` 拼字符串，GC 压力和 CPU 占用会直接吃掉业务预算。Zap 的设计目标就是把这个成本压到接近零，同时不牺牲结构化输出。

核心卖点：

- **零分配热路径**：用强类型 `Field`（`zap.String` / `zap.Int`）替代 `interface{}` 反射，`CheckedEntry` 先判级别再决定是否构造日志，禁用级别的日志几乎不产生分配；
- **双 API**：`Logger` 强类型、最快；`SugaredLogger` 支持 `Printf` / `w` 风格，牺牲一点性能换开发效率；
- **结构化优先**：默认输出 JSON，字段可被 Loki / ELK / Datadog 直接索引，而不是靠正则从文本里抠；
- **可组合内核**：`zapcore.Core` = Encoder + WriteSyncer + LevelEnabler，可 tee 到多目的地、采样、按级别分流；
- **运维内建**：`AtomicLevel` 运行时改级别、采样防日志风暴、Caller/Stacktrace、`zaptest/observer` 单测断言。

适用场景：QPS 较高的在线服务、需要集中式日志检索与告警的后端、库/框架里需要可注入 logger 的场景。**不适合**：CLI 小工具（标准库 `log/slog` 足够）、需要人类优先的极简彩色终端输出（开发期可切 Console encoder）。

### 安装

```bash
go get go.uber.org/zap
# 日志轮转（zap 不含切分，配合 lumberjack）
go get gopkg.in/natefinch/lumberjack.v2
```

### 最小可运行示例

```go
package main

import (
    "time"

    "go.uber.org/zap"
)

func main() {
    logger, _ := zap.NewProduction()
    defer logger.Sync() // 进程退出前 flush，见 6.1

    logger.Info("user login",
        zap.String("user", "alice"),
        zap.Int("attempt", 3),
        zap.Duration("latency", 42*time.Millisecond),
    )
    // {"level":"info","ts":...,"caller":"main/main.go:10","msg":"user login","user":"alice","attempt":3,"latency":0.042}
}
```

---

## 2. 功能特性总览

| 能力 | 说明 |
|---|---|
| 双 API | `Logger`（强类型 Field）/ `SugaredLogger`（`Infow`/`Infof`/`Infoln`），可 `.Sugar()` / `.Desugar()` 互转 |
| 级别 | `Debug` / `Info` / `Warn` / `Error` / `DPanic` / `Panic` / `Fatal`；`AtomicLevel` 运行时动态调整 |
| Encoder | JSON（生产）/ Console（开发）；时间格式、级别字段名、caller 格式均可配置 |
| Core 管线 | `zapcore.NewCore` / `NewTee` / `WrapCore` / `RegisterHooks`，自由组合输出、编码、过滤 |
| 结构化字段 | `String` / `Int` / `Error` / `Any` / `Object` / `Array` / `Namespace` / `Inline`；`With` 绑定公共字段 |
| 上下文 | `logger.With(...)` 派生子 logger；`zap.Stringer` 延迟格式化；`zap.Inline` 展开对象 |
| 采样 | `zapcore.NewSamplerWithOptions`：同一消息在窗口内按 N 条/秒限流，防死循环刷爆磁盘 |
| 调用栈 | `AddCaller()` / `AddCallerSkip(n)` / `AddStacktrace(level)`，可配置到指定级别自动附栈 |
| 测试 | `zaptest/observer` 捕获日志做断言、`zaptest.NewLogger(t)` 输出到测试日志 |
| 输出目标 | `WriteSyncer` 可以是文件、stdout、`zapcore.AddSync` 包装的任意 `io.Writer`、网络 sink |
| 错误语义 | `DPanic`：开发期 panic、生产期仅记日志；`Fatal` 会 `os.Exit(1)`，慎用 |

---

## 3. 核心概念与原理

### 3.1 zapcore 四层管线

```text
Logger.Info(msg, fields...)
   │ ① 级别检查（LevelEnabler）—— 不通过就直接返回，零分配
   ▼
CheckedEntry ──► Core.Write(entry, fields)
                    │ ② Encoder：把 entry + []Field 编码成 []byte（JSON / Console）
                    │ ③ WriteSyncer：写到 stdout / 文件 / 网络
                    ▼
                 缓冲与 Sync
```

这四层各自可替换，是 zap 与「一个 logger 打天下」的库最本质的区别：**过滤、编码、输出解耦**。例如「Error 以上单独写一份文件 + 全量写 stdout」用 `NewTee` 两个 Core 即可，不需要在业务代码里判断级别。

### 3.2 零分配是怎么做到的

- `zap.Field` 是带 `Type` 标签的结构体，内部用 `Integer`/`String`/`Interface` 联合体，避免 `map[string]interface{}` 的装箱与遍历；
- 级别过滤发生在**构造字段之前**：`logger.Debug(...)` 在 `Info` 级别下会走 `Check` 快速返回，参数甚至不会被完整求值（但注意参数里的函数调用仍会执行，见 6.5）；
- `CheckedEntry` 从 `sync.Pool` 复用；
- `SugaredLogger` 因为要处理 `...interface{}` 与反射，会有额外分配，但仍显著快于 logrus。

> 粗略量级（仅作直觉参考，务必自行压测）：`Logger` 每条约 0.1~0.3µs / 0~1 次分配，`SugaredLogger` 约 2~5 倍，logrus 约 10 倍以上。业务里真正的大头通常是**序列化大对象和同步写磁盘**，而不是 logger 本身。

### 3.3 结构化 vs 文本

```text
文本：  "user alice login failed after 42ms"           → 只能全文检索 / 正则
结构化：{"msg":"login failed","user":"alice","ms":42}  → 可按 user 聚合、按 ms 排序告警
```

一旦日志要进集中式系统，**结构化是刚需**。因此字段命名要有规范（`user_id`、`trace_id`、`duration_ms`），不要同一含义在不同地方叫 `uid` / `userID` / `UserID`。

### 3.4 采样原理

`zapcore.NewSamplerWithOptions(core, tick, first, thereafter)`：在时间窗口 `tick` 内，同一条 `msg + 级别` 的前 `first` 条全记，之后每 `thereafter` 条记 1 条。它挡的是「依赖挂了，每个请求都打 Error」这类日志风暴，而不是替代限流；被丢弃的条数需要自己用 `SamplerHook` 统计（见 4.7），zap 不会自动附加字段。

### 3.5 并发与派生：`Named`、`With`、`WrapCore` 的代价

`*zap.Logger` 可以被任意多个 goroutine 直接共用，不需要自己加锁 —— 但这建立在一条**接口契约**上：`zapcore.Core` 必须并发安全（`Write` / `Sync` 会被并发调用）。自己写 Core 时，任何共享可变状态（map、切片、计数器）都要加锁或改 `atomic`；官方 Core 靠 `zapcore.Lock(ws)` 给 writer 加锁、编码缓冲从 `bufferpool` 取来满足这条契约。

派生不是「共享一个指针」。每次 `With` 都做两件事：`log.clone()` 复制 Logger 结构体，然后 `l.core = l.core.With(fields)`；而 `ioCore.With` 的实现是 `addFields(clone.enc, fields)` —— **字段当场被序列化进新 encoder 的缓冲区**，并不是存成 `[]Field` 共享。所以已经绑了 10 个字段的 logger 再 `With` 一次，要把那 10 个字段的字节重拷一遍：字段越多、派生越深，单次 `With` 越贵，但之后每条日志省掉了重复编码。`Named("db")` 则是 `strings.Join` 拼前缀 + 复制 Logger，名字进每条日志的 `logger` 字段（`LoggerName`）；它不做 namespace、不产生嵌套对象，别拿它当字段分组。

```go
package example

import "go.uber.org/zap"

func loadItems(logger *zap.Logger, userID, route string, ids []string) {
	// 反例：循环里反复派生，每条日志都 clone 一次并重编码已绑定字段
	for _, id := range ids {
		logger.With(zap.String("user_id", userID)).Info("loaded", zap.String("item_id", id))
	}

	// 正例：入口派生一次，字段合并进一次 With，只编码一遍
	reqLog := logger.With(zap.String("user_id", userID), zap.String("route", route))
	for _, id := range ids {
		reqLog.Info("loaded", zap.String("item_id", id))
	}
}
```

`zap.New(core)` 与 `logger.WithOptions(...)` 的区别：前者从 Core 造一个全新 Logger（丢掉已绑定的 `name` / 字段，重算 caller 基准），后者是 `clone` + 改选项、**保留已绑定字段**。加 `AddCallerSkip`、调 caller 格式用 `WithOptions`；换一整套 Core 才用 `zap.New`。另外 `WithOptions` 每次调用都会重新 apply 选项 —— 把 `zap.WrapCore(f)` 写进 `WithOptions`，每调一次就再包一层，几周后就是五层包装。

`WrapCore` 做全局拦截（统一补字段、脱敏）必须在**构造期**用，且包装器有两个必做动作，漏一个就静默失效：

```go
package logging

import (
	"go.uber.org/zap"
	"go.uber.org/zap/zapcore"
)

type redactCore struct{ zapcore.Core }

// ① With 之后要返回包装器自身，否则派生一次就退化成内层 Core
func (c redactCore) With(fields []zapcore.Field) zapcore.Core {
	return redactCore{c.Core.With(fields)}
}

// ② Check 必须把 c 自己塞进 CheckedEntry，不能让内层去 AddCore
func (c redactCore) Check(e zapcore.Entry, ce *zapcore.CheckedEntry) *zapcore.CheckedEntry {
	if c.Enabled(e.Level) {
		return ce.AddCore(e, c)
	}
	return ce
}

func (c redactCore) Write(e zapcore.Entry, fields []zapcore.Field) error {
	out := append([]zapcore.Field{zap.String("env", "prod")}, fields...) // 统一补字段
	for i := range out {
		switch out[i].Key {
		case "password", "token", "id_card":
			out[i] = zap.String(out[i].Key, "***")
		}
	}
	return c.Core.Write(e, out)
}
```

装回去用 `zap.New(redactCore{core}, zap.AddCaller())`。`Write` 里的这次拷贝只发生在「已确定要写」的日志上，成本压在 I/O 之下可忽略；但热路径仍优先在调用点控制字段（见 4.9 的 `Safe`）。

---

## 4. 使用实战

### 4.1 生产配置（推荐起点）

```go
package logging

import (
    "go.uber.org/zap"
    "go.uber.org/zap/zapcore"
)

func New(level string) (*zap.Logger, zap.AtomicLevel, error) {
    cfg := zap.NewProductionConfig()
    cfg.Encoding = "json"
    cfg.EncoderConfig.TimeKey = "ts"
    cfg.EncoderConfig.EncodeTime = zapcore.ISO8601TimeEncoder
    cfg.EncoderConfig.EncodeLevel = zapcore.LowercaseLevelEncoder
    cfg.OutputPaths = []string{"stdout"} // 容器环境交给采集 agent，别自己写文件
    cfg.ErrorOutputPaths = []string{"stderr"}
    cfg.Sampling = &zap.SamplingConfig{Initial: 100, Thereafter: 100}

    lvl, err := zap.ParseAtomicLevel(level) // "debug" / "info" ...
    if err != nil { return nil, lvl, err }
    cfg.Level = lvl

    logger, err := cfg.Build(zap.AddCaller(), zap.AddStacktrace(zapcore.ErrorLevel))
    return logger, lvl, err
}
```

> **容器里优先 stdout**：让 Kubernetes / Docker 的日志采集器负责落盘与轮转，应用内写文件容易踩到「日志卷打满」「sidecar 采不到」「多副本文件互相覆盖」三类坑。

### 4.2 开发期彩色 Console

```go
cfg := zap.NewDevelopmentConfig()
cfg.EncoderConfig.EncodeLevel = zapcore.CapitalColorLevelEncoder
logger, _ := cfg.Build()
logger.Debug("cache miss", zap.String("key", "user:42"))
```

### 4.3 多目的地分流（Tee）

```go
encoderCfg := zap.NewProductionEncoderConfig()
encoderCfg.EncodeTime = zapcore.ISO8601TimeEncoder
enc := zapcore.NewJSONEncoder(encoderCfg)

// 全量 → stdout
all := zapcore.NewCore(enc, zapcore.Lock(os.Stdout), zapcore.DebugLevel)
// Error 以上 → 单独文件（便于告警系统只 tail 这一个文件）
errFile := zapcore.AddSync(&lumberjack.Logger{
    Filename: "/var/log/app/error.log", MaxSize: 100, MaxBackups: 7, MaxAge: 30,
})
errs := zapcore.NewCore(enc, errFile, zapcore.ErrorLevel)

logger := zap.New(zapcore.NewTee(all, errs), zap.AddCaller())
```

> `zapcore.Lock` 是必要的：`os.Stdout` 不是并发安全的 writer，多 goroutine 直接写会产生行交错。

### 4.4 上下文字段与 trace 串联

```go
// 请求入口绑定一次，后续全链路自动带上
reqLog := logger.With(
    zap.String("trace_id", traceID),
    zap.String("route", c.FullPath()),
)
reqLog.Info("request start")
// ... 把 reqLog 放进 context 传递，而不是每次重新拼字段
reqLog.Warn("slow query", zap.Duration("elapsed", d), zap.String("sql", sql))
```

```go
// 从 context 取 logger 的小工具
type ctxKey struct{}
func FromContext(ctx context.Context) *zap.Logger {
    if l, ok := ctx.Value(ctxKey{}).(*zap.Logger); ok { return l }
    return zap.L()
}
func WithContext(ctx context.Context, l *zap.Logger) context.Context {
    return context.WithValue(ctx, ctxKey{}, l)
}
```

### 4.5 运行时动态调整级别

```go
var atom = zap.NewAtomicLevelAt(zap.InfoLevel)

func newLogger() *zap.Logger {
	cfg := zap.NewProductionConfig()
	cfg.Level = atom
	logger, _ := cfg.Build()
	return logger
}

// 排障时临时开 Debug，无需重启；用完恢复
func enableDebugTemporarily() {
	atom.SetLevel(zap.DebugLevel)
	defer atom.SetLevel(zap.InfoLevel)
}
```

生产环境可把 `atom` 暴露到一个受控的 admin 接口（务必鉴权），或接配置中心，实现「出问题临时降级别、完事恢复」。

### 4.6 单测断言

```go
func TestWarnOnRetry(t *testing.T) {
    core, logs := observer.New(zap.WarnLevel)
    svc := NewService(zap.New(core))

    svc.Do()

    entries := logs.FilterMessage("retrying").All()
    require.Len(t, entries, 1)
    require.Equal(t, "retrying", entries[0].Message)
    require.Equal(t, int64(3), entries[0].ContextMap()["attempt"])
}
```

### 4.7 采样策略：高频日志怎么不炸

采样器按 `(级别, 消息)` 分组计数：每个 `tick` 窗口内，同一组的前 `initial` 条原样输出，之后每 `thereafter` 条抽 1 条，其余丢弃。v1.28 的计数器是 `[级别][4096]` 的原子数组，key 走 FNV 哈希落到槽位 —— 所以**不同消息可能撞槽、共享同一个计数**，这是为了「内存有界 + 无锁」有意做的取舍。

| 参数 | 含义 | 调参经验 |
|---|---|---|
| `tick` | 统计窗口（默认 1s） | 窗口越短，突发越容易被完整放行；窗口太长会把「每秒一条」的正常日志也误杀 |
| `initial` | 窗口内前 N 条全记 | 取「正常峰值 1~2 秒的条数」，太小会丢掉排障最需要的头几条 |
| `thereafter` | 之后每 M 条记 1 条 | 100 起步，等于把后续部分压到约 1% |

```json
{"level":"info","encoding":"json","sampling":{"initial":100,"thereafter":100}}
```

`zap.Config` 只能表达 `initial` / `thereafter`（`tick` 固定 1s），要自定义窗口就手写 `NewSamplerWithOptions`。

**采样日志必须能算出丢了多少**：采样后日志条数 ≠ 真实事件数，如果不知道丢了多少，Error 率、告警阈值、容量规划全是错的。两种补法 —— 用 `SamplerHook` 把丢弃数打成指标（推荐，聚合本就该交给指标系统），或给每条采样日志加 `sampled` / `dropped` 字段（下游可求和）：

```go
package logging

import (
	"sync/atomic"
	"time"

	"go.uber.org/zap/zapcore"
)

// Dropped 累计被采样器丢弃的条数，挂到 /metrics 上
var Dropped atomic.Uint64

func newSampler(core zapcore.Core) zapcore.Core {
	return zapcore.NewSamplerWithOptions(core, time.Second, 100, 100,
		zapcore.SamplerHook(func(_ zapcore.Entry, dec zapcore.SamplingDecision) {
			if dec&zapcore.LogDropped != 0 {
				Dropped.Add(1)
			}
		}))
}
```

**分级别采样**：Error 不采样。做法不是「采样整个 logger」，而是用 `NewTee` 拼两条链路 —— Info/Warn 那条 Core 外面套 sampler，Error 那条用 `zapcore.NewCore(enc, errSink, zapcore.ErrorLevel)` 全量写并单独告警。采样器只作用在它包住的那个 Core 上，Tee 的另一支不受影响。

- **消息里不要拼变量**。采样 key 是整条 `msg`，`"user "+id+" failed"` 会让每条都成为独立 key，采样彻底失效（同时也违反 4.9 的字段规范）。
- **应用层采样是第一道，采集端限流是最后一道**：Loki 侧的 `ingestion_rate_mb` / `per_stream_rate_limit` 一旦触发就返回 429，Promtail 重试后丢批次 —— 那是「不可控的丢」；应用层采样才是「可解释、有计数」的丢，别指望采集端兜底。

### 4.8 接 OpenTelemetry：把 trace_id / span_id 写进每条日志

分工一句话：**trace 记录「一次请求经过谁、各花多久」，log 记录「当时发生了什么」**；日志里带 `trace_id` 是把两者缝起来成本最低的手段，链路侧的细节（Span、Propagator、Collector）见 [OpenTelemetry 篇](../opentelemetry/opentelemetry-guide.md)。

字段名要对齐 OTel 语义约定：`trace_id`（32 位小写 hex）、`span_id`（16 位小写 hex），不要写 `traceId` / `TraceID` / `trace-id` —— Grafana、Tempo、Jaeger 的默认解析都按这套来。取 SpanContext 的小工具：

```go
package observability

import (
	"context"

	"go.opentelemetry.io/otel/trace"
	"go.uber.org/zap"
)

// TraceFields 从 ctx 提取 trace_id / span_id；没有有效 span 时返回 nil。
// OTel 的零值 SpanContext 是 invalid，别把全 0 的 id 打进日志。
func TraceFields(ctx context.Context) []zap.Field {
	sc := trace.SpanContextFromContext(ctx)
	if !sc.IsValid() {
		return nil
	}
	return []zap.Field{
		zap.String("trace_id", sc.TraceID().String()),
		zap.String("span_id", sc.SpanID().String()),
	}
}
```

注入时机只能**在调用点**：zap 的 `Logger` 不携带 `context.Context`（`zapcore.Entry` 里也没有 ctx 字段），Core 拿不到请求上下文。所以要在入口派生 logger、写回 ctx 往下传（`WithContext` 见 4.4）：

```go
package observability

import (
	"net/http"

	"go.opentelemetry.io/otel"
	"go.uber.org/zap"
)

var tracer = otel.Tracer("http")

// 在 OTel 中间件里派生 logger：span 必须先 Start，trace_id 才有值
func LoggingMiddleware(base *zap.Logger, next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		ctx, span := tracer.Start(r.Context(), r.Method+" "+r.URL.Path)
		defer span.End()

		l := base.With(TraceFields(ctx)...)
		next.ServeHTTP(w, r.WithContext(WithContext(ctx, l)))
	})
}
```

**与 `otelzap` bridge 的关系**：`go.opentelemetry.io/contrib/bridges/otelzap` 能把 zap Core 换成「直接产出 OTel LogRecord」的 Core，由 SDK 自动补当前 span 的 `trace_id` / `span_id`。代价是每条日志都走一遍 `LoggerProvider`（属性转换 + batch 开销），且整条采集链必须走 OTLP。**已有 Loki / Promtail / Alloy 体系的项目，继续让 zap 打 JSON、由采集器解析 `trace_id` 更简单**；只有「日志也要和 trace / metrics 走同一个 OTLP Collector」时才值得上 bridge。

双向跳转靠 Grafana 的 derived field：在 Loki 数据源里加一条 `matcherRegex: "\"trace_id\":\"([0-9a-f]+)\""` 指向 Tempo，点日志上的 `trace_id` 直接跳过去；反向（Tempo → Loki）由 Trace 视图按 `service.name` + 时间范围反查，所以 `service` 字段必须和 OTel 的 `service.name` 资源属性**逐字一致**，否则两边对不上。

### 4.9 日志规范：字段、错误与脱敏

| 约定 | 内容 |
|---|---|
| 固定字段 | `service`、`version`、`env` 由构造期 `zap.New(..., zap.Fields(...))` 或 `WrapCore` 统一注入；`ts` / `level` / `caller` / `msg` 由 encoder 负责，业务代码不要重复写 |
| 命名 | 全 `snake_case`，单位进名字（`duration_ms`、`size_bytes`），时间统一 ms 整数或 RFC3339；布尔用肯定式（`cached`、`ok`），别造 `not_ready` |
| 消息 | `msg` 是**常量**，描述「发生了什么」，所有变量进字段。`fmt.Sprintf` / 字符串拼接会把结构化压回文本，检索、聚合、采样三件事同时失效；同一含义也只允许一个名字（`uid` / `userID` / `userId` 并存等于下游要写三套查询） |

错误的边界要拎清：`zap.Error(err)` 只编码 error 的类型与消息，**不产生堆栈**。堆栈有两个来源，二选一即可 —— 全局 `zap.AddStacktrace(zapcore.ErrorLevel)`，或单点 `zap.Stack("stack")`。两处都加就是每行日志两份栈，白白撑大存储。

```go
package logging

import "go.uber.org/zap"

// Safe：字段级脱敏，敏感 key 一律掩码。命不中的日志零额外开销。
func Safe(key, value string) zap.Field {
	switch key {
	case "password", "token", "authorization", "id_card", "phone":
		if len(value) > 4 {
			return zap.String(key, value[:2]+"***"+value[len(value)-2:])
		}
		return zap.String(key, "***")
	}
	return zap.String(key, value)
}

func logFailure(l *zap.Logger, orderID string, err error) {
	l.Error("charge failed",
		zap.String("order_id", orderID),
		zap.Error(err),     // 结构化 err 字段，nil 也安全
		zap.Stack("stack"), // zap.Error 不带栈，要栈就显式加
	)
}
```

脱敏三条路，按成本从低到高：**①** 字段级 helper（上面的 `Safe`，只在调用点生效，最便宜）；**②** 干脆不打（白名单，敏感字段根本进不了日志，最可靠）；**③** 自定义 `zapcore.Encoder` 或 3.5 那种 `WrapCore` 兜底（覆盖全链路，但每条日志都过一遍判断）。实践是 ① + ②，③ 作为最后一道防线。

级别判断标准（写进 code review 清单，比写在文档里有效）：

| 级别 | 语义 | 例子 | 是否告警 |
|---|---|---|---|
| `Debug` | 排障细节，生产默认关 | 请求/响应体、SQL、缓存 key | 否 |
| `Info` | 关键业务事件，能重建「发生了什么」 | 启停、配置加载、订单状态迁移 | 否 |
| `Warn` | 可自愈 / 已降级 | 重试后成功、缓存穿透回源、慢查询 | 看趋势 |
| `Error` | **需要人介入** | 依赖不可用、数据不一致、recover 到的 panic | 是 |

日志量预算：**每请求 ≤ 3 条**（入口一条、结果一条、异常一条）。中间的每一步不该用日志表达，那是 span 的职责 —— 一条请求 20 条日志的系统，采样只能救急，救不了成本。

---

## 5. 版本演进速览

| 阶段 | 关键变化 |
|---|---|
| 早期 v1.x | `Logger` / `SugaredLogger` 双 API、`zapcore` 分层、JSON/Console encoder、`NewProduction` 预设 |
| v1.1x~1.2x | `AtomicLevel`、`zaptest/observer`、`RegisterHooks`、`zap.Inline` / `zap.Object` 等字段能力完善 |
| v1.2x 起 | `DPanic`、`NewStdLog` 桥接标准库、Sinks（`zap.Open` 支持 URL 形式输出目标）、采样器选项化 |
| v1.15 | `zapcore.NewSamplerWithOptions` 与 `SamplerHook` 引入（可拿到 `LogDropped` 决策并自行计数） |
| 近年 v1.26~1.27 | 依赖升级、错误与 `Any` 字段行为细化；**API 长期稳定，无破坏性变更** |

**与标准库 `log/slog`（Go 1.21+）的关系**：slog 已把结构化日志纳入标准库，通过 `zap.NewStdLog` 或 `slog.New(zapslog.NewHandler(core))`（`go.uber.org/zap/exp/zapslog`）可互操作。**新项目若不想引第三方依赖，slog 是合理选择**；已有 zap 生态、需要采样/Tee/极致性能的场景继续用 zap。

> zap 的 v1 已稳定多年，升级成本极低；锁 `go.mod` 版本即可，注意 `go.uber.org/multierr` 等间接依赖一起升。

---

## 6. 常见坑与最佳实践

1. **忘记 `Sync()`**：`NewProduction` 带缓冲，进程被 `SIGKILL` 或 `os.Exit` 时最后一批日志会丢。在 `main` 里 `defer logger.Sync()`；注意 `Fatal` 会直接 `os.Exit(1)`，**defer 不会执行**，关键场景先 `Sync` 再 `Fatal`，或改用 `Error` + 显式返回。
2. **热路径滥用 `SugaredLogger` 与 `fmt.Sprintf`**：`logger.Info(fmt.Sprintf("user %s cost %dms", u, ms))` 把结构化字段又压回字符串，既慢又不可索引。改成 `logger.Info("request done", zap.String("user", u), zap.Int64("ms", ms))`。
3. **日志里打敏感信息**：密码、token、身份证、完整请求体。建一个字段白名单或在 encoder 层做脱敏 Hook，比靠 code review 可靠。
4. **不设采样**：下游故障时的循环重试能在一分钟内写满磁盘。至少给 `Info/Warn` 配 `Sampling`，Error 单独分流并告警。
5. **以为「级别过滤」能省掉参数计算**：`logger.Debug("x", zap.String("k", expensive()))` 里 `expensive()` **仍会执行**（Go 的函数参数先求值）。昂贵字段要自己先判 `logger.Core().Enabled(zap.DebugLevel)`。
6. **用 `zap.Any` 打大对象**：`Any` 走反射，结构体大时开销陡增且可能输出海量字段。热路径用具体类型 Field，或只挑关键字段。
7. **Error 级别滥用**：把「用户输入不合法」打成 `Error` 会让告警全是噪音。约定：`Error` = 需要人介入的系统故障，`Warn` = 可自愈/降级，`Info` = 关键业务事件。
8. **`zap.Error(err)` 而不是 `zap.String("error", err.Error())`**：前者保留结构化 error 字段，且对 nil 安全；后者丢类型、易 panic。
9. **封装后 Caller 指错文件**：自己包一层 `log.Info()` 会导致 `caller` 显示封装文件。用 `zap.AddCallerSkip(1)` 或 `logger.WithOptions(zap.AddCallerSkip(1))`。
10. **每次请求新建 Logger**：`cfg.Build()` 很重。全局建一次，用 `logger.With(...)` 派生带上下文的 logger；但 `With` 会 clone encoder 并把已绑定字段重新编码，字段多、循环里反复派生时并不便宜（见 3.5）。
11. **多 goroutine 写同一个 `os.File` 不加 Lock**：日志行交错、JSON 被撕坏。用 `zapcore.Lock(os.Stdout)` 或让每个 Core 独占 writer。
12. **字段名不统一**：同一含义出现 `userID` / `user_id` / `uid`，下游聚合要写三套查询。制定字段词典并写进 lint / code review 清单。
13. **把日志当指标/链路用**：`duration_ms` 可以辅助排障，但 SLO 与 P99 应交给 [Prometheus 类指标系统](https://prometheus.io/)，跨服务调用链交给 OpenTelemetry，三者不要互相替代。

---

## 7. 选型对比

| 维度 | zap | log/slog（标准库） | zerolog | logrus | apex/log |
|---|---|---|---|---|---|
| 依赖 | 第三方 | **零依赖**（Go 1.21+） | 第三方 | 第三方（维护模式） | 第三方 |
| 性能 | **极高**（零分配热路径） | 中～高（视 Handler） | **极高**（链式 API） | 低（反射 + 锁） | 中 |
| API 风格 | 强类型 Field / Sugared | `slog.Info(msg, attrs...)` | 链式 `log.Info().Str(...).Msg()` | `WithFields` | 类 logrus |
| 结构化 | JSON 原生 | JSON/Text Handler | JSON 原生 | JSON 需 formatter | 有 |
| 采样 | **内建** | 需自定义 Handler | 需自实现 | 无 | 无 |
| 多目的地 / 分流 | **Core + Tee，最灵活** | 多 Handler 包装 | Writer 分级 | Hook | Handler |
| 动态级别 | `AtomicLevel` | `LevelVar` | 全局 Level | SetLevel | 有 |
| 生态成熟度 | **最成熟**，大量库直接支持 | 官方，快速成长 | 活跃 | 存量项目多 | 小众 |

一句话决策：

- **高性能在线服务、需要采样与多目的地分流、已有可观测体系** → **zap**，热路径用 `Logger`，脚本/低频逻辑才用 `SugaredLogger`；
- **新项目、想少一个依赖、性能不是瓶颈** → **标准库 `log/slog`**，配合自定义 Handler 也能覆盖大部分需求；
- **追求极简链式写法与最小分配** → zerolog，但生态与分流能力不如 zap；
- **存量 logrus 项目** → 不必强迁；若日志已成为 CPU 瓶颈，可用 `slog` Handler 做渐进替换；
- **CLI / 小工具** → 标准库 `log` 或 slog 的 TextHandler 就够，别引入 zap 的配置复杂度。

> 真正影响可观测性的往往不是选哪个库，而是**字段规范、trace_id 全链路透传、级别语义约定、以及日志/指标/链路三者的职责边界**。库只解决「写得快」，规范才决定「查得到」。
