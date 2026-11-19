# go-redis 技术介绍与使用指南

> Go 生态事实标准的 Redis 客户端：命令全覆盖、连接池与 Pipeline 开箱即用，同时支持单机 / Sentinel / Cluster / Ring。
> 仓库：<https://github.com/redis/go-redis>　|　主线版本：**v9**（模块路径 `/v9`，整理于 2026-11）　|　协议：BSD-2-Clause
> 服务端基线：Redis 6.0+（推荐 7.x）；具体版本以上游最新 release 为准。

---

## 1. 项目简介

需要先分清两件事：**Redis 是服务端**，**go-redis 是 Go 语言的客户端库**。本文讲后者——它把 Redis 的命令映射成类型安全的 Go 方法，并负责连接池、重试、超时、集群路由这些「不写就会踩坑」的工程细节。

核心卖点：

- **命令覆盖最全**：String / Hash / List / Set / ZSet / Stream / Bitmap / HyperLogLog / Geo / Lua / Pub-Sub / 事务，几乎 1:1 对应 Redis 命令；
- **连接管理内建**：`Client` 本身就是一个并发安全的连接池，全局复用即可，无需自己包一层 pool；
- **上下文原生**：v9 起所有命令首参强制 `context.Context`，超时、取消、链路追踪可一路透传；
- **拓扑透明**：单机、Sentinel 故障转移、Cluster 分片、Ring（客户端分片）用同一套 `Cmdable` 接口；
- **可观测**：Hook 接口可挂 OpenTelemetry / Prometheus，逐命令埋点。

适用场景：缓存、分布式锁、限流计数、排行榜、会话存储、轻量消息（Stream）、幂等去重。**不适合**：把它当主数据库存关系型数据、用它做需要严格事务与持久化的任务队列（后者请看 [asynq 篇](../asynq/asynq技术文档与使用手册.md)）、或在没有淘汰策略设计的情况下塞大对象。

### 安装

```bash
go get github.com/redis/go-redis/v9
# 注意模块路径带 /v9；老教程里的 github.com/go-redis/redis/v8 已归档
```

### 最小可运行示例

```go
package main

import (
    "context"
    "fmt"
    "time"

    "github.com/redis/go-redis/v9"
)

func main() {
    rdb := redis.NewClient(&redis.Options{
        Addr:     "localhost:6379",
        Password: "", // 无密码
        DB:       0,
    })
    defer rdb.Close()

    ctx := context.Background()
    if err := rdb.Set(ctx, "greeting", "hello", 10*time.Minute).Err(); err != nil {
        panic(err)
    }
    val, err := rdb.Get(ctx, "greeting").Result()
    if err != nil {
        panic(err)
    }
    fmt.Println(val) // hello
}
```

---

## 2. 功能特性总览

| 能力 | 说明 |
|---|---|
| 数据结构命令 | String / Hash / List / Set / ZSet / Stream / Bitmap / HLL / Geo，方法名与 Redis 命令同名 |
| 连接池 | `PoolSize` / `MinIdleConns` / `MaxIdleConns` / `PoolTimeout` / `ConnMaxLifetime`，`Client` 并发安全 |
| 批处理 | `Pipeline`（省 RTT）、`TxPipeline` / `TxPipelined`（MULTI/EXEC，原子）、`Pipelined` 回调式 |
| 事务与脚本 | `Watch` 乐观锁、`TxPipelined`；`redis.NewScript` 自动走 EVALSHA + NOSCRIPT 回退 |
| 发布订阅 | `Subscribe` / `PSubscribe` / `Publish`，`Channel()` 返回 Go channel |
| Stream | `XAdd` / `XReadGroup` / `XGroupCreateMkStream` / `XAck` / `XPending`，消费组语义完整 |
| 分布式锁 | `SetNX` + 唯一 token + Lua 释放；也可用官方 `redislocking` 等封装 |
| 拓扑支持 | `NewClient` / `NewFailoverClient`（Sentinel）/ `NewClusterClient` / `NewRing` |
| 弹性 | `MaxRetries` + 指数退避、`DialTimeout` / `ReadTimeout` / `WriteTimeout`、`Context` 超时 |
| 可观测 | `AddHook` 挂 OpenTelemetry / 指标；`PoolStats()` 拿连接池命中率 |
| 调试 | `redis.Nil` 哨兵错误、`Do()` 执行任意命令、`Process` 自定义命令流程 |

---

## 3. 核心概念与原理

### 3.1 Client 就是连接池

