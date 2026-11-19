# etcd 技术介绍与使用指南

> 分布式强一致 KV 存储，Kubernetes 控制平面的心脏。Raft 共识 + MVCC 多版本 + bbolt B+ 树。
> 仓库：<https://github.com/etcd-io/etcd>　|　当前主线：v3.7（2026.10 发布；v3.6 为 2025.5）

---

## 1. 项目简介

etcd 名字来源于 "/etc 分布式"——一个分布式的配置目录。它是**分布式、强一致的键值存储**，专为分布式系统中最关键的数据设计：集群元数据、服务发现、配置分发、 leader 选举、分布式锁。

典型用户：

- **Kubernetes**：所有对象（Pod/Service/ConfigMap…）都存在 etcd 里，API Server 是 etcd 的唯一客户端；
- **CoreDNS / Rook / Flannel** 等几乎所有 CNCF 项目的协调层；
- **Patroni**（PostgreSQL HA）、各类"配置中心 + 选主"场景。

能力边界：etcd 是**小数据量、低写入吞吐**的元数据存储（默认配额 2GiB，建议万级 QPS 以内），不是 Redis、不是消息队列。核心承诺是**强一致（线性一致读）与高可用（少数派故障可服务）**。

### 部署形态

```bash
# 单机（开发/测试）
etcd --listen-client-urls http://0.0.0.0:2379 --advertise-client-urls http://127.0.0.1:2379

# 生产：3 或 5 节点静态集群
# 节点1
etcd --name node1 --data-dir /var/lib/etcd \
  --listen-peer-urls http://0.0.0.0:2380 \
  --initial-advertise-peer-urls http://10.0.0.1:2380 \
  --listen-client-urls http://0.0.0.0:2379 \
  --advertise-client-urls http://10.0.0.1:2379 \
  --initial-cluster node1=http://10.0.0.1:2380,node2=http://10.0.0.2:2380,node3=http://10.0.0.3:2380 \
  --initial-cluster-state new
```

端口约定：**2379 客户端**、**2380 节点间（peer）**。

---

## 2. 功能特性总览

| 能力 | 说明 |
|---|---|
| KV 存储 | 扁平键空间（key 是字节串，通常 `/` 分层），Put/Get/Delete/Range 前缀查询 |
| 事务 Txn | 原子的 `If-Then-Else`：按版本/值/创建 revision 做条件 CAS，是分布式锁与乐观并发的基础 |
| 多版本 MVCC | 每次写产生全局递增 revision，可读历史版本、按 revision 回放 |
| Watch | 客户端订阅 key/前缀的变更事件流，从任意历史 revision 起订阅，事件保证有序不丢 |
| Lease 租约 | TTL 机制；租约可绑定多个 key，到期自动删除——服务注册/心跳的基石 |
| 线性一致读 | 默认读也走 Raft 共识（可退化为 serializable 读换性能） |
| 集群自愈 | Raft leader 选举 + 日志复制，容忍 (N-1)/2 节点故障 |
| 认证与权限 | root/普通用户、role 绑定 key 前缀的读写权限；TLS 双向认证 |
| 压缩与碎片整理 | compaction 裁剪历史 revision、defrag 回收 bbolt 空间 |
| 快照与恢复 | `snapshot save` / `restore`，灾备标准手段 |

---

## 3. 核心概念与原理

### 3.1 三层架构

```text
客户端 (etcdctl / clientv3)
    │ gRPC
    ▼
┌─────────────────────────────┐
│ etcdServer                   │
│  ┌───────────┐  ┌─────────┐ │
│  │ Raft 层    │  │ MVCC 层  │ │
│  │ 选举/复制   │→ │ 版本/Watch│ │
│  └───────────┘  └─────────┘ │
│        └──► bbolt (B+树单文件) │
└─────────────────────────────┘
```

- **Raft 层**：所有写请求（含集群成员变更）封装为 Raft 日志，由 leader 复制到多数派后 commit 才应用。leader 选举默认超时 1s、心跳 100ms。
- **MVCC 层**：每个 key 的每次修改都产生新版本。全局单调递增的 **revision = {main, sub}**，例如 `put /a` 后 revision 从 5 变 6。keyIndex 维护每个 key 的版本链。
- **存储层（bbolt）**：单文件 B+ 树（`member/snap/db`），key 按 revision 编码存储，外加一个 bucket 存 key→revisions 索引。

