# MongoDB 技术介绍与使用指南（Go 后端视角）

> 面向文档的分布式数据库：BSON 灵活 schema、WiredTiger 文档级并发、副本集与分片、聚合管道与多文档事务——讲清「什么时候该用文档模型，以及片键选错的代价」。
> 上游：<https://github.com/mongodb/mongo>　|　服务端主线：**7.0 / 8.0**（整理于 2026-11）　|　Go 驱动：[mongodb/mongo-go-driver](https://github.com/mongodb/mongo-go-driver) v1.x
> 协议：服务端 **SSPL**（4.0 起）/ Go 驱动 Apache-2.0；具体版本以上游最新 release 为准。

---

## 1. 项目简介

[MySQL 篇](../mysql/mysql-guide.md)存「行 + 表 + 外键」，[go-redis 篇](../redis/redis-guide.md)存「键 + 值」，MongoDB 存的是**自描述的 BSON 文档**：一个用户连同他的地址数组、偏好对象、标签列表，全在一条文档里，读取时一次 I/O 拿全。这是它最大的优势，也是最容易滥用的地方。

- **适用场景**：schema 频繁演进的半结构化数据、嵌套/数组天然的业务对象（商品属性、内容、事件）、需要水平扩展的大容量写入、原型快速迭代、地理与时序工作负载
- **不适合**：大量多表 JOIN 与复杂关联分析、强一致的跨行金融事务（[MySQL](../mysql/mysql-guide.md) 更直接）、全文检索与相关性排序（用 [Elasticsearch](../elasticsearch/elasticsearch-guide.md)）、纯 KV 高频缓存（用 [go-redis](../redis/redis-guide.md)）、固定 schema 的报表分析（用列存 OLAP）

> 与 ES 的分工：MongoDB 是**主数据的存储**，ES 是**为检索而建的副本**。常见组合是 MongoDB 存文档、ES 存检索字段；别把 ES 当主库，也别用 MongoDB 的 `$text` 去做严肃的全文搜索。

### 安装与本地起服务

```bash
go get go.mongodb.org/mongo-driver/mongo
docker run --rm -d --name mongo-dev -p 27017:27017 mongo:7.0
# 事务与 Change Stream 需要副本集，本地可起单节点副本集：
# docker run --rm -d -p 27017:27017 mongo:7.0 --replSet rs0
# docker exec mongo-dev mongosh --eval 'rs.initiate()'
```

### 最小可运行示例

```go
package main

import (
    "context"
    "fmt"
    "time"

    "go.mongodb.org/mongo-driver/bson"
    "go.mongodb.org/mongo-driver/mongo"
    "go.mongodb.org/mongo-driver/mongo/options"
)

func main() {
    ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
    defer cancel()

    client, err := mongo.Connect(ctx,
        options.Client().ApplyURI("mongodb://localhost:27017"))
    if err != nil { panic(err) }
    defer client.Disconnect(ctx)
    if err := client.Ping(ctx, nil); err != nil { panic(err) } // Connect 不建连

    coll := client.Database("app").Collection("users")
    res, err := coll.InsertOne(ctx, bson.M{"name": "alice", "tags": bson.A{"go", "db"}})
    if err != nil { panic(err) }
    fmt.Println("inserted:", res.InsertedID)

    var doc bson.M
    if err := coll.FindOne(ctx, bson.M{"name": "alice"}).Decode(&doc); err != nil {
        panic(err)
    }
    fmt.Println(doc)
}
```

> 和 `database/sql` 一样，**`mongo.Connect` 不做真实建连**，`Ping` 才是探活。`Client` 是连接池，全局复用、退出时 `Disconnect`。

---

## 2. 功能特性总览