```text
业务 goroutine ──┐
业务 goroutine ──┼──► redis.Client（Cmdable 接口）
业务 goroutine ──┘          │ 借/还连接（sync.Pool 风格的连接池）
                            ▼
                   ┌────────┬────────┬────────┐
                   │ conn 1 │ conn 2 │ conn N │  ── RESP 协议 ──► Redis Server
                   └────────┴────────┴────────┘
```

- **一个 `Client` 应全局复用**：每次 `NewClient` 都会新建一套连接池，在请求路径里创建会瞬间打满文件描述符并放大 TCP 握手开销；
- 每个命令从池里借一条连接、写完读回、归还；因此 `Client` 及其命令方法**并发安全**；
- `PoolSize` 默认约 `10 × GOMAXPROCS`，压测时按下游 Redis 的 `maxclients` 与业务并发反推，不是越大越好。

### 3.2 RESP 与命令构造

客户端把 `Set(ctx, k, v, ttl)` 序列化成 RESP 数组（`*5\r\n$3\r\nSET\r\n...`）发给服务端，再解析回复为 `*redis.StringCmd` 等命令对象。**命令对象本身只是结果容器**——必须调用 `.Result()` / `.Err()` / `.Val()` 才真正取值，漏掉 `.Err()` 是本库最高频的静默 bug。

### 3.3 Pipeline vs TxPipeline

| | Pipeline | TxPipeline |
|---|---|---|
| 网络 | N 条命令打包成 1 次写 + 1 次读 | 同左 |
| 服务端执行 | 逐条执行，中间可能插入其他客户端命令 | `MULTI ... EXEC`，**原子**执行 |
| 原子性 | 无 | 有 |
| 返回值 | 每个命令各自的 result | 同左，但 `EXEC` 失败时整体失败 |

一句话：**只是为了省 RTT 用 Pipeline；需要「读-改-写」不被打断用 TxPipeline 或 Lua**。注意 Pipeline 不减少服务端工作量，超大批量（万级）应分批，避免阻塞 Redis 单线程。

### 3.4 Context 传播

v9 把 `ctx` 作为所有命令的首参，超时会主动关闭底层连接（Redis 服务端无法中途取消命令，客户端只能断连）。因此**超时设得过短比不设更危险**：会造成连接频繁重建与命令半执行。建议命令级超时略大于 P99 延迟，另用 `DialTimeout` 控制建连。

### 3.5 Cluster 路由

Cluster 把键空间切成 16384 个 slot（`CRC16(key) mod 16384`）。客户端缓存 slot→节点映射，遇到 `MOVED` 刷新路由、遇到 `ASK` 临时转向。**跨 slot 的多键操作（MGET、Lua 多 KEYS、事务）会直接报错**，需要 hash tag（`{user42}:profile`）把相关键固定到同一 slot。

### 3.6 Cluster 与 Sentinel：MOVED/ASK、拓扑刷新与故障转移

`MOVED` 与 `ASK` 都是重定向，但语义完全不同，混淆会污染路由表：

| | `MOVED` | `ASK` |
|---|---|---|
| 含义 | 槽已经**永久**迁移到目标节点 | 槽正在迁移中，该键暂时在目标节点 |
| 客户端动作 | 更新本地 slot→节点表，后续请求直接走新节点 | **只对本次请求**临时转向，不更新本地表 |
| 原因 | 槽归属已变更 | 槽仍属原节点，只是这个键先搬过去了 |

迁移是逐键进行的（源节点 `MIGRATING`、目标节点 `IMPORTING`），此时槽并没有易主。若收到 `ASK` 就刷新路由表，尚未迁移的键会被打到新节点并收到 `MOVED`，形成重定向风暴。

go-redis 的 `ClusterClient` 内置拓扑发现：后台周期性执行 `CLUSTER SLOTS` 刷新 slot 表，也可以传 `ClusterSlots` 自定义（比如从注册中心拿地址）。`MaxRedirects` 限制单条命令的重定向次数，超限直接报错而不是无限跳转。Sentinel 则是主从架构的自动故障转移：客户端不写死主节点地址，先问 Sentinel「主是谁」，再**直连那个数据节点**（后续命令不经过 Sentinel）。两种拓扑的初始化只差一个构造函数：

```go
// Cluster：客户端自己分片，slot 表由 CLUSTER SLOTS 维护
cluster := redis.NewClusterClient(&redis.ClusterOptions{
    Addrs:          []string{"10.0.0.1:6379", "10.0.0.2:6379", "10.0.0.3:6379"},
    MaxRedirects:   3,    // MOVED/ASK 重定向上限，防止拓扑异常时死循环
    RouteByLatency: true, // 读请求按 RTT 选节点
    ReadOnly:       true, // 允许读落到 replica：吞吐上去了，但可能读到旧值（stale read）
    PoolSize:       50,
})

// Sentinel：先向 Sentinel 要主节点地址，再连主从
failover := redis.NewFailoverClient(&redis.FailoverOptions{
    MasterName:    "mymaster",
    SentinelAddrs: []string{"10.0.0.1:26379", "10.0.0.2:26379"},
    Password:      "",
    DB:            0,
})
```

