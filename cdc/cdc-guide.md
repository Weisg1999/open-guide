# 变更数据捕获（CDC）技术指南

> **一句话定位**：订阅数据库的变更日志，把「数据变了」变成「事件」，业务代码不用写一行双写逻辑。
>
> **上游仓库**：[debezium/debezium](https://github.com/debezium/debezium)、[alibaba/canal](https://github.com/alibaba/canal) · **版本基线**：Debezium 3.x、Canal 1.1.8 · **协议**：Apache-2.0

---

## 1. 项目简介

「写库之后顺手更新缓存 / 索引 / 数仓」是微服务里最常见的一行代码，也是最容易烂掉的一行：它把两个系统塞进一次请求，却没有给它们任何原子性。

| 缺陷 | 触发条件 | 现场 |
|---|---|---|
| **事务不一致** | 写库成功，写 ES / Redis / MQ 失败或超时 | 库里有、搜索没有，永久漂移，只能靠对账 |
| **漏写** | 新增写入口（后台、批处理、运维 SQL）忘了同步 | 覆盖率随代码增长单调下降，越老的表越脏 |
| **代码侵入** | 每条写路径都要埋点，还要处理重试与顺序 | 业务逻辑与同步逻辑耦合，评审时看不出来 |

并发下还有第四种：A 更新 → B 更新 → B 的同步先到、A 的后到，索引最终停在 A 的旧值。**双写没有全局顺序**，加多少重试都修不好。

CDC（Change Data Capture）换了一个信息源：**不再问业务代码「你改了什么」，而是读数据库的提交日志**。MySQL 的 binlog、PostgreSQL 的 WAL、Oracle 的 redo，都是「已提交事务的追加写日志」，天然具备三个性质：

- **完整**：HTTP 接口、定时任务、运维手工 SQL、批量脚本，只要提交了，日志里就有；
- **有序**：按事务提交顺序追加，同一行的变更不会倒序；
- **可重放**：位点（GTID / LSN / SCN）持久化后，进程重启从断点继续。

所以 CDC 的定位不是「同步工具」，而是**把「数据变更」提升为一等事件**：数据库负责产生事实，CDC 负责把事实翻译成事件流，下游各自决定拿它做什么。

| 场景 | 消费动作 | 关键要求 |
|---|---|---|
| 缓存失效 | 删除 key，而不是写 key | 低延迟；删除天然幂等 |
| 搜索/推荐同步 | 按主键 upsert 文档 | 顺序；能处理删除 |
| 数仓 / 数据湖入湖 | 追加 ODS 或 merge 成宽表 | 全量 + 增量；schema 演进 |
| 审计与风控 | 记录 before/after 与操作位点 | 不可篡改；可追溯 |

边界也要说清：CDC 只解决「数据变了要通知」，**不解决「多个写要原子」**——那是 [分布式事务篇](../distributed-transaction/distributed-transaction-guide.md) 的 Outbox / Saga 要处理的问题；它也**不做业务语义转换**，「支付成功要发券」应该在下游消费端判断，而不是塞进 CDC 管道。CDC 的下游通常是一条 [Kafka](../kafka/kafka-guide.md) 事件流，源头的日志格式则绕不开 [MySQL](../mysql/mysql-guide.md) 的 binlog。

---

## 2. 能力总览

| 能力 | 实现方式 | 边界与代价 |
|---|---|---|
| **全量快照 + 增量** | 一致性快照读 + 记录位点 + 从位点读日志 | 大表快照耗时长；锁位点会短暂阻塞写入 |
| **Schema 演进** | 解析 DDL、维护 schema history、发 schema change 事件 | 解析不了的 DDL 会让连接器停下 |
| **事务边界还原** | 事务元数据 topic，携带事务 id 与事件序号 | 需内存缓冲整个事务；跨表原子性仍要下游拼 |
| **Exactly-once 投递** | Kafka Connect KIP-618：数据与 offset 同事务提交 | 只覆盖「源库 → Kafka」，之后仍靠幂等 |
| **断点续传** | offset 存 Kafka 内部 topic；GTID / file+pos 作位点 | binlog 被清理后位点失效，只能重做快照 |
| **过滤与路由** | `table.include.list`、SMT、按字段路由 topic | 正则写错会静默漏表，需有「表数量」基线告警 |
| **可观测** | JMX 指标：延迟、队列余量、快照进度、断连次数 | 要接 JMX exporter 才能进 Prometheus |

三条硬约束，先接受再选型：

1. **默认是 at-least-once**：崩溃重启、offset 提交失败都会重放。所谓「不重复」是下游幂等的效果，不是投递层的承诺。
2. **顺序只在单主键内保证**：Kafka 按 key 分区，key 是主键；跨主键、跨表没有全局顺序。
3. **快照与增量会重叠**：快照读到的行，与随后重放的 binlog 事件可能指向同一行，下游必须按主键 upsert，而不是盲目 append。

---

## 3. 核心概念与原理

### 3.1 MySQL binlog 的三种格式：为什么 CDC 必须 ROW

| 格式 | 记录内容 | 能否还原行 | 典型问题 |
|---|---|---|---|
| `STATEMENT` | 原始 SQL 文本 | **不能** | `NOW()`、`RAND()`、无 `ORDER BY` 的 `LIMIT` 在主从上结果不同 |
| `ROW` | 每一行的变更镜像（before/after） | **能** | 日志量大；要 `binlog_row_image=FULL` 才有完整旧值 |
| `MIXED` | 默认 statement，非确定性语句转 row | 部分能 | CDC 必须同时处理两种，解析路径翻倍 |

`STATEMENT` 对 CDC 是死路：`UPDATE t SET stock = stock - 1` 只留下 SQL，消费端要自己连库重算；触发器、存储过程、`INSERT ... SELECT` 更无法还原成「哪一行变成了什么」。`ROW` 格式下 binlog 本身就是**行级事实**，CDC 只做解码与信封包装，不需要理解 SQL 语义。

```ini
# my.cnf（8.0+）；改完必须重启，ROW 是 CDC 的硬前提
[mysqld]
server-id                = 1
log-bin                  = mysql-bin
binlog_format            = ROW
binlog_row_image         = FULL
binlog_row_metadata      = FULL
gtid_mode                = ON
enforce_gtid_consistency = ON
binlog_expire_logs_seconds = 604800   # 7 天，必须大于「最长可接受停机时间」
```

两个容易漏的参数：`binlog_row_image=MINIMAL` 只记主键和被修改列，`before` 里没有未变的列，下游无法构造完整旧文档（`NOBLOB` 会省略未改的 TEXT/BLOB），要审计或全量同步必须 `FULL`；`binlog_row_metadata=FULL` 让 binlog 自带列名、类型、精度（8.0.1+），否则 Debezium 只能靠 DDL 历史推断，遇到不支持的语法更容易出错。

### 3.2 快照与增量的衔接：位点、一致性与那把全局锁

CDC 冷启动要回答两个问题：**历史数据怎么拿**、**从哪个位点开始追增量**。两者必须对得上，否则中间会丢一段或重一段。

```text
T0  FLUSH TABLES WITH READ LOCK          -- 只在这一步持锁
T1  SHOW BINARY LOG STATUS（8.4+；旧版 SHOW MASTER STATUS）→ file, pos, gtid_executed
T2  UNLOCK TABLES                        -- 锁通常只持有几十毫秒
T3  START TRANSACTION WITH CONSISTENT SNAPSHOT（REPEATABLE READ）
T4  分块 SELECT 每张表 → 发 op=r 事件 → 提交
T5  从 T1 的位点开始读 binlog → 发 op=c/u/d 事件
```

锁的作用只是让「读位点」和「确定快照起点」之间没有新事务插进来，**不是锁住整个快照过程**。Debezium 的 `snapshot.lock.timeout.ms` 默认 10 秒，拿不到锁就让快照失败重试，而不是无限等待把业务拖死；锁被长时间占用时 `HoldingGlobalLock=1` 会一直为真，这是最该告警的指标之一。

`FLUSH TABLES WITH READ LOCK` 的替代方案，按侵入性从低到高：

1. **在只读副本上快照**：锁只影响副本，主库写入无感；前提是副本延迟为 0，且连接器能读到副本的 binlog。
2. **`snapshot.lock.timeout.ms=0`**：拿不到锁立即失败，由调度层在低峰重试，用「失败重试」换「绝不阻塞业务」。
3. **`snapshot.mode=no_data` + 增量快照**：先只同步 schema，再用信号表触发 `execute-snapshot`，按 chunk 读取、不持全局锁；适合超大表，代价是链路要分两步编排。

位点选 GTID 还是 file+pos，直接决定主从切换后能不能续上：

| 位点 | 形态 | 主从切换后 | 建议 |
|---|---|---|---|
| **file + pos** | `mysql-bin.000003:154` | 新主库的文件名/偏移可能完全不同，位点错位 | 单机或从不切换的库 |
| **GTID** | `3e11fa47-...:1-100` | 全局唯一，新主从集合能识别已执行事务 | 生产默认，必须开 `gtid_mode=ON` |

还有一个反直觉的现象：快照读到的行，可能比随后重放的 binlog 事件更新。某行在 T1 之后被改过，快照里已是 T3 的值，而 T1 的事件稍后才到；下游无条件覆盖会**短暂回退**到旧值，等 T3 事件到达才恢复。解法是让写入单调：比较 `source.ts_ms` 或业务版本号，旧事件直接丢弃。

### 3.3 Debezium 的架构：Connector → Kafka Connect → Topic

```text
MySQL ──binlog──► Debezium Connector（快照线程 + binlog client）
                        │ SourceRecord
                        ▼
                 Kafka Connect Worker（SMT、序列化）
                        │
        ┌───────────────┼────────────────┐
        ▼               ▼                ▼
  <prefix>.<db>.<table>   ...      <prefix>.transaction
        └── offset 存 connect-offsets，schema 历史存 schema-changes.<prefix>
```

连接器本身不写 Kafka，它把 `SourceRecord` 交给 Kafka Connect；**offset 与 schema history 都存在 Kafka 内部 topic**，所以 Connect 集群是无状态的，换一台 worker 也能接着跑。表级 topic 名是 `<topic.prefix>.<database>.<table>`。

```json
{
  "before": { "id": 1001, "status": "PENDING" },
  "after":  { "id": 1001, "status": "PAID" },
  "source": { "version": "3.7.1.Final", "connector": "mysql", "db": "inventory",
              "table": "orders", "ts_ms": 1786435255000, "snapshot": "false",
              "gtid": "3e11fa47-...:42", "file": "mysql-bin.000003", "pos": 1572, "row": 0 },
  "op": "u", "ts_ms": 1786435255680,
  "transaction": { "id": "3e11fa47:42", "total_order": 1 }
}
```

| 字段 | 含义 | 使用注意 |
|---|---|---|
| `before` | 变更前整行；`c` 时为 `null` | 受 `binlog_row_image` / `REPLICA IDENTITY` 影响，可能只有主键 |
| `after` | 变更后整行；`d` 时为 `null` | `r`（快照读）与 `c`/`u` 都从这里取新值 |
| `op` | `c` 新增、`u` 更新、`d` 删除、`r` 快照读、`t` truncate | 不要只处理 `c/u`，否则快照数据全丢 |
| `source` | 位点与元数据：库、表、GTID、file、pos、row | 做幂等键、排查「这条事件从哪来」 |
| `source.ts_ms` | **binlog 事件时间**（数据库侧） | 计算延迟用它，不要用顶层 `ts_ms` |
| `ts_ms` | 连接器**处理**这条事件的时间 | 与 `source.ts_ms` 的差值≈链路延迟 |

Kafka 消息的 key 是主键 JSON（如 `{"id":1001}`），Debezium 用它做分区，所以**同一主键天然落同一分区、保持顺序**。没有主键的表 key 为 `null`，会被轮询到不同分区，更新和删除都无法定位行——这不是配置问题，是模型问题。

### 3.4 Canal 的架构：伪装成从库，自己管缓冲

```text
MySQL ──dump 协议（server_id=伪从库）──► Canal Server
                                          ├─ eventParser：协议解析
                                          ├─ eventSink：过滤/加工
                                          ├─ eventStore：环形缓冲 + ack
                                          └─ metaManager：位点持久化
                                                │
                              ┌─────────────────┴─────────────────┐
                              ▼                                   ▼
                        TCP 客户端（ack）              Kafka / RocketMQ / RabbitMQ
```

Canal 把「伪装从库」做得很彻底：它向 MySQL 发 `COM_BINLOG_DUMP`，用自己的 `server_id` 注册，MySQL 认为多了一个副本，开始推 binlog。区别在于它不写 relay log、不重放 SQL，而是解析成结构化事件放进 `eventStore`，客户端按 batch 拉取并 ack 位点，ack 之后才推进消费位置。

它和 Debezium 的差异不在「能不能捕获」，而在**职责边界**：

| 维度 | Debezium | Canal |
|---|---|---|
| 部署形态 | Kafka Connect 集群 + Kafka（或 Server 单机） | 独立 JVM；HA 依赖 ZooKeeper 选主 |
| 全量快照 | 连接器内置，一致性快照 + 增量快照 | Server **不做**快照，全量由 canal-adapter 另起任务 |
| Schema | DDL 解析 + schema history topic | 解析 DDL 维护表结构，主要面向 MySQL |
| 下游协议 | Kafka 生态（任意客户端、SMT、Connect） | TCP 私有协议 / 直投 MQ；Java 客户端为主 |
| 生态绑定 | 多云、多源（MySQL/PG/Oracle/Mongo…） | 阿里系（RDS、ADB、DTS、RocketMQ） |
| 运维成本 | 高（Connect + Kafka + 内部 topic） | 中（一个 JVM + 可选 ZK） |

Canal 的取舍很清晰：**轻、贴 MySQL、与阿里云生态无缝**；代价是快照能力弱、跨源支持少、社区迭代慢（1.1.8 是 2025-01 的版本）。Debezium 反过来：重，但把「快照、schema 历史、事务元数据、多 sink」都做进了平台。

### 3.5 事务边界与顺序：单主键有序，跨表要自己拼

| 范围 | 是否有序 | 是否原子 | 说明 |
|---|---|---|---|
| 单表、单主键 | **是** | 事件级 | key=主键，落同一分区；同一行的 c/u/d 按提交顺序到达 |
| 单表、跨主键 | 分区内有序，跨分区无序 | 否 | 不要假设「A 先于 B」 |
| 同库跨表事务 | 各自表内有序 | **否** | 默认逐条发，下游可能先看到订单、后看到库存 |
| 快照 + 增量 | 不保证 | 否 | 快照是「某一时刻的读」，与增量事件会重叠 |

跨表事务要还原，必须开事务元数据：连接器为每条事件加上 `transaction{id,total_order,data_collection_order}`，并额外写 `<topic.prefix>.transaction`，其中 `status` 为 `BEGIN`/`END`、带 `event_count`。消费端按 `transaction.id` 缓冲，收到 `END` 后再在一个本地事务里落库；`total_order` 排序，`data_collection_order` 表示该表在事务内的相对顺序。代价很实在：**连接器要在内存里攒完整个事务才能发**，大事务会直接放大堆内存（见 §6）。因此下游只关心单表时不要开它；若业务要求「订单与明细必须同时可见」，与其在消费端拼事务，不如把两张表合成一个聚合、发一条自包含事件——CDC 不负责帮你重新设计领域模型。

---

## 4. 使用实战

### 4.1 起一套 MySQL + Kafka + Debezium

```yaml
services:
  mysql:
    image: mysql:8.4
    command:
      - --server-id=1
      - --log-bin=mysql-bin
      - --binlog-format=ROW
      - --binlog-row-image=FULL
      - --binlog-row-metadata=FULL
      - --gtid-mode=ON
      - --enforce-gtid-consistency=ON
      - --binlog-expire-logs-seconds=604800
      - --mysql-native-password=ON
    environment:
      MYSQL_ROOT_PASSWORD: root
      MYSQL_DATABASE: inventory
    ports: ["3306:3306"]
    volumes: ["./init.sql:/docker-entrypoint-initdb.d/init.sql:ro"]
    healthcheck:
      test: ["CMD", "mysqladmin", "ping", "-h", "127.0.0.1", "-proot"]
      interval: 5s
      retries: 20

  kafka:
    image: apache/kafka:4.1.0
    environment:
      KAFKA_NODE_ID: 1
      KAFKA_PROCESS_ROLES: broker,controller
      KAFKA_LISTENERS: PLAINTEXT://:9092,CONTROLLER://:9093
      KAFKA_ADVERTISED_LISTENERS: PLAINTEXT://kafka:9092
      KAFKA_CONTROLLER_LISTENER_NAMES: CONTROLLER
      KAFKA_LISTENER_SECURITY_PROTOCOL_MAP: CONTROLLER:PLAINTEXT,PLAINTEXT:PLAINTEXT
      KAFKA_CONTROLLER_QUORUM_VOTERS: 1@kafka:9093
      KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR: 1
      CLUSTER_ID: MkU3OEVBNTcwNTJENDM2Qk

  connect:
    image: quay.io/debezium/connect:3.7.1.Final
    depends_on: { mysql: { condition: service_healthy }, kafka: { condition: service_started } }
    environment:
      BOOTSTRAP_SERVERS: kafka:9092
      GROUP_ID: cdc
      CONFIG_STORAGE_TOPIC: connect-configs
      OFFSET_STORAGE_TOPIC: connect-offsets
      STATUS_STORAGE_TOPIC: connect-status
      CONFIG_STORAGE_REPLICATION_FACTOR: 1
      OFFSET_STORAGE_REPLICATION_FACTOR: 1
      STATUS_STORAGE_REPLICATION_FACTOR: 1
      CONNECT_KEY_CONVERTER: org.apache.kafka.connect.json.JsonConverter
      CONNECT_VALUE_CONVERTER: org.apache.kafka.connect.json.JsonConverter
      CONNECT_KEY_CONVERTER_SCHEMAS_ENABLE: "false"
      CONNECT_VALUE_CONVERTER_SCHEMAS_ENABLE: "false"
    ports: ["8083:8083"]
```

```sql
-- init.sql：业务表 + CDC 账号（RELOAD 是 FTWRL 的前提）
CREATE TABLE inventory.orders (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  order_no VARCHAR(32) NOT NULL,
  status VARCHAR(16) NOT NULL,
  amount DECIMAL(10,2) NOT NULL,
  updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id), UNIQUE KEY uk_order_no (order_no)
) ENGINE=InnoDB;

CREATE USER 'debezium'@'%' IDENTIFIED WITH mysql_native_password BY 'dbz';
GRANT SELECT, RELOAD, SHOW DATABASES, REPLICATION SLAVE, REPLICATION CLIENT ON *.* TO 'debezium'@'%';
```

```bash
docker compose up -d
curl -s -X POST http://localhost:8083/connectors -H 'Content-Type: application/json' -d '{
  "name": "inventory-connector",
  "config": {
    "connector.class": "io.debezium.connector.mysql.MySqlConnector",
    "tasks.max": "1", "database.hostname": "mysql", "database.port": "3306",
    "database.user": "debezium", "database.password": "dbz",
    "database.server.id": "184054", "topic.prefix": "inventory",
    "table.include.list": "inventory.orders,inventory.outbox",
    "schema.history.internal.kafka.bootstrap.servers": "kafka:9092",
    "schema.history.internal.kafka.topic": "schema-changes.inventory",
    "database.connectionTimeZone": "UTC",
    "decimal.handling.mode": "string",
    "provide.transaction.metadata": "true"
  }
}'
curl -s http://localhost:8083/connectors/inventory-connector/status
docker compose exec kafka /opt/kafka/bin/kafka-console-consumer.sh \
  --bootstrap-server kafka:9092 --topic inventory.inventory.orders --from-beginning --max-messages 3
```

`database.server.id` 必须与 MySQL 的 `server-id` 不同，否则会抢同一个副本槽位；MySQL 连接器**永远只有一个 task**，`tasks.max` 调大不会并行，真正的并行在 3.x 的快照 chunk 上。

### 4.2 Go 消费：解析信封、四种 op、tombstone

```go
package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"os"
	"os/signal"
	"sync"
	"syscall"

	"github.com/segmentio/kafka-go"
)

type Envelope struct {
	Before map[string]any `json:"before"`
	After  map[string]any `json:"after"`
	Source Source         `json:"source"`
	Op     string         `json:"op"`
}

type Source struct {
	Table string `json:"table"`
	TsMs  int64  `json:"ts_ms"`
}

var (
	mu   sync.Mutex
	rows = map[string]map[string]any{} // PK JSON -> 行，仅演示；生产按主键 UPSERT
)

func main() {
	r := kafka.NewReader(kafka.ReaderConfig{
		Brokers:     []string{env("KAFKA_BROKERS", "localhost:9092")},
		GroupID:     "cdc-sync",
		Topic:       env("KAFKA_TOPIC", "inventory.inventory.orders"),
		StartOffset: kafka.FirstOffset,
		MinBytes:    1,
		MaxBytes:    10 << 20,
	})
	defer r.Close()

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	for {
		m, err := r.FetchMessage(ctx)
		if err != nil {
			if ctx.Err() != nil {
				return
			}
			slog.Error("fetch", "err", err)
			continue
		}
		if err := handle(m); err != nil {
			slog.Error("handle", "offset", m.Offset, "err", err) // 不提交 → 重启后重放
			continue
		}
		if err := r.CommitMessages(ctx, m); err != nil {
			slog.Error("commit", "offset", m.Offset, "err", err)
		}
	}
}

func handle(m kafka.Message) error {
	if m.Value == nil { // tombstone：compact topic 的删除标记
		return nil
	}
	var ev Envelope
	if err := json.Unmarshal(m.Value, &ev); err != nil {
		return fmt.Errorf("decode event: %w", err)
	}
	key := string(m.Key) // 主键 JSON，如 {"id":1001}
	switch ev.Op {
	case "r", "c", "u": // 快照读 / 新增 / 更新，统一 upsert
		mu.Lock()
		defer mu.Unlock()
		if key == "" {
			return fmt.Errorf("table %s has no primary key", ev.Source.Table)
		}
		rows[key] = ev.After // 生产：INSERT ... ON DUPLICATE KEY UPDATE
		return nil
	case "d":
		mu.Lock()
		defer mu.Unlock()
		delete(rows, key) // 生产：DELETE ... WHERE 主键 = ?
		return nil
	default: // t=truncate 等
		slog.Warn("skip op", "op", ev.Op, "table", ev.Source.Table)
		return nil
	}
}

func env(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}
```

三个细节决定这段代码在生产能不能活：

- **`r` 必须当 upsert**：快照事件不是「插入」，下游已有数据时要覆盖；只处理 `c/u` 会在重做快照时留下旧文档。
- **tombstone 是正常的**：`tombstones.on.delete=true`（默认）时，删除事件后跟一条 value 为 `null` 的消息，用于 compact topic 的物理清理；当成解析错误会让消费者卡死。
- **先提交业务、再提交 offset**：反了就会丢消息。重复靠主键 upsert 吸收；需要精确去重时，另存 `(gtid,file,pos)` 并定期清理。

### 4.3 Outbox 表 + Outbox Event Router：与业务写入同事务

CDC 最大的优势是零侵入，但「业务事件」往往需要业务语义（订单已支付、库存已锁定）。折中做法是**仍然写一张 outbox 表，但由 CDC 负责搬运**：业务只多做一次同库 INSERT，不碰 Kafka。

```sql
CREATE TABLE outbox (
  id            CHAR(36)    NOT NULL,
  aggregatetype VARCHAR(32) NOT NULL,   -- 路由维度：order / payment
  aggregateid   VARCHAR(64) NOT NULL,   -- 分区 key：订单号
  type          VARCHAR(64) NOT NULL,   -- 事件类型：order.paid
  payload       JSON        NOT NULL,
  created_at    DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id), KEY idx_created (created_at)
) ENGINE=InnoDB;

START TRANSACTION;
UPDATE orders SET status = 'PAID' WHERE order_no = 'A1001' AND status = 'PENDING';
INSERT INTO outbox (id, aggregatetype, aggregateid, type, payload)
VALUES (UUID(), 'order', 'A1001', 'order.paid', JSON_OBJECT('order_no', 'A1001', 'amount', 19.90));
COMMIT;  -- 业务行与事件行同生共死，没有双写窗口
```

在连接器配置里加一个 SMT：

```json
{
  "transforms": "outbox",
  "transforms.outbox.type": "io.debezium.transforms.outbox.EventRouter",
  "transforms.outbox.predicate": "isOutbox",
  "predicates": "isOutbox",
  "predicates.isOutbox.type": "org.apache.kafka.connect.transforms.predicates.TopicNameMatches",
  "predicates.isOutbox.pattern": "inventory\\.inventory\\.outbox",
  "transforms.outbox.route.by.field": "aggregatetype",
  "transforms.outbox.route.topic.replacement": "outbox.event.${routedByValue}",
  "transforms.outbox.table.field.event.key": "aggregateid",
  "transforms.outbox.table.field.event.payload": "payload",
  "transforms.outbox.table.expand.json.payload": "true",
  "transforms.outbox.table.fields.additional.placement": "type:header:eventType"
}
```

SMT **只处理 `op=c`**，所以 outbox 只该 INSERT、不该 UPDATE；`aggregateid` 变成 Kafka key，保证同一订单的事件有序；`eventType` 放在 header 里，消费端不解析 body 就能路由。outbox 行在事件进入 Kafka 后可按时间清理（例如保留 3 天），但**连接器停机期间别清**——binlog 里还有，行没了不影响追增量，可一旦重做快照就补不回来了。

### 4.4 Schema 变更：让下游「不认识的新列」不致命

| 变更 | 风险 | 做法 |
|---|---|---|
| **加列**（可空/有默认） | 低 | 消费端忽略未知字段；Avro/Protobuf 需 registry 兼容级别 `BACKWARD` |
| **加列**（NOT NULL 无默认） | 中 | 老行快照会带 `null`，下游必须容忍；分两次 DDL |
| **改类型** `INT → BIGINT` | 中 | JSON 数字无感；强类型 schema 可能不兼容，按 expand/contract 走 |
| **改名 / 删列** | **高** | 先加新列双写 → 回填 → 切读 → 删旧列，不要一步到位 |

两个具体坑：`decimal.handling.mode` 默认 `precise`，JSON 里是二进制/base64 结构而不是 `19.90`，Go 侧解析会直接失败——建议显式设成 `string`；MySQL 的 `time.precision.mode` 默认 `adaptive_time_microseconds`，`DATETIME(6)` 会变成微秒整数，和 `connect` 模式输出的毫秒时间戳不是同一种东西。**跨语言消费时，一律在连接器侧把类型收敛成字符串/整数，别让每个消费者各自猜。**

### 4.5 延迟与积压：怎么判断「追平了」

| 指标（JMX） | 含义 | 判读 |
|---|---|---|
| `MilliSecondsBehindSource` | binlog 事件时间到连接器处理时间的差 | 稳态应 < 1s；持续升高说明追不上 |
| `QueueRemainingCapacity` | 事件队列剩余容量 | 长期为 0 = 写 Kafka 反压，先查 broker/网络 |
| `SnapshotCompleted` / `RemainingTableCount` | 快照是否完成 / 还剩几张表 | 1 且 0 才算快照结束 |
| `HoldingGlobalLock` | 是否持有全局读锁 | 长期为 1 = 业务写入被阻塞，最高优先级告警 |

```promql
# 延迟超过 5s（指标前缀取决于 jmx_exporter 规则）
max by (server) (debezium_metrics_MilliSecondsBehindSource{context="binlog"}) > 5000
```

「追平」不能只看 Kafka lag：lag=0 只说明消费到了 Kafka 末尾，不说明 Kafka 里有最新数据。可靠做法是写一条 **canary 行**（专用表，每 10 秒更新一次），端到端量 `now - 消费到的 source.ts_ms`；这个数稳定在阈值内才叫真追平。再配一条 `MAX(updated_at)` 与下游最大时间戳的对账，防止「连接器活着但表被过滤掉了」。

---

## 5. 版本与生态

| 组件 | 版本（截至 2026-11） | 与本篇相关的要点 |
|---|---|---|
| **Debezium** | **3.7.1.Final** | Java 17+；3.0 基线 Kafka 3.8，3.2 起支持 Kafka 4.x，3.3 起核心连接器支持 Exactly-once（KIP-618）；3.x 快照支持 chunk 并行 |
| **Canal** | **1.1.8**（2025-01） | 支持 MySQL 5.1–8.0；`serverMode` 可选 tcp/kafka/rocketmq/rabbitmq/pulsar；canal-admin 管配置，canal-adapter 做全量+写下游 |
| **Kafka** | **4.x** | KRaft-only；Connect 的 EOS 要求 3.3+；Debezium 3.3 起以 Kafka 4.1 为构建基线 |
| **Flink CDC** | **3.x** | Pipeline 连接器把「全量+增量」做成一个 Flink 作业，靠 checkpoint 保证端到端一次 |

Debezium 3.x 的几个升级要点：

- **配置改名**：`database.history.*` 在 2.x 已废弃，3.x 用 `schema.history.internal.*`；老配置直接启动会报缺参。
- **信号表**：增量快照必须有 `signal.data.collection` 指向的表（示例名 `debezium_signal`，列为 `id/type/data`）；3.0 移除了 `additional-condition`，只认 `additional-conditions`。
- **Exactly-once**：worker 设 `exactly.once.source.support=enabled`，连接器设 `exactly.once.support=required`，`transaction.boundary` 保持默认 `poll`；它只保证「源记录 + offset」原子写 Kafka。

Canal 与阿里生态的绑定是优势也是天花板：RDS、ADB、DTS、RocketMQ 一条链路很顺，canal-adapter 直接写 ES/HBase；但跨源（PG、Oracle、Mongo）没有对等能力，社区节奏也慢。Flink CDC 则是另一个物种：它把 CDC 当**流式 ETL 的 source**，优势是无需 Kafka、全增量一体、checkpoint 容错；若需要「多个异构系统订阅同一份变更」，仍要落到 Kafka，而不是让每个团队各起一个 Flink 作业。

---

## 6. 常见坑与最佳实践

1. **大事务打爆堆内存**：`max.queue.size`（默认 8192 条）、`max.batch.size`（2048 条）只是队列上限；真正危险的是开了 `provide.transaction.metadata` 后连接器要缓冲整个事务。先限事务大小，再调 `max.queue.size.in.bytes` 和堆。
2. **DDL 解析失败导致连接器停**：Debezium 用 DDL 维护 schema，遇到不认识的语法直接 `ParsingException`，task 变 FAILED 并在同一条 binlog 上循环重启。DDL 先在 canary 表跑一遍；救急可开 `schema.history.internal.skip.unparseable.ddl`，但那意味着 schema 历史已不可信。
3. **binlog 过期，位点永久失效**：`binlog_expire_logs_seconds` 小于「最长停机时间」时，重启会报找不到日志文件，只能重做快照。保留期至少 7 天，并对延迟与连接器存活同时告警。
4. **重做快照时沿用旧 offset**：只改 `snapshot.mode=initial` 没用——offset 还在，连接器认为没必要快照。要么删连接器并清 `connect-offsets`，要么用新名字 + 新 `topic.prefix` 起一套再切流量。
5. **以为 EOS 能端到端去重**：Kafka Connect 的 exactly-once 只覆盖「源 → Kafka」，Kafka 到下游仍是 at-least-once；非幂等副作用（发短信、扣款）要单独去重。
6. **用时间戳当幂等键**：`ts_ms` 每次重放都会变，去重必然失效。稳定键是 `source.gtid` 或 `source.file + pos`；更稳的是直接用主键 upsert，让重复天然无害。
7. **`before` 里没有完整旧值**：MySQL 的 `binlog_row_image=MINIMAL` 只给主键和被改列；PostgreSQL 默认 `REPLICA IDENTITY DEFAULT` 同样只给主键，要完整旧值得 `ALTER TABLE ... REPLICA IDENTITY FULL`（代价是 WAL 放大）。
8. **时区导致快照与增量差 8 小时**：`TIMESTAMP` 由驱动按连接时区归一化，`DATETIME` 不带时区。显式设 `database.connectionTimeZone=UTC`、MySQL `default-time-zone='+00:00'`、JVM `-Duser.timezone=UTC`，三处必须一致。
9. **快照锁把主库写挂**：`FLUSH TABLES WITH READ LOCK` 期间所有写入排队；大表 DDL、长事务会让锁等更久。把 `snapshot.lock.timeout.ms` 设为可控值，优先在只读副本快照，超大表改增量快照。
10. **无主键表**：key 为 `null`，事件被轮询到各分区，同一行的顺序和删除都无法保证。补主键，或用 `message.key.columns` 指定业务唯一键。
11. **内部 topic 副本数沿用单机配置**：demo 里 `connect-offsets`、`connect-configs` 和 schema history topic 都是 1 副本；生产照抄的话 broker 一挂就丢位点或 schema 历史，恢复时可能重复快照。按 broker 数把 worker 的 `*.replication.factor` 提到 3、`min.insync.replicas=2`，schema history topic 也要单独确认副本数。
12. **监控只看 connector 状态**：`/connectors/{name}/status` 里 `connector.state=RUNNING` 但 `tasks[].state=FAILED` 很常见；必须分别判断，并对 task 重启次数告警。
13. **大字段撑爆单条消息**：LONGBLOB/长文本会让单条事件超过 Kafka `max.request.size`（默认 1MB），表现为 `RecordTooLargeException`。用 `producer.override.max.request.size` 放大，同时评估是否该把大字段拆表。
14. **把 CDC 当强一致读**：端到端延迟通常 100ms~数秒，写完立刻读下游必然读不到。「读己之写」要走主库或本地缓存，不能指望 CDC 追平。

---

## 7. 选型对比

| 维度 | Debezium | Canal | Maxwell | Flink CDC |
|---|---|---|---|---|
| **部署复杂度** | 高：Kafka Connect + Kafka + 内部 topic | 中：独立 JVM，HA 需 ZK | **低**：单进程 + 配置 | 高：Flink 集群 + checkpoint 存储 |
| **全量快照** | 内置一致性快照 + 增量快照 | Server 不做，adapter 另起全量任务 | 支持 bootstrap | 内置，全增量一体 |
| **Schema 演进** | 强：DDL 解析 + schema history | 中：解析 DDL，主要面向 MySQL | 弱：基本透传 | 强：部分链路支持 schema evolution |
| **事务边界** | 有 transaction metadata topic | 有 begin/end 事件 | 有 `xid` | 有 changelog 语义 |
| **投递语义** | at-least-once；3.3+ 源→Kafka EOS | at-least-once | at-least-once | checkpoint 端到端一次 |
| **输出** | Kafka（或 Server 到 Pulsar/Kinesis 等） | TCP / Kafka / RocketMQ | Kafka / Kinesis / SQS | Flink 表 / 湖仓 / Kafka |
| **生态** | 最广：多源、SMT、Operator、云托管 | 阿里系：RDS/ADB/DTS/RocketMQ | 轻量工具 | 流计算生态 |
| **适合场景** | 跨国队、多云、多源、长期演进 | 阿里云 RDS、RocketMQ 体系 | 小团队快速接 Kafka | 流式 ETL、湖仓入湖、实时宽表 |

一句话决策：

- **多云 / 跨国队 / 多源（MySQL + PG + Mongo）** → Debezium。平台复杂度高，但快照、schema 历史、事务元数据、SMT 都是现成的，出问题也有指标可查。
- **阿里云 RDS + RocketMQ 生态、只要 MySQL** → Canal。部署轻、链路短，配 canal-adapter 能直接写 ES/HBase。
- **只想把变更丢进 Kafka，不想维护 Connect** → Maxwell。单进程、配置少，功能也少，适合小规模。
- **目标是湖仓/数仓的实时入湖，而不是给多个业务订阅** → Flink CDC。别用它替代事件总线，它的强项是「全量 + 增量 + 变换 + 写入」的一条作业。
- **写入路径只有一两处、团队没有 Kafka 运维能力** → 先别上 CDC。直接在写库的同一个本地事务里写 outbox，用轮询投递，复杂度低一个数量级。

最后一条经验：CDC 的难点从来不是「把 binlog 读出来」，而是**位点、幂等、schema 演进**这三件长期维护的事。选型时先问「谁负责在凌晨三点处理 task FAILED」，而不是「谁的功能多」。

参考：<https://debezium.io/documentation/> · <https://github.com/alibaba/canal/wiki> · <https://nightlies.apache.org/flink/flink-cdc-docs-stable/>
