# robfig/cron 技术介绍与使用指南

> Go 语言中最流行的定时任务库，以标准 cron 表达式驱动，单机进程内调度。
> 仓库：<https://github.com/robfig/cron>　|　当前主线版本：v3

---

## 1. 库简介

`robfig/cron` 是一个用 Go 实现的**进程内定时任务调度器**。它的设计目标是把 Unix `cron` 守护进程的核心能力搬进 Go 应用：

- 使用**标准 5 字段 cron 表达式**（分 时 日 月 周）描述触发时间；
- 可选开启**秒级精度**（Quartz 风格 6 字段）；
- 支持**时区**（全局时区 + 单任务 `CRON_TZ=` 前缀）；
- 支持 `@every 5m` 这类**固定间隔描述符**；
- 基于**装饰器链（Chain/JobWrapper）**提供可插拔的横切能力：panic 恢复、防重叠执行、日志等。

它解决的是"单进程内的周期性触发"问题——不持久化、不分布式、不保证错过补偿（错过的触发会被跳过而非补跑）。如果需要分布式、可重试、可观测的任务队列，应考虑 Asynq 等方案；但在"一个服务里跑几个定时逻辑"的场景下，robfig/cron 几乎是事实标准。

### 安装

```bash
go get github.com/robfig/cron/v3
```

```go
import "github.com/robfig/cron/v3"
```

要求 Go 1.11+（Go Modules）。

---

## 2. 功能特性总览

| 能力 | 说明 |
|---|---|
| 标准表达式 | 默认解析 5 字段标准 cron spec（首字段为"分"） |
| 秒级精度 | `cron.New(cron.WithSeconds())` 开启 6 字段（Quartz 兼容，不含年字段） |
| 预定义描述符 | `@yearly` `@monthly` `@weekly` `@daily` `@hourly` `@every <dur>` |
| 时区支持 | 全局 `WithLocation()`；单条表达式 `CRON_TZ=Asia/Tokyo ...`（兼容旧 `TZ=`） |
| 动态管理 | 运行中 Add / Remove / Entries 快照，均线程安全 |
| 装饰器链 | `WithChain(...)` 挂载 JobWrapper，内置 Recover / SkipIfStillRunning / DelayIfStillRunning |
| 结构化日志 | 兼容 go-logr 的 `Logger` 接口，`WithLogger()` 注入 |
| 自定义解析器 | `WithParser()` 替换表达式解析行为（可扩展字段布局） |
| 优雅停止 | `Stop()` 返回 `context.Context`，可等待在跑任务全部结束 |

---

## 3. 核心概念与原理

### 3.1 五个核心抽象

```
Cron ────持有───► Entry[] ──引用──► Schedule（何时）
  │                                Entry ──引用──► Job（做什么）
  └──Chain 装饰 Job ► WrappedJob（怎么包着做）
```

- **`Cron`**：调度器本体。内部维护 `entries []*Entry`，一个独占的调度 goroutine，以及 add / remove / snapshot / stop 四个 channel。
- **`Schedule`**：调度策略接口，唯一方法 `Next(t time.Time) time.Time`——"给定当前时刻，下次触发是何时"。内置实现：
  - `SpecSchedule`：由 cron 表达式解析而来，按"分钟对齐"逐字段推进求解下一次触发；
  - `ConstantDelaySchedule`：`@every 5m` 生成，从 Add 时刻起每 5 分钟触发一次。
- **`Job`**：任务接口，唯一方法 `Run()`。库提供 `FuncJob`（`func()` 的适配器），任何实现 `Run()` 的结构体都可作为任务。
- **`Entry`**：一条调度记录 = ID + Schedule + Job + `Next`（下次触发时间）+ `Prev`（上次触发时间）。
- **`Chain` / `JobWrapper`**：装饰器模式。`JobWrapper` 是 `func(Job) Job`，`Chain.Then(job)` 等价于 `m1(m2(m3(job)))`——按传入顺序从外到内包裹任务。

