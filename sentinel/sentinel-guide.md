# Sentinel-Go 限流熔断实战指南

> **一句话定位**：把「限流、熔断、热点保护」从架构图上的三个词，变成能用规则动态调整的运行时能力。
>
> **上游仓库**：[alibaba/sentinel-golang](https://github.com/alibaba/sentinel-golang) · **版本基线**：sentinel-golang v1.0.4 · **协议**：Apache-2.0

---

## 1. 项目简介

[服务治理](../service-governance/service-governance-guide.md) 讲的是算法与取舍：令牌桶为什么允许突发、半开该放几个探针、重试预算怎么算。本篇是它的落地篇，只回答工程问题：**规则怎么定义、埋点写在哪、阈值怎么热更新、拦截量怎么变成指标**。概念不重复，遇到不清楚的机制先回上篇。

Sentinel 把进程内的保护拆成四件互不替代的事：

- **限流**：入口流量超过本机容量时挡住多余请求，保护的是自己；
- **熔断**：下游持续变慢或报错时停止调用，保护的是调用链和下游的恢复时间；
- **降级**：被限、被熔之后返回什么，这是业务决策，SDK 只能给你一个明确的 block 信号；
- **热点保护**：单个参数值（大 V 的 uid、爆款 sku）打爆服务时只限制这个值，不牵连其他用户。

为什么不能全靠网关？网关在进程外，能看见路由、IP、Header、总 QPS，看不见「这个进程还有多少 goroutine 卡在等 DB」「这个方法 P99 是 300ms 还是 3s」「uid=42 一个值占了 80% 的流量」。**单机并发、方法级 RT、参数值维度，只有进程内埋点才拿得到**。反过来，跨实例的总配额应该在网关或 Redis 做，SDK 的单机阈值乘以副本数就是集群上限，副本一扩就失真。两层是叠加关系，取更严格者生效。

依赖只有一条：`go get github.com/alibaba/sentinel-golang@v1.0.4`（Apache-2.0）。它会在 `go.mod` 里带入 `prometheus/client_golang`、`pkg/errors` 等依赖，不是零依赖的小库；v1.0.4 的 `go.mod` 仍写着 `go 1.13`，但代码能在 Go 1.21+ 正常编译。

---

## 2. 能力总览

| 能力 | 触发条件（判定依据） | 典型参数 | 规则包 |
|---|---|---|---|
| 流控 · 快速失败 | 窗口内通过数 ≥ Threshold | `Threshold`、`StatIntervalInMs` | `core/flow` |
| 流控 · 预热 | 同上，但允许阈值从 `Threshold/coldFactor` 线性爬升 | `WarmUpPeriodSec`、`WarmUpColdFactor` | `core/flow` |
| 流控 · 匀速排队 | 请求间隔小于 `1000/Threshold` ms 且排队未超时 | `MaxQueueingTimeMs` | `core/flow` |
| 熔断 · 慢调用比例 | 窗口内 `RT > MaxAllowedRtMs` 的占比 > Threshold | `MaxAllowedRtMs`、`MinRequestAmount` | `core/circuitbreaker` |
| 熔断 · 异常比例 | `err != nil` 的占比 > Threshold | `Threshold`、`StatIntervalMs` | `core/circuitbreaker` |
| 熔断 · 异常数 | 窗口内错误数 > Threshold | `Threshold`、`MinRequestAmount` | `core/circuitbreaker` |
| 热点参数 | 单个参数值的 QPS 或并发数 > Threshold | `ParamKey`、`BurstCount`、`DurationInSec`、`ParamsMaxCapacity` | `core/hotspot` |
| 系统自适应 | 整机 Load / CPU / 并发 / 入口 QPS / 平均 RT 超阈值 | `MetricType`、`TriggerCount`、`Strategy: BBR` | `core/system` |
| 规则动态推送 | 外部配置变更后调用 `LoadRules` | — | 自建或 `ext/datasource` |

规则是**数据**，不是代码：`flow.Rule` 是普通结构体，`LoadRules` 可以随时整批替换，不需要重启进程。这是 Sentinel 与 `x/time/rate` 最本质的差别——后者要改限流值只能改代码或自己加一层 setter。

入口 `Entry` 会依次过一遍 slot chain（统计、系统、流控、热点、熔断），任一 slot 返回 blocked 就短路，后面的规则不再执行。所以**同一个资源同时命中流控和熔断时，你拿到的 block type 是流控**，排查时别去翻熔断配置。

---

## 3. 核心概念与原理

### 3.1 资源与 Entry 的生命周期

资源名是 Sentinel 里唯一的绑定键：规则按资源名下发，埋点用同一个字符串进入。`sentinel.Entry` 返回两个值，语义必须记牢：

```text
Entry("order.create")
  ├─ 取 EntryContext + ResourceWrapper，过 slot chain（统计/系统/流控/热点/熔断）
  ├─ 被拦截 → (nil, *BlockError)，entry 为 nil
  └─ 放行   → (*SentinelEntry, nil)
e.Exit(...) 回写 RT/错误/并发数并归还 context，sync.Once 只执行一次
```

三个必须遵守的约束：**先判 `blockErr` 再 defer `Exit`**（blocked 时 entry 为 nil，defer 会 panic）；`Exit` 必须 defer（panic 或提前 return 都会漏掉统计）；错误要通过 `Exit(base.WithError(err))` 回填（否则熔断的异常比例永远是 0）。

### 3.2 滑动窗口：LeapArray 的桶与精度

Sentinel 用环形数组做滑动窗口，核心是三个量：`intervalInMs`（窗口长度）、`sampleCount`（桶数）、`bucketLengthInMs = intervalInMs / sampleCount`。全局统计默认 **1000ms 窗口、2 个桶**，即每桶 500ms。

```text
idx = (now / bucketLengthInMs) % sampleCount
bucketStart = now - now % bucketLengthInMs

时间轴   0     500    1000   1500   2000
桶       B0     B1     B0     B1     B0    ← 同一个数组循环复用
读取     最近 2 个未过期桶之和 = 最近 1s 的通过数
过期     now - bucketStart > intervalInMs 的桶直接跳过
```

写路径只对当前桶做原子累加；只有跨周期复用旧桶时才用 `TryLock` + CAS 重置，读路径无锁遍历。这带来两个工程结论：

- **精度是桶长，不是毫秒**。500ms 桶意味着限流判定每 500ms 才校准一次，窗口交界处仍可能多放接近一个桶的量。桶数由全局统计配置决定（默认 2），不能按规则单独调；`StatIntervalInMs` 必须是全局桶长的整数倍，否则内部 `sampleCount` 直接退化成 1。
- **熔断的 `StatSlidingWindowBucketCount` 必须整除 `StatIntervalMs`**，不满足时会被静默改成 1，同样退化成固定窗口——配置看起来生效了，实际精度全丢。

为什么不用固定窗口？固定窗口在交界处能放过 2 倍阈值：阈值 100 QPS，窗口 1 的最后 100ms 进 100 个、窗口 2 的前 100ms 再进 100 个，200ms 内实际 200 QPS。滑动窗口把「最近一个完整窗口」作为判定区间，消灭了这种整点突刺，但它是**计数**语义：窗口内先放进 100 个，第 101 个才拒，这 100 个仍可能挤在同一毫秒。

### 3.3 流控效果：两个字段组合出的三种行为

规则里其实是两个字段在配合：`TokenCalculateStrategy`（Direct / WarmUp）决定「允许的阈值怎么算」，`ControlBehavior`（Reject / Throttling）决定「超阈值怎么办」。

| 效果 | 字段组合 | 流量形态 | 不适合 |
|---|---|---|---|
| 快速失败 | `Direct` + `Reject` | 入口 API，超了立刻 429 | 需要平滑出口配额 |
| 预热 | `WarmUp` + `Reject` | 刚启动、缓存冷、连接池未建 | 已预热实例的滚动重启（浪费容量） |
| 匀速排队 | `Direct` + `Throttling` | 调用第三方、批处理 | 入口高并发（占 goroutine、放大尾延迟） |

**Warm Up**：起始阈值约为 `Threshold / WarmUpColdFactor`（默认 3），在 `WarmUpPeriodSec` 内线性升到 `Threshold`。它解决的是「新实例一上线就被打满，缓存还没热，全部超时」的问题。但冷启动比多数人预期长：阈值 300、预热 600s，前 100s 只能过 100 QPS，扩容后容量爬升很慢；而且改一次规则会重建 controller，预热重新开始。

**匀速排队**是漏桶：请求在 slot 里 `Sleep(nanosToWait)` 等到下一个可用间隔，排队超过 `MaxQueueingTimeMs` 才拒。注意这个 sleep 发生在**调用方 goroutine** 里，入口用它等于把「快速拒绝」变成「慢拒绝」，连接和 goroutine 都被占着。它只适合出口配额，且 `MaxQueueingTimeMs` 必须小于上游剩余 timeout。

### 3.4 熔断：三种策略与半开探测

```text
                 窗口内比例/数量超阈值
   closed ────────────────────────────────► open
     ▲                                        │ RetryTimeoutMs 到期 + CAS
     │ 探针全部成功                            ▼
     └────────────────────────────────── half-open
                         任一探针失败 → 立即回 open，重置冷却
```

| 策略 | 统计量 | Threshold 含义 | 什么时候用 |
|---|---|---|---|
| `SlowRequestRatio` | RT > `MaxAllowedRtMs` 的请求占比 | 比例 0~1 | 首选。雪崩早期错误率还是 0，RT 已经涨了 10 倍 |
| `ErrorRatio` | `err != nil` 的占比 | 比例 0~1 | 下游明确返回错误、连接失败 |
| `ErrorCount` | 窗口内错误绝对数 | 个数 | 高流量且错误预算明确，小流量下几乎不可用 |

三个参数决定它是否可靠：`MinRequestAmount` 是判定门槛（10s 内至少 20 个样本才允许熔断，避免「3 个请求错 2 个 = 66%」）；`StatIntervalMs` 是统计窗口；`RetryTimeoutMs` 是 open 冷却时长，必须大于下游典型恢复时间（重启 + 预热），否则会陷入「打开→半开→失败→打开」的抖动。

半开不是「全量放行试探」：`RetryTimeoutMs` 到期后，只有一个 goroutine 能 CAS 成功进入 half-open，随后按 `ProbeNum` 放行探针（不设时等价于单探针语义），**其余并发请求仍然被拒**。任一探针失败立刻回 open 并重置冷却；探针数量凑齐且全部成功才 closed，同时清空统计窗口。`ProbeNum` 建议 3~10，且探针要带独立短超时，别让一个卡住的探针把状态机吊在半开。

### 3.5 热点参数：按参数值限流

普通流控看的是资源总量，热点规则看的是**每个参数值各自一份配额**。规则字段不多，但每个都影响行为：

| 字段 | 作用 | 注意 |
|---|---|---|
| `ParamKey` | 从 `Entry` 的 Attachments 取值 | 优先级高于 `ParamIndex`，取不到就跳过（不限流） |
| `ParamIndex` | 从 `WithArgs(...)` 的位置取值，负数从末尾算 | `ParamKey` 优先；参数位置改了规则就错位 |
| `MetricType` | `QPS` 或 `Concurrency` | 并发维度适合「每个用户只能同时跑 1 个导出任务」 |
| `Threshold` | 每个参数值在 `DurationInSec` 内的令牌数 | 不是全局阈值 |
| `BurstCount` | 额外可攒的令牌，桶容量 = Threshold + BurstCount | 只对 QPS + `Reject` 生效 |
| `DurationInSec` | 统计窗口 | 调大等于放宽瞬时速率 |
| `ParamsMaxCapacity` | 参数值统计槽的 LRU 上限 | QPS 模式不设时为 `min(20000, 4000 × DurationInSec)` |
| `SpecificItems` | 给特定值单独阈值 | 大客户白名单，值类型要与参数一致 |

`Reject` 的语义是每参数一个令牌桶：每 `DurationInSec` 补 `Threshold` 个令牌，最多存 `Threshold + BurstCount` 个；`Throttling` 则按 `DurationInSec / Threshold` 的间隔匀速放行，超过 `MaxQueueingTimeMs` 拒绝。统计槽存在 LRU 里，所以「大 V 保护」不会无限吃内存——但前提是你设了合理的容量，且没有拿一个基数上千万的字段当 `ParamKey`。

---

## 4. 使用实战

### 4.1 最小可跑：初始化 + 一条流控规则

```go
package main

import (
	"log"
	"net/http"

	sentinel "github.com/alibaba/sentinel-golang/api"
	"github.com/alibaba/sentinel-golang/core/base"
	"github.com/alibaba/sentinel-golang/core/flow"
)

const resUserQuery = "user.query"

func userQuery(w http.ResponseWriter, r *http.Request) {
	e, b := sentinel.Entry(resUserQuery, sentinel.WithTrafficType(base.Inbound))
	if b != nil {
		http.Error(w, "busy", http.StatusTooManyRequests)
		return
	}
	defer e.Exit()
	_, _ = w.Write([]byte(`{"ok":true}`))
}

func main() {
	if err := sentinel.InitDefault(); err != nil {
		log.Fatal(err)
	}
	_, err := flow.LoadRules([]*flow.Rule{{
		Resource:               resUserQuery,
		TokenCalculateStrategy: flow.Direct,
		ControlBehavior:        flow.Reject,
		Threshold:              100,  // 单机 100 QPS
		StatIntervalInMs:       1000, // 1s 窗口，默认 2 个 500ms 桶
	}})
	if err != nil {
		log.Fatal(err)
	}
	http.HandleFunc("/api/user", userQuery)
	log.Fatal(http.ListenAndServe(":8080", nil))
}
```

`InitDefault` 只需在进程启动时调用一次。规则里的 `Resource` 必须与 `Entry` 的字符串逐字节一致，大小写、空格、前缀都算。`WithBatchCount(n)` 让一次 Entry 消耗 n 个 token（批量消费必用，否则 50 条消息只算 1 QPS）；`WithTrafficType(base.Inbound)` 决定它是否计入系统规则的入口 QPS / 平均 RT，内部 RPC、DB 调用应标 `Outbound`。

### 4.2 熔断：给下游调用加保护与降级返回

```go
package downstream

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"time"

	sentinel "github.com/alibaba/sentinel-golang/api"
	"github.com/alibaba/sentinel-golang/core/base"
	"github.com/alibaba/sentinel-golang/core/circuitbreaker"
)

const resUserSvc = "downstream.user.get"

var (
	ErrDegraded     = errors.New("user-service degraded")
	ErrUserNotFound = errors.New("user not found")
	httpCli         = &http.Client{Timeout: 800 * time.Millisecond}
)

func initUserBreaker() error {
	_, err := circuitbreaker.LoadRules([]*circuitbreaker.Rule{{
		Resource:         resUserSvc,
		Strategy:         circuitbreaker.SlowRequestRatio,
		RetryTimeoutMs:   5000,  // open 冷却 5s
		MinRequestAmount: 20,    // 10s 内至少 20 个样本
		StatIntervalMs:   10000, // 统计窗口 10s
		MaxAllowedRtMs:   300,   // 超过 300ms 算慢调用
		Threshold:        0.5,   // 慢调用占比 > 50% 熔断
		ProbeNum:         3,     // 半开放 3 个探针
	}})
	return err
}

// GetUser 返回值里 (nil, ErrUserNotFound) 是业务结果，不算下游故障。
func GetUser(ctx context.Context, id string) (body []byte, err error) {
	e, b := sentinel.Entry(resUserSvc)
	if b != nil {
		return nil, fmt.Errorf("%w: %s", ErrDegraded, b.BlockType())
	}
	defer func() {
		if errors.Is(err, ErrUserNotFound) { // 业务错误不回填，避免正常波动触发熔断
			e.Exit()
			return
		}
		e.Exit(base.WithError(err))
	}()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, "http://user-svc/users/"+id, nil)
	if err != nil {
		return nil, err
	}
	resp, err := httpCli.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()

	if resp.StatusCode == http.StatusNotFound {
		return nil, ErrUserNotFound
	}
	if resp.StatusCode >= 500 {
		return nil, fmt.Errorf("upstream status %d", resp.StatusCode)
	}
	return io.ReadAll(resp.Body)
}
```

启动时调用 `initUserBreaker()`；调用方拿到 `ErrDegraded` 就走兜底（缓存快照、空对象 + `degraded: true`），不要返回 `nil, nil`。另一个细节：`http.Client.Timeout` 要明显大于 `MaxAllowedRtMs`（例如 2~3 倍），否则慢调用在客户端就被掐断，`SlowRequestRatio` 永远统计不到；它同时要小于上游剩余 deadline。

### 4.3 热点参数：按 uid 限流

```go
package hotpath

import (
	"net/http"

	sentinel "github.com/alibaba/sentinel-golang/api"
	"github.com/alibaba/sentinel-golang/core/hotspot"
)

const resProfile = "user.profile"

func initHotspotRules() error {
	_, err := hotspot.LoadRules([]*hotspot.Rule{{
		Resource:          resProfile,
		MetricType:        hotspot.QPS,
		ControlBehavior:   hotspot.Reject,
		ParamKey:          "uid", // 从 WithAttachment 取，取不到则不限流
		Threshold:         20,    // 每个 uid 每秒 20 次
		BurstCount:        5,     // 桶容量 25
		DurationInSec:     1,
		ParamsMaxCapacity: 5000, // 只保留 5000 个活跃 uid 的统计槽
	}})
	return err
}

func profileHandler(w http.ResponseWriter, r *http.Request) {
	uid := r.URL.Query().Get("uid")
	e, b := sentinel.Entry(resProfile, sentinel.WithAttachment("uid", uid))
	if b != nil {
		http.Error(w, "too many requests for this user", http.StatusTooManyRequests)
		return
	}
	defer e.Exit()
	_, _ = w.Write([]byte(`{"uid":"` + uid + `"}`))
}
```

启动时调用 `initHotspotRules()`；参数也可以用位置传：`sentinel.WithArgs(uid, skuID)` 配 `ParamIndex: 0`，`-1` 表示最后一个。位置传参在 handler 签名变化时最容易被改错，优先 `ParamKey` + `WithAttachment`：名字改了编译期还能 grep 到。

### 4.4 规则动态化：从 etcd 拉取并热更新

`ext/datasource` 只提供了 `DataSource` / `PropertyHandler` 抽象和文件源，etcd、Nacos 需要自己接。自己接的好处是能控制版本、校验和失败策略：

```go
package ruler

import (
	"context"
	"encoding/json"
	"log"
	"sync/atomic"

	"github.com/alibaba/sentinel-golang/core/flow"
	clientv3 "go.etcd.io/etcd/client/v3"
)

type flowDoc struct {
	Rules []*flow.Rule `json:"rules"`
}

var lastRev atomic.Int64 // 用 etcd ModRevision 做幂等键

func Watch(ctx context.Context, cli *clientv3.Client, key string) error {
	load := func(raw []byte, rev int64) {
		var doc flowDoc
		if err := json.Unmarshal(raw, &doc); err != nil {
			log.Printf("sentinel: bad payload, keep old rules: %v", err) // fail-safe
			return
		}
		if rev <= lastRev.Load() { // 幂等：乱序/重复推送直接丢
			return
		}
		if _, err := flow.LoadRules(doc.Rules); err != nil {
			log.Printf("sentinel: load rules failed, keep old: %v", err)
			return
		}
		lastRev.Store(rev)
	}
	resp, err := cli.Get(ctx, key)
	if err != nil {
		return err
	}
	if len(resp.Kvs) > 0 {
		load(resp.Kvs[0].Value, resp.Kvs[0].ModRevision)
	}
	go func() {
		for wresp := range cli.Watch(ctx, key) {
			for _, ev := range wresp.Events {
				if ev.Type == clientv3.EventTypeDelete { // 删除事件只跳过，绝不 LoadRules(nil)
					continue
				}
				load(ev.Kv.Value, ev.Kv.ModRevision)
			}
		}
	}()
	return nil
}
```

Nacos 是同一个模式：监听配置变更 → 反序列化成 `[]*flow.Rule` / `[]*circuitbreaker.Rule` → `LoadRules`。三个必须自己兜住的点：**版本单调**（etcd revision 或配置里的 version）、**结构校验**（空 Resource、负阈值会让规则被静默丢弃）、**失败保留旧值**（解析失败、网络失败都不能清空）。etcd 的 watch 断连会自动重连并从上次 revision 续传，但应用重启后的第一次 `Get` 必须成功，否则进程会带着零规则跑——启动时应重试到成功再放量。

### 4.5 与 Gin / gRPC 集成

```go
package middleware

import (
	"net/http"

	sentinel "github.com/alibaba/sentinel-golang/api"
	"github.com/alibaba/sentinel-golang/core/base"
	"github.com/gin-gonic/gin"
)

func Guard() gin.HandlerFunc {
	return func(c *gin.Context) {
		e, b := sentinel.Entry(c.Request.Method+":"+c.FullPath(),
			sentinel.WithTrafficType(base.Inbound))
		if b != nil {
			c.AbortWithStatusJSON(http.StatusTooManyRequests, gin.H{
				"code":   http.StatusTooManyRequests,
				"reason": b.BlockType().String(),
			})
			return
		}
		defer func() {
			var err error
			if last := c.Errors.Last(); last != nil {
				err = last.Err
			}
			e.Exit(base.WithError(err))
		}()
		c.Next()
	}
}
```

```go
package interceptor

import (
	"context"

	sentinel "github.com/alibaba/sentinel-golang/api"
	"github.com/alibaba/sentinel-golang/core/base"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

func UnaryServer() grpc.UnaryServerInterceptor {
	return func(ctx context.Context, req any, info *grpc.UnaryServerInfo, handler grpc.UnaryHandler) (resp any, err error) {
		e, b := sentinel.Entry(info.FullMethod, sentinel.WithTrafficType(base.Inbound))
		if b != nil {
			return nil, status.Error(codes.ResourceExhausted, b.BlockType().String())
		}
		defer func() { e.Exit(base.WithError(err)) }()
		return handler(ctx, req)
	}
}
```

两处共同点：**统一 block 响应**（HTTP 429 + block type，gRPC `ResourceExhausted`），客户端才能区分「限流」和「服务端 500」并决定是否重试；**资源名从框架的路由信息生成**，不要在每个 handler 里手写字符串，否则规则和代码迟早对不上。

### 4.6 可观测：pass / block 打成 Prometheus 指标

v1.0.4 的 stat slot 已经自动上报 `sentinel_go_handled_total{resource,result,block_type}`，**pass / block 次数不用自己写**；`exporter/metric` 提供 registry 和 `HTTPHandler()`，`mux.Handle("/metrics", sentinelmetric.HTTPHandler())` 即可暴露。缺的是 RT 分布，补一个直方图：

```go
package metrics

import (
	"time"

	sentinel "github.com/alibaba/sentinel-golang/api"
	"github.com/alibaba/sentinel-golang/core/base"
	sentinelmetric "github.com/alibaba/sentinel-golang/exporter/metric"
)

var rtHist = sentinelmetric.NewHistogram("rt_seconds", "Resource RT.",
	[]float64{0.005, 0.02, 0.1, 0.3, 1}, []string{"resource"})

func init() { sentinelmetric.MustRegister(rtHist) }

func Do(resource string, fn func() error) error {
	start := time.Now()
	e, b := sentinel.Entry(resource)
	if b != nil {
		return b
	}
	err := fn()
	e.Exit(base.WithError(err))
	rtHist.Observe(time.Since(start).Seconds(), resource)
	return err
}
```

```promql
sum by (resource, block_type) (rate(sentinel_go_handled_total{result="block"}[1m]))

histogram_quantile(0.99, sum by (le, resource) (rate(sentinel_go_rt_seconds_bucket[5m])))
```

`block_type` 是排查的关键维度：`BlockTypeFlowControl` 说明阈值该调了，`BlockTypeCircuitBreaking` 说明下游有问题，`BlockTypeHotSpotParamFlow` 说明是单值热点而不是总量。指标都带 `host/app/pid` 常量标签，多副本下记得用 `sum by (resource)` 聚合。

---

## 5. 版本与生态

| 维度 | sentinel-golang v1.0.4 | Sentinel Java 1.8.x |
|---|---|---|
| 发布节奏 | tag 停在 2022-01，master 仍有零星提交 | 持续发版 |
| 集群流控 | **无**（没有 Token Server 实现） | 有 |
| 控制台 | 无官方 Dashboard | 有 |
| 数据源 | `ext/datasource` 抽象 + 文件源，etcd/Nacos 自己接 | 内置 Nacos/ZK/Apollo/etcd |
| 注解埋点 | 无，手写 `Entry` | `@SentinelResource` |
| 系统自适应 | Load / CPU / 并发 / 入口 QPS / 平均 RT + BBR | 同 |

**规则格式兼容性**要打个问号：两边概念一一对应，但 **JSON 不兼容**。Java 的流控规则是 `grade` + `count` + `controlBehavior`，Go 是 `tokenCalculateStrategy` + `threshold` + `controlBehavior`，枚举还是整数（`Direct=0`、`Reject=0`）。把 Java 控制台导出的规则直接灌给 Go SDK 会解析成零值，阈值变 0、全部拒绝。要么自己写一层字段映射，要么以 Go 的 `flow.Rule` 作为唯一事实源、用配置中心下发，别指望直接复用 Java 控制台。

替代品定位很清楚：`golang.org/x/time/rate` 是纯令牌桶，没有规则、没有统计、没有熔断，适合「这个 client 每秒最多调 50 次」；`uber-go/ratelimit` 是阻塞式漏桶，零分配，适合严格匀速；`sony/gobreaker` 只做熔断，API 面最小、语义最干净；go-zero 把自适应熔断和 load shedding 做进框架，但绑定它的工程结构。Sentinel 的不可替代项是**热点参数 + 规则热更新 + 系统自适应**三件套，以及和 Java 体系一致的资源/规则心智模型。

---

## 6. 常见坑

1. **blocked 之后还 defer `Exit`**：被拦截时 `Entry` 返回的 entry 是 nil，`defer e.Exit()` 直接 panic；反过来忘了 defer，统计里的并发数只增不减（热点/熔断的并发判定会永久失真），RT 也永远缺样本。
2. **资源名硬编码且到处复制**：拼错一个字符规则就静默不生效；把 uid、订单号拼进资源名更糟——资源表有 10000 个的软上限（`DefaultMaxResourceAmount`），超出只打 warning、不阻止创建，内存照涨且规则基本失效。资源名集中成常量，和规则生成用同一份定义。
3. **规则推送不幂等或乱序**：多个实例或多次推送没有版本约束时，旧配置可能后到并覆盖新配置；`LoadRules(nil)` 等于把保护全关。用 etcd revision 或单调 version 丢弃旧事件，删除事件只告警、保留最后一份有效规则。
4. **熔断用 `ErrorCount` 但流量很小**：阈值 20 个错误，10 QPS 要连续错 2 秒才触发，1 QPS 时 `MinRequestAmount` 都凑不满，永远不熔断。小流量用 `SlowRequestRatio`，或把 `MinRequestAmount` 降到个位数。
5. **Warm Up 冷启动比想象的长**：`WarmUpColdFactor=3` 意味着起始只有 1/3 阈值，预热 600s 的规则前 100s 都在低水位；改阈值会重建 controller、预热重新计时。扩容窗口紧的服务别用预热，或把 `WarmUpPeriodSec` 压到 30~60s。
6. **热点参数的内存增长**：每个参数值要维护时间戳、令牌数（并发模式还有并发计数）三份状态，高基数 `ParamKey`（如 session id）会迅速填满 LRU；QPS 模式默认容量是 `min(20000, 4000 × DurationInSec)`，不显式设 `ParamsMaxCapacity` 就可能比预期大一个量级。
7. **单机限流当集群限流**：10 个副本各配 200 QPS，集群上限 2000；HPA 扩到 20 个副本，上限自动变 4000，下游只有 2000 容量时扩容反而加速打垮它。Go 版没有 Token Server，跨实例配额必须走网关或 Redis。

另外两个容易忽略的口径问题：`Entry` 只包住业务函数，JSON 序列化、日志、中间件耗时都在外面，RT 会偏小；系统规则的 `InboundQPS` / `AvgRT` 只统计标了 `Inbound` 的 entry，内部调用不标 `Outbound` 的话，自适应看到的负载比真实值低。

---

## 7. 选型对比

| 维度 | Sentinel-Go | x/time/rate | 网关（Nginx / APISIX） | 服务网格（Envoy） |
|---|---|---|---|---|
| 粒度 | 资源 / 方法 / 参数值 / 依赖 | 单进程 limiter | 路由 / IP / Header | 服务 / 路由 |
| 动态规则 | `LoadRules` + 配置中心 | 改代码或 `SetLimit` | 有（Admin API / CRD） | 有（CRD / xDS） |
| 熔断 | 慢调用 / 异常比例 / 异常数 | 无 | 部分（插件） | 有（outlier detection） |
| 热点参数 | 有 | 需自建 map | 插件 / Lua | 无 |
| 集群配额 | 无 | 无 | 有（Redis / 集群模块） | 有（global rate limit） |
| 部署成本 | 引依赖 + 逐点埋点 | 极低 | 中（多一跳） | 高（sidecar） |

- **单机细粒度 + 规则要能动态改** → Sentinel-Go。尤其是「按 uid/sku 限流」「方法级 RT 熔断」「系统负载兜底」这三类需求，网关和 `rate.Limiter` 都做不到。
- **只要单机 QPS 上限** → `rate.Limiter` 就够，几十行中间件，别为三个词引入规则引擎和一套埋点规范。
- **跨实例的精确配额** → 网关或 Redis + Lua。SDK 里做集群限流只能做近似（哈希分摊/批量领配额），精确性要求高时不要自己造。
- **多语言、跨进程统一治理** → 服务网格，应用层只留兜底降级；但网格看不到参数值维度，热点保护仍要回到 SDK。

一句话：**入口总量交网关，进程内细粒度交 Sentinel，跨实例精确配额交 Redis，谁也别越界**。

参考：<https://github.com/alibaba/sentinel-golang>、<https://sentinelguard.io/zh-cn/docs/introduction.html>、<https://pkg.go.dev/github.com/alibaba/sentinel-golang/api>、<https://golang.org/x/time/rate>
