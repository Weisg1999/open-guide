# DTM 技术指南

> DTM 把 Saga、TCC、XA、二阶段消息与工作流做成一套 HTTP/gRPC 协议：业务侧只需暴露补偿与确认接口，剩下的事务状态机由它推进。
> 上游：<https://github.com/dtm-labs/dtm> **v1.18.x**　|　协议：HTTP / gRPC　|　许可：BSD-3-Clause

---

## 1. 项目简介

[分布式事务篇](../distributed-transaction/distributed-transaction-guide.md) 回答的是「要不要做分布式事务、用哪类模式」；本篇只回答一件事：**选定模式之后，用 DTM 怎么接、接口长什么样、哪些活它替你干了、哪些坑它替不了你**。理论推导、2PC 的阻塞分析、Outbox 的 relay 细节都在那一篇，这里不重复。

DTM 是一个 Go 写的**独立事务管理器进程**（`dtmsvr`）+ 一组多语言 SDK。它的工作方式很朴素：业务服务把自己的接口地址注册给 DTM，DTM 按事务模式依次 POST 这些地址，把每一步的结果记进自己的库，失败就回调补偿接口。**没有 sidecar、没有 SQL 代理、不需要改数据源**——这是它与 Seata AT 最本质的区别（AT 要在数据源上挂代理、解析 SQL、维护 `undo_log` 和全局锁）。

```text
        ①提交全局事务（gid + 分支列表）        ②按序回调分支（HTTP/gRPC）
业务 A ─────────────────────────► dtmsvr ─────────────────────────► 业务 A /TransOut
   ▲                                 │  │
   │  ④返回最终结果                   │  └──────────────────────────► 业务 B /TransIn
   └─────────────────────────────────┘
                   ③落盘 gid、分支、状态（MySQL / PostgreSQL / Redis）
```

三条由此推出的性质：

- **DTM 是无状态进程 + 有状态存储**：进程可以多副本、可以随便重启，事务状态在 `dtm` 库里，重启后靠定时扫描继续推进未完成的事务。所谓「单点」通常指它的**存储**，不是进程。
- **业务侧的最小契约是「能被回调的接口」**：一个 HTTP handler 或 gRPC method，加上一个本地库。跨语言栈（Java/PHP/Python/Node）只要按协议实现这几个接口，不需要官方 SDK。
- **DTM 只保证「推进到终态」，不保证「业务语义正确」**：它不知道你的补偿写得对不对，也不知道退款接口是否幂等。这半边永远在业务代码里。

什么时候**不该**上 DTM：两个写能塞进同一个库（本地事务就够）、只是「写完库要通知别人」且团队已有成熟 Outbox（多引入一个组件不划算）、流程要等人工审批或跨天定时（那是 [Temporal](../temporal/temporal-guide.md) 的地盘）。DTM 的甜区是**秒级、步骤固定、每步都有明确反向操作**的跨服务写。

---

## 2. 能力总览

| 模式 | 一致性 | 业务侧要写什么 | DTM 替你做的 | 典型落点 |
|---|---|---|---|---|
| **Saga** | 最终（补偿完成后） | 正向接口 + 补偿接口 | 顺序推进、失败逆序补偿、超时重试 | 跨服务多步骤、资源不可预留 |
| **TCC** | 最终（Confirm 后） | Try / Confirm / Cancel 三个接口 | 两阶段调度、空回滚/悬挂/幂等兜底 | 余额、库存、优惠券 |
| **XA** | 强一致（提交即一致） | 一个本地事务回调 | prepare/commit/rollback 与崩溃恢复 | 同机房同构 MySQL、短事务 |
| **二阶段消息** | 最终 | 本地事务 + 回查接口 | 消息持久化、投递、回查、重试 | 写完库要可靠通知下游 |
| **Workflow** | 最终 | 一个编排函数 + 补偿回调 | 断点续跑、混合 HTTP/gRPC/本地事务 | 步骤会变、协议混合 |
| **Outbox 对接** | 最终 | 只写业务表与 outbox 表 | 轮询并投递 outbox 行 | 已有 outbox 想交给外部推进 |

三条硬约束，选型前先接受：

1. **所有分支接口必须幂等**。DTM 的重试不是异常路径，是常规路径（见 3.2）。
2. **补偿必须可重入且不依赖当前状态**。「查一下订单是不是已支付，是就不退」这种补偿在重放时会永久失败。
3. **中间态一定会被看到**。DTM 不提供隔离性，`CREATED` 与 `PAID` 之间的窗口必须由业务显式表达。