### 3.2 运行循环（核心源码解读）

`Start()` 后调度 goroutine 执行 `run()`，其主循环逻辑：

```go
for {
    // 1. 每轮按 Next 时间升序排序（零值排最后）
    sort.Sort(byTime(c.entries))

    // 2. 计算最近的触发时间，设定 timer
    timer = time.NewTimer(c.entries[0].Next.Sub(now))

    for {
        select {
        case now = <-timer.C:                    // 到点：触发所有 Next <= now 的任务
            for _, e := range c.entries {
                if e.Next.After(now) { break }
                c.startJob(e.WrappedJob)         // 每个任务独立 goroutine 执行
                e.Prev = e.Next
                e.Next = e.Schedule.Next(now)    // 立即计算下一次触发
            }
        case newEntry := <-c.add:                // 运行中新增任务
        case replyChan := <-c.snapshot:           // Entries() 请求快照
        case id := <-c.remove:                   // 运行中删除任务
        case <-c.stop:                           // 停止
            return
        }
        break
    }
}
```

关键结论：

1. **单 goroutine 调度**：所有 entries 的读写只发生在这一个 goroutine 里，天然免锁；外部调用（Add/Remove/Entries）在 running 状态下全部通过 channel 传递，这就是它线程安全的来源。
2. **排序 + Timer**：每轮把 entries 按 `Next` 升序排列，只对最早的一个设 timer——而不是轮询。没有任务时睡眠 100000 小时（仍能响应 add/stop）。
3. **触发即起新 goroutine**：`startJob` 里 `go func(){ j.Run() }()`，因此**单个任务慢不会阻塞其他任务**，也不会阻塞调度循环。
4. **错过的触发会被跳过**：例如任务本应 10:00 触发但进程 10:05 才启动，10:00 的触发直接消失（`Next` 从当前时间重新求解），没有补跑机制。
5. **重叠执行默认允许**：若任务耗时超过触发间隔，下一轮到点会再次起新 goroutine，同一任务并发多份。防重叠需显式加 `SkipIfStillRunning` / `DelayIfStillRunning` 装饰器。

### 3.3 表达式解析（parser.go）

默认解析器为标准 5 字段布局：

```
┌───────────── 分钟 (0 - 59)
│ ┌───────────── 小时 (0 - 23)
│ │ ┌───────────── 日   (1 - 31)
│ │ │ ┌───────────── 月   (1 - 12)
│ │ │ │ ┌───────────── 周   (0 - 6, 周日=0)
│ │ │ │ │
* * * * *
```

语法规则：

- `*`：任意值；`5`：固定值；`*/15`：步进（每 15 个单位）；
- `1-5`：范围；`1,3,5`：列表；可组合如 `1-10/2`；
- 月/周可用英文缩写（`JAN-DEC`、`SUN-SAT`），不区分大小写；
- **日(Dom) 与 周(Dow) 同时指定时是"或"语义**（与标准 cron 一致）：`0 0 1 * 1` 表示每月 1 号**或**每周一零点；
- `WithSeconds()` 开启后为 6 字段（秒 分 时 日 月 周），兼容 Quartz 但不支持年字段。

预定义描述符：

| 描述符 | 等价表达式 | 含义 |
|---|---|---|
| `@yearly` / `@annually` | `0 0 1 1 *` | 每年 1 月 1 日 0 点 |
| `@monthly` | `0 0 1 * *` | 每月 1 日 0 点 |
| `@weekly` | `0 0 * * 0` | 每周日 0 点 |
| `@daily` / `@midnight` | `0 0 * * *` | 每天 0 点 |
| `@hourly` | `0 * * * *` | 每小时第 0 分 |
| `@every 1h30m` | — | 从添加时刻起固定间隔（`time.ParseDuration`） |

时区前缀：`CRON_TZ=Asia/Shanghai 0 30 9 * * *`（旧写法 `TZ=` 仍支持）。

