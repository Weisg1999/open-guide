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

```
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
