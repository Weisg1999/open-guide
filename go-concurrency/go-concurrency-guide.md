# Go 并发与 Context 技术指南

> Go 并发的完整心智模型：goroutine 便宜但不免费，channel 是通信而非队列，context 是取消树而非参数袋。
> 适用版本：Go 1.21+（context 语义稳定），示例基于 **Go 1.27.4**（2026-11-10）　|　标准库文档：<https://pkg.go.dev/context>　|　协议：BSD-3-Clause

---

## 1. 项目简介

Go 没有「并发框架」。能力全部内建在语言与运行时：`go` 语句、`chan`、`select`、`sync`、`sync/atomic`、`context`，加上外挂的 `golang.org/x/sync/errgroup`、`singleflight`。设计目标只有一个：**让「同时等待很多个 I/O」写起来像串行代码一样直白**。

它解决三个具体问题：**调度成本**（OS 线程栈按 MB 计、切换走内核；goroutine 初始栈 2 KB、切换在用户态）；**协作方式**（channel 把「交接数据」与「交接控制权」合成一个动作，让竞争条件从「忘记加锁」变成「没有共享」）；**生命周期**（一次请求扇出到 DB、缓存、下游 RPC，需要一条统一取消链在超时或客户端断开时砍掉整棵调用树）。

适用：HTTP/gRPC 服务端、I/O 扇出与聚合、后台作业、流式 pipeline。**不适合**：CPU 密集计算（并行度受 `GOMAXPROCS` 限制，收益常被 GC 与缓存失效吃掉）、硬实时系统、跨进程协调（那是 etcd / 消息队列的领域）。

本文立场：**先想清楚数据归谁所有，再选原语**。选 channel 还是 mutex 不是风格问题，而是所有权是否转移的问题。

---

## 2. 能力总览

| 原语 | 语义 | 关键成本 | 典型用途 |
|---|---|---|---|
| `go f()` | 运行时调度的可增长栈协程 | ~2 KB 栈；不回收则永久占内存 | 一切异步执行 |
| `chan T` | 带锁环形队列 + 等待队列 | 每次收发一次锁/原子操作 | 所有权转移、结果收集、信号 |
| `select` | 多路等待，随机公平选择 | O(cases)，需乱序 | 超时、取消、多源聚合 |
| `sync.Mutex` | 互斥，不可重入 | 无竞争一次 CAS，竞争则睡眠 | 保护共享状态 |
| `sync.RWMutex` | 读写锁 | 读锁也有原子开销；写可能饥饿 | 读多写少且临界区较长 |
| `sync.WaitGroup` | 等待一组任务结束 | 一个原子计数器 | 扇出后汇合 |
| `sync.Once` | 恰好执行一次 | 快路径一次原子读 | 单例、懒初始化 |
| `sync.Pool` | 临时对象复用，GC 会清空 | 每 P 一个私有队列 | 减少短命对象分配 |
| `sync/atomic` | 无锁原子操作（1.19+ 类型化） | 单条指令 + 屏障 | 计数器、标志位、无锁读 |
| `context` | 取消树 + 截止时间 + 请求域值 | 每次派生一次分配 | 请求生命周期管理 |
| `errgroup` | WaitGroup + 错误传播 + 取消 | 薄封装 | 扇出任务编排 |
| `singleflight` | 相同 key 的并发调用合并 | 一张 key→call 表 | 缓存击穿、重复加载 |

---

## 3. 核心概念与原理

### 3.1 GMP 调度模型

```text
G (goroutine)  待执行的函数 + 栈 + 状态
M (machine)    OS 线程，真正跑在 CPU 上
P (processor)  逻辑处理器，持本地队列 runq（256 个 G）

  ┌── P0 runq[256] ──► M0 运行中
  ├── P1 runq[256] ──► M1 自旋找活（work stealing 偷别的 P 一半）
  └── 全局队列（P 本地空了才取，带锁）
```