### 3.2 一次写入的旅程

1. 客户端 `Put /config/db-host "mysql-1"` → 任意节点；
2. 若非 leader，转发（v3.6 起 peer 端口不再服务客户端请求）；
3. leader 将写操作追加为 Raft 日志条目，并行发给 followers；
4. **多数派确认**后该条目 commit，leader 应用到状态机（MVCC 分配 revision，写入 bbolt）；
5. 应答客户端，Watch 事件异步推送。

这解释了 etcd 的写入成本：**一次 Put = 一次多数派共识 + 一次磁盘 fsync**（Raft 日志与 bbolt 落盘）。因此写吞吐有限（数千~万级/秒），且强烈建议 SSD。

### 3.3 读的两种模式

- **Linearizable（默认）**：请求也要经 leader 确认（通过 ReadIndex 机制确认自己仍是 leader），读到的一定是最新提交数据；
- **Serializable**：直接读本地节点 bbolt，可能读到旧数据，但零共识开销——适合对一致性不敏感的dashboard 类读。

### 3.4 MVCC、Watch 与压缩

- Watch 基于 MVCC：客户端可从"上次的 revision"续订，断线重连后从缺口继续，事件不丢；
- 历史 revision 会无限膨胀磁盘，因此必须配置压缩：

```yaml
auto-compaction-mode: periodic
auto-compaction-retention: "1h"   # 只保留 1 小时历史
```

- 压缩只是"逻辑删除"，空间要靠 `etcdctl defrag` 回收（在线操作，逐节点执行，注意 follower 优先）。

### 3.5 Lease：服务注册的标准姿势

租约是一个带 TTL 的服务端对象；key 可以附着在租约上，进程心跳续约（KeepAlive），进程挂了租约超时，**所有绑定的 key 自动删除**，Watch 的下游立刻感知——这是 etcd 做服务发现的核心机制。

### 3.6 容量与仲裁

| 集群规模 | Quorum | 容忍故障 |
|---|---|---|
| 1 | 1 | 0（仅开发） |
| 3 | 2 | 1 |
| 5 | 3 | 2 |

**永远用奇数节点**：4 节点与 3 节点同样只能容忍 1 台故障，却多付一份复制成本、更大分区脑裂风险。

### 3.7 Watch 风暴：慢消费者、revision 增长与缓冲

Watch 在语义上是两件事的拼接：**从指定 revision 重放历史事件 + 持续推送增量**。客户端传 `WithRev(rv+1)`，意思是"rv 之后的事件我全要"。这个 rv 在 etcd 原生 API 里叫 revision，在 K8s 里叫 `resourceVersion`——全部容错逻辑都由它决定。

服务端据此把 watcher 分两类：

- **synced**：起始 revision 已追平当前值，之后只需在每次事务提交时扇出事件；
- **unsynced**：起始 revision 落后，需要先从内存 `keyIndex` 回放历史，回放完才转 synced。

**起始 revision 一旦已被 compaction 裁掉，服务端只能返回 `ErrCompacted`。** 这是正常运维事件不是 bug：客户端必须放弃增量，重新全量 `Get --prefix` 拿到快照与 `header.Revision`，再从那里续订。只 Watch 不 List 的实现，压缩之后必然静默丢数据。

慢消费者的传导链条：

```text
客户端消费慢（apply 逻辑重 / GC 停顿 / 网络拥塞）
        │
        ▼
服务端 send 队列积压 ──► watchableStore 中该 watcher 的 pending 事件堆积
        │
        ▼
内存上涨（不是磁盘），etcd_debugging_mvcc_slow_watcher_total 上升
        │
        ▼
跟不上就被判为 victim 取消，客户端收到 Canceled，只能重建 watch
```

每个 watch stream 在服务端占一个 goroutine 加一条 gRPC 流，缓冲不是无限的。另外单条请求上限 `--max-request-bytes`（默认 1.5MB）间接限制了单次 watch 响应里能打包多少事件——大 value 配高频写，很容易把消费者打挂。