### 3.4 装饰器链（chain.go）

```go
NewChain(m1, m2, m3).Then(job) ≡ m1(m2(m3(job)))
```

内置三个 JobWrapper：

- **`Recover(logger)`**：`defer recover()` 捕获任务 panic，打印堆栈。**v3 默认不再 recover**，必须显式挂载；
- **`SkipIfStillRunning(logger)`**：上一次还没跑完就跳过本次触发（用容量 1 的 channel 实现，非阻塞抢锁）；
- **`DelayIfStillRunning(logger)`**：上一次没跑完则本次等待（mutex 串行化），延迟超过 1 分钟记日志。

Chain 是全局的（构造 Cron 时通过 `WithChain` 设定，对该实例所有任务生效），任务级差异可通过 `cron.Schedule(schedule, customWrappedJob)` 自行包裹实现。

### 3.5 v3 的主要变更（相对 v1/v2）

- 导入路径变为 `github.com/robfig/cron/v3`；
- 默认解析**标准 5 字段**（v1 默认带秒，是混乱之源），秒字段需显式开启；
- 构造改为函数式选项（`WithLocation/WithParser/WithChain/WithSeconds/WithLogger`），废弃 `SetLocation` 等 setter；
- 日志改用 go-logr 接口，移除 `WithPanicLogger`/`WithVerboseLogger`；
- 默认不 recover panic，交给 Chain；
- 修复了"删除任务导致下一个任务延迟执行"等多个历史 bug。

---

## 4. 使用方法（代码实战）

### 4.1 快速上手

```go
package main

import (
	"fmt"
	"time"

	"github.com/robfig/cron/v3"
)

func main() {
	c := cron.New() // 标准 5 字段解析器，时区 time.Local

	// 每分钟打印一次
	c.AddFunc("* * * * *", func() {
		fmt.Println("每分钟触发:", time.Now().Format("15:04:05"))
	})

	// 每天凌晨 2:30
	id, err := c.AddFunc("30 2 * * *", func() {
		fmt.Println("每日对账任务")
	})
	if err != nil {
		panic(err) // 表达式非法时返回 error
	}
	_ = id

	c.Start()
	defer c.Stop()

	select {} // 阻塞主 goroutine
}
```

### 4.2 秒级精度与预定义描述符

```go
c := cron.New(cron.WithSeconds()) // 6 字段：秒 分 时 日 月 周

c.AddFunc("*/5 * * * * *", func() {}) // 每 5 秒
c.AddFunc("0 30 9 * * MON-FRI", func() {}) // 工作日 9:30:00

c2 := cron.New()
c2.AddFunc("@every 2h30m", func() {}) // 每 2.5 小时（固定间隔）
c2.AddFunc("@daily", func() {})       // 每天 0 点
```

> 注意区别：`@every 1h` 是**固定间隔**（从 Add 时刻起算，走 `ConstantDelaySchedule`），而 `0 * * * *` 是**整点对齐**（走 `SpecSchedule`）。

### 4.3 时区

```go
// 方式一：全局时区
loc, _ := time.LoadLocation("Asia/Shanghai")
c := cron.New(cron.WithLocation(loc))

// 方式二：单表达式指定时区（推荐，规格化写法）
c.AddFunc("CRON_TZ=Asia/Tokyo 0 6 * * *", func() {}) // 东京时间早 6 点
```

### 4.4 自定义 Job（实现 Job 接口）

```go
type SyncJob struct {
	Table string
}

func (s *SyncJob) Run() {
	fmt.Printf("同步表 %s ...\n", s.Table)
}

func main() {
	c := cron.New()
	id, _ := c.AddJob("*/10 * * * *", &SyncJob{Table: "orders"})
	fmt.Println("entry id:", id)
	c.Start()
	select {}
}
```

### 4.5 装饰器：panic 恢复与防重叠

