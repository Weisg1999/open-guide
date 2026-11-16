# Go 性能剖析技术指南

> 性能问题不能靠猜：pprof 告诉你时间花在哪，trace 告诉你 goroutine 在等什么，benchmark 告诉你改动是否真的更快。
> 工具链：`runtime/pprof` / `net/http/pprof` / `runtime/trace`，适用 **Go 1.27.4**（2026-11-10）　|　文档：<https://pkg.go.dev/runtime/pprof>　|　协议：BSD-3-Clause

---

## 1. 项目简介

[Prometheus 篇](../prometheus/prometheus-guide.md)解决「系统整体趋势是否异常」，本篇解决更下一层的问题：**这 200ms 花在哪一行代码、这次分配为什么上了堆、这个 goroutine 为什么三个月没被调度**。指标告诉你「病了」，剖析告诉你「病灶在哪」。

性能剖析不是调参玄学，而是**有采样误差、有观测开销、需要对照实验**的测量方法：输出是概率分布而不是精确计时，所以必须配合统计工具才能下结论。该做剖析的信号是——要优化但只有猜测（「大概是 JSON 慢」，采 30 秒往往结论相反）、P99 抖动而均值正常（排队或 GC）、RSS 单调上升或 GC 占比超 5%、改完要证明有效、容量规划前找饱和点。反过来，没有 baseline 就改代码、线上不隔离不鉴权就开 profile、只采 5 秒就下结论，都是浪费时间的做法。

| 症状 | 第一手段 | 说明 |
|---|---|---|
| CPU 高、吞吐上不去 | CPU profile | 找热点函数与调用链，区分 flat / cum |
| 延迟高但 CPU 不高 | `go tool trace` / block profile | 在等锁、等网络、等调度，不是在算 |
| 内存涨、GC 频繁 | heap profile + `GODEBUG=gctrace=1` | 先分清「泄漏」还是「分配太快」 |
| goroutine 数持续上涨 | goroutine profile | 直接看几万个 goroutine 卡在哪一行 |

### 工具链与最小示例

`pprof` / `trace` / `bench` 都随工具链分发，唯一要装的是 benchstat：`go install golang.org/x/perf/cmd/benchstat@latest`。

```go
package main

import (
	"os"
	"runtime/pprof"
)

func work() { // 假装是被测逻辑
	var s float64
	for i := 0; i < 5e7; i++ {
		s += float64(i % 7)
	}
	_ = s
}

func main() {
	f, _ := os.Create("cpu.pprof")
	defer f.Close()
	if err := pprof.StartCPUProfile(f); err != nil {
		panic(err) // 同一时刻只能有一个 CPU profile
	}
	defer pprof.StopCPUProfile() // 必须调用，否则文件不完整
	work()
}
```

```bash
go run main.go && go tool pprof -top cpu.pprof
```

---

## 2. 能力总览

| 能力 | 观测对象 | 采样方式与开销 | 默认状态 | 典型入口 |
|---|---|---|---|---|
| **CPU profile** | on-CPU 时间分布（调用栈） | 100Hz 信号采样，约 5% 开销 | 按需开启 | `/debug/pprof/profile?seconds=30` |
| **heap profile** | 堆上存活 / 累计分配 | 平均每 512KB 分配采一次，开销低 | 常开（低频） | `/debug/pprof/heap?gc=1` |
| **block profile** | 阻塞在同步原语上的时长 | 按时间间隔采样，开销随采样率上升 | **关闭** | `runtime.SetBlockProfileRate` |
| **mutex profile** | 锁竞争等待时长 | 按事件 1/n 采样 | **关闭** | `runtime.SetMutexProfileFraction` |
| **goroutine profile** | 全量 goroutine 栈快照 | 无采样，快照时短暂 STW | 常开 | `/debug/pprof/goroutine?debug=2` |
| **trace** | 调度、GC、syscall、阻塞事件 | 事件流，文件大、开销高 | 按需开启 | `/debug/pprof/trace?seconds=5` |
| **benchmark** | 单测级别的耗时 / 分配 / 吞吐 | 无额外开销（独立进程） | 测试时 | `go test -bench=. -benchmem` |
| **benchstat** | 两组 benchmark 的显著性差异 | 纯离线统计（Mann-Whitney U） | — | `benchstat old.txt new.txt` |
| **PGO** | 用真实 profile 指导编译优化 | 构建变慢，运行时零额外开销 | 放 `default.pgo` 即自动生效 | `go build -pgo=auto` |

