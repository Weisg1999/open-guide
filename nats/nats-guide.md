# NATS 与 JetStream 技术介绍与使用指南

> 云原生时代的轻量级消息系统：单二进制、Subject 寻址、核心即双向消息总线，JetStream 补齐持久化。
> 仓库：<https://github.com/nats-io/nats-server>　|　当前主线：v2.11（2025.3.19）

---

## 1. 项目简介

NATS 起源于 2010 年（被 Apcera 捐赠，后成立 Synadia 维护），设计哲学是**极简与性能**：

- **单二进制**约 15MB，`nats-server` 一条命令即启动，无外部依赖；
- **Subject 寻址**：`orders.new`、`orders.*`、`orders.>`——发布订阅即开即用；
- **Core NATS**（核心模式）是 **at-most-once** 的极低延迟总线（进程内同级的开销）；
- **JetStream**（2.2+ 内置）在核心之上提供**持久化流存储**：at-least-once / exactly-once、重放、副本复制——对标 Kafka/RabbitMQ 的持久化能力。

常见定位：

| 模式 | 替代/对标 |
|---|---|
| Core NATS 请求-响应 | 轻量 RPC（替代裸 HTTP/gRPC 内网调用） |
| Core NATS 发布订阅 | 事件总线 |
| JetStream | 轻量级 Kafka / RabbitMQ |
| KV / Object Store | 分布式 KV / 小对象存储（建在 JetStream 上） |

与 Kafka 的取舍：NATS 胜在**运维成本几乎为零、协议简单、延迟低**；Kafka 胜在**超大规模吞吐、生态连接器、流处理**。中小规模微服务/AI Agent 总线，NATS 是当下热门选择。

### 启动

```bash
# 纯核心模式
nats-server

# 开启 JetStream（-js）+ 监控端口（-m 8222）
nats-server -js -m 8222

# 安装 CLI（强烈推荐）
# nats stream add / nats consumer add / nats pub / nats sub ...
```

Go 客户端：`go get github.com/nats-io/nats.go`

---

## 2. 核心概念

### 2.1 Subject：消息的地址

```
orders.new          精确
orders.*            * 匹配一个 token：orders.new / orders.paid（不匹配 orders.eu.new）
orders.>            > 匹配多级 token：orders.eu.new 也可
```

- 以 `.` 分隔 token，`*` 与 `>` 只能作为**整个 token** 出现；
- 无需预先声明 topic——发往没人订阅的 subject 就是"发了个寂寞"（Core NATS 直接丢弃）。

### 2.2 Core NATS 三种通信模式

```
① 发布订阅：           ② 队列组（负载均衡）：      ③ 请求-响应：
   pub                  pub                    requester
    │ msg                │ msg                    │ request("user.get", payload)
    ├─► sub A            └─► queue group          ▼
    ├─► sub B                  ├─► sub A      service: sub "user.get"
    └─► sub C                  └─► sub B           │ reply msg
                                                      └─► requester (m.Respond)
```

- **发布订阅**：所有匹配订阅者各得一份（广播）；
- **队列组（Queue Group）**：同名组内只有一个成员收到（工作队列语义），自动负载均衡；
- **请求-响应**：内置 inbox 机制（`_INBOX.xxxx`），天然支持微服务 RPC，支持超时与多应答扇出。

**Core NATS 是 at-most-once**：无持久化、无 ack、订阅者断线期间的消息不保留——要可靠性上 JetStream。

### 2.3 JetStream：流与消费者

```
Stream: ORDERS（subjects: orders.*）
  [seq 1][seq 2][seq 3][seq 4]...   ← 全局递增序列，按保留策略持久化
        ▲
        └── Consumer: billing（filter: orders.paid，durable，ack explicit）
        └── Consumer: audit（filter: orders.*，durable，ack all）
```

- **Stream**：消息的持久化日志（类似 Kafka topic），用 **subject 通配符**圈定收录哪些消息（一条流可对应多个 subject——比 Kafka 灵活）；
- **Retention（保留策略）**：
  - `Limits`：按条数/大小/时间保留（Kafka 式）；
  - `Interest`：所有（当前）消费者 ack 后即删；
  - `WorkQueue`：一条消息被一个消费者 ack 即删（队列语义）；