两个常被忽略的开关：`WithProgressNotify()` 让服务端每 `--watch-progress-notify-interval`（3.6 转正；3.4.11–3.5 叫 `--experimental-watch-progress-notify-interval`，默认 10min）推一次空事件、只带 `header.revision` 的响应，证明"你已追平"（K8s 的 bookmark 事件就来自这里）；`clientv3.WithRequireLeader(ctx)` 让 watch 在集群失去 leader 时直接返回 `ErrNoLeader`，否则它会安静地不动，调用方无法区分"没变化"和"集群坏了"。

规模化方面，N 个客户端 watch 同一前缀 = N 条流 + N 个服务端 goroutine + 每事件 N 次序列化。一万个进程各自 watch 自己的 key 是反模式，应该**在一个进程内合并成一条前缀 watch、本地按 key 分发**。K8s 正是这么做的（apiserver 的 watch cache 对 etcd 只开一路 watch，所有客户端共享）；1.27+ 又加了 `WatchList`（`sendInitialEvents`），把全量与增量合并进一个流式请求，从快照 revision 直接续上，消掉了 `List → Watch` 之间因 rv 过期而反复重试的窗口。

一个不丢事件又不会被压缩打死的循环：

```go
// watchLoop 从 rv 起监听 prefix，遇 ErrCompacted 自动全量重列后续订。
// apply 把一次 KV 变更合并进本地状态，reset 在重列前清空本地状态；返回最后处理到的 revision。
func watchLoop(ctx context.Context, cli *clientv3.Client, prefix string, rv int64,
	apply func(k, v []byte), reset func()) (int64, error) {
	for {
		wch := cli.Watch(clientv3.WithRequireLeader(ctx), prefix,
			clientv3.WithPrefix(), clientv3.WithRev(rv+1), clientv3.WithProgressNotify())
	replay:
		for resp := range wch {
			if err := resp.Err(); err != nil {
				if !errors.Is(err, rpctypes.ErrCompacted) {
					return rv, err
				}
				gresp, gerr := cli.Get(ctx, prefix, clientv3.WithPrefix())
				if gerr != nil {
					return rv, gerr
				}
				reset() // 先清空：只 upsert 会让压缩窗口内删除的 key 永久残留在本地状态
				for _, kv := range gresp.Kvs {
					apply(kv.Key, kv.Value)
				}
				rv = gresp.Header.Revision
				break replay // 拿新基线重新 Watch
			}
			for _, ev := range resp.Events {
				apply(ev.Kv.Key, ev.Kv.Value)
				rv = ev.Kv.ModRevision
			}
			if len(resp.Events) == 0 && resp.Header.Revision > rv {
				rv = resp.Header.Revision // progress notify：只推进水位，不改状态
			}
		}
		if err := ctx.Err(); err != nil {
			return rv, err
		}
	}
}
```

### 3.8 压缩与碎片：为什么磁盘不降、快照怎么恢复

MVCC 的代价是**写入即新增**：每次 Put 产生新 revision，旧 revision 仍在 bbolt 里，`keyIndex` 也保留完整版本链。所以磁盘用量跟着"写过多少次"涨，而不是跟着"现在有多少 key"涨。压缩是唯一的止血手段，但它只是**逻辑删除**——把 ≤ 目标 revision 的版本标记成墓碑，bbolt 里这些页变成 free page，文件本身不缩小。

| 操作 | 做了什么 | 代价 |
|---|---|---|
| compaction | 逻辑删除 ≤ N 的历史 revision | 走 Raft 提案、全网广播，CPU 尖刺 |
| defrag | 重写 bbolt 文件，回收 free page | 该成员短暂阻塞读写（秒~分钟） |
| snapshot save | 热备份全量状态 | 磁盘 IO，不影响可用性 |
| restore | 从快照重建集群 | 全集群停机，member/cluster ID 全部重生成 |

自动压缩两个开关：`--auto-compaction-mode=periodic`（默认，按时间，retention 写 `"1h"` 这类时长）或 `=revision`（按版本数，retention 写 `1000`，保留最近 1000 个 revision）。periodic 更贴合"我最多能容忍多长的 Watch 断线窗口"这个直觉；revision 模式适合写入速率稳定的集群，磁盘增长可预测。

defrag 的纪律：**逐节点、低峰、follower 先做、leader 最后**。同一时刻只做一台，否则集群失去多数派直接不可用；期间该节点读写被阻塞（客户端会看到 `etcdserver: defragmentation is in progress`）。是否需要 defrag，看 `DB SIZE` 与 `DB SIZE IN USE` 的差距：

