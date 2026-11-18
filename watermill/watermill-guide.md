# Watermill 技术指南

> Watermill 把「消息从哪来、到哪去」抽象成 Publisher / Subscriber，业务只写 Handler：换 broker 不改业务代码，中间件与 CQRS 也顺手。
> 上游：<https://github.com/ThreeDotsLabs/watermill> **v1.5.x（v1.5.3，2026-08；上一维护线 v1.4.7）**　|　实现：Kafka / NATS / RabbitMQ / Redis Streams / SQL　|　许可：MIT

---

## 1. 项目简介

Watermill 由 ThreeDotsLabs 在 2018 年开源，定位不是「又一个 MQ 客户端」，而是 **Go 的消息抽象层**：用一套 `Publisher` / `Subscriber` / `Router` / `Middleware` 接口把 Kafka、NATS、RabbitMQ、Redis Streams、SQL 甚至进程内 channel 统一起来。业务代码只依赖 `message` 包，换 broker 只换构造适配器那几行。

类比：**`database/sql` 之于各种数据库驱动，就是 Watermill 之于各种 broker**。它不替代 broker，也不隐藏 broker 的模型（分区、消费组、保留策略仍然存在），只是把「消费—处理—再发布—确认」这条主干流程标准化。

三块能力拼起来才是完整形态：

| 层 | 包 | 职责 |
|---|---|---|
| 消息模型 | `github.com/ThreeDotsLabs/watermill/message` | `Message`、`Publisher` / `Subscriber` 接口、`Router`、`Handler` |
| 中间件 | `message/router/middleware` | 重试、panic 恢复、超时、关联 ID、死信、限流、熔断、去重 |
| 应用组件 | `components/cqrs`、`components/requestreply`、`components/metrics` | 命令/事件总线、请求-响应、Prometheus 指标 |

生态是**独立 Go module**：核心 `watermill`，适配器 `watermill-kafka/v3`、`watermill-nats/v2`、`watermill-amqp/v3`、`watermill-redisstream`、`watermill-sql/v3`，可以只挑一个装。

---

## 2. 能力总览

| 能力 | 组件 / 类型 | 说明 |
|---|---|---|
| 统一消息模型 | `message.Message` | UUID + Metadata + Payload + Ack/Nack，broker 无关 |
| 发布 / 订阅 | `message.Publisher` / `message.Subscriber` | 两个方法 + `Close()`，自己实现即可接入私有中间件 |
| 消费编排 | `message.Router` | 注册 handler，自动订阅、调用、ack、发布下游、优雅停止 |
| 中间件 | `router/middleware` | Recoverer / Retry / Timeout / CorrelationID / PoisonQueue / Throttle / CircuitBreaker / Deduplicator |
| 命令总线 | `cqrs.CommandBus` | 一个命令一个处理者，点对点语义 |
| 事件总线 | `cqrs.EventBus` | 一个事件多个订阅者，广播语义 |
| 请求-响应 | `components/requestreply` | 阻塞等回复，泛型返回结果；本质仍是异步消息 |
| 可观测 | `components/metrics` | 消费量、处理耗时、发布延迟；另有 OTel 适配 |
| 事务性发件箱 | `watermill-sql` + forwarder | 与业务同库同事务写 outbox，后台转发到 broker |
| 内存实现 | `pubsub/gochannel` | 单测与本地开发零依赖，**不要用于生产** |
| 测试工具 | `pubsub/gochannel` + `message.TestPublisher` | 无需起 broker，直接断言 handler 输入输出 |

---

## 3. 核心概念与原理

### 3.1 Message：一条消息只有四样东西

```go
type Message struct {
    UUID     string            // 全局唯一，broker 无关；常被拿去当去重键
    Metadata Metadata          // map[string]string，可 JSON 序列化
    Payload  []byte            // 业务体，JSON / protobuf / 任意字节
    // 未导出：context、ack/nack 通道、是否已 ack 的标记
}
```

- `message.NewMessage(uuid, payload)` 构造，通常 `watermill.NewUUID()` 生成 UUID；
- `msg.Metadata.Set("partition", orderID)` —— Metadata 是**跨 broker 传递**的唯一通道，分区键、租户、追踪 ID 都靠它；
- `msg.Context()` 带取消信号，handler 里应基于它派生超时语境；
- `msg.Ack()` / `msg.Nack()` **手写消费循环时**必须自己调；走 `Router` 时由 Router 根据 handler 返回值代劳，业务里**不要**再调一次。

