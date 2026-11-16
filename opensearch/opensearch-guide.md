# OpenSearch 技术介绍与使用指南

> 从 Elasticsearch 7.10.2 分叉、全程 Apache-2.0 的社区驱动搜索与分析套件：保留 Lucene 检索内核，把安全、SQL/PPL、ISM、向量与神经检索做成开箱即用的内置插件。
> 仓库：<https://github.com/opensearch-project/OpenSearch>　|　当前主线：**3.x**（最新稳定 **3.9.0**，2026-09-29 发布，Apache-2.0 协议）

---

## 1. 项目简介

[ES 篇](../elasticsearch/elasticsearch-guide.md) 讲「Lucene 引擎怎么工作」——倒排索引、segment、NRT、分片与 Query DSL；本篇讲**同一套代码在 2021 年分叉之后归谁治理、多了什么、该怎么选**：内核同源，差异几乎都在许可、插件体系与演进路线上，原理层请直接读 ES 篇第 3 章，本篇不重复。

**分叉来龙去脉**：2021 年 1 月 Elastic 宣布自 7.11 起把 ES/Kibana 从 Apache-2.0 改为 **SSPL + Elastic License 2.0 双许可**，动机是阻止云厂商把 ES 当托管服务售卖。4 月，AWS 联合 Red Hat、SAP、Capital One、Logz.io 等，从**最后一个 Apache-2.0 版本 ES 7.10.2 / Kibana 7.10.2** 创建分支，命名 OpenSearch / OpenSearch Dashboards，全栈 **Apache-2.0**，并把 Open Distro for Elasticsearch 的插件（Security、Alerting、Anomaly Detection、SQL、ISM）整体迁入。7 月 12 日发布 **1.0 GA**（首个生产就绪版，含 Data Streams），9 月 Amazon Elasticsearch Service 更名 Amazon OpenSearch Service。2024 年 9 月治理权移交 **Linux 基金会旗下 OpenSearch Software Foundation**。一句话概括：**OpenSearch = ES 7.10 内核 + 原 Open Distro 企业插件 + Apache-2.0 + 独立演进路线**。

- **适用**：要求纯 Apache-2.0 合规、把搜索嵌入自有产品或对外提供托管服务、自建且希望安全/RBAC/告警/SQL/CCR 零授权费、向量检索与 RAG、日志与可观测性
- **不适合**：依赖 ES 8/9 独占特性（ES|QL、`semantic_text`、ELSER、原厂 APM/SIEM）、要原厂商业支持、需与上游版本同步迭代

### 安装与本地起服务

```bash
go get github.com/opensearch-project/opensearch-go/v2
docker run --rm -d --name os-dev -p 9200:9200 \
  -e "discovery.type=single-node" \
  -e "OPENSEARCH_INITIAL_ADMIN_PASSWORD=YourStr0ng!Pass" \
  -e "OPENSEARCH_JAVA_OPTS=-Xms1g -Xmx1g" \
  opensearchproject/opensearch:3.9.0
curl -ku admin:'YourStr0ng!Pass' https://localhost:9200
```

> 镜像标签仅为示例，请换当前 3.x 最新补丁版。**3.x 起初始管理员口令必须经环境变量设置**，不再有固定 `admin/admin`；`-k` 仅用于本地自签证书，生产配 CA。

---

## 2. 功能特性总览

