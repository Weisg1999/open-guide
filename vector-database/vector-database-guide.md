# 向量数据库技术指南

> 向量检索把「相似」变成可计算的距离：HNSW 用图换召回，IVF/PQ 用聚类与量化换内存，过滤与召回率才是落地的关键。
> 上游：<https://github.com/pgvector/pgvector> v0.8 · <https://github.com/milvus-io/milvus> v2.x · <https://github.com/qdrant/qdrant> v1.x　|　协议：PostgreSQL / Apache-2.0

---

## 1. 项目简介

[OpenSearch 篇](../opensearch/opensearch-guide.md) 已把 k-NN 当作搜索引擎的字段类型讲透了，[PostgreSQL 篇](../postgresql/postgresql-guide.md) 讲了 PG 的索引与扩展机制。这篇不重复 HNSW 的基础定义，只回答一个工程问题：**向量检索应该塞进已有的库，还是单独起一个服务？**

- **pgvector**：不是数据库，是 PG 的扩展。向量列与业务表同表，过滤、事务、权限、备份全部复用 PG；上限是单机 PG 的上限。
- **Qdrant**：Rust 写的独立服务。HNSW 之外把「过滤」做成索引的一等公民（payload index + filterable HNSW），量化档位多，单机可撑千万级。
- **Milvus**：分布式架构，存算分离，段（segment）+ 对象存储 + 消息队列。为亿级设计，代价是 etcd / MinIO / Pulsar 要自己维护。

> 判据：**向量只是业务表的一个属性 → pgvector；向量是核心负载且过滤很重 → Qdrant；量级到亿、要独立扩缩容 → Milvus。**

```bash
docker run --rm -d --name pgv -p 5432:5432 \
  -e POSTGRES_PASSWORD=devpass -e POSTGRES_DB=app pgvector/pgvector:pg17
docker run --rm -d --name qdrant -p 6333:6333 -p 6334:6334 qdrant/qdrant:v1.19.2  # 6333 REST / 6334 gRPC
go get github.com/jackc/pgx/v5/pgxpool github.com/pgvector/pgvector-go github.com/qdrant/go-client/qdrant github.com/milvus-io/milvus/client/v2
```

---

## 2. 能力总览

| 能力 | pgvector | Qdrant | Milvus |
|---|---|---|---|
| 形态 | PG 扩展（进程内） | 独立服务（Rust） | 分布式服务（Go/C++） |
| 索引 | HNSW、IVFFlat | HNSW（dense）+ 稀疏倒排 | HNSW、IVF_FLAT/PQ、DiskANN、GPU CAGRA |
| 量化 | `halfvec` / `bit` + 表达式索引 | scalar / binary / product / TurboQuant | PQ、SQ、RaBitQ、BIN_FLAT |
| 过滤 | SQL `WHERE`，0.8 起迭代扫描 | payload index，条件下推到图遍历 | 布尔表达式 + partition key + 位图 |
| 混合检索 | SQL 里拼 `tsvector` + 向量 | Query API prefetch + RRF/DBSF | HybridSearch + RRFRanker |
| 事务 | **完整 ACID** | 无跨点事务，单点原子 | 无跨集合事务 |
| 水平扩展 | 只读副本 / 表分区 | shard + replication_factor | 原生分片、副本、存算分离 |
| 多租户 | `tenant_id` 列 + 部分索引 | payload 过滤 / 多 collection | partition key |
| Go 客户端 | `pgx` + `pgvector-go` | `qdrant/go-client` 1.19.x | `milvus/client/v2`（2.6.x SDK） |

---

## 3. 核心概念与原理

### 3.1 距离度量：先想清楚「归一化」

| 度量 | pgvector 算子 | 索引 opclass | 距离定义 |
|---|---|---|---|
| L2（欧氏） | `<->` | `vector_l2_ops` | `‖a-b‖₂` |
| 余弦 | `<=>` | `vector_cosine_ops` | `1 - cos(a,b)` |
| 内积 | `<#>` | `vector_ip_ops` | `-a·b`（取负，保证升序即最相似） |
| 稀疏内积 | `<#>` | `sparsevec_ip_ops` | 用于 `sparsevec` |

