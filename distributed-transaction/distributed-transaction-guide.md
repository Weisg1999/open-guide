# 分布式事务技术指南

> 跨服务写操作没有免费的原子性：要么用 2PC 换一致性，要么用 Saga/Outbox 换可用性，代价是补偿、幂等与对账。
> 参考实现：Temporal **1.3x** · Kafka **4.x** · RocketMQ **5.x** · MySQL **9.7 LTS**　|　上游：<https://github.com/temporalio/temporal> · <https://github.com/apache/rocketmq>　|　协议：各自上游为准

---

## 1. 项目简介

[MySQL 篇](../mysql/mysql-guide.md) 讲的是**单机事务**：`BEGIN` 到 `COMMIT` 之间，InnoDB 用 redo/undo 保证原子性与隔离性。但这条边界很窄——**一个连接、一个 InnoDB 实例**。业务一旦变成「订单库扣库存 + 支付服务记账 + 仓库服务出库」，ACID 就失效了：跨服务没有共享的 redo log，也没有能同时锁住两边的锁管理器。

[Temporal 篇](../temporal/temporal-guide.md) 解决「长流程怎么可靠跑完」，本篇解决「多个服务各写各的库，数据怎么才能对上」。两者是**引擎与模式**的关系：Temporal 提供持久化执行与重试骨架，本篇给出一致性模式本身、存储层细节与失败边界。[RabbitMQ 篇](../rabbitmq/rabbitmq-guide.md) 讲消息怎么可靠投递，本篇讲**消息与本地事务怎么绑成一个整体**。

本文的立场不是「教你上框架」，而是先判断这里到底需不需要分布式事务：

| 场景 | 是否跨事务边界 | 推荐做法 |
|---|---|---|
| 同库同实例的多表写入 | 否 | 本地事务，直接 `BEGIN/COMMIT` |
| 同库写入 + 发一条 MQ 消息 | **是**（DB 与 MQ 两个系统） | 本地消息表 / Outbox |
| 两个服务各写自己的库 | **是** | Saga，优先于 TCC |
| 同机房两个库、短事务、强一致 | 是，但可控 | XA / Seata AT |
| 要求「要么都成功、要么立刻都失败」 | 是 | 只有 2PC 系，并接受其代价 |

判断标准只有一条：**这次写操作的失败，能不能靠「重试 + 补偿 + 对账」在可接受时间内收敛**。能，就是最终一致，别上 2PC；不能（钱、额度、库存的中间态不可见），才考虑强一致，并且优先想办法把两个写放回同一个库。

---

## 2. 能力总览

| 方案 | 一致性 | 业务侵入 | 隔离性 | 依赖组件 | 典型落点 |
|---|---|---|---|---|---|
| **2PC / XA** | 强一致（提交后必一致） | 低（驱动层协议） | 有（prepare 后持锁） | 支持 XA 的 DB + TM | 同机房跨库、遗留系统 |
| **TCC** | 最终一致（Confirm 后一致） | **高**（3 个接口 + 控制表） | 靠资源预留实现软隔离 | 事务协调器 | 支付、优惠券、额度 |
| **Saga** | 最终一致 | 中（每步要有补偿） | **无**，中间态可见 | 编排引擎或自研状态机 | 订单履约、跨服务长流程 |
| **本地消息表** | 最终一致 | 中（多一张表 + 投递任务） | 无 | DB + 定时任务 + MQ | 存量系统改造 |
| **事务性发件箱（Outbox）** | 最终一致 | 中（只写不投递） | 无 | DB + Relay（轮询/CDC）+ MQ | 新系统默认选择 |
| **事务消息** | 最终一致 | 中（实现回查接口） | 无 | RocketMQ 5.x | 已在用 RocketMQ 的团队 |
| **CDC 订阅** | 最终一致 | **最低**（不改业务代码） | 无 | Kafka Connect + 日志权限 | 存量库、跨团队同步 |

三条硬约束：**没有免费的原子性**（要么写时协调，要么写后补偿）；**异步方案都是 at-least-once**（「恰好一次」是幂等 + 去重的效果）；**中间态一定会被看到**（存在「钱扣了但券没发」的窗口，产品和前端必须显式表达）。

---

## 3. 核心概念与原理

### 3.1 2PC / XA：同步阻塞与协调者单点

```text
协调者(TM)                  参与者 A (DB)         参与者 B (DB)
   │── prepare ─────────────►│ 写 redo、持锁、投票 │
   │── prepare ────────────────────────────────────►│
   │◄───────── yes ──────────│                     │
   │   ← 此刻起参与者都不能单方面放手
   │── commit ──────────────►│ 提交、释放锁        │
```

**同步阻塞**的本质在第二步：prepare 成功后参与者已把决定权交出去，必须**持有行锁与连接**等最终指令，其他事务碰到同一行只能等。等于把「单机事务的锁持有时间」放大成「跨服务的网络往返时间」。

XA 在 MySQL 里的实际代价：

- **两轮额外 RTT**：`XA START` → 业务 SQL → `XA END` → `XA PREPARE` → `XA COMMIT`，跨可用区时单次 RTT 就到毫秒级。
- **锁持有时间 = 业务耗时 + TM 决策时间**：prepare 到 commit 的间隔由协调者决定，热点行上这是吞吐量的直接杀手。
- **连接被 xid 绑定**：从 `XA START` 到 `XA COMMIT` 必须用同一连接，无法归还池子，有效容量下降。
- **崩溃后是 in-doubt 状态**：TM 在 prepare 后挂掉，参与者一直挂在 prepared，只能靠 `XA RECOVER` 列出后由外部裁决——这就是**协调者单点**，也是 XA 最难运维的失败模式；加上它只对 InnoDB 生效、与 binlog 的两阶段提交叠加，恢复路径复杂、排查成本极高。

结论：XA 适合**同机房、短事务、低并发、强一致**的场景（账务日终、遗留系统对接）。高并发主交易链路不要用——悬挂锁比不一致更难处理。

### 3.2 TCC：Try / Confirm / Cancel 与三大问题

