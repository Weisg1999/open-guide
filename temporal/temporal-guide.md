# Temporal 技术介绍与使用指南（Go 工作流编排）

> 把「长事务、重试、超时、补偿」从业务代码里抽出来：用代码定义工作流，服务端持久化执行历史，进程崩溃后自动重放恢复。
> 上游：<https://github.com/temporalio/temporal>　|　主线：Server **1.2x** / Go SDK [temporalio/sdk-go](https://github.com/temporalio/sdk-go) v1.x（整理于 2026-11）
> 协议：MIT（Server 早期为 MPL-2.0，以官方 LICENSE 为准）；具体版本以上游最新 release 为准。

---

## 1. 项目简介

[asynq 篇](../asynq/asynq技术文档与使用手册.md) 解决「可靠地执行一个异步作业」，[Kafka 篇](../kafka/kafka-guide.md) 解决「可靠地传递一条事件」，Temporal 解决的是第三层问题：**一个要跑几小时到几个月、跨多个服务、中间会失败且必须能续跑的业务流程**（订单履约、退款审批、数据迁移、订阅计费）。

传统做法是自己维护状态机表 + 定时扫描 + 幂等 + 补偿，代码里一半是「状态怎么存、失败怎么续」。Temporal 的核心主张是 **durable execution**：你写一段看起来像同步顺序执行的代码，服务端把执行过程记成**事件历史**，worker 挂了就从历史重放到断点继续。

- **适用场景**：长周期业务流程、需要人工介入/等待信号的流程、跨服务 Saga 与补偿、必须可观测可重放的关键流程、定时循环且需状态累积的任务
- **不适合**：简单的一次性异步作业（用 asynq 更轻）、纯事件广播（用 Kafka / [RabbitMQ](../rabbitmq/rabbitmq-guide.md)）、毫秒级高吞吐无状态处理、数据量极大的批处理（事件历史会膨胀）

### 安装与本地起服务

```bash
go get go.temporal.io/sdk
# 开发服务器（内存持久化，仅本地用）
temporal server start-dev --port 7233 --ui-port 8233
# Web UI: http://localhost:8233
```

### 最小可运行示例

```go
package main

import (
    "context"
    "fmt"
    "log"
    "time"

    "go.temporal.io/sdk/client"
    "go.temporal.io/sdk/temporal"
    "go.temporal.io/sdk/worker"
    "go.temporal.io/sdk/workflow"
)

// Activity：所有外部 IO 都放这里（必须幂等）
func Charge(ctx context.Context, orderID string, amount int64) (string, error) {
    // 真实场景：调用支付网关，用 orderID 做幂等键
    return "txn-" + orderID, nil
}

// Workflow：只做编排，不做 IO；代码必须确定性
func OrderWorkflow(ctx workflow.Context, orderID string, amount int64) (string, error) {
    ctx = workflow.WithActivityOptions(ctx, workflow.ActivityOptions{
        StartToCloseTimeout: 30 * time.Second,
        RetryPolicy: &temporal.RetryPolicy{MaximumAttempts: 3},
    })
    var txnID string
    if err := workflow.ExecuteActivity(ctx, Charge, orderID, amount).Get(ctx, &txnID); err != nil {
        return "", err
    }
    return txnID, nil
}

func main() {
    c, err := client.Dial(client.Options{HostPort: "localhost:7233"})
    if err != nil { log.Fatal(err) }
    defer c.Close()

    w := worker.New(c, "order-tq", worker.Options{})
    w.RegisterWorkflow(OrderWorkflow)
    w.RegisterActivity(Charge)
    go func() { _ = w.Run(worker.InterruptCh()) }()

    run, err := c.ExecuteWorkflow(context.Background(), client.StartWorkflowOptions{
        ID:        "order-A1001",       // 业务唯一 ID → 天然幂等，重复启动返回已存在
        TaskQueue: "order-tq",
    }, OrderWorkflow, "A1001", 12800)
    if err != nil { log.Fatal(err) }

    var result string
    if err := run.Get(context.Background(), &result); err != nil { log.Fatal(err) }
    fmt.Println("workflow done:", result)
}
```

---

## 2. 功能特性总览