| 能力 | 说明 |
|---|---|
| 检索内核 | Apache Lucene（9.x 代起），倒排索引 + `doc_values` + `_source`；与 ES 同源 |
| 字段类型 | `text` / `keyword` / 数值 / 日期 / `nested` / `geo_point` / `knn_vector` / `rank_feature` |
| 查询 DSL | 对齐 ES 7.10：`match` / `term` / `range` / `bool` / `multi_match` / `nested`；聚合、深分页同构 |
| 向量检索 | **k-NN 插件**：`knn_vector` + `knn`；HNSW / IVF，Faiss / NMSLIB / Lucene 三引擎，PQ/SQ 量化 |
| 神经与混合检索 | **ML Commons + Neural Search**：`text_embedding` processor、`neural` 查询、search pipeline 归一化 + RRF、neural sparse（3.9 原生 SEISMIC 引擎） |
| 查询语言 | **SQL 插件**（`SELECT`、JDBC/ODBC）与 **PPL**（管道式日志分析，3.x 默认 Apache Calcite 引擎） |
| 索引治理 | **ISM**（对应 ES 的 ILM 但 API 不同）、索引模板、Rollover、**Data Streams** |
| 分层存储 | **可搜索快照**：直接搜索远程对象存储中的快照，无需先 restore |
| 安全 | **Security 插件**内置免费：TLS、RBAC、文档级/字段级安全（DLS/FLS）、字段脱敏、审计日志、多租户、LDAP/SAML/OIDC |
| 可观测与告警 | Alerting、Anomaly Detection、Trace Analytics、Observability、Query Insights、CCR；OpenSearch Dashboards（Kibana 7.10.2 分叉） |
| Go 客户端 | `opensearch-go`（分叉自 `go-elasticsearch` v7 时代）；与 v8 不通用，含 AWS SigV4 signer |
| 许可与治理 | 全栈 **Apache-2.0**；Linux 基金会下社区治理，可自由嵌入、转售、作为托管服务提供 |

---

## 3. 核心概念与原理

### 3.1 与 ES 共用的 Lucene 内核（简述）

倒排索引（term → posting list、FST 词典）、`doc_values`、`_source`、分析器三段式、`text`/`keyword` 区别，以及 `refresh_interval` → 可搜索 segment → translog → flush → segment merge 的 **NRT 写入链路**，两者**完全一致**。「写后查不到」「删除是标记」「生产别逐条 refresh」原样成立。**分叉没有改内核，改的是内核之外的东西**——细节见 [ES 篇 3.1–3.3](../elasticsearch/elasticsearch-guide.md)。

### 3.2 集群、分片路由与副本

`shard = hash(_routing) % 主分片数`、主分片数创建后不可改、副本随时改、scatter/gather 的 query-then-fetch 两阶段——同样与 ES 一致（见 [ES 篇 3.4](../elasticsearch/elasticsearch-guide.md)）。差异在**复制方式**：除默认**文档复制**（主分片转发文档给副本各自重放索引）外，OpenSearch 提供 **segment replication（段复制）**，主分片 merge 后把成品段文件拷给副本，副本不再重复分词建索引：副本 CPU 更低、写入吞吐更高，代价是故障切换与可见性延迟略高。

### 3.3 k-NN 向量检索

OpenSearch 2019 年即在搜索引擎中率先支持 k-NN，分叉后继续加码。核心是 `knn_vector` 字段 + `knn` 查询：

```json
PUT /rag-docs
{
  "settings": { "index": { "knn": true, "number_of_shards": 2, "number_of_replicas": 1 } },
  "mappings": { "dynamic": "strict", "properties": {
    "title":     { "type": "text" },
    "tenant":    { "type": "keyword" },
    "embedding": { "type": "knn_vector", "dimension": 768,
      "method": { "name": "hnsw", "engine": "faiss", "space_type": "l2",
                  "parameters": { "ef_construction": 256, "m": 16 } } }
  } }
}
```

- **精确 vs 近似**：`script_score` 暴力余弦/L2 是精确 k-NN；**ANN** 靠 HNSW/IVF，亿级向量仍可毫秒级返回，代价是召回率 < 100%。
- **HNSW**：分层近邻图，`m`（每节点连接数 2–100）、`ef_construction`（建图候选集）、`ef_search`（查询候选集）；内存换质量，默认首选。
- **IVF**：聚类分桶（`nlist`），查询只扫 `nprobes` 个桶；**需 Train API 训练模型**，内存更省、索引更快，质量略低。
- **三引擎**：**Faiss**（大规模首选，支持 HNSW/IVF 与 PQ/SQ 量化）、**Lucene**（千万级以下延迟与体积更优、支持智能过滤）、**NMSLIB**（早期实现，3.x 已 **deprecated**，新项目别选）。
- **量化与过滤**：`fp16` 省一半内存、`byte` 省 3/4、PQ 更省但有损，3.x 另有 1/2/4-bit 标量量化；2.4 起 adaptive filtering 自动在 pre/post-filter 与精确 k-NN 间择优。