- **M 必须绑定 P 才能执行 G**，`GOMAXPROCS` 限制的是 P 的数量（并行度），不是 goroutine 数量；
- **work stealing** 让「goroutine 数远大于核数」依然高效，负载自动均衡；
- **阻塞系统调用**时 M 会把 P 交出（handoff），另一个 M 接管继续跑队列——慢 syscall 不会拖住整个 P；但 cgo 与纯阻塞 syscall 会让线程数膨胀；
- **抢占**：Go 1.14 起基于信号的异步抢占，长循环也能被打断。

### 3.2 goroutine 的栈与成本

初始栈 2 KB（Go 1.4 起从 8 KB 下调），**按需倍增/收缩**，上限默认 1 GB。栈是**可移动的**（copy stack），所以不能把 Go 指针传给 C，也不能假设栈地址稳定。直接成本 ≈ 2 KB 栈 + `g` 结构体（约 300 B），创建约 200~300 ns，比线程轻两个数量级。但「便宜」只对**创建**成立，不对**驻留**成立：每个阻塞在 channel 上的 G 都占着栈与 sudog，一万个就是二十多 MB 常驻且被 GC 扫描——所以 goroutine 必须**有界**，裸 `go func()` 无上限是事故的常见起点。

### 3.3 channel 的内部结构

```text
type hchan struct {
    qcount   uint     // 当前元素个数
    dataqsiz uint     // 缓冲区容量（make 的第二个参数）
    buf      unsafe.Pointer // 指向 [dataqsiz]T 环形数组
    sendx    uint     // 下一个写入位置
    recvx    uint     // 下一个读取位置
    recvq    waitq    // 阻塞的接收者（sudog 链表）
    sendq    waitq    // 阻塞的发送者
    lock     mutex    // 保护以上全部字段
}
```

收发路径（`chansend` / `chanrecv`）：

1. **等待者优先直传**：发送时若 `recvq` 非空，直接把值从发送方栈拷到接收方栈，**跳过缓冲区**——这是无缓冲 channel 高效的原因；
2. **缓冲区**：未满则写 `buf[sendx]`，`sendx = (sendx+1) % dataqsiz`；
3. **满/空则阻塞**：G 被包成 `sudog` 挂到 `sendq`/`recvq`，`gopark` 让出 P，由对端 `goready` 唤醒；
4. **close**：置 `closed=1`，唤醒 `recvq` 全部（零值 + `ok=false`）与 `sendq` 全部（**panic: send on closed channel**）。

由此推出语义边界：无缓冲 channel 是**同步交接**（rendezvous），不是「发出去就完事」；有缓冲 channel 是容量为 n 的队列，满了照样阻塞，因此**自带背压**——这正是它优于无界内存队列之处；`len(ch)` 只是瞬时快照，**不是同步手段**；nil 或已关闭的 channel 再 close 会 panic，所以**只由发送方关闭且只关一次**。

### 3.4 select 的语义

- **随机公平**：运行时打乱 case 顺序（随机起点轮询），多个 channel 同时就绪时**等概率**选中。不要依赖书写顺序；
- **全阻塞则挂起**：无 `default` 且无就绪 case 时当前 G 睡眠；若**所有 case 都是 nil channel**，则永久阻塞——当进程内所有 G 都阻塞，运行时报 `all goroutines are asleep - deadlock!`，否则只是单个 G 静默泄漏；
- **nil channel 是有用特性**：永不就绪，可用于「动态关掉某一路」；
- **`default` 是忙轮询**：`for { select { case <-ch: ... default: } }` 会跑满一个核，只能用于 try-send，不能用来「等」；
- case 的 channel 表达式只求值一次；循环里 `case <-time.After(d)` 每次都新建定时器，应改用 `time.Ticker`。

### 3.5 context 的树形取消

```text
Background() / TODO()            ← 根，永不取消
      │
  cancelCtx   (WithCancel)       ← Done()/Err() + children map，负责传播
      ├── timerCtx (WithTimeout / WithDeadline)  ← timer + deadline
      │        └── valueCtx (WithValue)          ← 单链表，只存一对 k/v
      └── valueCtx (WithValue)
```