> `ReadOnly: true` 只影响读命令，写仍然路由到主。对「刚写完立刻回查」的 key 会读到复制延迟内的旧数据，这类读应固定走主，或干脆用单机 / Sentinel。

go-redis 通过 `SENTINEL get-master-addr-by-name` 发现主节点，并订阅 `+switch-master` 事件；主挂后 Sentinel 选出新主，客户端自动切过去。切换窗口内旧主可能仍接受写（脑裂）或返回 `READONLY`，业务必须靠重试 + 幂等兜底，不能假设「切换是瞬时的」。

排障两条命令：

```bash
redis-cli -c CLUSTER INFO    # cluster_state / slots_assigned / slots_ok
redis-cli -c CLUSTER NODES   # 槽区间、master/fail? 标记、迁移中的 [slot->-node] / [slot-<-node]
```

`cluster_state:fail` 常见于：槽未全覆盖（`slots_assigned < 16384`）、某主节点失联且故障转移未完成、或 `cluster-require-full-coverage yes` 下一个分片挂了全集群拒服。

跨槽操作则是 Cluster 最常踩的坑：`MGET k1 k2`、`MULTI/EXEC`、多 KEYS 的 `EVAL` 都要求所有键同槽，否则报 `CROSSSLOT Keys in request don't hash to the same slot`。hash tag 用花括号内的内容参与 CRC16，`{user:1}:profile` 与 `{user:1}:orders` 会固定到同一槽。代价是粒度失控——`{users}:*` 这种全局常量会把所有用户塞进一个节点，造成数据倾斜与单点热点。tag 要选**分布均匀且天然聚合**的业务主键（用户 ID、租户 ID），而不是图省事写死。

### 3.7 Lua 脚本：原子性与确定性约束

脚本在 Redis 单线程里执行，期间不会插入其他客户端命令，所以「读-改-写」天然原子——这是它相对 `WATCH` 乐观锁最大的价值。

加载流程：`EVAL` 每次把脚本体一起发过去，大脚本浪费带宽；`EVALSHA` 只发 40 字节 SHA1，命中则执行，未命中返回 `NOSCRIPT`。go-redis 的 `redis.NewScript` 已封装「先 EVALSHA、遇 NOSCRIPT 回退 EVAL 并缓存」，不必自己写。集群环境可用 `SCRIPT LOAD` 预加载 + `SCRIPT EXISTS` 探活，避免冷启动时每个节点都回退一次。

确定性约束：脚本里的写操作会传播到从库和 AOF。Redis 5.0 起默认按**效果复制**（effect replication）——复制脚本产生的实际写命令而非脚本本身；7.0 起彻底移除逐字复制。5.0 之前默认逐字复制，脚本里带 `TIME`、`math.random`、`SRANDMEMBER` 会让主从算出不同结果，直接数据分叉。即使在 5.0+，也建议把时间、随机数由调用方通过 `ARGV` 传入：可复现、可测试、不依赖节点时钟。

错误处理：`redis.call` 遇到错误会中断脚本并向上抛（**但已执行的写不会回滚**，Redis 脚本没有事务回滚）；`redis.pcall` 把错误包成 table 返回，脚本可以继续。想「失败就整体不生效」，只能靠脚本自己保证所有写都在校验之后。

超时：`busy-reply-threshold`（旧名 `lua-time-limit`，默认 5s）到点后 Redis 开始对其他客户端回 `BUSY`，但**不会主动杀脚本**，只能 `SCRIPT KILL`（仅限尚未产生写）或 `SHUTDOWN NOSAVE`。所以脚本必须短小、O(1)，不要在里面遍历大集合。

典型用法是令牌桶限流——取令牌与写回必须原子，否则并发下会超发：

```go
package cache

import (
	"context"
	"time"

	"github.com/redis/go-redis/v9"
)

// tokenBucket 是固定容量 + 匀速补充的令牌桶。
// 时间戳由调用方传入，脚本内不调用 TIME/RANDOM，保证可复现与主从一致。
var tokenBucket = redis.NewScript(`
local key  = KEYS[1]
local cap  = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
local now  = tonumber(ARGV[3])
local want = tonumber(ARGV[4])

local data   = redis.call("HMGET", key, "tokens", "ts")
local tokens = cap
local ts     = now
if data[1] then tokens = tonumber(data[1]) end
if data[2] then ts = tonumber(data[2]) end