```go
c := cron.New(
	cron.WithChain(
		cron.Recover(cron.DefaultLogger),        // 捕获 panic 并记日志
		cron.SkipIfStillRunning(cron.DefaultLogger), // 上次未跑完则跳过本次
	),
	cron.WithSeconds(),
)

c.AddFunc("*/5 * * * * *", func() {
	panic("boom") // 不会拖垮调度器
})
```

自定义 JobWrapper 也非常简单：

```go
func WithMetric(name string) cron.JobWrapper {
	return func(j cron.Job) cron.Job {
		return cron.FuncJob(func() {
			start := time.Now()
			j.Run()
			recordDuration(name, time.Since(start)) // 上报耗时
		})
	}
}
```

### 4.6 运行中动态管理任务

```go
c := cron.New()
c.Start()

id, _ := c.AddFunc("*/1 * * * *", func() { fmt.Println("tick") })
// 运行中添加：经 add channel 交给调度 goroutine，立即生效

for _, e := range c.Entries() { // 快照，线程安全
	fmt.Printf("entry=%d next=%v prev=%v\n", e.ID, e.Next, e.Prev)
}

time.Sleep(5 * time.Minute)
c.Remove(id) // 移除；Entry(id) 可查单个
```

### 4.7 优雅停止

```go
ctx := c.Stop() // 发停止信号，不再派发新触发
<-ctx.Done()    // 等待所有"正在执行"的任务结束
fmt.Println("所有任务已退出")
```

`Start()` 与 `Run()` 的区别：`Start` 起后台 goroutine 立即返回；`Run` 在**当前 goroutine** 阻塞执行（适合进程只跑调度器的场景）。

### 4.8 集成结构化日志

```go
logger := cron.VerbosePrintfLogger(log.New(os.Stdout, "cron: ", log.LstdFlags))
c := cron.New(cron.WithLogger(logger))
// 输出 schedule/wake/run/added/removed 等事件，便于排查"为什么没触发"
```

自定义日志只需实现 `cron.Logger`（Error/Info 两个方法），可直接对接 zap/logr 等。

---

## 5. 常见坑与最佳实践

1. **忘记 `Start()`**：Add 之后不 Start，任务静静躺在 entries 里。
2. **默认 5 字段**：从 v1 迁移时 `* * * * *` 不再是"每秒"而是"每分"；要秒级请 `WithSeconds()`。
3. **panic 不再默认恢复**：生产环境务必挂 `cron.Recover(logger)`，否则一个任务的 panic 会崩掉整个进程（任务 goroutine 的 panic 无人 recover 时会终止程序）。
4. **长任务重叠**：默认允许同一任务并发多份。清理/上报类任务通常要 `SkipIfStillRunning`。
5. **不要在任务里直接操作 entries**：一律走 Add/Remove/Entries 接口，它们才是线程安全的。
6. **错过即跳过**：进程重启后不会补跑停机期间错过的触发；需要补偿逻辑要在任务内部自行实现（如对比 `e.Prev`）。
7. **时区陷阱**：服务器时区 ≠ 业务时区时，显式 `WithLocation` 或 `CRON_TZ=`，并注意夏令时（库对不存在的时间点有处理，但业务上仍建议用无夏令时时区）。
8. **K8s 多副本场景**：每个 Pod 各自跑一份 cron，同一任务会被触发 N 次。需要分布式锁（如 Redis `SET NX`）或改用中心化调度方案——这也是 robfig/cron 与分布式任务队列（Asynq 等）的本质边界。

---

## 6. 版本演进一览

| 版本 | 要点 |
|---|---|
| v1 | 最初的可选秒字段格式，gopkg.in 分发 |
| v2 | 支持移除任务等（未正式发布为默认） |
| v3 (2019.6) | Go Modules、标准 spec 为默认、Chain/JobWrapper、logr 日志、大量 bug 修复；**当前推荐** |

v3 发布后 API 趋于稳定，社区以维护为主，新项目直接使用 `github.com/robfig/cron/v3` 即可。
