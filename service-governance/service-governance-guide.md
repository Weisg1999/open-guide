# 服务治理技术指南

> 服务治理的四个动作：限流挡住超额流量，熔断切断故障依赖，降级保住核心链路，重试放大可用性也可能放大故障。
> 参考实现：<https://github.com/alibaba/sentinel-golang> v1.x（最新 tag v1.0.4，master 持续提交）· <https://github.com/sony/gobreaker> v2.1 · <https://pkg.go.dev/golang.org/x/time/rate> v0.14　|　适用 Go 1.21+

---

## 1. 项目简介

服务治理不是某个库，而是一组**面向失败的设计**：当依赖变慢、变错、流量变多时，系统只丢掉该丢的那部分，而不是整体雪崩。它由四个可独立部署、也可组合使用的动作构成：限流回答"我最多能接多少"（保护自己），熔断回答"下游坏了还要不要打"（保护调用链），降级回答"坏了之后给用户什么"（保住核心），重试回答"失败了要不要再来一次"（利用瞬时抖动）。本篇讲**通用算法与独立库**：`golang.org/x/time/rate`（令牌桶）、`sony/gobreaker`（熔断器）、`alibaba/sentinel-golang`（流控 + 熔断 + 系统自适应），以及手写重试与超时封装。它们不绑定框架，能塞进 Gin 中间件、gRPC 拦截器，也能只用来保护一个后台任务里的 HTTP client。

与 [go-zero](../go-zero/go-zero-guide.md) 的分工：go-zero 篇讲**框架内置治理**（自适应熔断、load shedding、链式超时，默认开启、配置驱动）；本篇讲**机制本身**——为什么令牌桶允许突发而漏桶不允许、半开放几个探针、重试预算怎么算。理解机制，才能在默认值不适用时改对参数，也才能在非 go-zero 项目里自己搭一套。先划一条边界：**治理组件只能减少损失，不能增加容量**，限流阈值高于真实容量时，它只是让雪崩来得更整齐，所有阈值的前提都是容量评估与压测。

依赖：`go get golang.org/x/time@v0.14.0`（BSD-3-Clause）、`go get github.com/sony/gobreaker/v2@v2.1.0`（MIT）、`go get github.com/alibaba/sentinel-golang`（Apache-2.0）。

---

## 2. 能力总览

| 能力 | 典型手段 | 代表实现 | 关键参数 |
|---|---|---|---|
| 流量整形 | 令牌桶 / 漏桶 | `x/time/rate`、`uber-go/ratelimit` | rate、burst |
| 请求数限流 | 固定窗口 / 滑动窗口 | 自研、Sentinel flow 规则 | 阈值、统计窗口、桶数 |
| 并发隔离 | 信号量 / 舱壁 | `x/sync/semaphore` | 最大并发、获取超时 |
| 熔断 | 三态 + 滑动窗口 | `gobreaker`、Sentinel | 失败率、最小请求数、open 时长、探针数 |
| 自适应保护 | BBR / CPU / 并发数 | Sentinel system 规则 | 触发阈值、策略 |
| 重试 | 指数退避 + 抖动 + 预算 | 自研、gRPC retry policy | 次数、基数、预算比例 |
| 超时 | deadline 逐跳扣减 | `context.WithTimeout` | 每跳预算、buffer |
| 降级 | 兜底值 / 关非核心 / 异步化 | 业务代码 | 开关、兜底数据源 |
| 集群限流 | 中心化配额 / 一致性哈希 | Redis + Lua（Sentinel Token Server 仅 Java 版有） | 配额批大小、分片数 |
| 可观测 | 指标 / 回调 | Prometheus、`OnStateChange` | 拒绝数、熔断次数、RT 分位 |

把这当"任选清单"、每样装一个是常见错误。正确做法是**按失败模式选**：入口过载用限流，下游出错用熔断，下游变慢用超时 + 慢调用熔断，抖动用重试，容量不足用降级。装两个功能重叠的组件（Sentinel 流控 + 自研固定窗口），阈值会互相打架。

---

## 3. 核心概念与原理

### 3.1 限流四算法：差别在"边界"与"突发"

**固定窗口**最省事：每秒一个计数器，到点清零。