Metadata 的两个坑：同名 broker header 映射由适配器决定（Kafka 适配器默认把 metadata 序列化进 header），超长 header 会被 broker 拒绝；中间件之间靠固定 key 协作（如 `correlation_id`、`poisonqueue_topic`），改名即断链。

### 3.2 Publisher / Subscriber：只有三个方法

```go
type Publisher interface {
    Publish(topic string, messages ...*Message) error
    Close() error
}

type Subscriber interface {
    Subscribe(ctx context.Context, topic string) (<-chan *Message, error)
    Close() error
}
```

`Subscribe` 立即返回一个 channel，适配器内部起 goroutine 拉消息往里塞；**channel 被关闭表示订阅结束**。所以 Router 的模型非常朴素：拿到 channel，`for msg := range ch`。

同样的接口在不同 broker 上的行为差异巨大，这是选型时真正要看的东西：

| 维度 | Kafka | NATS JetStream | Redis Streams |
|---|---|---|---|
| 负载均衡单位 | consumer group | queue group / durable consumer | consumer group |
| 进度保存 | offset 提交到 `__consumer_offsets` | consumer 的 ack 序列 | `last-delivered-id` + PEL |
| Nack 后行为 | 不提交 offset，重新拉到（可能换实例） | `Nak()` 按 `MaxDeliver` 立即重投 | 留在 PEL，靠 `XAUTOCLAIM` 认领 |
| 顺序保证 | 仅分区内 | 单 subject 内 | 单 stream 内 |
| 再均衡 | 有，期间可能重复投递 | 无此概念 | 无此概念 |
| 语义天花板 | 至少一次 | 至少一次 | 至少一次 |

结论：**Watermill 统一的是 API，不是语义**。从 Kafka 换到 Redis Streams，代码不改，但重复投递的形态、积压的观测方式、顺序保证的边界全变了。

### 3.3 Router：每个 handler 一个 goroutine

`Router` 是 Watermill 的执行引擎。`Run(ctx)` 时会为每个已注册 handler 启动一个 goroutine，循环做五件事：

```text
从 Subscriber channel 取 msg
  → 依次穿过中间件链
  → 调 handler(msg)
  → handler 无错：发布返回的新消息，全部成功后 msg.Ack()
  → handler 报错：msg.Nack()（重投交给 PubSub 决定）
```

由此推出三条硬约束：

1. **一个 handler 串行处理自己的订阅**。handler 阻塞 10 秒，这个订阅（Kafka 上是分到的那些分区）就停 10 秒；提高并行度靠加分区 + 加实例，不是加 goroutine；
2. **handler 名称必须全局唯一**，重复注册直接报错；
3. 优雅停止由 `Close()` 完成：停止拉新消息 → 等待在途 handler 结束（上限 `RouterConfig.CloseTimeout`，默认 30s）→ 关闭 Pub/Sub。

注册方式三选一：`AddHandler`（订阅 + 发布，最常用）、`AddNoPublisherHandler`（纯消费者）、`AddConsumerHandler`（只订阅不产出，语义更明确）；`AddHandler` 返回 `*message.Handler`，可挂 handler 级中间件或单独 `Stop()`。

`Running()` 返回一个 channel，**Router 启动完成后关闭**，是「所有订阅就绪」的信号，健康检查/就绪探针应该等它。

### 3.4 ack 时机：这就是「至少一次」的全部秘密

Router 只在 handler 返回 `nil` 且下游消息全部发布成功后才 `Ack`。任一环节出错就 `Nack`，而 Nack 在 Kafka 适配器里的含义是「不提交 offset」——进程重启、rebalance、重连之后这条消息会被再投一次。

所以：**Watermill 默认给的是 at-least-once，不是 exactly-once**。官方文档对 Kafka 也是明确写 `ExactlyOnceDelivery: no`。