关键恒等式：**当 `‖a‖ = ‖b‖ = 1` 时，`a·b = cos(a,b)`**。所以「先归一化再用内积」与「直接算余弦」等价，但内积少一次求模、少一次除法；更重要的是量化（int8 / binary）对内积的误差远小于对余弦的误差。反过来，**没归一化就用内积，等于把向量长度当成了相关性**——BGE、GTE、E5 的原始输出长度不均，不归一化会让长文本、高置信样本系统性排前面，不报错，只让效果变差。OpenAI 的 `text-embedding-3-*` 返回时已归一化，但换模型时这个假设会悄悄失效。

> 约定：**写入前归一化、库内存单位向量、查询也归一化**，放在 embedding 服务的出口而不是散在每个调用方。只有模长本身编码了热度/置信度（推荐场景）时才刻意保留，此时用内积，且必须与 opclass 一致。

### 3.2 HNSW：M、efConstruction、efSearch 的三角

```text
Layer 2:   A ─────────────── F            ← 稀疏，长跳
Layer 1:   A ──── C ──── E ── F
Layer 0:   A ─ B ─ C ─ D ─ E ─ F ─ G      ← 全量，短跳
```

元素以指数衰减的概率出现在高层，查询从顶层贪心下降，到 layer 0 再做 beam search。三个参数各管一段：

| 参数 | 作用 | 影响 | 经验值 |
|---|---|---|---|
| `m` | 每层出边数（layer 0 约 `2m` 条双向边） | ↑ 召回↑、内存↑、构建慢 | 16；高维/高召回 32 |
| `efConstruction` | 建图时每层候选队列长度 | ↑ 图质量↑，构建时间近似线性 | 64 起步，生产 200–400 |
| `efSearch` | 查询时候选队列长度 | ↑ 召回↑，延迟近似线性 | 40–200，**必须 ≥ k** |

内存估算 `N × (dim × 4 + m × 2 × 4 + 元数据)`：100 万 × 768 维 float32 原始向量约 3 GB；HNSW 图按 m=16 计算约 `1e6 × 16 × 2 × 4B ≈ 128 MB`（m=32 约 256 MB），加上元数据与索引结构通常是数百 MB 量级，而不是 GB 级。**HNSW 的主要成本是内存而不是 CPU**，这是「召回-延迟-内存」三角里最先撞到的墙：加 `m` 换召回，等于按比例加内存。

`efSearch` 是唯一能在线调的旋钮，但有三个坑：小于 `k` 时结果直接不足；它是会话/请求级参数而非全局配置；调大只提升召回，上限由建图质量决定。所以「召回不够」先看 `efConstruction` 和图有没有被频繁删除搞坏，再谈调 `efSearch`。

### 3.3 IVF 与 PQ：用聚类换速度，用量化换内存

**IVF** 先用 k-means 把向量聚成 `nlist` 个簇，查询只扫最近的 `nprobe` 个。`nlist` 太小每簇太大扫得慢，太大则簇内样本少、边界效应明显：行数 < 100 万用 `rows / 1000`，> 100 万用 `sqrt(rows)`；`nprobe` 从 `sqrt(nlist)` 起调，召回不够就加，延迟近似线性。它**必须先有数据再建索引**（要训练质心），持续写入的新数据落在分配时的簇里，分布漂移后召回衰减，必须定期重建——这正是 pgvector 建议「先灌数据、后建 IVFFlat」的原因；HNSW 支持增量插入，更适合持续写入，代价是内存与构建时间。

**PQ（乘积量化）** 把 d 维切成 m 段，每段用 256 个质心编码成 1 字节：`768d float32 = 3072B → 96 段 × 1B = 96B`，压缩 32 倍。但距离在压缩空间里算，有量化误差，所以标准做法是 **PQ 粗排 + 原始向量精排**（over-fetch 3–10 倍再 rescore）。