两点补充：`allocs` profile 是 heap 的 `alloc_*` 视图（同一份数据换 `sample_index`）；`threadcreate` 只在怀疑系统线程泄漏时用。**pprof 只统计 Go 代码的时间**，cgo 里的耗时会算到调用点但看不到内部，纯 C 热点要靠 `perf` 转 pprof。

---

## 3. 核心概念与原理

### 3.1 CPU profile：100Hz 采样与采样偏差

runtime 用 `setitimer(ITIMER_PROF)` 每 **10ms** 发一次 `SIGPROF`，信号处理器抓取**当前正在运行的 goroutine** 的调用栈并累加。跑 1 秒 ≈ 100 个样本，由此带来三个后果：

**它是统计，不是计时**：只跑 3ms 的函数单次命中概率约 30%，连续错过几十次完全可能——短函数天然被低估，甚至从火焰图上消失。**内联会改写归属**（被内联的函数记在调用者名下），**且只看得到 on-CPU 时间**（阻塞、等锁、syscall 不在里面，别拿它解释「CPU 不高但慢」）。

```text
flat  flat%   sum%        cum   cum%
0.42s 42.00% 42.00%      0.42s 42.00%  runtime.memmove          ← flat：自身耗时
0.11s 11.00% 53.00%      0.63s 63.00%  codec.(*Encoder).Write   ← cum：含被调函数
```

纪律：**至少采 30 秒**；两版本之间 5% 的差异别用 profile 判断（置信区间比差异大），交给 benchmark + benchstat；排查先 `-cum` 找链路，再 `-flat` 找自身热点。`SetCPUProfileRate` 理论上能改采样率，但受内核定时器精度限制，**保持 100Hz**。

### 3.2 heap profile：MemProfileRate 与两个 space

heap profile 也是采样：`runtime.MemProfileRate` 默认 **512 × 1024 字节**，含义是「平均每分配 512KB 采一个样本」。采样点按指数分布随机选取，不是每 512KB 一刀切，所以小对象要靠**加权估算**（样本自带权重）。

```text
alloc_space ──────────────────────────────► 进程启动至今累计分配（只增不减）
inuse_space      ┌────┐          ┌───────► 最近一次 GC 时仍存活（随 GC 回落）
```

`inuse_space` 高 → 对象**长期活着**：全局 map 只增不删、缓存无上限、切片持有大底层数组、goroutine 泄漏带住栈上变量；`alloc_space` 高但 `inuse_space` 平稳 → **分配风暴**，不是泄漏：优化方向是 `sync.Pool`、预分配、减少拷贝、避免 `fmt.Sprintf`。

两个必踩细节：heap profile 是**上一次 GC 的快照**，抓之前先 `runtime.GC()` 或加 `?gc=1`；`MemProfileRate = 1` 能精确到每次分配，但开销大到只能本地短跑。

### 3.3 block / mutex / goroutine：三个默认关掉的视角

| profile | 记录什么 | 开启方式 | 什么时候看 | 陷阱 |
|---|---|---|---|---|
| block | 阻塞在 channel / select / Mutex / Cond / WaitGroup 上的**时长** | `SetBlockProfileRate(ns)`，`1` = 全记 | CPU 不高但延迟高 | 全量记录会明显拉低吞吐 |
| mutex | 锁**竞争**导致的等待时长 | `SetMutexProfileFraction(n)`，1/n 采样 | 怀疑锁粒度太粗 | 只记竞争，不记临界区执行时长 |
| goroutine | 所有 goroutine 的栈快照 | 默认开启 | goroutine 数单调上升 | 快照需要短暂 STW |

**mutex 只覆盖锁，block 覆盖所有同步原语**：「写无缓冲 channel 卡住」在 mutex profile 里完全看不见。生产建议 `SetBlockProfileRate(1e6)`（平均每 1ms 阻塞时间采一个样本，更短的事件按比例抽样），排障窗口开、结束归零。goroutine profile 是查泄漏最直接的工具：`?debug=1` 按栈聚合出「同一种栈有 8000 个」，`?debug=2` 打出完整栈；**压测后 goroutine 数回不到基线就是泄漏**，不用等内存涨。

### 3.4 逃逸分析：栈还是堆，是编译器决定的

Go 没有「堆对象」语法，变量放哪由编译器决定。栈分配代价接近于零（函数返回整块回收），堆分配要付出分配 + GC 扫描 + 可能的 assist。