| 能力 | 说明 |
|---|---|
| 核心对象 | Workflow（编排逻辑）/ Activity（外部 IO）/ Worker（执行器）/ Task Queue（任务队列） |
| 持久化执行 | 服务端记录**事件历史**，worker 崩溃后从历史重放恢复，无需业务存状态机 |
| 持久计时器 | `workflow.Sleep` / `workflow.Await` 可等待数小时到数月，计时由服务端持久化 |
| 重试 | Activity 级 `RetryPolicy`（次数、退避、不可重试错误类型）、超时分层 |
| 交互 | **Signal**（异步投递事件）、**Query**（只读查状态）、**Update**（有返回、可等待） |
| 组合 | Child Workflow、`Continue-As-New`（重置历史）、Side Effect / Mutable Side Effect |
| 版本化 | `workflow.GetVersion` / Worker Versioning，保证在途实例能按旧逻辑重放 |
| 调度 | Schedule（cron 式周期启动工作流，带重叠策略与暂停） |
| 可观测 | Web UI 看事件历史、搜索属性（Search Attributes）按业务 ID 检索、Replay 检测非确定性 |
| 测试 | `testsuite.TestWorkflowEnvironment` 本地跑工作流、时间跳跃、mock Activity、历史重放 |
| 服务端 | Frontend / History / Matching / Worker Service + 持久化（Cassandra / MySQL / PostgreSQL）+ Visibility（ES 或 SQL） |

---

## 3. 核心概念与原理

### 3.1 durable execution：重放不是「重跑」

```
Workflow 代码（看似同步顺序执行）
   │ worker 执行到 await 点 → 产生 Command（ScheduleActivity / StartTimer / ...）
   ▼
Temporal Server 把 Command 追加为 Event History（持久化）
   │ 分发 Activity Task 给 worker → Activity 执行 → 结果作为新 Event 追加
   ▼
worker 崩溃 / 部署重启 / 换机器
   │ 从 Event History 重放 Workflow 代码：遇到已完成的 Activity，直接取历史里的结果
   ▼  不再真正执行副作用
继续从断点往下走
```

**关键**：重放时 Workflow 代码会被再次执行，但**已经完成 Activity 的结果直接从历史返回，不会重复调用外部系统**。这就是为什么 Workflow 代码必须**确定性**——同样的历史必须推导出同样的 Command 序列，否则服务端会判定 `NonDeterminismError` 并卡住工作流。

### 3.2 确定性约束（最容易踩的坑）

| 禁止在 Workflow 里 | 替代方案 |
|---|---|
| `time.Now()` / `time.Sleep()` | `workflow.Now(ctx)` / `workflow.Sleep(ctx, d)` |
| `rand`、UUID、随机数 | `workflow.SideEffect` 保存结果，或在 Activity 里生成 |
| HTTP / DB / 文件 IO | 全部放进 **Activity** |
| 直接起 goroutine / channel | `workflow.Go` / `workflow.Channel` / `workflow.Await` |
| 读环境变量 / 全局可变配置 | 作为 Workflow 入参传入，或 `SideEffect` |
| 迭代 `map`（顺序不确定） | 排序后遍历 |

Activity **不受确定性约束**，因为它只执行一次（或重试），结果会被记进历史。

### 3.3 Task Queue 与 Worker

```
Starter ──StartWorkflow──► Server (History Service)
                              │ 产生 WorkflowTask → 放入 Task Queue "order-tq"
                              ▼
Worker 长轮询 Task Queue ──领取──► 重放 Workflow → 返回 Commands
                              │ 若 Command 是 ScheduleActivity
                              ▼
                       ActivityTask 入队 → Worker 执行 Activity → 回报结果
```

Task Queue 是**逻辑队列**，不是 Kafka；Worker 通过长轮询拉取，按 `WorkerOptions` 控制并发槽位。不同业务/版本用不同 Task Queue 名是常见的隔离手段。

### 3.4 Activity 是 at-least-once

Activity 可能因为超时、worker 崩溃、网络抖动被重试，**结果也可能已经成功但回报丢失**。因此：

- Activity **必须幂等**（用业务 ID 做幂等键，或 `ON CONFLICT DO NOTHING`，见 [PostgreSQL 篇](../postgresql/postgresql-guide.md)）
- 用 `RetryPolicy.NonRetryableErrorTypes` 明确区分「参数非法」（不重试）与「下游超时」（重试）
- 用 `temporal.NewApplicationError(err.Error(), "TYPE", false)` 把业务错误标记为不可重试；`workflow` 侧可用 `temporal.IsApplicationError` 判断

### 3.5 超时分层

| 超时 | 含义 |
|---|---|
| `WorkflowExecutionTimeout` | 整个工作流最长时长（含重试与等待） |
| `WorkflowRunTimeout` | 单次 run 的时限（`Continue-As-New` 会开启新 run） |
| `WorkflowTaskTimeout` | 单次 workflow task（重放决策）的时限，默认 10s |
| `ScheduleToStartTimeout` | Activity 排队等待被 worker 领取的时间 |
| `StartToCloseTimeout` | **单个 Activity 执行**的时限（最常配） |
| `ScheduleToCloseTimeout` | Activity 从调度到完成的总时限（含重试） |
| `HeartbeatTimeout` | 长 Activity 需 `activity.RecordHeartbeat`，超时判定 worker 死亡 |

