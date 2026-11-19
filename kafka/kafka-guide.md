# Apache Kafka 技术介绍与使用指南

> 高吞吐分布式事件流平台：日志型存储 + 发布订阅 + 流处理三合一。
> 官网：<https://kafka.apache.org>　|　当前主线：**4.x（2025.3.18 发布，彻底移除 ZooKeeper）**

---

## 1. 项目简介

Kafka 起源于 LinkedIn（2011 开源），最初为解决**海量活动日志的订阅分发**，如今是事实标准的事件流平台（Event Streaming Platform）。一句话模型：

> **分布式、分区的、可复制的、追加写日志（append-only log）**。生产者把事件追加到日志尾部，消费者按 offset 顺序读取；日志按保留策略持久保存，可重放。

三大能力域：

| 能力 | 组件 | 说明 |
|---|---|---|
| 消息系统 | Broker/Producer/Consumer | 发布订阅 + 消费组负载均衡，可替代传统 MQ |
| 存储系统 | Topic/Partition/Log | 数据按策略保留（时长/大小/tiered storage），安全可重放 |
| 流处理 | Kafka Streams / ksqlDB | 在 Kafka 之上做有状态实时计算 |

适用场景：事件溯源、微服务解耦、削峰填谷、CDC 数据管道、用户行为埋点、实时风控/大屏。

**不适用**：需要复杂路由与优先级队列的传统 MQ 场景（考虑 RabbitMQ）、海量小文件的存储（那是对象存储的事）。

### 快速启动（4.x KRaft 模式，无需 ZooKeeper）

```bash
# 生成集群 UUID
KAFKA_CLUSTER_ID="$(bin/kafka-storage.sh random-uuid)"
# 格式化存储（KRaft 把元数据放进日志自身）
bin/kafka-storage.sh format -t $KAFKA_CLUSTER_ID -c config/kraft/server.properties
# 启动
bin/kafka-server-start.sh config/kraft/server.properties
```

---

## 2. 核心概念

### 2.1 消息模型四要素

```text
Topic: orders (逻辑分类)
  ├── Partition 0:  [0][1][2][3][4][5]...  ← 每条消息有唯一 offset
  ├── Partition 1:  [0][1][2][3]...
  └── Partition 2:  [0][1][2][3][4]...
        每个分区 = 一个有序、不可变日志，分布在不同 Broker 上
```

- **Topic**：消息的逻辑分类（如 `orders`、`user-events`）；
- **Partition**：分片单位。**分区内部严格有序，跨分区不保证顺序**——需要顺序的消息（同一订单的状态流）要用相同的 key（如 order_id），相同 key 哈希到同一分区；
- **Offset**：消费者在分区内的进度游标，由**消费者自己提交**到内部 topic `__consumer_offsets`；
- **Replication**：每个分区有多个副本（`replication.factor=3`），一个 leader 处理读写，其余 follower 同步；leader 挂掉时从 ISR（同步副本集合）中选举新 leader。

### 2.2 消费组（Consumer Group）

- 同一 group 内的消费者**平分分区**（一个分区同一时刻只给组内一个消费者）——组内水平扩容的极限是分区数；
- 不同 group 之间互不影响（广播语义）：订单服务与风控服务各自消费全量消息；
- **Rebalance**：组内成员变化（扩缩容/宕机）触发分区重分配。KIP-848 新协议（4.0 GA）把 stop-the-world 式重balance 变为增量式，大幅减少抖动。

### 2.3 投递语义

| 语义 | 生产者 | 消费者 |
|---|---|---|
| At most once | acks=1/0，不重试 | 先提交 offset 再处理（可能丢） |
| At least once（默认推荐） | acks=all + retries + 幂等 | 先处理再提交 offset（可能重） |
| Exactly once | 幂等生产者 + 事务 | `isolation.level=read_committed` |

幂等生产者：`enable.idempotence=true`（broker 按 PID+序列号去重）；事务：跨分区原子写 + 消费-转换-生产（Kafka Streams 的 exactly-once）。

### 2.4 保留策略与分层存储

- 基于时间/大小的日志保留（`retention.ms=604800000` 即 7 天），到期分区头部的旧数据被截断；
- **Tiered Storage（KIP-405）**：把冷数据卸载到 S3 等对象存储，本地只留热数据——"无限保留 + 便宜的历史回溯"成为可能。3.6 引入时是 early access，3.9 转为正式可用；老版本集群不要按「已可用」规划。

