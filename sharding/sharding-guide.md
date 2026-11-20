# 分库分表技术介绍与使用指南（Go 后端视角）

> **一句话定位**：单库单表扛不住之后，把数据按规则拆到多个库表，同时不让业务代码感知到「拆了」。
>
> **上游仓库**：[apache/shardingsphere](https://github.com/apache/shardingsphere)、[vitessio/vitess](https://github.com/vitessio/vitess) · **版本基线**：ShardingSphere 5.5.x、Vitess 21.x · **协议**：Apache-2.0

---

## 1. 项目简介

[MySQL 篇](../mysql/mysql-guide.md) 的 4.9 给过一条治理顺序：**先改 SQL / 加索引，再谈改表结构，最后才考虑分库分表**。本篇讲的就是「最后」这一步——索引、归档、读写分离都做完了，单机仍然顶不住时，数据该怎么拆。两者边界要分清：90% 的「MySQL 撑不住」根因是索引缺失、长事务或把报表跑在主库上，不是表不够多。

先回答最常见的问题：**单表 2000 万行不是硬线**。B+ 树三到四层就能覆盖上亿行，一次点查仍是 3~4 次页访问；2000 万行的表只要索引合适、热数据能进 buffer pool，性能完全够用。真正的硬约束只有三个：

| 约束 | 量化信号 | 说明 |
|---|---|---|
| 写入吞吐 | 单表持续 > 5000 TPS，或单实例 > 2~3 万 TPS | 受 redo log、行锁、唯一索引维护限制，加从库救不了写 |
| 单表容量 | 单表 > 1~2 亿行且无法归档，或单实例磁盘 > 2TB | 备份/恢复窗口、DDL 时长随数据量线性增长 |
| DDL 窗口 | 加字段、建索引超过业务可接受的停机时间 | 在线 DDL 也有边界，超大表最终只能 gh-ost 跑数小时 |

反过来说，下面这些症状**不该**用分片解决：

| 症状 | 真实根因 | 先做什么 |
|---|---|---|
| 单表 8000 万，主键点查 3ms | 正常水位 | 什么都不用做 |
| 订单列表页 2s | 缺 `(user_id, status, created_at)` 联合索引 | 加索引、消除深分页 |
| 写入 TPS 打满、CPU 100% | 单机 IO 或热点行竞争 | 先归档冷数据、拆热点、再评估分片 |
| 凌晨备份跑 6 小时、磁盘 2TB 告警 | 全量窗口不够、冷热未分离 | 从库备份、按时间归档到 OLAP |

**拆分的代价必须先说清**，它不是一个「加了中间件就线性扩展」的免费升级：

| 能力 | 单库单表 | 分片之后 |
|---|---|---|
| JOIN | 随意关联 | 只能同片 JOIN；跨片要么广播成 N×M，要么冗余宽表 |
| 事务 | 一个 `BEGIN/COMMIT` | 跨片只能最终一致或 XA，代价见 3.5 |
| 分页 | `LIMIT offset, n` 直接返回 | 每个分片各取 `offset+n` 行再归并，深分页放大 N 倍 |
| 聚合 | `COUNT/SUM/GROUP BY` 一次算完 | 分片聚合 + 内存归并，`DISTINCT` 可能算错 |
| 唯一约束 | 全局唯一索引 | 只在片内唯一，跨片唯一靠全局 ID 或业务校验 |
| DDL | 一条 `ALTER TABLE` | 每个分片都要执行，漏一个就出现表结构漂移 |

所以判断标准只有一句：**能不能接受「部分查询变慢、部分功能改写、运维复杂度上升」，换取写入与容量的线性扩展**。不能接受，就继续在单机上做索引与归档。

---

## 2. 能力总览

分片中间件真正提供的是一组能力的组合，不只是「按 ID 取模」：

| 能力 | 说明 | 代价 |
|---|---|---|
| 分片路由 | 按分片键把逻辑 SQL 改写成实际 SQL，支持取模、范围、哈希、自定义 | 路由键选错就退化为全分片广播 |
| 读写分离 | 同一逻辑库下挂主库与多个从库，按策略路由读流量 | 主从延迟导致读到旧数据，需要强制读主 |
| 分布式事务 | LOCAL（默认）、XA、BASE（接 Seata）三种模式 | XA 持锁时间长；BASE 依赖外部协调器 |
| 弹性扩缩 | 新增分片后迁移数据、校验、切换路由 | 取模扩容要搬大量数据，需双写与回滚预案 |
| 数据加密 | 写入自动加密、查询自动解密，支持模糊查询列 | 加密列无法范围比较，索引失效 |
| 广播表 | 小字典表在每个分片存一份，避免跨片 JOIN | 写入要同步所有分片，只适合低频变更 |
| 数据脱敏 / 影子库 | 返回结果掩码；压测流量打到影子库隔离 | 脱敏不改存储；影子库需链路染色 |

```text
应用 (Go) ──MySQL 协议──► ShardingSphere-Proxy ──┬──► ds_0.t_order_0..7
                                                  └──► ds_1.t_order_0..7
```

---

## 3. 核心概念与原理

### 3.1 垂直拆分 vs 水平拆分

三个维度经常被混为一谈，解决的问题完全不同：

```text
垂直拆库（按业务域）:  app_db → order_db / user_db / item_db
垂直拆表（按列）:      user(40 列) → user_core / user_ext
水平拆分（按行）:      t_order → ds_0.t_order_0..7
                              → ds_1.t_order_0..7
```

| 方式 | 解决什么 | 不解决什么 | 典型场景 |
|---|---|---|---|
| 垂直拆库 | 业务耦合、单库连接数与锁竞争 | 单表仍然很大 | 订单、用户、商品分库 |
| 垂直拆表 | 宽表 IO 放大、大字段拖慢主表 | 行数不变 | 用户主表 + 扩展信息表 |
| 水平拆分 | 单表行数、写入吞吐、单机容量 | JOIN、事务、聚合变复杂 | 订单、流水、消息 |

工程上通常**先垂直、后水平**：垂直拆完每个库的表都变小了，很多表根本不需要再水平拆；上来就水平拆 40 张表，是典型的过度设计。

### 3.2 分片键选择：一个键决定全局

分片键（sharding key）是路由的唯一依据，选错之后所有优化都是补丁。三条判据按优先级排列：

1. **区分度足够**：基数要远大于分片数。`status` 只有几个枚举值，哈希后必然倾斜；
2. **覆盖高频查询**：最频繁的查询条件必须带它，否则每次都是广播；
3. **能避免跨片事务**：同一业务聚合根的数据落在同一片，比如「一个用户的所有订单」。

| 候选键 | 区分度 | 查询覆盖 | 跨片风险 | 结论 |
|---|---|---|---|---|
| `user_id` | 高 | 我的订单、我的消息都带 | 按订单号查会广播 | **订单域首选** |
| `order_id` | 高 | 详情、支付回调都带 | 「我的订单」列表要广播 | 与 `user_id` 二选一 |
| `merchant_id` | 中 | 商家后台带 | 用户侧查询广播 | 商家域单独建冗余表 |
| `created_at` | 高 | 报表带 | 最新分片成写热点 | 只适合归档，不适合分片 |
| `status` | 极低 | 偶尔带 | 数据倾斜 + 广播 | **禁用** |

**用户 ID 与订单 ID 的冲突**是分片设计里最经典的矛盾：按 `user_id` 分片，「我的订单」单片命中，但支付回调只带 `order_id`；按 `order_id` 分片反过来。解法是**基因法**——生成订单号时，把 `user_id` 的低位编进 `order_id` 的低位：

```text
order_id (64 bit) = [ 序列号 / 时间戳 高位 ][ user_id 低 10 位 ]
                              │                    │
                              └─ 保证唯一、单调     └─ 取模 1024 即还原
```

这样 `order_id % 1024 == user_id % 1024`，用任一字段算出的分片号都一致。前提是**分片数必须整除 1024**（比如 2 库 × 8 表 = 16 片），否则低位对齐失效；代价是订单号不再纯自增，且只有低 10 位参与，多个用户可能落到同一片——这没关系，分片本来就允许一片承载多个用户。

### 3.3 路由算法：取模、范围、一致性哈希、基因法

| 算法 | 规则 | 优点 | 缺点 | 适用 |
|---|---|---|---|---|
| 取模 | `hash(key) % N` | 分布均匀、实现最简单 | 扩容要迁移大量数据 | 分片数稳定 |
| 范围 | 按时间/ID 区间 | 扩容简单、易归档 | 最新区间成写热点 | 日志、流水、时序 |
| 一致性哈希 | 环上顺时针找节点 | 扩容只迁移 `1/N` | 需虚拟节点防倾斜，实现复杂 | 节点频繁增减 |
| 基因法 | 低位携带另一键信息 | 双键同片，避免广播 | ID 生成受限，分片数受限 | 订单/用户双查询路径 |

**取模的真实扩容代价**：从 2 片扩到 4 片，`id % 2` 与 `id % 4` 只有一半结果相同，意味着**约 50% 的数据要搬家**；从 4 片扩到 8 片同理。所谓「翻倍扩容只搬一半」不是优点，是取模绕不开的代价。

```go
func shardOf(id int64, shards int) int { return int(id % int64(shards)) }
```

**范围分片**按时间切，写入永远落在最后一个分片，形成单点写热点；好处是历史分片只读、可以直接归档或下线。所以它适合日志、账单这类「只写最新、旧数据只读」的场景，不适合订单主表。

**一致性哈希**把节点和键都映射到一个 `2^32` 的环上，键顺时针找到第一个节点。节点增减时只影响相邻区间，迁移量约为 `1/N`。单节点容易倾斜，所以每个物理节点放几百个虚拟节点：

```go
type Ring struct {
	nodes []uint32          // 虚拟节点哈希，已排序
	owner map[uint32]string // 虚拟节点 → 物理节点
}

func (r *Ring) Add(physical string, vnodes int) {
	for i := 0; i < vnodes; i++ {
		h := crc32.ChecksumIEEE([]byte(fmt.Sprintf("%s#%d", physical, i)))
		r.nodes, r.owner[h] = append(r.nodes, h), physical
	}
	sort.Slice(r.nodes, func(i, j int) bool { return r.nodes[i] < r.nodes[j] })
}

func (r *Ring) Get(key string) string {
	h := crc32.ChecksumIEEE([]byte(key))
	idx := sort.Search(len(r.nodes), func(i int) bool { return r.nodes[i] >= h })
	if idx == len(r.nodes) {
		idx = 0 // 回绕到环首
	}
	return r.owner[r.nodes[idx]]
}
```

**基因法**在 Go 侧只是一个位运算，但顺序不能反——低位放基因，高位放序列：

```go
const geneBits = 10

// NewOrderID: 低位 10 位放 user_id 的低位，高位放序列号
func NewOrderID(userID, seq int64) int64 {
	return seq<<geneBits | (userID & ((1 << geneBits) - 1))
}

// ShardOfOrder: 从订单号直接算出分片，与按 user_id 取模的结果一致
func ShardOfOrder(orderID int64, shards int) int {
	return int(orderID&((1<<geneBits)-1)) % shards
}
```

> 若序列号放在低位、`user_id` 放高位，取模结果就只由序列号决定，基因法失效。这类位序错误线上很难发现——它不报错，只是路由到了错误的片，表现为「偶发查不到数据」。

### 3.4 跨分片查询的三种解法

跨片查询无法消除，只能选择在哪一层消化：

| 解法 | 做法 | 优点 | 代价 |
|---|---|---|---|
| 广播 + 内存归并 | 中间件把一条逻辑 SQL 发到所有分片，内存里合并排序 | 对业务透明、无需改表 | 分片越多越慢，内存与连接数放大 |
| 冗余表（异构索引） | 按另一个维度再存一份，用 CDC/binlog 同步 | 查询单片命中、性能稳定 | 存储翻倍、同步有延迟、要保证一致 |
| 离线宽表 | 明细同步到 ClickHouse / ES，复杂查询走 OLAP | 不占 OLTP 资源、支持任意维度 | 有延迟，不适合强一致读 |

**广播查询**是最容易踩坑的一种：`SELECT * FROM t_order WHERE status = 1 ORDER BY id DESC LIMIT 20` 会被改写成 16 条 SQL，每条各取 20 行，再在 Proxy 内存里排序取前 20。分片数 16 时还扛得住；分片数 64、并发一上来，归并内存和连接数会先崩。

```sql
SELECT * FROM t_order WHERE status = 1 ORDER BY id DESC LIMIT 20;   -- 广播：16 片全扫
SELECT * FROM t_order WHERE user_id = 42 ORDER BY id DESC LIMIT 20; -- 单片命中
```

**冗余表**解决「换个维度查」：订单按 `user_id` 分片，商家后台却按 `merchant_id` 查，于是再建一张 `t_order_by_merchant`，按 `merchant_id` 分片，通过 binlog/CDC 异步同步。它本质是**用存储和一致性换查询性能**，同步延迟通常秒级，商家后台能接受，支付链路不能。

### 3.5 分布式事务与全局 ID

跨片写入没有单机事务可用。四种选择：

| 方案 | 一致性 | 性能 | 侵入性 | 适用 |
|---|---|---|---|---|
| LOCAL（默认） | 无全局保证，各片独立提交 | 最高 | 无 | 能保证同一分片内，或可接受最终一致 |
| XA | 强一致 | 低（两阶段、持锁） | 低 | 同机房、短事务、低并发 |
| BASE / Seata AT | 最终一致 | 中 | 中（需 Seata） | 跨片长流程、可补偿 |
| 本地消息表 / Outbox | 最终一致 | 高 | 中（多一张表） | 跨服务 + 跨片，**推荐** |

> 最常见的误用：以为开了 Proxy 就自动有分布式事务。默认的 LOCAL 模式下，`BEGIN` 跨两个分片时会**各自提交**，中间失败就一半成功一半失败。要么把一次写入收敛到同一分片（靠分片键设计），要么显式上 XA/Outbox。

**全局 ID** 是分片的孪生问题：自增主键在每个分片独立计数，必然冲突。两种主流方案：

```sql
-- 号段模式：一次取 1000 个，DB 只承担低频更新
BEGIN;
UPDATE id_segment SET max_id = max_id + 1000 WHERE biz_tag = 'order';
SELECT max_id FROM id_segment WHERE biz_tag = 'order';  -- 本段上界
COMMIT;
```

```text
雪花 ID (64 bit) = 1 符号位 | 41 时间戳(ms) | 10 机器位 | 12 序列号
                   ↑ 恒为 0    ↑ 可用 69 年    ↑ 1024 节点  ↑ 每 ms 4096 个
```

号段模式依赖 DB，应用在内存中从 `max_id-999` 发到 `max_id`，一次网络往返换 1000 个 ID，吞吐足够；雪花不依赖存储，但**机器位必须唯一**，K8s 里 Pod 重启拿到相同 worker-id 就会生成重复 ID。发放 worker-id 用 etcd/Redis 租约，细节见 [distributed-id 篇](../distributed-id/)。

---

## 4. 使用实战

### 4.1 ShardingSphere-Proxy 部署与规则配置

Proxy 是独立进程，对应用暴露 MySQL 协议，默认端口 3307。容器部署时注意两点：配置文件挂载进 `conf/`，MySQL JDBC 驱动放进 `ext-lib/`（官方镜像不自带）。

```bash
docker run -d --name ssp-proxy \
  -p 3307:3307 \
  -v "$PWD/conf:/opt/shardingsphere-proxy/conf" \
  -v "$PWD/ext-lib:/opt/shardingsphere-proxy/ext-lib" \
  apache/shardingsphere-proxy:5.5.2
# ext-lib 里至少要有 mysql-connector-j，否则启动时报找不到驱动
```

`conf/config-sharding.yaml` 完整示例（2 库 × 8 表，订单按 `user_id` 分库、`order_id` 分表）：

```yaml
databaseName: sharding_db

dataSources:
  ds_0:
    url: jdbc:mysql://mysql-0:3306/app?useSSL=false&serverTimezone=UTC&allowPublicKeyRetrieval=true
    username: app
    password: apppass
    maxPoolSize: 50
  ds_1:
    url: jdbc:mysql://mysql-1:3306/app?useSSL=false&serverTimezone=UTC&allowPublicKeyRetrieval=true
    username: app
    password: apppass
    maxPoolSize: 50

rules:
  - !SHARDING
    tables:
      t_order:
        actualDataNodes: ds_${0..1}.t_order_${0..7}
        tableStrategy:
          standard:
            shardingColumn: order_id
            shardingAlgorithmName: order_table_inline
        keyGenerateStrategy:
          column: order_id
          keyGeneratorName: snowflake
      t_order_item:
        actualDataNodes: ds_${0..1}.t_order_item_${0..7}
        tableStrategy:
          standard:
            shardingColumn: order_id
            shardingAlgorithmName: order_item_table_inline
    bindingTables:
      - t_order,t_order_item          # 绑定表：同一 order_id 落在同片，JOIN 不跨片
    broadcastTables:
      - t_dict                        # 字典表：每个分片各存一份
    defaultDatabaseStrategy:
      standard:
        shardingColumn: user_id
        shardingAlgorithmName: user_db_inline
    defaultTableStrategy:
      none:
    shardingAlgorithms:
      user_db_inline:
        type: INLINE
        props: {algorithm-expression: "ds_${user_id % 2}"}
      order_table_inline:
        type: INLINE
        props: {algorithm-expression: "t_order_${order_id % 8}"}
      order_item_table_inline:
        type: INLINE
        props: {algorithm-expression: "t_order_item_${order_id % 8}"}
    keyGenerators:
      snowflake:
        type: SNOWFLAKE
        props: {worker-id: 1}
```

`conf/server.yaml` 里再打开两个开关：`sql-show: true` 打印 Logic SQL 与实际 SQL（排查广播的第一手材料），`max-connections-size-per-query: 4` 限制单条查询占用的后端连接数，防广播打爆连接池。

连接并建表。**通过 Proxy 建表，DDL 会自动广播到所有实际分片**：

```sql
-- mysql -h 127.0.0.1 -P 3307 -u root -p sharding_db
CREATE TABLE t_order (
  order_id   BIGINT        NOT NULL,
  user_id    BIGINT        NOT NULL,
  amount     DECIMAL(12,2) NOT NULL,
  status     TINYINT       NOT NULL DEFAULT 0,
  created_at DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (order_id),
  KEY idx_user_created (user_id, created_at)
) ENGINE=InnoDB;

-- 命中 1 库 1 表
PREVIEW SELECT * FROM t_order WHERE user_id = 42 AND order_id = 10086;
-- 命中 1 库 8 表 = 8 条实际 SQL（表无法用 user_id 定位）
PREVIEW SELECT * FROM t_order WHERE user_id = 42 ORDER BY created_at DESC LIMIT 20;
```

> 注意第二个 `PREVIEW` 的结果：数据库按 `user_id` 路由到 1 个库，但表按 `order_id` 路由，只有 `user_id` 时无法定位表，于是**该库内 8 张表全部广播**。这正是 3.2 说的「一个键无法覆盖所有路径」——要么把表分片键也改成 `user_id`（订单号只做全局 ID），要么按 3.4 建冗余表。

### 4.2 Go 应用接入：Proxy 模式是唯一选择

这是 Go 团队最容易走错的一步：**ShardingSphere-JDBC 是 Java 库（jar 包），Go 用不了**。它是把分片逻辑织入 Java 应用的 `DataSource`，没有跨语言实现；Go 侧能用的只有 Proxy——走标准 MySQL 协议，`database/sql` 完全无感。

| 接入方式 | 语言 | 部署形态 | Go 可用 | 说明 |
|---|---|---|---|---|
| ShardingSphere-JDBC | Java | 嵌入应用进程 | **否** | 零额外跳数，但只支持 JVM |
| ShardingSphere-Proxy | 任意 | 独立进程 | **是** | 多一跳网络，运维多一个组件 |
| Vitess VTGate | 任意 | 独立进程 | **是** | MySQL 协议，适合 K8s 大集群 |
| 应用层分片 | 任意 | 代码内 | 是 | 无中间件，但路由/归并全自己写 |

```go
package db

import (
	"database/sql"
	"time"

	_ "github.com/go-sql-driver/mysql"
)

func OpenProxy() (*sql.DB, error) {
	// 注意库名是逻辑库 sharding_db，不是底层物理库
	dsn := "root:root@tcp(127.0.0.1:3307)/sharding_db" +
		"?charset=utf8mb4&parseTime=True&loc=Local&timeout=3s&readTimeout=5s"
	db, err := sql.Open("mysql", dsn)
	if err != nil {
		return nil, err
	}
	// 这里是逻辑连接数；Proxy 后端还会按分片数放大，别照搬单库的 200
	db.SetMaxOpenConns(30)
	db.SetMaxIdleConns(10)
	db.SetConnMaxLifetime(30 * time.Minute)
	return db, db.Ping()
}
```

四条实战约束：

1. **SQL 里必须带分片键**。ORM 生成的 `WHERE id = ?` 若 `id` 不是分片键，就是全分片广播；GORM 场景下要显式加条件，别指望中间件猜。
2. **`LastInsertId()` 不可靠**。`order_id` 由 Proxy 的雪花生成器产生，单条插入能拿到，批量插入在部分版本上只返回第一个或 0；重要链路用业务唯一键回查。
3. **事务不要跨片**。默认 LOCAL 模式下跨片 `BEGIN` 等于两个独立事务，失败即脏数据；确实需要就把同一聚合根的数据设计到同一片。
4. **`SHOW` / `SET` 会被转发但语义可能不同**。`SET autocommit=0` 这类会话状态在 Proxy 上行为受限，用标准 `BeginTx` 而不是手写事务语句。

### 4.3 双写迁移：单库 → 分片的三阶段

不能停机就不能「导出再导入」。标准路径是**双写 + 校验 + 切读 + 停旧写**，每一步都可回滚：

```text
阶段 0  存量同步   全量导出 + binlog 增量追平，按分片键重算目标片
阶段 1  双写       写旧库（事实源，失败即失败）+ 写分片（失败只记补偿）
阶段 2  灰度切读   按 user_id 哈希灰度 1% → 10% → 50% → 100%，双读比对
阶段 3  停旧写     旧库转只读，保留 7~30 天回滚窗口后下线
```

```go
// 双写：旧库是事实源，新库失败不阻塞主流程，交给补偿任务
func CreateOrder(ctx context.Context, oldDB, newDB *sql.DB, o *Order) error {
	if err := insertOld(ctx, oldDB, o); err != nil {
		return err // 旧库失败就是失败，不能让用户以为下单成功
	}
	if err := insertNew(ctx, newDB, o); err != nil {
		dualWriteFailure.Inc()             // 打点告警
		enqueueCompensation(ctx, o.OrderID) // 按主键回放，幂等 upsert
	}
	return nil
}
```

每一步都要有**对账**，否则「双写成功」只是你以为的成功：

```sql
-- 源库与目标分片各算一遍，按 user_id 的桶逐段比对
SELECT COUNT(*) AS cnt, COALESCE(SUM(amount), 0) AS total, MAX(order_id) AS max_id
FROM t_order WHERE user_id >= ? AND user_id < ?;
```

比对三个指标：**行数、金额汇总、最大主键**。行数对但金额不对，通常是双写时更新丢了；金额对但行数多，通常是补偿任务重复插入（缺唯一索引）。差异率低于十万分之一再切读，不要凭感觉。

### 4.4 扩容：翻倍迁移与一致性哈希

扩容的本质是**把一部分数据搬到新分片，并让路由规则指向新位置**。取模扩容的迁移量是固定的：

| 扩容 | 路由变化 | 需迁移比例 | 说明 |
|---|---|---|---|
| 2 → 4 | `%2` → `%4` | ~50% | 原 0 片的一半去 2 片，1 片的一半去 3 片 |
| 4 → 8 / 8 → 9（一致性哈希） | `%4`→`%8` / 环上插节点 | ~50% / ~1/9 | 取模永远搬一半；一致性哈希只影响相邻区间 |

翻倍扩容的迁移脚本思路：**先双写，再按主键游标批量搬历史，最后切路由**。批量大小控制在 500~1000，避免大事务与长锁。下面的 SQL 假设两个库在同一实例，跨实例时改成 SELECT 导出 + 批量 INSERT（或走 CDC）：

```sql
-- 从 ds_0 搬到 ds_2：只搬 user_id % 4 = 2 的部分
-- 用主键游标而非 OFFSET，避免深分页
INSERT INTO ds_2.t_order_0 (order_id, user_id, amount, status, created_at)
SELECT order_id, user_id, amount, status, created_at
FROM ds_0.t_order_0
WHERE user_id % 4 = 2 AND order_id > ?
ORDER BY order_id
LIMIT 1000
ON DUPLICATE KEY UPDATE amount = VALUES(amount), status = VALUES(status);
```

搬完必须做**逐片计数校验**（`src` 与 `dst` 的 `COUNT(*)` 相等才切路由），再做抽样比对。

> `ON DUPLICATE KEY UPDATE` 让迁移可重入——中断后从上次的 `order_id` 继续即可；没有这一条，重跑就会主键冲突，有了它最多重复写（前提是更新列覆盖全）。注意 `user_id % 4` 无法走索引，生产上要按 `user_id` 区间分段扫，别一条 SQL 扫全表。

Vitess 把这套流程产品化了：`MoveTables` 做库表搬迁、`Reshard` 做分片拆分，底层都是 VReplication 追 binlog，切换时短暂只读。ShardingSphere 5.x 也自带 Scaling 迁移作业，但生产上仍建议自己掌控校验与回滚点。

### 4.5 监控与慢查询：先看路由，再看执行计划

分片下的性能问题分两层：**路由层**（一条 SQL 被放大成几条）和**执行层**（单条 SQL 在单片上慢）。MySQL 篇 3.8 的联合索引顺序、4.5/4.7 的 `EXPLAIN` 判读依然成立，只是作用域变成了「单片」。

第一层用 `sql-show` 日志识别：

```text
[INFO] Logic SQL : SELECT id, amount FROM t_order WHERE user_id = 42 ORDER BY created_at DESC LIMIT 20
[INFO] Actual SQL: ds_0 ::: SELECT ... FROM t_order_3 WHERE user_id = 42 ...
[INFO] Actual SQL: ds_0 ::: SELECT ... FROM t_order_7 ...
```

判断标准很直接：**一条 Logic SQL 对应几条 Actual SQL**。1 条是理想；等于分片数是全广播；介于两者之间说明只路由到了部分分片（通常是 `IN` 或范围条件）。

第二层用 `PREVIEW` 确认路由后再 `EXPLAIN`：

```sql
PREVIEW SELECT * FROM t_order WHERE order_id = 10086;
-- 再对单片做执行计划；Proxy 返回多个结果集，用 mysql CLI 看，别用 Go 的 Query
EXPLAIN SELECT * FROM t_order WHERE user_id = 42 AND order_id = 10086;
```

分片下 `EXPLAIN` 的三个语义变化：

| 项 | 单库含义 | 分片下含义 |
|---|---|---|
| 结果集个数 | 一个 | 每个实际 SQL 一个，Go 侧 `Query` 只读第一个 |
| `rows` | 该表扫描行数 | 单个分片的扫描行数，总量要乘以分片数 |
| `Extra: Using filesort` | 单表排序 | 单片内排序；跨片归并排序发生在 Proxy 内存，`EXPLAIN` 看不到 |

慢查询治理要加一个维度：**归并开销**。跨片 `ORDER BY ... LIMIT` 会让每个分片各返回 `offset+limit` 行，Proxy 再做堆排序——`LIMIT 100000, 20` 在 16 个分片上就是 160 万行进入归并，99.99% 的数据白传。`GROUP BY` / `DISTINCT` 则要把所有分组的中间结果拉回内存，分片数 16、每片 10 万行时，Proxy 的堆内存会先于数据库崩溃。

对策与单库一致，但更紧迫：**游标分页**（记住上一页的 `order_id`）、**限制最大页数**、**把导出类需求赶到 OLAP**。

---

## 5. 版本与生态

| 项目 | 版本基线 | 定位 | 关键点 |
|---|---|---|---|
| ShardingSphere-Proxy | 5.5.x | 独立代理，MySQL/PG 协议 | Go 可用；DistSQL 在线改规则；需自带 JDBC 驱动 |
| ShardingSphere-JDBC | 5.5.x | Java 嵌入式 | 零跳数，仅 JVM；Go 不可用 |
| Vitess | 21.x | 云原生 MySQL 分片 | VTGate/VTTablet/topology；K8s Operator；VReplication |
| TiDB | 8.x | 原生分布式 HTAP | MySQL 兼容、Raft 多副本、无需中间件 |
| PolarDB-X | 2.x | 阿里云分布式 | 计算存储分离，兼容 MySQL 生态 |

**ShardingSphere 5.x 的两条线同源不同形态**：JDBC 把内核织进 Java 应用，Proxy 把同一内核包成独立进程。规则语法（`!SHARDING`、`actualDataNodes`、DistSQL）两边一致，所以文档可以互相参照；差别在部署与故障域——JDBC 没有额外跳数但升级要动应用，Proxy 升级独立但多一个必须高可用的进程。4.x 到 5.x 的配置从 `shardingRule:` 平铺结构改成 `rules:` 列表 + `!SHARDING` 标签，老配置不能直接复用。

**Vitess 的出身决定了它的形态**：2010 年在 YouTube 为应对 MySQL 分片而写，2018 年进 CNCF、2019 年毕业，Slack、Square、PlanetScale 都在用。它的核心是 `VTGate`（SQL 路由与归并）+ `VTTablet`（管理单个 MySQL）+ 拓扑服务（etcd/ZooKeeper/Consul），分片信息放在 vschema 里：

```json
{
  "sharded": true,
  "vindexes": { "user_hash": { "type": "hash" } },
  "tables": {
    "orders": { "columnVindexes": [{ "column": "user_id", "name": "user_hash" }] }
  }
}
```

```bash
vtctldclient ApplyVSchema --vschema-file vschema.json commerce
vtctldclient Reshard --workflow=reshard_80 --target-keyspace=commerce \
  --source-shards=- --target-shards=-80,80- create   # 1 片拆 2 片，参数随版本微调
```

它的优势是**为 K8s 而生**：vitess-operator 把 keyspace/shard/tablet 都做成 CRD，扩缩容是声明式的；代价是概念多（keyspace、shard、vindex、tablet type），学习曲线比 ShardingSphere 陡。

**云上分布式数据库 vs 中间件分片**：TiDB、PolarDB-X、OceanBase 把分片做进存储引擎，应用连一个地址，扩容由控制面完成，事务是原生的。代价是**迁移成本与厂商绑定**——SQL 兼容度、运维习惯、成本模型都要重新评估。中间件分片则是「保留 MySQL、在应用与 DB 之间加一层」，改造小、可控，但 JOIN/事务/分片的复杂度留在你这边。

---

## 6. 常见坑与最佳实践

1. **跨片 JOIN 退化成笛卡尔积**：`t_order JOIN t_user` 且两表分片键不同时，中间件会把每个分片的结果两两组合。对策：用 `bindingTables` 保证同键同片，小表设 `broadcastTables`，其余走冗余宽表。
2. **`COUNT(*)` / `ORDER BY` 归并内存爆炸**：每个分片都返回全量结果，Proxy 在内存里合并。对策：`max-connections-size-per-query` 限流、分片数控制在 16~64、聚合走预计算或 OLAP。
3. **深分页 `LIMIT 100000, 20`**：放大成 `分片数 × 100020` 行。对策：游标分页（`WHERE order_id > ?`）、限制最大页数、导出走离线。
4. **自增主键冲突 / 唯一索引只在片内唯一**：每个分片独立 `AUTO_INCREMENT`，必然重复；`UNIQUE(email)` 在 16 个分片里各唯一，全局仍可能重复。对策：写入前用雪花/号段生成主键，全局唯一键单独建索引表或用 Redis 兜底。
5. **分布式事务误用**：默认 LOCAL 模式下跨片事务各自提交，失败一半。对策：先靠分片键把写收敛到同片；确实跨片用 Outbox，别默认开 XA 拖垮吞吐。
6. **分片键选错导致全广播**：用 `status`、`created_at` 当分片键，或查询条件永远不带分片键。对策：分片键必须是高频等值查询列，且基数远大于分片数。
7. **`DISTINCT` / `GROUP BY` 结果错误**：跨片 `DISTINCT` 需要全局去重，部分实现只做片内去重。对策：确认中间件版本的归并能力，必要时改写成「分片聚合 + 应用层合并」。
8. **DDL 只在部分分片执行**：绕过 Proxy 直连底层库改表，或新增分片时漏建表。对策：DDL 统一走 Proxy/DistSQL，上线后跑一遍表结构一致性校验。
9. **连接数放大**：1 个逻辑连接可能对应 N 个后端连接，`MaxOpenConns=200` 会把底层 MySQL 打满。对策：按「逻辑连接 × 分片数 × 副本数」估算，Proxy 侧设 `maxPoolSize` 上限。

---

## 7. 选型对比

| 维度 | ShardingSphere-Proxy | Vitess | 应用层分片 | TiDB |
|---|---|---|---|---|
| 侵入性 | 低（改 DSN 端口） | 低（MySQL 协议） | **高**（路由/归并自己写） | 低（换连接地址） |
| 事务能力 | LOCAL / XA / BASE | 单分片事务，跨片靠应用 | 自己实现 | **原生分布式事务** |
| 扩容 | Scaling 作业 / 手工双写 | `Reshard` + VReplication | 全手工 | 自动调度（Region 分裂） |
| SQL 兼容 | 高，部分归并受限 | 高，MySQL 语法 | 取决于实现 | 高，少数函数不支持 |
| JOIN | 同片/广播表可用 | 同片可用，跨片受限 | 自己拼 | 原生支持（跨节点较慢） |
| 运维成本 | 中（多一个代理） | **高**（拓扑、tablet、K8s） | 低（无额外组件） | 中高（集群 + 监控） |
| 生态/Hosted | Apache 社区 + 云厂商 | CNCF + PlanetScale | 无 | TiDB Cloud / 各云 |

一句话决策：

- **能换分布式数据库就别自己分片**。数据量在亿级、团队没有专职 DBA、又不想维护中间件，直接上 TiDB / PolarDB-X，事务与扩容都是原生的。
- **必须保留 MySQL、Go 技术栈** → **ShardingSphere-Proxy**。JDBC 模式 Go 用不了，这是硬结论；Proxy 是唯一无侵入的选择。
- **已经在 K8s、分片规模大（几十到上百片）、需要频繁 Reshard** → Vitess，但要做好学习成本与运维投入的准备。
- **分片逻辑简单、只有两三张表、团队想完全掌控** → 应用层分片，省掉中间件但也省不掉归并、扩缩容、对账这些活。

> 最后一句话：分片是**架构上的最后一张牌**，打出去就很难收回。先用 [MySQL 篇](../mysql/mysql-guide.md) 的索引与慢查询治理把单机榨干，再考虑 [分布式事务](../distributed-transaction/distributed-transaction-guide.md) 与全局 ID 的配套成本，最后才动分片键。
