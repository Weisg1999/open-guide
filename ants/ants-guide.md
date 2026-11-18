# ants 技术指南

> 池化 goroutine 不是为了「省创建成本」，而是为了给出上限：没有上限的 `go func()` 是内存与下游连接的隐形泄漏。
> 上游：<https://github.com/panjf2000/ants> **v2.12.1**（2026-05）　|　许可：MIT

---

## 1. 项目简介

ants 是 panjf2000 维护的 goroutine 池库，MIT 许可。v2 线当前版本为 **v2.12.1**（2026-05-25 发布），`go.mod` 声明最低 **Go 1.19**。它只做一件事：把「同时运行的 goroutine 数量」钉在一个可配置的上限内，并复用这批 goroutine 执行源源不断的短任务。

与《Go 并发与 Context 技术指南》的分工：那一篇讲 GMP、channel、context、errgroup、singleflight，回答「怎么让并发正确」；本篇只回答「怎么给并发装一个闸门」。调度器负责把 G 跑满，ants 负责不让 G 无限变多，两者不冲突。

它带来三个真实收益：**上限**（保护内存、fd、下游连接池）、**复用**（worker 的栈与 `g` 结构体不反复分配回收，高频短任务下分配压力明显下降）、**可观测**（`Running()` / `Free()` / `Waiting()` 是现成的并发度指标，裸 `go func()` 没有）。

不适用：需要任务结果或错误回传（`Submit` 没有返回值）、需要严格顺序、需要持久化或失败重试（那是 asynq / Kafka 的领域）、纯 CPU 密集计算（上限设成 `GOMAXPROCS` 即可，用 `semaphore.Weighted` 更简单）。

---

## 2. 能力总览

| 能力 | API | 说明与注意 |
|---|---|---|
| 固定容量池 | `ants.NewPool(size, opts...)` | `size` 是**最大并发 worker 数**；`size <= 0` 会变成 `-1`，即无上限 |
| 提交任务 | `pool.Submit(func())` | 异步语义：返回 `nil` 只代表任务被接收，不代表执行成功 |
| 带参任务池 | `ants.NewPoolWithFunc(size, f)` + `Invoke(arg)` | 池内固定一个函数，每次只换参数；适合「同一种作业」 |
| 泛型带参池 | `ants.NewPoolWithFuncGeneric[T]` | v2.11.0 引入，避免 `any` 断言 |
| 多池分片 | `ants.NewMultiPool` / `MultiPoolWithFunc` | v2.9.0 引入，按 RoundRobin 或 LeastTasks 选子池，降低单池锁竞争 |
| 运行时调容量 | `pool.Tune(n)` | 对无限池与 `PreAlloc` 池无效；调小不会杀掉正在跑的任务 |
| 指标 | `Running()` / `Free()` / `Waiting()` / `Cap()` | `Free()` 在无限池返回 `-1`；`Waiting()` 数的是被阻塞的**提交方** |
| 关闭 | `Release()` / `ReleaseTimeout(d)` / `ReleaseContext(ctx)` | `Release` 立即返回；后两者等 worker 退出，超时分别返回 `ErrTimeout` / `ctx.Err()` |
| 重启 | `pool.Reboot()` | 已释放的池可复用；官方要求先用 `ReleaseTimeout` 等干净，否则有 data race |
| 预分配 | `ants.WithPreAlloc(true)` | 一次性分配环形队列，超大容量 + 长任务时降低分配开销 |
| 过期回收 | `ants.WithExpiryDuration(d)` / `WithDisablePurge(true)` | 默认 1s 扫描一次，回收空闲超过 `d` 的 worker |
| 拒绝策略 | `WithNonblocking(true)` / `WithMaxBlockingTasks(n)` | 前者立即返回 `ErrPoolOverload`，后者给阻塞的提交方设上限 |

---

## 3. 核心概念与原理

### 3.1 为什么需要池：goroutine 便宜，但驻留不免费

初始栈 2 KB、`g` 结构体约 300 B，创建一个 goroutine 只要几百纳秒。但成本有三块不在「创建」上：

- **内存与 GC**：每个阻塞中的 G 都保留栈和 `sudog`。十万个 G 就是数百 MB 常驻内存，且 goroutine 栈属于 GC root set，标记阶段要逐个扫描；栈收缩是延迟的，不会因为空闲立刻归还。
- **fd 与连接**：一个 G 通常对应一个下游请求，占一个 fd 或一条连接。`ulimit -n` 是 1024 还是 65535，决定了你能并发多少；连接池只有 50 条时，5000 个 G 里有 4950 个在驱动内部排队。
- **下游承载能力**：DB、Redis、第三方 API 都有硬上限。无上限的扇出不报错，它只是把压力推给下游，然后以超时、重试、雪崩的形式还回来。