```text
窗口1 [0ms ────── 1000ms] 计数 100/100 满
窗口2 [1000ms ────── 2000ms] 计数 100/100 满
        ↑ 交界处 200ms 内实际通过 200 个 = 2 倍阈值
```

这就是**临界突刺**：阈值 100 QPS 的固定窗口，在窗口交界可短时承受 200 QPS。秒杀开场、整点任务这类"整点脉冲"必然打穿它。固定窗口只适合对突发不敏感的后台任务。

**滑动窗口**把时间切成 N 个桶，统计"最近一个窗口"内所有桶之和，桶越细越平滑，代价是 O(N) 的内存与统计开销。Sentinel 的 `StatIntervalInMs` + 桶数即此实现。它解决了临界突刺，但仍是**计数**语义：窗口内先放进 100 个，第 101 个才拒，被放的 100 个可能在同一毫秒涌入。

**漏桶**把请求排队，以恒定速率流出，突发被完全抹平。`uber-go/ratelimit` 的 `Take()` 就是阻塞式漏桶，零分配、极快，适合"必须匀速调用第三方"。缺点同样明显：**不允许突发**，一个本可一次处理 50 条的批任务会被摊成 50 个间隔，尾延迟变差；队列满时只能阻塞调用方 goroutine 或丢弃，没有中间态。

**令牌桶**是工业界默认解：桶容量 b，以 r/s 补充令牌，取到令牌才放行。空闲 2s 攒满 100 个令牌，就能瞬时放行 100 个（允许突发）；持续打满时长期均值收敛到 r/s（不允许持续超发）。`rate.NewLimiter(50, 100)` 意为"长期 50 QPS，允许 100 个瞬时突发"。**burst 不是冗余参数**：burst=1 时退化为严格匀速（接近漏桶）；burst 远大于 rate 时，长时间空闲的服务会在流量回来的第一秒被自己的令牌淹掉。经验值 `burst ≈ QPS × 平均 RT`（Little's Law 的在途请求量），而不是拍一个 1000。

| 算法 | 允许突发 | 平滑度 | 内存 | 典型实现 |
|---|---|---|---|---|
| 固定窗口 | 是（临界 2×） | 差 | O(1) | 自研计数器 |
| 滑动窗口 | 是（窗口内） | 中 | O(桶数) | Sentinel flow |
| 漏桶 | 否 | 最好 | O(队列) | uber-go/ratelimit |
| 令牌桶 | 是（≤burst） | 好 | O(1) | x/time/rate |

### 3.2 单机限流与集群限流的本质区别

单机限流只回答"这个进程还能不能接"，它**不知道其他副本的存在**。10 个副本各配 100 QPS，集群上限是 1000；扩到 20 个副本，上限自动变 2000——若下游只有 1000 的容量，扩容反而放大了打垮下游的速度。这是"单机阈值 = 集群阈值 / 副本数"最致命的缺陷：**副本数是弹性的，除法是写死的**。

集群限流必须引入全局视角：**要"全局精确"就接受 RTT，要"低延迟"就接受近似**，两条路线——中心化配额与哈希分摊。

- **中心化配额**：所有实例向中心（Redis + Lua、Sentinel Token Server）申请令牌。语义精确，但每次多一次 RTT，中心自身是单点与瓶颈。生产做法是**批量领取**：一次领 50 个配额在本地慢慢花，RTT 摊薄到 1/50，代价是实例间利用率不均（有人领了不用，有人在等）。
- **一致性哈希分摊**：按 key（租户/用户/接口）哈希到固定实例，由该实例做本地判定并承担该 key 的全部配额。无额外 RTT，但节点增减会引起配额重分配抖动，热点 key 仍会压垮单实例。

多数业务要的不是精确 QPS，而是"总量别超太多"，批量配额 + 本地令牌桶（领来的配额灌进本地桶）是性价比最高的组合。

### 3.3 熔断器三态与滑动窗口统计

```text
        失败率达阈值                Timeout 到期
 closed ──────────────► open ──────────────────► half-open
   ▲                      │ 直接快速失败              │ 放行 MaxRequests 个探针
   └──────────────────────┴──────────────────────────┘
        探针全部成功                    任一探针失败 → 回 open
```