```bash
etcdctl endpoint status --cluster -w table
etcdctl endpoint status --cluster -w json \
  | jq '[.[] | {ep: .Endpoint, size: .Status.dbSize, inUse: .Status.dbSizeInUse}]'
```

备份与恢复的完整流程。注意 **restore 生成的是新集群**：member ID 与 cluster ID 全部重写，原 data-dir 作废：

```bash
# 1) 备份：任意一个成员即可，快照是一致性快照
etcdctl snapshot save /backup/etcd-$(date +%F).db \
  --endpoints=https://10.0.0.1:2379 \
  --cacert=/etc/etcd/ca.crt --cert=/etc/etcd/server.crt --key=/etc/etcd/server.key
etcdutl snapshot status /backup/etcd-2026-11-19.db -w table   # 校验 revision 与 key 数

# 2) 恢复：每个节点各自执行，参数用的是"新集群"的成员表
etcdutl snapshot restore /backup/etcd-2026-11-19.db \
  --name node1 \
  --initial-cluster node1=https://10.0.0.1:2380,node2=https://10.0.0.2:2380,node3=https://10.0.0.3:2380 \
  --initial-cluster-token etcd-restore-1 \
  --initial-advertise-peer-urls https://10.0.0.1:2380 \
  --data-dir /var/lib/etcd-restored    # 不覆盖原 data-dir，留作事后取证

# 3) 三节点都指向 /var/lib/etcd-restored 启动，集群以快照时刻的状态对外服务
#    只恢复一个成员再拉回旧集群是行不通的：member ID 对不上，peer 校验直接失败
```

**`alarm NOSPACE` 的处置顺序**（告警触发后集群只读，连 `del` 都会被拒）：

1. `etcdctl alarm list` 确认是 NOSPACE，并看 `endpoint status` 里 `DB SIZE` 是否顶到 `--quota-backend-bytes`；
2. 先 `etcdctl alarm disarm` 腾出写窗口（不解除告警就删不掉数据，这是个鸡生蛋问题）；
3. 删掉可回收的大前缀 → `etcdctl compaction <rev>` → 逐节点 `etcdctl defrag`；
4. 复查 `DB SIZE IN USE` 确实降下来。降不下来就只能提配额或迁移数据——告警不会自愈。

---

## 4. 使用实战（Go，clientv3）

```bash
go get go.etcd.io/etcd/client/v3
```

### 4.1 基础 KV 与前缀查询

```go
cli, _ := clientv3.New(clientv3.Config{
    Endpoints: []string{"127.0.0.1:2379"},
    DialTimeout: 5 * time.Second,
})
defer cli.Close()

ctx, cancel := context.WithTimeout(context.Background(), time.Second)
defer cancel()

// Put
cli.Put(ctx, "/config/db-host", "mysql-1")

// Get 单键
resp, _ := cli.Get(ctx, "/config/db-host")
fmt.Println(string(resp.Kvs[0].Value)) // mysql-1

// 前缀查询（模拟"目录"）
cli.Put(ctx, "/services/user/10.0.0.5", "8080")
presp, _ := cli.Get(ctx, "/services/user/", clientv3.WithPrefix())
for _, kv := range presp.Kvs {
    fmt.Printf("%s -> %s\n", kv.Key, kv.Value)
}
```

### 4.2 Watch：监听配置变更

```go
rch := cli.Watch(context.Background(), "/config/", clientv3.WithPrefix())
for wresp := range rch {
    for _, ev := range wresp.Events {
        switch ev.Type {
        case clientv3.EventTypePut:
            fmt.Printf("更新 %s = %s\n", ev.Kv.Key, ev.Kv.Value)
        case clientv3.EventTypeDelete:
            fmt.Printf("删除 %s\n", ev.Kv.Key)
        }
    }
}
```

### 4.3 Lease + 注册：服务发现骨架

```go
// 申请 10s 租约
grant, _ := cli.Grant(ctx, 10)
// key 绑定租约
cli.Put(ctx, "/services/user/10.0.0.5", "8080", clientv3.WithLease(grant.ID))
// 自动续约（进程存活期间）；进程退出后 10s 内 key 自动删除
ch, _ := cli.KeepAlive(context.Background(), grant.ID)
go func() { for range ch {} }() // 消费续约应答
```

