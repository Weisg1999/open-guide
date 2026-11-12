# Elasticsearch 技术介绍与使用指南（Go 后端视角）

> 基于 Lucene 的分布式搜索与分析引擎：倒排索引、分片与副本、近实时刷新、Query DSL 与聚合——同时承担全文检索与日志检索（ELK）两副担子。
> 上游：<https://github.com/elastic/elasticsearch>　|　服务端主线：**8.x / 9.x**（整理于 2026-11）　|　Go 客户端：[elastic/go-elasticsearch](https://github.com/elastic/go-elasticsearch) v8
> 协议：服务端 Elastic License 2.0（另提供 AGPLv3 选项）/ Go 客户端 Apache-2.0；具体版本以上游最新 release 为准。

---

## 1. 项目简介

Elasticsearch 在后端技术栈里通常有**两个身份**：业务侧的全文检索/商品搜索，以及可观测性侧的日志检索（[Zap 篇](../zap/zap-guide.md) 产出的日志经采集器落到这里）。两个场景的建模方式完全不同——前者重相关性与字段设计，后者重写入吞吐、时序索引生命周期与成本控制。

它的核心是 **Lucene 倒排索引 + 分布式分片**：把文档按字段拆成词条，建立「词 → 文档列表」的映射，再把这个索引切成多个分片分散到节点，查询时并行散射/汇聚。

- **适用场景**：全文检索与模糊匹配、多条件过滤 + 排序、日志与 trace 检索、聚合分析（Terms / Date Histogram）、向量检索与混合排序
- **不适合**：当主数据库用（无事务、删除是标记、刷新有延迟、跨文档一致性弱）、频繁逐字段更新的场景、复杂多表 JOIN、要求强一致与精确去重计数、纯时序指标的长期监控（用 [Prometheus](../prometheus/prometheus-guide.md)）

> 与 [MySQL 篇](../mysql/mysql-guide.md) 的分工：MySQL 负责「写入正确」，ES 负责「查得快、查得模糊」。常见架构是 MySQL 为源，通过 CDC / 双写把检索字段同步到 ES，并接受最终一致。

### 安装与本地起服务

```bash
go get github.com/elastic/go-elasticsearch/v8
docker run --rm -d --name es-dev -p 9200:9200 \
  -e "discovery.type=single-node" \
  -e "xpack.security.enabled=false" \
  -e "ES_JAVA_OPTS=-Xms1g -Xmx1g" \
  docker.elastic.co/elasticsearch/elasticsearch:8.15.0
curl http://localhost:9200   # 确认集群可达
```

> 镜像标签仅为示例，请换成当前 8.x/9.x 最新补丁版。**8.x 起默认开启 TLS 与认证**，本地示例才显式关闭；生产不要照抄这两行环境变量。

### 最小可运行示例

```go
package main

import (
    "fmt"
    "strings"

    "github.com/elastic/go-elasticsearch/v8"
)

func main() {
    es, err := elasticsearch.NewClient(elasticsearch.Config{
        Addresses: []string{"http://localhost:9200"},
    })
    if err != nil { panic(err) }

    res, err := es.Info() // 建连探活
    if err != nil { panic(err) }
    defer res.Body.Close()
    fmt.Println("cluster:", res.Status())

    _, _ = es.Index("articles", strings.NewReader(`{"title":"Go 并发编程","tags":["go"]}`),
        es.Index.WithDocumentID("1"),
        es.Index.WithRefresh("true")) // 演示用；生产不要逐条 refresh
}
```

---

## 2. 功能特性总览