| 能力 | 说明 |
|---|---|
| 数据模型 | BSON 文档（最大 16MB）、嵌套文档与数组、`_id` 与 ObjectId、Schema Validation |
| CRUD | `InsertOne/Many`、`Find`、`UpdateOne/Many` + 更新运算符（`$set` / `$inc` / `$push` / `$addToSet`）、Upsert、`FindOneAndUpdate` |
| 查询 | 丰富的查询运算符（`$eq` / `$in` / `$elemMatch` / `$expr`）、投影、排序、`explain()` |
| 聚合管道 | `$match` → `$project` → `$group` → `$sort` → `$lookup` → `$facet`、`$merge`、窗口函数 |
| 索引 | 单键/复合/多键/唯一/稀疏/部分/TTL/文本/地理/哈希/通配符；ESR 规则 |
| 高可用 | 副本集（Primary/Secondary/Arbiter）、oplog、自动选举与故障转移 |
| 水平扩展 | 分片集群（mongos / config server / shard）、片键、chunk 分裂与均衡 |
| 一致性 | `writeConcern`（w / j）、`readConcern`（local / majority / snapshot / linearizable）、`readPreference` |
| 事务 | 多文档 ACID 事务（副本集 4.0+ / 分片 4.2+），快照隔离 |
| 变更流 | Change Streams（基于 oplog，可续传 resume token） |
| 其他 | GridFS 大文件、时序集合、Atlas Search / Vector Search、可查询加密 |
| Go 驱动 | `bson.M` / `bson.D` / 结构体 tag、连接池配置、Session、重试写、`CommandError` |

---

## 3. 核心概念与原理

### 3.1 BSON 与文档模型

```
{
  _id: ObjectId("..."),          // 主键，12 字节：时间戳 + 随机值 + 计数器
  name: "alice",
  address: { city: "Shenzhen" }, // 嵌套文档
  tags: ["go", "db"],            // 数组 → 多键索引
  created_at: ISODate("...")
}
```

- BSON 是 JSON 的二进制扩展，多了 `ObjectId` / `Date` / `Decimal128` / `Binary` 等类型
- **单文档最大 16MB**：无界数组（把日志、评论无限 `$push`）是最常见的建模事故
- 文档模型的核心取舍：**用冗余换读取性能**。一次查询取全对象，但同一份数据在多处冗余时更新要自己保证一致

`bson.M`（map，**无序**）与 `bson.D`（有序切片）的差异在 Go 里非常关键：**命令、索引键、排序条件、聚合管道阶段必须用 `bson.D`**，因为字段顺序有语义；用 `bson.M` 可能导致索引前缀不生效或命令解析异常。

### 3.2 WiredTiger 存储引擎

```
写入 ──► WiredTiger Cache（B+ 树，文档级并发控制）
      ──► Journal（预写日志，默认每 100ms / 提交时刷盘）
      ──► Checkpoint（每 60s 把一致性快照落盘）
```

- **文档级锁**：不同文档可并发写，粒度比 MySQL 的行锁相近但实现不同；同一文档的两个 `$inc` 是原子的
- 默认 **snappy 压缩**（可换 zstd），索引前缀压缩
- MVCC：读操作看到某个快照，不被写阻塞
- Cache 默认约 `(RAM - 1GB) / 2`，容器里务必设内存限制，否则容易被 OOM

### 3.3 副本集、选举与 oplog

```
            ┌──────────► Primary（所有写）
            │                 │ oplog（capped collection，幂等的操作日志）
   App ─────┤                 ▼
            └──────────  Secondary（异步复制，可承担读）
```

- 写先落 Primary 的 oplog，Secondary 拉取并重放；**默认异步复制**，所以读 Secondary 可能读到旧数据
- 选举基于 Raft 变体，多数派可用才能选出 Primary；**节点数用奇数**，Arbiter 不存数据只投票
- `w: majority` 表示写被多数节点确认才返回，是「主从切换不丢数据」的最低要求

三个旋钮必须分清：

| 旋钮 | 回答的问题 | 常用值 |
|---|---|---|
| `writeConcern` | 写要被多少节点确认才成功 | `w: majority, j: true` |
| `readConcern` | 读到的数据要多「稳」 | `local` / `majority` / `snapshot` |
| `readPreference` | 从哪个节点读 | `primary`（默认）/ `secondaryPreferred` |

### 3.4 分片：片键决定上限

```
App ──► mongos（路由）──► config server（chunk → shard 映射）──► shard 1..N
```