---

## 3. 核心概念与原理

### 3.1 架构与存储：一个独立服务，两种协议

`dtmsvr` 对外只做四类事：`newGid`（取全局事务号）、`submit`（提交全局事务）、`prepare/abort`（二阶段消息与 TCC 的准备/回滚）、`registerBranch`（分支注册与状态回报）。业务侧调 SDK，SDK 把这些动作翻译成 HTTP 或 gRPC。

存储底座决定运维形态：

| 底座 | 配置 | 特点 | 适用 |
|---|---|---|---|
| **MySQL / MariaDB** | `Store.Driver: mysql` | 事务表落盘，崩溃可恢复，功能最全 | **生产默认** |
| **PostgreSQL** | `Store.Driver: postgres` | 同上，PG 栈团队用 | 生产可用 |
| **Redis** | `Store.Driver: redis` | 吞吐高、延迟低；持久性取决于 AOF/RDB，宕机可能丢状态 | 秒杀、可接受极端情况重放 |
| **BoltDB** | 本地文件 | 零依赖 | 只用于开发与单测 |
| **MongoDB** | — | 只能作为**业务库**（`MongoCall` 屏障可用）；作为 DTM 状态存储截至 2026-11 仍是规划项 | 不建议 |

Redis 底座要额外注意：状态丢了不会「自动对账」，只会让在途事务永远停在 `prepared`。所以用 Redis 的前提是业务侧有对账兜底，或者这批事务本身可重放。

### 3.2 gid 与幂等：重试是常态，不是意外

`gid`（全局事务 ID）由调用方生成——`shortuuid.New()` 或 `dtmcli.MustGenGid(server)`，长度上限 128。它有两个语义，很多人只记住了第一个：

- **唯一标识一笔全局事务**：同一笔业务用同一个 gid。
- **幂等键**：同一个 gid 重复 `Submit`，DTM 视为同一笔事务的重试，**不会开第二笔**；反过来，业务失败后换个 gid 重试，就是两笔独立事务，可能扣两次钱。

重试来自三处，无法用「小心一点」消除：DTM 侧的分支超时按退避重发、进程重启后扫描未完成事务继续推进；网络侧客户端等不到 `submit` 响应用同一 gid 再提交；业务侧处理完了但响应丢了，DTM 认为失败、稍后再调一次同一个接口。

所以「接口幂等」不是优化项，是接入 DTM 的**前置条件**。三种落地方式：

| 方式 | 写法 | 适用 |
|---|---|---|
| **唯一键** | `INSERT ... ON DUPLICATE KEY UPDATE` / 唯一索引 + 捕获 1062 | 有天然业务唯一号（订单号、流水号） |
| **状态机 CAS** | `UPDATE ... WHERE status = 'PENDING'`，影响 0 行即已处理 | 状态流转明确的实体 |
| **子事务屏障** | `bb.Call(tx, busiCall)`，框架用一张表挡住重复 | TCC/Saga/消息，**推荐** |

### 3.3 Saga：正向顺序、补偿逆序，以及「补偿失败」这个真问题

`T1 → T2 → T3`，T2 失败则执行 `C1`（只补偿已成功的步骤，且逆序）。DTM 记录每个分支的状态，所以它能精确知道「哪些正向成功了」，不会去补偿没执行过的分支。并发 Saga（`SetConcurrent`）的补偿顺序是**完成顺序的逆序**，不是声明顺序。

补偿失败是 Saga 最容易被低估的部分。DTM 的行为是**持续重试**：全局事务停在 `aborting`，按退避间隔反复调补偿接口，直到成功。这带来两个必然后果：

- 补偿接口**不能有前置校验**。`if order.Status != "PAID" { return nil }` 看似安全，实际会在重放时把「已退款」当成「无需退款」跳过，或者因为状态已被别的流程改掉而永远抛错。
- 补偿失败**必须告警到人**。DTM 会一直重试，但不会告诉你它重试了 300 次；监控要盯 `aborting` 状态的事务数与最老事务年龄，超阈值转人工对账。

Saga 还有个隐藏问题：正向接口也可能「超时但实际成功」，此时 DTM 发起补偿，而正向请求在网络里滞留后到达——这就是 TCC 里的**空补偿**与**悬挂**，Saga 一样存在。所以 Saga 的每个分支（正向和补偿）也应该套 `BranchBarrier`，而不是只靠手写唯一键。