| 阶段 | 做什么 | 失败时 |
|---|---|---|
| **Try** | 检查并**预留**（冻结 100 元，不是扣 100 元） | 预留失败 → 全局回滚 |
| **Confirm** | 使用预留，真正扣减（冻结 → 扣除） | 必须成功，靠重试 |
| **Cancel** | 释放预留（解冻） | 必须成功，靠重试 |

它比 2PC 好在**不长期持锁**（Try 结束即提交本地事务），代价是三套接口 + 三个必须处理的问题：

| 问题 | 现象 | 根因 | 解法 |
|---|---|---|---|
| **空回滚** | Cancel 先于 Try 到达 | Try 超时但实际未执行，TM 判定失败发起 Cancel | Cancel 发现无 Try 记录时，插入 `CANCELLED` 标记并正常返回 |
| **悬挂** | Cancel 执行完，Try 又到了 | Try 请求在网络里滞留后重放 | Try 执行前先查控制表，有 `CANCELLED` 记录则拒绝 |
| **幂等** | 同一分支被重复 Confirm/Cancel | 重试、超时重发 | 控制表 `(xid, branch_id)` 唯一 + 状态机 CAS |

控制表就三列：`tcc_branch(xid, branch_id, status)`，`status` 取 `1=TRIED / 2=CONFIRMED / 3=CANCELLED`。规则写死：**Try 只能「无记录」→ `TRIED`**；**Cancel 只能「无记录/TRIED」→ `CANCELLED`**（无记录也插入，这就是空回滚标记）；**Confirm 只能 `TRIED` → `CONFIRMED`**。三步全是带条件更新，重放天然幂等。真正的难点不在协议而在**资源模型**：券要能锁定、库存要能预占、余额要能冻结；没有预留语义（如第三方支付已划走钱），TCC 就退化成「直接扣 + 退款」，那就是 Saga。

### 3.3 Saga：编排与协同，以及补偿的不可逆性

Saga 把长事务拆成一串本地事务，正向失败就反向执行已完成的步骤（`T1→T2→T3→T4`，T3 失败则执行 `C2、C1`）。**编排（orchestration）**有中心协调者（Temporal Workflow、自研状态机）显式定义顺序、超时、重试、补偿；**协同（choreography）**每个服务监听事件自己决定下一步，没有中心：

| 维度 | 编排 | 协同 |
|---|---|---|
| 全局可见性 / 排障 | **强**：一处代码看全，看编排实例状态 | 弱：散落在各服务消费者里，要串日志与消息轨迹 |
| 耦合 / 循环依赖 | 协调者知道所有参与者，循环风险低 | 只认事件、耦合低，但**容易绕成环** |
| 加一步的成本 | 改协调者 | 改多个服务 + 事件契约 |
| 适合 | 步骤固定、需人工介入、需补偿 | 步骤少、事件天然广播、团队边界清晰 |

**补偿的不可逆性**是最容易被低估的地方：短信和推送收不回来；第三方支付已结算只能走退款（有手续费、到账延迟、还可能失败）；已发货只能退货；税控、银行、物流根本不提供反向接口。所以**可补偿步骤要前置**并承担主要逻辑，**不可逆步骤后置**到「所有可能失败的前置都成功之后」，并接受最后一步失败只能人工介入。补偿本身也要幂等 + 有重试 + 有告警，否则「补偿失败」就是没人知道的脏数据。另一个常被忽略的点：Saga **没有隔离性**，T1 提交后、T3 完成前，其他请求能看到「订单已创建但库存未扣」；标准解法是**语义锁**——给业务行加中间状态（`CREATED` 而非 `PAID`），让查询与后续操作显式处理它，而不是指望数据库锁。

### 3.4 本地消息表与 Outbox：同库事务 + 至少一次

最朴素的「先写库再发 MQ」有两种必然失败：

```text
写库成功 ──✗ 进程崩溃 ──► 消息永久丢失（下游不知道）
发消息成功 ──✗ 写库回滚 ──► 下游收到一个不存在的订单
```

中间件的事务消息能解决第二种，但多数团队没有 RocketMQ。**Outbox 的思路是：把「要发的消息」也当业务数据，写进同一个库、同一个事务。**

```sql
INSERT INTO outbox (aggregate_id, event_type, payload)
VALUES ('A1001', 'order.created', '{"order_no":"A1001"}');
```

事务提交 → 事件一定存在；事务回滚 → 事件一定不存在。双写不一致被消灭，剩下的是**怎么把 outbox 行变成 MQ 消息**：

| 投递方式 | 延迟 | 成本 | 注意点 |
|---|---|---|---|
| **轮询（polling）** | 100ms~数秒 | 低，一个 goroutine | 多副本要 `FOR UPDATE SKIP LOCKED` 或 CAS 认领；扫表要控索引 |
| **CDC（Debezium）** | 100ms~1s | 中，要 Kafka Connect + 日志权限 | 只认 INSERT，不需要 status 字段；位点丢失会重放 |
| **事务消息** | 低 | 中，绑定 RocketMQ | 需回查接口，见 3.5 |

三个必须接受的事实：**至少一次**（投递成功但「标记 sent」失败，下轮重发，这是设计不是 bug）；**顺序只在单聚合内保证**（用 `aggregate_id` 作 Kafka key 落同一分区）；**outbox 会一直长**（必须有归档任务，否则索引膨胀拖慢投递扫描）。「本地消息表」是早期叫法（业务表 + message 表 + 定时投递），Outbox 更强调「只写不投递、由外部 relay 消费」，工程上是同一件事。

### 3.5 事务消息与 Kafka 事务：各自的边界

**RocketMQ 事务消息**用回查把「本地事务」和「发消息」粘起来：

```text
1. 发半消息（half message）→ Broker 存内部 Topic，消费者不可见
2. Broker 返回成功 → Producer 执行本地事务（写订单库）
3. 成功 → COMMIT，消息可见；失败 → ROLLBACK，丢弃半消息
4. 第 3 步响应丢了 → Broker 定时回查 checkLocalTransaction
   → 回查必须能明确回答「这条消息对应的本地事务到底提交了吗」
```