- **目标查询**（带完整片键）只打一个分片；**scatter-gather**（不带片键）广播到所有分片再汇聚，成本随分片数线性增长
- 片键三要素：**高基数**（可分的 chunk 多）、**低频率**（单值不占太大比例）、**非单调**（避免所有新写入集中在最后一个 chunk 形成热点）
- 单调递增的 `_id`（ObjectId 含时间戳）做片键 → 写入热点；低基数字段（如 `status`）做片键 → jumbo chunk 无法拆分
- 常用方案：哈希片键（写入均匀，范围查询变差）或复合片键 `{tenant_id: 1, created_at: 1}`（同租户局部、可范围查）
- **片键创建后不可改**（4.4+ 可用 `refineCollectionShardKey` 加后缀，但不能换字段）

### 3.5 索引与 ESR 规则

复合索引字段顺序遵循 **E → S → R**：

1. **E**quality：等值匹配字段放最前（`status: "active"`）
2. **S**ort：排序字段次之（`created_at: -1`），可避免内存排序
3. **R**ange：范围条件最后（`age: {$gte: 18}`）

```javascript
db.orders.createIndex({ tenant_id: 1, created_at: -1, amount: 1 })
// 适合：tenant_id 等值 + created_at 排序 + amount 范围
```

多键索引（数组字段）会让一个文档产生多个索引项；复合索引里**最多一个数组字段**。无索引排序受 100MB 内存限制，超限需 `allowDiskUse: true`（但那是救急，不是方案）。

### 3.6 聚合管道与事务边界

管道逐阶段流式处理，`$match` 和 `$project` **越靠前越省**，最好能让 `$match` 命中索引。单阶段内存上限 100MB。

多文档事务提供快照隔离，但：**默认事务最长 60 秒**、跨分片事务代价高、写冲突会 abort（错误含 `WriteConflict`），应用必须能**整体重试事务**。事务不是用来替代文档内嵌建模的——能用单文档原子更新解决，就不要开事务。

---

## 4. 使用实战

### 4.1 生产级客户端初始化

```go
package db

import (
    "context"
    "time"

    "go.mongodb.org/mongo-driver/mongo"
    "go.mongodb.org/mongo-driver/mongo/options"
    "go.mongodb.org/mongo-driver/mongo/readpref"
)

func Open(uri string) (*mongo.Client, error) {
    ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
    defer cancel()

    opts := options.Client().ApplyURI(uri).
        SetMaxPoolSize(100).
        SetMinPoolSize(10).
        SetMaxConnIdleTime(5 * time.Minute).
        SetServerSelectionTimeout(5 * time.Second).
        SetRetryWrites(true).
        SetReadPreference(readpref.Primary())

    client, err := mongo.Connect(ctx, opts)
    if err != nil { return nil, err }
    if err := client.Ping(ctx, readpref.Primary()); err != nil {
        _ = client.Disconnect(ctx)
        return nil, err
    }
    return client, nil
}
```

> `MaxPoolSize × 服务副本数` 必须小于服务端连接上限。所有操作传带超时的 `ctx`，否则一次网络抖动会挂住整个连接池。

### 4.2 结构体、bson.M 与 bson.D

```go
type User struct {
    ID        primitive.ObjectID `bson:"_id,omitempty"`
    Name      string             `bson:"name"`
    Email     string             `bson:"email"`
    Tags      []string           `bson:"tags,omitempty"`
    CreatedAt time.Time          `bson:"created_at"`
}

// 简单条件：bson.M 够用
filter := bson.M{"email": email}

// 命令/索引/排序/管道：必须用有序 bson.D
sort := bson.D{{Key: "created_at", Value: -1}}
idx := mongo.IndexModel{
    Keys:    bson.D{{Key: "tenant_id", Value: 1}, {Key: "created_at", Value: -1}},
    Options: options.Index().SetName("tenant_created"),
}
_, err := coll.Indexes().CreateOne(ctx, idx)
```

### 4.3 CRUD 与常见运算符