| 能力 | 说明 |
|---|---|
| 倒排索引 | term → posting list，FST 词典加速；`doc_values` 列式结构服务排序与聚合 |
| 字段类型 | `text`（分词，全文）/ `keyword`（不分词，精确过滤与聚合）/ 数值 / 日期 / `nested` / `object` / `geo_point` / `dense_vector` |
| 分析器 | Character Filter → Tokenizer → Token Filter；内置 standard、可装 IK 等中文分词插件 |
| 查询 DSL | `match` / `term` / `range` / `bool`（must / filter / should / must_not）/ `multi_match` / `nested` / `script` |
| 聚合 | 桶聚合（terms / date_histogram / range）、指标聚合（avg / sum / percentiles）、管道聚合 |
| 分页 | `from + size`（上限 1 万）、`scroll`（快照）、`search_after` + PIT（实时深翻页） |
| 写入 | 单条 Index / `Bulk` NDJSON 批量、乐观并发（`if_seq_no` / `if_primary_term`）、Ingest Pipeline 预处理 |
| 分布式 | 主分片 + 副本、路由 `hash(_routing) % 主分片数`、scatter/gather 两阶段查询 |
| 近实时 | `refresh_interval` 默认 1s、translog、flush、segment merge |
| 索引治理 | Alias、Rollover、ILM（hot/warm/cold/delete）、索引模板与 component template |
| Go SDK | `esapi` 底层请求、`es.Index/Search/Bulk` 便捷方法、TypedClient、`elastictransport` 可配重试与连接池 |

---

## 3. 核心概念与原理

### 3.1 倒排索引：为什么全文检索快

```
文档 1: "Go 并发编程"     ──分词──► [go, 并发, 编程]
文档 2: "Go Web 开发"     ──分词──► [go, web, 开发]

倒排索引:
  go    → [1, 2]
  并发  → [1]
  web   → [2]
  ...   （词典用 FST 压缩常驻内存；posting list 存 docID + 词频 + 位置）

查询 "go 并发" → 取两个 posting list 求交/求并 → 命中文档 → BM25 打分 → 排序
```

三个容易混淆的结构：

| 结构 | 用途 | 特点 |
|---|---|---|
| 倒排索引 | 从「词」找「文档」 | 搜索快，但按文档取字段值很慢 |
| `doc_values` | 从「文档」找「字段值」 | **列式**存储，排序、聚合、脚本取值走它，默认对 keyword/数值/日期开启 |
| `_source` | 原始 JSON | 返回命中内容、`update` / reindex 依赖它；关闭后无法高亮与重建 |

`text` 字段默认**没有** `doc_values`，对它做 terms 聚合会报错——需要精确聚合时用 `.keyword` 多字段（multi-field）。

### 3.2 分析器与 text / keyword

```
"Hello-Go 2026"
  → Character Filter（如 html_strip）
  → Tokenizer（standard → ["Hello", "Go", "2026"]）
  → Token Filter（lowercase → ["hello", "go", "2026"]）
```

- `text`：走分析器，用于 `match` 全文检索；**不能用 `term` 精确匹配**（存的是词条，不是原文）
- `keyword`：不分词，整串入索引，用于 `term` 精确过滤、排序、聚合
- 中文默认 standard 分词器按单字切，检索效果差 → 装 IK（`ik_max_word` 建索引 / `ik_smart` 查询）

### 3.3 近实时（NRT）与写入链路

```
写入请求
  ├─► 写 translog（追加，保证崩溃可恢复）
  └─► 写 index buffer（内存，此时还搜不到）
          │ 每 refresh_interval（默认 1s）生成新的可搜索 Lucene segment
          ▼
      可搜索（这就是「近实时」而非实时的原因）
          │ flush：buffer 落盘、清空 translog
          ▼
      后台 segment merge（合并小段、真正清除标记删除的文档）
```

关键推论：

- 写入后立刻查可能查不到 → 测试可用 `refresh=wait_for`，**生产不要逐条 refresh**（每次都生成小 segment，写入吞吐暴跌）
- 删除是**打标记**，空间要到 merge 才释放
- `refresh_interval` 调到 30s~120s 能显著提升日志写入吞吐，代价是可见性延迟

### 3.4 分片、副本与路由

```
索引 articles (3 主分片, 1 副本)
   shard 0 [primary@nodeA, replica@nodeB]
   shard 1 [primary@nodeB, replica@nodeC]
   shard 2 [primary@nodeC, replica@nodeA]

写入路由: shard = hash(_routing) % 3      （_routing 默认 = _id）
```

- **主分片数创建后不可改**（改要 reindex 或 split），副本数可随时改
- 分片不是越小越好：每分片都有文件句柄、内存与线程开销。常见经验值是单分片 20~50GB（日志场景可更大）
- 查询是 **scatter/gather**：协调节点把请求发给所有相关分片 → 各分片本地查询取 topN → 汇聚排序 → 再按 ID 去取 `_source`（query then fetch）。分片越多，扇出成本越高