第 4 步是全部难点。回查**不能**靠「订单表里有没有这行」推断——本地事务可能还没提交，也可能被别的原因回滚。必须有**事务状态表**，在本地事务里同步写 `COMMIT/ROLLBACK`，回查只读它。回查有次数上限（默认 60s 一次、最多 15 次，以 broker 配置为准），超限后进死信/特殊 Topic 等人工处理。

**Kafka 事务**常被误解。它保证：producer 的一批写（可跨 topic/partition）原子可见；`sendOffsetsToTransaction` 让「消费 offset」与「生产消息」同事务；`read_committed` 的消费者看不到未提交/已 abort 的消息。它**不保证**「Kafka 写入 + 外部 DB 写入」的原子性——transaction coordinator 只裁决 Kafka 自己的分区。所以「消费 Kafka → 写 DB → 再发 Kafka」要么 DB 事务里写 outbox 再由 relay 发，要么把 offset 存进业务库与业务写入同事务，要么接受 at-least-once 用幂等表去重。Kafka 4.0 起 KIP-890 强化了服务端事务协议（防僵尸 producer 重复写），但边界没变。

### 3.6 幂等、去重与对账：最终一致的兜底

三层缺一层，就会在某个深夜出现对不上的数据。**幂等键**用**业务唯一号**（订单号、支付流水号、`order-created-12345`），不要用 MQ 的 message id——重发时它变了，等于没有幂等。**去重与状态机**两种写法都可靠，选一种用到底：

```sql
-- 去重表：与业务写入同事务，唯一键冲突即「已处理」
INSERT INTO consumed_event (consumer, event_id) VALUES ('inventory', 'order-created-12345');
-- 状态机 CAS：影响 0 行说明状态已变，天然幂等
UPDATE orders SET status = 'PAID' WHERE order_no = 'A1001' AND status = 'PENDING';
```

**对账**兜住前两层管不了的事（relay 挂三天、outbox 被误删、补偿写错）：

| 档位 | 频率 | 比对内容 | 处理 |
|---|---|---|---|
| 实时 | 分钟级 | outbox 积压量、最老事件年龄 | 告警，人工看 relay |
| 准实时 | 5~15 分钟 | 订单状态 vs 支付流水状态 | 自动补发/补偿 |
| 日终 | T+1 | 全量按业务号比对金额与状态 | 落差异表，人工处理 |

**「最终一致」的时长要写进产品文档**：同步双写约等于 RPC 耗时（几十 ms）；Outbox 轮询 100ms~5s；CDC 100ms~1s；人工兜底小时级。前端不要「转圈等一致」，而是**乐观展示 + 明确中间态**（「处理中，预计 10 秒内完成」）+ 可查询进度。用户能接受「稍后到账」，不能接受「页面显示成功但实际没有」。

### 3.7 从业务不变量反推方案：一张决策树

方案选择不该从「我们有什么框架」开始，而该从「这笔业务绝对不能出现什么状态」开始。四个问题按顺序问，前一个能收口就不要看后一个：

```text
Q1 这两个写能否放进同一个库、同一个本地事务？
   能（多数场景，尤其系统早期）──► 本地事务。到此结束
   不能 ──► Q2

Q2 中间态（钱扣了券没发）能否被用户看到并容忍几分钟？
   不能（余额、额度、库存不能为负）──► 强一致：XA / Seata AT
                                        仅限同机房、短事务、低并发
   能 ──► Q3

Q3 失败那一步的补偿是否可逆？
   可逆（解冻、退款、取消预占、回滚状态）──► Saga 或 TCC
   不可逆（短信、已发货、已结算、券已核销）──► 不可逆步骤后置 + 人工兜底

Q4 吞吐与延迟预算还剩多少？
   紧（单机 >1k TPS 或 P99 <100ms）──► 必须异步化，Outbox / Saga
   松 ──► 同步方案也可以接受
```

四个真实业务的落地结论：

| 业务 | 必须守住的不变量 | 中间态能否被看到 | 补偿可逆性 | 结论 |
|---|---|---|---|---|
| **下单扣库存** | 库存不为负、不超卖 | 能（「待支付」本身就是中间态） | 可逆（关单释放） | 同库则本地事务；跨库用 Outbox + 幂等扣减 + 超时关单释放。**不需要 TCC**，除非要做「下单即预占」 |
| **跨行转账** | 钱不能凭空出现或消失 | 不能（用户盯着余额） | 跨行不可逆（已入对方账） | 行内两账户同库 → 本地事务。跨行**根本不存在 2PC**：本地记账 + 清算指令 + 人行对账文件，错账靠 T+1 冲正 |
| **订单发券** | 不重复发、不超发 | 能（「发放中」可接受） | **不可逆**（券可能已被核销） | 落「发券任务」表 + 消费幂等 + 重试 + 死信 + 人工补发。绝不 Saga 反向「收回券」 |
| **跨系统开户** | 核心有户则外围必有档 | 能（分钟级） | 可逆但很重（要销户） | 编排式 Saga：核心开户 → 外围建档，失败走销户补偿；销户失败转人工工单 |

一条反直觉的结论：**上表四个场景里，三个的第一选择都不是分布式事务框架，而是「改边界」或「异步 + 对账」。** 「不要为了分布式而分布式」不是口号：引入 TM/TC 集群、TCC 三套接口、Saga 编排引擎，换来的常常只是把「数据不一致」变成「锁等待 + 悬挂事务 + 更复杂的排障」。真正的成本项永远是 3.6 的三层兜底——幂等、重试、对账；缺了它们，用什么协议都会在半年后还债。

---

## 4. 使用实战

目标：下单写订单 + 写 outbox（同一事务），后台 relay 投递 Kafka，库存消费者幂等扣减。三张表同库：`orders`（业务）、`consumed_event(consumer, event_id)`（去重）、`outbox(id, aggregate_id, event_type, payload, status, retry_count, next_retry_at, claimed_at, created_at)`，其中 `status` 为 `0 待投递 / 1 投递中 / 2 已投递`，索引建在 `(status, next_retry_at, id)` 上。

