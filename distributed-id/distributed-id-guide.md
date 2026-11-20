# 分布式 ID 生成技术指南

> **一句话定位**：在多个进程同时写库时，生成全局唯一、趋势递增、不泄露业务量的 ID。
>
> **涉及方案**：雪花算法、号段模式、UUIDv7、数据库自增
>
> **上游仓库**：[google/uuid](https://github.com/google/uuid) · [sony/sonyflake](https://github.com/sony/sonyflake) · **版本基线**：google/uuid v1.6、sony/sonyflake v2 · **协议**：BSD-3-Clause / MIT

---

## 1. 项目简介

单库单表时，`AUTO_INCREMENT` 是完美的：唯一、递增、8 字节、零额外依赖。一旦拆成多个库、多个进程，它的三个前提同时失效：

- **唯一性失效**：两个库各自从 1 开始自增，`id=1001` 会出现两次；
- **有序性失效**：跨库的自增序列互相穿插，全局看是乱序的；
- **性能瓶颈**：所有写都要排同一个全局序列，等于把并发压回串行。

分布式 ID 就是解决这三个问题的一层"基础设施"。它不产生业务价值，但一旦出错就是**主键冲突、数据错位、对账失败**这类不可修复的事故，所以值得单独设计。

### ID 的五个属性

| 属性 | 含义 | 冲突点 |
|---|---|---|
| 唯一 | 任何时刻、任何节点都不会重复 | 与"无中心"冲突 |
| 趋势递增 | 新 ID 大体比旧 ID 大，利于 B+ 树顺序写 | 与"无中心"冲突 |
| 不可预测 | 外部无法从 ID 推出下一个或业务量 | 与"递增"冲突 |
| 可读 | 人能看出时间、分片等信息 | 与"短"冲突 |
| 性能 | 单节点 10 万+/s，延迟微秒级 | 与"强一致"冲突 |

**没有任何一种方案能同时满足这五条。** 自增牺牲无中心，雪花牺牲绝对不可预测，UUID 牺牲可读与索引友好，号段牺牲严格递增。选型的第一步不是挑方案，而是明确这篇业务**必须保住哪三条**——同一个系统里，内部主键、对外单号、幂等键的诉求往往完全不同，混成一套是常见的过度设计。

---

## 2. 能力总览

| 方案 | 唯一性保证 | 有序性 | 性能 | 依赖 | ID 长度 |
|---|---|---|---|---|---|
| 数据库自增 | 单表单库内绝对唯一 | 严格递增 | 受单库写入上限约束（千~万级/s） | MySQL/PG | 8 字节 |
| 自增 + 步长 | `auto_increment_increment=n`，按实例错开 | 全局趋势递增 | 同单库，横向扩容即提升 | 需运维规划步长 | 8 字节 |
| UUIDv4 | 122 位随机，碰撞概率可忽略 | 完全无序 | 本地生成，无网络开销 | 无 | 16 字节（文本 36） |
| UUIDv7 | 48 位毫秒 + 74 位随机 | 按时间趋势递增 | 本地生成 | 无 | 16 字节（文本 36） |
| 雪花 | 时间 + 机器号 + 序列 | 单机严格，全局趋势 | 单机 400 万/s 理论值，实测 100 万+/s | 时钟 + 机器号 | 8 字节（int64） |
| 号段模式 | DB 行锁保证段不重叠 | 段内递增，跨实例趋势 | 极高，DB 压力降到 1/step | MySQL + 双 buffer | 8 字节 |
| Redis INCR | 单 key 原子递增 | 严格递增 | 10 万+/s（单分片） | Redis 持久化策略 | 8 字节 |

两条容易忽略的读法：

- **"性能"一列要看瓶颈在哪**：UUID 是纯本地计算，雪花是本地计算 + 时钟，号段与 Redis 是"每 N 次一次网络"，自增是"每次一次网络 + 一次 fsync"。
- **"ID 长度"决定索引体积**：16 字节 UUID 做主键，二级索引每条都要多存 16 字节；一张 1 亿行的表，二级索引会凭空多出 1.6GB。**"依赖"则决定故障面**：UUID 与雪花不依赖任何服务，进程活着就能发号；号段依赖 DB，Redis 依赖缓存。

---

## 3. 核心概念与原理

### 3.1 雪花的位布局

雪花把 64 位有符号整数切成四段，本质是**在一个整数里编码"什么时候、哪台机器、第几个"**：

```text
 0 | 41 bit 毫秒时间戳 | 10 bit 机器号 | 12 bit 序列号
 ^   ^                   ^              ^
 符号位恒 0              0 ~ 1023       0 ~ 4095
```

| 段 | 宽度 | 溢出边界 | 推导 |
|---|---|---|---|
| 符号位 | 1 | 恒为 0，保证 int64 非负 | — |
| 时间戳 | 41 | **69.7 年** | 2^41 ms ÷ 1000 ÷ 86400 ÷ 365 ≈ 69.7 |
| 机器号 | 10 | **1024 台** | 2^10 |
| 序列号 | 12 | **4096 个/毫秒/台** | 2^12 |

三个数字决定了容量上限：**69 年**是从自定义 epoch 起算的——选项目启动那年（如 2024-01-01），不要用 1970，否则白扔 54 年，且 epoch 一旦上线就不能改，改了全表 ID 冲突；**4096/ms** 换算成单机 **409 万/s** 的理论值，实际受 `sync.Mutex` 与 `time.Now()` 限制在 **100 万/s** 量级，对任何业务都过剩。

编码只有一行：`id = (now_ms - epoch) << 22 | machine_id << 12 | sequence`（左移 22 = 10 + 12）。反解同样直接：`id >> 22` 拿时间、`id >> 12 & 0x3FF` 拿机器号。

### 3.2 时钟回拨

雪花的唯一性建立在"时间戳单调不减"上。但服务器时钟会被 NTP 校正、虚拟机会被热迁移、容器可能被调度到另一台宿主机——**时间往回跳是常态，不是异常**。回拨 N 毫秒，就意味着这 N 毫秒内的序列号会被重新发一遍。

三种处理方式的真实代价：

| 策略 | 做法 | 代价 | 能否上线 |
|---|---|---|---|
| 等待 | 回拨 < N ms 就 `sleep` 追上再发 | 写路径被阻塞 N 毫秒 | 小回拨（< 5ms）可以 |
| 抛错 | 直接返回错误，上层重试或降级 | 这段时间不可用 | 大回拨必须这样，且要告警 |
| 借位 | 序列号位借给时间用，或用逻辑时钟继续自增 | 时间戳不再真实，透支未来 | 能上线，但透支上限必须可控 |

**能真正上线的是组合拳，不是单选**：系统层用 `chrony` 平滑校时（默认 slew 而不是 step，时钟是"追"上去而非"跳"过去），而不是 `ntpd -g` 那种一次性大跳；代码层回拨 ≤ 5ms 时自旋等待，超过就 **fail-fast 抛错**；抛错后由上层重试（写库失败重试是业务本来就该有的能力），同时打点告警。

"借位"（百度 UidGenerator 的 RingBuffer 思路）确实能保证不重复且不阻塞，但它的前提是"回拨窗口有限、且未来时间够用"。一旦时钟被往回拨了几秒而你借了几秒的未来，接下来这几秒的 ID 会全部落在"未来"，重启后从真实时间重新发号就会撞车。**只有在你完全无法接受写失败时才用它，并且要限制最大透支窗口。**

### 3.3 机器号分配

10 位机器号是雪花唯一的"配置项"，也是最容易出事的地方。三种做法：

| 方式 | 做法 | 风险 |
|---|---|---|
| 手写配置 | 启动参数 / 环境变量写死 | 容器化后必然重复：扩容、滚动发布都会复制同一份配置 |
| 注册中心 | etcd/ZooKeeper 注册临时节点，抢 0~1023 的空位 | 依赖注册中心可用；进程假死时租约未过期会占位 |
| IP 哈希取模 | `crc32(ip) % 1024` | 冲突概率随规模平方增长，且重启换 IP 就换号 |

IP 哈希的冲突概率按生日问题计算：`P ≈ 1 - e^(-k(k-1)/2M)`。M=1024 时空位看似充足，实际**20 个 Pod 就有 8.9% 的概率撞上一对，50 个到 70%，100 个是 99.2%**。所以它只适合几十个节点以内、且能容忍极小概率冲突的场景。Kubernetes 里更实用的两个技巧：

- **StatefulSet 的序号就是机器号**：`order-0` ~ `order-2` 的 ordinal 稳定不变，直接取 `$(hostname##*-)`，零依赖；
- **etcd 注册**：无状态 Deployment 用它，租约绑定，进程退出自动释放号位（见 4.4）。

### 3.4 号段模式

号段模式把"每次取一个 ID"改成"每次取一段 ID"：

```text
应用启动 → UPDATE id_segment SET max_id = max_id + 1000 WHERE biz_tag='order' → 读回新上界
发号     → 本地持有 [1001..2000]，纯内存自增、零 IO；用尽再取下一段
```

DB 压力从"每个 ID 一次写"降到"每 1000 个 ID 一次写"，**降低三个数量级**。代价是 ID 不再严格递增：两个实例各自持有不同段，A 的 2000 可能比 B 的 1500 晚生成，但全局看仍在同一量级。

**双 buffer 是它能不能扛住抖动的关键**：内存里同时持有 `[1001..2000]` 与预留的 `[2001..3000]`，当前段剩余 20% 时后台就去 DB 取下一段，用尽时原子切换指针，发号不中断。没有它时，段用尽那一刻所有请求都要等一次 DB 往返（几毫秒），表现为**周期性的 P99 毛刺**——每 1000 个请求抖一次，压测时非常明显。

号段的两个隐含成本：**重启丢弃**（内存里没用完的段直接作废，频繁发布的环境要调小 step）和**依赖 DB 可用性**（DB 挂了只能靠本地剩余号段撑住，撑完就停）。

### 3.5 UUIDv7

RFC 9562 在 2024 年 5 月正式定义了 UUIDv7，布局是"时间在前、随机在后"：

```text
 48 bit unix_ts_ms | 4 bit ver=7 | 12 bit rand_a | 2 bit var=0b10 | 62 bit rand_b
```

对比 UUIDv4（122 位纯随机），差别全在**索引行为**上：

| 维度 | UUIDv4 | UUIDv7 |
|---|---|---|
| 生成顺序 | 完全随机 | 按毫秒递增 |
| B+ 树插入 | 随机落在任意页 → 页分裂、缓冲池命中率低 | 追加到最右页 → 顺序写 |
| 可读性 | 无 | 前 48 位可解出时间 |
| 信息泄露 | 无 | 泄露"创建时间"（毫秒级） |

InnoDB 的聚簇索引按主键物理排序。用 v4 做主键，每次插入都相当于在书的随机一页插入内容，页分裂导致**写放大 2~3 倍、索引碎片率飙升**；用 v7，插入集中在最右页，行为和自增几乎一致。所以"要用 UUID 做主键"这句话，在今天应该理解成"**要用 UUIDv7**"。存储上还有两个细节：**必须用 `BINARY(16)` 而不是 `CHAR(36)`**（36 字节的字符串主键会让所有二级索引跟着膨胀）；MySQL 里 `UUID_TO_BIN(uuid, 1)` 的 swap 参数是给 v1 用的，v7 本身就是有序的，直接 `UUID_TO_BIN(uuid)` 即可。

### 3.6 趋势递增 vs 严格递增

这两个词经常被混用，但它们的实现成本差了几个数量级：

- **严格递增**：任意两个 ID，先生成的必定更小。这要求全局单点或每个 ID 一次共识（Raft/Paxos），吞吐直接掉到千级，没有分布式系统会这么做。
- **趋势递增**：同一节点内单调，跨节点按时间大致有序。允许"后生成的 ID 略小"，只要偏差在毫秒级。

绝大多数业务只需要趋势递增，因为它的收益（索引局部性、按 ID 排序≈按时间排序）已经拿到了，而代价为零。真正需要严格递增的只有两类：**对账文件按 ID 排序后要求无空洞**、**增量同步用 ID 做水位线**。

趋势递增下水位的坑：用 `WHERE id > last_id` 做增量拉取，如果另一台机器的时钟慢了 3ms，它生成的 ID 可能小于 `last_id` 但写入更晚——**这条数据会被永久跳过**。正确做法是水位线回退一个安全窗口：

```sql
SELECT * FROM orders
WHERE id > ? AND created_at <= NOW() - INTERVAL 2 SECOND
ORDER BY id LIMIT 1000;
```

用时间做二次约束，宁可重复拉一小段（下游幂等），也不要漏。

---

## 4. 使用实战

### 4.1 手写一个雪花实现

完整可编译版本，包含回拨处理、`sync.Mutex` 保护与可注入时钟（为了测试回拨）：

```go
package snowflake

import (
	"errors"
	"fmt"
	"sync"
	"time"
)

const (
	epoch        int64 = 1704067200000 // 2024-01-01T00:00:00Z，毫秒
	machineBits        = 10
	sequenceBits       = 12
	maxMachine   int64 = 1<<machineBits - 1
	maxSequence  int64 = 1<<sequenceBits - 1
	machineShift       = sequenceBits
	timeShift          = sequenceBits + machineBits
	maxDriftMs   int64 = 5
)

var (
	ErrClockBackwards = errors.New("snowflake: 时钟回拨")
	ErrMachineID      = errors.New("snowflake: 机器号越界")
)

type Node struct {
	mu       sync.Mutex
	machine  int64
	lastMs   int64
	sequence int64
	now      func() int64
}

func NewNode(machineID int64) (*Node, error) {
	if machineID < 0 || machineID > maxMachine {
		return nil, ErrMachineID
	}
	return &Node{machine: machineID, now: func() int64 { return time.Now().UnixMilli() }}, nil
}

func (n *Node) Next() (int64, error) {
	n.mu.Lock()
	defer n.mu.Unlock()

	now := n.now()
	if now < n.lastMs {
		drift := n.lastMs - now
		if drift > maxDriftMs {
			return 0, fmt.Errorf("%w: 回拨 %dms", ErrClockBackwards, drift)
		}
		time.Sleep(time.Duration(drift) * time.Millisecond)
		now = n.now()
	}

	if now == n.lastMs {
		n.sequence = (n.sequence + 1) & maxSequence
		if n.sequence == 0 { // 本毫秒 4096 个用尽，自旋到下一毫秒
			for now <= n.lastMs {
				now = n.now()
			}
			n.lastMs = now
		}
	} else { // 新的一毫秒，序列归零
		n.sequence = 0
		n.lastMs = now
	}

	return (now-epoch)<<timeShift | n.machine<<machineShift | n.sequence, nil
}
```

三个实现细节值得单独说：

- **锁必须覆盖"读时间 + 改序列"整个过程**，只锁序列号没有意义；
- **自旋等待下一毫秒时不要放锁**，否则同一毫秒会有多个 goroutine 同时判定"序列用尽"；
- **`now` 是函数字段**，线上用 `time.Now`，测试里替换成假时钟，回拨逻辑才可测（见 4.6）。

### 4.2 现成实现：sonyflake 与 google/uuid v7

```go
import (
	"fmt"
	"time"

	"github.com/google/uuid"
	"github.com/sony/sonyflake/v2"
)

// sonyflake：39 位时间（10ms 单位，约 174 年）+ 8 位序列 + 16 位机器号
func newSonyflake() (*sonyflake.Sonyflake, error) {
	return sonyflake.New(sonyflake.Settings{
		MachineID: func() (int, error) { return 3, nil }, // 生产从 etcd/环境变量取
		StartTime: time.Date(2024, 1, 1, 0, 0, 0, 0, time.UTC),
	})
}

func demo() {
	sf, _ := newSonyflake()
	id, _ := sf.NextID() // 默认每 10ms 一个周期，单周期 256 个
	u, _ := uuid.NewV7() // v1.6.0 起提供
	fmt.Println(id, u.String(), u.Time())
}
```

sonyflake 与 Twitter 雪花的差异值得知道：它用 **10ms 为一个时间片**（而不是 1ms）换更长的寿命，序列位只有 8 位（每 10ms 256 个，即 2.56 万/s），机器号 16 位。**低 QPS 场景下它的 ID 低位更"干净"，高 QPS 场景下它的上限远低于标准雪花**——选之前先算峰值。`uuid.NewV7()` 则内部维护了一个单调计数器：同一毫秒内多次调用时递增 `rand_a`，保证**同一进程内严格有序**；跨进程仍可能乱序，但毫秒内的偏差对索引没有影响。

### 4.3 号段模式：表设计与双 buffer

```sql
CREATE TABLE id_segment (
  biz_tag    VARCHAR(64) NOT NULL PRIMARY KEY,
  max_id     BIGINT      NOT NULL DEFAULT 0,
  step       INT         NOT NULL DEFAULT 1000,
  updated_at TIMESTAMP   NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE = InnoDB;

INSERT INTO id_segment (biz_tag, max_id, step) VALUES ('order', 0, 1000);
```

取号必须在一个事务里"读取 + 推进"。MySQL 没有 `UPDATE ... RETURNING`，标准写法是 `SELECT ... FOR UPDATE` 锁行再更新；如果在意往返次数，可以用 `UPDATE ... SET max_id = LAST_INSERT_ID(max_id + step)` 再读 `LAST_INSERT_ID()` 的技巧，一次往返拿到新上界（注意它绑定连接，必须复用同一连接）。

```sql
BEGIN;
SELECT max_id FROM id_segment WHERE biz_tag = 'order' FOR UPDATE;
UPDATE id_segment SET max_id = max_id + 1000 WHERE biz_tag = 'order';
COMMIT;
```

Go 侧的双 buffer（`step` 与 `fetch` 在初始化时注入，首次取号时懒加载第一段）：

```go
type span struct{ cur, end int64 }

type Segment struct {
	mu      sync.Mutex
	cur     *span
	next    *span
	loading bool
	step    int64
	fetch   func(ctx context.Context, step int64) (start, end int64, err error)
}

func (s *Segment) Next(ctx context.Context) (int64, error) {
	s.mu.Lock()
	if s.cur == nil || s.cur.cur >= s.cur.end {
		if s.next == nil { // 预取没赶上，同步兜底
			s.mu.Unlock()
			start, end, err := s.fetch(ctx, s.step)
			if err != nil {
				return 0, err
			}
			s.mu.Lock()
			s.next = &span{cur: start, end: end}
		}
		s.cur, s.next = s.next, nil
	}
	id := s.cur.cur
	s.cur.cur++

	// 剩余不足 20% 时异步预取，把 DB 往返挪出发号路径
	prefetch := s.next == nil && !s.loading && (s.cur.end-s.cur.cur)*5 <= s.step
	if prefetch {
		s.loading = true
	}
	s.mu.Unlock()

	if prefetch {
		go func() {
			start, end, err := s.fetch(context.Background(), s.step)
			s.mu.Lock()
			defer s.mu.Unlock()
			s.loading = false
			if err == nil {
				s.next = &span{cur: start, end: end}
			}
		}()
	}
	return id, nil
}
```

`step` 的取值是吞吐与浪费的权衡：step=1000 时 DB 写 QPS 是发号 QPS 的 1/1000；但如果每天发布 50 次、每次丢掉剩余号段，日均浪费最多 5 万个号，对 BIGINT 无影响，**只影响"ID 连续"这种业务假设**。

### 4.4 机器号用 etcd 注册

思路：在 `0 ~ 1023` 里找一个空的键，用事务的 `CreateRevision == 0` 抢占，键绑定租约，进程退出后自动释放。

```go
func allocMachineID(ctx context.Context, cli *clientv3.Client, prefix string, slots int) (int64, clientv3.LeaseID, error) {
	lease, err := cli.Grant(ctx, 15) // 15s 租约，后台 KeepAlive 续
	if err != nil {
		return 0, 0, err
	}
	for i := 0; i < slots; i++ {
		key := fmt.Sprintf("%s/%d", prefix, i)
		resp, err := cli.Txn(ctx).
			If(clientv3.Compare(clientv3.CreateRevision(key), "=", 0)).
			Then(clientv3.OpPut(key, "1", clientv3.WithLease(lease.ID))).
			Commit()
		if err != nil {
			return 0, 0, err
		}
		if resp.Succeeded {
			return int64(i), lease.ID, nil
		}
	}
	return 0, 0, errors.New("etcd: 机器号已用尽")
}
```

拿号之后起一个 goroutine 消费 `cli.KeepAlive(ctx, lease.ID)` 的通道；如果租约丢失（网络分区超过 15s），**进程必须停止发号并退出**，否则它持有的机器号可能已被别的进程抢走——这时"活着但发错号"比"挂掉"危险得多。这就是 `../etcd/` 里 Lease 语义的直接应用。

### 4.5 分库分表下的基因法

用户表和订单表按 `uid % 8` 分 8 个库时，订单号如果只是纯雪花，按 `order_id` 查询就无法定位分片（得扫 8 个库）。**基因法把分片键"编进"ID**：

```go
// 取 uid 的低 3 位作为基因，拼在雪花 ID 的最低位。
// 结果：order_id % 8 == uid % 8，按任意一个都能算出同一个分片。
func orderID(snow int64, uid int64) int64 {
	gene := uid & 0x7
	return snow<<3 | gene
}

func shardOf(id int64) int64 { return id & 0x7 }
```

代价要写清楚：**每个毫秒的序列容量被除以 8**（4096 → 512/ms/台），且 ID 的低位不再连续。对订单这种量级完全够用；如果分片数取 1024，就必须重新评估雪花的位分配。若还要求"订单号能看出日期"，可在最前面拼 8 位日期（`20261120` + 19 位雪花 + 1 位基因），此时订单号是 `string`、列用 `VARCHAR(32)` 并建唯一索引——**对外可读性与内部存储效率，只能选一个**。

### 4.6 测试：并发生成 100 万个查重 + 回拨模拟

```go
func TestNextUnique(t *testing.T) {
	n, _ := NewNode(7)
	const total = 1_000_000
	ids := make([]int64, total)

	var wg sync.WaitGroup
	for w := 0; w < 8; w++ {
		wg.Add(1)
		go func(offset int) {
			defer wg.Done()
			for i := offset; i < total; i += 8 {
				id, err := n.Next()
				if err != nil {
					t.Error(err)
					return
				}
				ids[i] = id
			}
		}(w)
	}
	wg.Wait()

	seen := make(map[int64]struct{}, total)
	for _, id := range ids {
		if _, ok := seen[id]; ok {
			t.Fatalf("重复 ID: %d", id)
		}
		seen[id] = struct{}{}
	}
}

func TestClockBackwards(t *testing.T) {
	n, _ := NewNode(1)
	fake := int64(1_700_000_000_000)
	n.now = func() int64 { return fake }

	if _, err := n.Next(); err != nil {
		t.Fatal(err)
	}
	fake -= 100 // 回拨 100ms，超过 maxDriftMs
	if _, err := n.Next(); !errors.Is(err, ErrClockBackwards) {
		t.Fatalf("期望 ErrClockBackwards，得到 %v", err)
	}
}
```

跑 `go test -race`：100 万个 ID 在 8 个 goroutine 下并发产生，`-race` 能顺带验证锁的覆盖范围。注意这个测试会真实消耗约 250ms 的时钟（100 万 ÷ 4096/ms），不要放到 `-short` 的快速回归里。

---

## 5. 版本与生态

**雪花家族的三个流派**（都在 Java 生态成熟后被 Go 借鉴）：

| 实现 | 语言 | 核心改进 | 备注 |
|---|---|---|---|
| Twitter Snowflake | Scala | 原始 41+10+12 布局 | 已归档，仅作参考 |
| 美团 Leaf | Java | snowflake + segment 双模式 | 号段模式的事实标准，依赖 ZK 做机器号 |
| 百度 UidGenerator | Java | RingBuffer + 借未来时间 | 吞吐最高，但时钟逻辑最难排障 |
| sony/sonyflake | Go | 39 位 10ms 时间片 + 16 位机器号 | **v2 的 `MachineID` 回调签名改为返回 `int`**，接口更干净 |
| bwmarrin/snowflake | Go | 严格对齐 Twitter 布局 | API 简单，无内置机器号分配 |

- 2024-05，**RFC 9562** 正式发布，UUIDv6/v7/v8 从草案变成标准，同时 v1（暴露 MAC 地址）被标注为不推荐；`google/uuid` **v1.6.0** 起提供 `NewV7()`，是目前 Go 生态最稳的 v7 实现；
- `gofrs/uuid`、`rs/xid`、`oklog/ulid` 是同类替代：xid 是 12 字节（4 字节时间 + 3 字节机器 + 3 字节计数），比 UUID 更短；ULID 是 26 字符 Crockford Base32，可读性更好但仍是 16 字节。

**数据库侧**：

- **MySQL**：8.0 起 `AUTO_INCREMENT` 计数器**持久化到 redo log**，重启不再回落到 `max(id)+1`——5.7 时代"重启后复用已删除 ID"的坑已经不存在；但主从切换仍可能回退，依赖 `auto_increment_increment` 的步长规划。
- **PostgreSQL 与分布式数据库**：`SEQUENCE` 支持 `CACHE n` 预分配，多连接各自缓存会导致 ID 乱序、重启丢弃缓存，本质是"内置的号段模式"；TiDB 的 `AUTO_RANDOM` 把自增 ID 的高位打散以解决单 Region 写热点。它们都在说同一件事——**自增在分布式下需要额外机制，不是免费的**。

---

## 6. 常见坑与最佳实践

1. **容器化后机器号重复**。Deployment 滚动发布时新旧 Pod 并存，若机器号来自配置文件，两个进程会拿到同一个号，**在同一毫秒发号即冲突**。用 StatefulSet ordinal 或 etcd 注册；并保证"拿不到号就退出"，不要用默认值兜底。
2. **时钟回拨没处理**。这是雪花唯一可能产生重复 ID 的路径。至少要 fail-fast，绝不能"忽略回拨继续发"。
3. **低 QPS 下序列号每毫秒重置**。QPS=10 时，绝大多数 ID 的低 12 位都是 0，ID 末尾大量 `000`。这不影响正确性，但**从 ID 就能反推出 QPS 很低**，且浪费了位宽；号段模式没有这个问题。
4. **连续 ID 泄露业务量**。竞对下两单，看到订单号从 `100231` 变成 `100233`，就知道你昨天大概卖了 2 单。对外暴露的 ID 必须加前缀（`ORD20261120...`）或做可逆变换（Feistel / 号段 + 混淆表），**内部主键不要直接对外**。
5. **JS 前端精度丢失**。`int64` 超过 `2^53`（9007199254740992）后，`JSON.parse` 会静默截断末位。雪花 ID 现在就在这个量级附近，接口必须**序列化成字符串**（`json:"id,string"`），这是 Go 服务端最容易忘的一行 tag。
6. **号段重启浪费**。每次发布丢掉内存里剩余的号段，一天发 50 次版、step=10000 就是 50 万个号。对 BIGINT 无害，但如果业务假设"订单号连续可对账"，就会天天对不上。
7. **UUID 当主键导致页分裂**。`CHAR(36)` + v4 是双重打击：字符串主键让所有二级索引膨胀，随机插入让 B+ 树持续分裂。用 `BINARY(16)` + UUIDv7，或干脆用雪花。
8. **NTP 直接 step**。`ntpd -g` 或容器启动时的一次性校时可能一次跳几百毫秒，直接触发大批回拨错误。生产用 `chrony` 的 slew 模式，并在启动脚本里**先校时再启动发号器**。

---

## 7. 选型对比

| 维度 | 自增 | UUIDv4 | UUIDv7 | 雪花 | 号段 |
|---|---|---|---|---|---|
| 有序性 | 严格 | 无 | 时间趋势 | 时间趋势 | 段内严格 |
| 单机性能 | 千~万/s（DB 瓶颈） | 千万/s（纯内存） | 千万/s | 百万/s | 百万/s（每 step 一次 DB） |
| 外部依赖 | DB | 无 | 无 | 时钟 + 机器号 | DB |
| 可读性 | 好（连续） | 差 | 一般（可解时间） | 一般（可解时间） | 好 |
| ID 长度 | 8 字节 | 16 字节 | 16 字节 | 8 字节 | 8 字节 |
| 适用 | 单库、内部表 | 幂等键、trace id | 主键、客户端生成 | 分库分表主键 | 高吞吐、无时钟依赖 |

- **单库能撑住就别折腾**。日订单 10 万以内、短期没有分库计划，`AUTO_INCREMENT` 是最优解，任何分布式 ID 方案都是给自己加故障面。
- **分库分表且要趋势递增，选雪花**。机器号交给 etcd 或 StatefulSet ordinal，回拨做 fail-fast，这套组合在生产上已经被验证过无数次。
- **不想运维机器号，选号段**。它把复杂性从"时钟 + 号段分配"转移到"一张 DB 表"，用你最熟悉的组件解决问题；代价是要接受 ID 不严格连续。
- **对外暴露的 ID 必须不可预测**。内部雪花 + 外部随机前缀，或直接用 UUIDv7；永远不要把内部主键原样返回给前端。