```json
POST /rag-docs/_search
{ "query": { "knn": { "embedding": { "vector": [0.12, 0.48, "..."], "k": 10 } } } }
```

> `dimension` 必须与嵌入模型输出维度一致；`space_type`（`l2`/`cosinesimil`/`innerproduct`）要与模型训练口径一致，否则排序系统性偏差。每个分片、每个 segment 各返回 `k` 条再汇聚，`size` 才是最终条数。

### 3.4 神经网络搜索与混合检索

k-NN 要调用方自算查询向量；**Neural Search** 把「生成向量」搬进集群：ML Commons 托管嵌入模型，ingest pipeline 用 `text_embedding` processor 建索引时把文本转向量，`neural` 查询只传原始文本即可语义检索。

```json
PUT /_ingest/pipeline/embedding-pipeline
{ "processors": [{ "text_embedding": { "model_id": "<id>", "field_map": { "title": "title_vec" } } }] }
POST /rag-docs/_search
{ "query": { "neural": { "title_vec": { "query_text": "Go 并发编程", "k": 10 } } } }
```

**混合检索**（BM25 + 向量）把 `match` 与 `neural` 同放 `hybrid.queries`，用 `search_pipeline` 挂 `normalization-processor` 以 **RRF（Reciprocal Rank Fusion）** 或 z-score 归一化融合两路结果，通常比单一路更稳。另有 **neural sparse**（学习式稀疏检索），3.9 起用 C++ 原生 **SEISMIC** 引擎，堆占用更小、吞吐更高。这整套是 OpenSearch 相对 ES 7.10 最显著的增量。

### 3.5 ISM 与 Data Streams

**ISM（Index State Management）**来自 Open Distro，对应 ES 的 **ILM** 但 API/策略格式完全不同——迁移不能照抄 ILM。策略是状态机：每个 state 带 `actions`（`rollover`、`force_merge`、`read_only`、`replica_count`、`snapshot`、`delete` 等）与 `transitions`（条件如 `min_index_age`/`min_doc_count`/`min_size` 满足才进入下一状态），由 Job Scheduler 周期调度，用 `ism_template` 自动套到新索引。

```json
PUT _plugins/_ism/policies/logs-policy
{ "policy": { "default_state": "hot", "states": [
  { "name": "hot", "actions": [{ "rollover": { "min_size": "50gb", "min_index_age": "1d" } }],
    "transitions": [{ "state_name": "warm", "conditions": { "min_index_age": "7d" } }] },
  { "name": "warm", "actions": [{ "force_merge": { "max_num_segments": 1 } }],
    "transitions": [{ "state_name": "delete", "conditions": { "min_index_age": "30d" } }] },
  { "name": "delete", "actions": [{ "delete": {} }] }
], "ism_template": [{ "index_patterns": ["app-logs-*"], "priority": 100 }] } }
```

**Data Streams**（1.0 引入）面向只追加时序数据：写入固定名称，背后是隐藏 `.ds-*` 后备索引 + 写别名，配合 ISM `rollover` 自动滚动。日志场景应「Data Stream + 固定写别名 + ISM」，别让应用按天拼索引名。

### 3.6 分叉后的内置能力：Security、SQL/PPL 与可搜索快照

- **Security 插件**：默认随发行版启用（HTTPS + 认证），配置在 `plugins.security.*`，核心是 `internal_users`/`roles`/`roles_mapping`/`action_groups` 四张表；特色是**细粒度访问控制**（DLS 文档级、FLS 字段级、字段脱敏）与 Dashboards 多租户且**全部免费**（ES 侧多属付费订阅），外部认证支持 LDAP/AD、SAML、OIDC、Kerberos、代理认证，并有审计日志。
- **SQL 与 PPL**：SQL 插件把 `SELECT ... FROM index` 翻译成 DSL，提供 JDBC/ODBC；**PPL** 用管道语法面向日志探索，3.x 以 **Apache Calcite** 为默认引擎：

```sql
SELECT tenant, count(*) AS cnt FROM rag-docs WHERE tenant = 'acme' GROUP BY tenant ORDER BY cnt DESC LIMIT 10
```