`next_retry_at` 必须有默认值：relay 用 `next_retry_at <= NOW(3)` 扫描，若该列为 NULL 且无默认，新插入的事件永远扫不到（`NULL <= x` 恒为 UNKNOWN）。最小可用的 DDL：

```sql
CREATE TABLE outbox (
  id            BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  aggregate_id  VARCHAR(64)  NOT NULL,
  event_type    VARCHAR(64)  NOT NULL,
  payload       JSON         NOT NULL,
  status        TINYINT      NOT NULL DEFAULT 0,          -- 0 待投递 / 1 投递中 / 2 已投递
  retry_count   INT          NOT NULL DEFAULT 0,
  next_retry_at DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  claimed_at    DATETIME(3)  NULL,
  sent_at       DATETIME(3)  NULL,                        -- 投递成功时间，便于排查「多久才送达」
  created_at    DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  KEY idx_pending (status, next_retry_at, id)
) ENGINE=InnoDB;
```

### 4.1 写入侧：业务行与事件行同事务

写入侧（`internal/order/create.go`）——业务行与事件行同事务，没有第二种写法：

```go
package order

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
)

// Create：提交才有事件，回滚就没有事件。
func Create(ctx context.Context, db *sql.DB, orderNo, sku string, qty, amount int64) error {
	tx, err := db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()

	res, err := tx.ExecContext(ctx,
		`INSERT INTO orders (order_no, amount, status) VALUES (?, ?, 'PENDING')`, orderNo, amount)
	if err != nil {
		return err // 1062 唯一键冲突 = 重复下单
	}
	id, _ := res.LastInsertId()
	payload, err := json.Marshal(map[string]any{
		"event_id": fmt.Sprintf("order-created-%d", id), // 稳定可推导，重放不变
		"order_no": orderNo, "sku": sku, "qty": qty,
	})
	if err != nil {
		return err
	}
	if _, err := tx.ExecContext(ctx,
		`INSERT INTO outbox (aggregate_id, event_type, payload)
		 VALUES (?, 'order.created', ?)`, orderNo, payload); err != nil {
		return err
	}
	return tx.Commit()
}
```

### 4.2 Relay：CAS 认领与投递

投递侧（`internal/relay/relay.go`）——先 CAS 认领 `0→1`，抢到才发布；网络调用放在事务外，不持锁做 IO：

```go
package relay

import (
	"context"
	"database/sql"

	"github.com/segmentio/kafka-go"
)

type pending struct {
	id      int64
	key     string
	typ     string
	payload []byte
}

// PublishOnce：CAS 认领让多副本天然不重复；换 FOR UPDATE SKIP LOCKED 也等价。
func PublishOnce(ctx context.Context, db *sql.DB, w *kafka.Writer) error {
	rows, err := db.QueryContext(ctx, `
		SELECT id, aggregate_id, event_type, payload FROM outbox
		WHERE status = 0 AND next_retry_at <= NOW(3) ORDER BY id LIMIT 100`)
	if err != nil {
		return err
	}
	var recs []pending
	for rows.Next() {
		var p pending
		if err := rows.Scan(&p.id, &p.key, &p.typ, &p.payload); err != nil { rows.Close(); return err }
		recs = append(recs, p)
	}
	rows.Close()
	for _, p := range recs {
		res, err := db.ExecContext(ctx,
			`UPDATE outbox SET status = 1, claimed_at = NOW(3) WHERE id = ? AND status = 0`, p.id)
		if err != nil {
			return err
		}
		if n, _ := res.RowsAffected(); n == 0 {
			continue // 被别的副本抢走
		}
		msg := kafka.Message{Key: []byte(p.key), Value: p.payload,
			Headers: []kafka.Header{{Key: "event_type", Value: []byte(p.typ)}}}
		if err := w.WriteMessages(ctx, msg); err != nil {
			// 退回待投递 + 指数退避；重复发布由消费端幂等兜住
			_, _ = db.ExecContext(ctx, `UPDATE outbox SET status = 0,
				retry_count = retry_count + 1,
				next_retry_at = NOW(3) + INTERVAL LEAST(POW(2, retry_count), 300) SECOND
				WHERE id = ?`, p.id)
			continue
		}
		_, _ = db.ExecContext(ctx,
			`UPDATE outbox SET status = 2, sent_at = NOW(3) WHERE id = ?`, p.id)
	}
	return nil
}
```

后台用 `time.Ticker` 每 200ms 调一次 `PublishOnce`，每轮再顺手回收「认领后崩溃」的行：`UPDATE outbox SET status = 0 WHERE status = 1 AND claimed_at < NOW(3) - INTERVAL 60 SECOND`。`kafka.Writer` 用 `&kafka.Hash{}` 作 Balancer（同 key 落同一分区 → 单聚合保序）+ `RequiredAcks: kafka.RequireAll`。

### 4.3 消费端：幂等扣减

消费侧（`internal/consumer/inventory.go`）——去重表与业务写入同事务，唯一键冲突直接当成功：

```go
package consumer

import (
	"context"
	"database/sql"
	"errors"
	"fmt"

	"github.com/go-sql-driver/mysql"
)

// HandleOrderCreated：先写去重表，再改业务；两者同事务，重放安全。
func HandleOrderCreated(ctx context.Context, db *sql.DB, eventID, sku string, qty int64) error {
	tx, err := db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()

	if _, err := tx.ExecContext(ctx,
		`INSERT INTO consumed_event (consumer, event_id) VALUES ('inventory', ?)`,
		eventID); err != nil {
		if isDuplicate(err) { return nil } // 已处理过，直接 ACK
		return err
	}
	res, err := tx.ExecContext(ctx,
		`UPDATE stock SET available = available - ? WHERE sku = ? AND available >= ?`,
		qty, sku, qty)
	if err != nil {
		return err
	}
	n, err := res.RowsAffected()
	if err != nil {
		return err
	}
	if n == 0 {
		// 影响 0 行 = 库存不足。必须回滚：去重行不能提交，否则消息被当成已处理，库存永久少扣
		return fmt.Errorf("inventory: insufficient stock for sku %s", sku)
	}
	return tx.Commit() // 提交后再 commit offset：宁可重放，不可丢
}

func isDuplicate(err error) bool {
	var me *mysql.MySQLError
	return errors.As(err, &me) && me.Number == 1062
}
```