推论只有一条，但极其重要：**handler 必须幂等**。可用手段：
- 以 `msg.UUID` 建去重表（Redis `SETNX` + TTL 或唯一索引），命中即跳过；
- 业务层用幂等写（`INSERT ... ON CONFLICT DO NOTHING`、状态机只允许单向跃迁）；
- 副作用外置（发短信、扣款）用「本地状态表 + 唯一约束」兜住，而不是靠不重投。

同时注意 `middleware.Deduplicator` 是**处理前去重**，默认内存实现只在单实例内有效；多副本部署必须换成 Redis 等共享存储的 `ExpiringKeyRepository`。

### 3.5 中间件链：顺序就是语义

中间件签名是 `func(HandlerFunc) HandlerFunc`，`router.AddMiddleware` **先加的在最外层**（包裹后加的）。这决定了组合顺序：

```go
router.AddMiddleware(
    middleware.CorrelationID,          // 最外层：把 correlation_id 复制到产出消息
    poisonQueue,                        // 重试彻底失败后才落死信
    retryMiddleware,                    // 内部重试，耗尽后把 error 抛出
    middleware.Recoverer,               // 最内层：panic 转 error，交给 Retry
)
```

| 中间件 | 作用 | 注意 |
|---|---|---|
| `Recoverer` | 捕获 panic，转成带堆栈的 error | 必须放最内层，否则 panic 直接杀进程 |
| `Retry` | 指数退避重试 handler（cenkalti/backoff） | **阻塞式**，占着这个 handler goroutine；总预算应小于 broker 的 session timeout |
| `Timeout` | 给 `msg.Context()` 加 deadline | handler 必须尊重 `ctx.Done()`，否则形同虚设 |
| `CorrelationID` | 透传 `correlation_id` 到下游 | 入口用 `middleware.SetCorrelationID` 播种 |
| `PoisonQueue` | 失败消息转发到死信 topic 并附失败原因 | 不配 = 毒消息无限重投；要放在 Retry 外层 |
| `Throttle` | 令牌桶限速，保护下游 | 阻塞排队，不是丢弃 |
| `CircuitBreaker` | 连续失败后快速失败 | 防雪崩，恢复期半开探测 |
| `Deduplicator` | 内容哈希去重 | 默认内存，多实例要共享存储 |

`PoisonQueue` 会往消息 Metadata 写 `reason`、`poisonqueue_topic`、`poisonqueue_handler_name`，消费死信时靠这些字段定位问题、决定是否重放。

### 3.6 CQRS 与请求-响应

CQRS 组件把「按类型路由」这件事封装掉：`CommandBus` / `EventBus` 负责发，`CommandProcessor` / `EventProcessor` 基于 Router 负责收并分发到具体 handler。

```go
commandBus.Send(ctx, &IssueInvoice{OrderID: "o-1"})   // 一个命令，期望一个处理者
eventBus.Publish(ctx, &OrderPaid{OrderID: "o-1"})     // 一个事件，N 个订阅者
```

topic 由 `GeneratePublishTopic` / `GenerateSubscribeTopic` 回调决定（一般是 `commands.<Name>`、`events.<Name>`），序列化由 `cqrs.JSONMarshaler{GenerateName: cqrs.StructName}` 负责。`EventProcessor` 支持 `AddHandlersGroup`，让多个事件 handler 共用一个订阅以保证顺序——否则同一 topic 上的事件会被并行处理。

请求-响应用 `components/requestreply`：请求方 `SendWithReply[T](ctx, backend, cmd)` 阻塞等回复，回复 topic 由 operation ID 关联。官方态度很明确：**默认不推荐**，它把两个服务在时间上耦合起来；只是为了拿返回值，gRPC 更合适。它真正的价值场景是「必须确保命令被处理，同时要把处理结果回给用户」。

---

## 4. 使用实战

### 4.1 依赖

```bash
go get github.com/ThreeDotsLabs/watermill@v1.5.3
go get github.com/ThreeDotsLabs/watermill-kafka/v3
```

### 4.2 Kafka 订阅 → 处理 → 发布下游（完整可运行）