所以池化的核心不是性能优化，而是**把「并发度」变成一个显式配置**：它必须是你在容量规划里写过、在监控里看得见、在告警里配有阈值的数字。

### 3.2 Pool 的调度模型

```text
Submit(fn)
   │
   ├─ 空闲栈非空？ ──是──► 取出最近归还的 worker（LIFO），把 fn 交给它的 task channel
   │
   ├─ running < cap？ ──是──► 新建一个 worker goroutine 并交付
   │
   └─ 否则 ──► Nonblocking 或 Waiting() >= MaxBlockingTasks ？── 是 ──► ErrPoolOverload
                     │
                     └─ 否 ──► 调用方在 cond.Wait() 上阻塞，Waiting +1
```

worker 的主循环只有三行语义：`for fn := range task { fn(); revertWorker() }`。执行完一个任务后，worker 把自己放回**空闲栈**并更新 `lastUsed`，等待下一个任务——这就是「复用」的全部。

关键细节：

- **空闲 worker 用 LIFO 栈，不用队列**。刚归还的 worker 最先被复用，热栈缓存友好；长期没被用到的 worker 沉在栈底，正好被回收。
- **回收由独立的 purge goroutine 做**。每隔 `expiryDuration` 扫一遍空闲栈，把空闲超过该时长的 worker 摘下来、发一个 `nil` 让它退出循环。默认 `expiryDuration = 1s`，非常激进——低频业务里 worker 基本不会被复用，池的意义主要是限流而非复用。
- **时间戳是粗粒度的**。另有 ticktock goroutine 每 500ms 更新一次池内「现在」，worker 归还时读缓存值，避免每次 `time.Now()`。
- **`Release` 不等任务**。CAS 把状态置为 `CLOSED`，停掉 purge/ticktock，清空空闲栈，`Broadcast` 唤醒所有阻塞的提交方（它们会拿到 `ErrPoolClosed`），然后立即返回；正在跑的任务继续跑完。`ReleaseTimeout` / `ReleaseContext` 才会等 `Running()` 归零，超时分别返回 `ErrTimeout` / `ctx.Err()`。
- **`Reboot` 复用的是同一个对象**。所以必须先 `ReleaseTimeout` 确认所有 worker 已退出，否则 purge/ticktock 的收尾会和重启后的初始化撞上。

### 3.3 容量语义：cap 是并发上限，不是队列长度

`NewPool(32)` 的含义是「同时最多 32 个 worker 在跑」，**不是**「队列长度 32」。ants 没有池级别的任务队列：任务要么被塞进某个 worker 的私有 `task` channel（容量 1，单核时是 0），要么提交方直接阻塞。因此：

- 积压不发生在池里，而发生在**调用方**。每个被阻塞的 `Submit` 都占着一个调用方 goroutine、一段请求上下文、可能还有一条上游连接；
- 把 `cap` 从 100 调到 10000，不会「容纳更多排队」，只会让更多任务同时打到下游；
- `Waiting()` 是阻塞在 `Submit` 上的调用方数量，不是排队任务数；`Free() = Cap() - Running()`，无限池返回 `-1`；
- `size <= 0` 会被归一成 `-1`，即无限池。包级函数 `ants.Submit(fn)` 用的正是容量 `math.MaxInt32` 的默认池——**等于没有上限**，不要在生产代码里用它。

需要真正的队列（持久化、削峰、重试）时，正确答案是 asynq / Kafka / Redis Stream，而不是把池调大。

### 3.4 阻塞与非阻塞：两种完全不同的失败模式

默认模式下池满即阻塞，形成**背压**：上游自然变慢，不会丢任务。代价是调用方被挂起，且这个挂起没有超时——`Submit` 不接受 `context.Context`，你无法用 ctx 取消一次提交。

`WithNonblocking(true)` 改成快速失败：没有空闲 worker 时立刻返回 `ErrPoolOverload`（错误文案是 "too many goroutines blocked on submit or Nonblocking is set"）。这时**调用方必须处理这个 error**：返回 503 + `Retry-After`、降级到缓存、或丢弃并计数。忽略它等于静默丢任务，而且 `Submit` 的返回值很容易被 `_ =` 掉。