pgvector 没有 PQ，只有 `halfvec`（2 倍）与 `bit`（32 倍，必须配 rescore）；Milvus 有 IVF_PQ/SQ/RaBitQ，Qdrant 有 scalar/binary/product 三档。**要省内存选 Qdrant 或 Milvus**，pgvector 的省内存手段只有 halfvec。IVF 的甜点区是百万到千万、内存不足以放整张 HNSW 图的场景；再大就该上 DiskANN 这类磁盘索引。

### 3.4 过滤检索：post-filter 为什么会少结果

| 策略 | 做法 | 问题 |
|---|---|---|
| **post-filter** | 先 ANN 取 top-N，再按条件过滤 | 选择性 1% 时，N=100 只出 1 条 |
| **pre-filter** | 先按条件筛出集合，再在集合内暴力/ANN 搜索 | 集合大时退化成全表扫 |
| **filtered-HNSW** | 遍历图时跳过不匹配节点，为被过滤节点额外建边 | 实现复杂，索引膨胀 |

本质矛盾：**ANN 的候选集按距离取，过滤按属性删，两者彼此独立**。过滤越狠，候选被删得越多；过滤后只剩 1% 时，top-100 的候选期望只命中 1 条，于是「查到了但不够 k 条」。pgvector 在 0.8.0 之前就是这个行为，只能靠硬调 `ef_search` 或用部分索引绕开。

三条实用解法：

1. **pgvector 迭代扫描**（0.8.0+）：`hnsw.iterative_scan = relaxed_order`，扫不满 k 条就继续扫，直到 `hnsw.max_scan_tuples`（默认 20000）耗尽；`strict_order` 保证严格按距离排序，但会多扫。
2. **部分索引 / 分区**：租户、状态这类固定维度，`CREATE INDEX ... WHERE tenant_id = 42` 让索引里只有该租户的向量，从结构上消灭 post-filter。
3. **Qdrant / Milvus 原生过滤**：Qdrant 在 HNSW 遍历时下推 payload 条件并为被过滤点补边，Milvus 用 partition key 把租户物理隔离。

> 判定标准：**过滤后命中率 < 10% 时，任何 post-filter 实现都会掉召回**。先算 `匹配行数 / 总行数`，再决定策略。

### 3.5 混合检索与 RRF：别把两种分数直接相加

向量擅长语义，BM25 擅长专有名词、型号、错误码。混合检索融合两路结果，但**分数不可比**：余弦在 `[-1, 1]`，BM25 无上界且随查询长度漂移。直接 `0.7 * vec + 0.3 * bm25` 是错的。**RRF（Reciprocal Rank Fusion）** 只用排名，天然免疫量纲：

```text
score(d) = Σ_i  1 / (k + rank_i(d))        # k 通常取 60
```

第 1 名贡献 `1/61`，第 10 名 `1/70`；`k` 压平头部差异，让「两路都进前 10」优于「一路第 1、一路未命中」。RRF 无需调权重，是新系统默认选择；要业务偏好时再用 query 内归一化（min-max / z-score）后的加权和。Milvus 用 `HybridSearch` + `RRFRanker`，Qdrant 用 Query API 的 `prefetch` + `Fusion_RRF`，本质相同。pgvector 的优势在这里最明显：**向量与全文检索在同一个 SQL、一个事务、一次往返**。

```sql
WITH vec AS (
  SELECT id, ROW_NUMBER() OVER (ORDER BY embedding <=> $1) AS rnk
  FROM documents WHERE tenant_id = $2 ORDER BY embedding <=> $1 LIMIT 50
), kw AS (
  SELECT id, ROW_NUMBER() OVER (ORDER BY ts_rank_cd(tsv, q) DESC) AS rnk
  FROM documents, plainto_tsquery('simple', $3) q
  WHERE tenant_id = $2 AND tsv @@ q LIMIT 50
)
SELECT COALESCE(v.id, k.id) AS id,
       COALESCE(1.0 / (60 + v.rnk), 0) + COALESCE(1.0 / (60 + k.rnk), 0) AS score
FROM vec v FULL OUTER JOIN kw k USING (id)
ORDER BY score DESC LIMIT 10;
```

### 3.6 写入、段与索引重建