### 3.5 BM25 与相关性评分

默认相似度是 BM25（词频 TF、逆文档频率 IDF、字段长度归一化）。注意 **IDF 按分片本地统计**，分片数多且文档分布不均时评分可能失真；小索引可用 `search_type=dfs_query_then_fetch` 先全局收集词频，代价是多一次往返。

`bool` 查询里 **`filter` 不参与评分且结果可缓存**，`must` 参与评分。把「时间范围、状态、租户」这类纯过滤条件放 `filter` 是最容易拿到的性能收益。

### 3.6 深分页的三种方式

| 方式 | 原理 | 适用 | 限制 |
|---|---|---|---|
| `from + size` | 各分片取 `from+size` 条再汇聚 | 前几页 | `from + size ≤ 10000`（`max_result_window`），越深越慢 |
| `scroll` | 创建搜索上下文快照 | 离线全量导出 | 占资源、不反映期间的新写入 |
| **`search_after` + PIT** | 用上一页排序值作游标，PIT 保证一致性 | 实时深翻页 | 不能随机跳页；需稳定唯一排序键（常加 `_id` 或 `_shard_doc`） |

### 3.7 聚合的代价

桶聚合在分片上先算局部 topN 再汇聚，因此 `terms` 聚合默认**不保证精确**（`doc_count_error_upper_bound` 会告诉你误差）。高基数字段（用户 ID）做 terms 聚合会吃大量内存，需调 `shard_size`、限定 `size`，或改用 `composite` 聚合分页遍历。

---

## 4. 使用实战

### 4.1 客户端初始化

```go
package search

import (
    "github.com/elastic/go-elasticsearch/v8"
)

func New(addresses []string, user, pass string) (*elasticsearch.Client, error) {
    return elasticsearch.NewClient(elasticsearch.Config{
        Addresses: addresses,
        Username:  user,
        Password:  pass,
        // 生产：配 CACert / Transport TLS；日志场景可调大连接池与重试
        // RetryOnStatus: []int{429, 502, 503, 504},
        // MaxRetries:    3,
    })
}
```

> 复用同一个 `Client`（内部持有连接池），不要每次查询新建。`esapi.Response` **必须 `defer res.Body.Close()`**，否则连接不会归还。

### 4.2 建索引与 Mapping（显式优于动态）

```go
const mapping = `{
  "settings": {
    "number_of_shards": 3,
    "number_of_replicas": 1,
    "refresh_interval": "30s",
    "analysis": {
      "analyzer": {
        "ik_index": {"type":"custom","tokenizer":"ik_max_word"},
        "ik_query": {"type":"custom","tokenizer":"ik_smart"}
      }
    }
  },
  "mappings": {
    "dynamic": "strict",
    "properties": {
      "title":    {"type":"text","analyzer":"ik_index","search_analyzer":"ik_query",
                   "fields":{"raw":{"type":"keyword","ignore_above":256}}},
      "status":   {"type":"keyword"},
      "tenant_id":{"type":"keyword"},
      "price":    {"type":"scaled_float","scaling_factor":100},
      "tags":     {"type":"keyword"},
      "created_at":{"type":"date","format":"strict_date_optional_time||epoch_millis"}
    }
  }
}`

func CreateIndex(es *elasticsearch.Client, name string) error {
    res, err := es.Indices.Create(name,
        es.Indices.Create.WithBody(strings.NewReader(mapping)))
    if err != nil { return err }
    defer res.Body.Close()
    if res.IsError() { return fmt.Errorf("create index: %s", res.String()) }
    return nil
}
```

`dynamic: "strict"` 让未声明字段直接报错，是防「mapping 爆炸」最有效的一行配置。

### 4.3 Bulk 批量写入（唯一正确的批量姿势）