`WithMaxBlockingTasks(n)` 是中间态：允许阻塞，但阻塞者超过 n 个后也开始返回 `ErrPoolOverload`（`n = 0` 表示不限，且该选项在 `Nonblocking` 下失效）。

为什么必须给阻塞设上限？因为被阻塞的提交方往往持有昂贵资源：HTTP 连接、请求体的内存缓冲、数据库事务、分布式锁。池一满，这些资源跟着堆积；上游等不到响应开始重试，重试又产生新的提交，最终是「池没满、但服务已经死了」的雪崩。生产上更常见的选择是**非阻塞 + 明确降级**，或者**有界阻塞 + 端到端超时**。

### 3.5 Invoke 的真相：它不在 Pool 上，也没有 ctx

这是文档与博客里最常见的误记。v2 的 API 事实是：

- `Pool` 只有 `Submit(task func()) error`，没有 `Invoke`；
- `Invoke(arg any) error` 属于 `PoolWithFunc`；`PoolWithFuncGeneric[T].Invoke(arg T)` 属于泛型版本；`MultiPoolWithFunc.Invoke(args any)` 会先按策略选子池；
- **`Invoke` 的参数是「数据」，不是「函数」**，签名里既没有 `ctx` 也没有 `timeout`。

所以 `pool.Invoke(ctx, fn)` 这种写法在 v2 里不存在。要传 ctx，就用 `Submit` 包一个闭包把 ctx 捕获进去；要超时，就在任务内部 `select ctx.Done()`，或者由调用方自己计时并在超时后放弃等待。

同步语义同样容易被误解：`Submit` / `Invoke` 都是**投递即返回**，返回 `nil` 不表示任务执行成功，更不表示已经执行完。要结果就自己准备一个带缓冲的 channel 或 `WaitGroup`；要错误就让任务把 error 写回 channel——ants 不替你做这件事。

### 3.6 panic：被 recover 的是 worker，不是任务

worker 的 goroutine 最外层有一个 `defer`，里面 `recover()`：任务 panic 不会带崩进程，默认会打印 panic 值与完整栈。但要看清它的作用域——recover 在 `for fn := range task` 的**外层**，所以一次 panic 会终止这个 worker 的循环：

1. `Running()` 减一，worker 对象被放回 `sync.Pool`；
2. 该任务剩余逻辑全部丢失，`Submit` 早已返回 `nil`，调用方永远等不到结果；
3. 后续任务会按需创建新 worker，容量不会永久少一块，但会有一段时间的抖动。

`WithPanicHandler(fn)` 拿到的是 panic 值（`any`），可以用来打点、计数、上报。注意源码是 `if ph != nil { ph(p) } else { log(...) }`——**一旦设置了 handler，默认的栈日志就不再打印**，你得自己在 handler 里补上 `debug.Stack()`。

handler 也无法把错误回传给提交方。想让「任务失败」可被感知，正确做法是任务内部自己 `defer recover()` 并把 error 写进结果 channel，把 panic 转成普通错误，而不是依赖池的兜底。

---

## 4. 使用实战

### 4.1 观察容量：cap、running、waiting 的关系

```go
package main

import (
	"fmt"
	"sync"
	"time"

	"github.com/panjf2000/ants/v2"
)

func main() {
	p, err := ants.NewPool(3, ants.WithExpiryDuration(2*time.Second))
	if err != nil {
		panic(err)
	}
	defer p.Release()

	var wg sync.WaitGroup
	start := time.Now()
	for i := 0; i < 9; i++ {
		wg.Add(1)
		go func() { // 每个提交者是一个独立的调用方 goroutine
			defer wg.Done()
			if err := p.Submit(func() {
				time.Sleep(100 * time.Millisecond)
			}); err != nil {
				fmt.Println("submit:", err)
			}
		}()
	}

	time.Sleep(50 * time.Millisecond)
	fmt.Printf("cap=%d running=%d free=%d waiting=%d\n",
		p.Cap(), p.Running(), p.Free(), p.Waiting())

	wg.Wait()
	// 9 个任务 / 3 个 worker / 每个 100ms，约 300ms 跑完；waiting 最多时是 6
	fmt.Println("elapsed:", time.Since(start).Round(10*time.Millisecond))
}
```