三种系统的写入模型不同，决定了「写完能不能立刻查到」和「删除为什么没释放空间」：

- **pgvector**：向量就是行，INSERT 进表同时增量插入 HNSW 图，可见性与 PG 事务一致（read-your-writes）。但 `CREATE INDEX`（非 CONCURRENTLY）持 `ShareLock` 阻塞写，100 万条要几分钟到几十分钟——「上线加索引把库锁死」的经典事故。
- **Qdrant**：写入先落 WAL 再进内存段，段满刷成不可变段，后台 optimizer 合并小段；删除是墓碑，段合并时才真正回收。`wait=true` 才能读到刚写的点。
- **Milvus**：写入进 growing segment（可查），按大小/时间 seal 成 sealed segment 后异步建索引。查询要同时扫 growing + sealed，所以**新数据召回质量低于老数据**；compaction 负责合并小段、清理被删数据。

| | 分片 | 副本 | 注意 |
|---|---|---|---|
| pgvector | 表分区（租户/时间） | 流复制只读副本 | 副本不分摊写，向量写入仍单主 |
| Qdrant | `shard_number` 建集合时定 | `replication_factor` | 1.19 支持在线 resharding，但耗时长 |
| Milvus | 按主键哈希，建集合时定 | 按集群资源分配 | 分片数决定写并行度，**建后不可改** |

**embedding 模型变更 = 全量重建**，这是最容易被低估的运维动作。不同模型的向量空间不可比，两批向量混在一个索引里距离没有意义。正确流程：加 `model` 列 → 双写新旧向量（或新表）→ 后台回填 → 离线评测 → 切读路径 → 保留旧列一个周期再删。维度变化时必须新建索引，`vector(768)` 与 `vector(1024)` 不能共存于一个索引。

---

## 4. 使用实战

### 4.1 建表与 HNSW 索引

```sql
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE documents (
    id         BIGSERIAL PRIMARY KEY,
    tenant_id  BIGINT      NOT NULL,
    content    TEXT        NOT NULL,
    tsv        tsvector GENERATED ALWAYS AS (to_tsvector('simple', content)) STORED,
    embedding  vector(768) NOT NULL,
    model      TEXT        NOT NULL DEFAULT 'bge-m3',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

SET maintenance_work_mem = '2GB';            -- 不够会落盘，慢十倍
SET max_parallel_maintenance_workers = 7;
CREATE INDEX CONCURRENTLY documents_embedding_hnsw
    ON documents USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 200);
```

> `vector` 建 HNSW 索引上限 **2000 维**（`halfvec` 4000、`bit` 64000）。3072 维模型必须降维或改用 `halfvec`。`CONCURRENTLY` 不能放在事务块里，失败会留下 invalid 索引，需 `DROP INDEX` 重来。

### 4.2 Go：pgx 查询与按连接调参

```go
package main

import (
	"context"

	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/pgvector/pgvector-go"
)

type Doc struct {
	ID         int64
	Content    string
	Similarity float32
}

// ef_search 是会话级参数，SET 与查询必须在同一条连接上，否则对 pool 无效
func SearchDocs(ctx context.Context, pool *pgxpool.Pool,
	tenantID int64, query []float32, k int) ([]Doc, error) {

	conn, err := pool.Acquire(ctx)
	if err != nil { return nil, err }
	defer conn.Release()

	if _, err := conn.Exec(ctx, `SET hnsw.ef_search = 100`); err != nil { return nil, err }
	// 0.8.0+：过滤后不足 k 条时继续扫描，缓解 post-filter 掉结果
	if _, err := conn.Exec(ctx, `SET hnsw.iterative_scan = relaxed_order`); err != nil { return nil, err }

	vec := pgvector.NewVector(query)
	rows, err := conn.Query(ctx, `
		SELECT id, content, 1 - (embedding <=> $1) AS similarity
		FROM documents
		WHERE tenant_id = $2
		ORDER BY embedding <=> $1      -- 算子必须与 opclass 一致，写 <-> 就不走索引
		LIMIT $3`, vec, tenantID, k)
	if err != nil { return nil, err }
	defer rows.Close()

	var out []Doc
	for rows.Next() {
		var d Doc
		if err := rows.Scan(&d.ID, &d.Content, &d.Similarity); err != nil { return nil, err }
		out = append(out, d)
	}
	return out, rows.Err()
}
```