```go
func BulkIndex(es *elasticsearch.Client, index string, docs []Article) error {
    var buf bytes.Buffer
    for _, d := range docs {
        meta := map[string]any{
            "index": map[string]any{"_index": index, "_id": d.ID},
        }
        if err := json.NewEncoder(&buf).Encode(meta); err != nil { return err }
        if err := json.NewEncoder(&buf).Encode(d); err != nil { return err }
    }
    res, err := es.Bulk(bytes.NewReader(buf.Bytes()),
        es.Bulk.WithIndex(index))
    if err != nil { return err }
    defer res.Body.Close()
    if res.IsError() { return fmt.Errorf("bulk: %s", res.String()) }

    var r struct {
        Errors bool `json:"errors"`
        Items  []map[string]struct {
            Status int `json:"status"`
            Error  any `json:"error"`
        } `json:"items"`
    }
    if err := json.NewDecoder(res.Body).Decode(&r); err != nil { return err }
    // HTTP 200 不代表全部成功，必须逐条检查 errors/items
    if r.Errors { return fmt.Errorf("bulk partial failure: %+v", r.Items) }
    return nil
}
```

> Bulk 请求体是 **NDJSON：每个 JSON 占一行**，`json.Encoder.Encode` 自动补换行。单批控制在 5~15MB；遇到 429（`es_rejected_execution_exception`）要指数退避重试，而不是加大并发。

### 4.4 Bool 查询：filter 与 must 的正确分工

```go
func Search(es *elasticsearch.Client, index, kw, tenant string, minPrice float64) ([]Article, error) {
    q := map[string]any{
        "size": 20,
        "query": map[string]any{
            "bool": map[string]any{
                "must": []any{
                    map[string]any{"multi_match": map[string]any{
                        "query": kw, "fields": []string{"title^3", "tags"},
                    }},
                },
                // 纯过滤条件放 filter：不评分、可缓存
                "filter": []any{
                    map[string]any{"term": map[string]any{"tenant_id": tenant}},
                    map[string]any{"term": map[string]any{"status": "published"}},
                    map[string]any{"range": map[string]any{
                        "price": map[string]any{"gte": minPrice},
                    }},
                },
            },
        },
        "sort": []any{
            map[string]any{"_score": map[string]any{"order": "desc"}},
            map[string]any{"id": map[string]any{"order": "asc"}}, // 为 search_after 提供稳定 tiebreaker
        },
        "aggs": map[string]any{
            "by_tag": map[string]any{
                "terms": map[string]any{"field": "tags", "size": 10},
            },
        },
    }
    var buf bytes.Buffer
    if err := json.NewEncoder(&buf).Encode(q); err != nil { return nil, err }

    res, err := es.Search(
        es.Search.WithIndex(index),
        es.Search.WithBody(&buf),
        es.Search.WithTrackTotalHits(true),
    )
    if err != nil { return nil, err }
    defer res.Body.Close()
    if res.IsError() { return nil, fmt.Errorf("search: %s", res.String()) }

    var out struct {
        Hits struct {
            Total struct{ Value int `json:"value"` } `json:"total"`
            Hits  []struct {
                Source Article `json:"_source"`
                Sort   []any   `json:"sort"`
            } `json:"hits"`
        } `json:"hits"`
    }
    if err := json.NewDecoder(res.Body).Decode(&out); err != nil { return nil, err }
    articles := make([]Article, 0, len(out.Hits.Hits))
    for _, h := range out.Hits.Hits { articles = append(articles, h.Source) }
    return articles, nil
}
```

### 4.5 深分页：search_after + PIT

```go
// 1) 打开 PIT，保证翻页期间视图一致
pitRes, _ := es.OpenPointInTime(
    es.OpenPointInTime.WithIndex([]string{"articles"}),
    es.OpenPointInTime.WithKeepAlive("2m"))
// 解析出 pitRes 中的 id ...

// 2) 每页带上上一页最后一条的 sort 值
q := map[string]any{
    "size": 20,
    "pit":  map[string]any{"id": pitID, "keep_alive": "2m"},
    "sort": []any{"_shard_doc"}, // 用 PIT 时可用内置稳定游标，无需业务唯一键
    "search_after": []any{lastSortValue},
}
// 3) 结束后 es.ClosePointInTime(...) 释放上下文，别等它自己过期
```

> `sort` 不用 `_shard_doc` 时，必须拼一个唯一字段（如 `created_at` + `_id`）做 tiebreaker，否则相同排序值的文档可能重复或丢失。

### 4.6 时序索引：Alias + Rollover + ILM

日志场景不要「一天一个索引名让应用自己切」，用固定写入别名 + 自动滚动：