不想自研 relay 就用 CDC 等价替代：Debezium 的 Outbox Event Router 只认 INSERT，直接把 outbox 行转成 Kafka 消息，`aggregateid` 作 key、`id` 进 header（默认列 `id/aggregatetype/aggregateid/type/payload`，需按现有表结构配置映射）。生产上必须监控 `status IN (0,1)` 的行数与最老 `created_at` 的年龄，并对账「订单 = 支付流水 = 库存扣减」三方金额。

### 4.4 端到端实战：下单 → 扣库存 → 发券

前面各讲一个零件，这里把它们装成一台能跑的机器。和 4.1–4.3 的区别只有一处：**券服务会失败，而库存已经扣了**——「库存扣了券没发」的真实形态就在这里。代码用内存 broker + map 存储，`go run .` 直接可跑；换 Kafka/MySQL 只替换 `Broker` 实现与那几个 map。

`broker.go`：一条消息给每个订阅者各投一份（等价两个消费组），失败重投、三次进死信：

```go
package main

import (
	"context"
	"sync"
)

type Event struct {
	ID, Type, OrderNo, SKU string // ID 必须是业务唯一号：order-created-A1001
	Qty                    int64
	Attempt                int // 重投次数，用来判死信
}

type Broker interface {
	Publish(ctx context.Context, topic string, e Event) error
	Subscribe(topic string, buf int) chan Event
}

var deadLetter = make(chan Event, 64) // 生产是 order-events.DLQ

type memBroker struct {
	mu   sync.Mutex
	subs map[string][]chan Event
}

func NewMemBroker() *memBroker { return &memBroker{subs: map[string][]chan Event{}} }

func (b *memBroker) Subscribe(topic string, buf int) chan Event {
	ch := make(chan Event, buf)
	b.mu.Lock()
	b.subs[topic] = append(b.subs[topic], ch)
	b.mu.Unlock()
	return ch
}

func (b *memBroker) Publish(ctx context.Context, topic string, e Event) error {
	b.mu.Lock()
	targets := append([]chan Event(nil), b.subs[topic]...)
	b.mu.Unlock()
	for _, ch := range targets {
		select {
		case ch <- e:
		case <-ctx.Done():
			return ctx.Err()
		}
	}
	return nil
}

// run 是「至少一次」的消费骨架：失败重投，第 3 次进死信，别让一条毒消息堵住分区。
func run(ctx context.Context, ch chan Event, handle func(context.Context, Event) error) {
	for {
		select {
		case <-ctx.Done():
			return
		case e := <-ch:
			if err := handle(ctx, e); err != nil {
				if e.Attempt++; e.Attempt >= 3 {
					deadLetter <- e // 落地后由 4.6 的人工补偿入口接管
					continue
				}
				ch <- e
			}
		}
	}
}
```

`app.go`（同 package）：写订单与写 outbox 在同一把锁里，等价于同一个本地事务；relay 发成功才删行：

```go
package main

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"time"
)

var (
	mu      sync.Mutex
	orders  = map[string]string{}
	outbox  = map[string]Event{}
	seen    = map[string]bool{} // consumed_event(consumer, event_id) 唯一键
	stock   = map[string]int64{"SKU-1": 100}
	coupons = map[string][]string{}
)

func CreateOrder(orderNo, sku string, qty int64) {
	mu.Lock()
	defer mu.Unlock()
	orders[orderNo] = "PENDING"
	outbox["order-created-"+orderNo] = Event{
		ID: "order-created-" + orderNo, Type: "order.created",
		OrderNo: orderNo, SKU: sku, Qty: qty,
	}
}

// relay：两个 topic 都发成功才删 outbox。券 topic 失败就整批留到下轮重发，
// 库存会收到重复消息 —— 这正是 3.6 幂等必须存在的原因。
func relay(ctx context.Context, b Broker) {
	t := time.NewTicker(100 * time.Millisecond)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			mu.Lock()
			batch := make([]Event, 0, len(outbox))
			for _, e := range outbox {
				batch = append(batch, e)
			}
			mu.Unlock()
			for _, e := range batch {
				if b.Publish(ctx, "inventory", e) != nil {
					continue
				}
				if b.Publish(ctx, "coupon", e) != nil {
					continue // 券没发出去，下一轮重试
				}
				mu.Lock()
				delete(outbox, e.ID)
				mu.Unlock()
			}
		}
	}
}

func handleInventory(_ context.Context, e Event) error {
	mu.Lock()
	defer mu.Unlock()
	if seen["inventory:"+e.ID] {
		return nil // 重复投递，直接 ACK
	}
	if stock[e.SKU] < e.Qty {
		return fmt.Errorf("insufficient stock: %s", e.SKU) // 重试无意义，进死信
	}
	stock[e.SKU] -= e.Qty
	seen["inventory:"+e.ID] = true
	return nil
}

func handleCoupon(_ context.Context, e Event) error {
	mu.Lock()
	defer mu.Unlock()
	if seen["coupon:"+e.ID] {
		return nil
	}
	if e.Attempt < 2 {
		return errors.New("coupon svc timeout") // 模拟前两次失败，触发重投
	}
	coupons[e.OrderNo] = append(coupons[e.OrderNo], "CASH-10")
	seen["coupon:"+e.ID] = true
	return nil
}

func main() {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	b := NewMemBroker()
	go run(ctx, b.Subscribe("inventory", 16), handleInventory)
	go run(ctx, b.Subscribe("coupon", 16), handleCoupon)
	go relay(ctx, b)
	CreateOrder("A1001", "SKU-1", 2)
	time.Sleep(time.Second)
	mu.Lock()
	fmt.Printf("stock=%d coupons=%v outbox=%d dlq=%d\n",
		stock["SKU-1"], coupons["A1001"], len(outbox), len(deadLetter))
	mu.Unlock()
}
```