- **Consumer（消费者）**：从流中读的视图，两种交付方式：
  - **Push**：服务端推给客户端（旧主推，简单场景）；
  - **Pull**：客户端主动拉（生产推荐，背压可控）；
  - 持久化（durable）消费者记录自己的进度（类似 consumer group offset）；
- **Ack 策略**：explicit（处理完逐条 ack）/ none / all（批量 ack）——**ack explicit + durable = at-least-once**；
- **Exactly-once**：发布去重（`Nats-Msg-Id` 头 + duplicate window）+ 双重确认窗口。

### 2.4 JetStream 之上的高层 API

- **KV Store**：`nats.KV("config")`——最终一致的分布式 KV，`Watch` 监听变更（配置中心）；
- **Object Store**：小对象存储（分片进流）；
- **两者本质都是特殊配置的 Stream**——一套内核多种用法。

### 2.5 集群与超大集群

- **Cluster**：3/5 节点，流以 Raft 副本形式复制（`replay`/`R3`）；
- **Supercluster / Gateways**：跨地域联邦，客户端就近接入；
- **Leaf Nodes**：边缘节点桥接到中心集群——物联网/边缘场景利器，2.11 还支持 TLS-first 握手。

---

## 3. 原理速览

- 通信基于自定义 TCP 文本协议（`PUB orders.new 5\r\nhello\r\n`），实现极简——这是低延迟的来源；
- 服务器间（cluster route）使用 gossip 同步订阅兴趣，消息按需转发（**interest-driven**），Core 模式不落盘；
- JetStream 写入路径：请求 → leader 判断（subject 归属）→ Raft 复制到副本 → 文件存储（filestore，消息按块顺序写 + 索引）→ ack 应答；
- 每个 stream 在集群内有 leader 节点，写入需多数派确认（与 etcd 类似的 quorum 语义）。

---

## 4. 使用实战（Go）

### 4.1 Core NATS：pub/sub、队列组、请求响应

```go
nc, _ := nats.Connect(nats.DefaultURL) // nats://127.0.0.1:4222
defer nc.Drain()

// 订阅
nc.Subscribe("orders.*", func(m *nats.Msg) {
    fmt.Printf("收到 %s: %s\n", m.Subject, string(m.Data))
})

// 发布
nc.Publish("orders.new", []byte(`{"id":1}`))

// 队列组：同组只有一个收到（水平扩展 worker）
nc.QueueSubscribe("jobs.email", "email-workers", func(m *nats.Msg) {
    m.Respond([]byte("done")) // 可选应答
})

// 请求-响应（超时 2s）
msg, err := nc.Request("user.get", []byte(`{"id":42}`), 2*time.Second)
```

### 4.2 JetStream：发布与流管理

```go
js, _ := jetstream.New(nc)

// 创建/更新流
js.CreateStream(context.Background(), jetstream.StreamConfig{
    Name:     "ORDERS",
    Subjects: []string{"orders.*"},
    Storage:  jetstream.FileStorage,  // 文件存储（还有 MemoryStorage）
    Replicas: 3,                      // R3 集群副本
    MaxAge:   72 * time.Hour,         // Limits 保留 72h
})

// 发布（带去重 ID + 消息级 TTL，2.11+）
js.Publish(context.Background(), "orders.paid", []byte(`{"id":1}`),
    jetstream.WithMsgID("order-1"),      // 服务端按 MsgID 去重
    jetstream.WithTTL(10*time.Minute),   // 单条消息 TTL（需流开启 per-message TTL）
)
```

### 4.3 JetStream：持久消费者（生产范式）

```go
// 创建持久 pull 消费者（相当于 Kafka 消费组 + 订阅过滤合体）
cons, _ := js.CreateOrUpdateConsumer(context.Background(), "ORDERS",
    jetstream.ConsumerConfig{
        Durable:       "billing",
        FilterSubject: "orders.paid",
        AckPolicy:     jetstream.AckExplicitPolicy, // at-least-once
        MaxDeliver:    5,                           // 最多投递 5 次（超过进 DLQ 自建）
    },
)

// 消费循环
cc, _ := cons.Consume(func(msg jetstream.Msg) {
    if err := process(msg); err != nil {
        msg.Nak() // nak：重新投递
        return
    }
    msg.Ack()     // ack：确认
})
defer cc.Stop()
```