```bash
go build -gcflags='-m' ./...        # 结论：escapes to heap / moved to heap
go build -gcflags='-m -m' ./...     # 原因：为什么逃逸
go build -gcflags='-m -l' ./...     # 关内联，避免「被内联后看不见」
```

常见原因：**返回局部变量指针**（语言要求，无法避免）、**赋给 `interface{}` 装箱**、**被逃逸的闭包捕获**、**放进 channel / 全局 map / 逃出函数的 slice**、**对象过大超过栈上限**。反直觉的是 `defer` 在开放编码后基本不产生额外逃逸，而 `fmt.Println(x)` 里的 `x` 几乎一定逃逸——参数是 `...any`。

```go
package main

import "fmt"

type Point struct{ X, Y int }

func onStack() Point { return Point{1, 2} }  // 值返回，通常留在栈上
func onHeap() *Point { return &Point{1, 2} } // 逃逸：指针被返回给调用者
func boxed() any     { return Point{1, 2} }  // 逃逸：装箱进 interface

func noEscape() int {
	p := &Point{3, 4}
	return p.X + p.Y // 没传出函数，编译器可把它放栈上
}

func main() { fmt.Println(onStack(), *onHeap(), boxed(), noEscape()) }
```

隐性成本是**栈扩容**：goroutine 栈从 2KB 起，不够就翻倍并整体拷贝（上限 1GB）。深递归、大栈帧会反复触发拷贝，这也是「把大函数拆小反而更快」的原因之一。

### 3.5 GC 模型：三色标记、写屏障、GOGC 与 GOMEMLIMIT

```text
标记准备(STW, 几十µs) → 并发标记(占 25% CPU，写屏障记录新指针) → 标记终止(STW, 几十µs) → 并发清扫
```

三色抽象：**白**（未访问，回收候选）、**灰**（已发现，子对象未扫完）、**黑**（自身扫完）。不变式是「黑对象不能指向白对象」，破坏它就丢对象，所以并发标记期间靠**混合写屏障**（Dijkstra 插入 + Yuasa 删除）在指针写入时把新对象染灰、把旧对象保活。

**STW 不是长尾主因**：只在标记开始/终止，通常几十微秒；百毫秒尖刺多半来自 **assist**（分配太快的 goroutine 被抓去帮忙扫描）或内存归还 OS。**`GOGC=100`** 表示下次 GC 目标堆为存活堆的 2 倍，调大省 CPU 费内存，`GOGC=off` 只在配了 `GOMEMLIMIT` 时才敢用。**`GOMEMLIMIT`（1.19+）是软上限**：接近时 GC 更激进甚至突破 GOGC 目标，但栈、runtime 元数据、cgo 内存不受控，仍可能 OOM。

```bash
GODEBUG=gctrace=1 ./app
# gc 12 @3.421s 2%: 0.021+1.8+0.003 ms clock, 0.17+0.4/1.2/0.1+0.02 ms cpu, 42->44->21 MB, 48 MB goal, 8 P
#        ↑第12轮 ↑启动秒 ↑GC占总CPU ↑STW ↑并发标记 ↑STW              ↑标记前→标记后→存活  ↑下次目标
```

`2%` 是 GC 占总 CPU 比例，超过 5% 就值得优化；`21` 一直涨说明存活对象在累积（回到 3.2）。

```go
package main

import "runtime/debug"

func main() {
	debug.SetMemoryLimit(900 << 20) // 容器 limit 1GiB，留 10% 给栈与元数据
	debug.SetGCPercent(200)         // 用内存换 CPU，让 GOMEMLIMIT 兜底
}
```

### 3.6 trace：看 goroutine 在等什么

CPU profile 回答「算了什么」，trace 回答「**没在算的时候在等什么**」。它记录每个 goroutine 的状态迁移（running / runnable / blocked）、每个 P 的调度、GC 周期、syscall 与网络阻塞，精度到微秒。

- **Goroutine analysis**：按栈聚合，直接给出「这种 goroutine 平均阻塞 800ms」，泄漏与慢调用一眼可见；
- **Scheduler latency**：runnable → running 的间隔。持续 >1ms 说明 P 不够，或有长耗时 goroutine 长期不让出（1.14 后有异步抢占，但 syscall 与 cgo 仍可能拖住）；
- **时间线**：每个 G 一条轨道，能看出「醒了却排不上队」和「有 8 个 P 却只有一个在跑」。