tokens = math.min(cap, tokens + (now - ts) / 1000 * rate)
local ok = 0
if tokens >= want then
	tokens = tokens - want
	ok = 1
end
redis.call("HMSET", key, "tokens", tokens, "ts", now)
redis.call("PEXPIRE", key, math.floor(cap / rate * 1000))
return ok
`)

func Allow(ctx context.Context, rdb *redis.Client, key string, capacity int, ratePerSec float64, want int) (bool, error) {
	n, err := tokenBucket.Run(ctx, rdb, []string{key},
		capacity, ratePerSec, time.Now().UnixMilli(), want).Int()
	if err != nil {
		return false, err
	}
	return n == 1, nil
}
```

脚本只操作一个 key，Cluster 下无需 hash tag；若需要多 key，务必保证同槽（见 3.6）。

### 3.8 大 key 与热 key：发现与治理

没有官方阈值，以下是社区通用经验值：

- String 值 > 10KB；Hash / List / Set / ZSet 元素数 > 5000，或单 key 内存 > 1MB；Stream 长度 > 1 万。
- 危害：单线程处理大 key 的 `HGETALL`、序列化、`DEL` 都会阻塞其他请求；带宽被单 key 吃满；Cluster 下形成 slot 倾斜；RDB/AOF 重写时写时复制放大内存。

发现手段都带局限：

```bash
redis-cli --bigkeys                    # 全库 SCAN 采样，按元素数量找各类型最大的 key，占 CPU
redis-cli --memkeys                    # 同上，但按 MEMORY USAGE 排序（Redis 4+）
redis-cli MEMORY USAGE user:42         # 单键内存估算，嵌套结构是采样值
redis-cli --scan --pattern 'session:*' | head -100   # 自己抽样，再批量 MEMORY USAGE
```

`--bigkeys` 对 String 按字节数、对集合按元素数量排序：大 String 能发现，漏的是 field/元素很少但单个元素体积很大的 Hash / List / Set；`MEMORY USAGE` 对集合是抽样估算，`SAMPLES 0` 才精确但很慢；`DEBUG OBJECT` 在生产慎用。彻底排查应离线分析 RDB（rdb-tools、redis-rdb-cli），不影响线上。

热 key 指 QPS 远高于均值的 key：

- `redis-cli --hotkeys` 依赖 LFU 计数，`maxmemory-policy` 必须是 `allkeys-lfu` / `volatile-lfu`，否则直接报错；`OBJECT FREQ key` 可查单键频次。
- `MONITOR` 能看到全部命令，但有明显性能损耗（官方说法 50% 左右），只能短时定点排障，别常开。
- 最可靠的是客户端 Hook 按 key 维度打点，成本可控且能关联调用方。

治理四板斧：

1. **拆分**：大 Hash 按 field 哈希拆成 N 个子 key，大 String 分块；读取一律 `HSCAN`/`SSCAN` 游标增量，禁止 `HGETALL`/`SMEMBERS` 全量拉取。
2. **本地缓存**：热 key 在进程内缓存几百毫秒到几秒（Ristretto / bigcache），把绝大部分读挡在 Redis 之前；多实例要处理失效广播。
3. **读写分离**：`ReadOnly` 从副本读（见 3.6），或加从库分摊读流量，接受 stale read。
4. **限流保护**：热点接口做 singleflight 合并 + 令牌桶限流，防止缓存失效瞬间把 DB 打穿。

删除大 key 必须用 `UNLINK`（Redis 4+，后台线程异步回收），`DEL` 是同步释放，几百万元素的 Hash 能阻塞主线程数百毫秒；也可 `HSCAN` 分批 `HDEL`。同时打开 `lazyfree-lazy-expire yes`、`lazyfree-lazy-eviction yes`，让过期与淘汰也走异步。批量写入/预热时给 TTL 加 ±10% 随机抖动，避免同一时刻集体失效。

---

## 4. 使用实战

### 4.1 生产级初始化

```go
package cache

import (
    "context"
    "time"

    "github.com/redis/go-redis/v9"
)

func New(redisAddr, password string) (*redis.Client, error) {
    rdb := redis.NewClient(&redis.Options{
        Addr:         redisAddr,
        Password:     password,
        DB:           0,
        PoolSize:     50,               // 按并发量压测调整
        MinIdleConns: 10,               // 预热长连接，削掉冷启动毛刺
        DialTimeout:  3 * time.Second,
        ReadTimeout:  500 * time.Millisecond,
        WriteTimeout: 500 * time.Millisecond,
        MaxRetries:   2,                // 只对幂等命令安全，见 6.9
    })
    ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
    defer cancel()
    if err := rdb.Ping(ctx).Err(); err != nil {
        _ = rdb.Close()
        return nil, err
    }
    return rdb, nil
}
```