### 3.4 TCC 与子事务屏障：一张表同时解决三个问题

TCC 把「检查」和「使用」拆开：Try 只**预留**（冻结 100 元），Confirm 才真正扣，Cancel 解冻。好处是不长期持锁；代价是三个接口 + 三个经典故障。

DTM 的解法叫**子事务屏障**（sub-transaction barrier）：在业务库建一张 `barrier` 表，唯一键是 `(gid, branch_id, op, barrier_id)`，所有分支先插记录再执行业务，插入是否成功决定要不要执行。

```sql
CREATE DATABASE IF NOT EXISTS dtm_barrier;
CREATE TABLE IF NOT EXISTS dtm_barrier.barrier (
  id          BIGINT PRIMARY KEY AUTO_INCREMENT,
  trans_type  VARCHAR(45) DEFAULT '',
  gid         VARCHAR(128) DEFAULT '',
  branch_id   VARCHAR(128) DEFAULT '',
  op          VARCHAR(45) DEFAULT '',   -- try / confirm / cancel / action / compensate
  barrier_id  VARCHAR(45) DEFAULT '',
  create_time DATETIME DEFAULT NOW(),
  UNIQUE KEY uk_barrier (gid, branch_id, op, barrier_id)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4;
```

`bb.Call(tx, busiCall)` 的语义（`CallWithDB` 只是帮你开了事务）：

| 到达的分支 | 屏障先做什么 | 什么时候执行 `busiCall` |
|---|---|---|
| **Try** | `INSERT IGNORE (gid, branch, 'try')` | 仅当本次插入成功（首次） |
| **Confirm** | `INSERT IGNORE (gid, branch, 'confirm')` | 仅当本次插入成功（首次） |
| **Cancel** | 先 `INSERT IGNORE ... 'try'`，再 `INSERT IGNORE ... 'cancel'` | 仅当 **try 已存在**（说明 Try 真执行过）且 cancel 首次插入成功 |

三个问题因此同时消失：

- **空回滚**：Cancel 先到，它插 try 时成功了 → 说明 Try 从未执行 → 不调用 `busiCall`，直接返回成功。业务侧不会出现「解冻了没冻结的钱」。
- **悬挂**：Cancel 已经把 `try` 记录占住，迟到的 Try 再插 try 会失败 → 不执行正向逻辑。预留不会在回滚之后发生。
- **幂等**：唯一键让同一分支的同一 op 只会执行一次，重放直接返回成功。

关键前提：**屏障记录必须与业务写在同一个本地事务里**。`CallWithDB` 会自己开事务；用 GORM 时必须把 `tx.Statement.ConnPool.(*sql.Tx)` 传进 `Call`，不能另开一个连接——否则「屏障写了、业务没写」或反之，屏障就成了假的。

### 3.5 二阶段消息：本地事务 + 消息表，但 relay 由 DTM 当

普通 Outbox 要自己写 relay（轮询或 CDC）把 outbox 行搬到 MQ；DTM 的二阶段消息把这层接过去了：

```text
1. Prepare：告诉 DTM「我要发这条消息」→ DTM 落一条 prepared 记录（业务不可见）
2. 本地事务：业务写入 + 屏障记录，同一个 tx 提交
3. Submit：告诉 DTM 可以发了 → DTM 回调 action 接口（可多条、可延迟）
4. 中间崩溃：DTM 回查 queryPrepared → 你回答「本地事务提交了吗」→ 决定投递或丢弃
```

第 4 步的回查接口**必须读业务库的真实状态**（通常是 `bb.QueryPrepared(db)`，即查屏障记录），不能靠内存变量。它和 RocketMQ 事务消息的回查是同一个难点，只是回查方向相反（DTM 问你，而不是你问 Broker）。

与自研 Outbox 的取舍：

| 维度 | 自研 Outbox | DTM 二阶段消息 |
|---|---|---|
| 谁投递 | 自己的 relay / Debezium | DTM 服务 |
| 额外组件 | MQ + 调度 | DTM + 存储（MQ 可选，可直接 HTTP 回调） |
| 投递语义 | at-least-once | at-least-once（消费端仍要幂等） |
| 多库支持 | 每库一套 relay | 一个 DTM 管多库 |
| 延迟 | 轮询 100ms~5s | 通常百 ms 级 |