开销比 CPU profile 大一个量级（几十 MB/秒），**生产只开 1–5 秒**，或用 `trace.NewFlightRecorder` 的环形缓冲保留最近 N 秒、出事再落盘。进程内采集只需 `trace.Start(f)` 与 `defer trace.Stop()`，注意同一时刻只能有一个 trace；HTTP 服务直接打 `/debug/pprof/trace?seconds=5` 即可。

```bash
go tool trace -http=:8080 trace.out   # 优先看 Goroutine analysis 与 Scheduler latency
```

---

## 4. 使用实战

### 4.1 `net/http/pprof` 接入与鉴权

标准做法是**独立端口 + 独立 mux + 鉴权 + 只监听回环**。直接 `import _ "net/http/pprof"` 会注册到 `DefaultServeMux`，业务一旦也用默认 mux，pprof 就跟着业务端口裸奔。

```go
package main

import (
	"crypto/subtle"
	"log"
	"net/http"
	"net/http/pprof"
)

func requireToken(token string, next http.Handler) http.Handler {
	want := []byte("Bearer " + token)
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if subtle.ConstantTimeCompare([]byte(r.Header.Get("Authorization")), want) != 1 {
			http.Error(w, "unauthorized", http.StatusUnauthorized)
			return
		}
		next.ServeHTTP(w, r)
	})
}

func main() {
	mux := http.NewServeMux() // 独立 mux，不污染业务路由
	mux.HandleFunc("/debug/pprof/", pprof.Index)
	mux.HandleFunc("/debug/pprof/profile", pprof.Profile) // CPU，默认 30s
	mux.HandleFunc("/debug/pprof/trace", pprof.Trace)     // 轨迹，默认 1s

	srv := &http.Server{
		Addr:    "127.0.0.1:6060", // 只监听回环，外部走跳板机 / kubectl port-forward
		Handler: requireToken("change-me", mux),
	}
	log.Fatal(srv.ListenAndServe()) // 业务服务在另一端口，互不影响
}
```

> 更稳：pprof 端口不要出现在 Service 上，只走 `port-forward`；或用 sidecar 反代做 IP allowlist + mTLS。token 放 Header 而非 URL query，避免被 access log 与浏览器历史留存。

### 4.2 采 CPU 与生成火焰图

```bash
# 直接起 Web UI（内置火焰图、调用图、源码视图）
go tool pprof -http=:8081 'http://127.0.0.1:6060/debug/pprof/profile?seconds=30'
curl -s -o cpu.pprof 'http://127.0.0.1:6060/debug/pprof/profile?seconds=30'
go tool pprof -top -nodecount=25 cpu.pprof && go tool pprof -list='codec.*Write' cpu.pprof
```

火焰图读法：**横轴是样本占比，不是时间顺序**；自下而上是调用链，每层宽度是该函数（含子调用）的占比。找「宽而平顶」的帧——宽说明耗时长，平顶说明耗时在自身。若顶层出现 `runtime.memmove`、`runtime.mallocgc`、`runtime.gcAssistAlloc`，根因在**分配**而不是算法，往下找谁在分配。

### 4.3 benchmark 前后对比与 benchstat

```go
package codec

import (
	"bytes"
	"testing"
)

var sink []byte // 包级变量接收结果，防止编译器把循环整个优化掉

func BenchmarkEncode(b *testing.B) {
	payload := bytes.Repeat([]byte("go"), 4096)
	var buf bytes.Buffer

	b.ReportAllocs()
	b.SetBytes(int64(len(payload))) // 让输出带 MB/s
	b.ResetTimer()                  // 丢掉 setup 的时间

	for i := 0; i < b.N; i++ {
		buf.Reset()
		if err := Encode(&buf, payload); err != nil {
			b.Fatal(err)
		}
	}
	sink = buf.Bytes()
}
```

```bash
go test -run='^$' -bench='BenchmarkEncode$' -benchmem -count=6 ./codec > old.txt
# 改完代码后
go test -run='^$' -bench='BenchmarkEncode$' -benchmem -count=6 ./codec > new.txt
benchstat old.txt new.txt
```

```text
                 │   old.txt    │               new.txt                │
                 │    sec/op    │    sec/op     vs base                │
Encode-8            412.5µs ± 2%   288.1µs ± 1%  -30.16% (p=0.002 n=6)
                 │   allocs/op  │   allocs/op    vs base                │
Encode-8             128 ± 0%        12 ± 0%    -90.62% (p=0.002 n=6)
```