```go
// 插入（批量优先）
_, err := coll.InsertMany(ctx, []any{u1, u2},
    options.InsertMany().SetOrdered(false)) // 无序：单条失败不影响其余

// 查询 + 投影 + 排序 + 分页
cur, err := coll.Find(ctx,
    bson.M{"status": "active", "created_at": bson.M{"$gte": since}},
    options.Find().
        SetProjection(bson.D{{Key: "name", Value: 1}, {Key: "email", Value: 1}}).
        SetSort(bson.D{{Key: "created_at", Value: -1}}).
        SetLimit(20))
if err != nil { return err }
defer cur.Close(ctx)
for cur.Next(ctx) {
    var u User
    if err := cur.Decode(&u); err != nil { return err }
    handle(u)
}
if err := cur.Err(); err != nil { return err } // 别忘了检查游标错误

// 更新（区分 $set 与「替换整文档」）
_, err = coll.UpdateOne(ctx,
    bson.M{"_id": id},
    bson.M{"$set": bson.M{"name": "bob"}, "$inc": bson.M{"login_count": 1}},
    options.Update().SetUpsert(true))

// 数组
_, _ = coll.UpdateOne(ctx, bson.M{"_id": id},
    bson.M{"$addToSet": bson.M{"tags": "vip"}})
```

### 4.4 错误处理

```go
var u User
err := coll.FindOne(ctx, bson.M{"email": email}).Decode(&u)
switch {
case errors.Is(err, mongo.ErrNoDocuments):
    return nil // 未找到是业务分支，不是故障
case err != nil:
    return err
}

_, err = coll.InsertOne(ctx, doc)
if mongo.IsDuplicateKeyError(err) {
    return ErrAlreadyExists // 唯一索引冲突，转成业务错误
}
var ce mongo.CommandError
if errors.As(err, &ce) && ce.HasErrorLabel("TransientTransactionError") {
    // 事务整体重试
}
```

### 4.5 聚合管道

```go
pipeline := mongo.Pipeline{
    bson.D{{Key: "$match", Value: bson.D{
        {Key: "status", Value: "paid"},
        {Key: "created_at", Value: bson.D{{Key: "$gte", Value: since}}},
    }}},
    bson.D{{Key: "$group", Value: bson.D{
        {Key: "_id", Value: "$city"},
        {Key: "total", Value: bson.D{{Key: "$sum", Value: "$amount"}}},
        {Key: "orders", Value: bson.D{{Key: "$sum", Value: 1}}},
    }}},
    bson.D{{Key: "$sort", Value: bson.D{{Key: "total", Value: -1}}}},
    bson.D{{Key: "$limit", Value: 10}},
}
cur, err := coll.Aggregate(ctx, pipeline,
    options.Aggregate().SetAllowDiskUse(true))
```

> `$match` 放最前且要能命中索引；`$lookup` 是多文档 JOIN，在分片集群上代价很高，别把它当关系型 JOIN 滥用。

### 4.6 事务（含重试）

```go
func Transfer(ctx context.Context, client *mongo.Client, from, to int64, amount int64) error {
    sess, err := client.StartSession()
    if err != nil { return err }
    defer sess.EndSession(ctx)

    // WithTransaction 会自动处理 TransientTransactionError 与 commit 重试
    _, err = sess.WithTransaction(ctx, func(sc mongo.SessionContext) (any, error) {
        accounts := client.Database("app").Collection("accounts")
        r := accounts.FindOneAndUpdate(sc,
            bson.M{"_id": from, "balance": bson.M{"$gte": amount}},
            bson.M{"$inc": bson.M{"balance": -amount}})
        if r.Err() != nil { return nil, r.Err() }
        if _, e := accounts.UpdateOne(sc, bson.M{"_id": to},
            bson.M{"$inc": bson.M{"balance": amount}}); e != nil { return nil, e }
        return nil, nil
    })
    return err
}
```

> 需要副本集（或分片集群）；事务默认 60s 超时；闭包里**必须用 `sc`（SessionContext）而不是外层 `ctx`**，否则操作不在事务内。

### 4.7 分页与 Change Streams