这段代码要读出两件事：`running` 不会超过 `cap`，而超出的部分既不在队列里、也没被拒绝——它们变成了 6 个阻塞的调用方，体现在 `waiting` 上。这就是「池不是队列」的直接证据。

### 4.2 HTTP 服务：限制下游并发 + 指标 + 优雅关闭

```go
package main

import (
	"context"
	"errors"
	"fmt"
	"log"
	"net/http"
	"os/signal"
	"syscall"
	"time"

	"github.com/panjf2000/ants/v2"
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promauto"
	"github.com/prometheus/client_golang/prometheus/promhttp"
)

type result struct {
	body string
	err  error
}

func callDownstream(ctx context.Context, id string) (string, error) {
	select {
	case <-time.After(200 * time.Millisecond):
		return "ok:" + id, nil
	case <-ctx.Done():
		return "", ctx.Err()
	}
}

func main() {
	submitted := promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "downstream_submit_total",
		Help: "Submit 结果计数，按 accepted/rejected/panic 区分",
	}, []string{"result"})

	// cap=32：同时最多 32 个下游调用；非阻塞：满了立刻拒绝，绝不堆积
	pool, err := ants.NewPool(32,
		ants.WithNonblocking(true),
		ants.WithExpiryDuration(5*time.Second),
		ants.WithPanicHandler(func(v any) {
			submitted.WithLabelValues("panic").Inc()
			log.Printf("task panic: %v", v) // 设了 handler 就不会再打默认栈，需要自己补
		}),
	)
	if err != nil {
		log.Fatal(err)
	}

	promauto.NewGaugeFunc(prometheus.GaugeOpts{
		Name: "downstream_pool_running", Help: "正在执行的 worker 数",
	}, func() float64 { return float64(pool.Running()) })
	promauto.NewGaugeFunc(prometheus.GaugeOpts{
		Name: "downstream_pool_free", Help: "空闲 worker 数",
	}, func() float64 { return float64(pool.Free()) })
	promauto.NewGaugeFunc(prometheus.GaugeOpts{
		Name: "downstream_pool_waiting", Help: "阻塞在 Submit 上的调用方数",
	}, func() float64 { return float64(pool.Waiting()) })

	mux := http.NewServeMux()
	mux.Handle("/metrics", promhttp.Handler())
	mux.HandleFunc("/fetch", func(w http.ResponseWriter, r *http.Request) {
		id := r.URL.Query().Get("id")
		ctx, cancel := context.WithTimeout(r.Context(), 300*time.Millisecond)
		defer cancel()

		ch := make(chan result, 1) // 缓冲 1：调用方超时返回后，任务仍能写完退出
		err := pool.Submit(func() {
			body, err := callDownstream(ctx, id)
			ch <- result{body, err}
		})
		if err != nil {
			submitted.WithLabelValues("rejected").Inc()
			if errors.Is(err, ants.ErrPoolOverload) {
				w.Header().Set("Retry-After", "1")
				http.Error(w, "busy", http.StatusServiceUnavailable)
				return
			}
			http.Error(w, err.Error(), http.StatusInternalServerError)
			return
		}
		submitted.WithLabelValues("accepted").Inc()

		select {
		case res := <-ch:
			if res.err != nil {
				http.Error(w, res.err.Error(), http.StatusBadGateway)
				return
			}
			fmt.Fprint(w, res.body)
		case <-ctx.Done():
			http.Error(w, "upstream timeout", http.StatusGatewayTimeout)
		}
	})

	srv := &http.Server{Addr: ":8080", Handler: mux}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	go func() {
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Printf("serve: %v", err)
		}
	}()

	<-ctx.Done()
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	_ = srv.Shutdown(shutdownCtx) // 先停新流量

	// 再等在途任务退出；超时返回 ants.ErrTimeout
	if err := pool.ReleaseTimeout(5 * time.Second); err != nil {
		log.Printf("release pool: %v", err)
	}
}
```

三个要点：**先 `Shutdown` 再 `Release`**，否则新请求会拿到 `ErrPoolClosed`；**结果 channel 必须带缓冲**，否则调用方超时返回后任务会永久阻塞在 `ch <-` 上；**`ErrPoolOverload` 必须映射成 503**，而不是 500 或静默丢弃。`downstream_pool_waiting` 持续大于 0 是容量不足的早期信号，比等 P99 恶化再扩容快得多。

### 4.3 Invoke：池内固定函数，结果自己回收