### 4.2 缓存读写：`redis.Nil` 必须单独处理

```go
func GetUser(ctx context.Context, rdb *redis.Client, id string) (*User, error) {
    key := "user:" + id
    b, err := rdb.Get(ctx, key).Bytes()
    switch {
    case err == nil:
        var u User
        if e := json.Unmarshal(b, &u); e != nil {
            return nil, e
        }
        return &u, nil

    case errors.Is(err, redis.Nil): // 缓存未命中，不是故障
        u, e := loadFromDB(ctx, id)
        if e != nil {
            return nil, e
        }
        // 空结果也缓存，防穿透；TTL 加随机抖动，防雪崩
        ttl := 10*time.Minute + time.Duration(rand.Intn(300))*time.Second
        _ = rdb.Set(ctx, key, mustJSON(u), ttl).Err()
        return u, nil

    default: // 真故障：不要让缓存挂掉拖垮 DB，按需降级
        return loadFromDB(ctx, id)
    }
}
```

> `redis.Nil` 表示「键不存在」，**它是正常业务分支**；把它当普通 error 上报会让监控全是假告警。

### 4.3 Pipeline 批量写

```go
pipe := rdb.Pipeline()
cmds := make([]*redis.StatusCmd, 0, len(items))
for _, it := range items {
    cmds = append(cmds, pipe.Set(ctx, "item:"+it.ID, mustJSON(it), time.Hour))
}
if _, err := pipe.Exec(ctx); err != nil && !errors.Is(err, redis.Nil) {
    return err
}
for _, c := range cmds { // Exec 之后才能读各命令结果
    if err := c.Err(); err != nil { /* 逐条处理 */ }
}
```

### 4.4 分布式锁（SetNX + Lua 释放）

```go
var releaseLock = redis.NewScript(`
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
end
return 0`)

func TryLock(ctx context.Context, rdb *redis.Client, key string, ttl time.Duration) (string, bool, error) {
    token := uuid.NewString() // 必须唯一，防止误删别人的锁
    ok, err := rdb.SetNX(ctx, key, token, ttl).Result()
    return token, ok, err
}

func Unlock(ctx context.Context, rdb *redis.Client, key, token string) error {
    return releaseLock.Run(ctx, rdb, []string{key}, token).Err()
}
```

> 这是**单实例**锁。Redlock / 跨主从故障转移的锁语义有争议；强一致互斥请改用 [etcd 篇](../etcd/etcd-guide.md) 的 Lease 锁。业务执行时间可能超过 TTL 时，还需要看门狗续期。

### 4.5 Stream 消费组

```go
// 生产
_, _ = rdb.XAdd(ctx, &redis.XAddArgs{
    Stream: "orders", MaxLen: 10000, Approx: true,
    Values: map[string]interface{}{"order_id": "A1001"},
}).Result()

// 建组（幂等：已存在会报 BUSYGROUP，忽略即可）
_ = rdb.XGroupCreateMkStream(ctx, "orders", "workers", "$").Err()

// 消费：> 表示只取从未投递给本组的消息
streams, err := rdb.XReadGroup(ctx, &redis.XReadGroupArgs{
    Group: "workers", Consumer: "worker-1",
    Streams: []string{"orders", ">"}, Count: 10, Block: 2 * time.Second,
}).Result()
for _, s := range streams {
    for _, msg := range s.Messages {
        handle(msg.Values)
        _ = rdb.XAck(ctx, "orders", "workers", msg.ID).Err() // 别忘了 ACK
    }
}
```

> 未 ACK 的消息进 PEL（Pending Entries List），需要定期 `XAutoClaim` 处理崩溃的消费者，否则会永久堆积。

### 4.6 运维速查

```bash
redis-cli INFO clients          # 连接数是否打满
redis-cli SLOWLOG GET 10        # 慢命令
redis-cli --bigkeys             # 找大 Key（用 --memkeys 更准）
redis-cli --scan --pattern 'user:*' | head   # 遍历键，生产禁用 KEYS *
```

### 4.7 缓存一致性：三种模式与失效顺序

| 模式 | 写路径 | 读路径 | 一致性 | 适用 |
|---|---|---|---|---|
| Cache-Aside（旁路缓存） | 更新 DB → **删除**缓存 | 未命中读 DB 并回填 | 最终一致 | 绝大多数业务 |
| Read/Write-Through | 写缓存，由缓存层同步写 DB | 只读缓存 | 取决于实现 | 缓存中间件/库 |
| Write-Behind | 只写缓存，异步批量刷 DB | 只读缓存 | 弱，宕机可能丢写 | 计数、允许丢失的场景 |