```go
package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/ThreeDotsLabs/watermill"
	"github.com/ThreeDotsLabs/watermill-kafka/v3/pkg/kafka"
	"github.com/ThreeDotsLabs/watermill/message"
	"github.com/ThreeDotsLabs/watermill/message/router/middleware"
	"github.com/ThreeDotsLabs/watermill/message/router/plugin"
)

func main() {
	logger := watermill.NewSlogLogger(slog.Default())

	pub, err := kafka.NewPublisher(kafka.PublisherConfig{
		Brokers:   []string{"localhost:9092"},
		Marshaler: kafka.DefaultMarshaler{},
	}, logger)
	must(err)
	defer pub.Close()

	sub, err := kafka.NewSubscriber(kafka.SubscriberConfig{
		Brokers:         []string{"localhost:9092"},
		Unmarshaler:     kafka.DefaultMarshaler{},
		ConsumerGroup:   "billing-service", // 不设 = 每个实例都消费全量
		NackResendSleep: 500 * time.Millisecond,
	}, logger)
	must(err)
	defer sub.Close()

	router, err := message.NewRouter(message.RouterConfig{
		CloseTimeout: 30 * time.Second, // 等在途 handler 结束的上限
	}, logger)
	must(err)
	router.AddPlugin(plugin.SignalsHandler) // SIGTERM 自动 Close

	poisonQueue, err := middleware.PoisonQueue(pub, "orders.dead_letter")
	must(err)

	router.AddMiddleware(
		middleware.CorrelationID,
		poisonQueue,
		middleware.Retry{
			MaxRetries:      3,
			InitialInterval: 200 * time.Millisecond,
			MaxInterval:     5 * time.Second,
			Multiplier:      2,
			Logger:          logger,
		}.Middleware,
		middleware.Recoverer,
	)

	router.AddHandler(
		"order_paid_to_invoice",  // 名称全局唯一
		"orders.paid",            // 订阅
		sub,
		"billing.invoice_issued", // 产出
		pub,
		handleOrderPaid,
	)

	go func() {
		if err := router.Run(context.Background()); err != nil {
			logger.Error("router stopped", err, nil)
		}
	}()
	<-router.Running() // 订阅就绪

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, syscall.SIGINT, syscall.SIGTERM)
	<-stop

	if err := router.Close(); err != nil { // 停拉取 → 等 handler → 关连接
		logger.Error("router close", err, nil)
	}
}

func handleOrderPaid(msg *message.Message) ([]*message.Message, error) {
	ctx, cancel := context.WithTimeout(msg.Context(), 5*time.Second)
	defer cancel()

	var ev struct {
		OrderID string `json:"order_id"`
		Amount  int64  `json:"amount"`
	}
	if err := json.Unmarshal(msg.Payload, &ev); err != nil {
		// 不可恢复错误：重试无意义，交给 PoisonQueue 落死信
		return nil, fmt.Errorf("unmarshal order_paid: %w", err)
	}

	// 幂等：同一 order_id 重复投递只会开一张发票
	if err := issueInvoiceOnce(ctx, ev.OrderID, ev.Amount); err != nil {
		return nil, err // 返回 error = Nack = 稍后重投
	}

	out := message.NewMessage(watermill.NewUUID(), []byte(`{"order_id":"`+ev.OrderID+`"}`))
	// 分区键写进 Metadata：同一订单必须落同一分区，否则下游乱序
	out.Metadata.Set("partition", ev.OrderID)
	return message.Messages{out}, nil
}

func issueInvoiceOnce(ctx context.Context, orderID string, amount int64) error {
	return nil // 用唯一索引 / 幂等 upsert 实现
}

func must(err error) {
	if err != nil {
		panic(err)
	}
}
```

配套的 broker 配置（本地 Redpanda 即可，不必装 Kafka）：

```yaml
services:
  kafka:
    image: redpandadata/redpanda:v26.1.7
    command: >
      redpanda start --mode dev-container --smp 1
      --kafka-addr internal://0.0.0.0:9092,external://0.0.0.0:19092
      --advertise-kafka-addr internal://kafka:9092,external://localhost:19092
    ports: ["19092:19092"]
```

### 4.3 CQRS：命令总线 + 处理器