---

## 3. 架构原理

### 3.1 4.0 之前的架构（ZooKeeper 时代）

```text
Producer/Consumer ──► Broker 集群 ──► ZooKeeper（元数据、controller 选举）
```

痛点：两套系统双份运维；ZK watch 风暴；分区规模上限约 20 万；controller 故障切换 18~30s。

### 3.2 KRaft 架构（Kafka Raft，4.0 唯一模式）

```text
                ┌── Controller quorum（3/5 台，Raft 选举 active controller）
元数据日志 @metadata ──► 各 Broker 通过快照+日志增量同步元数据
                
Producer ──► Broker（读写分区日志）◄── Consumer
```

- 元数据存进内部 topic `__cluster_metadata`，用 Raft 维护一致性；
- **收益**：运维面减半、controller 切换秒级（<5s）、分区上限从几十万到**百万级**、topic 创建等元操作 O(1)；
- 部署形态：可组合（broker+controller 同机，小集群）或分离（大规模推荐）。

### 3.3 高吞吐的三大支柱

1. **顺序写磁盘**：追加日志避免随机 IO，普通磁盘也能数百 MB/s；
2. **页缓存 + 零拷贝**：消费时 broker 通过 `sendfile` 把页缓存数据直接送到网卡，不进用户态；
3. **批量 + 压缩**：生产者攒批（`batch.size`/`linger.ms`）压缩（lz4/zstd）后传输，吞吐成倍提升。

### 3.4 一次写入路径

```text
Producer: key 哈希 → 目标分区 → 攒批压缩 → push
Broker(leader): 追加本地日志 → 等 ISR 同步（acks=all）→ 返回
Follower: 拉取 leader 日志追加
Consumer: pull 模式按批次拉取 → 处理 → 提交 offset
```

注意 Kafka 消费是 **pull** 模型：消费者按自己的能力拉取，天然背压，broker 无需维护每个消费者的推送状态。

### 3.5 再平衡：协议、触发条件与「风暴」怎么来的

再平衡（rebalance）是消费组重新分配分区的过程，协议三步：**JoinGroup**（成员上报订阅）→ **SyncGroup**（leader 算出方案下发）→ 各成员按新分配开始拉取。代价在于**分区会被收回**：处理到一半的消息要么处理完再交还，要么重投。

两种再平衡协议：

| 协议 | 行为 | 停顿范围 | 版本要求 |
|---|---|---|---|
| Eager（`Range`/`RoundRobin`/`Sticky`Assignor） | 所有成员**先放弃全部分区**，再重新加入拿新分配 | 全组停摆，stop-the-world | 全版本 |
| Cooperative（`CooperativeStickyAssignor`） | 只收回**需要迁移**的分区，其余继续消费；可能要两轮才收敛 | 只有被迁走的分区短暂停顿 | 客户端 ≥ 2.4 |

触发条件有四种，前两种最常见：

| 触发条件 | 怎么被感知 | 常见误触发原因 |
|---|---|---|
| 成员加入 | 新消费者启动、心跳恢复 | 滚动发布逐台重启（没配静态成员） |
| 成员离开 | 主动 LeaveGroup、心跳超时 `session.timeout.ms`、poll 间隔超 `max.poll.interval.ms` | 单条消息处理太慢、长时间 GC、下游阻塞 |
| 订阅变化 | 成员订阅的 topic 列表变更（含正则匹配到新 topic） | 误改 `topics` 配置、正则把新 topic 卷进组里 |
| 分区数变化 | 元数据变更，leader 重新计算分配 | 高峰期直接扩分区 |

两个超时最容易搞混：

```text
session.timeout.ms    = 45s（4.x 默认）  由心跳线程负责，处理线程卡住不影响心跳
max.poll.interval.ms  = 300s（默认 5min）两次 poll() 之间的最大间隔，超了就被踢出组
```

配合关系：心跳还活着、poll 却停了 → 客户端"以为自己还在组里"，broker 早已踢掉它 → 提交位点时抛 `CommitFailedException` / `UNKNOWN_MEMBER_ID`。长耗时的处理应该**缩小 `max.poll.records` 或异步化**，而不是无脑调大 `max.poll.interval.ms`（调大只是把卡死时间拉长）。

