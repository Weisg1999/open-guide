# asynq 技术文档与使用手册

> 依据来源：[hibiken/asynq](https://github.com/hibiken/asynq) 官方仓库（README、Wiki、godoc、源码、CHANGELOG）与 [hibiken/asynqmon](https://github.com/hibiken/asynqmon) 仓库
> 对应版本：asynq **v0.26.0**（2026-02-03 发布）· asynqmon v0.7.x · MIT 协议
> 文档定位：技术原理 + API 使用手册 + 部署指南（含 Web UI）
> 成文时间：2026-08-31

---

## 目录

- [一、asynq 是什么](#一asynq-是什么)
- [二、技术栈与总体架构](#二技术栈与总体架构)
- [三、核心概念与任务生命周期](#三核心概念与任务生命周期)
- [四、功能详解：原理与代码](#四功能详解原理与代码)
- [五、完整可运行示例](#五完整可运行示例)
- [六、部署指南（含 Asynqmon UI）](#六部署指南含-asynqmon-ui)
- [七、最佳实践与常见陷阱](#七最佳实践与常见陷阱)
- [八、版本演进速览](#八版本演进速览)
- [九、参考链接](#九参考链接)

---

## 一、asynq 是什么

### 1.1 定义

asynq 是一个 **Go 语言实现的分布式任务队列库**（library，不是独立服务），基于 **Redis** 作为消息代理（broker）：

- **Client** 把任务放入队列；
- **Server** 从队列拉取任务，为每个任务启动一个 worker goroutine 调用你的 Handler；
- 多个 Server 实例（跨机器）组成 worker 集群，并发消费，天然实现水平扩展与高可用。

官方一句话定位：**Simple, reliable & efficient distributed task queue in Go**。

它解决的典型问题：异步处理（发邮件、转码）、延时任务（T+1 执行）、失败重试与死信、周期任务（cron）、任务去重、优先级调度——即"把工作从请求路径上摘下来，可靠地、可观测地异步完成"。

### 1.2 设计哲学

| 原则 | 体现 |
|---|---|
| 库而非框架 | 无需部署独立调度服务，Client/Server 以库形式嵌入你的进程 |
| Redis 单一依赖 | 唯一外部依赖是 Redis（≥4.0），运维面极小 |
| 可组合 | Handler/Middleware/ServeMux 设计与 `net/http` 同构，心智平移 |
| 可观测 | 内置 Prometheus 指标、CLI、Web UI（Asynqmon）三件套 |
| at-least-once | 任务持久化于 Redis，worker 崩溃后任务可被找回重跑 |

### 1.3 能力总览（官方 Feature 列表）

| 能力 | 说明 |
|---|---|
| at-least-once 执行 | 任务至少执行一次（要求业务幂等） |
| 任务调度 | `ProcessIn/ProcessAt` 指定未来时刻执行 |
| 失败重试 | 默认最多 25 次、指数退避、可定制 |
| 崩溃自动恢复 | worker 崩溃后 lease 过期的任务自动找回 |
| 加权优先级队列 | 按权重分配各队列的处理时间占比 |
| 严格优先级队列 | 高优先级队列清空前不处理低优先级 |
| 任务去重 | `Unique`（TTL 锁）/ `TaskID`（唯一 ID）两种方式 |
| 超时与截止 | 任务级 `Timeout`/`Deadline`，context 取消传播 |
| 任务聚合 | 同 `(queue, group)` 任务合并为一个批量执行 |
| Handler + 中间件 | 灵活的处理接口，支持中间件链 |
| 队列暂停/恢复 | 运行时暂停某队列的消费 |
| 周期任务 | Scheduler + cron 表达式；`PeriodicTaskManager` 支持动态增删 |
| Redis 哨兵 | 自动故障转移 |
| Prometheus 集成 | 队列指标采集与可视化 |
| Web UI / CLI | Asynqmon 监控运维界面；asynq 命令行工具 |

### 1.4 生态组件

| 组件 | 形态 | 用途 |
|---|---|---|
| `github.com/hibiken/asynq` | Go 库 | 核心库（Client/Server/Scheduler/Inspector） |
| `github.com/hibiken/asynqmon` | Web UI（二进制 / Docker / Go 库） | 队列与任务的监控、管理界面 |
| `tools/asynq`（asynq CLI） | 命令行 | 终端内查看/操作队列与任务 |
| `x/metrics` | Go 包 | 队列指标采集（Prometheus 格式） |
| `tools/metrics_exporter` | 二进制 | 独立指标导出器（不用 UI 时） |

### 1.5 与同类方案一句话对比

- **Sidekiq（Ruby）/ Celery（Python）**：同类思想的 Go 实现；asynq 无需额外 worker 进程模型，直接嵌入业务进程。
- **River（Postgres）**：事务性入队（与业务同库同事务）；asynq 走 Redis，吞吐高、无业务库耦合，但不具备事务性入队。
- **Machinery**：同为 Go + Redis 任务库，API 与社区活跃度上 asynq 更胜（pkg.go.dev 显示被 2300+ 项目引用）。
- **自研 cron + Redis 锁**：asynq 把"互斥、重试、恢复、可见性"全部产品化，替代手拼。

---

## 二、技术栈与总体架构

### 2.1 技术栈

| 层 | 选型 | 说明 |
|---|---|---|
| 语言 | Go（官方支持最近两个大版本，CI 当前覆盖 1.24/1.25） | 纯库形态，无 cgo |
| Broker | Redis ≥ 4.0 | 单机 / 主从+哨兵 / Cluster 均支持 |
| Redis 客户端 | `github.com/redis/go-redis/v9` | 可传入自建 UniversalClient 复用连接池 |
| cron 解析 | `github.com/robfig/cron/v3` | Scheduler 的 spec 解析（`* * * * *`、`@every` 等） |
| 消息编码 | 内部 protobuf | 任务消息在 Redis 中的序列化格式 |
| 指标 | Prometheus client_golang | `x/metrics` 与 asynqmon 内建 exporter |
| UI | asynqmon（React + Go） | 官方 Web UI |

### 2.2 Redis 部署模式

| 模式 | 连接类型 | 说明 |
|---|---|---|
| 单机 | `RedisClientOpt` | 最简单，生产需做好持久化（见 6.3） |
| 主从 + 哨兵 | `RedisFailoverClientOpt` | 自动故障转移，官方推荐的高可用形态 |
| 集群 | `RedisClusterClientOpt` | **按队列（queue）分片**：同一队列的所有任务落在同一节点；官方提示部分 Lua 脚本在 Cluster 下可能存在兼容性限制，选用前建议验证 |

连接参数也可用 `asynq.ParseRedisURI` 从 URI 解析：支持 `redis://`、`rediss://`（TLS）、`redis-socket://`、`redis-sentinel://` 四种 scheme。

### 2.3 总体架构

```text
                          ┌────────────────────────────────┐
                          │           Redis（Broker）        │
                          │  asynq:{queue}:pending/active/  │
                          │  scheduled/retry/archived/...   │
                          └───────▲──────────▲──────▲───────┘
              Enqueue(任务/延时)   │          │拉取   │周期入队
                   ┌─────────────┘          │      │
             ┌─────┴─────┐          ┌───────┴──┐ ┌─┴──────────┐
             │  Client   │          │  Server  │ │ Scheduler  │
             │(嵌入生产者 │          │(Worker 集群│ │(单实例运行) │
             │  进程)     │          │ 多副本)   │ │ cron 驱动   │
             └───────────┘          └────▲─────┘ └────────────┘
                                         │ Handler 处理任务
                                   ┌─────┴──────┐
                                   │ ServeMux + │
                                   │ Middleware │
                                   └────────────┘
     管理面：Inspector API / asynq CLI / Asynqmon Web UI（读写 Redis）
     观测面：x/metrics → Prometheus → Grafana（或 asynqmon 内建图表）
```

### 2.4 Server 内部组件（源码视角）

`asynq.Server` 启动后运行一组后台 goroutine（对应仓库根目录各源码文件）：

| 组件 | 文件 | 职责 |
|---|---|---|
| processor | `processor.go` | 核心：轮询 pending 队列，原子取出任务到 active，goroutine 池执行 Handler |
| heartbeater | `heartbeat.go` | 周期上报 server 存活信息（host/pid/队列/并发度）到 Redis，并为 active 任务**续租 lease** |
| forwarder | `forwarder.go` | 周期检查 `scheduled`/`retry` 两个 ZSET，把到期任务搬运回 `pending` |
| recoverer | `recoverer.go` | 找回异常任务：lease 过期（worker 崩溃/失联）的任务，标记 `ErrLeaseExpired` 并转 retry 或 archived |
| subscriber | `subscriber.go` | 订阅 Redis pub/sub 的取消信号，实现 `Inspector.CancelProcessing` 主动取消 active 任务 |
| syncer | `syncer.go` | 周期同步队列元数据（如 paused 状态）到进程内存 |
| janitor | `janitor.go` | 清理已过期（超过 Retention）的 completed 任务 |
| aggregator | `aggregator.go` | 实现任务聚合：按 grace period / max delay / max size 把组内任务合成一个 |
| healthchecker | `healthcheck.go` | 周期 ping Redis，异常回调 `Config.HealthCheckFunc` |

理解这组组件是理解 asynq 可靠性语义的钥匙：**processor 负责"跑"，heartbeater 负责"续命"，recoverer 负责"收尸"，forwarder 负责"准点投递"**。

### 2.5 Redis 键空间（内部实现，供运维排查用）

> ⚠️ 以下为 v0.26 的内部实现细节，**非公开 API**，跨版本可能变化。日常排查可用 `redis-cli --scan --pattern 'asynq:*'` 观察。

每个队列在 Redis 中维护一组键（`<q>` 为队列名）：

| 键 | Redis 结构 | 语义 |
|---|---|---|
| `asynq:queues` | SET | 全部队列名注册表 |
| `asynq:{<q>}:pending` | LIST | 待处理任务（入队 RPUSH，消费端原子搬到 active） |
| `asynq:{<q>}:active` | LIST | 正在执行的任务 |
| `asynq:{<q>}:scheduled` | ZSET（score=应执行时刻） | 延时/定时任务，由 forwarder 到期搬运 |
| `asynq:{<q>}:retry` | ZSET（score=下次重试时刻） | 等待重试的任务 |
| `asynq:{<q>}:archived` | ZSET | 重试耗尽归档的死信任务（容量上限 10,000，超出淘汰最旧） |
| `asynq:{<q>}:completed` | ZSET（score=留存到期时刻） | 带 Retention 的已完成任务 |
| `asynq:{<q>}:lease` | ZSET（score=租约到期时刻） | active 任务的执行租约（heartbeater 周期续期） |
| `asynq:{<q>}:deadlines` | ZSET | 任务 Timeout/Deadline 截止时刻登记 |
| `asynq:{<q>}:paused` | STRING | 键存在即该队列被暂停 |
| `asynq:{<q>}:groups` / `asynq:{<q>}:g:<group>` | ZSET | 任务聚合的组索引与组成员 |
| `asynq:{<q>}:processed:<date>` / `failed:<date>` | STRING 计数器 | 按日统计 |
| `asynq:servers:<host:pid:serverID>` | HASH | 存活 server 心跳信息 |
| `asynq:schedulers:<...>` / `asynq:scheduler_history:<entryID>` | HASH / LIST | Scheduler 心跳与每个 entry 的入队历史 |

### 2.6 原子性保障：Lua 脚本

所有关键状态迁移（pending→active 的取出、lease 续期、到期前移、聚合、唯一锁获取等）都由 **Redis Lua 脚本**原子完成，这是 asynq 不依赖分布式锁也能保证"同一任务同一时刻只被一个 worker 持有"的根基——多副本 worker 竞争消费的安全性全部押在 Redis 单线程执行脚本的原子性上。

---

## 三、核心概念与任务生命周期

### 3.1 Task：类型 + 载荷 + 选项

```go
task := asynq.NewTask("email:deliver", payloadJSON) // type + payload([]byte)
task := asynq.NewTaskWithHeaders("email:deliver", payloadJSON,
    map[string]string{"trace_id": "abc123"})        // v0.26 起支持 headers
```

- **Type**（string）：任务类型名，消费端按它路由 Handler，推荐 `域:动作` 命名（如 `email:deliver`、`image:resize`）；
- **Payload**（`[]byte`）：任意字节，官方示例用 JSON（也可 protobuf/gob 等）；
- **Headers**（`map[string]string`，v0.26 新增）：随任务传递的元信息（如 trace id），不影响路由；
- **Options**：创建时或入队时指定处理行为（见 4.1 的选项表）。

### 3.2 七种任务状态与状态机

| 状态 | 含义 |
|---|---|
| Scheduled | 未来才执行（`ProcessAt/ProcessIn` 入队） |
| Pending | 就绪，等待任意空闲 worker 领取 |
| Active | 正在被某个 worker 执行 |
| Retry | 执行失败，等待下次重试时刻 |
| Archived | 重试耗尽（或 SkipRetry），归档待人工处理 |
| Completed | 执行成功且带 `Retention`，留存至 TTL 到期 |
| Aggregating | 已入组，等待聚合（v0.23+） |

```text
+-------------+   到期    +---------+   领取    +--------+   成功    +-----------+
|  Scheduled  │─────────▶│ Pending │─────────▶│ Active │─────────▶│ Completed │
+-------------+          +---------+          +--------+          +-----------+
                              ▲                   │  ▲                  (Retention)
                              │   到期重试         │  │ lease过期
                          +---+----+   失败       │  │ (worker崩溃)
                          │  Retry │◀────────────┘  │
                          +---+----+                ▼
                              │  重试耗尽    +----------+
                              └───────────▶│ Archived │
                                           +----------+
```

成功且无 Retention 的任务立即从 Redis 删除（默认行为）。

### 3.3 交付语义：at-least-once

asynq 保证任务**至少执行一次**，不保证恰好一次。以下场景会造成重复执行，因此 **Handler 必须幂等**：

- worker 处理完成后、向 Redis 回报结果前崩溃（任务被 recoverer 找回重跑）；
- 任务超时被放弃后 Handler 其实仍在跑完；
- 网络分区导致 lease 判定过期。

### 3.4 调度时间基准

- 延时/重试的"到期"判定在 Redis 侧完成（Lua 脚本内基于 Redis 服务器时间），**worker 节点时钟漂移不影响触发正确性**；
- Scheduler 计算 cron 的下一次触发时刻使用调度器进程时钟，因此 **Scheduler 所在机器的时钟与 NTP 同步很重要**；
- Scheduler 支持通过 `SchedulerOpts.Location` 指定时区（默认 UTC）。

---

## 四、功能详解：原理与代码

> 本章每个小节 = 能力描述 → 背后原理 → Go 代码示例。

### 4.1 任务定义与入队（Client）

**能力**：生产者侧创建任务并投递；入队即持久化，Redis 写入延迟低（LIST RPUSH 级别）。

**原理**：`Client.Enqueue` 组装选项（compose options，后者覆盖前者；`NewTask` 时给的选项可被 `Enqueue` 时的选项覆盖），把 protobuf 编码的消息写入目标队列的 `pending` LIST（延时任务写 `scheduled` ZSET），涉及唯一性/TaskID 时通过 Lua 脚本原子完成。

**默认值**：未指定时 MaxRetry = **25**，Timeout = **30 分钟**。

```go
package tasks

import (
    "context"
    "encoding/json"
    "fmt"
    "github.com/hibiken/asynq"
)

const TypeEmailDelivery = "email:deliver"

type EmailDeliveryPayload struct {
    UserID     int    `json:"user_id"`
    TemplateID string `json:"template_id"`
}

// 惯例：为每类任务提供 NewXXXTask 构造函数
func NewEmailDeliveryTask(userID int, tmplID string) (*asynq.Task, error) {
    payload, err := json.Marshal(EmailDeliveryPayload{UserID: userID, TemplateID: tmplID})
    if err != nil {
        return nil, err
    }
    // 也可以在 NewTask 时给默认选项（入队时仍可覆盖）
    return asynq.NewTask(TypeEmailDelivery, payload), nil
}
```

```go
// 生产者侧
client := asynq.NewClient(asynq.RedisClientOpt{Addr: "127.0.0.1:6379"})
defer client.Close()

task, _ := tasks.NewEmailDeliveryTask(42, "tmpl:001")

// ① 立即执行
info, err := client.Enqueue(task)
log.Printf("enqueued: id=%s queue=%s", info.ID, info.Queue)

// ② 需要传上下文（如取消入队操作本身）用 EnqueueContext
info, err = client.EnqueueContext(ctx, task)
```

**任务选项（Option）一览**：

| 选项 | 作用 | 备注 |
|---|---|---|
| `asynq.Queue(name)` | 指定队列 | 默认 `default` |
| `asynq.MaxRetry(n)` | 最大重试次数 | 默认 25 |
| `asynq.Timeout(d)` | 执行超时 | 默认 30min；相对 Handler 开始执行时刻 |
| `asynq.Deadline(t)` | 绝对截止时刻 | 与 Timeout 冲突时取更早者 |
| `asynq.ProcessIn(d)` / `ProcessAt(t)` | 延时/定时执行 | 二者冲突时后传者生效 |
| `asynq.TaskID(id)` | 自定义任务 ID | 同 ID 冲突返回 `ErrTaskIDConflict` |
| `asynq.Unique(ttl)` | 唯一性锁 | TTL ≥ 1s，详见 4.7 |
| `asynq.Retention(d)` | 完成后留存时长 | 配合 ResultWriter 详见 4.13 |
| `asynq.Group(name)` | 聚合分组 | 详见 4.12 |

### 4.2 延时 / 定时任务

**能力**：`ProcessIn(d)`（相对现在）或 `ProcessAt(t)`（绝对时刻）让任务在未来某时刻执行。

**原理**：入队写入 `scheduled` ZSET，score = 目标执行时刻；Server 的 **forwarder** 组件按 `DelayedTaskCheckInterval`（默认 5s，可配）周期扫描，把到期任务原子搬到 `pending`。任务全程持久化在 Redis，进程重启不丢。

```go
// 24 小时后执行
info, err := client.Enqueue(task, asynq.ProcessIn(24*time.Hour))

// 指定绝对时刻（例如站点本地 16:00 换算成的 UTC 时刻）
loc, _ := time.LoadLocation("Europe/Berlin")
at := time.Date(2026, 9, 1, 16, 0, 0, 0, loc)
info, err := client.Enqueue(task, asynq.ProcessAt(at))
```

> 时区提示：`ProcessAt` 接受绝对时刻，跨时区场景请先在业务层把"站点本地时间 + IANA 时区"换算为 `time.Time` 再传入（DST 语义由 Go `time` 包处理），worker 侧与容器时区无关。

### 4.3 任务处理器（Handler）与 ServeMux

**能力**：消费端注册"任务类型 → 处理函数"；`ServeMux` 支持前缀匹配路由，最长模式优先（同 `net/http` 风格）。

**原理**：`Server.Start(handler)` 后，processor 每取到一个任务，用整个任务类型名去 mux 匹配注册的模式串，调用最匹配的 Handler；未命中走 NotFoundHandler（返回 `ErrHandlerNotFound`）。

```go
// Handler 接口只有一个方法：
// type Handler interface { ProcessTask(context.Context, *Task) error }

// 方式一：普通函数 + ServeMux
mux := asynq.NewServeMux()
mux.HandleFunc(tasks.TypeEmailDelivery, handleEmailDelivery)
mux.Handle("email:", defaultEmailHandler) // 前缀兜底：匹配所有 email:* 任务
mux.Use(loggingMiddleware)                // 中间件见 4.14

// 方式二：结构体实现接口（便于携带依赖：DB、RPC client 等）
type EmailProcessor struct{ db *sql.DB }
func (p *EmailProcessor) ProcessTask(ctx context.Context, t *asynq.Task) error {
    var payload tasks.EmailDeliveryPayload
    if err := json.Unmarshal(t.Payload(), &payload); err != nil {
        // payload 非法属于不可恢复错误：包 SkipRetry 直接归档
        return fmt.Errorf("unmarshal failed: %v: %w", err, asynq.SkipRetry)
    }
    // ... 业务逻辑
    return nil // nil = 成功；非 nil = 触发重试
}
mux.Handle(tasks.TypeEmailDelivery, &EmailProcessor{db: db})
```

### 4.4 失败重试

**能力**：Handler 返回非 nil error 或 panic → 任务按退避策略重试；默认最多 25 次；耗尽后归档。

**原理**：processor 捕获 handler 返回值/panic（panic 会被 recover 并转成错误），RetryDelayFunc 计算延迟写入 `retry` ZSET（score=下次重试时刻），forwarder 到期搬回 pending。每次重试 retry 计数 +1。

```go
// ① 入队时控制重试次数
client.Enqueue(task, asynq.MaxRetry(5))

// ② Server 级自定义重试延迟（签名：n=已重试次数, e=错误, t=任务）
srv := asynq.NewServer(redisOpt, asynq.Config{
    Concurrency: 20,
    RetryDelayFunc: func(n int, e error, t *asynq.Task) time.Duration {
        if t.Type() == "foo" {
            return 2 * time.Second // 特定类型固定 2s
        }
        return asynq.DefaultRetryDelayFunc(n, e, t) // 其余走默认指数退避
    },
})

// ③ 非失败错误：不消耗重试次数（如资源暂不可用）
var ErrResourceNotAvailable = errors.New("no resource available")
srv := asynq.NewServer(redisOpt, asynq.Config{
    IsFailure: func(err error) bool {
        return !errors.Is(err, ErrResourceNotAvailable) // 该错误不计失败
    },
})

// ④ 跳过重试直接归档：返回 SkipRetry（可被 wrap）
return fmt.Errorf("bad payload: %w", asynq.SkipRetry)

// ⑤ 既不重试也不归档（v0.25+，如任务已被别处处理）
return asynq.RevokeTask
```

**panic 语义**：handler panic 会被框架 recover，任务按失败处理进入重试；在 `ErrorHandler` 中可用 `asynq.IsPanicError(err)` 识别 panic 类错误单独告警。

### 4.5 死信归档与人工恢复

**能力**：重试耗尽的任务进入 `archived`（死信），不再自动执行；可通过 CLI / Web UI / Inspector 人工"运行、删除、批量处理"。归档集合容量有限（10,000 条），满了淘汰最旧任务——生产上应监控并及时处理死信。

```go
inspector := asynq.NewInspector(redisOpt)
defer inspector.Close()

// 把归档任务重新置为 pending（人工重跑）
inspector.RunTask("default", taskID)

// 批量：全部归档任务重跑 / 删除
inspector.RunAllArchivedTasks("default")
inspector.DeleteAllArchivedTasks("default")
```

CLI 等价操作：`asynq task ls --queue=default --state=archived`、`asynq task run <id> --queue=default`。

### 4.6 超时、截止与取消

**能力**：任务级 `Timeout`（相对执行开始）与 `Deadline`（绝对时刻，与 Timeout 取早者）；到期时 handler 收到的 `ctx` 被取消，**超时未返回的任务会被重试**。active 任务可被人工取消。

**原理**：入队时登记 `deadlines` ZSET；执行时 processor 以 `context.WithTimeout/WithDeadline` 包装传给 handler；人工取消走 Redis pub/sub，由 subscriber 组件接收并向对应 worker 发取消信号（尽力而为 best-effort）。

```go
// 30 秒超时
client.Enqueue(task, asynq.Timeout(30*time.Second))

// 绝对截止：2026-09-01 前必须完成
deadline := time.Date(2026, 9, 1, 0, 0, 0, 0, time.UTC)
client.Enqueue(task, asynq.Deadline(deadline))

// Handler 必须尊重 ctx 取消（用 select 或把 ctx 传给下游调用）
func handle(ctx context.Context, t *asynq.Task) error {
    select {
    case <-ctx.Done():
        return ctx.Err() // 取消信号到达，放弃本次执行 → 触发重试
    case res := <-doWork(t):
        return res
    }
}
```

```bash
# CLI 取消正在执行的任务
asynq task ls --queue=default --state=active
asynq task cancel <task_id>
```

### 4.7 任务唯一性（去重）

**能力**：两种防重复入队机制。

| 机制 | 语义 | 冲突错误 | 适用 |
|---|---|---|---|
| `TaskID(id)` | 同一时刻全系统只允许存在一个该 ID 的任务（强约束，直至任务完成/删除） | `ErrTaskIDConflict` | 业务上有天然唯一键（如订单号、站点号） |
| `Unique(ttl)` | 基于 `(Type, Payload, Queue)` 三元组的 TTL 锁；锁在任务成功处理或 TTL 到期后释放 | `ErrDuplicateTask` | 防短时间重复投递 |

**原理**：TaskID 唯一性由消息 ID 索引保证（入队 Lua 脚本原子检查）；Unique 则尝试 `SET NX EX` 式的唯一锁键，成功才入队。

**注意**：Unique 是 **best-effort**——锁 TTL 过期后即使原任务尚未处理，也可能重复入队；`Unique` 的 TTL 必须 ≥ 1s。

```go
// TaskID 方式
_, err := client.Enqueue(task, asynq.TaskID("site-1001:report:2026-09-01"))
if errors.Is(err, asynq.ErrTaskIDConflict) { /* 已在队列中，忽略 */ }

// Unique 方式：1 小时内同 type+payload+queue 只入队一次
_, err := client.Enqueue(task, asynq.Unique(time.Hour))
if errors.Is(err, asynq.ErrDuplicateTask) { /* 重复，忽略 */ }
```

### 4.8 队列与优先级

**能力**：多队列 + 两种优先级模式；入队时用 `asynq.Queue(name)` 选择队列；队列可运行时暂停/恢复。

**原理**：
- **加权模式**（默认）：processor 按权重随机挑选队列取任务，权重比≈处理时间占比，避免低优先级饿死；
- **严格模式**（`StrictPriority: true`）：高优先级队列清空前不碰低优先级队列（可能饿死低优先级）。

```go
srv := asynq.NewServer(redisOpt, asynq.Config{
    Concurrency: 10,
    Queues: map[string]int{
        "critical": 6, // ≈60% 的处理时间
        "default":  3, // ≈30%
        "low":      1, // ≈10%
    },
    // StrictPriority: true, // 严格模式：critical 清空才轮到 default
})

// 入队到指定队列
client.Enqueue(task, asynq.Queue("critical"))
```

```go
// 暂停/恢复队列（Inspector 或 CLI 或 Web UI）
inspector.PauseQueue("low")
inspector.UnpauseQueue("low")
```

### 4.9 并发控制

**能力**：`Config.Concurrency` 限制**单个 Server 实例**内并发执行的任务数；多实例集群的总并发 = 各实例之和，横向扩容即提高总吞吐。

**原理**：processor 维护 semaphore 大小 = Concurrency，goroutine 池执行；默认值 = 当前进程可用 CPU 数。注意：这是"同时执行的任务数"上限，与队列深度无关；配额应参照下游依赖（DB 连接池、目标 API 限速）反推。

```go
srv := asynq.NewServer(redisOpt, asynq.Config{
    Concurrency: 20, // 每实例最多 20 个任务同时执行
})
```

### 4.10 周期任务（Scheduler）

**能力**：cron 表达式或 `@every <duration>` 周期性入队任务。

**原理**：Scheduler 进程内为每个 entry 计算 next 触发时刻，到点调用内部 Client 入队（即周期任务与普通任务共用同一执行面）。**Scheduler 没有内置选主**——同一套 entry 只能运行一个 Scheduler 实例，否则重复入队；部署上用单副本 + 每周期 `TaskID` 兜底（官方 wiki 明确要求"ensure only a single scheduler is running"）。

```go
scheduler := asynq.NewScheduler(redisOpt, &asynq.SchedulerOpts{
    Location: loc, // 时区，默认 UTC
    // 入队前后钩子（PostEnqueueFunc 为 v0.24 推荐，EnqueueErrorHandler 已废弃）
    PostEnqueueFunc: func(info *asynq.TaskInfo, err error) {
        if err != nil { log.Printf("scheduler enqueue failed: %v", err) }
    },
})

task, _ := tasks.NewEmailDeliveryTask(42, "tmpl:daily")

// cron spec（5 段式，分钟粒度）
entryID, err := scheduler.Register("0 6 * * *", task, asynq.Queue("critical"))

// 间隔式
entryID, err = scheduler.Register("@every 30s", task)

// 运行：阻塞直至收到 TERM/INT 信号
if err := scheduler.Run(); err != nil { log.Fatal(err) }

// 运行中动态注销
scheduler.Unregister(entryID)
```

**时区**：`SchedulerOpts.Location` 是 **Scheduler 实例级**时区（所有 entry 共用）；默认 UTC。若需要 per-entry 时区（同一调度器内多时区站点任务），需业务层自行换算为绝对时刻入队，或每时区一个 Scheduler 实例（此为常见封装层做法，官方未提供 per-entry 时区 API）。

### 4.11 动态周期任务（PeriodicTaskManager）

**能力**：运行时增删改周期任务而无需重启 Scheduler；配置源可以是 DB、文件、配置中心。

**原理**：`PeriodicTaskManager`（v0.21+，官方推荐优先于裸用 Scheduler）周期调用你实现的 `PeriodicTaskConfigProvider.GetConfigs()`，将 Scheduler entries 与返回的配置集 **diff 同步**（新增的 Register、消失的 Unregister）。

```go
// ① 实现配置提供者（示例：YAML 文件，生产可换成 DB 查询）
type FileConfigProvider struct{ filename string }

func (p *FileConfigProvider) GetConfigs() ([]*asynq.PeriodicTaskConfig, error) {
    data, err := os.ReadFile(p.filename)
    if err != nil { return nil, err }
    var c struct {
        Configs []struct {
            Cronspec string `yaml:"cronspec"`
            TaskType string `yaml:"task_type"`
        } `yaml:"configs"`
    }
    if err := yaml.Unmarshal(data, &c); err != nil { return nil, err }
    var configs []*asynq.PeriodicTaskConfig
    for _, cfg := range c.Configs {
        configs = append(configs, &asynq.PeriodicTaskConfig{
            Cronspec: cfg.Cronspec,
            Task:     asynq.NewTask(cfg.TaskType, nil),
        })
    }
    return configs, nil
}

// ② 启动 manager：每 10s 与配置源同步一次
mgr, err := asynq.NewPeriodicTaskManager(asynq.PeriodicTaskManagerOpts{
    RedisConnOpt:            redisOpt,
    PeriodicTaskConfigProvider: &FileConfigProvider{filename: "periodic.yml"},
    SyncInterval:            10 * time.Second,
})
if err != nil { log.Fatal(err) }
if err := mgr.Run(); err != nil { log.Fatal(err) }
```

DB 模式提示：将任务注册表落库作为事实源，`GetConfigs` 读表，管理接口写表——这是"动态任务注册中心"的官方推荐骨架。

### 4.12 任务聚合（Groups，v0.23+）

**能力**：同一 `(queue, group)` 的多个任务在服务端**合并为一个**再交给 Handler——用于批量通知、合并写库、削峰省钱。

**原理**：入队时 `Group` 选项把任务写入组 ZSET；aggregator 组件监视各组：每当组内有新任务则**续展宽限期**（grace period）；宽限期累计不超过最大延迟（max delay）；组内数量达到 max size 立即聚合。`GroupAggregator` 把 `[]*Task` 合成一个新任务（总是入回原队列）。注意：**调度（ProcessAt/In）与聚合冲突时，调度静默优先**。

```go
// 客户端：同一组投三个任务
client.Enqueue(t1, asynq.Queue("notifications"), asynq.Group("user:1001:email"))
client.Enqueue(t2, asynq.Queue("notifications"), asynq.Group("user:1001:email"))
client.Enqueue(t3, asynq.Queue("notifications"), asynq.Group("user:1001:email"))

// 服务端
func aggregate(group string, tasks []*asynq.Task) *asynq.Task {
    var b strings.Builder
    for _, t := range tasks {
        b.Write(t.Payload()); b.WriteString("\n")
    }
    return asynq.NewTask("notifications:batch", []byte(b.String()))
}

srv := asynq.NewServer(redisOpt, asynq.Config{
    Queues:          map[string]int{"notifications": 1},
    GroupAggregator: asynq.GroupAggregatorFunc(aggregate),
    GroupGracePeriod: 2 * time.Minute, // 收到新任务后再等 2min（可续展）
    GroupMaxDelay:   10 * time.Minute, // 最长攒 10min 必须聚合
    GroupMaxSize:    20,               // 组满 20 个立即聚合
})
```

### 4.13 任务留存与结果（Retention / ResultWriter）

**能力**：默认成功即删；`Retention(d)` 让完成任务留存 d 时长（期间可在 UI/CLI 看到 completed 态）；Handler 内用 `ResultWriter` 把处理结果写回 Redis 与任务关联，事后可编程查询。

**原理**：completed ZSET score=到期时刻，janitor 周期清理过期项。ResultWriter 数据随任务删除一起消失——**只适合小结果、任务生命周期内使用**，大结果请落 DB/对象存储。

```go
// 入队：完成后留存 24h
client.Enqueue(task, asynq.Retention(24*time.Hour))

// Handler 内写结果
func handle(ctx context.Context, t *asynq.Task) error {
    res, err := doWork(ctx, t)
    if err != nil {
        return fmt.Errorf("process failed: %v", err)
    }
    if _, err := t.ResultWriter().Write(res); err != nil {
        return fmt.Errorf("write result failed: %v", err)
    }
    return nil
}

// 事后查询（Inspector）
info, _ := inspector.GetTaskInfo("default", taskID) // info.Result 为结果数据
completed, _ := inspector.ListCompletedTasks("default", asynq.PageSize(100))
```

### 4.14 中间件（Middleware）

**能力**：`MiddlewareFunc = func(Handler) Handler`，装饰任务处理；`ServeMux.Use` 按注册顺序执行。可嵌套多个 mux 实现"按任务前缀分组套不同中间件"。

```go
func loggingMiddleware(h asynq.Handler) asynq.Handler {
    return asynq.HandlerFunc(func(ctx context.Context, t *asynq.Task) error {
        start := time.Now()
        log.Printf("start %q", t.Type())
        err := h.ProcessTask(ctx, t)
        log.Printf("finished %q elapsed=%v err=%v", t.Type(), time.Since(start), err)
        return err
    })
}

mux := asynq.NewServeMux()
mux.Use(loggingMiddleware, metricsMiddleware)

// 分组中间件：product:* 与 order:* 各挂各的
productMux := asynq.NewServeMux(); productMux.Use(productMW); productMux.HandleFunc("product:update", h1)
orderMux := asynq.NewServeMux(); orderMux.Use(orderMW); orderMux.HandleFunc("order:refund", h2)
top := asynq.NewServeMux(); top.Use(globalMW)
top.Handle("product:", productMux)
top.Handle("order:", orderMux)
```

Prometheus 指标中间件示例（processed/failed/in-progress 按 task_type 统计）见官方 wiki Monitoring and Alerting 页，核心：

```go
func metricsMiddleware(next asynq.Handler) asynq.Handler {
    return asynq.HandlerFunc(func(ctx context.Context, t *asynq.Task) error {
        inProgressGauge.WithLabelValues(t.Type()).Inc()
        err := next.ProcessTask(ctx, t)
        inProgressGauge.WithLabelValues(t.Type()).Dec()
        if err != nil { failedCounter.WithLabelValues(t.Type()).Inc() }
        processedCounter.WithLabelValues(t.Type()).Inc()
        return err
    })
}
```

### 4.15 错误处理与告警（ErrorHandler）

**能力**：每次 Handler 失败（含 panic 转错误、lease 过期 `ErrLeaseExpired`）回调 `Config.ErrorHandler`，是接告警的官方挂点。配套 context 辅助函数可拿到任务元数据。

```go
srv := asynq.NewServer(redisOpt, asynq.Config{
    ErrorHandler: asynq.ErrorHandlerFunc(func(ctx context.Context, t *asynq.Task, err error) {
        retried, _ := asynq.GetRetryCount(ctx)  // 已重试次数
        maxRetry, _ := asynq.GetMaxRetry(ctx)   // 上限
        taskID, _ := asynq.GetTaskID(ctx)       // 任务 ID（重试间不变）
        queue, _ := asynq.GetQueueName(ctx)     // 队列名

        switch {
        case asynq.IsPanicError(err):
            // panic 类错误：立即告警
            alertPanic(taskID, t.Type(), err)
        case retried >= maxRetry:
            // 重试耗尽：死信告警
            alertDeadLetter(taskID, t.Type(), err)
        default:
            log.Printf("task %s failed (retry %d/%d): %v", taskID, retried, maxRetry, err)
        }
    }),
})
```

### 4.16 Inspector：编程式管理 API

**能力**：完整覆盖 CLI/UI 的管理操作。常用方法分组：

| 分组 | 方法 |
|---|---|
| 队列 | `Queues()`、`GetQueueInfo(q)`、`History(q, n)`、`PauseQueue/UnpauseQueue`、`DeleteQueue(q, force)` |
| 任务查询 | `GetTaskInfo(q, id)`、`ListPending/Scheduled/Retry/Archived/Completed/Active/AggregatingTasks`（分页 `Page/PageSize`，默认 30 条） |
| 任务操作 | `RunTask`（置为 pending）、`DeleteTask`、`ArchiveTask`、`UpdateTaskPayload`（仅 scheduled 态，v0.26）、`CancelProcessing`（取消 active，尽力而为）、`RunAll*/DeleteAll*/ArchiveAll*` 批量族 |
| 集群观测 | `Servers()`（在线 worker 列表）、`SchedulerEntries()`、`ListSchedulerEnqueueEvents(entryID)`、`Groups(q)` |
| Cluster | `ClusterKeySlot(q)`、`ClusterNodes(q)` |

```go
insp := asynq.NewInspector(redisOpt)
defer insp.Close()

info, err := insp.GetQueueInfo("default")
// info.Queue, info.Pending, info.Active, info.Scheduled, info.Retry,
// info.Archived, info.Completed, info.Processed, info.Failed, info.Paused ...

servers, _ := insp.Servers()       // 正在运行的 worker 实例
entries, _ := insp.SchedulerEntries() // 在运行的周期任务 entry
```

### 4.17 优雅停机

**能力**：`Run()` 阻塞至 TERM/INT 并自动优雅停机；手动托管时 `Start()` + `Stop()` + `Shutdown()` 分步控制。

**语义**：
- `Stop()`：停止拉取新任务，等待（但未强制）在执行任务完成；
- `Shutdown()`：等待 active 任务完成，超过 `ShutdownTimeout`（默认 **8s**）的未完成任务**推回 Redis**（稍后被重试/其他实例接管）——发布滚动不丢任务。

```go
// 手动托管（需自己控制生命周期时）
if err := srv.Start(mux); err != nil { log.Fatal(err) }

// 退出流程
<-shutdownSignal
srv.Stop()      // 先停止接新任务
srv.Shutdown()  // 再等 in-flight 完成（超时回队）
```

### 4.18 限速模式（Rate Limiting）

**能力**：官方模式 = 自定义错误类型 + `IsFailure` 不计失败 + `RetryDelayFunc` 指定稍后重试时间。注意这是**单 Server 实例级**限速，全局限速需自建（如 Redis 令牌桶中间件）。

```go
type RateLimitError struct{ RetryIn time.Duration }
func (e *RateLimitError) Error() string { return fmt.Sprintf("rate limited (retry in %v)", e.RetryIn) }

var limiter = rate.NewLimiter(10, 30) // 10 QPS，突发 30（golang.org/x/time/rate）

srv := asynq.NewServer(redisOpt, asynq.Config{
    IsFailure: func(err error) bool { return !isRateLimitError(err) },
    RetryDelayFunc: func(n int, err error, t *asynq.Task) time.Duration {
        var rle *RateLimitError
        if errors.As(err, &rle) { return rle.RetryIn }
        return asynq.DefaultRetryDelayFunc(n, err, t)
    },
})

func handler(ctx context.Context, t *asynq.Task) error {
    if !limiter.Allow() {
        return &RateLimitError{RetryIn: time.Duration(rand.Intn(10)) * time.Second}
    }
    // ...
    return nil
}
```

### 4.19 多 Redis 拓扑与连接复用

```go
// ① 单机
opt := asynq.RedisClientOpt{Addr: "127.0.0.1:6379", Password: "xxx", DB: 2}

// ② 哨兵（自动故障转移）
opt := asynq.RedisFailoverClientOpt{
    MasterName:    "mymaster",
    SentinelAddrs: []string{"s1:5000", "s2:5001", "s3:5002"},
}

// ③ 集群（按队列分片到不同节点）
opt := asynq.RedisClusterClientOpt{Addrs: []string{":7000", ":7001", ":7002"}}

// ④ 从 URI 解析（redis:// rediss:// redis-socket:// redis-sentinel://）
opt, err := asynq.ParseRedisURI("redis-sentinel://:pass@h1:5000,h2:5001?master=mymaster")

// ⑤ 复用业务已有的 redis 客户端连接池（asynq 不负责关闭它）
client := asynq.NewClientFromRedisClient(myUniversalClient)
srv := asynq.NewServerFromRedisClient(myUniversalClient, cfg)
```

集群注意：asynq 以**队列名**做分片单位（同队列任务同节点），`redis-cli --cluster reshard` 迁移 slot 期间涉及多键操作的 asynq 命令可能短暂不可用；README 明确提示部分 Lua 脚本与 Redis Cluster 存在兼容性限制，选用前先验证。

---

## 五、完整可运行示例

### 5.1 工程结构与 tasks 包

```text
demo/
├── tasks/
│   └── tasks.go      // 任务定义 + Handler（生产者消费者共用）
├── client/main.go    // 生产者
└── worker/main.go    // 消费者（含周期任务调度）
```

```go
// tasks/tasks.go
package tasks

import (
    "context"
    "encoding/json"
    "fmt"
    "log"
    "time"

    "github.com/hibiken/asynq"
)

const (
    TypeEmailDelivery = "email:deliver"
    TypeImageResize   = "image:resize"
)

type EmailDeliveryPayload struct {
    UserID     int    `json:"user_id"`
    TemplateID string `json:"template_id"`
}

func NewEmailDeliveryTask(userID int, tmplID string) (*asynq.Task, error) {
    payload, err := json.Marshal(EmailDeliveryPayload{UserID: userID, TemplateID: tmplID})
    if err != nil {
        return nil, err
    }
    return asynq.NewTask(TypeEmailDelivery, payload), nil
}

func HandleEmailDeliveryTask(ctx context.Context, t *asynq.Task) error {
    var p EmailDeliveryPayload
    if err := json.Unmarshal(t.Payload(), &p); err != nil {
        return fmt.Errorf("json.Unmarshal failed: %v: %w", err, asynq.SkipRetry)
    }
    log.Printf("Sending email to user=%d template=%s", p.UserID, p.TemplateID)
    // ... 实际发信逻辑（ctx 需传入以响应取消）
    return nil
}
```

### 5.2 生产者

```go
// client/main.go
package main

import (
    "log"
    "time"

    "github.com/hibiken/asynq"
    "demo/tasks"
)

func main() {
    client := asynq.NewClient(asynq.RedisClientOpt{Addr: "127.0.0.1:6379"})
    defer client.Close()

    task, err := tasks.NewEmailDeliveryTask(42, "welcome:001")
    if err != nil {
        log.Fatalf("create task: %v", err)
    }

    // 立即执行，关键队列，最多重试 5 次，60s 超时
    info, err := client.Enqueue(task,
        asynq.Queue("critical"),
        asynq.MaxRetry(5),
        asynq.Timeout(60*time.Second),
    )
    if err != nil {
        log.Fatalf("enqueue: %v", err)
    }
    log.Printf("enqueued: id=%s queue=%s", info.ID, info.Queue)
}
```

### 5.3 消费者（Server + Scheduler 一体）

```go
// worker/main.go
package main

import (
    "log"
    "time"

    "github.com/hibiken/asynq"
    "demo/tasks"
)

func main() {
    redisOpt := asynq.RedisClientOpt{Addr: "127.0.0.1:6379"}

    // ---------- Worker Server ----------
    srv := asynq.NewServer(redisOpt, asynq.Config{
        Concurrency: 10,
        Queues: map[string]int{
            "critical": 6,
            "default":  3,
            "low":      1,
        },
        RetryDelayFunc: asynq.DefaultRetryDelayFunc,
        ErrorHandler: asynq.ErrorHandlerFunc(func(ctx context.Context, t *asynq.Task, err error) {
            log.Printf("task %s failed: %v", t.Type(), err)
        }),
    })

    mux := asynq.NewServeMux()
    mux.HandleFunc(tasks.TypeEmailDelivery, tasks.HandleEmailDeliveryTask)
    // mux.HandleFunc(tasks.TypeImageResize, tasks.HandleImageResizeTask)

    // ---------- Scheduler（单实例运行！） ----------
    scheduler := asynq.NewScheduler(redisOpt, &asynq.SchedulerOpts{
        Location: time.Local, // 生产建议显式指定，如 time.UTC 或站点时区
    })
    dailyTask, _ := tasks.NewEmailDeliveryTask(0, "digest:daily")
    if _, err := scheduler.Register("0 6 * * *", dailyTask, asynq.Queue("default")); err != nil {
        log.Fatalf("register cron: %v", err)
    }

    // 先起 scheduler（非阻塞），再阻塞跑 server
    go func() {
        if err := scheduler.Run(); err != nil {
            log.Fatalf("scheduler: %v", err)
        }
    }()

    if err := srv.Run(mux); err != nil {
        log.Fatalf("server: %v", err)
    }
}
```

运行：

```bash
# 终端 1：worker（含调度器）
go run ./worker

# 终端 2：投递任务
go run ./client

# 终端 3：观察
asynq stats          # CLI（见 6.6）
# 或打开 Asynqmon： http://localhost:8080
```

---

## 六、部署指南（含 Asynqmon UI）

### 6.1 环境要求

| 组件 | 要求 |
|---|---|
| Go | 官方支持最近两个大版本（当前 1.24/1.25） |
| Redis | ≥ 4.0（README 要求）；生产建议 6.x/7.x 稳定版 |
| asynqmon 版本 | 与 asynq 版本存在兼容矩阵（见 6.5） |

### 6.2 安装

```bash
# 核心库
go get github.com/hibiken/asynq

# CLI 工具（可选）
go install github.com/hibiken/asynq/tools/asynq@latest
```

### 6.3 Redis 生产配置要点

**单机 Redis 是 asynq 可靠性的第一道闸门**，以下为硬性建议：

```conf
# redis.conf 关键项
appendonly yes              # 必须：开 AOF，否则重启丢全部任务
appendfsync everysec        # 每秒刷盘（性能与安全的平衡点）
save 900 1                  # RDB 快照作为二级保障
maxmemory <留足余量>
maxmemory-policy noeviction # 关键！禁止淘汰：asynq 的键是任务数据，
                            # 被淘汰 = 任务静默丢失；内存不足宁可写入报错
```

- 建议为任务系统使用**独立 DB 编号或独立实例**，与缓存类负载隔离（缓存可 LRU 淘汰，任务键绝不可）；
- 单机部署时容量规划：任务消息 KB 级，10 万在途任务约几十 MB 量级，但需计入留存（Retention）与归档占用；
- 升级为哨兵/集群前先读 4.19 与官方 Redis-Cluster wiki。

### 6.4 进程部署形态

| 组件 | 副本建议 | 说明 |
|---|---|---|
| Client | 随业务进程 | 只是入队 API，无状态 |
| Server（Worker） | 多副本 | 队列竞争消费天然互斥；扩容=加副本，与副本数解耦 |
| Scheduler | **单实例** | 无内置选主，多实例会重复入队；k8s 上用 `replicas: 1` 的 Deployment；兜底手段：周期任务统一带 `TaskID`（如 `cron:<entry>:<timestamp>` 粒度）使重复入队被唯一性约束拒绝 |
| PeriodicTaskManager | **单实例** | 同 Scheduler（内部管理一个 Scheduler） |

**k8s 优雅停机配合**（worker 发布滚动不丢任务）：

```yaml
spec:
  terminationGracePeriodSeconds: 30   # > Config.ShutdownTimeout（默认 8s）
  containers:
  - name: worker
    lifecycle:
      preStop:
        exec:
          command: ["sh", "-c", "sleep 5"]   # 先摘流，让 LB/informer 收敛
    readinessProbe:                          # 用 asynq Server.Ping() 暴露健康端点
      httpGet: { path: /healthz, port: 8080 }
```

### 6.5 Asynqmon（Web UI）部署

Asynqmon 既是**可执行文件/Docker 镜像**，也是**可嵌入的 Go 库**。默认监听 `:8080`，连接 `127.0.0.1:6379`。

**官方版本兼容矩阵**：

| asynq 版本 | asynqmon 版本 |
|---|---|
| 0.23.x | 0.7.x |
| 0.22.x | 0.6.x |
| 0.20.x / 0.21.x | 0.5.x |
| 0.19.x | 0.4.x |
| 0.18.x | 0.2.x / 0.3.x |
| 0.16.x / 0.17.x | 0.1.x |

> 注意：官方矩阵未覆盖 asynq 0.24+（asynqmon 最新版 v0.7.1 已 3 年未发版）。社区普遍反馈 v0.7.x 与 0.24~0.26 仍兼容（Redis 键结构未破坏性变更，headers 等新特性不展示但不影响使用），生产使用前请实测验证；也有社区 fork 提供适配。

**方式一：Docker（推荐）**

```bash
docker pull hibiken/asynqmon

docker run --rm --name asynqmon \
  -p 8080:8080 \
  hibiken/asynqmon \
  --redis-addr=redis-host:6379 --redis-password=secret
```

docker-compose 示例（本地开发全套）：

```yaml
version: "3"
services:
  redis:
    image: redis:7-alpine
    command: redis-server --appendonly yes
    ports: ["6379:6379"]
  asynqmon:
    image: hibiken/asynqmon
    ports: ["8080:8080"]
    environment:
      - REDIS_ADDR=redis:6379
      - ENABLE_METRICS_EXPORTER=true
    depends_on: [redis]
```

**方式二：k8s Deployment**

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: asynqmon
spec:
  replicas: 1
  selector: { matchLabels: { app: asynqmon } }
  template:
    metadata: { labels: { app: asynqmon } }
    spec:
      containers:
      - name: asynqmon
        image: hibiken/asynqmon:latest
        args:
          - --redis-addr=redis.infra.svc:6379
          - --redis-password=$(REDIS_PASSWORD)
          - --read-only=false
          - --enable-metrics-exporter
          - --prometheus-addr=http://prometheus.monitoring:9090
        env:
        - name: REDIS_PASSWORD
          valueFrom: { secretKeyRef: { name: redis-auth, key: password } }
        ports: [{ containerPort: 8080 }]
        resources:
          requests: { cpu: 50m, memory: 64Mi }
          limits: { cpu: 200m, memory: 128Mi }
---
apiVersion: v1
kind: Service
metadata: { name: asynqmon }
spec:
  selector: { app: asynqmon }
  ports: [{ port: 8080, targetPort: 8080 }]
# 生产：再配 Ingress + 统一鉴权（或 Service 内网 only）
```

**方式三：作为库嵌入已有 Web 服务**（复用现有鉴权/路由/域名）

```go
package main

import (
    "log"
    "net/http"

    "github.com/hibiken/asynq"
    "github.com/hibiken/asynqmon"
)

func main() {
    h := asynqmon.New(asynqmon.Options{
        RootPath:     "/monitoring", // 挂载子路径
        RedisConnOpt: asynq.RedisClientOpt{Addr: ":6379"},
    })
    // 注意 net/http.ServeMux 注册需带尾斜杠
    http.Handle(h.RootPath()+"/", h)
    log.Fatal(http.ListenAndServe(":8080", nil))
}
```

**完整 flags / 环境变量表**：

| Flag | Env | 说明 | 默认 |
|---|---|---|---|
| `--port` | `PORT` | Web 监听端口 | 8080 |
| `--redis-url` | `REDIS_URL` | Redis/sentinel URL（`redis://`、`redis-sentinel://...?master=x`） | "" |
| `--redis-addr` | `REDIS_ADDR` | Redis 地址 | 127.0.0.1:6379 |
| `--redis-db` | `REDIS_DB` | DB 编号 | 0 |
| `--redis-password` | `REDIS_PASSWORD` | 密码 | "" |
| `--redis-cluster-nodes` | `REDIS_CLUSTER_NODES` | 集群节点列表（逗号分隔） | "" |
| `--redis-tls` | `REDIS_TLS` | TLS 校验的 server name | "" |
| `--redis-insecure-tls` | `REDIS_INSECURE_TLS` | 跳过 TLS 主机名校验 | false |
| `--enable-metrics-exporter` | `ENABLE_METRICS_EXPORTER` | 暴露 `/metrics`（Prometheus 抓取） | false |
| `--prometheus-addr` | `PROMETHEUS_ADDR` | Prometheus 查询地址（UI 内显示时序图） | "" |
| `--read-only` | `READ_ONLY` | 只读模式（禁用 UI 上的管理操作） | false |

三种连接拓扑示例：

```bash
./asynqmon --redis-url=redis://:pass@localhost:6380/2
./asynqmon --redis-url=redis-sentinel://:pass@h1:5000,h2:5001?master=mymaster
./asynqmon --redis-cluster-nodes=h1:7000,h2:7001,h3:7002
```

**UI 功能**：Queues 视图（各状态数量、速率）、Tasks 视图（按状态筛选、payload/错误详情、重试历史、删除/归档/运行操作）、Scheduler entries、在线 Workers、Redis 信息、Prometheus 指标图表、暗色模式、只读模式。

### 6.6 CLI 工具（asynq）

```bash
go install github.com/hibiken/asynq/tools/asynq@latest
```

| 命令 | 作用 |
|---|---|
| `asynq dash` | 终端面板（TUI） |
| `asynq stats [--json]` | 队列统计快照 |
| `asynq queue ls / inspect / history / rm / pause / unpause` | 队列管理 |
| `asynq task ls / cancel / delete / archive / run / deleteall / archiveall / runall` | 任务管理 |
| `asynq server ls` | 在线 worker 列表 |
| `asynq cron ls` / `asynq cron history <entryID>` | 周期任务 entry 与入队历史 |

常用全局 flags：`-u/--uri`（Redis 地址）、`-n/--db`、`-p/--password`、`-U/--username`（ACL）、`--cluster` + `--cluster_addrs`（集群）；配置文件默认读 `$HOME/.asynq.yaml`（可写 `uri/db/password` 设默认值）。

示例：

```bash
asynq stats -u redis.prod:6379 -p secret
asynq task ls --queue=default --state=archived
asynq task run <task_id> --queue=default   # 死信重跑
asynq queue pause low
```

### 6.7 Prometheus 监控

两条路线：

1. **asynqmon 内建**：启动时 `--enable-metrics-exporter`（暴露 `/metrics` 供 Prometheus 抓取）+ `--prometheus-addr`（UI 内直接画时序图）；
2. **独立导出器**（不用 UI 时）：运行 `tools/metrics_exporter` 二进制，或引用 `x/metrics` 包自建 exporter；
3. Worker 侧业务指标用 4.14 的中间件埋点（processed/failed/in-progress by task_type）。

告警建议项：队列 pending 深度持续增长（消费能力不足）、archived 增长速率（死信暴增）、retry 堆积、server 心跳消失、Redis 内存/淘汰策略告警。

### 6.8 安全清单

- Redis 启用密码或 ACL（`--username`/`-U` 用于 ACL 用户，v0.26 CLI 已支持）；
- asynqmon 端口**不要裸露公网**：内网 Service / Ingress 加鉴权 / `--read-only` 只读模式三选一起步；
- TLS：`RedisClientOpt.TLSConfig` / asynqmon `--redis-tls`；
- UI 上能看到任务 payload——payload 里不要放明文敏感信息（密码、token）。

---

## 七、最佳实践与常见陷阱

### 7.1 最佳实践

1. **Handler 必须幂等**（at-least-once 语义的前提）：用 TaskID/业务唯一键做去重护栏，写操作带条件更新。
2. **任务类型命名用 `域:动作`**，配合 ServeMux 前缀路由与分组中间件。
3. **payload 尽量小**（KB 级）：传 ID 引用而非整块数据；大结果落外部存储，ResultWriter 只存小结果。
4. **每类任务显式设置 MaxRetry / Timeout / Queue**，在 `NewTask` 给默认值、入队处按场景覆盖——不要依赖框架默认（25 次/30min）。
5. **Handler 尊重 ctx**：所有长操作 `select ctx.Done()` 或把 ctx 传给下游（HTTP/SQL），否则 Timeout 形同虚设。
6. **周期任务带 TaskID**：既防 Scheduler 意外双实例重复入队，也便于在 UI 追踪。
7. **配置源 + PeriodicTaskManager** 做动态任务注册中心，注册表落 DB，变更走 reconcile。
8. **告警接 ErrorHandler**：panic 类与重试耗尽类单独分级告警。
9. **队列规划**：固定少量业务队列（critical/default/low 或按域），**不要**按租户/用户动态建无限多队列（每个队列都是一组 Redis 键 + 每实例都要轮询）。
10. **压测定容量**：Concurrency 按下游承受力反推；pending 深度是消费能力不足的第一信号。

### 7.2 常见陷阱

| 陷阱 | 后果 | 对策 |
|---|---|---|
| 忘记开 AOF / 用了可淘汰策略（allkeys-lru） | Redis 重启/内存压力下**任务静默丢失** | 6.3 的 Redis 配置清单 |
| 多实例跑 Scheduler | 同一周期任务**重复入队** | 单副本部署 + TaskID 兜底 |
| Handler 内 `time.Sleep` 大量轮询而不看 ctx | 超时/取消不生效，worker 被 IO 挂死 | ctx 传播 + select |
| 把 Unique 当强去重 | TTL 过期后仍可能重复（best-effort） | 强语义用 TaskID |
| SkipRetry 滥用 | 本可自愈的瞬时错误直接进死信 | 只对不可恢复错误（payload 非法等）用 SkipRetry |
| 严格优先级 + 低优先级队列常年堆积 | 低优先级任务饿死 | 默认用加权模式 |
| 任务聚合 + 延时同时使用 | 调度静默优先，聚合不生效 | 二者不要混用（官方明确） |
| 忽视 archived 上限（10k 淘汰最旧） | 死信被静默挤掉 | 监控 archived 深度并定期处理 |
| 在 Redis Cluster 上未验证就上生产 | 部分 Lua 脚本兼容性问题 | 先按业务场景验证（官方 README 提示） |
| 依赖 v0.x API 永久稳定 | 升级 0.x 小版本可能有破坏性变更 | 锁 go.mod 版本；升级读 CHANGELOG |
| Scheduler 用 `time.Local` 且容器 TZ 与预期不符 | 周期任务整体偏移 | `SchedulerOpts.Location` 显式指定 |

---

## 八、版本演进速览

| 版本 | 日期 | 关键变化 |
|---|---|---|
| v0.26.0 | 2026-02 | 任务 Headers 支持（`NewTaskWithHeaders`）；Inspector `UpdateTaskPayload`；CLI `--username`（ACL）|
| v0.25.0 | 2024-10 | `IsPanicError`、`RevokeTask`、`Ping` 方法、`TaskCheckInterval`、`NewClientFromRedisClient`、Go ≥1.22 |
| v0.24.0 | 2023-01 | Scheduler `PreEnqueueFunc/PostEnqueueFunc`（替代 `EnqueueErrorHandler`） |
| v0.23.0 | 2022-04 | **任务聚合**（Group/GroupAggregator 及三个组配置） |
| v0.22.0 | 2022-02 | **lease 过期恢复**（`ErrLeaseExpired`）；`BaseContext`；`IsOrphaned` |
| v0.21.0 | 2022-01 | **PeriodicTaskManager**（动态周期任务） |
| v0.20.0 | 2021-12 | `x/metrics` 包与 metrics_exporter 工具 |
| v0.19.0 | 2021-11 | API 大改（Config 结构、ServeMux 等），升级需跑 `asynq migrate` |
| v0.18.0 | 2021-06 | 任务消息编码更换，需 CLI 迁移 |
| v0.17.0 ~ | 2021 | Retention/Completed 状态、Inspector 扩充等 |

> master 分支已合入 **batch enqueue**（批量入队）等新特性，尚未随正式版本发布；跟进时关注 CHANGELOG 的 Unreleased 段。

---

## 九、参考链接

| 资源 | 链接 |
|---|---|
| asynq 仓库 | https://github.com/hibiken/asynq |
| asynq godoc | https://pkg.go.dev/github.com/hibiken/asynq |
| Wiki：Getting Started | https://github.com/hibiken/asynq/wiki/Getting-Started |
| Wiki：Life of a Task | https://github.com/hibiken/asynq/wiki/Life-of-a-Task |
| Wiki：Task Retry | https://github.com/hibiken/asynq/wiki/Task-Retry |
| Wiki：Queue Priority | https://github.com/hibiken/asynq/wiki/Queue-Priority |
| Wiki：Unique Tasks | https://github.com/hibiken/asynq/wiki/Unique-Tasks |
| Wiki：Periodic Tasks | https://github.com/hibiken/asynq/wiki/Periodic-Tasks |
| Wiki：Dynamic Periodic Task | https://github.com/hibiken/asynq/wiki/Dynamic-Periodic-Task |
| Wiki：Task Aggregation | https://github.com/hibiken/asynq/wiki/Task-aggregation |
| Wiki：Task Retention and Result | https://github.com/hibiken/asynq/wiki/Task-Retention-and-Result |
| Wiki：Timeout, Cancelation | https://github.com/hibiken/asynq/wiki/Task-Timeout-and-Cancelation |
| Wiki：Handler Deep Dive | https://github.com/hibiken/asynq/wiki/Handler-Deep-Dive |
| Wiki：Rate Limiting | https://github.com/hibiken/asynq/wiki/Rate-Limiting |
| Wiki：Automatic Failover（Sentinel） | https://github.com/hibiken/asynq/wiki/Automatic-Failover |
| Wiki：Redis Cluster | https://github.com/hibiken/asynq/wiki/Redis-Cluster |
| Wiki：Monitoring and Alerting | https://github.com/hibiken/asynq/wiki/Monitoring-and-Alerting |
| Asynqmon（Web UI） | https://github.com/hibiken/asynqmon |
| asynq CLI | https://github.com/hibiken/asynq/tree/master/tools/asynq |
| CHANGELOG | https://github.com/hibiken/asynq/blob/master/CHANGELOG.md |

---

*本文档基于 asynq v0.26.0 官方资料整理；内部实现细节（Redis 键空间等）以仓库源码为准。*