### 3.6 Signal / Query / Update

| | 是否改状态 | 是否有返回 | 典型用途 |
|---|---|---|---|
| **Signal** | 是（异步） | 否 | 人工审批通过、取消订单、外部回调 |
| **Query** | 否（只读） | 是 | 查询当前进度（**不能阻塞、不能改状态**） |
| **Update** | 是 | 是（可等待） | 需要确认「已处理」的状态变更 |

混淆三者是常见设计错误：Query 里做 IO 或改变量会破坏确定性；Signal 处理函数里阻塞会卡住工作流。

### 3.7 版本化与 Continue-As-New

- 改 Workflow 代码（增删 Activity、调整顺序）会让**在途实例**重放时 Command 序列对不上 → 用 `workflow.GetVersion` 给新旧分支打标，或启用 Worker Versioning 让旧实例固定跑旧版本 worker
- 长时间循环的工作流事件历史会无限增长（每次循环都记 Event），导致重放变慢、超出历史大小限制 → 用 **`Continue-As-New`** 开启新 run 并重置历史，把必要状态作为参数传下去

---

## 4. 使用实战

### 4.1 带等待与信号的订单工作流

```go
func OrderWorkflow(ctx workflow.Context, orderID string, amount int64) (string, error) {
    ao := workflow.ActivityOptions{
        StartToCloseTimeout: 30 * time.Second,
        RetryPolicy: &temporal.RetryPolicy{
            InitialInterval:    time.Second,
            BackoffCoefficient: 2.0,
            MaximumInterval:    time.Minute,
            MaximumAttempts:    3,
            NonRetryableErrorTypes: []string{"InvalidArgument"},
        },
    }
    ctx = workflow.WithActivityOptions(ctx, ao)

    // 1. 保留库存（幂等 Activity）
    if err := workflow.ExecuteActivity(ctx, ReserveStock, orderID).Get(ctx, nil); err != nil {
        return "", err
    }

    // 2. 等待支付：最长 15 分钟，支持 Signal 提前完成
    paid := workflow.GetSignalChannel(ctx, "payment-confirmed")
    var paymentID string
    timer := workflow.NewTimer(ctx, 15*time.Minute)
    selector := workflow.NewSelector(ctx)
    selector.AddReceive(paid, func(c workflow.ReceiveChannel, more bool) {
        c.Receive(ctx, &paymentID)
    })
    selector.AddFuture(timer, func(f workflow.Future) {
        paymentID = "" // 超时
    })
    selector.Select(ctx)
    if paymentID == "" {
        _ = workflow.ExecuteActivity(ctx, ReleaseStock, orderID).Get(ctx, nil) // 补偿
        return "", fmt.Errorf("payment timeout")
    }

    // 3. 支付扣款
    var txnID string
    if err := workflow.ExecuteActivity(ctx, Charge, orderID, amount).Get(ctx, &txnID); err != nil {
        _ = workflow.ExecuteActivity(ctx, ReleaseStock, orderID).Get(ctx, nil)
        return "", err
    }
    return txnID, nil
}
```

> 补偿（Saga）用 `defer` 风格的显式补偿 Activity，而不是依赖数据库回滚——跨服务没有分布式事务。若补偿本身失败，靠它自己的 `RetryPolicy` 兜底。

### 4.2 Query：暴露进度给外部

```go
func OrderWorkflow(ctx workflow.Context, orderID string, amount int64) (string, error) {
    state := struct{ Stage string; Paid bool }{Stage: "created"}
    if err := workflow.SetQueryHandler(ctx, "state", func() (any, error) {
        return state, nil // 只读，不做 IO、不阻塞
    }); err != nil { return "", err }
    // ... 推进流程时更新 state.Stage / state.Paid
    return "", nil
}
```

```go
// 外部查询
resp, err := c.QueryWorkflow(ctx, "order-A1001", "", "state")
_ = resp.QueryValue(&state)
```

### 4.3 发送 Signal

```go
err := c.SignalWorkflow(context.Background(), "order-A1001", "",
    "payment-confirmed", "pay-7788")
```

### 4.4 版本化（改动在途工作流代码）

```go
v := workflow.GetVersion(ctx, "use-new-charge", workflow.DefaultVersion, 1)
if v == workflow.DefaultVersion {
    err := workflow.ExecuteActivity(ctx, ChargeV1, orderID).Get(ctx, nil)
    return "", err
}
err := workflow.ExecuteActivity(ctx, ChargeV2, orderID, amount).Get(ctx, nil)
return "", err
```