```json
PUT _index_template/logs-template
{
  "index_patterns": ["app-logs-*"],
  "template": {
    "settings": {"number_of_shards": 2, "number_of_replicas": 1,
                 "index.lifecycle.name": "logs-policy"},
    "mappings": {"dynamic": "strict", "properties": {
      "ts": {"type":"date"}, "level": {"type":"keyword"},
      "trace_id": {"type":"keyword"}, "msg": {"type":"text"}
    }}
  }
}
```

```bash
# 首次创建带写别名
PUT app-logs-000001
{"aliases":{"app-logs-write":{"is_write_index":true}}}

# ILM：热阶段 50GB / 1 天滚动，30 天后删除
```

应用始终写 `app-logs-write`，查询用 `app-logs-*`；配合 [Zap 篇](../zap/zap-guide.md) 输出的 `trace_id`，可以一条检索把日志与 [OpenTelemetry](../opentelemetry/opentelemetry-guide.md) 链路对上。

### 4.7 错误处理清单

```go
defer res.Body.Close()
if res.IsError() {
    body, _ := io.ReadAll(res.Body)
    // 429 / 503 → 退避重试；4xx → 修查询或 mapping，不要重试
    return fmt.Errorf("es %s: %s", res.Status(), body)
}
// Bulk 即使 200 也要检查 body.errors（见 4.3）
```

---

## 5. 版本演进速览

| 版本 | 关键变化与迁移注意点 |
|---|---|
| 5.x / 6.x | 一个索引可有多个 mapping type；6.x 起限制单 type，为移除做准备 |
| **7.x** | **彻底移除 mapping type**（`_type`）；主分片默认 1；Zen2 集群协调、ILM 成熟；引入 `search_after` 改进 |
| **8.x** | **默认开启安全（TLS + 内置用户认证）**；Java 17 运行时；API 清理与部分字段重命名；kNN 向量检索 GA；Go 客户端切到 `/v8` 模块并新增 TypedClient |
| 9.x | 继续简化配置与查询性能、增强向量/混合检索与 serverless 形态 |

**许可证变化务必留意**：7.10 及之前为 Apache-2.0；7.11 起改为 **Elastic License 2.0**，社区随后 fork 出 **OpenSearch**（Apache-2.0）；Elastic 后来又提供 AGPLv3 作为可选许可。商业集成、二次分发前请自行核对当前许可条款，不要沿用老教程里「ES 是 Apache-2.0」的说法。

**升级注意**：

- 6.x → 7.x：拆 type、改客户端（`/v7`）、检查 `include_type_name` 相关 API
- 7.x → 8.x：**必须配置 TLS/认证或显式关闭**，否则客户端连不上；Go 客户端从 `github.com/elastic/go-elasticsearch/v7` 换到 `/v8`，`esapi` 请求构造方式变化
- 服务端与客户端大版本要对齐；跨大版本混用会出现兼容性头校验失败

---

## 6. 常见坑与最佳实践