### 4.4 KV：一行配置中心

```go
kv, _ := js.CreateKeyValue(context.Background(), jetstream.KeyValueConfig{
    Bucket: "app-config",
})
kv.Put(ctx, "db.host", []byte("mysql-1"))
v, _ := kv.Get(ctx, "db.host")
watch, _ := kv.Watch(ctx, "db.host")
for upd := range watch.Updates() { /* 配置热更新 */ }
```

### 4.5 CLI 常用命令

```bash
nats pub orders.new '{"id":1}'
nats sub 'orders.*'
nats request user.get '{"id":42}'

nats stream add ORDERS --subjects 'orders.*' --storage file --replicas 3 --max-age 72h
nats stream ls && nats stream info ORDERS
nats consumer add ORDERS billing --filter orders.paid --ack explicit --durable
nats consumer report ORDERS            # 看 pending/ack 等状态
nats server report jetstream           # 集群 JS 健康一览
nats bench --pub 8 orders.test --size 1KB --msgs 100000   # 压测
```

---

## 5. NATS 2.11 版本要点（2025.3）

- **按消息 TTL**：`Nats-TTL` 头让单条消息独立于流限制过期（ADR-43）；
- **分布式消息追踪**：`Nats-Trace-Dest` 头开启消息级追踪，事件回报 ingress/映射/路由全链路；
- **Pull 消费者优先级组**：主备消费者故障转移（pinning/overflow 策略）；
- **消费者暂停**：`PauseUntil`——维护窗口挂起投递，到点自动恢复；
- **流摄入限速**：`max_buffered_msgs/size` 防过载；
- **`cluster_traffic` 隔离**：Raft 复制流量可隔离到资产所在 account，避免多租户头阻塞；
- 其他：MQTT 3.1.1/5 支持、Windows TPM 密钥存储、配置摘要指纹（`-t`）。

---

## 6. 常见坑与最佳实践

1. **Core NATS 无持久化**：直接用 `nc.Publish` 发关键业务消息而没接 JetStream，订阅者一掉线消息就没了——可靠性需求一律 `js.Publish`；
2. **消费者忘配 durable**：非持久消费者断线即丢进度，重连从头/最新开始，可能漏消息；
3. **FilterSubject 别省**：消费者不配过滤会吃进整个流，挤占带宽与处理能力；
4. **一个流装天下**：不同域（订单/工具调用/审计）分流建 stream——保留策略、副本数、TTL 需求不同；
5. **ack 忘记调**：ack explicit 下不 ack 会触发 `MaxDeliver` 上限内的反复投递，处理失败要显式 `Nak()`/`Term()`；
6. **重试与死信要自建**：`MaxDeliver` 超限后消息停止投递，标准做法是建 `DLQ.<domain>` 流 + republish/转发；
7. **流副本数**：单节点（R1）磁盘坏 = 数据丢，生产 R3；
8. **监控**：`/healthz`（带 `js=true` 检查 JetStream）、Prometheus 指标（`-m 8222` 或专用 7777 端口导出）。

---

## 7. 选型对比

| 维度 | NATS (Core) | NATS JetStream | Kafka | RabbitMQ |
|---|---|---|---|---|
| 交付语义 | at-most-once | at-least/exactly-once | at-least/exactly-once | at-least-once |
| 持久化/回放 | 无 | 有（文件/Raft 副本） | 有（强项） | 弱 |
| 运维成本 | 极低 | 低 | 中 | 低 |
| 请求响应/RPC | 原生 | — | 不适合 | RPC 模式 |
| 吞吐天花板 | 极高（无落盘） | 高 | 极高 | 中 |
| 生态（连接器/流处理） | 少 | 少 | 极丰富 | 中 |

一句话：**内网总线/RPC/轻量持久化选 NATS；大数据管道与流处理生态选 Kafka**。两者也可共存（边缘 NATS 汇聚到中心 Kafka）。