```go
// 深分页不要用 skip（越深越慢，和 MySQL 的 OFFSET 同理）
filter := bson.M{"status": "active"}
if lastID != "" { // 游标式：用上一页最后一个 _id
    oid, _ := primitive.ObjectIDFromHex(lastID)
    filter["_id"] = bson.M{"$lt": oid}
}
cur, _ := coll.Find(ctx, filter,
    options.Find().SetSort(bson.D{{Key: "_id", Value: -1}}).SetLimit(20))

// 变更流：需要副本集，保存 resume token 以便断点续传
cs, err := coll.Watch(ctx, mongo.Pipeline{
    bson.D{{Key: "$match", Value: bson.D{
        {Key: "operationType", Value: bson.D{{Key: "$in", Value: bson.A{"insert", "update"}}}},
    }}},
}, options.ChangeStream().SetFullDocument(options.UpdateLookup))
defer cs.Close(ctx)
for cs.Next(ctx) {
    var ev bson.M
    if err := cs.Decode(&ev); err != nil { return err }
    token := cs.ResumeToken() // 持久化，重启后 SetResumeAfter(token)
    _ = token
    handleChange(ev)
}
```

---

## 5. 版本演进速览

| 版本 | 关键变化与迁移注意点 |
|---|---|
| 3.6 / 4.0 | WiredTiger 成为默认；**4.0 引入副本集多文档事务**；许可从 AGPL 改为 **SSPL** |
| 4.2 | **分片集群多文档事务**、按需物化视图、`$merge`；分布式事务代价仍高 |
| 4.4 | `refineCollectionShardKey` 可给片键加后缀、可隐藏索引、聚合 `$unionWith` |
| 5.0 | **时序集合**（time-series collections）、`$setWindowFields` 窗口函数、Stable API（版本化 API，降低升级破坏风险） |
| 6.0 / 7.0 | 查询与分片性能改进、可查询加密（Queryable Encryption）、change stream 增强、复合片键与均衡优化 |
| 8.0 | 批量写入与 `$lookup` 性能大幅提升、查询与复制优化、默认配置调整 |

**升级注意**：

- 升级前用 **Stable API**（`serverApi` + `SetServerAPIOptions`）锁定 API 版本，可显著降低跨大版本的兼容性风险
- 驱动 v1.x → **v2** 已发布：模块路径变为 `go.mongodb.org/mongo-driver/v2/...`，`bson` / `options` 包路径与部分构造 API 有变化，**不是简单改 import 就能升**；本文示例基于 v1.x
- 特性版本（feature compatibility version, FCV）需按官方路径逐级升级，不能跨大版本直接跳
- **SSPL 许可**：自建内部使用通常没问题，但把 MongoDB 作为服务对外提供需核对条款；这也是很多团队选 PostgreSQL `jsonb` 或兼容方案的原因之一

---

## 6. 常见坑与最佳实践