Cache-Aside 的写顺序为什么是「先更新库、再删缓存」：

- **删除缓存，而不是更新缓存**。更新缓存要求写入方完整重建对象，且并发写写时「更新 DB 的顺序」与「更新缓存的顺序」可能相反，缓存会永久留下旧值；删除是幂等的，谁最后删都收敛到正确结果。
- **不要先删缓存再更新库**。删完到 DB 提交之间若有读请求，会把旧值回填，DB 更新后缓存就成了脏数据。先更新库、后删缓存，脏窗口只存在于「删除完成前」，且大概率被下一次读纠正。
- 更新库成功、删缓存失败仍会长期不一致，需要补偿：本地重试队列（带退避，注意进程退出丢任务）、消息队列、或订阅 binlog（Canal/Debezium）异步失效。binlog 方案把「改库」与「失效」解耦，最可靠，代价是多一条链路要运维。无论如何，**给缓存设 TTL 是最后的一致性兜底**。

延迟双删（更新前后各删一次，第二次延迟 500ms~1s）只解决一种场景：读请求在「删缓存」与「DB 提交」之间把**从库的旧值**回填。延迟应约等于主从延迟 P99；拍脑袋写 1s 既不能保证删除成功、又延长了不一致窗口，主从延迟抖动大时基本无效。把它当强一致方案是误用。

三个「缓存故障」要对症下药：

- **击穿**：单个热点 key 失效瞬间大量请求回源。用 `SETNX` 互斥重建（只有拿到锁的 goroutine 查 DB 并回填），进程内再用 `singleflight` 合并并发请求；其余请求短暂等待或直接返回旧值。
- **雪崩**：大批 key 同时过期。TTL 加随机抖动、多级缓存、下游限流降级。
- **穿透**：查询根本不存在的数据。缓存空值（短 TTL）+ 布隆过滤器前置拦截（有假阳性、无假阴性），并校验参数合法性。

下面把 singleflight 与逻辑过期组合起来：物理 key 的 TTL 很长，是否新鲜由 value 里的 `expire_at` 判断，过期后先返回旧值、后台只重建一次，避免失效瞬间打穿 DB。

```go
package cache

import (
	"context"
	"encoding/json"
	"errors"
	"time"

	"github.com/redis/go-redis/v9"
	"golang.org/x/sync/singleflight"
)

type entry struct {
	Data     json.RawMessage `json:"data"`
	ExpireAt int64           `json:"expire_at"` // 逻辑过期时间（Unix 秒）
}

type Loader struct {
	rdb  *redis.Client
	sf   singleflight.Group
	load func(ctx context.Context, key string) ([]byte, error)
	ttl  time.Duration // 物理 TTL，恒大于逻辑过期，保证旧值可兜底
}

func (l *Loader) Get(ctx context.Context, key string) ([]byte, error) {
	raw, err := l.rdb.Get(ctx, key).Bytes()
	if errors.Is(err, redis.Nil) {
		return l.rebuild(ctx, key) // 冷启动：同步回源
	}
	if err != nil {
		return nil, err
	}
	var e entry
	if err := json.Unmarshal(raw, &e); err != nil {
		return l.rebuild(ctx, key)
	}
	if time.Now().Unix() < e.ExpireAt {
		return e.Data, nil // 新鲜
	}
	// 逻辑过期：先返回旧值，后台重建（rebuild 内部用 singleflight 去重）
	go func() { _, _ = l.rebuild(context.Background(), key) }()
	return e.Data, nil
}

func (l *Loader) rebuild(ctx context.Context, key string) ([]byte, error) {
	v, err, _ := l.sf.Do(key, func() (interface{}, error) {
		data, err := l.load(ctx, key)
		if err != nil {
			return nil, err
		}
		b, err := json.Marshal(entry{Data: data, ExpireAt: time.Now().Add(l.ttl / 2).Unix()})
		if err != nil {
			return nil, err
		}
		_ = l.rdb.Set(ctx, key, b, l.ttl).Err()
		return data, nil
	})
	if err != nil {
		return nil, err
	}
	return v.([]byte), nil
}
```

> 逻辑过期牺牲了「读到的可能不是最新」，换来稳定延迟。余额、库存这类强一致读不要用，直接读主库或做版本号校验。另注意 `rebuild` 里的 `singleflight.Do` 不可重入：同一 key 的 fn 内不能再对同一 key 调 `Do`，否则永远等自己。

### 4.8 分布式锁的边界：从 SetNX 到 Redlock 争议

4.4 给了最小可用版本，这里补齐边界。一把合格的 Redis 锁要同时满足三件事：

