# 进程内缓存技术介绍与使用指南

> **一句话定位**：Redis 之前的那一层——用进程内内存兜住热点读，把网络往返和 Redis 带宽一起省掉。
>
> **上游仓库**：[dgraph-io/ristretto](https://github.com/dgraph-io/ristretto)、[allegro/bigcache](https://github.com/allegro/bigcache)、[coocood/freecache](https://github.com/coocood/freecache) · **版本基线**：ristretto v0.2、bigcache v3.3、freecache v1.0 · **协议**：MIT / Apache-2.0

---

## 1. 项目简介

「本地缓存」指的是**跑在业务进程里的 KV 存储**——没有网络、没有序列化协议、没有连接池，一次命中就是一次内存读。它和 [Redis](../redis/redis-guide.md) 不是替代关系，而是上下游：Redis 挡住数据库，本地缓存挡住 Redis。

把这一层加进去，省下的是三笔开销：

- **网络往返**：同机房 Redis 一次 RTT 约 0.2–0.5ms，本地缓存是几十纳秒，差三个数量级；
- **Redis 带宽与 CPU**：一个 5 万 QPS 的接口，命中率 90% 就意味着 Redis 少扛 4.5 万次/秒；
- **长尾**：Redis 抖动、主从切换、连接池排队都不会传导到读路径。

### 什么时候值得加

| 信号 | 说明 |
|---|---|
| 读远多于写 | 读写比 10:1 以上，缓存才有收益；写多读少的表加了只是多一份要失效的状态 |
| key 集中、有明显热点 | 头部 1% 的 key 承担 80% 流量时，几 MB 内存就能挡住大部分请求 |
| 能容忍秒级不一致 | 商品详情、配置、用户资料、榜单；余额、库存、权限判定不行 |
| 单实例 QPS 高 | 每实例每秒几千次以上读，网络开销才值得用内存去换 |
| 下游是共享资源 | Redis 集群容量吃紧、或数据库已经出现热点行锁 |

### 什么时候是灾难

- **强一致读**：多实例各自持有一份，A 实例已更新、B 实例还是旧值，用户刷新两次看到两个答案；
- **key 无界**：按 `userId`、`orderId` 做缓存又不设容量上限，内存随流量单调增长，最终 OOM 或拖长 GC；
- **value 很大**：把整张表、整个列表塞进本地缓存，GC 扫描与复制成本全部翻倍，延迟抖动比省下的 RTT 更贵；
- **写后立即读**：写完马上去查自己的数据，命中旧副本，体验上就是「保存失败」。

一句话：**本地缓存是拿一致性换延迟的杠杆，杠杆的另一头必须有人兜底**——兜底通常是短 TTL，不是「我保证每次都能删干净」。

### 安装

```bash
go get github.com/dgraph-io/ristretto   # TinyLFU 准入，功能最全
go get github.com/allegro/bigcache/v3   # 分片 + 免 GC 扫描，适合海量小对象
go get github.com/coocood/freecache     # 单块预分配内存，零 GC 抖动
```

### 最小可运行示例

```go
package main

import (
	"fmt"
	"time"

	"github.com/dgraph-io/ristretto"
)

func main() {
	c, err := ristretto.NewCache(&ristretto.Config{
		NumCounters: 1 << 20, // 计数器的数量，约为预期 key 数的 10 倍
		MaxCost:     1 << 28, // 容量预算，单位由 Cost 决定；这里按字节算 256MB
		BufferItems: 64,      // Set 缓冲区大小，官方推荐 64
	})
	if err != nil {
		panic(err)
	}
	defer c.Close()

	if ok := c.SetWithTTL("user:42", []byte(`{"name":"ann"}`), 16, time.Minute); !ok {
		panic("set dropped")
	}
	c.Wait() // 等异步写入落到 store，否则紧接着 Get 可能读不到

	if v, hit := c.Get("user:42"); hit {
		fmt.Println(string(v.([]byte)))
	}
}
```

---

## 2. 能力总览

三个库解决的是同一个问题的三种取法：**在有限内存里，尽量把该留的留下，同时别让 GC 因为这块内存变慢**。

| | ristretto | bigcache | freecache |
|---|---|---|---|
| 实现原理 | TinyLFU 频率草图 + 准入策略；分片 map + Set 缓冲区，写入异步 | N 个分片（每片带锁）+ 分片内环形 byte 队列 | 一块预分配 `[]byte` 环形缓冲区 + 自管理哈希索引（256 段） |
| 淘汰依据 | 频率估计（比受害者高才准入），近似 LFU | 时间：`LifeWindow` 到期整条清除，无容量淘汰 | 时间 + 容量：段内 LRU 式覆盖，写满即淘汰最旧 |
| GC 影响 | 低：值是 `interface{}`，仍会被扫描；靠对象数少来降成本 | 极低：值存 `[]byte`，队列无指针，GC 不扫描内容 | 极低：大块 `[]byte` 只分配一次，之后零分配 |
| 命中率 | **最高**，抗扫描污染 | 中，热点会被 `LifeWindow` 到点清掉 | 中，近似 LRU，长尾 key 容易被冲掉 |
| 值类型 | 任意 `interface{}`（注意成本要自己算） | 只能 `[]byte` | 只能 `[]byte` |
| TTL 语义 | 精确到条目，`Get` 时过滤过期 | **非滑动**，写入后 `LifeWindow` 到点整条消失 | 秒级，`Set` 时给定，0 表示不过期 |
| 并发模型 | 无锁读 + 分片写 | 分片 `RWMutex` | 分段锁，读写都加锁 |
| 复杂度 | 高（异步语义、`Cost`、`Wait`） | 低 | 低 |
| 协议 | Apache-2.0 | Apache-2.0 | MIT |

选型直觉：**要命中率选 ristretto，要吞吐与稳定 GC 选 bigcache，要延迟可预测选 freecache**。三者都不解决多实例一致性问题，那是第 4 章的事。

---

## 3. 核心概念与原理

### 3.1 淘汰算法：LRU / LFU / TinyLFU

| 算法 | 依据 | 优点 | 典型失效场景 |
|---|---|---|---|
| FIFO | 进入顺序 | 实现最简单 | 热点 key 被周期性清空 |
| LRU | 最近一次访问时间 | 贴合「刚用过还会用」 | **全表扫描/爬虫**把热点全冲掉 |
| LFU | 累计访问次数 | 抗扫描 | 老热点永不让位（缓存污染），计数无法衰减 |
| TinyLFU | 频率估计 + 新条目准入 | 抗扫描且能新陈代谢 | 突发新热点有短暂延迟（需攒够计数） |

LRU 的命门是一次顺序扫描：假设缓存 1 万条，业务顺序遍历 10 万条冷数据，每读一条就挤掉一条热数据，扫完之后缓存里全是只读一次的垃圾，命中率瞬间归零。这不是「缓存太小」，而是**只看近度、不看频度**。

TinyLFU 的做法是给每个 key 记一个近似频率（Count-Min Sketch，4 bit 计数器，定期全体减半以衰减），新条目要进缓存时和「即将被淘汰的受害者」比频率，高才准入：

```text
新 key ──► 频率草图估计 ──► 比受害者高？ ──是──► 准入，淘汰受害者
                                    └──否──► 拒绝（Set 返回 true 但不会命中）
```

所以 ristretto 的 `Set` 返回 `true` 只是「没被缓冲区丢掉」，**不代表条目最终在缓存里**；被策略拒绝是正常行为，不是 bug。真正想看拒绝量，读 `Metrics.SetsRejected()`。

### 3.2 bigcache 为什么快：分片 + `[]byte` + 环形队列

Go 的 GC 是并发的标记-清除，标记阶段要顺着指针走。一个 `map[string]*entry` 里有几百万个对象，就是几百万个待扫描节点；再加上每个 `entry` 单独分配，堆上碎成一地——GC 周期变长、STW 之外的辅助标记还会抢 CPU。

bigcache 把这两个成本一起干掉：

```text
key ──hash──► shard[0..N-1]（每个 shard 一把 RWMutex）
                 │
                 ├─ map[string]uint32  只存「key → 队列偏移」，value 不在这里
                 └─ queue：一串固定大小的 []byte 块，按写入顺序追加
```

- **值不落在 map 里**：map 只保存偏移量，真正的字节按顺序追加进队列；队列本身是 `[]byte`，**内部没有指针**，GC 扫描时直接跳过内容；
- **分片降锁竞争**：默认 1024 片，`Shards` 必须是 2 的幂，否则启动报错；
- **写入是覆盖式**：删掉的条目只是索引失效，空间要等整块队列被回收才释放，所以内存曲线是阶梯状而不是锯齿状。

代价是：值必须是 `[]byte`（自己序列化）、没有容量淘汰、`LifeWindow` 到期是**整条丢弃**而不是滑动续期。

### 3.3 freecache 的零 GC：预分配 + 自管理索引

freecache 常被称作「零 GC 堆外缓存」，严格说它**仍在 Go 堆上**：`NewCache(size)` 一次性 `make` 一块大 `[]byte`，切成 256 段环形缓冲区，之后所有写入都在块内原地覆盖。

- **零分配**：`Set` 不产生新对象，写完就完，GC 没有新增工作量；
- **零指针**：数据区是纯字节，GC 不扫描内容；索引是定长数组 + 每段 256 个槽的有序表，也不持有 value 指针；
- **容量固定**：`size` 必须 ≥ 512KB，写满后覆盖最旧条目；因为空间不增长，延迟曲线非常平。

它的短板也来自这个设计：只能存 `[]byte`、TTL 精度是秒、写放大（覆盖时要挪动环形缓冲区）、以及**分配后不能扩容**——容量估小了只能重建缓存实例。

### 3.4 ristretto 的准入与代价

ristretto 的两阶段写入是它最容易被误用的地方：

1. `SetWithTTL` 把条目推进**环形缓冲区**，立即返回；
2. 后台 goroutine 批量消费缓冲区，走 TinyLFU 准入，通过才落到 store。

所以 `Set` 之后立刻 `Get` 有概率读不到。官方给的解法是 `cache.Wait()`（等缓冲区排空，测试里常用），生产上则应该接受这个语义：**缓存是「尽力而为」，回源路径必须永远正确**。

第二个代价是 `Cost`。`MaxCost` 的单位完全由你定义，最省事的做法是按字节：

```go
package cache

func costOf(v interface{}) int64 {
	b, ok := v.([]byte)
	if !ok {
		return 1
	}
	return int64(len(b)) // 只算 payload；key 与结构体开销由 IgnoreInternalCost 控制
}
```

把 `costOf` 赋给 `Config.Cost` 即可。注意它只在 `Set` 的 `cost` 参数为 0 时才被调用；如果每次 `Set` 都显式传 `len(b)`，这个函数永远不会执行，写了也没用。`Cost` 恒返回 0 是最危险的写法，见第 6 章。

### 3.5 单飞（singleflight）：缓存击穿时只放一个请求回源

本地缓存失效的瞬间，同一个进程里可能有几百个 goroutine 同时发现未命中，然后一起去查数据库——这就是**击穿**。`golang.org/x/sync/singleflight` 按 key 合并并发调用：100 个 goroutine 同时 miss，只有第一个做真实加载，其余等待并共享同一份结果。

三个必须知道的边界：

- **只在本进程内合并**，多实例仍然是 N 次回源，跨实例要靠 [Redis 分布式锁](../redis/redis-guide.md) 或逻辑过期；
- **leader 的 `ctx` 取消会传染**：等待者跟着拿到 `context.Canceled`，长耗时加载建议用 `DoChan` + `select` 让等待者能自行超时；
- **不可重入**：同一个 key 的 `fn` 里再对同一 key 调 `Do` 会永久等自己。

---

## 4. 使用实战

### 4.1 ristretto：初始化、TTL、三值返回值与命中率

```go
package usercache

import (
	"time"

	"github.com/dgraph-io/ristretto"
)

type Cache struct {
	c *ristretto.Cache
}

func New(maxCost int64) (*Cache, error) {
	c, err := ristretto.NewCache(&ristretto.Config{
		NumCounters: 1 << 22, // 约 400 万计数器，对应 40 万 key
		MaxCost:     maxCost, // 字节数
		BufferItems: 64,
		Metrics:     true, // 约 10% 吞吐开销，压测时关掉
		Cost: func(v interface{}) int64 {
			if b, ok := v.([]byte); ok {
				return int64(len(b))
			}
			return 1
		},
	})
	if err != nil {
		return nil, err
	}
	return &Cache{c: c}, nil
}

// 返回 false 表示被 Set 缓冲区丢弃；返回 true 也不保证最终准入。
func (c *Cache) Set(key string, val []byte, ttl time.Duration) bool {
	return c.c.SetWithTTL(key, val, 0, ttl) // cost=0 → 交给 Config.Cost
}

func (c *Cache) Get(key string) ([]byte, bool) {
	v, ok := c.c.Get(key)
	if !ok {
		return nil, false
	}
	b, ok := v.([]byte)
	return b, ok
}

func (c *Cache) Del(key string) { c.c.Del(key) }

// 命中率 = Hits / (Hits + Misses)，Metrics 未开启时恒为 0。
func (c *Cache) HitRatio() float64 { return c.c.Metrics.Ratio() }
```

关于返回值：ristretto 的 `Get` 是 **`(value, ok)` 两个返回值**——不少文章写成三值 `(value, ok, err)`，那是二次封装后的形态；官方不回传 `err`，未命中和类型断言失败都只能靠 `ok` 判断，所以外层一定要做类型断言。

两个容易踩的点：

- `Metrics.Ratio()` 只统计 `Get` 路径，`GetsDropped()` 增长说明读缓冲满了，此时**未命中数被低估**，命中率会虚高；
- `SetWithTTL(k, v, cost, 0)` 等于永不过期，负 TTL 直接丢弃且不报错；`Close()` 之后再 `Get` 不 panic，但永远未命中。

### 4.2 bigcache：`LifeWindow` / `CleanWindow` / `Shards` 怎么定

```go
package store

import (
	"context"
	"errors"
	"time"

	"github.com/allegro/bigcache/v3"
)

var ErrMiss = errors.New("store: miss")

type Store struct{ c *bigcache.BigCache }

func New(ttl time.Duration) (*Store, error) {
	c, err := bigcache.New(context.Background(), bigcache.Config{
		Shards:             1024,           // 2 的幂；写并发高再加，但每片内存开销也涨
		LifeWindow:         ttl,            // 条目存活时间，从写入时刻算，不滑动
		CleanWindow:        ttl / 4,        // 清理周期；0 表示后台不清理，靠覆盖复用
		MaxEntriesInWindow: 60 * 1000 * 10, // 预估窗口内条目数，用于初始分配
		MaxEntrySize:       512,            // 单条初始容量上限，仅影响预分配
		HardMaxCacheSize:   1024,           // 硬上限（MB），0 为不限制
		StatsEnabled:       true,
	})
	if err != nil {
		return nil, err
	}
	return &Store{c: c}, nil
}

func (s *Store) Set(key string, val []byte) error { return s.c.Set(key, val) }

func (s *Store) Get(key string) ([]byte, error) {
	b, err := s.c.Get(key)
	if errors.Is(err, bigcache.ErrEntryNotFound) {
		return nil, ErrMiss
	}
	return b, err
}
```

参数怎么拍：

| 参数 | 定法 | 定错的后果 |
|---|---|---|
| `Shards` | 写并发 ÷ 每片可承受的锁竞争，从 1024 起 | 过小锁冲突、过大内存碎片 |
| `LifeWindow` | 业务可容忍的最长不一致时间，通常 30s–10min | 过大读到过期数据，过小命中率低 |
| `CleanWindow` | `LifeWindow` 的 1/4–1/2；0 关闭主动清理 | 设为 0 后过期条目仍占内存，直到被覆盖 |
| `HardMaxCacheSize` | 按容器内存上限的 10%–20% | 不设 = 无上限，OOM 时最先被 kill |
| `MaxEntriesInWindow` | 真实窗口条目数的 1.2 倍 | 估小会频繁扩容队列，产生额外分配 |

### 4.3 二级缓存：本地 + Redis 的读路径与失效顺序

读路径固定为 L1 → L2 → 回源，回填顺序相反：

```text
读：L1 命中 → 返回；未命中 → L2 命中 → 回填 L1(短 TTL) → 返回
    L2 也未命中 → singleflight 回源 → 写 L2 → 写 L1
```

L1 的 TTL 一定要**远短于** L2（典型 2–10s vs 5–30min），它不是用来保证正确的，只是给广播失效兜底。

写路径的顺序是这一节唯一需要背下来的结论：

```text
更新 DB ──► 删除 L2(Redis) ──► 广播删除各实例 L1
```

为什么不是先删 L1：

- 先删 L1、再删 L2 的间隙里，别的实例可能 L1 未命中 → 读到**尚未删除的旧 L2** → 回填进自己的 L1，脏数据反而多活了一轮；
- 先删 L2 之后，任何没收到广播的实例即使 L1 还是旧值，最多撑到 L1 TTL 到期，重新加载时读到的已经是干净数据。

两个补充：

1. **删而不是更新**：更新缓存要求写入方完整重建对象，并发写时「写库顺序」与「写缓存顺序」可能相反，缓存会永久留下旧值；删除是幂等的。
2. **删 L2 失败要重试**：把删除任务投进带退避的本地队列或消息队列；删除失败 + 广播失败 + TTL 很长，就是长期脏读。

### 4.4 多实例一致性：广播失效与短 TTL 兜底

单机多副本、K8s 多 Pod，各自删各自的 L1 是删不干净的，必须广播。最省事的是复用 Redis Pub/Sub：

```go
package twolayer

import (
	"context"
	"strings"

	"github.com/dgraph-io/ristretto"
	"github.com/redis/go-redis/v9"
)

const invalidateChannel = "cache:invalidate"

type TwoLevel struct {
	l1  *ristretto.Cache // L1：本进程
	rdb *redis.Client    // L2：共享 Redis
}

// 发布端：写库成功后调用。key 之间用逗号分隔，单条消息别塞太多 key。
func PublishInvalidate(ctx context.Context, rdb *redis.Client, keys ...string) error {
	return rdb.Publish(ctx, invalidateChannel, strings.Join(keys, ",")).Err()
}

// 订阅端：每个实例起一个 goroutine，收到就删本地。
func (t *TwoLevel) WatchInvalidation(ctx context.Context) error {
	sub := t.rdb.Subscribe(ctx, invalidateChannel)
	defer sub.Close()

	for {
		msg, err := sub.ReceiveMessage(ctx)
		if err != nil {
			return err // ctx 取消时返回 context.Canceled，由调用方决定是否重启
		}
		for _, k := range strings.Split(msg.Payload, ",") {
			t.l1.Del(k)
		}
	}
}
```

| 方案 | 延迟 | 可靠性 | 代价 |
|---|---|---|---|
| Redis Pub/Sub | 毫秒级 | **至多一次**，断连期间的消息永久丢失 | 便宜，但必须配短 TTL |
| NATS / Kafka | 毫秒到秒级 | 至少一次，可回放 | 多一个中间件要运维 |
| 短 TTL 轮询 | 等于 TTL | 最终一致 | 零成本，但 TTL 内必然有脏读 |
| 版本号校验 | 无额外延迟 | 读时比对版本，最严格 | 每次读多一次版本查询，等于回到 Redis |

实践组合：**Pub/Sub 广播 + L1 TTL 2–5s + value 里带 `version`**。广播负责把窗口压到毫秒，TTL 负责在消息丢失、实例刚扩容、网络分区时兜底。别指望 Pub/Sub 做到「一定送达」——它连自己的订阅关系都不持久化。

### 4.5 单飞 + 本地缓存的完整封装

把前面几块拼成一个可以直接抄的 `Loader`：本地缓存挡在前面，未命中走 singleflight，回源结果按字节计费写入。

```go
package loader

import (
	"context"
	"errors"
	"time"

	"github.com/dgraph-io/ristretto"
	"golang.org/x/sync/singleflight"
)

var ErrNotFound = errors.New("loader: not found")

type Loader struct {
	local *ristretto.Cache
	sf    singleflight.Group
	load  func(ctx context.Context, key string) ([]byte, error)
	ttl   time.Duration
}

func New(load func(context.Context, string) ([]byte, error), ttl time.Duration, maxCost int64) (*Loader, error) {
	c, err := ristretto.NewCache(&ristretto.Config{
		NumCounters: 1 << 22,
		MaxCost:     maxCost,
		BufferItems: 64,
		Cost: func(v interface{}) int64 {
			if b, ok := v.([]byte); ok {
				return int64(len(b))
			}
			return 1
		},
	})
	if err != nil {
		return nil, err
	}
	return &Loader{local: c, load: load, ttl: ttl}, nil
}

func (l *Loader) Get(ctx context.Context, key string) ([]byte, error) {
	if v, ok := l.local.Get(key); ok {
		if b, ok := v.([]byte); ok {
			return b, nil
		}
	}

	// 同一个 key 的并发未命中只放一个 goroutine 回源，其余共享结果。
	v, err, _ := l.sf.Do(key, func() (interface{}, error) {
		// 等锁期间可能已被别的请求填好，二次检查能省一次回源。
		if v, ok := l.local.Get(key); ok {
			return v, nil
		}
		b, err := l.load(ctx, key)
		if err != nil {
			return nil, err
		}
		// cost 传 0 让 Config.Cost 算；写入是异步的，本次仍直接返回 b。
		l.local.SetWithTTL(key, b, 0, l.ttl)
		return b, nil
	})
	if err != nil {
		return nil, err
	}
	b, ok := v.([]byte)
	if !ok {
		return nil, ErrNotFound
	}
	return b, nil
}

func (l *Loader) Invalidate(key string) { l.local.Del(key) }
```

用之前先明确三条语义：

- **leader 的 `ctx` 决定所有人**：回源超时要设在 `load` 内部，而不是靠调用方的 `ctx`；否则一个客户端断开，会连带取消几十个等待者；
- **`load` 返回错误不缓存**：空值穿透要靠返回 `[]byte{}` 或单独的空值标记，不要返回 `nil, nil`；
- **异步写入意味着下一次可能仍未命中**：不要用「连续两次 `Get` 都应该命中」做正确性断言，测试里加 `Wait()`，或者接受这一语义。

---

## 5. 版本与生态

| 库 | 当前基线 | 状态 |
|---|---|---|
| ristretto | v0.2 | 仍是 v0 主版本，API 稳定但**没有 v1 承诺**；泛型化 / v2 模块一直在讨论中，是否落地、何时落地都不确定，升级前看 release notes |
| bigcache | v3.3 | 功能基本冻结，维护节奏慢（issue 响应以月计），但代码简单、行为稳定，生产可用 |
| freecache | v1.0 | 长期低维护，接口极小，适合「写完就不动」的场景 |

**Go 1.24 的 `weak` 包**值得一提。`weak.Pointer[T]` 提供不阻止 GC 回收的弱引用（常与 `runtime.AddCleanup` 搭配），可以让「没人再用的 value」自动从索引里消失。它解决的是「缓存留着对象导致回收不掉」，**不适合**替代带 TTL 与容量控制的缓存：何时清掉完全由 GC 决定，命中率不可预测，也没有 `MaxCost`。目前社区实践很少，生产上建议仍用成熟库。

---

## 6. 常见坑

1. **TTL 设 0 当「永不过期」**：ristretto 的 `SetWithTTL(..., 0)` 与 bigcache 的 `LifeWindow: 0` 都表示「永不按时间过期」——前者仍受 `MaxCost` 约束，后者连容量淘汰都没有，配上无上限的 key 空间，内存会缓慢爬升到 OOM；而且因为增长慢，压测很难复现。
2. **`Cost` 恒返回 0**：`MaxCost` 永远用不满，TinyLFU 认为所有条目免费，实际进程内存由真实对象决定——等于关掉了容量保护。
3. **bigcache 的 `LifeWindow` 是「整条到期」而不是滑动过期**：每次 `Set` 重置寿命，但读不会续期；`CleanWindow` 一到，整片过期条目一起消失，表现为周期性命中率掉坑，需要在 TTL 上加随机抖动。
4. **多实例各自为政**：没有广播、TTL 又设了 10 分钟，用户在 A 实例改了昵称，刷新落到 B 实例仍是旧名。这类问题通常报成「偶现」，排查成本极高。
5. **本地缓存放了大对象**：value 是几十 KB 的 JSON 时，每次回填都是一次堆分配 + 复制，GC 周期被拉长，P99 抖动比不用缓存更差。大对象留在 Redis，本地只放小字段或指针。
6. **命中率算错**：把 `Get` 未命中当命中（忘记判 `ok`）、把 `Set` 成功计入命中、或忽略 `GetsDropped()`，都会得到一条漂亮但错误的曲线。命中率应只用 `Metrics.Hits()` / `Metrics.Misses()` 计算。
7. **`Set` 之后立刻 `Get` 断言命中**：ristretto 的写入是异步的，单测里不加 `Wait()` 会随机失败；生产代码不能依赖「写完马上可读」。
8. **误以为 freecache 是堆外内存**：它在 Go 堆上分配，只是内容不含指针、GC 不扫描；`GODEBUG=gctrace=1` 里 heap 依然包含这块，容量规划要算进容器 limit。

---

## 7. 选型对比

| 维度 | ristretto | bigcache | freecache | `sync.Map` + TTL |
|---|---|---|---|---|
| GC 影响 | 中低（值被扫描） | 极低（无指针字节队列） | 极低（单块预分配） | **高**（每 key 一个对象，全量扫描） |
| 命中率 | **最高**（TinyLFU 抗扫描） | 中（时间淘汰） | 中（近似 LRU） | 取决实现，通常最差 |
| 容量控制 | `MaxCost` + `Cost` 精确 | `HardMaxCacheSize` 粗粒度 | 固定 `size`，不可扩容 | 无，需自己实现 |
| 淘汰 | 频率准入 + 近似 LFU | 仅时间 | 时间 + 覆盖 | 自己写 |
| 复杂度 | 高（异步语义） | 低 | 低 | **最低**（但容易写错） |
| 值类型 | 任意 | `[]byte` | `[]byte` | 任意 |
| 适合 | 读多写少、key 有热点、要命中率 | key 数量巨大、value 是字节、要稳 GC | 延迟敏感、容量可预估 | 几百条配置、单测、原型 |

决策顺序：

- **先问要不要**：QPS 没到几千、或读写比接近 1:1，直接读 Redis，别引入第二份状态；
- **热点集中、要命中率** → ristretto，用 `MaxCost` 按字节管住内存，配 `Metrics` 看真实命中率；
- **key 上千万、value 是序列化字节、GC 是主要矛盾** → bigcache，接受「到期整条消失」的语义；
- **P99 抖动是红线、容量能估准** → freecache，用固定内存换平稳延迟；
- **条目只有几百条、进程生命周期短** → `sync.Map` + 一个后台清理 goroutine 就够，引入第三方库反而多一层语义要理解；
- **需要跨实例一致** → 无论选哪个，都要补 4.4 的广播失效 + 短 TTL，库本身不提供。

> 本地缓存的收益是线性的（省 RTT），成本却是非线性的（一致性、内存、GC 三本账）。上线前先把「不一致窗口能容忍多久」写成数字，再决定 TTL——这个数字比选哪个库重要得多。