```text
source = app-logs-* | where level = 'ERROR' | stats count() by service | sort - count | head 20
```

- **可搜索快照**：把快照仓库（S3/GCS/Azure）中的索引直接挂成可搜索索引，按需从对象存储拉取并本地缓存，不必先 restore。冷数据降本关键，常配 ISM 冷阶段；**只适合低频查询**，别当热数据用。

---

## 4. 使用实战

### 4.1 客户端与差异

`opensearch-go` 分叉自 `go-elasticsearch` v7 时代代码，保留 `opensearch.Config` / `opensearchapi.*Request` / `.Do(ctx, client)`。与 v8 的关键差异：**v8 客户端握手做 product check**（`X-Elastic-Product: Elasticsearch`），连 OpenSearch 直接报错，反之亦然——**必须用配套客户端且大版本对齐**。`Request` 接口只有 `Do(ctx, Transport) (*Response, error)`，`Transport` 只要求 `Perform(*http.Request)`；`*opensearch.Client` 同时充当两者，故既能用结构化请求对象，也能用 `client.Transport.Perform` 发任意 REST 请求。复用同一个 `Client`（内部持有连接池），别每次新建，所有 `Response.Body` 都 `defer Close()`。

### 4.2 初始化与探活

```go
package main

import (
    "crypto/tls"
    "fmt"
    "log"
    "net/http"
    "github.com/opensearch-project/opensearch-go/v2"
)

func main() {
    client, err := opensearch.NewClient(opensearch.Config{
        Addresses: []string{"https://localhost:9200"},
        Username:  "admin",
        Password:  "YourStr0ng!Pass", // 生产改环境变量 / 密钥管理
        Transport: &http.Transport{TLSClientConfig: &tls.Config{
            InsecureSkipVerify: true}}, // 仅本地自签证书；生产配 CA
    })
    if err != nil { log.Fatal(err) }
    res, err := client.Info()
    if err != nil { log.Fatal(err) }
    defer res.Body.Close()
    if res.IsError() { log.Fatalf("info: %s", res.String()) }
    fmt.Println("cluster:", res.Status())
}
```

### 4.3 建索引、Bulk 写入与查询

```go
package main

import (
    "bytes"
    "context"
    "encoding/json"
    "fmt"
    "io"
    "log"
    "net/http"
    "strings"
    "github.com/opensearch-project/opensearch-go/v2"
    "github.com/opensearch-project/opensearch-go/v2/opensearchapi"
)
const mapping = `{"mappings":{"dynamic":"strict","properties":{
  "title":{"type":"text"},"tenant":{"type":"keyword"}}}}`
func main() {
    client, err := opensearch.NewClient(opensearch.Config{
        Addresses: []string{"http://localhost:9200"}, // 本地关闭 Security 插件时
    })
    if err != nil { log.Fatal(err) }
    ctx := context.Background()
    create := opensearchapi.IndicesCreateRequest{Index: "articles", Body: strings.NewReader(mapping)}
    if res, err := create.Do(ctx, client); err != nil { log.Fatal(err) } else {
        defer res.Body.Close()
        if res.IsError() { log.Fatalf("create: %s", res.String()) }
    }
    // Bulk 是 NDJSON：每两行一组（动作行 + 数据行）
    var buf bytes.Buffer
    for _, d := range []map[string]any{{"title": "Go 并发编程", "tenant": "acme"}, {"title": "Go Web 开发", "tenant": "acme"}} {
        meta, _ := json.Marshal(map[string]any{"index": map[string]any{"_index": "articles"}})
        buf.Write(meta); buf.WriteByte('\n')
        _ = json.NewEncoder(&buf).Encode(d)
    }
    req, _ := http.NewRequestWithContext(ctx, http.MethodPost, "/_bulk?refresh=wait_for", &buf)
    req.Header.Set("Content-Type", "application/x-ndjson")
    resp, err := client.Transport.Perform(req)
    if err != nil { log.Fatal(err) }
    defer resp.Body.Close()
    body, _ := io.ReadAll(resp.Body)
    if resp.StatusCode >= 300 { log.Fatalf("bulk: %s", body) }
    var br struct{ Errors bool `json:"errors"` } // 200 也可能是部分失败
    if err := json.Unmarshal(body, &br); err != nil || br.Errors { log.Fatalf("bulk: %s", body) }
    q := `{"size":10,"query":{"bool":{"must":[{"match":{"title":"Go"}}],"filter":[{"term":{"tenant":"acme"}}]}}}`
    search := opensearchapi.SearchRequest{Index: []string{"articles"}, Body: strings.NewReader(q)}
    sres, err := search.Do(ctx, client)
    if err != nil { log.Fatal(err) }
    defer sres.Body.Close()
    if sres.IsError() { log.Fatalf("search: %s", sres.String()) }
    fmt.Println("search:", sres.Status())
}
```

