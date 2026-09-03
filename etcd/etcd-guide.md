# etcd 技术介绍与使用指南

> 分布式强一致 KV 存储，Kubernetes 控制平面的心脏。Raft 共识 + MVCC 多版本 + bbolt B+ 树。
> 仓库：<https://github.com/etcd-io/etcd>　|　当前主线：v3.6（2025.5.15 发布，距 v3.5 近四年）

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

```
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
import "go.etcd.io/etcd/client/v3/concurrency"

// 简单方式：官方封装的分布式锁
session, _ := concurrency.NewSession(cli)
mutex := concurrency.NewMutex(session, "/lock/order-job")
mutex.Lock(context.Background())   // 底层：Txn(create key) + 自旋
// ... 临界区 ...
mutex.Unlock(context.Background())
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

---

## 5. v3.6 版本要点（2025.5）

- **v2 存储正式退场**：`--enable-v2` 等标志移除，成员数据全部迁入 v3store（v3.6 内部完成迁移）；
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