输出应为 `stock=98 coupons=[CASH-10] outbox=0 dlq=0`。

**券服务彻底失败（不是超时，是业务拒绝）时怎么办**，这是本节的真正考点，四条缺一不可：

| 环节 | 做法 | 不做的后果 |
|---|---|---|
| **重试上限** | 示例代码是立即重投（`ch <- e`）；生产应改为 3 次、间隔 1s/4s/16s 的指数退避 + 抖动（`time.AfterFunc` 或延迟队列） | 无限重试把分区打死，库存消息排在后面全部阻塞 |
| **死信** | 超限进 `order-events.DLQ`，**同时**写 `compensation_task(biz_no, action, status)` | 只进 DLQ 没人看，等于静默丢单 |
| **人工补偿入口** | `POST /admin/compensations`，带 `biz_no / reason / operator / idempotency_key`，先落 `compensation_log` 再异步执行 | 运维直接改库，无法审计、无法重放、无法回答「谁改的」 |
| **对账兜底** | T+1 比对「订单已支付」vs「券已发」，差异落 `recon_diff`，见 4.6 | 死信被清理或补偿接口没调，问题永远暴露不了 |

顺序很重要：**库存扣减不可回滚，所以券必须只做「补发」，不做「回滚」**。消费者只认 `event_id` 幂等，任何重放都只会补发一次；对账负责发现「连死信都丢了」的极端情况。

### 4.5 故障注入：把「至少一次」打成真的

「至少一次」在 PPT 上是一句话，在代码里是一堆没被跑过的分支。上线前这四次演练必做，本地或预发都能完成：

```bash
# ① 重复投递：消费组 offset 回拨 1，最后一条必然重放
kafka-consumer-groups.sh --bootstrap-server localhost:9092 \
  --group order-inventory --topic order-events \
  --reset-offsets --shift-by -1 --execute

# ② 乱序：同一聚合拆成两个 key，落到不同分区并发消费
kafka-console-producer.sh --bootstrap-server localhost:9092 \
  --topic order-events --property parse.key=true <<'EOF'
A1001#v2	{"event_id":"order-updated-A1001-v2","status":"PAID"}
A1001#v1	{"event_id":"order-created-A1001-v1","status":"PENDING"}
EOF

# ③ 消费者中途崩溃：处理函数里 sleep 30s，第 10 秒动手
kill -9 $(pgrep -f 'order-consumer')

# ④ broker 不可用：让 relay 连续失败，观察重试与积压告警
docker pause kafka && sleep 120 && docker unpause kafka && docker logs --tail 20 kafka
```

```text
演练            注入点                  应该观察到
① 重复投递      offset 回拨             库存只扣一次；日志出现 duplicate/skip；
                                        consumed_event 行数不增
② 乱序          同聚合拆 key + 并发消费   状态机 CAS 影响 0 行；不出现「已支付」被改回
                                        「待支付」（状态机只允许单向迁移）
③ 消费中崩溃    事务提交前 kill -9        消息未提交 offset，重启后重放；业务只生效一次；
                                        outbox 的 status=1 行由 sweeper 回收（见 4.2 末尾的 `claimed_at` 超时回置）
④ broker 不可用 relay Publish 失败        outbox 行不删除、retry_count 增长、最老事件年龄告警；
                                        恢复后自动追平，无需人工介入
```

三种「看起来有去重、实际完全失效」的幂等键，几乎每个团队都写过至少一种：

| 写法 | 为什么失效 | 现场现象 |
|---|---|---|
| 用自增 `id` 作键 | 每次重放都是新行，自增必然不同 | 去重表永远为空，库存被扣两次 |
| 用 `time.Now().UnixNano()` | 重放时间不同，键必然不同 | 同上，且代码评审时看着「有幂等逻辑」 |
| 用 `uuid.New()` 每次现生成 | 随机值无法由事件内容推导 | 同一条消息的两个副本拿到两个 UUID，双写都成功 |
| **正确**：`order-created-{order_no}` | 由业务唯一号稳定推导，重放恒定 | 第二次插入唯一键冲突 → 直接 ACK |

演练要进预发发布清单，不是一次性动作：**每次改消费逻辑，重跑 ① 和 ③**。幂等分支没被执行过的代码，等于没有。

### 4.6 对账与人工补偿：最终一致的最后一公里

对账不是「跑个脚本比一下」，而是三张表 + 一套状态机。铁律：**只写差异表，不改业务库**——所有修复都走独立的补偿接口，否则对账脚本本身就是最大的脏数据来源。

```sql
CREATE TABLE biz_flow (          -- ① 业务流水：一行 = 一次真实发生的业务
  biz_no    VARCHAR(64)   NOT NULL,   -- 订单号 / 支付号，对账主键
  biz_type  VARCHAR(32)   NOT NULL,   -- order / pay / stock / coupon
  amount    DECIMAL(18,2) NOT NULL DEFAULT 0,
  status    VARCHAR(16)   NOT NULL,
  created_at DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),  -- 对账按天切分要用
  PRIMARY KEY (biz_no, biz_type),
  KEY idx_type_status_time (biz_type, status, created_at)
);
CREATE TABLE event_log (         -- ② 事件流水：进出 MQ 的每条消息都留痕
  event_id  VARCHAR(96) NOT NULL,
  topic     VARCHAR(64) NOT NULL,
  direction ENUM('out','in') NOT NULL,
  biz_no    VARCHAR(64) NOT NULL,
  PRIMARY KEY (event_id, direction, topic)
);
CREATE TABLE recon_diff (        -- ③ 差异表：对账唯一输出，结构天然支持重跑
  biz_date  DATE        NOT NULL,
  biz_no    VARCHAR(64) NOT NULL,
  diff_type VARCHAR(24) NOT NULL,   -- MISSING / DUPLICATE / AMOUNT_MISMATCH / STATUS_MISMATCH
  detail    JSON        NOT NULL,
  status    TINYINT     NOT NULL DEFAULT 0,  -- 0 待处理 / 1 自动修复 / 2 人工工单 / 3 已关闭
  PRIMARY KEY (biz_date, biz_no, diff_type)
);
```