- **closed**：正常放行并统计。窗口内"失败率 > 阈值"且"请求数 ≥ 最小请求数"才触发，后者避免"3 个请求错 2 个 = 66%"的小样本误判。
- **open**：不再发起真实调用，直接返回错误。价值是**把恢复时间还给下游**——继续打只会让它起不来。open 时长应大于下游典型恢复时间（重启 + 预热），否则会陷入"打开→半开→失败→打开"的抖动。
- **half-open**：放少量探针验证。探针数是核心取舍：只放 1 个，恢复判定要串行等 `Timeout × 探针数`，慢；一次放全量，刚恢复的下游会被瞬间打回原形。经验值 `max(1, 正常并发的 1%~5%)`，且探针要带**独立短超时**，失败立刻回 open。
- **统计维度**：失败率（错）、**慢调用比例**（慢）、连续失败数。最该先上的是慢调用比例——雪崩常从 P99 由 200ms 涨到 3s 开始，而那时错误率还是 0。Sentinel 的 `SlowRequestRatio` + `MaxAllowedRtMs` 正为此设计。
- **粒度**：必须"一个依赖实例 / 一个下游接口一个熔断器"。全局一个，会导致订单服务挂了把用户服务一起切断。`IsSuccessful` 还要排除业务错误：400/404 是"调用成功但业务失败"，计入失败会让熔断器在正常业务波动时误开。

### 3.4 自适应限流：阈值从哪里来

固定阈值的前提是"容量已知且稳定"。现实中容量随数据量、缓存命中率、下游 RT 漂移，压测出的 2000 QPS 两周后可能只剩 1200。自适应从运行时指标反推安全水位，主流三种：

- **BBR 类**（Sentinel `BBR` 策略）：基于 Little's Law 与采样的最大吞吐/最小 RT 估算拥塞窗口，本质是让在途请求贴着瓶颈带宽。无需压测、自动跟随容量；但冷启动期无样本，对"下游突然变慢"的反应依赖采样窗口。
- **CPU / Load**：`load1 > 核数` 就开始拒。实现最简单，但对 IO 密集型失真严重（等 DB 的 goroutine 不占 CPU，CPU 很闲却已过载），容器里读到的还是宿主机指标。
- **并发数（in-flight）**：直接限制同时处理的请求数，语义最贴近资源占用，适合每请求内存/连接开销大的服务；但接口耗时差异大，一个慢接口会占满配额。
- **分层取舍**：自适应不取代固定阈值。**入口用自适应兜底（保护自己不被压垮），出口用固定配额（保证公平与可预测）**，两层同时生效、取更严格者：自适应负责"活着"，固定阈值负责"按合同办事"。上线先 dry-run 只记录不拦截，观察一周触发率再真开。

### 3.5 重试：放大系数、预算与幂等

重试最容易被滥用，数学也很残酷：单次成功率 p，重试 n 次后的总请求量是 `1 + p + p² + ...`。失败率 50% 时，3 次尝试意味着每个用户请求平均打出 1.75 个下游请求；90% 时是 2.71 个。上游越糟重试越凶——这正是重试风暴的形成机制。

三条硬约束：

1. **只重试幂等且可重试的错误**。GET/PUT/DELETE 天然幂等，POST 需要幂等键；连接被拒、`503`、`429`（带 `Retry-After`）可重试；`400`/`401`/`404` 重试一万次也是错；`context.Canceled` 绝对不重试。
2. **重试预算**（retry budget）：Google SRE 的做法是"重试请求数不超过总请求数的 10%"，用令牌桶控制。预算耗尽时即使失败也直接返回——这是唯一能在故障期真正封住放大系数的机制。只配最大次数、不配预算，等于给雪崩装加速器。
3. **指数退避 + 抖动**：`base × 2^i` 再加**全长抖动** `rand(0, d)`，否则所有客户端会在同一毫秒一起重试，形成周期性脉冲。退避总时长必须落在上游剩余 deadline 内，否则重试只制造必然超时的请求。
4. **只在一层做**：网关 3 次 × 服务 A 3 次 × 服务 B 3 次 = 27 倍流量。选定一个责任层（通常是直接调用方或 sidecar），其余层禁止重试。