1. **原子加锁**：`SET key <token> NX PX <ttl>` 一条命令完成「不存在才写 + 设过期」。拆成 `SETNX` + `EXPIRE` 两条，中间进程崩溃就是永久死锁。go-redis 的 `SetNX(ctx, key, token, ttl)` 内部就是 `SET ... NX PX`。
2. **过期兜底**：持有者崩溃后锁能自动释放，TTL 必须大于正常执行时间。
3. **value 校验**：value 是每次加锁生成的唯一 token（UUID/随机串），释放时用 Lua 比对后再删；否则自己的锁过期后会把别人的锁删掉。

释放脚本见 4.4。续期（看门狗）在此基础上加一条「仍是自己的锁才 `PEXPIRE`」：

```go
package lock

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"time"

	"github.com/redis/go-redis/v9"
)

var ErrLockLost = errors.New("lock: lost")

// 只有 value 仍属于自己才续期，避免把别人的锁续上。
var renewScript = redis.NewScript(`
if redis.call("get", KEYS[1]) == ARGV[1] then
	return redis.call("pexpire", KEYS[1], ARGV[2])
end
return 0`)

type Lock struct {
	rdb   *redis.Client
	key   string
	token string
	ttl   time.Duration
}

func Acquire(ctx context.Context, rdb *redis.Client, key string, ttl time.Duration) (*Lock, bool, error) {
	b := make([]byte, 16)
	if _, err := rand.Read(b); err != nil {
		return nil, false, err
	}
	token := hex.EncodeToString(b)
	ok, err := rdb.SetNX(ctx, key, token, ttl).Result()
	if err != nil || !ok {
		return nil, false, err
	}
	return &Lock{rdb: rdb, key: key, token: token, ttl: ttl}, true, nil
}

// Renew 返回 ErrLockLost 表示锁已易主，调用方必须立刻停止临界区。
func (l *Lock) Renew(ctx context.Context) error {
	n, err := renewScript.Run(ctx, l.rdb, []string{l.key}, l.token, l.ttl.Milliseconds()).Int()
	if err != nil {
		return err
	}
	if n == 0 {
		return ErrLockLost
	}
	return nil
}
```

> 看门狗不是银弹：续期协程与 Redis 网络分区、进程长时间 STW、机器时钟漂移，都会让「客户端以为自己持锁」与「Redis 认为锁已过期」脱节。续期还掩盖了业务执行过久的问题，持锁时长必须单独监控。

**Redlock 的争议**。antirez 的方案是在 N 个**互相独立**的 Redis 主节点上依次加锁，多数派成功且总耗时小于 TTL 才算成功，以此摆脱单点。Kleppmann 的反驳是：分布式锁的正确性依赖「持锁者在临界区内，别人进不来」，而 Redlock 依赖各节点时钟大致同步、进程不会长时间暂停——GC 停顿或时钟跳变后，锁已过期而客户端浑然不知，两个客户端同时进入临界区。他的替代方案是 **fencing token**：锁服务发放单调递增的 token，下游存储拒绝比已见 token 更小的写，这样即使旧持有者「复活」也写不进去。

实践结论：

- 单 Redis 主从（异步复制）下，主在加锁成功后、复制到从之前宕机，从升主后锁就没了——**Redis 锁不能保证互斥，只能降低冲突概率**。
- 锁有两类用途：**效率型**（避免重复计算，如缓存重建）丢了顶多浪费一次劳动；**正确型**（扣库存、转账）丢了就是资损。Redis 锁只适合前者。
- 正确型互斥优先用数据库唯一约束/行锁、[etcd](../etcd/etcd-guide.md) 的 lease（线性一致、revision 可当 fencing token）。非要用 Redis，就在被保护的资源侧加 fencing token 或幂等校验，别把「加锁成功」当成长久有效的通行证。

---

## 5. 版本演进速览

| 版本 | 关键变化 |
|---|---|
| v6 / v7 | `github.com/go-redis/redis`，无 Context 首参 |
| v8 | 引入 `context.Context`，模块路径 `/v8` |
| **v9**（当前主线） | 仓库迁移到 `github.com/redis/go-redis`，模块路径 `/v9`；`ctx` 成为**强制**首参；`Options` 字段调整、`NewFailoverClient` 统一；Hook 机制稳定 |
| 后续 | 持续适配 Redis 7.x 新命令（Functions、ACL、Sharded Pub/Sub 等）、OpenTelemetry 生态增强 |

**v8 → v9 迁移要点**：改 import 路径、给每个命令补 `ctx`、检查 `redis.Options` 中重命名/废弃字段（如 `IdleTimeout` → `ConnMaxIdleTime`）。v9 是 API 稳定线，锁 `go.mod` 版本、升级前读 CHANGELOG 即可。