### 4.4 事务：分布式锁的核心（concurrency 包）

```go
// 简单方式：官方封装的分布式锁（需 import "go.etcd.io/etcd/client/v3/concurrency"）
func tryLock(cli *clientv3.Client) error {
	session, _ := concurrency.NewSession(cli)
	defer session.Close()

	mutex := concurrency.NewMutex(session, "/lock/order-job")
	if err := mutex.Lock(context.Background()); err != nil { // 底层：Txn(create key) + 自旋
		return err
	}
	// ... 临界区 ...
	return mutex.Unlock(context.Background())
}
```

手写 CAS 一窥事务原语：

```go
// 若 /counter 版本未变，则原子执行两个写
txn := cli.Txn(ctx).
    If(clientv3.Compare(clientv3.Version("/counter"), "=", 3)).
    Then(
        clientv3.OpPut("/counter", "4"),
        clientv3.OpPut("/audit", "updated"),
    ).
    Else(clientv3.OpGet("/counter"))
resp, _ := txn.Commit()
```

### 4.5 运维常用命令

```bash
etcdctl put /foo bar
etcdctl get /foo
etcdctl get /services/ --prefix --keys-only
etcdctl del /services/ --prefix
etcdctl watch /config/ --prefix
etcdctl lease grant 60
etcdctl lease keep-alive <lease-id>

etcdctl member list                        # 集群成员
etcdctl endpoint status --cluster -w table # 健康与 DB 大小
etcdctl endpoint health --cluster
etcdctl snapshot save backup.db            # 备份
etcdutl snapshot restore backup.db --data-dir /new-data  # 恢复（v3.6 起命令归 etcdutl）
etcdctl compaction 1000                    # 压缩到 revision 1000
etcdctl defrag                             # 碎片整理
```

### 4.6 集群运维：成员增删、learner 与迁移

**成员变更本身就是 Raft 提案**，所以纪律是：一次只改一台，等集群稳定（`endpoint status` 里所有成员的 `raftIndex` 基本齐平）再动下一台。扩容顺序是"先加后删"：新成员先加入、追平日志、确认能服务，再移除旧成员——绝不要先删后加，中间那段时间 quorum 会变小。

learner 是扩容的缓冲垫：只接收日志、**不投票、不计入 quorum**，追平后 `member promote` 才成为正式成员。它解决两个问题：新节点从零回放几 GB 日志时不会拖慢提交（voting member 落后会卡住 commit 的确认路径）；跨机房加入时不会因高延迟抖动影响选举。默认最多 1 个 learner（`--max-learners`）。

跨机房/跨 AZ 的核心参数是心跳与选举超时：

- `--heartbeat-interval` 默认 100ms——leader 多久发一次心跳；
- `--election-timeout` 默认 1000ms——follower 多久没收到心跳就开始选举，**必须是心跳的 5~10 倍**；
- RTT 接近或超过心跳时，follower 会误判 leader 失联，陷入无休止重选（`etcd_server_leader_changes_seen_total` 疯涨、写入全超时）。跨 AZ 把心跳提到 RTT 的 2~3 倍（200~300ms），选举相应提到 2000~3000ms，代价是真实故障的检测与切换变慢；
- 地域间 RTT > 50ms 就别放进同一个集群：写延迟等于最慢那个多数派的往返时间。

**多数派丢失时的决策树**：

| 现状 | 能做什么 |
|---|---|
| 仍有多数派 | 正常服务；挂掉的成员修好后 `member remove` 再重新 `member add` |
| 失去多数派、数据盘都在 | 无法读写，也无法 `member remove`（remove 同样要 quorum）。先恢复网络/机器，实在不行走快照恢复 |
| 只剩一个存活成员、且没有快照 | 只能在该成员上用 `--force-new-cluster`（丢弃未提交日志，成员数变 1），再逐个 `member add` 重建；其他成员必须清空 data-dir，否则校验失败拒绝启动 |

没有快照的恢复是最后手段——这正是"每天 `snapshot save` + 异地留存"的唯二理由之一（另一个是升级前回滚）。