### 3.6 隔离、超时预算与降级形态

**隔离**最轻的实现是信号量（`semaphore.Weighted`）：限制"同时有多少请求在打某个依赖"，而非 QPS。它比限流更贴近资源——慢请求会一直占着配额，天然把"下游变慢"转换成"对下游的并发压力受限"。舱壁（bulkhead）则是按依赖/租户切分配额：推荐服务 20 并发、支付服务 50 并发，推荐拖死时支付不受影响。注意信号量是**同进程内**的，防的是"自己的 goroutine 被拖光"，不是"下游被打垮"。

**超时预算**要逐跳扣减，而非每跳重置：入口 800ms → 网关 700ms → 服务 A 600ms → 服务 B 500ms，每跳预留 100ms 给序列化/网络/重试，任一跳超时立即向上返回。若每层都 `context.WithTimeout(ctx, 1s)`，上游已放弃、下游还在跑，最坏总耗时是层数 × 1s，且这些请求持续占用下游资源。正确写法是基于父 ctx 派生（`WithTimeout(ctx, ...)` 天然带剩余时间），前提是不要用 `context.Background()` 重置。降级则有三种形态：

| 形态 | 做法 | 适用 | 风险 |
|---|---|---|---|
| 返回兜底 | 缓存快照、默认值、空列表 | 读多写少、可容忍陈旧 | 兜底数据过期 |
| 关闭非核心 | 关推荐位、关评论、关画像 | 核心链路资源紧张 | 需要开关与灰度 |
| 异步化 | 写消息队列，稍后补偿 | 写操作可延迟 | 一致性延迟、需幂等 |

铁律：**降级不能返回 nil**。`return nil, nil` 会让上游把"降级"当成"查询成功且结果为空"，把空值写进缓存或数据库；`nil` 指针还会在序列化或字段访问时 panic——降级逻辑本身成了故障源。正确做法是返回**零值结构体 + 明确的降级标记**（`degraded: true`、`source: "fallback"`），让调用方能区分"真的没有"和"暂时拿不到"。

---

## 4. 使用实战

### 4.1 x/time/rate 做 HTTP 入口限流

```go
package middleware

import (
	"net/http"
	"sync"

	"golang.org/x/time/rate"
)

// Limit 按来源地址分桶：长期 r QPS，允许 burst 个瞬时突发。
func Limit(r rate.Limit, burst int) func(http.Handler) http.Handler {
	var m sync.Map // key -> *rate.Limiter；生产必须加 TTL 淘汰，否则一次性 key 撑爆内存
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
			v, ok := m.Load(req.RemoteAddr)
			if !ok {
				v, _ = m.LoadOrStore(req.RemoteAddr, rate.NewLimiter(r, burst))
			}
			if !v.(*rate.Limiter).Allow() {
				http.Error(w, "too many requests", http.StatusTooManyRequests)
				return
			}
			next.ServeHTTP(w, req)
		})
	}
}
```

入口用 `Allow()`（非阻塞、立即拒）而非 `Wait()`。`Wait()` 会把请求挂在服务端排队，超出 deadline 才报错，等于把"拒绝"变成"慢拒绝"，白占连接与 goroutine。只有**出站**配额（调用第三方）才适合 `Wait()`。

### 4.2 gobreaker 包装下游 client