1. **`bson.M` 用于有序场景**：命令、索引键、排序、聚合管道必须用 `bson.D`。用 map 会导致字段顺序随机，索引前缀失效甚至命令语义错误。
2. **无界数组 / 文档超 16MB**：把评论、日志、事件无限 `$push` 进一个文档。拆成独立集合，用引用 + 分页。
3. **片键选错且不可改**：单调递增字段 → 写入热点；低基数字段 → jumbo chunk；不带片键的查询 → 广播所有分片。上线前用真实数据分布压测。
4. **读 Secondary 读到旧数据**：默认异步复制；报表类可接受，写后读自己必须 `readPreference=primary`。
5. **`writeConcern` 用 `w:1`**：主从切换时可能丢已确认的写。关键数据用 `w:majority, j:true`，代价是延迟略升。
6. **事务当万能药**：事务默认 60s、跨分片代价高、写冲突要重试。优先用单文档原子更新（`$inc` / `$set`）与内嵌建模。
7. **事务闭包里用错 ctx**：必须用 `SessionContext sc`，否则操作不在事务里，看似成功实则裸写。
8. **深分页用 `skip`**：与 MySQL `LIMIT offset` 同理，越深越慢。用 `_id` 或稳定排序字段做游标。
9. **无索引排序 / 大聚合不打 `$match`**：触发 100MB 内存上限。`allowDiskUse` 只是兜底，正解是加符合 ESR 的复合索引并把 `$match` 前置。
10. **`$where` / `$expr` / 前缀正则**：无法用索引，全集合扫描。用具体运算符与预计算字段。
11. **`$lookup` 滥用**：把它当关系型 JOIN 写多层管道，分片集群上会很慢。文档模型应该靠内嵌减少关联。
12. **唯一索引与 `null` / 稀疏字段**：稀疏或部分索引的行为与 MySQL 不同，唯一性在缺字段时可能不符预期，用 `partialFilterExpression` 明确定义。
13. **时间类型与时区**：Go 的 `time.Time` 以 UTC 存入 BSON Date；不要把时间存成字符串再比较，也不要在应用层混用本地时区。
14. **不设 Context 超时 / 不检查游标 `Err()`**：网络抖动挂住连接池；`for cur.Next()` 循环结束必须检查 `cur.Err()`，否则迭代中的错误被静默吞掉。
15. **连接池与容器内存**：`MaxPoolSize × 副本数` 超服务端上限；WiredTiger cache 默认吃一半内存，容器里不设 limit 容易 OOM。
16. **Change Stream 没存 resume token**：进程重启后从「现在」开始监听，中间变更永久丢失。
17. **把 MongoDB 当搜索或缓存**：`$text` 能力有限（用 [Elasticsearch](../elasticsearch/elasticsearch-guide.md)）；高频 KV 用 [go-redis](../redis/redis-guide.md)。

---

## 7. 选型对比

| 维度 | MongoDB | MySQL / PostgreSQL | Elasticsearch | DynamoDB / Cassandra | Couchbase |
|---|---|---|---|---|---|
| 模型 | **BSON 文档** | 关系行（PG 有 `jsonb`） | 倒排索引文档 | KV / 宽列 | 文档 + KV |
| schema | 灵活，应用侧约束 | 强 schema | mapping（可 strict） | 键设计即 schema | 灵活 |
| 事务 | 多文档 ACID（有代价） | **强，成熟** | 无 | 有限 | 有限 |
| 关联查询 | `$lookup`（弱） | **JOIN 强** | 不支持 | 不支持 | 有限 |
| 全文检索 | 弱 | 弱（PG 尚可） | **强** | 无 | 一般 |
| 水平扩展 | **分片原生** | 需分库分表 | 分片原生 | 原生 | 原生 |
| 运维成本 | 中（副本集/分片） | 低～中 | 高（JVM） | 托管低 / 自建高 | 中 |
| 许可 | **SSPL** | GPL / PostgreSQL License | ELv2 / AGPLv3 | 专有 / Apache-2.0 | 专有 / Apache |

一句话决策：

- **数据本身就是嵌套/数组、schema 还在快速演进、或需要原生水平扩展** → MongoDB，同时接受「用内嵌换 JOIN」的建模约束。
- **主数据强一致、多表关联、金融类事务** → [MySQL](../mysql/mysql-guide.md)；**既要关系又要半结构化** → PostgreSQL 的 `jsonb` 往往比引入一个新数据库更省事。
- **搜索、相关性排序、日志检索** → [Elasticsearch](../elasticsearch/elasticsearch-guide.md) 或 Loki，**不要**用 `$text` 硬撑。
- **超大规模 KV、云上托管、写入线性扩展** → DynamoDB / Cassandra，但查询能力受键设计约束，前期建模要一次做对。
- **高频缓存、锁、计数** → [go-redis](../redis/redis-guide.md)；**指标与告警** → [Prometheus](../prometheus/prometheus-guide.md)。
- **把 MongoDB 作为对外服务提供** → 先核对 SSPL 合规边界，必要时选 Apache-2.0 生态的替代方案。

> 一条经验：MongoDB 出问题的地方，八成不是数据库不行，而是**片键没想清楚**、**文档无界增长**、**把 `$lookup` 当 JOIN 用**。建模阶段花的每一小时，都会省下后面分片迁移的一周。