省掉的是 relay 的运维（积压、认领、归档），付出的是多一次 RPC 和一个必须高可用的 DTM。

### 3.6 XA 的边界，与「工作流」到底解决什么

**XA** 在 DTM 里是唯一能给出强一致的选项：业务侧只写一个本地事务回调，DTM 当 TM 发 `prepare`/`commit`。但边界很硬：目前主要对 MySQL 协议系可用；从 `prepare` 到 `commit` 期间连接被 xid 绑定、行锁不释放，等于把单机锁持有时间放大成跨服务 RTT；TM 崩溃会留下 in-doubt 事务，只能靠 `XA RECOVER` 人工裁决。**只在同机房、短事务、低并发、且两个库都是 MySQL 时用**，不要放在主交易链路。

**工作流**解决的是 Saga 表达力不够的问题：一个函数里混用 HTTP 调用、gRPC 调用和本地事务，分支的补偿写在 `OnRollback` 里；进程崩溃后 DTM 会重试整个函数，SDK 用拦截器把已完成分支的结果直接回放，所以函数必须幂等。

```go
workflow.Register("pay-order", func(wf *workflow.Workflow, data []byte) error {
	wf.NewBranch().OnRollback(func(bb *dtmcli.BranchBarrier) error { return unfreeze(wf.Context) })
	if err := freeze(wf.Context); err != nil {
		return err
	}
	// 本地库操作也能作为一个分支，和远程分支混在同一个全局事务里
	return wf.Do(func(bb *dtmcli.BranchBarrier) ([]byte, error) {
		return nil, bb.CallWithDB(db, func(tx *sql.Tx) error { return insertOrder(tx) })
	})
})
```

如果只是「几步之间要并行、要依赖顺序」，不必上 workflow：Saga 的 `AddBranchOrder` + `SetConcurrent` 就是一张轻量 DAG（见 4.4）。超时有两层：DTM 服务端的 `TimeoutToFail`（默认 35s，超时后判定失败并回滚）和业务自己的 `context.WithTimeout`；后者必须**小于**前者，否则 DTM 已经放弃、你的请求还在跑。

> 版本备注：社区文章里常见的 `trans.New(...)`、`dtm.NewFromDB(...)` 这类一体化编排写法，截至 2026-11 未在公开 SDK 的稳定文档中出现；对外的编排入口是 `github.com/dtm-labs/client/workflow`，事务模型类型留在服务端内部。生产代码请以 `dtmcli` / `dtmgrpc` / `workflow` 三个包的签名为准。

---

## 4. 使用实战

### 4.1 起一个 DTM 服务

```bash
git clone https://github.com/dtm-labs/dtm && cd dtm
mysql -uroot -p -e "CREATE DATABASE IF NOT EXISTS dtm"   # 再导入 sqls/ 下的建表脚本
go run main.go -c conf.yml     # HTTP 36789 / gRPC 36790
# 验证：curl -s "http://127.0.0.1:36789/api/dtmsvr/query?gid=$(uuidgen)"
```

```yaml
# conf.yml：只需 Store 一节，Driver 可选 mysql / postgres / redis
Store: {Driver: mysql, Host: 127.0.0.1, Port: 3306, User: root, Password: '123456', Db: dtm}
```

业务库另建 `dtm_barrier`（3.4 的 DDL），并给每个参与方服务配好 `dtmcli.SetCurrentDBType("mysql")`。

### 4.2 Saga 转账：扣款 + 入账 + 补偿