`dynamic: "strict"` 防 mapping 爆炸；Bulk 单批 5–15MB、遇 429 指数退避；纯过滤放 `filter` 不评分可缓存。判错要点：`IsError()` 判 4xx/5xx、Bulk 即使 200 也要解析 `errors`/`items`、用 `errors.As(err, &opensearchapi.Error{})` 拿结构化错误做幂等容忍。生产批量写入建议用 `opensearchutil.NewBulkIndexer` 自动分批与重试，其余见 [ES 篇第 4 章](../elasticsearch/elasticsearch-guide.md)。

### 4.4 AWS SigV4 签名请求

连 Amazon OpenSearch Service 必须用 **AWS Signature Version 4** 签名。`signer/awsv2`（配 aws-sdk-go-v2）注入 `Config.Signer`，凭据走默认凭据链（环境变量 / `~/.aws` / IAM Role）：

```go
package main

import (
    "context"
    "fmt"
    "log"
    "github.com/aws/aws-sdk-go-v2/config"
    "github.com/opensearch-project/opensearch-go/v2"
    requestsigner "github.com/opensearch-project/opensearch-go/v2/signer/awsv2"
)

func main() {
    ctx := context.Background()
    awsCfg, err := config.LoadDefaultConfig(ctx, config.WithRegion("us-east-1"))
    if err != nil { log.Fatal(err) }
    // 托管域 service 名 "es"；Amazon OpenSearch Serverless 用 "aoss"
    signer, err := requestsigner.NewSignerWithService(awsCfg, "es")
    if err != nil { log.Fatal(err) }
    client, err := opensearch.NewClient(opensearch.Config{
        Addresses: []string{"https://search-xxx.us-east-1.es.amazonaws.com"},
        Signer:    signer,
    })
    if err != nil { log.Fatal(err) }
    res, err := client.Info()
    if err != nil { log.Fatal(err) }
    defer res.Body.Close()
    fmt.Println("signed info:", res.Status())
}
```

> 用 AWS SDK v1 时换 `signer/aws` 与 `session.Options`；**service 名写错（`es`/`aoss`）会直接 403**，这是最常见的接入故障。

---

## 5. 版本演进与要点

| 时间 / 版本 | 关键变化与迁移注意点 |
|---|---|
| 2021-01 | Elastic 宣布 7.11 起把 ES/Kibana 由 Apache-2.0 改为 **SSPL + ELv2** 双许可，触发分叉 |
| **2021-04** | AWS 等从 **ES 7.10.2 / Kibana 7.10.2**（最后 ALv2 版本）fork，全栈 **Apache-2.0**，迁入 Open Distro 插件 |
| 1.x（2021-07 **1.0 GA**） | 首个生产就绪版；Data Streams、ISM、Security、SQL/PPL、k-NN、Alerting 随发行版提供；API 对齐 ES 7.10 |
| 2.x（2022–2024） | 可搜索快照、Lucene 9 k-NN 引擎与 adaptive filtering、segment replication、Neural Search / 混合检索 |
| 2024-08 / 09 | Elastic 追加 **AGPLv3** 第三许可选项；OpenSearch 治理移交 **Linux 基金会 OpenSearch Software Foundation** |
| **3.0（2025）** | 向量/性能/安全增强，Apache Calcite 成为 PPL 默认引擎、gRPC、pull-based ingestion、面向 RAG/Agent 的 ML Commons |
| **3.x 主线（2026）** | 3.2–3.9 快速迭代；**3.9.0（2026-09-29）** 引入 neural sparse 原生 SEISMIC 引擎、统一告警视图 GA、自适应并发限制、Resource Sharing GA |

