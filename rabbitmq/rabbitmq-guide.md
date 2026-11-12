# RabbitMQ 与 RocketMQ 技术介绍与使用指南（传统消息中间件）

> 业务消息队列的两大代表：RabbitMQ 的 Exchange / Queue 灵活路由，RocketMQ 的顺序、事务与定时消息——补齐 [Kafka](../kafka/kafka-guide.md) 事件流之外的「业务可靠投递」拼图。
> 上游：<https://github.com/rabbitmq/rabbitmq-server> · <https://github.com/apache/rocketmq>　|　主线：RabbitMQ **4.x** / RocketMQ **5.x**（整理于 2026-11）
> Go 客户端：[rabbitmq/amqp091-go](https://github.com/rabbitmq/amqp091-go) v1.x · [apache/rocketmq-client-go](https://github.com/apache/rocketmq-client-go) v2（5.x 另有 [rocketmq-clients/golang](https://github.com/apache/rocketmq-clients)）
> 协议：RabbitMQ MPL-2.0 / RocketMQ Apache-2.0；具体版本以上游最新 release 为准。

---

## 1. 项目简介

仓库里已有三种「异步」组件，先分清边界，否则选型必然重复：

| 组件 | 本质 | 消费后 | 典型问题 |
|---|---|---|---|
| [Kafka](../kafka/kafka-guide.md) | **可回放的事件日志** | 不删除，按 offset 保留 | 事件流、数据管道 |
| [NATS](../nats/nats-guide.md) | **轻量消息总线** | 不落盘（Core） | 内网 RPC / 低延迟总线 |
| [asynq](../asynq/asynq技术文档与使用手册.md) | **任务队列** | 完成即删 | 有副作用、要重试/死信的作业 |
| **RabbitMQ / RocketMQ** | **业务消息队列** | ACK 后删除 | 服务解耦、削峰、可靠投递、顺序与事务消息 |

RabbitMQ 与 RocketMQ 同属「传统 MQ」，但设计取向不同：**RabbitMQ 强在路由灵活与协议/生态成熟**（AMQP、多语言、交换机模型），**RocketMQ 强在海量堆积、顺序消息、事务消息与定时消息**（为电商交易场景而生，CommitLog 顺序写）。

- **适用场景**：业务事件广播、削峰填谷、最终一致（本地消息表 / 事务消息）、顺序处理（订单状态流转）、延时任务、跨系统解耦
- **不适合**：需要长期回放与多消费者独立位点（用 Kafka）、超低延迟内网 RPC（用 NATS）、需要任务级重试/死信/优先级/Web UI（用 asynq）、纯缓存（用 [go-redis](../redis/redis-guide.md)）

### 安装与本地起服务

```bash
go get github.com/rabbitmq/amqp091-go
go get github.com/apache/rocketmq-client-go/v2

docker run --rm -d --name rmq -p 5672:5672 -p 15672:15672 rabbitmq:4-management
# 管理台 http://localhost:15672  (guest/guest)

docker run --rm -d --name rmq-namesrv -p 9876:9876 apache/rocketmq:5 sh mqnamesrv
# Broker 需另起并指向 namesrv，详见官方文档
```

### 最小可运行示例（RabbitMQ）

```go
package main

import (
    "context"
    "log"

    amqp "github.com/rabbitmq/amqp091-go"
)

func main() {
    conn, err := amqp.Dial("amqp://guest:guest@localhost:5672/")
    if err != nil { log.Fatal(err) }
    defer conn.Close()
    ch, err := conn.Channel() // channel 是 TCP 连接上的轻量多路复用
    if err != nil { log.Fatal(err) }
    defer ch.Close()

    if err := ch.ExchangeDeclare("orders", "topic",
        true, false, false, false, nil); err != nil { log.Fatal(err) }
    q, err := ch.QueueDeclare("order.created",
        true, false, false, false, nil)
    if err != nil { log.Fatal(err) }
    if err := ch.QueueBind(q.Name, "order.created", "orders",
        false, nil); err != nil { log.Fatal(err) }

    err = ch.PublishWithContext(context.Background(), "orders", "order.created",
        false, false, amqp.Publishing{
            ContentType:  "application/json",
            DeliveryMode: amqp.Persistent, // 持久化消息
            Body:         []byte(`{"order_id":"A1001"}`),
        })
    if err != nil { log.Fatal(err) }
    log.Println("published")
}
```

---

## 2. 功能特性总览

| 能力 | RabbitMQ 4.x | RocketMQ 5.x |
|---|---|---|
| 核心模型 | Producer → Exchange → Binding → Queue → Consumer | Producer → Topic → MessageQueue → Consumer Group |
| 路由 | **direct / topic / fanout / headers 交换机**，路由键与绑定极灵活 | Topic + Tag 过滤，路由相对简单 |
| 顺序 | 单队列单消费者可保序（并发消费者不保序） | **分区顺序消息**（同业务 ID 进同一 MessageQueue）+ 顺序消费锁 |
| 事务 | 生产者事务（性能差）/ Publisher Confirm | **事务消息**（半消息 + 本地事务 + 回查），金融级常用 |
| 定时/延时 | 延迟交换机插件 / TTL + DLX（有队头阻塞问题） | **原生定时/延时消息**（5.x 支持任意精度定时） |
| 重试与死信 | DLX 死信交换机 + `nack(requeue)` | `%RETRY%` 重试队列（16 级退避）+ `%DLQ%` 死信队列 |
| 确认机制 | 手动 `ack` / `nack` / `reject` + `prefetch` | 消费位点提交（集群/广播模式），失败返回 `RECONSUME_LATER` |
| 持久化 | 队列/消息 durable + Publisher Confirm | CommitLog 顺序写 + 同步/异步刷盘 + 主从复制 |
| 高可用 | **Quorum Queue**（Raft）替代已移除的镜像队列 | NameServer 无状态发现 + Broker 主从 / DLedger 控制器 |
| 协议 | AMQP 0-9-1、STOMP、MQTT、Stream 插件 | 自有 Remoting 协议 / 5.x gRPC Proxy |
| 管理 | Web 管理台、Prometheus 插件、丰富 CLI | Dashboard、ACL、轨迹（消息轨迹追踪） |
| 典型场景 | 复杂路由、企业集成、多语言混部 | 交易链路、顺序事件、海量堆积、事务消息 |

---

## 3. 核心概念与原理

### 3.1 RabbitMQ：交换机模型

```
Producer ──(routing key)──► Exchange
                              │ 按类型 + Binding 规则
             ┌────────────────┼────────────────┐
             ▼                ▼                ▼
          Queue A          Queue B          Queue C   ──► Consumer
        (fanout 广播 / direct 精确 / topic 通配 / headers 按头匹配)
```

- **Exchange 不存消息**，只做路由；消息最终落在 Queue
- **vhost** 提供逻辑隔离（类似命名空间），连接时指定
- **Channel** 是 TCP 连接内的逻辑通道，多 goroutine 各有自己的 Channel；**Channel 不是线程安全的**，不要跨 goroutine 共享
- 一条消息可被复制到多个 Queue（广播），每个 Queue 各自维护 ACK 与消费进度

**消息可靠的三道闸门**（缺一不可）：Exchange / Queue / 消息都设 `durable` + 生产者开启 **Publisher Confirm** + 消费者**手动 ACK**。

### 3.2 RabbitMQ：Confirm、ACK 与 DLX

```
Publisher ──confirm──► Broker 落盘 ──► Queue ──deliver──► Consumer
                                                            │
                           ack（成功）/ nack(requeue=true)（重投）│
                                                            ▼
                         nack(requeue=false) 或 TTL 过期 ──► DLX 死信交换机 ──► 死信队列
```

`prefetch`（QoS）限制每个消费者未 ACK 的最大条数：设太大导致某些消费者堆积、设太小吞吐上不去；通常 10~100 并按处理耗时压测。

### 3.3 RocketMQ：CommitLog 与索引

```
Producer ──► Broker
              ├─► CommitLog（所有 Topic 共用的顺序追加文件，1G/个）
              │      → 顺序写 + mmap 零拷贝，这是海量堆积仍稳定的根本原因
              ├─► ConsumeQueue（Topic/Queue → CommitLog 偏移的消费索引）
              └─► IndexFile（按 Message Key 建哈希索引，用于轨迹查询）

NameServer（无状态，各节点独立） ── 提供 Topic 路由元数据
```

消费时先读轻量的 ConsumeQueue 拿到偏移，再从 CommitLog 取消息体——**逻辑队列与物理存储解耦**，所以队列数可以很多而文件不会爆炸。这与 [Kafka 篇](../kafka/kafka-guide.md) 的「分区即日志文件」是两种不同的取舍。

### 3.4 RocketMQ：顺序、事务与定时消息

**顺序消息**：发送时用 `MessageQueueSelector`（按 `orderID` 哈希选队列）配合 `SendSyncSharding` 把同一业务 ID 固定到同一 MessageQueue；消费端用 `consumer.WithConsumeMode(consumer.Orderly)` 开启顺序消费（对队列加锁，失败会阻塞整个队列）。

**事务消息**（解决「本地事务成功但消息没发出去」）：

```
1. 发送 Half Message（对消费者不可见）
2. 执行本地事务（DB 写入）
3. COMMIT → 消息可见 / ROLLBACK → 丢弃
4. 若 2 的结果没回来，Broker 回查 Producer 的 checkLocalTransaction
   → 必须实现回查接口，且本地事务结果要可查询（事务状态表）
```

**定时/延时消息**：5.x 支持任意时刻定时（4.x 只有固定 18 个延迟级别）。比 RabbitMQ 的 TTL+DLX 方案更精确、无队头阻塞。

### 3.5 两者与 Kafka 的本质差异

| | RabbitMQ / RocketMQ | Kafka |
|---|---|---|
| 消费模型 | **队列**：ACK 后删除，消费者竞争消费 | **日志**：按 offset 独立位点，可重复读 |
| 多消费者 | 广播需复制到多个队列（RabbitMQ）/ 多个 Group（RocketMQ） | 天然多 Group 独立消费同一份数据 |
| 堆积成本 | 队列堆积会影响性能（RabbitMQ 尤其明显） | 顺序日志，堆积成本低 |
| 路由 | 强 | 弱（Topic + Partition） |
| 事务/顺序/延时 | **内建丰富** | 需业务实现 |

---

## 4. 使用实战

### 4.1 RabbitMQ：持久化 + Publisher Confirm

```go
func Setup(ch *amqp.Channel) error {
    if err := ch.Confirm(false); err != nil { return err } // 开启 confirm 模式
    return ch.Qos(20, 0, false)                            // 每次最多 20 条未 ACK
}

func Publish(ctx context.Context, ch *amqp.Channel, body []byte) error {
    confirm := ch.GetNextPublishSeq() // 需要自行维护序号与确认映射，或使用封装库
    _ = confirm
    return ch.PublishWithContext(ctx, "orders", "order.created",
        true,  // mandatory：无法路由时返回，避免静默丢消息
        false,
        amqp.Publishing{
            DeliveryMode: amqp.Persistent,
            MessageId:    uuid.NewString(), // 消费端幂等去重键
            Timestamp:    time.Now(),
            Body:         body,
        })
}
```

> `mandatory=true` + Confirm/Return 监听是生产必备，否则 routing key 写错时消息会被**静默丢弃**。上面的 Confirm 序号处理是示意，实际项目建议用成熟封装（`wagslane/go-rabbitmq` 等）或自行维护 `map[uint64]chan` 做确认与超时重发。

### 4.2 RabbitMQ：手动 ACK 消费

```go
func Consume(ctx context.Context, ch *amqp.Channel, queue string) error {
    msgs, err := ch.Consume(queue, "order-worker",
        false, // autoAck = false，必须手动确认
        false, false, false, nil)
    if err != nil { return err }
    for {
        select {
        case <-ctx.Done(): return nil
        case d, ok := <-msgs:
            if !ok { return errors.New("channel closed") }
            if err := handle(ctx, d.Body); err != nil {
                // requeue=true 会立刻重投，容易形成死循环；
                // 生产更常见是 requeue=false 交给 DLX，并按次数退避
                _ = d.Nack(false, false)
                continue
            }
            _ = d.Ack(false)
        }
    }
}
```

死信与延时队列声明：

```go
args := amqp.Table{
    "x-dead-letter-exchange":    "orders.dlx",
    "x-dead-letter-routing-key": "dead",
    "x-max-length":              100000, // 防无限堆积打满磁盘
}
_, err := ch.QueueDeclare("order.created", true, false, false, false, args)
```

> **TTL + DLX 做延时有队头阻塞**：队列头是 1 小时后才过期的消息，后面 5 秒的消息也得等。精确延时请用 `rabbitmq_delayed_message_exchange` 插件，或换 [asynq](../asynq/asynq技术文档与使用手册.md) / RocketMQ 定时消息。

### 4.3 RocketMQ：生产与消费（client-go v2）

```go
package mq

import (
    "context"

    "github.com/apache/rocketmq-client-go/v2"
    "github.com/apache/rocketmq-client-go/v2/consumer"
    "github.com/apache/rocketmq-client-go/v2/primitive"
    "github.com/apache/rocketmq-client-go/v2/producer"
)

var nameSrvs = []string{"127.0.0.1:9876"}

func StartProducer() (rocketmq.Producer, error) {
    p, err := rocketmq.NewProducer(
        producer.WithNameServer(nameSrvs),
        producer.WithGroupName("order-producer"),
        producer.WithRetry(2),
    )
    if err != nil { return nil, err }
    return p, p.Start()
}

func SendOrder(ctx context.Context, p rocketmq.Producer, body []byte, orderID string) error {
    msg := primitive.NewMessage("OrderTopic", body).
        WithTag("created").
        WithKeys([]string{orderID}) // Keys 用于消息轨迹查询与幂等
    _, err := p.SendSync(ctx, msg)
    return err
}

func StartConsumer() error {
    c, err := rocketmq.NewPushConsumer(
        consumer.WithNameServer(nameSrvs),
        consumer.WithGroupName("order-consumer"),
        consumer.WithConsumeFromWhere(consumer.ConsumeFromFirstOffset),
    )
    if err != nil { return err }
    if err := c.Subscribe("OrderTopic", consumer.MessageSelector{
        Type: consumer.TAG, Expression: "created || paid",
    }, func(ctx context.Context, msgs ...*primitive.MessageExt) (consumer.ConsumeResult, error) {
        for _, m := range msgs {
            if err := handle(ctx, m.Body); err != nil {
                // 返回重试；超过最大重试次数自动进 %DLQ%
                return consumer.ConsumeRetryLater, nil
            }
        }
        return consumer.ConsumeSuccess, nil
    }); err != nil { return err }
    return c.Start()
}
```

> 顺序消息要用 `SendSyncSharding` + `MessageQueueSelector`（按业务 ID 选队列）并配 `consumer.WithConsumeMode(consumer.Orderly)`；**顺序消费失败会阻塞整个队列**，重试策略要谨慎。5.x 新客户端改用 `rocketmq-clients/golang/v5`（gRPC + Proxy），API 与 v2 不兼容，迁移需单独评估。`rocketmq-client-go/v2` 的小版本间选项名也有差异（如 `WithNameServer` / `WithNameSrvs`），请以所用版本的 godoc 为准。

### 4.4 幂等消费（两者通用）

所有主流 MQ 的语义都是 **at-least-once**，重复投递是常态。两种可靠去重方式：

```sql
-- 方案 A：消费记录表，与业务写入放在同一个本地事务里（推荐）
CREATE TABLE consumed_msg (
    msg_id VARCHAR(64) PRIMARY KEY,
    consumed_at TIMESTAMP NOT NULL
);
```

```go
func handle(ctx context.Context, body []byte, msgID string) error {
    tx, err := db.BeginTx(ctx, nil)
    if err != nil { return err }
    defer tx.Rollback()
    // 1. 尝试插入消费记录；主键冲突说明已处理过，直接返回成功
    if _, err := tx.ExecContext(ctx,
        "INSERT INTO consumed_msg(msg_id, consumed_at) VALUES (?, NOW())", msgID); err != nil {
        if isDuplicateKey(err) { return nil } // 重复消息，幂等吞掉
        return err
    }
    // 2. 业务写入（同一事务）
    if err := doBusiness(ctx, tx, body); err != nil { return err }
    return tx.Commit()
}
```

方案 B：用 [go-redis](../redis/redis-guide.md) 的 `SET NX` 加去重窗口，适合允许极端情况下重复的业务。

### 4.5 运维速查

```bash
# RabbitMQ
rabbitmqctl list_queues name messages_ready messages_unacknowledged consumers
rabbitmqctl list_connections name channels
rabbitmq-diagnostics -q check_running
# 关键指标：队列深度、unacked、消息入/出速率、连接/channel 数

# RocketMQ
sh mqadmin topicRoute -n 127.0.0.1:9876 -t OrderTopic
sh mqadmin consumerProgress -n 127.0.0.1:9876 -g order-consumer
# 关键指标：消息堆积量（diff）、消费 TPS、发送 RT、DLQ 数量
```

---

## 5. 版本演进速览

| 组件 | 变化与迁移注意点 |
|---|---|
| RabbitMQ 3.x | 镜像队列（Classic Mirrored Queue）做 HA；Quorum Queue 引入并逐步成熟 |
| **RabbitMQ 4.x** | **镜像队列移除**，Quorum Queue 成为推荐 HA 方案；Stream 插件（可回放日志，补齐与 Kafka 的差距）；Khepri 替代 Mnesia 存元数据；管理台与 Prometheus 指标增强 |
| RocketMQ 4.x | CommitLog + ConsumeQueue 架构成熟；固定 18 级延迟消息；事务消息与顺序消息；`rocketmq-client-go/v2` 广泛使用 |
| **RocketMQ 5.x** | **任意精度定时消息**、gRPC Proxy 与存算分离、Controller/DLedger 替代手工主从、新多语言客户端 `rocketmq-clients`；消息粒度与可观测增强 |

**迁移注意**：

- RabbitMQ 3.x → 4.x：镜像队列策略失效，必须迁到 Quorum Queue（注意 Quorum Queue **不支持**优先级、部分 TTL 行为不同）
- `streadway/amqp` 已归档，统一迁到 `rabbitmq/amqp091-go`（API 大体兼容，主要改 import 路径）
- RocketMQ v2 Go 客户端与 5.x 的 `rocketmq-clients/golang` **不是同一个库**，新项目要先定用哪套
- Broker / NameServer / Proxy 版本需配套升级，跨大版本混用要查兼容性矩阵

---

## 6. 常见坑与最佳实践

1. **只做一半持久化**：队列或消息没设 `durable`，或没开 Publisher Confirm，Broker 重启即丢消息。三道闸门必须齐全。
2. **`mandatory=false` 静默丢消息**：routing key 写错 / 无绑定队列时消息被直接丢弃，生产毫无察觉。设 `true` 并监听 Return。
3. **自动 ACK**：`autoAck=true` 时消息一发出就算完成，处理失败直接丢失。一律手动 ACK。
4. **无脑 `nack(requeue=true)`**：失败消息立刻重投，形成无限循环打满 Broker。用 DLX + 重试次数计数 + 退避，或交给 asynq 这类任务队列。
5. **prefetch 设置不当**：太大导致单个消费者堆积、负载不均；太小吞吐不足。按处理耗时压测，常从 10~100 起步。
6. **Channel 跨 goroutine 共享**：AMQP Channel 非线程安全。每个 goroutine 用自己的 Channel，共享 Connection。
7. **队列无限堆积**：不设 `x-max-length` / 不监控深度，磁盘打满导致整个 Broker 停写。设上限 + 告警 + 降级（转储或丢弃非关键消息）。
8. **TTL+DLX 做精确延时**：队头阻塞导致延时不准。用延迟交换机插件、RocketMQ 定时消息，或 [asynq](../asynq/asynq技术文档与使用手册.md) 的 `ProcessIn`。
9. **RabbitMQ 还在用镜像队列**：4.x 已移除，且镜像队列在脑裂与同步阻塞上有已知问题。迁 Quorum Queue。
10. **顺序消息与重试冲突**：RocketMQ 顺序消费失败会阻塞整个 MessageQueue。要么快速失败进死信由人工处理，要么业务层自己保证幂等后用并发消费。
11. **扩容 MessageQueue 破坏顺序**：队列数变化会改变分片映射，同一业务 ID 可能落到不同队列。顺序场景中途扩队列要谨慎。
12. **事务消息不实现回查**：`checkLocalTransaction` 缺失或返回结果不可靠，半消息会一直挂起。必须落本地事务状态表供回查。
13. **消费不幂等**：at-least-once 是默认语义，网络抖动、Rebalance、消费者重启都会重复。用消费记录表与业务写入同事务（见 4.4）。
14. **消息体过大**：大对象塞进消息（图片、整张订单快照）会拖慢 Broker 与复制。只传 ID + 事件类型，详情回落存储。
15. **用 MQ 替代分布式事务**：发了消息就当事务提交成功。正确姿势是本地消息表 / 事务消息 / Saga，并接受最终一致。
16. **消费者组位点配置错误**：RocketMQ 广播模式不保存位点、重启丢进度；`ConsumeFromWhere` 在新 Group 首次启动时才生效，别指望它"重放"历史。
17. **只监控 Broker 不监控消费延迟**：堆积量（diff）与消费 TPS 才是业务是否健康的信号，配合 [Prometheus](../prometheus/prometheus-guide.md) 与 [OpenTelemetry](../opentelemetry/opentelemetry-guide.md) 把消息链路串起来。
18. **连接不关闭 / 不复用**：每次发布新建 Connection，握手开销大且耗尽句柄。全局复用 Connection，Channel 按 goroutine 创建并关闭。

---

## 7. 选型对比

| 维度 | RabbitMQ | RocketMQ | Kafka | NATS JetStream | asynq | Redis Stream |
|---|---|---|---|---|---|---|
| 模型 | Exchange + Queue | Topic + MessageQueue | 分区日志 | Subject + Stream | 任务队列 | Stream |
| 路由能力 | **最强** | 中（Tag） | 弱 | 中（Subject） | 弱 | 弱 |
| 顺序 / 事务 / 定时 | 定时较弱 | **最全** | 需自实现 | 部分 | **任务级最全** | 弱 |
| 回放 / 多消费者位点 | 弱（Stream 插件可补） | 弱 | **最强** | 强 | 无 | 中 |
| 堆积成本 | 中（怕堆积） | **低** | **低** | 中 | 中 | 高（内存） |
| 运维复杂度 | 低 | 中～高 | 中～高 | **低** | **极低** | 极低 |
| 外部依赖 | Erlang 节点 | NameServer + Broker | KRaft 集群 | 单二进制 | Redis | Redis |
| 典型场景 | 复杂路由、企业集成、多语言 | 交易链路、顺序/事务消息 | 事件流、数据管道 | 云原生轻量总线 | 异步作业 | 轻量流 |

一句话决策：

- **路由规则复杂、多语言混部、需要成熟管理台与快速落地** → **RabbitMQ**，中小规模最省心。
- **交易/订单链路、海量堆积、顺序 + 事务 + 定时消息** → **RocketMQ**，尤其在国内电商与金融生态里资料最全。
- **事件流、日志管道、需要回放与流处理生态** → [Kafka](../kafka/kafka-guide.md)。
- **云原生内网总线、RPC、极低运维** → [NATS](../nats/nats-guide.md)。
- **「执行一个作业」而不是「广播一条消息」**（发邮件、转码、重试、死信、优先级、Web UI）→ [asynq](../asynq/asynq技术文档与使用手册.md)，别在 RabbitMQ 上重造这套轮子。
- **已经只有 Redis、量级很小** → Redis Stream 够用，但要自己处理重试、死信与监控。

> 一个反复出现的架构错误：把「业务作业」和「业务事件」塞进同一个 MQ。事件是「发生了什么，谁关心谁订阅」，作业是「这件事必须被执行且只成功一次」。前者用 RabbitMQ/RocketMQ/Kafka，后者用 asynq——两者可以共存，也正是常见组合：MQ 广播事件 → 消费端把具体作业入队到 asynq。