```go
package main

import (
	"database/sql"
	"net/http"

	"github.com/dtm-labs/client/dtmcli"
	"github.com/gin-gonic/gin"
	"github.com/lithammer/shortuuid/v3"
)

const DtmServer, Busi = "http://127.0.0.1:36789/api/dtmsvr", "http://127.0.0.1:8081"

type TransferReq struct {
	From   int64 `json:"from"`
	To     int64 `json:"to"`
	Amount int64 `json:"amount"`
}

// Transfer 发起方：gid 只生成一次，重试必须复用；换 gid 等于又转了一笔
func Transfer(c *gin.Context) {
	var req TransferReq
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}
	saga := dtmcli.NewSaga(DtmServer, shortuuid.New()).
		Add(Busi+"/TransOut", Busi+"/TransOutCompensate", req).
		Add(Busi+"/TransIn", Busi+"/TransInCompensate", req)
	if err := saga.Submit(); err != nil {
		c.JSON(http.StatusConflict, gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusOK, gin.H{"result": "SUCCESS"})
}

// adjust 正向与补偿共用一个实现：sign=-1 扣款，sign=+1 退款。
// 返回码不同：正向 409 让 DTM 立刻回滚，补偿 5xx 让 DTM 持续重试。
func adjust(c *gin.Context, db *sql.DB, sign int64) {
	bb, err := dtmcli.BarrierFromQuery(c.Request.URL.Query())
	if err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}
	var req TransferReq
	_ = c.ShouldBindJSON(&req)

	code := http.StatusConflict
	if sign > 0 {
		code = http.StatusInternalServerError
	}
	err = bb.CallWithDB(db, func(tx *sql.Tx) error {
		delta := sign * req.Amount
		res, err := tx.Exec(
			`UPDATE account SET balance = balance + ? WHERE uid = ? AND balance + ? >= 0`,
			delta, req.From, delta)
		if err != nil {
			return err
		}
		if n, _ := res.RowsAffected(); n == 0 {
			return dtmcli.ErrFailure // 余额不足
		}
		return nil
	})
	if err != nil {
		c.JSON(code, gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusOK, gin.H{"result": dtmcli.ResultSuccess})
}

// 路由：/TransOut → adjust(c, db, -1)，/TransOutCompensate → adjust(c, db, +1)；
// TransIn 一侧同构，只换账户。补偿里不要加任何状态校验：重放时它会「假装成功」。
```

`adjust` 把「扣款」和「退款」压成一条带符号的 SQL，这与 DTM 官方示例的 `SagaAdjustBalance` 同构。真正要小心的是返回码：正向失败回 **409**、补偿失败回 **5xx**，写反了事务就停在不该停的地方。

### 4.3 TCC 库存：一个 handler 覆盖三个阶段

```go
type StockReq struct {
	SKU string `json:"sku"`
	Qty int64  `json:"qty"`
}

// StockStage 同时注册到 /StockTry /StockConfirm /StockCancel，用回调里的 op 区分阶段
func StockStage(c *gin.Context, db *sql.DB) {
	bb, err := dtmcli.BarrierFromQuery(c.Request.URL.Query())
	if err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}
	var req StockReq
	_ = c.ShouldBindJSON(&req)

	err = bb.CallWithDB(db, func(tx *sql.Tx) error {
		var res sql.Result
		var execErr error
		switch bb.Op {
		case dtmcli.BranchTry: // 只冻结，可用量不动
			res, execErr = tx.Exec(`UPDATE stock SET frozen = frozen + ?
				WHERE sku = ? AND available - frozen >= ?`, req.Qty, req.SKU, req.Qty)
		case dtmcli.BranchConfirm: // 冻结转实扣
			_, execErr = tx.Exec(`UPDATE stock SET available = available - ?, frozen = frozen - ?
				WHERE sku = ? AND frozen >= ?`, req.Qty, req.Qty, req.SKU, req.Qty)
		case dtmcli.BranchCancel: // 解冻
			_, execErr = tx.Exec(`UPDATE stock SET frozen = frozen - ?
				WHERE sku = ? AND frozen >= ?`, req.Qty, req.SKU, req.Qty)
		}
		if execErr != nil {
			return execErr
		}
		if res != nil {
			if n, _ := res.RowsAffected(); n == 0 {
				return dtmcli.ErrFailure // 库存不足，全局回滚
			}
		}
		return nil
	})
	if err != nil {
		c.JSON(http.StatusConflict, gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusOK, gin.H{"result": dtmcli.ResultSuccess})
}
```

发起侧只有一句 `dtmcli.TccGlobalTransaction(DtmServer, gid, func(tcc *dtmcli.Tcc) (*resty.Response, error) { return tcc.CallBranch(req, Busi+"/StockTry", Busi+"/StockConfirm", Busi+"/StockCancel") })`：返回 `nil` 表示三个分支的 Confirm 全部完成，非 `nil` 表示已回滚或正在回滚。

Try 里的 `available - frozen >= qty` 是资源预留的核心：可用量在 Try 阶段不变，只是被冻结，所以并发的其他请求看到的是真实的剩余可售量。

### 4.4 二阶段消息下单 + Saga 的并行 DAG