| 类型 | 取消时发生什么 |
|---|---|
| `cancelCtx` | `close(done)`、`err=Canceled`、递归 cancel 全部 children |
| `timerCtx` | timer 到期 → `cancel(true, DeadlineExceeded)` 并 stop 定时器 |
| `valueCtx` | 不参与取消；`Value` 沿父链单向查找，O(depth) |

- **取消是协作式的**：`ctx.Done()` 只是 `<-chan struct{}`，没人替你杀 goroutine，被调用方必须自己 `select` 它并返回；
- **`WithValue` 不是参数袋**：只放 trace_id、身份、超时预算这类请求域元数据，业务参数走签名；key 用私有空结构体类型；
- 派生 context **必须 cancel**，否则 `timerCtx` 的定时器与父节点 children map 会挂到超时。

Go 1.20/1.21 补齐三块：`WithCancelCause` + `Cause`（区分「谁取消的」，比只有 `Canceled` 有用）；`WithoutCancel`（切断取消与截止时间但**保留 value**，异步审计/补偿的标配）；`AfterFunc`（取消时**异步**执行回调并返回 `stop`，不常驻 goroutine）。

### 3.6 happens-before 与内存模型

Go 不保证「多核看到同一份数据」。只有下列关系建立可见性：**程序顺序**（同 goroutine 内）、**channel**（第 n 次发送完成 happens before 第 n 次接收完成；`close` happens before 收到零值）、**Mutex**（`Unlock` before 后续 `Lock` 返回）、**Once / WaitGroup**（`Done` before `Wait` 返回）、**atomic**（`sync/atomic` 一直是顺序一致语义；Go 1.19 起提供 `atomic.Int64` 等类型化封装，可直接当同步原语）。

`go test -race` 用 ThreadSanitizer 做**动态** happens-before 检测，只能发现**本次执行真正发生**的竞争。所以「race 通过」不等于「没有竞争」，只等于没被抓到——CI 常态跑 `-race`，并让并发路径的集成测试也跑一遍。

---

## 4. 使用实战

### 4.1 errgroup：扇出 + 限流 + 错误传播

```go
package main

import (
	"context"
	"errors"
	"fmt"
	"time"

	"golang.org/x/sync/errgroup"
)

func main() {
	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()

	g, ctx := errgroup.WithContext(ctx) // 任一任务出错 → 派生 ctx 取消
	g.SetLimit(4)                       // 并发上限，超出的 Go 调用阻塞

	out := make([]string, 10)
	for i := range out {
		g.Go(func() error {
			select {
			case <-time.After(20 * time.Millisecond):
				out[i] = fmt.Sprintf("item-%d", i) // 各写各的下标，无竞争
				return nil
			case <-ctx.Done():
				return ctx.Err() // 超时或兄弟任务失败
			}
		})
	}
	err := g.Wait() // 只保留首个错误；nil 表示全部成功
	fmt.Println(len(out), err, errors.Is(err, context.DeadlineExceeded))
}
```

`WithContext` 只返回**第一个**错误，其余被丢弃（要全收就自己用 `errors.Join`）。`SetLimit` 是写 worker pool 最短的路径，代价是排队时阻塞调用方。

### 4.2 pipeline 与 fan-out/fan-in

```go
package main

import (
	"context"
	"fmt"
	"sync"
)

// 通用 stage：上游关闭即退出，每次阻塞都有取消出口
func stage(ctx context.Context, in <-chan int, f func(int) int) <-chan int {
	out := make(chan int)
	go func() {
		defer close(out)
		for n := range in {
			select {
			case out <- f(n):
			case <-ctx.Done():
				return
			}
		}
	}()
	return out
}

func main() {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	in := make(chan int, 8)
	for i := 1; i <= 8; i++ {
		in <- i
	}
	close(in) // 源关闭 → 各 stage 依次退出
	square := func(n int) int { return n * n }

	results := make(chan int, 8)

	var wg sync.WaitGroup
	for i := 0; i < 2; i++ { // fan-out：两个 worker 抢同一输入
		wg.Add(1)
		go func() {
			defer wg.Done()
			for v := range stage(ctx, in, square) {
				results <- v // fan-in：汇总到同一 channel
			}
		}()
	}
	go func() { wg.Wait(); close(results) }() // 全部 worker 结束才关闭

	sum := 0
	for v := range results {
		sum += v
	}
	fmt.Println("sum:", sum) // 204
}
```