---

## 6. 常见坑与最佳实践

1. **每请求 `NewClient`**：连接池泄漏、端口耗尽、延迟飙升。全局单例，进程退出时 `Close()`。
2. **忽略命令返回值**：`rdb.Set(...)` 不接 `.Err()` 等于错误被吞掉；`Pipeline().Exec()` 之后还要逐条 `c.Err()`。
3. **把 `redis.Nil` 当故障**：键不存在是正常分支，必须 `errors.Is(err, redis.Nil)` 单独判断。
4. **误以为 Pipeline 有原子性**：它只是打包传输；需要原子用 `TxPipeline` / Lua，需要「读后写不被打断」用 `Watch` + `TxPipelined`。
5. **缓存三件套**：穿透（空值缓存 / 布隆过滤）、击穿（热点用 singleflight 或互斥重建）、雪崩（TTL 加随机抖动）——三者是不同问题，别只加一个 TTL 就以为解决了。
6. **大 Key 与热 Key**：一个 Hash 塞百万 field、`MGET` 千个键、或把整张表 JSON 塞进一个 String，都会卡住 Redis 单线程。拆片 + `HSCAN` 增量遍历。
7. **生产用 `KEYS *`**：O(N) 阻塞全实例，一律改 `SCAN` 游标遍历。
8. **Cluster 跨 slot**：多键操作报 `CROSSSLOT`，用 `{hash_tag}` 把相关键绑到同一 slot；跨 slot 的 Lua 脚本无法执行。
9. **`MaxRetries` 与幂等**：客户端重试对 `INCR`、`LPUSH` 这类非幂等命令可能造成重复副作用。要么关掉自动重试，要么让命令本身幂等（带唯一 ID / `SET NX`）。
10. **锁的 TTL 与误删**：`SetNX` 必须带唯一 token、用 Lua 比较后再删；直接 `DEL` 会在锁过期后删掉别人的锁。
11. **超时级联**：命令 `ReadTimeout` 小于服务端 P99 时，客户端断连但服务端仍在执行，形成「幽灵写入」。超时应略大于 P99，并让命令幂等。
12. **序列化别用 `gob`**：跨语言/跨版本不可读，缓存值统一用 JSON（或 MessagePack），并预留版本号字段。
13. **连接池监控**：定期看 `rdb.PoolStats()` 的 `Hits/Misses/Timeouts`，`Timeouts` 增长说明 `PoolSize` 不足或有慢命令占着连接。

---

## 7. 选型对比

| 维度 | go-redis v9 | redigo | rueidis | 进程内缓存（Ristretto / bigcache） | Memcached |
|---|---|---|---|---|---|
| 维护状态 | **活跃、事实标准** | 归档维护 | 活跃、新锐 | 活跃 | 稳定但功能少 |
| API 风格 | 类型安全方法 + Context | `Do(cmd, args...)` 原始命令 | 自动生成、客户端缓存 | Go API | 文本/binary 协议 |
| 拓扑 | 单机/Sentinel/Cluster/Ring | 需自己接池与集群 | Cluster 优先 | 无（单进程） | 客户端分片 |
| 性能 | 高 | 中 | **极高**（自动 pipeline + 客户端缓存） | **极高**（无网络） | 高（多线程） |
| 数据结构 | 全 | 全（手写） | 全 | 仅 KV | 仅 KV |
| 持久化 / 共享 | Redis 提供 | 同 | 同 | **无，重启即失** | 无持久化 |
| 适用 | 绝大多数 Go 服务 | 存量老项目 | 极致吞吐、可接受较新生态 | 本地热点、无共享需求 | 纯 KV 大集群 |

一句话决策：

- **新项目默认 go-redis v9**——生态、文档、Hook 与社区答案最全，性能对 99% 的业务足够；
- **只有在压测证明客户端是瓶颈**时才考虑 rueidis（它用客户端缓存与自动 pipeline 换复杂度，语义与 go-redis 有差异）；
- **本机热点、允许各实例数据不一致** → 进程内缓存，并与 Redis 组成「L1/L2」两级，注意失效广播；
- **需要可靠异步任务（重试/死信/延时/优先级）** → 别用 `BLPOP` 自己造轮子，用 [asynq](../asynq/asynq技术文档与使用手册.md)；
- **需要强一致协调（选主、分布式锁）** → [etcd](../etcd/etcd-guide.md)，而不是 Redis 主从。

> 选 Redis 还是「不选 Redis」更值得先问：很多场景加一层缓存只是在掩盖慢查询与缺失索引，反而引入了双写一致性这个长期成本。