T+1 任务必须幂等可重跑：固定 `biz_date`、`ON DUPLICATE KEY UPDATE`、**不覆盖 `status IN (2,3)`**（人工刚处理完不能被脚本改回去）：

```sql
-- 找出「订单已支付但券没发」，昨天全量，按业务号左连接比对
INSERT INTO recon_diff (biz_date, biz_no, diff_type, detail)
SELECT DATE(o.created_at), o.biz_no, 'MISSING',
       JSON_OBJECT('expect', 'coupon', 'found', 'none')
FROM biz_flow o
LEFT JOIN biz_flow c ON c.biz_no = o.biz_no AND c.biz_type = 'coupon'
WHERE o.biz_type = 'order' AND o.status = 'PAID'
  AND DATE(o.created_at) = CURDATE() - INTERVAL 1 DAY
  AND c.biz_no IS NULL
ON DUPLICATE KEY UPDATE detail = VALUES(detail);
```

| 差异类型 | 典型成因 | 处理边界 |
|---|---|---|
| `MISSING`（漏发） | relay 挂、消息被清、进死信后被清 | **可自动**：按 `event_id` 重投，同一差异自动修复 ≤3 次 |
| `DUPLICATE`（重发） | 至少一次 + 幂等键失效 | 可自动冲正，**仅当**幂等键能反查到唯一业务行；否则人工 |
| `AMOUNT_MISMATCH` | 口径、优惠、汇率、退款未同步 | **只告警不改数**，金额差异一律人工 |
| `STATUS_MISMATCH` | 状态机漏推、事件丢失 | 可重放；终态（已退款、已核销）转人工 |

三条纪律：

1. **阈值**：`diff_count / total > 0.0001`（0.01%）即电话告警，别等日报；单笔金额 > 1000 元的差异无条件置顶。
2. **补偿是接口不是 SQL**：`POST /admin/compensations`，body 带 `biz_no / action / reason / operator / idempotency_key`，先写 `compensation_log`（唯一键 = idempotency_key）再异步执行，结果回写 `recon_diff.status`。直接 `UPDATE orders SET status=...` 的「临时修数」不可审计、不可重放，就是下一次事故的种子。
3. **自动/人工的边界只按「可逆性 + 金额」划**：可逆且金额小 → 自动；不可逆或金额大 → 工单。不要让脚本去猜。

对账跑通的标准不是「差异为 0」，而是**「每一笔差异都有归属」**：自动修复、工单跟进、或明确记为可接受差异。剩下没人认领的那部分，才是真正的技术债。

---

## 5. 版本与生态

| 组件 | 版本（截至 2026-11） | 与本篇相关的要点 |
|---|---|---|
| **Seata（Java）** | **2.6.0**（2026-01）；2.7.0 在发布投票中 | AT 模式靠 `undo_log` + 全局锁自动补偿，支持 MySQL/Oracle/PostgreSQL/TiDB 等；TCC/Saga/XA 并存；Server 需独立部署 + DB/Redis 存储 |
| **Seata-Go** | **2.1.0**（2026-02） | 覆盖 AT/TCC/SAGA/XA，要求 Server 2.5.0+；生态弱于 Java 版，Go 项目主流仍是 Outbox/Saga 自研 |
| **RocketMQ** | **5.5.1**（2026-08） | 事务消息（半消息 + 回查）仍是核心能力；5.x 有 gRPC Proxy、任意精度定时消息；事务消息内部已改为队列化实现 |
| **Kafka** | **4.3.1**（2026-06） | 4.0 起 KRaft-only（移除 ZooKeeper）；KIP-890 强化事务协议；EOS 边界仍只覆盖 Kafka 读写 |
| **Debezium** | **3.7.1.Final**（2026-11） | Outbox Event Router 默认列 `id/aggregatetype/aggregateid/type/payload`，`aggregateid` 作 key，只处理 INSERT |
| **Temporal** | Server **1.3x** / Go SDK v1.x | 承担 Saga 的编排与持久化执行，补偿写在 Workflow 里；不提供 DB 层原子性 |
| **MySQL** | **9.7 LTS** | XA 语法与语义未变（`XA START/END/PREPARE/COMMIT`），新项目直接用 LTS 线 |

生态判断：Java 团队已有 Seata 时 AT 上手最快，但要接受全局锁导致的吞吐下降与 `undo_log` 膨胀；Go 团队优先 **Outbox + Kafka/RocketMQ**，长流程交给 [Temporal](../temporal/temporal-guide.md)，不要在 Go 里硬套 Seata AT；存量系统不能改代码时 CDC 是唯一现实选项；已用 RocketMQ 5.x 则事务消息是最短路径，但先把回查表和状态机设计对。

---

## 6. 常见坑与最佳实践