大 value 的量化边界：`--max-request-bytes` 默认 **1.5MB**，限制的是**整个请求**（key + value + Txn 内所有操作的总和），超了直接报 `etcdserver: request is too large`；`--quota-backend-bytes` 默认 **2GiB**。经验值：单 value < 100KB，单 key 写频率 < 10 次/秒，总量 < 1GiB。因为大 value 会被复制进 Raft 日志、写进 WAL、打进 snapshot，一次写的成本被放大好几倍。

```bash
export ETCDCTL_API=3                       # v3.4+ 默认即 3，老脚本里仍在设
alias ectl='etcdctl --endpoints=https://10.0.0.1:2379,https://10.0.0.2:2379,https://10.0.0.3:2379 \
  --cacert=/etc/etcd/ca.crt --cert=/etc/etcd/client.crt --key=/etc/etcd/client.key'

# 扩容：先加 learner，等追平再提升
ectl member add node4 --peer-urls=https://10.0.0.4:2380 --learner
# 用输出里的 ETCD_NAME / ETCD_INITIAL_CLUSTER / ETCD_INITIAL_CLUSTER_STATE=existing 启动新节点
ectl endpoint status -w table               # 看 node4 的 raftIndex 是否追上 leader
ectl member promote <node4-member-id>

# 缩容：确认剩余成员仍是奇数且构成多数派，一次删一个
ectl member remove <old-member-id>
ectl member list -w table
```

`member add` 的输出就是新节点启动时该用的环境变量。抄错一个字母，新节点会以错误的 member ID 加入，之后只能 `member remove` 重来。

### 4.7 性能与调优：fsync、批量与请求大小

一次 Put 的物理成本是**两次落盘**：Raft 日志 append 时的 WAL fsync，以及 apply 到 MVCC 时的 bbolt commit（后者按 `--backend-batch-interval` / `--backend-batch-limit` 合并，默认 100ms / 10000 条，所以并发写会被批处理摊薄）。串行单条写的吞吐上限 ≈ 1 / fsync 延迟：SSD 上 1ms 就是 1000 QPS，机械盘或共用云盘 10ms 就只剩 100 QPS。**写瓶颈在 fsync，不在 CPU，也不在 etcd 的代码。**

优化的方向也就三条：

- **合并事务**：N 个 Put 塞进一个 `Txn`，只有一次 Raft 往返、一次 fsync，吞吐接近 N 倍。约束是 `--max-txn-ops`（默认 128）与 `--max-request-bytes`（1.5MB）；也别贪多，超大 Txn 会让 apply 变慢，`etcd_server_slow_apply_total` 跟着涨。
- **复用连接**：一个进程一个 `clientv3.Client`（内置 gRPC 连接与 stream 复用），不要每次请求都 `clientv3.New`。TLS 双向认证的握手成本远高于单次 KV 操作，启用 `--client-cert-auth` 后尤其明显；watch 多的客户端还要留意 `--max-concurrent-streams`——单连接上流太多时，一条慢流会拖累同连接的其他流。
- **读分级**：默认线性一致读要走 ReadIndex（一次心跳往返，可能还要等 leader 确认），延迟约 1 个 RTT；`clientv3.WithSerializable()` 直接读本地 bbolt，延迟降到百微秒级，但可能读到落后的数据（follower 落后没有上限）。巡检、dashboard、配置展示用 serializable，选主、配置下发、加锁必须 linearizable。

配额与告警：`--quota-backend-bytes` 默认 2GiB，用量到 60%~70% 就该处理（压缩 + defrag，或提配额），打满即 NOSPACE 只读。

```bash
# 生产基线参数（SSD、独立盘、3 节点）
etcd \
  --quota-backend-bytes=8589934592 \
  --auto-compaction-mode=periodic \
  --auto-compaction-retention=1h \
  --snapshot-count=10000 \
  --max-request-bytes=1572864 \
  --max-txn-ops=128 \
  --heartbeat-interval=100 --election-timeout=1000
# quota 8GiB；snapshot-count 10000 是 v3.6 起的默认值（内存更省）；
# max-request-bytes 1.5MB 也是默认值，显式写出来便于审计

etcdctl check perf --load=m     # 单机基线体检，PASS 说明磁盘/网络够用
```

必盯的五个指标（其余大多是它们的派生）：