> 标记要**长期保留**（直到所有旧版本实例结束），删掉会让在途实例重放失败。新项目可直接用 Worker Versioning（`worker.BuildID` + 服务端 deployment 配置）避免手工打标。

### 4.5 长 Activity 心跳与取消

```go
func MigrateBatch(ctx context.Context, batchID string) error {
    for i := 0; i < total; i++ {
        select {
        case <-ctx.Done():
            return ctx.Err() // 响应取消，保存现场
        default:
        }
        if err := processOne(i); err != nil { return err }
        activity.RecordHeartbeat(ctx, i) // 每 N 条上报进度
    }
    return nil
}
```

`activity.GetHeartbeatDetails(ctx, &last)` 可在重试后从上次心跳位置续跑。

### 4.6 单测与重放检测

```go
func TestOrderWorkflow(t *testing.T) {
    env := testsuite.NewTestWorkflowEnvironment()
    env.RegisterActivity(ReserveStock)
    env.RegisterActivity(Charge)
    env.RegisterActivity(ReleaseStock)
    env.RegisterDelayedCallback(func() {
        _ = env.SignalWorkflow("payment-confirmed", "pay-7788")
    }, time.Minute) // 测试环境可瞬间跳过虚拟时间

    env.ExecuteWorkflow(OrderWorkflow, "A1001", 12800)
    require.True(t, env.IsWorkflowCompleted())
    require.NoError(t, env.GetWorkflowError())
}
```

```go
// 用生产事件历史重放，检测代码改动是否引入非确定性
replayer := worker.NewWorkflowReplayer()
replayer.RegisterWorkflow(OrderWorkflow)
_ = replayer.ReplayWorkflowHistoryFromJSONFile(nil, "history.json")
```

### 4.7 搜索属性与运维

```bash
temporal operator search-attribute create --name OrderId --type Text
temporal workflow list --query 'OrderId = "A1001"'
temporal workflow show --workflow-id order-A1001   # 查看完整事件历史
temporal workflow signal --workflow-id order-A1001 --name payment-confirmed
```

在 `StartWorkflowOptions.TypedSearchAttributes` 里写入业务 ID，才能在 Web UI 里按订单号检索——否则只能靠 workflow ID 猜。

---

## 5. 版本演进速览

| 阶段 | 关键变化与迁移注意点 |
|---|---|
| 起源 | 源自 Uber **Cadence**；Temporal 由原团队创建，SDK/API 与 Cadence 相似但不兼容 |
| Server 1.x | 架构定型（Frontend / History / Matching / Worker）；Visibility 长期依赖 Elasticsearch |
| 近年 | **标准可见性（SQL-based Visibility）** 让小集群不必再部署 ES；**Worker Versioning / Deployment** 简化在途实例升级；**Nexus** 跨 namespace 调用；**Update** API 稳定；Schedule 与 Eager Activity 优化 |
| Go SDK v1 | API 稳定；`workflow.GetVersion` → Worker Versioning、`TypedSearchAttributes` 等新 API 逐步引入 |

**迁移注意**：

- Server 与 SDK 版本建议同代，跨太多版本可能出现 gRPC 接口与 Command 类型不兼容
- Cadence → Temporal 不是改 import：`go.uber.org/cadence` 与 `go.temporal.io/sdk` 的客户端、worker、context 类型全部不同，需要重写胶水层
- 从 ES Visibility 迁到 SQL Visibility 需按官方步骤做双写与回填，历史数据不可直接复用
- 启用 Worker Versioning 前，先确认在途实例的 Build ID 策略与回滚路径

---

## 6. 常见坑与最佳实践