1. **双写不做保护**：`Commit()` 之后直接 `producer.Send()`，中间崩溃就永久丢事件；先发消息再写库，则通知一个不存在的订单。跨系统写必须收敛到 Outbox / 事务消息 / CDC。
2. **outbox 当业务表用**：塞整张订单快照、图片 base64，表迅速膨胀、投递变慢。只放事件必要字段 + ID，详情让消费方回查。
3. **忘了归档**：`status=2` 的行永不删除，半年后索引几千万行，投递扫描与主从延迟一起爆。按天分区或定时批量删除。
4. **relay 在事务里发网络请求**：`BEGIN` → `FOR UPDATE` → 调 Kafka → `COMMIT`，一次抖动就让行锁持有多秒。先认领（短事务），再在事务外发布。
5. **多副本 relay 重复投同一批**：没有 CAS 抢占也没有 `SKIP LOCKED`，两个实例会同时发同一行。认领必须是原子的。
6. **认领后崩溃，行卡在 `status=1`**：必须有不依赖业务进程的 sweeper（`claimed_at` 超时回置），否则这条事件再也不会被投递。
7. **用消息 ID 做幂等键**：重发时 ID 变化，去重完全失效。幂等键必须是**业务唯一号**，且能由事件内容稳定推导。
8. **去重表与业务写入不同事务**：先写去重表再写业务（或反过来），中间失败就出现「标记已处理但业务没做」。必须同库同事务。
9. **TCC 不做空回滚与悬挂防护**：只写三个接口不建控制表，重试一多就出现「解冻了没冻结的钱」。控制表 + 状态机 CAS 是必需品。
10. **把不可逆操作放 Saga 中间**：第 2 步发短信、第 3 步才扣款，扣款失败短信收不回。可补偿步骤前置，不可逆步骤后置 + 人工兜底。
11. **以为 Saga 有隔离性**：中间态被读到，出现「库存显示已售但订单未支付」。用语义锁 + 查询侧显式过滤，别指望数据库隔离级别。
12. **补偿失败被 `_ =` 吞掉**：正向成功、反向没做，数据永久不一致。补偿要与正向同等级的重试、幂等、告警。
13. **XA 事务里夹 RPC**：`XA START` 之后调下游，prepare 前的锁持有时间被网络放大数倍。XA 事务内只放本地 SQL。
14. **Kafka 事务当跨库事务**：开了 `transactional.id` 就以为「Kafka + MySQL」原子。它只管 Kafka 分区与 offset，DB 侧仍要 Outbox 或幂等表。
15. **先 commit offset 再处理**：进程在两步之间崩溃，消息永久丢失。先提交业务事务，再提交 offset，重复靠幂等兜住。
16. **只监控服务存活**：relay 活着但一直失败（权限、Topic 不存在、序列化错误），健康检查全绿而数据停更几小时。积压量与最老事件年龄必须告警。
17. **只信「消息一定会到」**：至少一次投递也可能因误删、Topic 重建、消费组重置而丢数据。日终对账是最后防线，不是可选项。
18. **为两三个写操作上框架**：先问「能否放进同一个库」「能否用一次同步调用 + 重试」「能否接受 5 秒最终一致」。多数场景的正确答案是**改业务边界**，而不是引入 TM/TC 集群。

---

## 7. 选型对比

| 一致性要求 | 是否跨库/跨服务 | 补偿是否可逆 | 团队运维能力 | 推荐 |
|---|---|---|---|---|
| 强一致，中间态不可见（钱、额度） | 否（能放同库） | — | — | **本地事务**，先改边界 |
| 强一致，中间态不可见 | 是，同机房、短事务、低并发 | — | 有中间件经验 | XA / Seata AT |
| 最终一致 | 是 | 可逆（预留、冻结、可退款） | 中 | **Saga**（Temporal 编排） |
| 最终一致 | 是 | 不可逆（已发货、已结算） | 中 | Saga + 不可逆步骤后置 + 人工兜底 |
| 只需「事件可靠送达」 | 是（DB → MQ） | 不涉及 | 低 | **Outbox + 轮询** |
| 只需「事件可靠送达」 | 是，在 Kafka 生态内 | 不涉及 | 中（有 Kafka Connect） | Outbox + Debezium CDC |
| 只需「事件可靠送达」 | 是，已用 RocketMQ | 不涉及 | 中 | 事务消息 + 回查表 |
| 不能改业务代码 / 存量系统 | 是 | — | 有 DBA 与平台团队 | CDC 订阅 |

| 维度 | 2PC / XA | TCC | Saga | 本地消息表 / Outbox | 事务消息 | CDC |
|---|---|---|---|---|---|---|
| 一致性 | **强** | 最终 | 最终 | 最终 | 最终 | 最终 |
| 隔离性 | **有**（持锁） | 软隔离（预留） | 无 | 无 | 无 | 无 |
| 业务侵入 | 低 | **最高** | 中 | 中 | 中 | **最低** |
| 吞吐影响 | **大**（锁 + 2 RTT） | 小 | 小 | 小 | 小 | 无（走外部链路） |
| 典型延迟 | 提交即一致 | 秒级 | 秒~分钟 | 100ms~5s | 百 ms | 100ms~1s |
| 运维复杂度 | 中（TM 单点） | 中 | 中~高（编排引擎） | **低** | 中 | 高（Connect + 日志） |
| 主要失败模式 | in-doubt 悬挂锁 | 空回滚 / 悬挂 | 补偿不可逆 | 重复投递 | 回查不准 | 位点丢失重放 |
| 适合团队 | 有 DBA 与中间件 | 金融核心 | 业务复杂度高 | **大多数团队** | RocketMQ 用户 | 平台/数据团队 |

一句话决策：

- **能不用就不用。** 先把两个写塞进同一个库、同一个本地事务；做不到就把其中一个改成异步（先落本地，再补偿）。
- **必须强一致且跨库** → XA / Seata AT，接受吞吐下降与悬挂事务的运维成本，只放在同机房短事务上。
- **跨服务、能补偿、流程会变长** → Saga，用 [Temporal](../temporal/temporal-guide.md) 编排，别自己写状态机 + 定时扫描。
- **只是「写完库要通知别人」** → Outbox；新系统首选轮询（简单可控），已有 Kafka Connect 就上 Debezium。
- **钱和券要预留、不能直接扣** → TCC，但先把控制表与三个幂等状态机写对。
- **存量系统、不许改代码** → CDC，用平台复杂度换零侵入。

> 最后一条经验：分布式事务的复杂度不在「选哪个框架」，而在「补偿、幂等、对账」这三件没人愿意写的脏活。任何方案缺了这三样都会在半年后还债，而 2PC 只是把债换成了锁等待与悬挂事务。

参考：<https://microservices.io/patterns/data/saga.html> · <https://microservices.io/patterns/data/transactional-outbox.html> · <https://seata.apache.org> · <https://debezium.io/documentation/reference/stable/transformations/outbox-event-router.html>