三条铁律：**每个 stage 自己 `defer close(out)`**、**消费方必须读到关闭**、**每个阻塞点都要有 `<-ctx.Done()` 出口**。缺任一条就是泄漏。

### 4.3 优雅退出：信号 + 独立关闭预算

```go
// package serve：真实项目里由 main 传入 *http.Server
package serve

import (
	"context"
	"errors"
	"fmt"
	"os/signal"
	"syscall"
	"time"
)

type Server interface{ Shutdown(context.Context) error }

func run(ctx context.Context, srv Server) error {
	ctx, stop := signal.NotifyContext(ctx, syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	<-ctx.Done() // 收到信号，或上游取消
	fmt.Println("cause:", context.Cause(ctx))

	// 父 ctx 已取消，但关停需要独立的 10s 预算，所以先脱钩再派生超时
	shutdownCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 10*time.Second)
	defer cancel()

	if err := srv.Shutdown(shutdownCtx); err != nil {
		return errors.Join(err, ctx.Err())
	}
	return nil
}
```

这里是 context 最容易写错的地方：直接 `srv.Shutdown(ctx)` 会立刻返回 `context.Canceled`，一个连接都等不到。**脱钩（保留 value/日志字段）+ 独立超时**才是正确姿势。

### 4.4 singleflight：缓存击穿

```go
package main

import (
	"context"
	"fmt"
	"sync"
	"sync/atomic"
	"time"

	"golang.org/x/sync/singleflight"
)

var group singleflight.Group
var dbCalls atomic.Int64

func loadFromDB(ctx context.Context, key string) (string, error) {
	dbCalls.Add(1)
	select {
	case <-time.After(80 * time.Millisecond):
		return "value-of-" + key, nil
	case <-ctx.Done():
		return "", ctx.Err()
	}
}

func Get(ctx context.Context, key string) (string, error) {
	// 第三个返回值 shared 标记「结果是否来自别人的调用」，可用于统计合并率
	v, err, _ := group.Do(key, func() (any, error) {
		// 共享调用不能绑定某一个调用者的 ctx，否则它断开会连带取消所有人
		return loadFromDB(context.WithoutCancel(ctx), key)
	})
	if err != nil {
		return "", err
	}
	return v.(string), nil
}

func main() {
	var wg sync.WaitGroup
	for i := 0; i < 100; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			_, _ = Get(context.Background(), "user:1")
		}()
	}
	wg.Wait()
	fmt.Println("loadFromDB calls:", dbCalls.Load()) // 通常为 1：100 个并发只回源一次
}
```

`shared=true` 时要注意：**这一次传进来的 ctx 被忽略是正常的**——共享计算的生存期属于第一个发起者，而不是任意一个等待者。

### 4.5 sync.Pool、atomic 与检测

`sync.Pool` 只解决「短命对象的分配压力」，不承担缓存职责——每次 GC 都会清空（Go 1.13 起保留两个周期）。用法三条：`Get` 后当脏对象处理先 `Reset`；`Put` 之前不要继续持有引用；取出的大对象若返回给调用方，**必须拷贝**（Pool 里的实例随时会被别人取走）。`sync/atomic` 的适用边界是**单个变量的读写**：计数器用 `atomic.Int64`，无锁读配置用 `atomic.Pointer[T]`；一旦涉及「多个字段要保持一致」，就必须退回 mutex，原子操作组合不出事务。

检测三件套：`go test -race -count=1 ./...`（CI 常态；需 cgo 工具链、慢 2~20 倍，不要带到生产）、`go vet ./...`（抓 `copylocks` 复制锁与 `lostcancel` 漏调 cancel）、`pprof` 的 `/debug/pprof/goroutine?debug=2`（看全部 goroutine 栈）。