**静态成员**（`group.instance.id`）把成员身份从"每次启动都是新成员"变成"固定身份"：重启后只要在 `session.timeout.ms` 内回来，broker 就认为它只是抖动，**不触发再平衡**。K8s 滚动更新几乎必备。注意两点：静态成员"离开"不会立刻触发 rebalance（要等 session 超时），主动缩容时要显式发 LeaveGroup；`session.timeout.ms` 可以配到 `group.max.session.timeout.ms`（默认 30min）以内。

**`CooperativeStickyAssignor` 的取舍**：换来"发布时只停被迁移的分区"，代价是收敛慢（可能需要两轮 JoinGroup）、日志和诊断更绕；另外 `partition.assignment.strategy` 必须全组一致，从 Eager 切换要分两步（先把 Cooperative 追加到列表末尾，全组升级完再删掉 Eager），否则会退化回 Eager。

停顿时长估算（用于评估发布窗口）：

```text
崩溃触发：session.timeout.ms（发现失联）+ join/sync（1~3s）
滚动重启：每台消费者一次 rebalance ≈ N × (join+sync)，N 为消费者台数
Eager 模式下这段窗口全组消费为 0；Cooperative 下只有被迁走的分区停
```

### 3.6 幂等与事务：exactly-once 的真实边界

Kafka 的 exactly-once 是**分区日志 + 消费位点**范围内的原子性，不是分布式事务的银弹。分两层看。

**第一层：幂等生产者（单分区、单会话去重）**

`enable.idempotence=true`（3.0 起客户端默认开启）不是孤立开关，它会**强制**绑定一组条件：

| 隐含条件 | 原因 |
|---|---|
| `acks=all` | 只有全部 ISR 确认，重试才安全 |
| `retries > 0`（实际是 MAX） | 失败要允许重发 |
| `max.in.flight.requests.per.connection ≤ 5` | 保证重试批次不会在序列上覆盖后续批次 |

机制：broker 给每个生产者分配 **PID**（Producer ID），每个 `(PID, partition)` 维护单调递增的**序列号**；broker 按分区缓存最近 5 个序列号，重复批次直接丢弃但仍回 ACK。

```text
Producer PID=1000
  batch seq=7 → P0  写入成功
  batch seq=8 → P0  写入成功
  batch seq=8 → P0  重试，broker 判定重复 → 丢弃但回 ACK
  batch seq=9 → P0  写入成功
```

作用域边界：**单分区、单生产者会话**。PID 在生产者重启后就会变，跨会话不去重——这正是事务要补的那一层。

**第二层：事务（跨分区 + 跨会话）**

生产者配置业务唯一且稳定的 `transactional.id`（如 `order-svc-tx-0`），broker 据此分配 PID 与 **epoch**：

1. `initTransactions()`：向 transaction coordinator 注册，epoch 递增；
2. `beginTransaction()` → 向多个分区写入；
3. `sendOffsetsToTransaction()`：把消费组位点也写进事务（消费-转换-生产的闭环）；
4. `commitTransaction()` / `abortTransaction()`：向各参与分区写 **COMMIT / ABORT marker**。

```text
Producer(txn.id=order-tx-0)
  initTransactions → 注册 PID/epoch；旧 epoch 实例的写入被 fence
  ├─ write orders-3
  ├─ write audit-0
  ├─ sendOffsets(group=billing, orders-3 → offset 8821)
  └─ commit → 各分区落 COMMIT marker
                        │
Consumer(isolation.level=read_committed)
  只读到 LSO（Last Stable Offset）之前、且未 abort 的消息
```

**fencing**：同一个 `transactional.id` 只允许一个活跃实例。新实例 `initTransactions` 让 epoch 递增，旧实例的写入被 broker 拒绝（客户端报 `ProducerFenced`）——这是防"僵尸生产者"重复写入的关键，也是滚动发布不会出现双写的原因。

**边界（务必记住）**：

```text
Kafka 内部：读 Kafka → 处理 → 写 Kafka + 提交位点   = exactly-once ✅
跨系统：    写 Kafka ＋ 写 MySQL / 调 HTTP          = at-least-once ❌
```