`-count=6` 是底线（benchstat 需要多样本算分布）；`p<0.05` 才算显著，`~` 表示差异在噪声内；`±2%` 是本机抖动，**跨机器比较毫无意义**。分配从 128 降到 12 通常收益最大——它同时降 CPU 与 GC 压力，而 `sec/op` 只反映前者。

### 4.4 内存泄漏定位流程

第一步永远是**区分泄漏与高分配**：`inuse_space` 单调涨是泄漏，`alloc_space` 涨而 `inuse` 平稳是分配风暴。

```bash
# 1) 抓基线（?gc=1 先强制 GC，避免陈旧快照）；业务量稳定时等 15~30 分钟再抓第二份
curl -s 'http://127.0.0.1:6060/debug/pprof/heap?gc=1' -o heap-1.pprof
curl -s 'http://127.0.0.1:6060/debug/pprof/heap?gc=1' -o heap-2.pprof

# 2) 相减：只看两份之间「新增且未释放」的对象，再定位到行
go tool pprof -sample_index=inuse_space -base heap-1.pprof -top heap-2.pprof
go tool pprof -sample_index=inuse_space -base heap-1.pprof -list='.*' heap-2.pprof

# 3) 对照 alloc_space，区分泄漏与分配太快
go tool pprof -sample_index=alloc_space -top heap-2.pprof
```

在 `-top` 里对照下面的签名，通常一眼就能定位：

```text
inuse_space 涨 + 栈顶 net/http.(*persistConn)   → resp.Body 没 Close，连接与 buffer 一起被带住
inuse_space 涨 + 栈顶 time.NewTicker/time.After → 循环里起了永不停止的定时器
inuse 平 + inuse_objects 暴涨                   → 每请求新建 buffer / logger / regexp
goroutine 数单调涨 + 栈停在 chan receive        → 消费者已退出，生产者永久阻塞
```

最后是**验证**：修复后同样跑 30 分钟，`heap_objects` 与 RSS 应回到平台期。heap profile 不含 goroutine 栈（每个至少 2KB）、cgo 分配与 mmap，RSS 涨而 heap 平的时候去查 goroutine 数与外部组件。

---

## 5. 版本演进速览

| 版本 | 与性能剖析相关的关键变化 |
|---|---|
| Go 1.8 | 并发 GC 改用**混合写屏障**，STW 从毫秒级降到通常几十微秒；`go tool trace` 已能看调度时间线 |
| Go 1.14 | **异步抢占**（基于信号），长循环不再拖住调度与 GC，profile 更贴近真实 |
| Go 1.16 | `runtime/metrics` 稳定，可结构化读取 GC / 堆 / 调度指标 |
| Go 1.19 | **`GOMEMLIMIT`** 软内存上限（`debug.SetMemoryLimit`），容器场景的关键拼图 |
| Go 1.20 | PGO 预览（`-pgo`），需显式指定 profile 文件 |
| Go 1.21 | **PGO 正式 GA**：主包目录放 `default.pgo` 即自动启用，典型 CPU 收益 2%~7% |
| Go 1.22 | 循环变量改为每轮独立，闭包捕获的逃逸行为随之变化，升级后应重跑 benchmark |
| Go 1.24 | `testing.B.Loop` 简化基准写法；map 改用 Swiss table，查找与扩容行为变化 |
| 近年 1.2x | trace 支持环形缓冲（FlightRecorder）、容器感知的 `GOMAXPROCS` 默认值、PGO 内联策略增强 |

> PGO 的采集：把生产 CPU profile 存成主包目录下的 `default.pgo`，`go build` 自动启用（`-pgo=off` 关闭）。收益高度依赖 profile 是否代表生产负载——mock 流量可能零收益甚至负优化，算法级问题永远优先。

---

## 6. 常见坑与最佳实践