三个细节：`SET` 不能写在 `pool.Exec` 上（连接池下一次可能换连接），要么 `Acquire` 要么用 `pgxpool.Config.AfterConnect` 设默认值；`ORDER BY` 的算子必须匹配 opclass；相似度要 `1 - 距离`，直接返回 `<=>` 是距离，下游容易排反。

### 4.3 过滤与部分索引

```sql
-- 高选择性条件用部分索引，从结构上消灭 post-filter
CREATE INDEX CONCURRENTLY documents_t42_hnsw ON documents
    USING hnsw (embedding vector_cosine_ops) WHERE tenant_id = 42;
```

> 部分索引只在查询谓词字面匹配时生效（`WHERE tenant_id = 42`；用 `$1` 时 generic plan 下可能失效）。多租户更稳的做法是按 `tenant_id` HASH 分区，每个分区各建索引。

### 4.4 Qdrant：collection / upsert / 带过滤查询

```go
package main

import (
	"context"
	"fmt"

	"github.com/qdrant/go-client/qdrant"
)

func main() {
	ctx := context.Background()
	cli, err := qdrant.NewClient(&qdrant.Config{Host: "localhost", Port: 6334})
	if err != nil { panic(err) }
	defer cli.Close()

	if _, err := cli.CreateCollection(ctx, &qdrant.CreateCollection{
		CollectionName: "docs",
		VectorsConfig: qdrant.NewVectorsConfig(&qdrant.VectorParams{Size: 768, Distance: qdrant.Distance_Cosine}),
	}); err != nil { panic(err) }

	wait := true // 不加 wait，紧随其后的查询可能读不到
	if _, err := cli.Upsert(ctx, &qdrant.UpsertPoints{
		CollectionName: "docs", Wait: &wait,
		Points: []*qdrant.PointStruct{{
			Id:      qdrant.NewIDNum(1),
			Vectors: qdrant.NewVectors(make([]float32, 768)...),
			Payload: qdrant.NewValueMap(map[string]any{"lang": "go", "tenant": 42}),
		}},
	}); err != nil { panic(err) }

	points, err := cli.Query(ctx, &qdrant.QueryPoints{
		CollectionName: "docs",
		Query:          qdrant.NewQuery(make([]float32, 768)...),
		Filter: &qdrant.Filter{
			Must: []*qdrant.Condition{qdrant.NewMatchKeyword("lang", "go")},
		},
		WithPayload: qdrant.NewWithPayload(true),
		Limit:       qdrant.PtrOf(uint64(5)),
	})
	if err != nil { panic(err) }
	for _, p := range points {
		fmt.Println(p.Id.GetNum(), p.Score, p.Payload["lang"].GetStringValue())
	}
}
```

> 过滤要快，`lang`、`tenant` 必须建 **payload index**，否则退化成逐点判断。1.19 移除了旧版 `/search`、`/recommend`，统一走 `Query`。

### 4.5 Milvus：检索调用与前置顺序

```go
package main

import (
	"context"
	"fmt"

	"github.com/milvus-io/milvus/client/v2/entity"
	"github.com/milvus-io/milvus/client/v2/milvusclient"
)

// 前置顺序：CreateCollection → CreateIndex(HNSW) → LoadCollection，漏一步 Search 就会报 not loaded
func main() {
	ctx := context.Background()
	cli, err := milvusclient.New(ctx, &milvusclient.ClientConfig{Address: "localhost:19530"})
	if err != nil { panic(err) }
	defer cli.Close(ctx)

	sets, err := cli.Search(ctx, milvusclient.NewSearchOption("docs", 5,
		[]entity.Vector{entity.FloatVector(make([]float32, 768))}).
		WithANNSField("embedding").
		WithFilter(`lang == "zh"`).
		WithOutputFields("content"))
	if err != nil { panic(err) }
	for _, set := range sets {
		fmt.Println(set.IDs.FieldData().GetScalars(), set.Scores)
	}
}
```