外部副作用没法和 Kafka 事务一起提交：要么把下游做成**幂等**（upsert / 唯一键去重），要么用**事务性发件箱（outbox）**——业务表和 outbox 表在同一个本地事务里写，再靠 CDC 把 outbox 投递进 Kafka。另外 `read_committed` 要等事务结束才可见，长事务会推高 LSO 并放大读延迟。

### 3.7 分区键与顺序性：乱序从哪来

顺序性只有一条规则：**分区内有序，跨分区无序**。所有乱序都能归到"同 key 的消息被投到了不同分区，或同一分区内批次顺序被打乱"。

**前提：同 key 同分区**

默认分区器对 key 做 `murmur2(key) % numPartitions`。key 为 `null` 时，Java 客户端 2.4+ 走 **sticky partitioner**：一批消息全发往同一分区，攒满或 `linger.ms` 到期再换下一个。它比逐条轮询的批次更大、延迟更稳，但**不同批次可能落在不同分区，null key 本身不保序**。

**乱序来源一：重试**

```text
batch1(seq=1) → P0  超时 ❌
batch2(seq=2) → P0  成功 ✅
batch1 重试    → P0  成功 ✅      ← 1 落在 2 之后，乱序
```

`max.in.flight.requests.per.connection=1` 能消除乱序（同一时刻只有一个在途批次），代价是吞吐明显下降。**开启幂等后 `max.in.flight ≤ 5` 依然保序**——broker 会按序列号拒绝越序批次，这是首选方案（见 §3.6）。

**乱序来源二：分区扩容**

`murmur2(key) % 6` 和 `murmur2(key) % 12` 的结果毫无关系。从 6 扩到 12 后，同一 `order_id` 的新消息去了新分区，旧分区里还有积压——两条流并行推进，消费端看到的顺序就错了。所以：

- 顺序敏感的 topic **不要在线扩容**；确需扩容，选低峰期 + 消费彻底追平，或按业务维度拆新 topic 平滑迁移；
- 扩容同时会触发消费组再平衡（§3.5），乱序叠加 rebalance 抖动，排查难度翻倍。

**消费端并行度**

一个分区同一时刻只能被组内一个消费者消费，所以**并行度上限 = 分区数**。多出来的消费者不是分摊压力，而是排队等接盘（闲置）；反过来，分区数远多于消费者数时，单消费者串行处理多个分区，慢分区会拖住同批拉取的快分区——这时用 `pause`/`resume` 做隔离，比继续加分区更有效。

---

## 4. 使用实战（Go：segmentio/kafka-go）

```bash
go get github.com/segmentio/kafka-go
```

### 4.1 生产者

```go
writer := &kafka.Writer{
    Addr:  kafka.TCP("localhost:9092"),
    Topic: "orders",
    Balancer: &kafka.Hash{},          // 相同 key 进同一分区（保序）
    RequiredAcks: kafka.RequireAll,   // acks=all：全部 ISR 确认
    AllowAutoTopicCreation: true,
    BatchSize: 100,                   // 攒批提吞吐
    Compression: kafka.Snappy,
}
defer writer.Close()

err := writer.WriteMessages(context.Background(),
    kafka.Message{
        Key:   []byte("order-1001"),   // 用 orderId 做 key
        Value: []byte(`{"amount":99}`),
    },
)
```

### 4.2 消费组

```go
reader := kafka.NewReader(kafka.ReaderConfig{
    Brokers: []string{"localhost:9092"},
    GroupID: "billing-service",        // 消费组：组内平分分区
    Topic:   "orders",
    MinBytes: 1, MaxBytes: 10e6,
})
defer reader.Close()

for {
    m, err := reader.ReadMessage(context.Background())
    if err != nil { break }
    process(m)                          // 先处理……
    reader.CommitMessages(context.Background(), m) // ……再提交（at-least-once）
}
```

Go 客户端还有 franz-go（性能最强、特性最全）与 confluent-kafka-go（官方但基于 librdkafka C 库）。

### 4.3 命令行运维

```bash
bin/kafka-topics.sh --create --topic orders --partitions 6 --replication-factor 3 \
  --bootstrap-server localhost:9092
bin/kafka-topics.sh --describe --topic orders --bootstrap-server localhost:9092
bin/kafka-console-producer.sh --topic orders --bootstrap-server localhost:9092
bin/kafka-console-consumer.sh --topic orders --from-beginning --bootstrap-server localhost:9092
bin/kafka-consumer-groups.sh --describe --group billing-service --bootstrap-server localhost:9092 # 看 lag
bin/kafka-metadata-quorum.sh describe --status -b localhost:9092   # KRaft quorum 状态
```