1. **Workflow 里做 IO / 用 `time.Now` / `rand`**：非确定性，重放时 `NonDeterminismError`，工作流直接卡住。所有副作用进 Activity，时间用 `workflow.Now`，随机用 `SideEffect`。
2. **直接改 Workflow 代码不做版本化**：在途实例用新代码重放旧历史，Command 序列对不上。用 `GetVersion` 或 Worker Versioning，并长期保留标记。
3. **Activity 不幂等**：at-least-once + 超时重试 + 结果回报丢失 = 重复扣款。用业务 ID 做幂等键，配合 [PostgreSQL 篇](../postgresql/postgresql-guide.md) 的唯一约束或 [go-redis 篇](../redis/redis-guide.md) 的 `SET NX`。
4. **大 payload 进参数 / 结果**：事件历史会永久保存输入与输出（默认有大小限制，约 2MB）。大对象放外部存储，只传 ID/引用。
5. **事件历史无限增长**：无限循环里反复 ExecuteActivity 会让历史越滚越大、重放越来越慢。用 `Continue-As-New` 重置，或把循环体移到单个带心跳的 Activity。
6. **超时只配一半**：只设 `WorkflowExecutionTimeout` 不设 `StartToCloseTimeout`，Activity 挂住会一直占着工作流。分层配置齐全。
7. **混淆 Signal / Query / Update**：Query 里做 IO 或改状态会破坏确定性；Signal handler 里阻塞会卡死事件循环。
8. **重试策略无上限 / 不区分错误类型**：参数非法这类永久错误被无限重试。设 `MaximumAttempts` 与 `NonRetryableErrorTypes`。
9. **长 Activity 不报心跳**：worker 被杀后服务端要等满 `StartToCloseTimeout` 才判定失败，恢复极慢。`RecordHeartbeat` + `HeartbeatTimeout` + 续跑。
10. **忽略取消**：Activity 不检查 `ctx.Done()`，工作流取消后仍在跑副作用。长循环必须响应取消并保存现场。
11. **Task Queue 名混乱 / 多版本 worker 混跑**：旧代码 worker 领到新工作流会重放失败。用版本化部署或分队列灰度。
12. **把 Temporal 当消息队列 / 任务队列**：为每条消息起一个工作流的成本高、延迟也高。简单作业用 [asynq](../asynq/asynq技术文档与使用手册.md)，事件广播用 [Kafka](../kafka/kafka-guide.md)。
13. **dev server 上生产**：`temporal server start-dev` 是内存存储、单进程、无 HA。生产需外部持久化（MySQL/PostgreSQL/Cassandra）、多副本与备份。
14. **不跑重放检测**：代码改动上线前用 `WorkflowReplayer` 对生产历史做一次重放，能提前发现非确定性，比线上卡住再修便宜得多。
15. **不建搜索属性 / Workflow ID 设计随意**：出事时按业务单号查不到工作流。ID 用稳定业务键（天然幂等），并写 `TypedSearchAttributes`。
16. **补偿没做幂等 / 没重试策略**：Saga 的反向操作同样会失败，需要与正向操作同等级的重试和幂等设计。
17. **Workflow 里用 map 迭代或依赖外部可变配置**：迭代顺序不确定、配置变更导致重放分歧。排序遍历，配置在启动时作为入参固化。

---

## 7. 选型对比

| 维度 | Temporal | asynq | Airflow / Argo Workflows | AWS Step Functions | 自研状态机 + 定时扫描 |
|---|---|---|---|---|---|
| 编排形态 | **代码定义工作流** | 单个异步作业 | DAG / YAML | JSON 状态机 | DB 表 + 轮询 |
| 状态持久化 | 服务端事件历史（自动） | Redis 任务状态 | 元数据 DB | 托管 | 自己建表维护 |
| 长时等待 | **原生持久计时器**（月级） | 延时任务（有限） | 调度触发 | 原生 Wait | 定时扫描 |
| 崩溃恢复 | **自动重放** | 重试 / 崩溃回收 | 任务重跑 | 托管 | 自己实现 |
| 补偿 / Saga | 代码里显式，易写 | 需自己拼 | DAG 分支 | 内置 Catch | 自己实现 |
| 运维成本 | 中～高（多服务 + DB） | **极低**（只要 Redis） | 中高 | 极低（托管） | 低（但代码成本高） |
| 语言生态 | Go / Java / TS / Python / .NET | Go | Python / YAML | 各语言 SDK | — |
| 典型场景 | 订单履约、退款审批、订阅计费 | 发邮件、转码、对账 | 数据管道 / 批处理 | 云上 Serverless 编排 | 简单两三步流程 |

一句话决策：

- **流程跨多个服务、要跑很久、失败必须续跑且要可观测** → **Temporal**，它能省掉一整套自研状态机代码。
- **只是「把一件活扔到后台可靠执行」** → [asynq](../asynq/asynq技术文档与使用手册.md)，引入成本低一个数量级。
- **定时数据管道 / ETL DAG** → Airflow / Argo Workflows，它们面向「批任务依赖」而非「长事务实体」。
- **已在云上且接受厂商锁定** → Step Functions，运维成本最低。
- **流程只有两三步、团队小** → 状态机表 + 幂等 + 定时扫描完全够用，**别为了一个短流程引入 Temporal 集群**。

> 判断标准很简单：如果流程需要「等一个可能几天后才来的外部事件，期间进程可以随便重启」，Temporal 才真正值回票价；否则它带来的运维与心智成本会超过收益。
