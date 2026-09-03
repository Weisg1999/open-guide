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

```
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
- **Tiered Storage（KIP-405，3.6 引入、生产可用）**：把冷数据卸载到 S3 等对象存储，本地只留热数据——"无限保留 + 便宜的历史回溯"成为可能。

---

## 3. 架构原理

### 3.1 4.0 之前的架构（ZooKeeper 时代）

```
Producer/Consumer ──► Broker 集群 ──► ZooKeeper（元数据、controller 选举）
```

痛点：两套系统双份运维；ZK watch 风暴；分区规模上限约 20 万；controller 故障切换 18~30s。

### 3.2 KRaft 架构（Kafka Raft，4.0 唯一模式）

```
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

```
Producer: key 哈希 → 目标分区 → 攒批压缩 → push
Broker(leader): 追加本地日志 → 等 ISR 同步（acks=all）→ 返回
Follower: 拉取 leader 日志追加
Consumer: pull 模式按批次拉取 → 处理 → 提交 offset
```

注意 Kafka 消费是 **pull** 模型：消费者按自己的能力拉取，天然背压，broker 无需维护每个消费者的推送状态。

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
