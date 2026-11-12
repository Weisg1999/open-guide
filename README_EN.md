# open-guide

[![Docs](https://img.shields.io/badge/docs-26%20guides-16a34a)](./README_EN.md)
[![Language](https://img.shields.io/badge/topic-Go%20ecosystem-00ADD8?logo=go)](https://go.dev)
[![Format](https://img.shields.io/badge/format-Markdown%20%2B%20HTML-orange)](./README_EN.md)
[![License](https://img.shields.io/badge/license-MIT-green)](./LICENSE)
[![中文](https://img.shields.io/badge/README-中文-red)](./README.md)

> **A collection of in-depth, hands-on guides for the Go backend ecosystem.**
> Not another Hello World — these are manuals you can actually read, search, and copy working code from.
>
> 26 long-form guides · ~10,200 lines of Markdown · each one ships as **Markdown** *and* a **single-file HTML** page (sidebar TOC + syntax highlighting, just open it in a browser).

[中文说明](./README.md) · [中文 README](#中文-readme)

> 📌 Note: the guides themselves are written in Chinese. This English README exists so non-Chinese readers can navigate the repo, understand its structure, and find the right guide (machine translation works well on these pages).

---

## 1. What this is

`open-guide` is a **documentation-only repository** covering the open-source libraries and language features that Go backend engineers reach for most — and get bitten by most.

Each guide is *not* a translation of a project's README. It is a rewrite following one consistent structure:

**Overview → Feature Matrix → Core Concepts & Internals → Hands-on Go Code → Version History → Pitfalls & Best Practices → Comparison with Alternatives**

It exists to solve three real problems:

| Problem | How this repo addresses it |
| --- | --- |
| Official docs are scattered, dated, or just an API list | Reorganized as *internals → code → pitfalls → trade-offs*, so one sitting is enough to get productive |
| Blog posts are version-blind and their conclusions feel risky | Every guide states the **exact upstream version and release date** it was written against |
| You learn the API but never the "why" | Every feature comes with the mechanism behind it (etcd's Raft/MVCC, asynq's Redis data structures, generics' GC Shape stenciling, …) |

This repo contains **no third-party source code** — only curated explanation. All copyright belongs to the respective upstream projects (see [License & Disclaimer](#8-license--disclaimer)).

---

## 2. Guide Index

### 🔤 Go Language Core

| Topic | What's covered | Files |
| --- | --- | --- |
| **Go Generics** | Type parameters, constraints, generic types & methods, how GC Shape Stenciling actually works, the limits and footguns of generics, hands-on: a type-safe generic cache | [Markdown](./go-generics/go-generics-tutorial.md) · [HTML](./go-generics/go-generics-tutorial.html) |
| **Go Reflection** | The three laws of reflection, `reflect.Type` / `reflect.Value`, dynamic invocation & mutation, struct tags in depth (how `encoding/json` and ORMs work), performance cost, reflection vs. generics | [Markdown](./go-reflection/go-reflection-tutorial.md) · [HTML](./go-reflection/go-reflection-tutorial.html) |
| **samber/lo** | The Lodash-style generics toolkit: the full slice / map / channel / string / function helper catalog, and how it relates to the stdlib `slices` / `maps` packages | [Markdown](./samber-lo/samber-lo-guide.md) · [HTML](./samber-lo/samber-lo-guide.html) |

### 🌐 Web & Microservice Frameworks

| Topic | What's covered | Files |
| --- | --- | --- |
| **Gin** | Radix-tree routing, middleware chain, binding & validation, response rendering, graceful shutdown, v1.10 → v1.12 evolution | [Markdown](./gin/gin-guide.md) · [HTML](./gin/gin-guide.html) |
| **Kratos** (Bilibili) | API-first / Protobuf-driven development, unified HTTP + gRPC transport, pluggable components (registry / config / log / middleware), v3.0 highlights and the v2 migration path | [Markdown](./kratos/kratos-guide.md) · [HTML](./kratos/kratos-guide.html) |
| **go-zero** (TAL) | `.api` DSL + `goctl` code generation, built-in adaptive circuit breaking / rate limiting / load shedding, web and RPC in one framework | [Markdown](./go-zero/go-zero-guide.md) · [HTML](./go-zero/go-zero-guide.html) |
| **gRPC-Go** | HTTP/2 multiplexing and Protobuf encoding, `.proto` contracts and codegen, four streaming modes, interceptor chains, deadline propagation, resolver/balancer | [Markdown](./grpc/grpc-guide.md) · [HTML](./grpc/grpc-guide.html) |

### 🗄️ Data Stores & Search

| Topic | What's covered | Files |
| --- | --- | --- |
| **MySQL** | B+ tree clustered indexes and table lookups, leftmost-prefix and covering indexes, MVCC / ReadView and isolation levels, next-key locks and deadlocks, `EXPLAIN` for slow queries, replication lag, `database/sql` connection pooling | [Markdown](./mysql/mysql-guide.md) · [HTML](./mysql/mysql-guide.html) |
| **GORM** | Chained API, associations & preloading, transactions and savepoints, hooks, migrator, soft delete, read/write-splitting plugin, generics-based interfaces | [Markdown](./gorm/gorm-guide.md) · [HTML](./gorm/gorm-guide.html) |
| **go-redis** | Connection pool & `Cmdable`, Pipeline vs TxPipeline, `redis.Nil` semantics, Lua and distributed locks, Stream consumer groups, Cluster slots and hash tags, cache-stampede defences | [Markdown](./redis/redis-guide.md) · [HTML](./redis/redis-guide.html) |
| **etcd** | Raft consensus, MVCC, the bbolt B+ tree, leases / watches / distributed locks, clientv3 in practice, what's new in v3.6 | [Markdown](./etcd/etcd-guide.md) · [HTML](./etcd/etcd-guide.html) |
| **InfluxDB 2.x** | Bucket / token model, the TSM storage engine, Flux queries, tasks & alerting, writing and querying from Go | [Markdown](./influxdb-v2/influxdb-v2-guide.md) · [HTML](./influxdb-v2/influxdb-v2-guide.html) |
| **InfluxDB 3** | The Rust rewrite: FDAP stack (Flight + DataFusion + Arrow + Parquet), SQL queries, object-storage-first design, infinite cardinality, Core vs. Enterprise | [Markdown](./influxdb-v3/influxdb-v3-guide.md) · [HTML](./influxdb-v3/influxdb-v3-guide.html) |
| **MinIO** | S3-compatible object storage, erasure coding, bucket / object model, multipart upload, presigned URLs, the 2025 product-line changes | [Markdown](./minio/minio-guide.md) · [HTML](./minio/minio-guide.html) |
| **Elasticsearch** | Inverted index and `text`/`keyword` fields, analyzers and IK for Chinese, NRT refresh and segment merge, shard routing and BM25, Bool Query DSL and aggregations, `search_after` + PIT, Alias/Rollover/ILM for time-series indices | [Markdown](./elasticsearch/elasticsearch-guide.md) · [HTML](./elasticsearch/elasticsearch-guide.html) |
| **MongoDB** | BSON document model and `bson.D` ordering, WiredTiger document-level concurrency, replica sets and oplog, shard-key rules and routing, aggregation pipelines and ESR indexes, transactions and Change Streams | [Markdown](./mongodb/mongodb-guide.md) · [HTML](./mongodb/mongodb-guide.html) |
| **ClickHouse** | MergeTree parts and background merges, sparse primary index and data-skipping indexes, `ORDER BY` design, vectorized execution, materialized views, `PrepareBatch` bulk writes, `ReplacingMergeTree` eventual dedup, and the FDAP comparison with InfluxDB 3 | [Markdown](./clickhouse/clickhouse-guide.md) · [HTML](./clickhouse/clickhouse-guide.html) |

> 💡 Elasticsearch wears two hats: business full-text search and log search. For logs, pair the `trace_id` from the [Zap guide](./zap/zap-guide.md) with [OpenTelemetry](./opentelemetry/opentelemetry-guide.md) traces.

### 📨 Messaging, Tasks & Scheduling

| Topic | What's covered | Files |
| --- | --- | --- |
| **asynq** ⭐ | The flagship guide (1,300+ lines). Redis-backed distributed task queue: task lifecycle, delayed jobs, retries & dead letters, weighted / strict priority queues, deduplication, task aggregation, timeouts and deadlines, Prometheus metrics, deploying the Asynqmon web UI | [Markdown](./asynq/asynq技术文档与使用手册.md) · [HTML](./asynq/asynq技术文档与使用手册.html) |
| **Apache Kafka** | Partitions and replicas, consumer groups and offsets, ISR and ack semantics, hands-on with `segmentio/kafka-go`, Kafka 4.x dropping ZooKeeper | [Markdown](./kafka/kafka-guide.md) · [HTML](./kafka/kafka-guide.html) |
| **NATS / JetStream** | Subject-based addressing, pub/sub and request-reply, queue groups, JetStream streams and consumers, `nats.go` in practice | [Markdown](./nats/nats-guide.md) · [HTML](./nats/nats-guide.html) |
| **robfig/cron** | 5-field and 6-field cron expressions, parser extensions (`@every`, `@daily`, …), job wrappers (`SkipIfStillRunning`, …), time zones and concurrency, custom jobs | [Markdown](./robfig-cron/robfig-cron-guide.md) · [HTML](./robfig-cron/robfig-cron-guide.html) |
| **RabbitMQ / RocketMQ** | Exchange/Queue routing, publisher confirms and manual ACKs, Quorum Queues and DLX, CommitLog sequential writes, ordered / transactional / scheduled messages, at-least-once idempotency, and the boundaries with Kafka / NATS / asynq | [Markdown](./rabbitmq/rabbitmq-guide.md) · [HTML](./rabbitmq/rabbitmq-guide.html) |

> 💡 The four messaging guides (Kafka / NATS / asynq / RabbitMQ·RocketMQ) each end with a **head-to-head comparison table** — start there if you're making a technology decision.

### 🔭 Observability

| Topic | What's covered | Files |
| --- | --- | --- |
| **Zap** | Zero-allocation hot path and typed Fields, `Logger` vs `SugaredLogger`, the four-layer `zapcore` pipeline (Encoder / WriteSyncer / LevelEnabler), Tee fan-out, sampling and `AtomicLevel`, and how it relates to stdlib `log/slog` | [Markdown](./zap/zap-guide.md) · [HTML](./zap/zap-guide.html) |
| **Prometheus** | Pull model and labelled time series, the four metric types and `rate()`, TSDB inverted index, PromQL / recording rules / Alertmanager, the high-cardinality trap, boundaries with InfluxDB and OTel Metrics | [Markdown](./prometheus/prometheus-guide.md) · [HTML](./prometheus/prometheus-guide.html) |
| **OpenTelemetry** | Trace/Span model and W3C `traceparent` propagation, Sampler / BatchSpanProcessor / OTLP, Resource and semantic conventions, Gin/gRPC/asynq instrumentation, writing `trace_id` into Zap logs | [Markdown](./opentelemetry/opentelemetry-guide.md) · [HTML](./opentelemetry/opentelemetry-guide.html) |

> 💡 The three observability guides (Zap / Prometheus / OpenTelemetry) form the logs / metrics / traces pillars, linked by a shared `service.name` and `trace_id`.

### 🐳 Deployment & Orchestration

| Topic | What's covered | Files |
| --- | --- | --- |
| **Docker / Kubernetes** | Image layers and multi-stage builds, namespaces/cgroups, the declarative reconcile loop, Pod/Deployment/Service, three probes and QoS classes, rolling updates and graceful shutdown, client-go and RBAC | [Markdown](./docker-k8s/docker-k8s-guide.md) · [HTML](./docker-k8s/docker-k8s-guide.html) |


---

## 3. Suggested Reading Paths

```
                    ┌────────────────────────────────┐
                    │  Generics → Reflection → lo     │   Language core
                    └───────────────┬────────────────┘
                                    │
        ┌───────────────────────────┼───────────────────────────┐
        ▼                           ▼                           ▼
┌────────────────┐        ┌──────────────────┐        ┌──────────────────┐
│ Gin            │        │ GORM             │        │ robfig/cron      │
│   ↓            │        │   ↓              │        │   ↓              │
│ Kratos/go-zero │        │ InfluxDB 2 → 3   │        │ asynq            │
│   ↓            │        │   ↓              │        │   ↓              │
│ etcd           │        │ MinIO            │        │ Kafka / NATS     │
└────────────────┘        └──────────────────┘        └──────────────────┘
   Services track            Data / IoT track          Async & scheduling
```

> 🔭 **Cross-cutting**: `gRPC-Go` belongs to the services track; `MySQL` / `go-redis` / `ClickHouse` to the data track (OLTP → KV → OLAP); `Elasticsearch` / `MongoDB` are the search and document branches; `RabbitMQ / RocketMQ` to async and scheduling; `Zap` / `Prometheus` / `OpenTelemetry` are the shared observability foundation; and `Docker / K8s` is the deployment layer every guide eventually lands on.

| Your goal | Suggested order |
| --- | --- |
| Solidify Go fundamentals | Generics → Reflection → samber/lo |
| Ship your first API service | Gin → **MySQL** → GORM → robfig/cron → **Zap** (establish logging conventions early) |
| Build microservices | Gin → **gRPC-Go** → Kratos *and* go-zero (read them side by side) → etcd → asynq |
| Solidify the data layer | **MySQL** (indexes / transactions / locks) → GORM → **go-redis** |
| Add caching / distributed locks | **go-redis** → then asynq / etcd to understand the boundaries between cache, task queue and strong-consistency coordination |
| Build search / log retrieval | **Elasticsearch** → pair with Zap `trace_id` and OpenTelemetry traces |
| Store semi-structured, fast-changing data | **MongoDB** → alongside MySQL (source of truth) and ES (search replica) |
| Build IoT / monitoring / time-series | InfluxDB 2.x → InfluxDB 3 (to see the evolution) → MinIO → Kafka / NATS |
| Set up observability | **Zap** → **Prometheus** → **OpenTelemetry** (logs / metrics / traces, linked by `trace_id`) |
| Build OLAP analytics / log detail | **ClickHouse** → ingest with the Kafka table engine |
| Decouple services / ordered and transactional messages | **RabbitMQ / RocketMQ** → split work with asynq (job execution) |
| Ship and release a service | **Docker / K8s** → with Zap stdout logs, Prometheus probes and graceful shutdown |
| Make a technology decision | Jump straight to the "Comparison" section at the end of each guide, especially Kafka vs. NATS vs. asynq |

---

## 4. Two Ways to Read

### 1. Markdown (best for searching, copying code, and sending PRs)

Open any `.md` file on GitHub. Every code block is language-tagged and copy-ready.

### 2. HTML (best for reading cover-to-cover, or as an offline manual)

Each directory contains a **dependency-free single-file HTML** page with a sidebar TOC, a hero header, language-labeled code blocks and syntax highlighting. No build step, no network required.

```bash
# Option 1: just double-click any *-guide.html file

# Option 2: serve the folder locally (recommended — better navigation)
cd open-guide
python -m http.server 8000
# then open http://localhost:8000
```

> HTML is a **build artifact**, generated from the matching Markdown by `tools/build_html.py`. Do not edit HTML by hand. If they ever disagree, **the Markdown file is authoritative**.

### 3. Rebuilding HTML (maintainers)

The builder uses only the Python 3 standard library — no third-party packages. **All body text comes from Markdown**; the three files below carry presentation data only, and are version-controlled so every HTML page is fully reproducible:

| File | Purpose |
|---|---|
| [`tools/themes.json`](./tools/themes.json) | Per-guide colours, hero subtitle and tags |
| [`tools/fragments.json`](./tools/fragments.json) | SVG architecture diagrams, feature cards and callouts that have no Markdown equivalent, injected by chapter anchor (274 blocks / 72 diagrams) |
| [`tools/anchor_map.json`](./tools/anchor_map.json) | Legacy semantic anchors -> heading slugs, so links inside those blocks keep working |

```bash
python tools/build_html.py                 # rebuild every guide
python tools/build_html.py --topic gin     # rebuild one guide
python tools/build_html.py --check         # fail if HTML is stale (used by CI)
python tools/build_html.py --stats         # structure stats + internal-anchor check
```

A new topic needs no build configuration: the script auto-discovers `*/ *.md` and assigns a stable palette from the directory name. CI ([`.github/workflows/check-html.yml`](./.github/workflows/check-html.yml)) runs `--check` on every push / PR, which mechanically prevents MD/HTML drift.

---

## 5. Required Elements of Every Guide

The original convention was a strict "7-section" layout. In practice, a deep manual like asynq reads better as nine chapters (what / architecture / concepts / feature deep-dive / full example / deployment / pitfalls / history / comparison), while language tutorials naturally progress by difficulty. Forcing a fixed section count hurt the content. The convention is therefore a **checklist of 7 required elements**: numbering and headings are free, the elements are not.

| # | Element | Acceptance criteria | Common equivalent headings |
|---|---|---|---|
| 1 | Positioning | What it is, the problem it solves, key selling points, when *not* to use it | Overview / What is X / Why generics |
| 2 | Capability matrix | A scannable table of capabilities | Feature matrix / Feature catalog / Data model |
| 3 | Internals | Mechanisms — architecture, data structures, execution flow — not an API list | Core concepts / Architecture / Implementation |
| 4 | Runnable hands-on | Language-tagged Go code + commands, copy-paste ready | Hands-on / Full example / Deployment |
| 5 | Versions & evolution | Documented upstream version, recent major changes, migration notes | Version history / Release highlights / Product-line changes |
| 6 | Pitfalls & best practices | Things that actually bite in production, with mitigations | Pitfalls / Common traps / Caveats |
| 7 | Comparison | Head-to-head table against alternatives + a one-line decision rule | Comparison / How to choose / Relation to X |

**Form rules**

- Chapter numbering (`1.`, `一、`, or none) and exact wording are free. One element may span several chapters (asynq splits #4 into feature deep-dive / full example / deployment), or merge with a neighbour.
- **#6 and #7 must be standalone chapters at the end** — they are the most-jumped-to sections and what separates this repo from an API translation.
- Language-feature tutorials (Go Generics, Go Reflection) have no upstream release; #5 may be satisfied by "applicable Go version + language evolution notes". All other elements still apply.
- The opening `>` blockquote (one-line positioning + upstream repo + documented version + license) is the minimum bar for #5.

**Self-check**: `python tools/build_html.py --audit` prints a per-guide element matrix and lists anything missing. CI runs both `--check` and `--audit`.

---

## 6. Upstream Projects & Versions

| Guide | Upstream repo | Version documented |
| --- | --- | --- |
| asynq | [hibiken/asynq](https://github.com/hibiken/asynq) | v0.26.0 · asynqmon v0.7.x |
| etcd | [etcd-io/etcd](https://github.com/etcd-io/etcd) | v3.6 |
| Gin | [gin-gonic/gin](https://github.com/gin-gonic/gin) | v1.12.0 |
| go-zero | [zeromicro/go-zero](https://github.com/zeromicro/go-zero) | v1.10.3 |
| GORM | [go-gorm/gorm](https://github.com/go-gorm/gorm) | v1.31.2 |
| InfluxDB | [influxdata/influxdb](https://github.com/influxdata/influxdb) | 2.7 / 3 Core (GA 2025-04) |
| Kafka | [apache/kafka](https://github.com/apache/kafka) | 4.x |
| Kratos | [go-kratos/kratos](https://github.com/go-kratos/kratos) | v3.0.0 |
| MinIO | [minio/minio](https://github.com/minio/minio) | Community Edition (AGPLv3) |
| NATS | [nats-io/nats-server](https://github.com/nats-io/nats-server) | v2.11 |
| robfig/cron | [robfig/cron](https://github.com/robfig/cron) | v3 |
| samber/lo | [samber/lo](https://github.com/samber/lo) | v1 |
| go-redis | [redis/go-redis](https://github.com/redis/go-redis) | v9 (module path `/v9`) |
| gRPC-Go | [grpc/grpc-go](https://github.com/grpc/grpc-go) | v1.x (stable API, continuous releases) |
| Zap | [uber-go/zap](https://github.com/uber-go/zap) | v1.27.x |
| MySQL | [mysql/mysql-server](https://github.com/mysql/mysql-server) | 8.0 / 8.4 LTS · go-sql-driver/mysql v1.x |
| Prometheus | [prometheus/prometheus](https://github.com/prometheus/prometheus) | 3.x · client_golang v1.x |
| OpenTelemetry | [open-telemetry/opentelemetry-go](https://github.com/open-telemetry/opentelemetry-go) | Go SDK v1.x · OTLP |
| Elasticsearch | [elastic/elasticsearch](https://github.com/elastic/elasticsearch) | 8.x / 9.x · go-elasticsearch v8 |
| MongoDB | [mongodb/mongo](https://github.com/mongodb/mongo) | 7.0 / 8.0 · mongo-go-driver v1.x |
| ClickHouse | [ClickHouse/ClickHouse](https://github.com/ClickHouse/ClickHouse) | 25.x · clickhouse-go v2 |
| Docker / Kubernetes | [kubernetes/kubernetes](https://github.com/kubernetes/kubernetes) | Engine 27+ / K8s 1.3x · client-go v0.3x |
| RabbitMQ / RocketMQ | [rabbitmq/rabbitmq-server](https://github.com/rabbitmq/rabbitmq-server) | 4.x / 5.x · amqp091-go / rocketmq-client-go v2 |
| Go Generics / Reflection | [Official Go proposal & blog](https://go.dev/blog/laws-of-reflection) | Go 1.18+ |

> ⏱ **On freshness:** these projects move fast. Always cross-check against the latest upstream release. If you spot an outdated version or a stale conclusion, an Issue or PR is very welcome.

---

## 7. Contributing

Contributions are very welcome, especially:

- **New topics** (Viper, PostgreSQL, OpenSearch, Loki, Temporal, slog in practice, …; Redis / gRPC / Zap / MySQL / Prometheus / OpenTelemetry / Elasticsearch / MongoDB / ClickHouse / Docker·K8s / RabbitMQ·RocketMQ are now covered)
- **Version bumps** — sync a guide after an upstream release
- **Corrections** — wrong internals, broken code, outdated conclusions
- **Build script** — improve parsing/rendering in `tools/build_html.py` (never hand-edit HTML)

```bash
git clone https://github.com/Weisg1999/open-guide.git
cd open-guide
git checkout -b feat/your-topic
# add your-topic/your-topic-guide.md (cover the 7 required elements in Section 5)
python tools/build_html.py --topic your-topic
git commit -m "docs: add xxx guide"
git push origin feat/your-topic
```

**Writing conventions** (consistency is what makes the collection readable):

1. Open with a `>` blockquote: one-line positioning + upstream repo link + documented version & license.
2. Cover the 7 required elements in [Section 5](#5-required-elements-of-every-guide) (numbering/headings are flexible, the elements are not).
3. Tag every code block with its language (``` ```go ```, ``` ```bash ```) and make it runnable.
4. End with two standalone chapters: "Pitfalls & Best Practices" and "Comparison" (the latter needs a head-to-head table and a one-line decision rule).
5. HTML is built with `python tools/build_html.py` from the Markdown — **never hand-edit HTML**. Keep the naming convention `<topic>/<topic>-guide.{md,html}`; unconfigured topics get an automatic palette.

---

## 8. License & Disclaimer

### This repository

The **documentation content** of this repository — the original explanations, structure and diagrams in the Markdown and HTML files — is released under the **MIT License**. See [LICENSE](./LICENSE).

```
MIT License

Copyright (c) 2026 Weisg1999
```

You are free to use, copy, modify, merge, publish and distribute these documents (including for commercial purposes), provided the copyright notice and this permission notice are retained.

### Regarding upstream projects

- This repository contains **no source code from the upstream projects** — only educational notes and explanations.
- Copyright for each upstream project belongs to its respective authors; their licenses (mostly MIT / Apache-2.0, MinIO Community Edition is AGPLv3) are authoritative in their own repositories. **The MIT license covers only the original compilation in this repo, not the upstream projects themselves.**
- Code samples and architectural explanations are compiled from the projects' official READMEs, Wikis, godocs and public source code.
- The content is provided for reference only. Validate against official documentation and your own benchmarks before relying on it in production.

---

<div align="center">

If these guides helped you, a ⭐ would mean a lot — and feel free to share the repo with a teammate who writes Go.

</div>