```go
commandBus, err := cqrs.NewCommandBusWithConfig(pub, cqrs.CommandBusConfig{
	GeneratePublishTopic: func(p cqrs.GenerateCommandMessageTopicParams) (string, error) {
		return "commands." + p.CommandName, nil
	},
	Marshaler: cqrs.JSONMarshaler{GenerateName: cqrs.StructName},
	Logger:    logger,
})
must(err)

// 一个命令 → 一个订阅者；SubscriberConstructor 里给每个命令建独立 consumer group
processor, err := cqrs.NewCommandProcessorWithConfig(router, cqrs.CommandProcessorConfig{
	GenerateSubscribeTopic: func(p cqrs.CommandProcessorGenerateSubscribeTopicParams) (string, error) {
		return "commands." + p.CommandName, nil
	},
	SubscriberConstructor: func(p cqrs.CommandProcessorSubscriberConstructorParams) (message.Subscriber, error) {
		return kafka.NewSubscriber(kafka.SubscriberConfig{
			Brokers:       []string{"localhost:9092"},
			ConsumerGroup: "billing-" + p.HandlerName,
		}, logger)
	},
	Marshaler: cqrs.JSONMarshaler{GenerateName: cqrs.StructName},
})
must(err)
must(processor.AddHandlers(cqrs.NewCommandHandler("IssueInvoice", handleIssueInvoice)))

must(commandBus.Send(context.Background(), &IssueInvoice{OrderID: "o-1"}))
```

### 4.4 指标：几行拿到 Prometheus

```go
registry := prometheus.NewRegistry()
builder := metrics.NewPrometheusMetricsBuilder(registry, "billing", "watermill")
builder.AddPrometheusRouterMetrics(router) // 处理耗时、消费数、发布延迟
http.Handle("/metrics", promhttp.HandlerFor(registry, promhttp.HandlerOpts{}))
```

---

## 5. 版本演进

| 版本 | 时间 | 变化 |
|---|---|---|
| 1.0 – 1.2 | 2019 – 2022 | 核心定型：`Message` / `Publisher` / `Subscriber` / `Router` 与中间件体系，此后 API 基本未破 |
| 1.3 | 2023 | 新增 `requestreply`（含泛型结果）、`CircuitBreaker`、slog 适配；`watermill-sql` 升 v2 修掉丢消息问题 |
| 1.4.x | 2024 – 2025 | 维护线，v1.4.7（2025-07）为末版；各适配器跟进新 broker 客户端 |
| 1.5.0 / 1.5.1 | 2025-08 / 2025-09 | 进入 1.5 主线，`watermill-kafka/v3` 持续跟进 Sarama v1.4x |
| 1.5.2 | 2026-05 | 文档补齐 `AddConsumerHandler`、SQLite / MySQL 延迟消息；`Retry` 增加 `OnRetriesExhausted` 钩子（重试耗尽时做清理） |
| 1.5.3 | 2026-08 | forwarder 支持自定义 handler 名；NATS 文档更新（截至 2026-11，最新版本） |

升级建议：`1.4.x → 1.5.x` 是兼容升级，改 `go.mod` 即可；跨大版本主要是适配器的 module 路径（`watermill-kafka/v2` → `/v3` 属于 Sarama 大版本跟进）与 `watermill-sql` v1 → v2 的**表结构不兼容**，后者必须新建表 + 数据迁移，不能原地改。

---

## 6. 常见坑与最佳实践