---

## 5. 版本演进

| 版本 | 并发相关变化 | 迁移注意 |
|---|---|---|
| Go 1.4 | goroutine 初始栈从 8 KB 降到 2 KB | 栈按需增长，创建成本大幅下降 |
| Go 1.14 | 基于信号的**异步抢占** | 长循环不再饿死调度器 |
| Go 1.19 | `atomic.Int64` / `Pointer[T]` / `Bool` 类型化封装 | 新代码优先类型化 API；顺序一致语义并非 1.19 才引入 |
| Go 1.20 | `WithCancelCause` / `CancelCauseFunc` / `Cause` | 别再只判 `Canceled`，把原因带上 |
| Go 1.21 | `WithoutCancel`、`AfterFunc`、`WithDeadlineCause`、`WithTimeoutCause` | 异步任务终于有官方「脱钩保值」手段 |
| Go 1.22 | **循环变量每轮独立**；`math/rand/v2` 去掉全局锁 | 依赖共享循环变量的老代码**语义变了**，升级必审 |
| Go 1.23 | 迭代器；未被引用的 `Timer`/`Ticker` 可被 GC 回收 | `time.After` 泄漏风险下降，高频循环仍用 Ticker |
| Go 1.24 | Swiss table map、`weak` 包、`runtime.AddCleanup` | 大 map 并发读更快；替代 `SetFinalizer` |
| Go 1.25 | `sync.WaitGroup.Go`；`testing/synctest` 稳定；容器感知 `GOMAXPROCS` | K8s 里不再需要 automaxprocs |
| Go 1.27 | 调度器与 channel 持续优化 | 升级前跑全量 `-race` 回归 |

---

## 6. 常见坑与最佳实践

1. **goroutine 泄漏——阻塞在无人接收的 channel**。`go func(){ ch <- v }()` 而消费方提前返回，G 永久挂起。**对策**：所有发送写成 `select { case ch <- v: case <-ctx.Done(): }`，并在 `TestMain` 用 `goleak` 断言无残留。
2. **泄漏——`range` 一个永不关闭的 channel**。**对策**：明确「谁关闭」，只用发送方关闭，用 `defer close(out)`。
3. **泄漏——HTTP body / rows / 连接没关**。`resp.Body` 不关会让 transport 的读 goroutine 常驻。**对策**：`defer resp.Body.Close()`，并读完 `io.Copy(io.Discard, resp.Body)` 才能复用连接。
4. **泄漏——重试循环没有退出条件**。**对策**：次数上限 + `ctx.Done()` 出口 + 指数退避，三者缺一不可。
5. **把 context 存进 struct 或当参数袋**。**对策**：ctx 永远第一个参数显式传递；业务值走结构体，只有跨 API 边界的元数据进 `WithValue`。
6. **忘记 `cancel()`**。timer 会活到超时，父节点 children map 一直挂。**对策**：`ctx, cancel := ...` 下一行立刻 `defer cancel()`；`go vet` 的 `lostcancel` 能抓一部分。
7. **`WaitGroup.Add` 写在 goroutine 里**。`Wait` 可能先返回，或计数为负 panic。**对策**：`Add` 必须在启动前调用；Go 1.25+ 用 `wg.Go(...)`。
8. **复制含锁的结构体**。值传递后两个副本各自加锁 = 没锁。**对策**：`go vet` 的 `copylocks`，含锁类型一律指针接收者并内嵌 `noCopy`。
9. **`Mutex` 不可重入**。同一 goroutine 重复 `Lock` 直接死锁。**对策**：把临界区抽成不加锁的私有方法，公开方法只负责加锁。
10. **以为 `RWMutex` 一定更快**。临界区只有几条指令时它比 `Mutex` 更贵，还有写饥饿。**对策**：临界区极短用 `Mutex`；读多写少且读操作有实际耗时（序列化、拷大结构）才上 `RWMutex`。
11. **用 channel 当无界队列**。`make(chan T, 100000)` 只是把内存当缓冲，消费者一慢就 OOM。**对策**：容量取「能吸收的突变量」，满了阻塞形成背压；真要缓冲用 Kafka/Redis。
12. **`select` 误用：`default` 空转或 nil channel 死锁**。前者跑满一个核，后者让 case 永不就绪。**对策**：`default` 只用于 try-send，要等就阻塞、要定频用 `time.Ticker`；`select` 前保证 channel 非 nil，故意用 nil 语义时必须写注释。
14. **共享 ctx 给 singleflight / 后台任务**。第一个调用者断开会连带取消所有等待者。**对策**：共享计算用 `WithoutCancel` 或独立 `Background()` + 自己的超时。
15. **无差别 `go func()` 包住每个请求**。并发度随流量线性上升，DB 连接池瞬间打爆。**对策**：`errgroup.SetLimit` 或带缓冲 channel 当信号量，把并发钉在连接池大小附近。