1. **动态映射字段爆炸**：任由业务写入任意字段，`mapping` 字段数暴涨 → 集群状态膨胀、内存吃紧甚至 `limit of total fields exceeded`。设 `dynamic: strict` 或 `false` + `index.mapping.total_fields.limit`。
2. **`text` 与 `keyword` 用反**：对 `text` 用 `term` 精确查（查不到）、对 `text` 做 terms 聚合（报错）。精确过滤/排序/聚合一律用 `.keyword` 多字段。
3. **把 ES 当主数据库**：无跨文档事务、删除是标记、刷新有延迟、副本默认异步。**源数据放 [MySQL](../mysql/mysql-guide.md)**，ES 只作检索副本并设计好重放全量重建的流程。
4. **写后立刻查不到**：NRT 默认 1s 刷新。测试用 `refresh=wait_for`；业务的「写后读自己」应回源库读，不要靠强制刷新掩盖一致性问题。
5. **生产逐条写入 / 逐条 refresh**：两者都是吞吐杀手。用 Bulk（5~15MB）+ 合理的 `refresh_interval`。
6. **`from + size` 深翻页**：超过 1 万条直接报错，之前也会线性变慢。用 `search_after` + PIT，并记得关闭 PIT。
7. **主分片数拍脑袋**：创建后不能改，只能 reindex。按数据总量与增长预估（单分片 20~50GB），日志类可用 Rollover 自动扩。
8. **不设 ILM / Rollover**：时序索引无限增长或天天手工建索引。用固定写别名 + ILM 自动滚动与过期删除。
9. **`filter` 写成 `must`**：纯过滤条件参与评分、无法命中 query cache，白白浪费 CPU。
10. **前缀通配 `*`、正则、`script` 查询**：全词典扫描，容易把集群打挂。用 `match_phrase` / `n-gram` / 预计算字段替代。
11. **高基数聚合**：对 `user_id` 做 terms、`size` 设几万、嵌套多层聚合 → 节点 OOM。用 `composite` 分页、限 `shard_size`，或把明细交给 [ClickHouse 类 OLAP](https://clickhouse.com/)。
12. **`_source` 过大 / `SELECT *` 习惯**：只取需要的字段用 `_source_includes`，聚合/排序用 `docvalue_fields`，别把大文档全量拉回应用。
13. **Bulk 只判 HTTP 状态**：200 也可能是部分失败，必须检查响应体 `errors` 与 `items`。
14. **429 硬加重试并发**：写入队列满了应指数退避并降速，加大并发只会更糟。
15. **中文用默认分词**：standard 按单字切，搜索「并发」会命中包含「并」「发」的无关文档。装 IK 并区分建索引/查询分析器。
16. **分片数影响相关性**：小索引多分片时 IDF 按分片统计导致评分偏差，必要时 `dfs_query_then_fetch`。
17. **裸奔到公网**：8.x 默认开安全是有原因的，历史上大量数据泄露来自无认证的 9200 端口。生产开 TLS + RBAC + 网络隔离。

---

## 7. 选型对比

| 维度 | Elasticsearch | OpenSearch | Solr | ClickHouse | Loki | Meilisearch / Typesense |
|---|---|---|---|---|---|---|
| 定位 | 搜索 + 日志分析 | ES 7.10 的 Apache-2.0 fork | 传统企业搜索 | 列存 OLAP | 日志检索（只索引标签） | 轻量即时搜索 |
| 协议 | **ELv2 / AGPLv3** | **Apache-2.0** | Apache-2.0 | Apache-2.0 | AGPLv3 | MIT / GPL |
| 相关性 / DSL | **最成熟** | 接近（逐步分化） | 强 | 弱（无倒排为主） | 无全文相关性 | 简单、开箱即用 |
| 聚合分析 | 强 | 强 | 中 | **极强（SQL）** | 弱（LogQL） | 弱 |
| 资源占用 | 高（JVM） | 高 | 高 | 中 | **低** | 低 |
| 运维复杂度 | 中～高 | 中～高 | 高 | 中 | 低 | **极低** |
| 典型场景 | 检索 + ELK 一体化 | 许可敏感 / 自建搜索 | 存量 Lucene 栈 | 日志/行为分析报表 | K8s 日志（配合 Grafana） | 中小站站内搜索 |

一句话决策：

- **既要业务检索又要日志分析、团队能承担 JVM 运维** → Elasticsearch；生态、文档与云托管最全。
- **许可合规要求纯 Apache-2.0、或希望完全社区治理** → OpenSearch，API 大体兼容但新特性已分化，客户端要用对应的 fork 版本。
- **只做 Kubernetes 日志检索、成本敏感** → **Loki**：只索引标签、原始日志压缩存对象存储，比 ES 省一个数量级，代价是全文检索能力弱。
- **日志/行为数据主要做 SQL 聚合报表** → ClickHouse，写入与压缩比更强；但别指望它做相关性排序。
- **只是一个中小站的搜索框** → Meilisearch / Typesense，零 JVM、几分钟上线，没必要上 ES 集群。
- **主数据、事务、强一致** → [MySQL](../mysql/mysql-guide.md)；**高频 KV 缓存** → [go-redis](../redis/redis-guide.md)；**指标趋势与告警** → [Prometheus](../prometheus/prometheus-guide.md)。

> 一个常被忽略的成本项：ES 的「实时性」是用 refresh 频率换来的，「灵活性」是用 mapping 字段数换来的，「聚合精度」是用内存换来的。上线前先明确业务能接受哪三个代价，再定 `refresh_interval`、`dynamic` 与分片数。