### 4.4 关键配置速查

| 场景 | 配置 |
|---|---|
| 不丢消息 | producer: acks=all, retries=MAX, enable.idempotence=true; broker: min.insync.replicas=2; topic: replication.factor=3 |
| 高吞吐 | producer: linger.ms=20, batch.size=64KB, compression=lz4/zstd |
| 顺序性 | 同 key 同分区 + max.in.flight ≤5（幂等开启后无碍） |
| 消费能力不足 | 增加分区数 → 扩消费者；或用 share group（4.0 EA，见 §5） |

### 4.5 客户端选型与调优：segmentio / franz-go / confluent

| 客户端 | 出身 / 维护 | 特点 | 代价 |
|---|---|---|---|
| `IBM/sarama`（原 Shopify/sarama） | Shopify 起家，后由 IBM 接手 | 老牌、生态最广，很多工具与 Connect 组件基于它 | API 偏重、配置项多，性能中游，新协议跟进偏慢 |
| `segmentio/kafka-go` | Segment | API 极简（`Writer`/`Reader`），上手最快，本文 §4.1/§4.2 即它 | 特性相对少（事务、精细批量控制较弱），超高性能场景不够用 |
| `twmb/franz-go` | 社区活跃、纯 Go | **协议覆盖最全（事务、KIP-848、flexible versions）、性能第一梯队**，批量/重试/取消控制精细 | `kgo.Opt` 选项多，需要读文档 |
| `confluent-kafka-go` | Confluent 官方 | 行为与 Java 客户端最一致，功能最全 | **cgo + librdkafka**，交叉编译和容器镜像变重，排障要懂 C 库 |

选型结论：新项目直接 **franz-go**；只要简单生产消费、不想学新 API 用 segmentio；必须与 Java 端严格对齐、或已深度使用 Schema Registry / Confluent 生态才选 confluent-kafka-go。sarama 存量项目多，迁移收益通常不足以覆盖改造风险。

**生产者（franz-go）**

```go
package main

import (
	"context"
	"fmt"
	"time"

	"github.com/twmb/franz-go/pkg/kgo"
)

func main() {
	client, err := kgo.NewClient(
		kgo.SeedBrokers("localhost:9092"),
		kgo.DefaultProduceTopic("orders"),
		kgo.RequiredAcks(kgo.AllISRAcks()),          // acks=all
		kgo.EnableIdempotentProducer(),              // 幂等：隐含 acks=all、有序、有限重试
		kgo.ProducerBatchMaxBytes(1<<20),            // 单批上限 1MB（batch.size）
		kgo.ProducerLinger(10*time.Millisecond),     // 攒批等待（linger.ms）
		kgo.ProducerBatchCompression(kgo.ZstdCompression()),
	)
	if err != nil {
		panic(err)
	}
	defer client.Close()

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	// 同步发送：等 broker ACK 再返回；异步用 Produce + 回调，吞吐更高但要自己处理错误
	rec := &kgo.Record{Key: []byte("order-1001"), Value: []byte(`{"amount":99}`)}
	if err := client.ProduceSync(ctx, rec).FirstErr(); err != nil {
		fmt.Println("produce failed:", err)
	}
}
```

**消费者（franz-go，手动提交 + 优雅退出）**