```go
package downstream

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"time"

	"github.com/sony/gobreaker/v2"
)

var (
	errClientError = errors.New("4xx from user-service") // 业务错误，不计入熔断
	httpCli        = &http.Client{Timeout: 2 * time.Second}
	// userCB：一个下游接口一个熔断器，下面的参数只是起点，不是标准答案
	userCB = gobreaker.NewCircuitBreaker[[]byte](gobreaker.Settings{
		Name:        "user-service",
		MaxRequests: 5,                // half-open 探针数
		Interval:    30 * time.Second, // 统计窗口
		Timeout:     10 * time.Second, // open 时长
		ReadyToTrip: func(c gobreaker.Counts) bool {
			if c.Requests < 20 { // 最小请求数，避免小样本误判
				return false
			}
			return float64(c.TotalFailures)/float64(c.Requests) > 0.5
		},
		IsSuccessful: func(err error) bool { // 4xx 是业务错误，不算下游故障
			return err == nil || errors.Is(err, errClientError)
		},
	})
)

func GetUser(ctx context.Context, id string) ([]byte, error) {
	body, err := userCB.Execute(func() ([]byte, error) {
		req, err := http.NewRequestWithContext(ctx, http.MethodGet,
			"http://user-service/users/"+id, nil)
		if err != nil {
			return nil, err
		}
		resp, err := httpCli.Do(req)
		if err != nil {
			return nil, err
		}
		defer resp.Body.Close()
		if resp.StatusCode >= 500 {
			return nil, fmt.Errorf("upstream status %d", resp.StatusCode)
		}
		if resp.StatusCode >= 400 {
			return nil, errClientError
		}
		return io.ReadAll(resp.Body)
	})
	if errors.Is(err, gobreaker.ErrOpenState) {
		err = fmt.Errorf("user-service unavailable: %w", err) // 熔断拒绝要能被上层识别
	}
	return body, err
}
```

`ErrOpenState` 必须翻译成业务错误，否则上层把"熔断拒绝"和"下游 500"混在一起，降级分支写不对。这里 `http.Client.Timeout` 与 ctx 的 deadline 是"取更小者"，两者都要有。

### 4.3 sentinel-golang 规则加载

```go
package bootstrap

import (
	sentinel "github.com/alibaba/sentinel-golang/api"
	"github.com/alibaba/sentinel-golang/core/circuitbreaker"
	"github.com/alibaba/sentinel-golang/core/flow"
	"github.com/alibaba/sentinel-golang/core/system"
)

func InitSentinel() error {
	if err := sentinel.InitDefault(); err != nil {
		return err
	}
	if _, err := flow.LoadRules([]*flow.Rule{{
		Resource:               "GET:/api/orders",
		TokenCalculateStrategy: flow.Direct,
		ControlBehavior:        flow.Reject,
		Threshold:              200, // 单机 200 QPS
		StatIntervalInMs:       1000,
	}}); err != nil {
		return err
	}
	if _, err := circuitbreaker.LoadRules([]*circuitbreaker.Rule{{
		Resource:         "GET:/api/orders",
		Strategy:         circuitbreaker.SlowRequestRatio,
		RetryTimeoutMs:   5000, // open 5s
		MinRequestAmount: 20,
		StatIntervalMs:   10000,
		MaxAllowedRtMs:   300, // 慢调用阈值 300ms
		Threshold:        0.5, // 慢调用占比 > 50% 则熔断
	}}); err != nil {
		return err
	}
	_, err := system.LoadRules([]*system.Rule{{MetricType: system.Load, TriggerCount: 8, Strategy: system.BBR}})
	return err
}
```

埋点只需两行：`e, blockErr := sentinel.Entry("GET:/api/orders")`，`blockErr != nil` 即被限流或熔断，正常路径 `defer e.Exit()`。三个细节：规则**按资源名**绑定，`Entry` 的字符串必须与规则完全一致；`RetryTimeoutMs` 是 open 时长，要大于下游重启时间；规则可用 `ext/datasource` 从 Nacos/etcd/YAML 热加载，但拉取失败时**保留旧规则**，`LoadRules(nil)` 会让保护瞬间消失。

### 4.4 重试 + 熔断 + 超时的组合封装