```go
package main

import (
	"fmt"
	"sync/atomic"

	"github.com/panjf2000/ants/v2"
)

func main() {
	var sum atomic.Int64
	done := make(chan struct{}, 1000)

	// Invoke 属于 PoolWithFunc：worker 跑的是同一个函数，每次只换参数
	p, err := ants.NewPoolWithFunc(4, func(arg any) {
		sum.Add(int64(arg.(int)))
		done <- struct{}{}
	})
	if err != nil {
		panic(err)
	}
	defer p.Release()

	for i := 1; i <= 1000; i++ {
		if err := p.Invoke(i); err != nil {
			fmt.Println("invoke:", err) // 池满时阻塞；WithNonblocking 下返回 ErrPoolOverload
		}
	}
	for i := 0; i < 1000; i++ {
		<-done // Invoke 不同步，完成要自己等
	}
	fmt.Println("sum:", sum.Load()) // 500500
}
```

如果这 1000 个任务需要「任一失败就取消其余」，ants 帮不了你——那是 errgroup 的职责；如果只需要「同时最多 4 个」，`PoolWithFunc(4, ...)` 就够了。

---

## 5. 版本演进

| 版本 | 变化 | 迁移注意 |
|---|---|---|
| v1 → v2 | 模块路径改为 `github.com/panjf2000/ants/v2`，配置改为函数式 `Option` | v1 与 v2 是两个模块，可同时存在于一个二进制里，但会得到两个互不相干的池与两套上限 |
| v2.8.0 | 新增 `ReleaseTimeout` | 关停要等任务跑完时用它，不要裸 `Release` |
| v2.9.0 | 新增 `MultiPool` / `MultiPoolWithFunc` | 单池成为锁竞争瓶颈时按分片拆，按 RoundRobin/LeastTasks 选池 |
| v2.11.0 | 新增 `PoolWithFuncGeneric[T]` | 带参池可以摆脱 `any` 断言 |
| v2.11.4 | 修正 `Free()` 的计算结果 | 依赖 `Free()` 做指标的要升级 |
| v2.12.0（2026-03-20） | 新增 `ReleaseContext`；最低 Go 版本提到 1.19；worker 时间戳改纳秒 | 低于 Go 1.19 的项目不能升级 |
| v2.12.1（2026-05-25） | `allDone` 的关闭时机从 `Reboot` 移到 `Release` | 修的是「无 worker 时 `Release`/`Reboot` 互相等待」的竞态，建议跟进 |

截至 2026-11，v2 线最新 tag 为 **v2.12.1**。v2.12.x 之后 `dev` 分支仍有提交，但没有新的稳定 tag；生产环境请钉 tag，不要跟分支。

---

## 6. 常见坑与最佳实践

1. **把池当队列**。`NewPool(1000)` 不会给你 1000 个排队位，只会在 1000 个并发跑满后把调用方全部挂起。**对策**：把 `cap` 当「并发预算」按下游容量定，需要削峰就上真正的队列。
2. **非阻塞模式下忽略 `ErrPoolOverload`**。最常见的写法是 `_ = pool.Submit(fn)`，池一满任务就静默消失。**对策**：每个 `Submit` 都检查 error，把 overload 映射成 503/降级，并单独计数。
3. **`Submit` 闭包捕获循环变量**。Go 1.22 之前所有闭包共享同一个变量，任务里读到的是最后一个值。**对策**：`for _, v := range xs { v := v; pool.Submit(func(){ use(v) }) }`，或升级到 Go 1.22+ 并确认模块的 `go` 指令已更新。
4. **忘记 `Release`**。purge 与 ticktock 两个后台 goroutine 会一直活着，测试里尤其明显。**对策**：`defer pool.Release()`；`TestMain` 里用 `goleak` 断言；长生命周期池在服务关停时 `ReleaseTimeout`。
5. **在池里做长阻塞 IO 或等锁**。容量会被慢任务占死，`Running` 打满、`Waiting` 飙升，所有请求排队。**对策**：进池的任务必须有自己的超时；按「阻塞型任务」而非 CPU 核数估算 `cap`；不要让任务无限等待下游。
6. **以为 panic 会返回给调用方**。默认只把 panic 值与栈打到日志，worker 退出、任务丢失，`Submit` 早就返回了 `nil`。**对策**：关键任务内部自己 `defer recover()` 转成 error 写回结果 channel；`WithPanicHandler` 只用来计数和上报。
7. **设置了 `WithPanicHandler` 却不再打栈**。源码是 if/else，handler 存在时默认日志被跳过。**对策**：handler 里自己 `log.Printf("%v\n%s", v, debug.Stack())`。
8. **只看 `Running` 不看 `Waiting`**。`Running` 打满可能只是正常满载，`Waiting` 持续增长才说明容量不够或下游变慢。**对策**：三个指标一起采集，给 `Waiting > 0` 的持续时间设告警。
9. **在池任务里再向同一个池 `Submit`**。池满时外层任务会占着 worker 等内层任务，内层又拿不到 worker，形成自锁；官方注释专门警告过这一点。**对策**：拆成两个池（IO 池与计算池），或内层用 `WithNonblocking` 快速失败。
10. **把 `Submit` 返回 `nil` 当成任务成功**。它只表示「已交付给某个 worker」。**对策**：需要结果就传 channel；需要错误就让任务把 error 写回来，别指望 `Submit`。
11. **任务绑定请求的 ctx，handler 却先返回了**。ctx 被 cancel，任务要么白跑要么写结果时才发现无人接收。**对策**：要么同步等任务结束，要么用 `context.WithoutCancel` + 自己的超时，并保证结果 channel 有缓冲。
12. **`Tune` 用错场景**。对无限池与 `PreAlloc` 池无效；调小不会终止正在跑的任务，只是不再新建；调大也不保证立刻扩容。**对策**：容量在启动时按容量规划定好，`Tune` 只作为应急手段。
13. **用 `size <= 0` 或包级 `ants.Submit`**。两者都等于无上限，和「不用池」没区别，还多了一层假象。**对策**：显式写正数；包级 API 只用于一次性脚本。