> 「不要通过共享内存来通信」的正确理解是**优先转移所有权**，不是禁用 mutex。反例：用 channel 传一个 `map`，但发送方之后还在改它——既没转移所有权，又多了 channel 开销，是最差的组合。判断标准只有一句：**发送之后，发送方是否还持有并修改这个值？** 若还持有，就该用 mutex。

---

## 7. 选型对比

| 维度 | channel | `sync.Mutex` | `sync/atomic` | `errgroup` | `singleflight` | 串行 |
|---|---|---|---|---|---|---|
| 解决问题 | 所有权转移、信号、编排 | 保护共享状态 | 单变量无锁更新 | 一组任务的错误与取消 | 相同 key 请求合并 | 最简单 |
| 开销 | 中（锁 + 唤醒） | 低（无竞争一次 CAS） | **最低**（单指令） | 低（薄封装） | 低（一张表） | 零 |
| 可读性 | 扇出/流水线最佳 | 状态机、缓存最佳 | 计数/标志最佳 | 批量 I/O | 缓存加载 | 最佳 |
| 主要风险 | 泄漏、死锁、nil | 死锁、粒度、复制 | 复合操作非原子 | 只留第一个错误 | 共享 ctx 被连带取消 | 延迟叠加 |
| 典型场景 | worker pool、pipeline | 配置热更新、连接池 | QPS 计数、closed 标志 | 并行调 N 个下游 | 缓存击穿 | 有顺序依赖 |

一句话结论：**传递「数据 + 控制权」用 channel；保护「一块共享状态」用 mutex；只是一个数字或开关用 atomic；扇出独立任务用 errgroup；同一 key 被并发重复计算用 singleflight；剩下的情况先怀疑要不要并发。**

### 什么时候串行更快

- **任务少且都快**（单个 < 1 µs）：goroutine 创建 + 调度 + channel 交接的开销超过被并行的工作本身；
- **逻辑天然有序**：B 依赖 A 的结果，并发只增加同步点；
- **资源已饱和**：并发全在同一把锁上排队，或 CPU 已打满——只增加切换与 GC 压力；
- **顺序 I/O**：并发写同一 `*os.File` 或同一 TCP 连接要额外加锁，收益为负。

### 什么时候不该用并发

- **正确性成本 > 性能收益**：一年跑几次的批处理，串行 3 秒和并行 300 ms 对业务没区别，但并发 bug 可能查三天；
- **给不出有界并发**：没有连接池上限、没有背压、没有超时——那不是用并发，是制造雪崩；
- **外部约束不允许**：审计日志、账务流水必须有序写入；依赖的 SDK 不是并发安全的（部分 C 库绑定、老 SDK），加锁封装常比串行更慢更脆。

参考：[Go 内存模型](https://go.dev/ref/mem)、[Go 并发模式（pipelines）](https://go.dev/blog/pipelines)、[context 包文档](https://pkg.go.dev/context)、[调度器源码 proc.go](https://go.dev/src/runtime/proc.go)。