```go
package main

import (
	"context"
	"os/signal"
	"syscall"

	"github.com/twmb/franz-go/pkg/kgo"
)

func main() {
	client, err := kgo.NewClient(
		kgo.SeedBrokers("localhost:9092"),
		kgo.ConsumerGroup("billing-service"),
		kgo.ConsumeTopics("orders"),
		kgo.DisableAutoCommit(),                          // 手动提交：先处理，再提交
		kgo.Balancers(kgo.CooperativeStickyBalancer()),   // 增量再平衡，减少停顿
	)
	if err != nil {
		panic(err)
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	for ctx.Err() == nil {
		fetches := client.PollFetches(ctx) // 一次拿到多个分区的一批记录
		if ctx.Err() != nil {
			break // 收到退出信号，跳出主循环
		}

		// 拉取错误（如分区迁移中）：本批丢弃，退避后再拉，避免空转打满 CPU
		if errs := fetches.Errors(); len(errs) > 0 {
			time.Sleep(500 * time.Millisecond)
			continue
		}

		failed := false
		for iter := fetches.RecordIter(); !iter.Done(); {
			rec := iter.Next()
			if err := process(rec); err != nil {
				// 处理失败：本批一条都不提交，整批重放（消费端必须幂等）。
				// 生产上应先把毒消息送死信再提交，否则它会永久卡住这个分区。
				failed = true
				break
			}
		}

		// 只有整批成功才提交。CommitUncommittedOffsets 提交的是「已 poll 的最高位点」，
		// 失败时调用它会把失败记录连同后面的记录一起提交掉，语义直接退化成 at-most-once。
		if !failed {
			if err := client.CommitUncommittedOffsets(ctx); err != nil {
				log.Printf("commit failed, batch will replay: %v", err)
			}
		}
	}

	// 优雅退出：停止拉取 → 提交进度 → 离开消费组（触发一次 Cooperative 再平衡）
	_ = client.CommitUncommittedOffsets(context.Background())
	client.Close()
}

func process(rec *kgo.Record) error {
	_ = rec // 业务处理：写库 / 调下游，需保证幂等
	return nil
}
```

**三个吞吐参数怎么调**

| 参数（Go 客户端对应项） | 作用 | 调大 | 调小 |
|---|---|---|---|
| `batch.size` / `ProducerBatchMaxBytes` | 单批字节上限 | 吞吐↑、内存占用↑、尾部延迟↑ | 延迟↓、批次数↑、压缩率↓ |
| `linger.ms` / `ProducerLinger` | 攒批最长等待 | 批次更满、吞吐↑ | 延迟↓，低流量时批次变碎 |
| `compression.type` / `ProducerBatchCompression` | 压缩算法 | zstd 压缩比最好、CPU 最高；lz4 均衡；none 最省 CPU | — |

调法：先定延迟预算（P99），`linger.ms` 取预算的 1/10 量级，再用 `batch.size` 和压缩把吞吐顶上去。`acks=all` + 幂等是默认档位，**不要为了吞吐关掉**——省下的那点延迟，会以重复和乱序的形式还回来。

### 4.6 监控与故障演练

**必须盯的指标**

| 指标 | 含义 | 参考阈值 |
|---|---|---|
| `UnderReplicatedPartitions` | 副本数不足的分区数 | > 0 持续 5min（短时抖动正常，持续则查 broker/磁盘/网络） |
| `OfflinePartitionsCount` | 无 leader 的分区数 | **> 0 立即告警**，该分区不可读写 |
| `RequestHandlerAvgIdlePercent` | 请求处理线程空闲率 | < 30% 表示 broker 过载，考虑扩节点或迁分区 |
| 消费 lag | 组内未消费消息数 | 持续增长，或折合延迟超过业务 SLA |
| `rebalance-rate-per-hour` / `rebalance-total` | 单位时间再平衡次数 | 突增说明成员在反复进出（见 §3.5）；这也是「消费突然变慢」的常见前兆 |

**lag 的两种算法，别混用**

- **分区级**：`LEO - committed_offset`（即 `kafka-consumer-groups` 的 `LAG` 列）是真实积压，表示「还有多少没确认」；
- **瞬时落后**：`LEO - 消费者当前读取位置`，只存在于客户端内部（或用 `fetches` 里最后一条的 offset 估算），用于观察处理速度，不是可靠的积压指标；
- **组级**：所有分区 lag 求和。求和会掩盖「某个分区卡死、其余追平」，所以**一定要同时看 max(partition lag)**。

判断 lag 在不在缩小，别只看一个采样点：

```bash
# 每 10s 采样观察 LAG 走向；CONSUMER-ID / HOST 列能看出是哪个实例在拖
watch -n 10 'bin/kafka-consumer-groups.sh --describe --group billing-service --bootstrap-server localhost:9092'
```

三种典型形态：`LAG` 单调下降 = 健康；`LAG` 不动且 `CURRENT-OFFSET` 也不动 = 消费者卡死（处理阻塞，或心跳活着但 poll 停了）；`LAG` 阶梯跳变 + `CONSUMER-ID` 频繁变化 = 反复 rebalance。