> 判断要不要池，只问一句：**这个并发度有没有一个「超过就会出事」的数字？** 有，就把它写进配置、接进监控；没有，说明你还没找到真正的瓶颈，先别加池。

---

## 7. 选型对比

| 维度 | 裸 `go func()` | `errgroup.SetLimit` | `semaphore.Weighted` | `ants.Pool` | 手写 worker pool |
|---|---|---|---|---|---|
| 并发上限 | 无 | 有，但仅限本次 `Wait` 范围内 | 有，进程级共享 | 有，进程级共享 | 有，自己实现 |
| worker 复用 | 否 | 否，每个任务新建 G | 否，仍是每个任务一个 G | **是**，空闲栈复用 | 是 |
| 任务排队 | 无，直接压给调度器 | 超出 limit 时阻塞调用方 | 拿不到许可时阻塞 | 默认阻塞调用方，可改为拒绝 | 取决于实现 |
| 结果与错误回传 | 自己做 channel | 首个 error 由 `Wait` 返回 | 不涉及 | **不提供** | 自己做 |
| 取消传播 | 自己做 select | `WithContext` 自动派生 | ctx 只作用于 `Acquire` | 不提供，任务内自己处理 | 自己做 |
| 可观测性 | 无 | 弱 | 弱 | `Running/Free/Waiting/Cap` | 自己加 |
| 代码量 | 最低 | 低 | 低 | 低 | 高 |
| 典型场景 | 一次性、数量可控 | 一次请求内扇出 N 个下游 | 保护单个下游或临界区 | 常驻服务的高频短任务 | 需要优先级、批量、结果协议 |

四条结论：

- **`errgroup` 管的是「一组任务的生命周期」**：错误传播、取消、汇合。它的 `SetLimit` 是**每次调用局部**的限制，两个请求各自 `SetLimit(10)`，对下游就是 20 个并发。要全局共享的预算，靠它做不到。
- **`semaphore.Weighted` 管的是「许可」**：进入前 `Acquire`、离开 `Release`，任务本身仍然是每次新建 goroutine。它适合保护一个明确的临界区或下游，但不复用 worker，也不提供任何池指标。
- **`ants` 管的是「worker 的复用与总量」**：适合长期存在、提交频率高、单个任务短的服务。它不提供结果、错误、取消、顺序——这些必须由调用方补齐，补不齐就不要用。
- **手写 worker pool 只在需要 ants 给不了的东西时才有意义**：优先级队列、批量拉取、任务持久化、每任务结果协议。否则你只是在重写一个更差的 ants。

最后一句：**`errgroup` 与 `ants` 不冲突，它们是两层**。errgroup 负责「这次请求的 10 个下游调用要么全成要么全取消」，ants 负责「整个进程对某个下游的总并发不超过 32」。把两层混成一层，才会出现「限流限了个寂寞」。