**许可决策影响**（选型的硬约束，不只是法务问题）：

- **Apache-2.0**（OpenSearch）：OSI 认证宽松许可，可自由使用、修改、嵌入商业产品、**作为托管/SaaS 服务提供**、转售，无开源回馈义务、无 copyleft 传染——这是 AWS/Aiven 等能合法运营 OpenSearch 托管服务的原因。**要把搜索能力做成对外产品/服务、或需完全自主可控的二次分发，它是唯一无附加义务的选项**。
- **ELv2**：核心是**禁止把软件作为托管服务提供给第三方**、禁止绕过 license key 暴露付费功能、禁止移除许可声明；自建内部使用无碍。
- **SSPL**：要求把「提供该服务所涉及的整套软件」一并开源，争议最大，未被 OSI 认定为开源许可证；**AGPLv3** 则是强 copyleft，网络服务触发源码提供义务。纯内部自建且依赖 ES 生态时 ELv2 通常够用，引入 SSPL/AGPLv3 组件前务必做合规评审。

**API 兼容边界**：与 **ES 7.10** 高度兼容，7.x 应用换客户端后基本可迁；**ES 8/9 应用不能直接搬**——product check 头、客户端 API 差异、ES|QL/`semantic_text`/ELSER 独占特性、8.x 索引格式都不兼容；两边**插件互不通用**，自定义插件需重写。升级前读官方 upgrade guide，客户端与服务端大版本对齐。

---

## 6. 常见坑与最佳实践

1. **拿 go-elasticsearch v8 连 OpenSearch**：product check 直接拒绝。对策：改用 `opensearch-go` 且大版本对齐，别指望「REST 兼容所以客户端能混用」。
2. **沿用默认认证 / `admin/admin` 上生产**：3.x 起必须用 `OPENSEARCH_INITIAL_ADMIN_PASSWORD` 设初始口令。对策：强口令 + TLS CA 校验 + 最小权限 roles + 网络隔离 + 审计日志。
3. **按 ES 8.x 教程写 OpenSearch**：ES|QL、`semantic_text`、ELSER、新聚合都不存在。对策：把 **7.10 API 当基线**，语义检索用 `neural`/k-NN，分析用 SQL/PPL。
4. **k-NN 忘了 `index.knn: true` 或 `dimension` 与模型不符**：报错或打分失真。对策：先定嵌入模型维度与 `space_type` 再写 mapping，显式开 `index.knn`。
5. **引擎与参数乱选**：选了 3.x 已废弃的 NMSLIB、HNSW 的 `m`/`ef_construction` 拍脑袋、Faiss IVF 忘训练。对策：千万级以下用 Lucene，大规模用 Faiss；IVF 先走 Train API 生成 model；用 PQ/SQ/fp16 量化压内存。
6. **neural 查询没配 ingest pipeline**：查询文本没被向量化，召回为空。对策：`text_embedding` processor + `neural` query + 混合检索 search pipeline（RRF）三者配套。
7. **`knn` 结果被过滤吃掉**：过滤条件放错位置导致召回不足。对策：用 `knn` 内的 `filter` 或 adaptive filtering。
8. **照抄 ES 的 ILM 配置**：OpenSearch 是 **ISM**，路径 `_plugins/_ism/policies`、策略格式与状态机都不同。对策：重写策略，用 `ism_template` 自动绑定新索引。
9. **不配 ISM / retention、或让应用按天拼索引名**：时序索引无限增长、难回滚。对策：Data Streams + 固定写别名 + ISM rollover + delete，第 1 天就上。
10. **把可搜索快照当热数据用**：延迟高、依赖对象存储与本地缓存。对策：只用于冷/温数据降本，与 ISM 冷阶段配合。
11. **SigV4 service 名写错 / 凭据链没配**：托管域返回 403。对策：托管域用 `"es"`、Serverless 用 `"aoss"`，凭据交默认凭据链，别硬编码 AK/SK。
12. **Bulk 只判 HTTP 200**：部分失败被吞。对策：解析 `errors`/`items`，遇 429 指数退避，单批 5–15MB；动态 mapping 爆炸、`text` 做 terms 聚合、深翻页、中文分词等通用坑见 [ES 篇第 6 章](../elasticsearch/elasticsearch-guide.md)。