```go
// CreateOrder 本地事务写订单，成功后 DTM 才会去调 DeductStock / SendCoupon
func CreateOrder(c *gin.Context, db *sql.DB) {
	var req OrderReq
	_ = c.ShouldBindJSON(&req)
	msg := dtmcli.NewMsg(DtmServer, shortuuid.New()).
		Add(Busi+"/DeductStock", req).
		Add(Busi+"/SendCoupon", req)
	err := msg.DoAndSubmitDB(Busi+"/QueryPrepared", db, func(tx *sql.Tx) error {
		_, err := tx.Exec(`INSERT INTO orders (order_no, uid, sku, qty, status)
			VALUES (?, ?, ?, ?, 'CREATED')`, req.OrderNo, req.UID, req.SKU, req.Qty)
		return err // 唯一键冲突即重复下单，返回错误让整条消息作废
	})
	if err != nil {
		c.JSON(http.StatusConflict, gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusOK, gin.H{"result": "SUCCESS"})
}
```

回查接口单独实现，只读屏障记录：`bb.QueryPrepared(db)` 返回 `nil` 回答 `ResultSuccess`（已提交，DTM 继续投递），返回错误则回答 `ResultFailure`（未提交，丢弃消息）。别读内存标志位或缓存——进程重启后它们都不存在。

步骤有依赖关系时，用 Saga 的 DAG 而不是自己写 goroutine：

```go
saga := dtmcli.NewSaga(DtmServer, gid).
	Add(Busi+"/FreezeStock", Busi+"/UnfreezeStock", req). // 0
	Add(Busi+"/LockCoupon", Busi+"/UnlockCoupon", req).   // 1
	Add(Busi+"/CreateOrder", Busi+"/CancelOrder", req).   // 2
	AddBranchOrder(2, []int{0, 1}). // 2 必须等 0、1 都成功
	SetConcurrent()                 // 0、1 并行
```

---

## 5. 版本演进

| 版本 | 时间 | 与落地相关的点 |
|---|---|---|
| v1.15 | 2022 | 客户端代码迁到独立仓库 `dtm-labs/client`，import 路径变为 `.../client/dtmcli`；老 `dtmcli` 包逐步弃用，混用会报 `undefined: dtmdriver.GetHTTPDriver` |
| v1.16 | 2023 | 消息支持按 Topic 投递（`Msg.AddTopic`）、可配超时的 HTTP 客户端；Postgres 底座成熟 |
| v1.17 | 2023 | `workflow.ExecuteCtx` 取代 `Execute`（后者废弃）；driver 化，接入 go-zero / kratos 注册中心 |
| **v1.18** | 2023-11 | 存储新增 SQL Server；**本篇基线**，社区绝大多数生产部署停在这条线 |
| v1.19 | 2025-02 | 以构建与依赖升级为主，协议与 API 无破坏性变更；**截至 2026-11 仍是最新 release** |

现状判断：DTM 已进入维护节奏（提交以依赖升级和 issue 修复为主），不是高速迭代期。这对使用方是好事——协议稳定、行为可预期；但要接受「新特性少、遇到冷门问题要自己读源码」。依赖锁 `github.com/dtm-labs/client v1.18.x`，不要用 `@latest` 混搭服务端与客户端。

---

## 6. 常见坑与最佳实践