1. **没有 baseline 就动手**：一次改三处再一起测，无法归因。一次只改一个变量，先存 `old.txt`。
2. **benchmark 失真**：setup 算进计时（漏 `b.ResetTimer`）、结果没被使用而被编译器整个删掉（用包级 `sink` 或 `b.Loop`）、`b.N` 太小导致启动成本占比过高。
3. **微基准不代表真实负载**：单 goroutine、无锁竞争、数据全在 L1、没有 GC 压力。基准里快 3 倍，线上可能因为多一层指针跳转变慢。互补手段是压测 + trace。
4. **只看平均值不看分位数**：`go test -bench` 报的是均值，SLO 看的是 P99/P999。均值 5ms、P99 800ms 的服务「平均很快」，体感极差。
5. **把 CPU 打满当成容量足够**：70% 之后排队延迟非线性上升，再叠上 GC assist、连接池与 fd 上限，真正饱和点远早于 100%。容量规划看延迟拐点。
6. **pprof 端口裸奔**：heap 与 goroutine 快照里有函数名、包路径、栈上的参数值（可能含 token），profile 接口还能被反复调用来打 DoS。必须独立端口 + 鉴权 + 内网。
7. **线上开 profile 不设限**：CPU profile 约 5% 开销、30 秒通常几百 KB 到数 MB（`go tool trace` 才是几十 MB 量级），并发请求还能把采样器拖垮；`SetBlockProfileRate(1)` 会让每次 channel 操作都记一笔，吞吐可能掉一半。限时、限流、用完归零。
8. **混淆 `inuse_space` 与 `alloc_space`**：拿 `alloc_space` 的 top 去修「泄漏」，改半天 RSS 不降；抓 heap 不加 `?gc=1`，看到的是旧快照。
9. **只盯业务代码不看 GC**：`gcAssistAlloc` 占比高说明分配速率超过回收速率，加机器没用，要减少分配或调 `GOGC/GOMEMLIMIT`。
10. **容器里不看 CPU limit**：Go 1.25+ 已默认容器感知，但旧版本或显式设置 `GOMAXPROCS` 时仍按宿主机核数推导；2 核 limit 的 Pod 以为自己有 64 核，结果是海量 assist 与调度延迟。
11. **PGO 用错样本**：拿 mock 或旧版本代码的 profile，收益归零甚至负优化。大改后重采，多实例 profile 用 `pprof -proto` 合并。
12. **把火焰图宽度当因果**：宽只代表耗时多，不代表该优化。先确认它在关键路径上、且复杂度可降。

---

## 7. 选型对比

| 工具 | 回答什么问题 | 时间尺度 | 开销 | 生产可用 | 盲区 |
|---|---|---|---|---|---|
| **pprof（cpu/heap）** | 时间花在哪个函数、内存是谁占的 | 秒~分钟 | 低（~5%） | 可，短时开启 | 阻塞、调度、syscall 内部 |
| **block / mutex profile** | 在等哪把锁、哪个 channel | 秒~分钟 | 中~高 | 谨慎，短窗口 | 网络 IO、无竞争的锁 |
| **goroutine profile** | 几万个 goroutine 卡在哪一行 | 瞬时快照 | 低（短暂 STW） | 可，常开 | 生命周期与因果 |
| **`go tool trace`** | 为什么延迟高但 CPU 不高 | 毫秒~秒 | 高（事件流） | 谨慎，1~5s | 函数内部耗时 |
| **benchmark + benchstat** | 改动是否真的更快、差异是否显著 | 纳秒~毫秒 | 无 | 只在 CI/本地 | 真实并发与长尾 |
| **PGO** | 不改代码还能不能再榨几个点 | 编译期 | 构建变慢 | 是，零运行时开销 | 算法与内存问题 |
| **持续剖析（Pyroscope / Parca）** | 长期趋势、哪次发布引入回归 | 分钟~天 | 低（采样） | 是 | 单次请求细节 |
| **eBPF（Pixie / btrace）** | 无侵入看 syscall、网络、锁 | 微秒~秒 | 低~中 | 是 | Go 用户态语义、内联函数 |

一句话决策：

- **默认顺序**：`GODEBUG=gctrace=1` + 指标判断类型 → CPU 高上 CPU profile，延迟高上 trace，内存涨上 heap profile → 效果用 benchmark + benchstat 验证。
- **线上排障**：pprof 独立端口 + 鉴权 + 限时；集群层面叠 Pyroscope / Parca 做持续剖析，回答「哪次发布变慢了」。
- **不想改代码、要看 syscall 与网络**：eBPF 工具，但 Go 的内联与用户态调度仍要靠 pprof / trace 补。
- **性能治理常态化**：把 benchmark 放进 CI，用 benchstat 卡住 >5% 的回归，比事后救火便宜得多。
- **PGO 是加分项**：先把分配降下来、锁粒度调对，再谈那 2%~7%。

参考：<https://pkg.go.dev/runtime/pprof>、<https://pkg.go.dev/runtime/trace>、<https://go.dev/doc/pgo>、<https://github.com/google/pprof>