---

## 7. 选型对比

| 维度 | OpenSearch | Elasticsearch | Solr | ZincSearch | Loki | ClickHouse |
|---|---|---|---|---|---|---|
| 定位 | ES 7.10 的 ALv2 分叉，搜索 + 日志 + 向量 | 原厂搜索与分析平台 | 传统企业搜索 | 轻量全文检索 | 日志检索（只索引标签） | 列存 OLAP |
| 许可 | **Apache-2.0** | ELv2 / SSPL / AGPLv3 | Apache-2.0 | Apache-2.0 | AGPLv3 | Apache-2.0 |
| 治理 | Linux 基金会 / 社区 | Elastic 公司 | Apache 基金会 | 社区 | Grafana Labs | 社区 / 公司 |
| 检索内核 | Lucene + 倒排 | Lucene + 倒排 | Lucene | 倒排（Go 实现） | 无正文索引 | 列存 + 跳数索引 |
| 相关性 / DSL | 强（对齐 ES 7.10） | **最成熟**（8/9 代） | 强 | 中 | 无全文相关性 | 弱 |
| 向量 / AI | **强**（k-NN 多引擎、neural、RAG） | 强（ELSER、`semantic_text`） | 中 | 弱 | 无 | 有向量但非主业 |
| 聚合分析 | 强 + SQL/PPL | 强 + ES\|QL | 中 | 弱 | 弱（LogQL） | **极强（SQL）** |
| 日志检索 | 强（全文） | 强（全文） | 中 | 可用 | **成本最优** | 强（报表向） |
| 资源占用 | 高（JVM） | 高（JVM） | 高 | **低（Go）** | **低** | 中 |
| 运维复杂度 | 中～高 | 中～高 | 高 | **低** | 低～中 | 中 |
| Go 客户端 | `opensearch-go`（含 SigV4） | `go-elasticsearch` v8/v9 | 无官方强绑定 | HTTP/REST | 标准库 HTTP | clickhouse-go |

**一句话决策结论**：许可合规要求纯 Apache-2.0、要把搜索能力嵌进自有产品或对外提供托管服务、且希望安全/RBAC/告警/SQL/ISM/CCR 零授权费，就选 **OpenSearch**；其余按「生态 vs 成本 vs 分析」在 ES、Loki、ClickHouse 间取舍。

- **要原厂商业支持、Elastic 全家桶（APM/SIEM/ES|QL）、或必须跟 ES 8/9 新特性** → Elasticsearch，接受 ELv2 条款与订阅成本。
- **已有 Grafana + Prometheus、日志按服务/时间查、极致省钱** → **Loki**（见 [Loki 篇](../loki/loki-guide.md)），只索引标签；**主要跑 SQL 聚合报表** → **ClickHouse**。
- **中小站一个搜索框、不想碰 JVM** → ZincSearch（Go 单二进制）或 Meilisearch / Typesense；**存量 Solr 栈**留在 Solr，别为「新」而迁。

**何时不该用 OpenSearch**：① 主数据存储与事务、强一致、频繁逐字段更新（用 [MySQL](../mysql/mysql-guide.md)）；② 只需要 K8s 日志且成本敏感（Loki 更省）；③ 主要诉求是 OLAP 报表（ClickHouse 更合适）；④ 深度绑定 Elastic 付费生态或官方支持合同；⑤ 团队无法承担 JVM 调优、分片规划、ISM/Security 配置与升级运维；⑥ 强依赖 ES 8.x+ 独占特性，迁移改造成本高于收益。

参考：<https://github.com/opensearch-project/OpenSearch>　|　<https://opensearch.org/docs/latest/>　|　<https://github.com/opensearch-project/opensearch-go>　|　<https://aws.amazon.com/blogs/opensource/introducing-opensearch/>