> Milvus 的 `Upsert` 本质是「先删后插」，主键相同的旧向量在 compaction 前仍占存储；频繁 upsert 要盯段数量与 compaction 进度。3.0 起 schema 可在线加列，但**分片数、分区键仍不可改**。

### 4.6 召回率评测

```go
// recall@k = |ANN_k ∩ Exact_k| / |Exact_k|（Exact 取满 k 条时分母即 k）
func Recall(ann, exact []int64) float64 {
	truth := make(map[int64]struct{}, len(exact))
	for _, id := range exact { truth[id] = struct{}{} }
	hit := 0
	for _, id := range ann { if _, ok := truth[id]; ok { hit++ } }
	return float64(hit) / float64(len(exact))
}
```

`Exact_k` 用关闭索引的精确扫描生成（`SET enable_indexscan = off`），抽 200 条查询即可。评测集必须**按过滤切片统计**（有过滤/无过滤、大租户/小租户）：聚合后的 recall 0.95 常常掩盖「小租户只有 0.6」，而小租户正是过滤后结果不足的重灾区。

---

## 5. 版本演进

| 项目 | 版本 | 关键变化 |
|---|---|---|
| pgvector | **0.5**（2023-08） | HNSW 落地，向量检索真正可用 |
| pgvector | 0.6 / 0.7 | 并行 HNSW 构建；`halfvec`、`sparsevec`、`binary_quantize` |
| pgvector | **0.8**（2024-10） | **迭代索引扫描**，过滤场景不再掉召回 |
| pgvector | 0.8.3–0.8.6（2026） | HNSW vacuum / IVFFlat 构建的**索引正确性**修复，生产建议 ≥ 0.8.4 |
| pgvector | 0.8.7（2026-10） | 当前版本：修复 IVFFlat 构建缓冲区溢出（**CVE-2026-103484**，可导致数据库进程内代码执行）；0.8.6 及以前均受影响，应尽快升级 |
| Milvus | 2.4 / 2.5 | 多向量混合检索、稀疏向量与内置 BM25、partition key |
| Milvus | 2.6（2025–2026） | 稳定性与 compaction 优化，仍是多数生产集群的基线 |
| Milvus | **3.0**（2026-07） | 湖原生架构、外部 Collection、在线 schema 演进；3.0.3 为当前补丁版 |
| Qdrant | **1.19**（2026-10） | TurboQuant、内存分层、在线 resharding；**移除旧 `/search`**，1.10 起统一 `/query` |

迁移注意：

- pgvector 升级包之后必须 `ALTER EXTENSION vector UPDATE`，否则 SQL 里没有新函数；0.8.0 之前建的 HNSW 索引建议 `REINDEX`
- Milvus 2.x → 3.0 是架构级升级，先读 release notes 再动生产；Go SDK 的 `milvus-sdk-go/v2` 已由 `milvus/client/v2`（对应 2.6.x）取代，3.0 需用 `milvus/client/v3`

---

## 6. 常见坑与最佳实践