```go
package resilience

import (
	"context"
	"errors"
	"fmt"
	"math"
	"math/rand"
	"time"

	"github.com/sony/gobreaker/v2"
)

type Policy struct {
	Timeout     time.Duration // 单次尝试超时，必须小于上游剩余预算
	MaxAttempts int           // 总尝试次数（含首次）
	BaseBackoff time.Duration // 退避基数
}

// Retryable 只放行瞬时性故障：父 ctx 已取消、熔断打开都不重试。
// 单次尝试超时（callCtx 到期）但父 ctx 仍存活时属于瞬时故障，必须允许退避重试，
// 否则 Timeout 一旦生效就永远不重试。
func Retryable(parent context.Context, err error) bool {
	return err != nil &&
		parent.Err() == nil &&
		!errors.Is(err, gobreaker.ErrOpenState) &&
		!errors.Is(err, gobreaker.ErrTooManyRequests)
}

// Call 在父 ctx 的剩余预算内执行"熔断 + 超时 + 指数退避重试"。
func Call[T any](ctx context.Context, cb *gobreaker.CircuitBreaker[T],
	p Policy, fn func(context.Context) (T, error)) (T, error) {
	var zero T
	var lastErr error
	for i := 0; i < max(p.MaxAttempts, 1); i++ {
		if i > 0 {
			// 全长抖动 rand(0, base*2^(i-1))，打散同步重试
			d := time.Duration(float64(p.BaseBackoff) * math.Pow(2, float64(i-1)))
			d = time.Duration(rand.Int63n(int64(d) + 1))
			select {
			case <-ctx.Done():
				return zero, ctx.Err()
			case <-time.After(d):
			}
		}
		// 从父 ctx 派生：剩余预算自动继承，不会每跳重置成满额
		callCtx, cancel := context.WithTimeout(ctx, p.Timeout)
		v, err := cb.Execute(func() (T, error) { return fn(callCtx) })
		cancel() // 必须立即释放定时器，否则泄漏到整条调用链结束
		if err == nil {
			return v, nil
		}
		lastErr = err
		if !Retryable(ctx, err) {
			return zero, err
		}
	}
	return zero, fmt.Errorf("give up after %d attempts: %w", max(p.MaxAttempts, 1), lastErr)
}
```

三个易漏的正确性点：`cancel()` 必须在每次尝试后立即调用；退避在**熔断器之外**，被熔断拒绝的请求不进退避循环；`WithTimeout` 基于父 ctx，父预算耗尽后后续尝试立刻返回 `DeadlineExceeded`，而不是继续睡。

---

## 5. 版本演进

| 组件 | 当前状态（截至 2026-11） | 关键变化 |
|---|---|---|
| `x/time/rate` | v0.16.0，BSD-3-Clause | 长期稳定，`Limiter` API 多年未变；`Wait/Allow/Reserve` + `SetLimit/SetBurst` 热调整 |
| `sony/gobreaker` | **v2.4.0**，MIT | v2 引入泛型 `CircuitBreaker[T]`；新增 `BucketPeriod` 把固定窗口升级为滑动窗口、`IsExcluded` 让取消类错误不计入统计 |
| `alibaba/sentinel-golang` | v1.x，Apache-2.0 | **release tag 停在 v1.0.4（2022-01）**，master 持续提交（2026-07 仍有 commit）；v1.0.4 的 `go.mod` 为 go 1.13，master 已升到 1.18 |
| `uber-go/ratelimit` | v0.3.1，MIT | 漏桶，`Take()` 阻塞式、零分配；无 ctx 支持，取消要靠外层 select |

两条实操建议：gobreaker v1→v2 是**编译期破坏性变更**（`NewCircuitBreaker` 变泛型函数、路径带 `/v2`），运行时语义兼容，成本主要在 `errors.As` 处理 `ErrOpenState` 的地方；Sentinel 因 tag 停滞，选它就要接受"跟 master 走"的维护成本，或 fork 后自建 tag——对审计严格的公司是真实决策点。

---

## 6. 常见坑与最佳实践