**三个必做演练**（先在预发做，每季度一次）

| 演练 | 操作 | 应该看到 | 不该看到 |
|---|---|---|---|
| kill 一个 broker | `kill -9` broker 进程 | ISR 收缩 → 秒级选出新 leader → `UnderReplicatedPartitions` 短时上升后回落；生产端重试成功，RF=3 + minISR=2 下无丢失 | `OfflinePartitionsCount` 长期 > 0；生产端持续 `NotEnoughReplicas` |
| kill 一个消费者 | 杀掉组内一台消费者 | 分区被收回 → 再平衡 → 存活成员接管；lag 短时抬升后恢复下降 | 消息丢失（未提交位点会重投，只应有重复）；lag 长时间不回落（说明剩余消费者扛不住） |
| 网络分区 | 用 iptables/tc 切断 broker 间或客户端到 broker 流量 | 客户端在 `session.timeout.ms` 后判定失联并再平衡；落后副本掉出 ISR | 脑裂写入（minISR 生效时应是"不可写"而不是"写坏"）；消费者自认存活却提交失败 |

演练的目的不是"别报警"，而是验证**告警能在几分钟内触发、值班的人知道该看哪张图**。

---

## 5. Kafka 4.0 变化速览

| 变化 | 说明 |
|---|---|
| ZooKeeper 移除 | KRaft 唯一模式；旧 ZK 集群需经 3.9 桥接版本完成迁移 |
| KIP-848 GA | 新消费组协议：增量 rebalance、更快的成员变更，消除 stop-the-world |
| KIP-932（EA） | Share Groups：同组多消费者并发消费**同一分区**，支持单条 ack——Kafka 兼具队列语义 |
| Tiered Storage 成熟 | 冷数据下沉对象存储，本地盘只留热段 |
| 元数据能力 | 分区百万级，元操作 O(1) |
| 移除的 API | 部分老协议/旧配置清理，升级前必读迁移文档 |

升级路径：ZK 模式 → 3.9（迁移）→ 4.0；3.3+ KRaft 可直升。

---

## 6. 常见坑与最佳实践

1. **分区数别拍脑袋**：分区 = 并行度上限与 Rebalance 成本。过多分区拖慢 leader 选举与元数据；常见起点 6/12，按吞吐增长再扩（**分区只能加不能减**）；
2. **rebalance 风暴**：`session.timeout` 过小、处理超时（`max.poll.interval.ms` 默认 5min）会触发反复 rebalance——长任务要调大或异步化；
3. **offset 提交时机**：先提交后处理会丢消息；先处理后提交会重复——消费端做幂等（upsert / 去重表）是标配；
4. **监控消费延迟（lag）**：lag 是消费侧健康的第一指标，Burrow/exporter + 告警；
5. **min.insync.replicas 与可用性权衡**：=2 时一台 broker 宕机仍可写，两台宕机则不可写（保一致性，正确取舍）；
6. **大消息**：默认单条上限 1MB；大对象建议外置对象存储（如 MinIO），消息里只放引用；
7. **不要用 Kafka 做任务队列的强时限场景**：没有原生 DLQ/延迟队列，需要自行建 retry topic + DLQ topic 的分层重试拓扑；
8. **客户端版本对齐**：客户端协议版本与 broker 差异过大会有兼容性坑，升级按官方兼容矩阵灰度。

---

## 7. 选型对比

| 维度 | Kafka | RabbitMQ | NATS JetStream | Pulsar |
|---|---|---|---|---|
| 模型 | 分区日志 | 交换机路由队列 | Subject + 流 | 分区日志+分层 |
| 吞吐 | 极高（百万/s） | 中 | 高（轻量） | 极高 |
| 回放/保留 | 强（原生） | 弱 | 强（JetStream） | 强 |
| 运维复杂度 | 中（4.0 后大降） | 低 | 极低 | 高（BookKeeper+ZK） |
| 生态 | 事实标准、Connect/Streams | 传统企业 | 云原生/AI 轻量总线 | 多租户强 |

一句话：**主流高吞吐事件流默认 Kafka；轻量微服务/边缘消息选 NATS；需要复杂路由的传统 MQ 选 RabbitMQ**。