1. **维度不匹配**：`vector(768)` 写入 1024 维直接报错；Milvus 建集合时维度写死，换模型必须重建集合。把 `dim` 与模型名一起放进配置。
2. **忘记归一化**：用余弦索引却传了未归一化向量，或声称「已归一化」其实没有。在 embedding 服务出口统一归一化，并加断言 `‖v‖ ≈ 1`。
3. **`efSearch` 太低**：默认 40，`k=50` 时结果直接不足。规则是 `ef_search ≥ 2k`，且按请求覆盖而非改全局。
4. **过滤后结果不足 k 条**：post-filter 的必然结果。开 `hnsw.iterative_scan`、建部分索引，或换 Qdrant/Milvus 的下推过滤；用最坏租户验证。
5. **建索引锁表**：`CREATE INDEX`（非 CONCURRENTLY）持 `ShareLock`，大表上等于停写。用 `CONCURRENTLY` + 足够 `maintenance_work_mem`，低峰期做。
6. **embedding 换了没重建**：新旧向量混在一个索引里，距离不可比，结果「有点相关但不对」。加 `model` 列、双写、回填、评测、切换，缺一步都出事。
7. **把向量库当主库**：向量库没有跨实体事务、不保证强一致、备份粒度粗。原文、权限、状态留在 PG/MySQL，向量库只存 id + 向量 + 过滤字段。
8. **top-k 开太大**：`k=1000` 迫使 `ef_search` 拉高，延迟翻几倍，下游 rerank 也扛不住。正确做法是 over-fetch 3–5 倍（`k=50` 取 150）再重排。
9. **删除后空间不释放**：pgvector 的 HNSW 要 VACUUM 才摘除死元组；Qdrant/Milvus 的删除是墓碑，靠段合并回收。监控膨胀率并定期重建。
10. **`ORDER BY` 算子与 opclass 不一致**：建 `vector_cosine_ops` 却用 `<->` 排序，PG 静默全表扫。`EXPLAIN` 里看到 `Seq Scan` 就是它。
11. **在连接池上 `SET` 参数**：`pool.Exec(ctx, "SET hnsw.ef_search=100")` 作用在随机一条连接上，下次查询大概率不生效。
12. **两路分数直接加权**：余弦 `[-1,1]` 与 BM25（无上界）相加没有意义。默认用 RRF，要加权就先做 query 内归一化。
13. **只测平均延迟不测 p99**：HNSW 尾延迟受图连通性影响，p99 可能是 p50 的 5–10 倍。压测报分位值，并带上过滤条件。
14. **跨模型复用相似度阈值**：`0.8` 在模型 A 上意味着「几乎重复」，在模型 B 上可能只是「同主题」。阈值跟模型版本走，写进配置别硬编码。

---

## 7. 选型对比

| 维度 | pgvector | Qdrant | Milvus | OpenSearch k-NN | Redis（RediSearch） |
|---|---|---|---|---|---|
| 数据规模 | 百万级（单机 PG） | 千万级（单机/集群） | **亿级以上** | 千万级 | 百万级（内存受限） |
| 过滤能力 | SQL，需迭代扫描/部分索引 | **强，过滤下推图遍历** | 强，partition key | 强（布尔查询） | 中 |
| 混合检索 | **SQL 内 JOIN 全文** | prefetch + RRF | 多向量 + RRF | BM25 + k-NN 原生 | 有限 |
| 事务/一致性 | **ACID** | 最终一致 | 最终一致 | 近实时刷新 | 单点原子 |
| 内存效率 | 低（无 PQ，仅 halfvec） | **高（多档量化）** | 高（PQ/DiskANN） | 中 | 低（全内存） |
| 水平扩展 | 只读副本 | 分片 + 副本 | **原生存算分离** | 原生分片 | 集群 |
| 运维复杂度 | **最低** | 低 | 高 | 中 | 低 |
| 典型场景 | 业务库附带语义检索 | RAG、重过滤、多租户 | 大规模召回、推荐 | 已有 ES 栈加向量 | 低延迟缓存式检索 |

一句话决策：

- **已在用 PG，向量 < 500 万，过滤条件能用 SQL 表达** → pgvector。少一个系统就是少一半故障面，`JOIN`、事务、权限全部白送。
- **过滤很重（多租户、ACL）、内存紧张、单机要撑到千万级** → Qdrant。filterable HNSW 与量化档位是核心竞争力。
- **亿级向量、要独立扩缩容、有 K8s 运维能力** → Milvus。为规模付运维成本，规模不到就是负收益。
- **已有 OpenSearch/ES 集群，检索以文本为主** → 直接用 [OpenSearch k-NN](../opensearch/opensearch-guide.md)，别为向量再起一套。
- **缓存式低延迟、数据可丢** → Redis，但别把它当唯一存储。

> 最后一条经验：**先用量级与过滤选择性做决策，再用自己的数据跑 recall@10 与 p99**。厂商 benchmark 的配置、数据集、过滤比例都和你不一样。

参考：<https://github.com/pgvector/pgvector> · <https://qdrant.tech/documentation/> · <https://milvus.io/docs/>