1. **handler 不幂等 + 自动重试 = 重复副作用**。这是最高频的线上事故：handler 发完短信后进程 OOM，重启后消息重投，客户收到两条。所有副作用都要有幂等键（`msg.UUID` 或业务主键）。
2. **忘记 `defer router.Close()`（或没装 `plugin.SignalsHandler`）**。容器收到 SIGTERM 直接退出，在途消息被 Nack，重启后集中重投，还可能出现「发布了下游消息但没 ack 上游」的重复。优雅关闭是必选项，且 `CloseTimeout` 要小于 K8s 的 `terminationGracePeriodSeconds`。
3. **handler 阻塞导致整个分区停摆**。Router 每个 handler 一个 goroutine，串行处理自己的订阅。handler 里做 `time.Sleep`、同步 HTTP 调用、大文件解析，Kafka 侧直接表现为 lag 飙升。慢活拆成独立 topic + 独立消费者。
4. **没配 PoisonQueue，毒消息无限重投**。一条反序列化不了的消息会 Nack → 重投 → 又失败，永远卡在分区头部，后面的消息全部堵死。死信队列不是可选件。
5. **Metadata 丢字段**。自己实现中间件或 Publisher 时忘记复制 `msg.Metadata`，`correlation_id`、`partition` 静默消失。产出的新消息应显式 `for k, v := range msg.Metadata` 或依赖 `CorrelationID` 中间件；跨 broker 时 metadata 的映射规则要看适配器实现。
6. **Kafka 分区键不设，下游乱序**。`DefaultMarshaler` 不设置 key，消息被轮询到各分区，同一订单的 `paid` / `shipped` 可能被两个消费者并发处理。用 `kafka.NewWithPartitioningMarshaler` 或把 key 放 Metadata 再映射。
7. **长任务超过 session timeout**。handler 跑 2 分钟，超过 Kafka `session.timeout` / `max.poll.interval`，组内判定该消费者死亡触发 rebalance，分区被抢走又重投，形成「永远处理不完」的循环。长任务要异步化或者显著调大超时。
8. **ConsumerGroup 忘了设**。Kafka 适配器里 `ConsumerGroup` 为空表示「消费所有分区的全量消息」，多副本部署时每个实例都收到同一份——看着像重复投递，实际是配置错。
9. **Retry 阻塞占用 goroutine，且预算过大**。`MaxRetries` 大 + 退避长，等于这个 handler 长时间不可用；结合 `Timeout` 使用，并让总重试时长明显小于 broker 的 session timeout。
10. **生产用了 `gochannel`**。内存实现重启即丢，且没有跨进程投递。它只适合单测和本地跑通链路，上线前必须换真 broker。
11. **数据库提交与消息发布不是原子的**。`publisher.Publish` 成功、`tx.Commit` 失败，或者反过来，都会造成状态不一致。需要强一致就用 outbox：业务数据与 outbox 记录同事务写入，后台 forwarder 转发。
12. **把抽象层当成「屏蔽一切」**。Watermill 不处理 schema 演进、消息大小上限、ACL、保留策略、积压监控，这些依然要按 broker 的规矩来；出问题时日志里只有 topic 和 UUID，必须自己补指标和链路追踪，否则排查比直接写 SDK 更痛苦。

---

## 7. 选型对比

| 维度 | 直接用 broker SDK | go-micro / broker | 自研抽象层 | Watermill | Temporal |
|---|---|---|---|---|---|
| 抽象层次 | 无，直接面对协议 | 轻，仅 pub/sub 接口 | 取决于投入 | 中高：消息 + 路由 + 中间件 + CQRS | 高：工作流/活动编排 |
| 换 broker 成本 | 重写消费循环 | 换插件，但语义仍自管 | 看设计 | 换适配器构造几行 | 与 broker 无关 |
| 重试 / 死信 | 自己实现 | 基本没有 | 自己实现 | 内置 Retry / PoisonQueue | 内置，按活动重试 |
| ack 控制粒度 | 完全可控 | 弱 | 可控 | 中，Router 托管 | 不适用 |
| 可调试性 | 最好，能看到每条细节 | 中 | 中 | 偏差，需要补日志与追踪 | 好，有可视化历史 |
| 性能开销 | 基准 | 低 | 低 | 小（多一层函数调用与 metadata 复制） | 大（持久化状态机） |
| 适用场景 | 单 broker、极致性能、特殊协议特性 | 老项目、要极轻 | 平台团队、有长期维护预算 | 多 broker、微服务事件驱动、要 CQRS | 长事务、Saga、需要人工介入的流程 |

几条判断规则：

- **只用一个 broker，且要用事务、精确一次、分层存储等专有特性**——直接写 SDK（franz-go / nats.go），抽象层只会让你离细节更远；
- **要跨 broker 迁移、或团队里多个服务各用各的 MQ**——Watermill 的收益最大，中间件与 CQRS 是白送的；
- **需要「流程状态」而不只是「消息投递」**——长跑几天的订单编排、需要补偿与人工审批，用 Temporal；它解决的是另一个维度的问题，两者可以并存（Watermill 传事件，Temporal 管流程）；
- **自研抽象层**只在有平台团队长期维护时成立，否则半年后就是一套没人敢改的私有框架。

一句话：**Watermill 买的是「消费循环 + 重试 + 死信 + 优雅关闭 + CQRS」这套骨架的标准实现，代价是失去对 broker 细节的直接掌控**。骨架值钱就上，细节值钱就别上。