1. **只加限流不加超时**：限流控制"进来多少"，超时控制"占多久"。没有超时，100 个慢请求就能占满连接池与 goroutine；且超时必须逐跳扣减，每层 `WithTimeout(context.Background(), 1s)` 会让故障总耗时等于层数 × 1s。
2. **熔断阈值拍脑袋**：`失败率 > 50%` 配 `Requests >= 1`，等于第一个请求失败就熔断，服务会在正常抖动中反复开合。最小请求数应覆盖一个窗口的约 1/10 流量，open 时长要大于下游典型恢复时间。
3. **重试打爆下游**：无预算重试会把 1 倍流量放大到 3~10 倍。必须有预算（建议 ≤10%）、必须有抖动、只重试幂等操作、且只在一层做。
4. **单机限流当集群限流**：`NewLimiter(1000, 1000)` 在 10 个副本上就是 10000 QPS。要么除以副本数并接受弹性失效，要么上中心化配额/一致性哈希。
5. **降级逻辑自身抛错**：兜底走缓存，缓存也挂；兜底调备用接口，备用也超时。降级代码必须有**二级兜底**（常量/空结构体）且自身包 recover，不能比主逻辑更脆。
6. **降级返回 nil**：`return nil, nil` 让上游把降级当成功，空值写进 DB/缓存；`nil` 指针序列化时直接 panic。永远返回零值结构 + `degraded` 标记。
7. **监控缺失导致熔断黑盒**：只在 `OnStateChange` 打日志、没打指标，事后无法回答"熔断多久、拒了多少、恢复几次"。至少要有 `circuit_state`、`rejected_total`、`upstream_rt`。
8. **burst 配得离谱、`Wait()` 用错位置**：`NewLimiter(10, 10000)` 空闲后能瞬间放出 10000 个，等于没限流；服务端 `Wait()` 又会把请求挂在内存排队，超时才返回 503，期间连接、goroutine、内存全被占。burst 约等于在途并发，入口用 `Allow()`。
9. **熔断器粒度过粗**：一个进程一个熔断器，支付挂了把商品详情也切断；把 400/404 当失败还会在业务高峰误开。按"依赖 + 接口"建熔断器，用 `IsSuccessful` / `IsExcluded` 精确分类。
10. **信号量忘了释放**：`Acquire` 后 `defer Release(1)` 写在错误分支之外，panic 或提前 return 就漏放，几次后配额永久泄漏、服务假死。
11. **规则热加载清空**：拉取失败时 `LoadRules(nil)` 会清空全部规则，保护瞬间消失。失败必须保留旧规则（fail-safe，而非 fail-open）。

---

## 7. 选型对比

| 维度 | x/time/rate | uber-go/ratelimit | gobreaker v2 | sentinel-golang | hystrix-go |
|---|---|---|---|---|---|
| 定位 | 令牌桶限流 | 漏桶整形 | 纯熔断器 | 流控 + 熔断 + 自适应 | 熔断 + 隔离（已停更） |
| 算法 | 令牌桶（允许突发） | 漏桶（匀速） | 三态 + 滑动窗口 | 滑动窗口 + BBR | 三态 + 计数窗口 |
| 集群限流 | 否 | 否 | 否 | 需 Token Server | 否 |
| 自适应 | 否 | 否 | 否 | **是**（Load/CPU/并发） | 否 |
| 依赖 | 零 | 零 | 零 | 8 个直接依赖 | 较多 |
| 性能 | 高（~85ns/op） | 最高（~68ns/op） | 高 | 中（规则与统计开销） | 中 |
| 可观测 | 无（自建指标） | 无 | `OnStateChange` 回调 | 内置 metric exporter | dashboard（已过时） |
| 维护 | 活跃 | 低频但稳定 | 活跃 | tag 停滞、master 活跃 | **归档/停更** |
| 许可 | BSD-3-Clause | MIT | MIT | Apache-2.0 | MIT |

- **只要入口限流** → `x/time/rate`，几十行中间件搞定，别为"完整治理"引入规则引擎；
- **要保护第三方配额、要求严格匀速** → `uber-go/ratelimit`，`Take()` 一行、零分配；
- **只要熔断** → `gobreaker/v2`，API 面最小、语义最清晰，泛型免类型断言；
- **要流控 + 熔断 + 自适应一站式，且能接受跟 master** → `sentinel-golang`，尤其适合已有 Sentinel 控制台/Java 体系的团队；
- **要跨语言、跨进程统一治理** → 别在应用层做，交给服务网格（Envoy `local_ratelimit` / `circuit_breakers`）或 API 网关，应用层只留兜底降级；
- **hystrix-go** → 不要再新选，已停更多年，熔断用 gobreaker 平替。

一句话：**单机限流用 `x/time/rate`，熔断用 `gobreaker`，需要自适应与规则中心才上 Sentinel；三者可以叠加，但每加一层都要问"它防的失败模式，上一层是不是已经防住了"**。

参考：<https://pkg.go.dev/golang.org/x/time/rate>、<https://github.com/sony/gobreaker>、<https://github.com/alibaba/sentinel-golang>、<https://github.com/uber-go/ratelimit>、<https://sre.google/sre-book/handling-overload/>（重试预算）。