1. **补偿接口不幂等，重复退款**：补偿里写 `balance = balance + amount` 而没有任何去重，网络一抖就多退一次。补偿也要走 `bb.Call`，或至少带业务唯一号。
2. **gid 重复使用**：把订单号当 gid 复用给「重试下单」，DTM 认为还是那笔老事务，直接返回上次结果，新订单永远不生效。一笔业务一个新 gid，重试复用同一个。
3. **Cancel 在 Try 之前到达（悬挂）**：只写三个接口不建屏障表，超时重发时 Try 会在 Cancel 之后执行，冻结了一笔已经回滚的库存。`BranchBarrier` 是必需品，不是可选项。
4. **空回滚没处理**：Try 请求超时但从未落库，DTM 发起 Cancel，代码 `UPDATE ... SET frozen = frozen - 100` 把冻结量扣成负数。屏障的「Cancel 先插 try」就是专门挡这个的。
5. **屏障和业务不在同一个事务**：用 GORM 时另开了连接，barrier 提交、业务回滚，重放时被当成「已处理」直接跳过——数据永久丢失且无人报错。
6. **把非事务操作塞进 Saga**：发短信、调第三方支付划款、推 IM 消息放在中间步骤，失败后无法补偿。可补偿步骤前置，不可逆步骤后置并接受人工兜底。
7. **补偿里做业务校验**：`if status != PAID { return nil }` 在重放时等于「假装补偿成功」，账永远对不上。补偿只做反向写。
8. **超时时间小于下游 P99**：`TimeoutToFail` 设 3s 而下单接口 P99 是 5s，DTM 每笔都判超时、每笔都补偿，系统看起来在「疯狂回滚」。超时至少取下游 P99 的 3 倍。
9. **DTM 服务当单点部署**：进程挂了在途事务停住（不是丢），恢复后继续；但如果存储也挂了且用 Redis 无持久化，状态就真丢了。至少两副本 + MySQL 主从。
10. **Redis 底座当强一致用**：Redis 丢了 `prepared` 记录，业务侧已经扣的钱没有任何人负责回滚。要么有对账，要么别用 Redis 存关键事务。
11. **业务返回码乱写**：失败回 200、补偿失败回 409，DTM 的判断全反。约定：正向失败 409（`ErrFailure`），临时故障 5xx（触发重试），成功 200。
12. **`aborting` 堆积没人看**：补偿一直失败，事务永远停在回滚中，监控只看进程存活是发现不了的。必须告警「`aborting` 数量」与「最老事务年龄」。
13. **以为 DTM 提供隔离性**：Saga 分支提交后、全局未完成前，别人能读到中间态。用语义锁（`CREATED`/`FROZEN` 等中间状态）+ 查询侧显式过滤。

---

## 7. 选型对比

| 维度 | **DTM** | **Temporal** | **Seata（AT/TCC）** | **RocketMQ 事务消息** | **自研 Outbox** |
|---|---|---|---|---|---|
| 定位 | 轻量事务协调器 | 持久化执行平台 | Java 系事务中间件 | 消息侧的事务投递 | 模式，不是产品 |
| 一致性 | 最终（XA 除外） | 最终 | AT 准强一致 / TCC 最终 | 最终 | 最终 |
| 业务侵入 | 中（暴露回调接口） | 高（Workflow/Activity 改造） | AT 低 / TCC 高 | 中（回查接口） | 中（outbox 表 + relay） |
| 编排能力 | 中（Saga DAG + workflow） | **强**（代码即流程、子流程、信号） | 弱（按分支顺序） | 无 | 无 |
| 长流程 / 定时器 | 弱（分钟级以上不适合） | **强**（天级、定时、人工任务） | 无 | 无 | 无 |
| 语言生态 | Go 原生，HTTP/gRPC 跨语言 | Go/Java/TS/Python/PHP | Java 为主，Go SDK 较弱 | Java/Go/C++ | 任意 |
| 运维成本 | 低（一个服务 + 一个库） | 高（Server + Cassandra/PG + 可见性库） | 中高（Server + DB + 注册中心） | 低（已有 MQ 则几乎为零） | 低，但要自己写 relay + 归档 |
| 适合 | 跨服务写一致性、秒级收敛 | 长流程、复杂编排、需要人工介入 | Java 存量系统、AT 快速接入 | 只差「写完库发消息」 | 已有 MQ、团队想完全自控 |

按场景给结论：

- **跨服务写、步骤固定、能补偿** → DTM 的 Saga，配 `BranchBarrier`，秒级收敛，运维最轻。
- **钱/券/库存要预留** → DTM 的 TCC；**同机房两个 MySQL、短事务、必须强一致** → DTM 的 XA，但要先确认能接受连接占用与 in-doubt 运维。
- **写完库只要通知下游** → DTM 的二阶段消息，等价于「托管版 Outbox」，省掉 relay；**Java 团队已有 Seata、要 AT 的低侵入** → Seata，接受全局锁与 `undo_log`。
- **流程跨天、要等人工、要定时器** → Temporal，别用 DTM 硬撑（它的重试与状态机不是为长流程设计的）。

最后说清两篇的分工：**[分布式事务篇](../distributed-transaction/distributed-transaction-guide.md) 负责「判断」**——该不该做、选哪类模式、补偿与幂等的通用原理、失败边界在哪；**本篇负责「落地」**——选定 DTM 之后，接口怎么签名、屏障表怎么建、gid 怎么管、坑在哪。先读那篇决定要不要引入协调者，再读这篇决定怎么把它接进代码。

> DTM 的价值不是「让分布式事务变简单」，而是把「状态机 + 重试 + 幂等表」这三件重复劳动收进框架；补偿的业务语义、监控告警、对账兜底，仍然一件都少不了。