| 指标 | 关注阈值 | 异常说明什么 |
|---|---|---|
| `etcd_server_leader_changes_seen_total` | 24h 内为 0 | 非 0 就是选举在抖：网络分区、CPU 饥饿、election-timeout 太小 |
| `etcd_server_proposals_failed_total` | 增长速率为 0 | 提案失败：leader 切换、成员失联、quorum 不完整 |
| `etcd_mvcc_db_total_size_in_bytes` / `_in_use_bytes` | 配额 70% 告警 | 前者涨是历史版本多（该压缩），两者差距大是碎片多（该 defrag） |
| `etcd_disk_backend_commit_duration_seconds` | p99 < 25ms | bbolt 提交慢 = 磁盘扛不住，直接体现为写延迟 |
| `etcd_server_slow_apply_total` | 增长速率为 0 | apply 跟不上提交：CPU 不足或单次 Txn 太大 |

再补一个底层指标：`etcd_disk_wal_fsync_duration_seconds` 的 p99 就是写延迟的"天花板"，它超过 10ms 该换盘，而不是继续调 etcd 参数。

---

## 5. 版本要点：v3.7 / v3.6（2025.5–2026.10）

- **v3.7（2026.10）**：延续 v3.6 的存储与内存优化，主要在稳定性、运维接口与客户端兼容性上迭代；新集群直接上 v3.7，v3.6 → v3.7 属小版本升级；
- **v2 存储正式退场**（v3.6）：`--enable-v2` 等标志移除，成员数据全部迁入 v3store（v3.6 内部完成迁移）；
- **内存减半**：`--snapshot-count` 默认从 100000 降到 10000，快照保留更少的历史，平均内存占用下降 50%+；
- **livez/readyz**：新增 K8s 风格健康检查端点，`/livez` 判进程是否需重启、`/readyz` 判是否可服务流量；
- **官方支持降级（downgrade）**：升级出问题可在限定版本间回退；
- **Feature Gates 机制**：实验特性显式开关；
- **治理变化**：etcd 移交 Kubernetes 社区成为 SIG-etcd；raft 库独立为 `github.com/etcd-io/raft`，依赖 bbolt 1.4；
- 破坏性变更：**peer 端口(2380)不再响应客户端请求**、旧版本二进制与新版 schema 不兼容（升级需逐节点滚动、不可跨大版本跳跃）。

版本线：v3.4 已 EOL，v3.5 维护中（K8s 1.22–1.33 可用），**新集群建议 v3.6**。

---

## 6. 常见坑与最佳实践

1. **容量红线**：默认 2GiB 配额，超限进入只读 alarm。存大 value（>1MB 禁止，建议 <100KB）是误用——etcd 是元数据存储不是数据湖；
2. **必须开自动压缩**：不压缩的历史 revision 会撑爆磁盘并拖慢性能；
3. **奇数节点、不要 7 台以上**：写吞吐随集群规模下降，3~5 台是甜点；
4. **磁盘是生命线**：fsync 延迟直接决定写延迟，生产必须 SSD，且不要与其他 IO 密集进程共盘；
5. **defrag 要逐节点、低峰执行**：会短暂锁 DB；
6. **K8s 多副本服务的定时/选主逻辑**：用 etcd 的 concurrency 包（锁/选举）而不是自己造轮子；
7. **大 range 分页**：`WithLimit` + `WithFromKey` 分批拉取，防止大响应打爆内存；
8. **升级纪律**：一次只升一个小版本、逐节点滚动、先备份 snapshot、看官方兼容矩阵。

---

## 7. 选型对比

| 维度 | etcd | Redis | ZooKeeper | Consul |
|---|---|---|---|---|
| 一致性 | 强一致(Raft) | 异步复制/弱 | 强一致(ZAB) | 强一致(Raft) |
| 数据模型 | MVCC KV | 丰富数据结构 | 树状 ZNode | KV+服务目录 |
| Watch | 历史可回放 | keyspace 通知(弱) | 一次性 Watch | 长轮询/blocking query |
| 适用 | K8s/元数据/锁 | 缓存/队列 | 老式 Hadoop 系 | 服务发现+DNS |

一句话：**元数据与服务协调选 etcd，缓存与高吞吐数据选 Redis，不要混用**。
