# open-guide

[![Docs](https://img.shields.io/badge/docs-15%20guides-16a34a)](./README_EN.md)
[![Language](https://img.shields.io/badge/topic-Go%20ecosystem-00ADD8?logo=go)](https://go.dev)
[![Format](https://img.shields.io/badge/format-Markdown%20%2B%20HTML-orange)](./README_EN.md)
[![License](https://img.shields.io/badge/license-MIT-green)](./LICENSE)
[![中文](https://img.shields.io/badge/README-中文-red)](./README.md)

> **A collection of in-depth, hands-on guides for the Go backend ecosystem.**
> Not another Hello World — these are manuals you can actually read, search, and copy working code from.
>
> 15 long-form guides · ~5,800 lines of Markdown · each one ships as **Markdown** *and* a **single-file HTML** page (sidebar TOC + syntax highlighting, just open it in a browser).

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

### 🗄️ Data Stores

| Topic | What's covered | Files |
| --- | --- | --- |
| **GORM** | Chained API, associations & preloading, transactions and savepoints, hooks, migrator, soft delete, read/write-splitting plugin, generics-based interfaces | [Markdown](./gorm/gorm-guide.md) · [HTML](./gorm/gorm-guide.html) |
| **etcd** | Raft consensus, MVCC, the bbolt B+ tree, leases / watches / distributed locks, clientv3 in practice, what's new in v3.6 | [Markdown](./etcd/etcd-guide.md) · [HTML](./etcd/etcd-guide.html) |
| **InfluxDB 2.x** | Bucket / token model, the TSM storage engine, Flux queries, tasks & alerting, writing and querying from Go | [Markdown](./influxdb-v2/influxdb-v2-guide.md) · [HTML](./influxdb-v2/influxdb-v2-guide.html) |
| **InfluxDB 3** | The Rust rewrite: FDAP stack (Flight + DataFusion + Arrow + Parquet), SQL queries, object-storage-first design, infinite cardinality, Core vs. Enterprise | [Markdown](./influxdb-v3/influxdb-v3-guide.md) · [HTML](./influxdb-v3/influxdb-v3-guide.html) |
| **MinIO** | S3-compatible object storage, erasure coding, bucket / object model, multipart upload, presigned URLs, the 2025 product-line changes | [Markdown](./minio/minio-guide.md) · [HTML](./minio/minio-guide.html) |

### 📨 Messaging, Tasks & Scheduling

| Topic | What's covered | Files |
| --- | --- | --- |
| **asynq** ⭐ | The flagship guide (1,300+ lines). Redis-backed distributed task queue: task lifecycle, delayed jobs, retries & dead letters, weighted / strict priority queues, deduplication, task aggregation, timeouts and deadlines, Prometheus metrics, deploying the Asynqmon web UI | [Markdown](./asynq/asynq技术文档与使用手册.md) · [HTML](./asynq/asynq技术文档与使用手册.html) |
| **Apache Kafka** | Partitions and replicas, consumer groups and offsets, ISR and ack semantics, hands-on with `segmentio/kafka-go`, Kafka 4.x dropping ZooKeeper | [Markdown](./kafka/kafka-guide.md) · [HTML](./kafka/kafka-guide.html) |
| **NATS / JetStream** | Subject-based addressing, pub/sub and request-reply, queue groups, JetStream streams and consumers, `nats.go` in practice | [Markdown](./nats/nats-guide.md) · [HTML](./nats/nats-guide.html) |
| **robfig/cron** | 5-field and 6-field cron expressions, parser extensions (`@every`, `@daily`, …), job wrappers (`SkipIfStillRunning`, …), time zones and concurrency, custom jobs | [Markdown](./robfig-cron/robfig-cron-guide.md) · [HTML](./robfig-cron/robfig-cron-guide.html) |

> 💡 The three messaging guides (Kafka / NATS / asynq) each end with a **head-to-head comparison table** — start there if you're making a technology decision.

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

| Your goal | Suggested order |
| --- | --- |
| Solidify Go fundamentals | Generics → Reflection → samber/lo |
| Ship your first API service | Gin → GORM → robfig/cron |
| Build microservices | Gin → Kratos *and* go-zero (read them side by side) → etcd → asynq |
| Build IoT / monitoring / time-series | InfluxDB 2.x → InfluxDB 3 (to see the evolution) → MinIO → Kafka / NATS |
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

> Markdown and HTML are generated from the same source. If they ever disagree, **the Markdown file is authoritative** (HTML is the rendered artifact).

---

## 5. The Standard Structure of Every Guide

```
1. Overview              — what it is, what problem it solves, key selling points
2. Feature Matrix        — a table of capabilities
3. Core Concepts         — internals (architecture / data structures / execution flow)
4. Hands-on              — runnable Go code and shell commands
5. Version History       — what changed across recent major releases
6. Pitfalls & Best       — the things that actually bite you in production
7. Comparison            — head-to-head table against the alternatives
```

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
| Go Generics / Reflection | [Official Go proposal & blog](https://go.dev/blog/laws-of-reflection) | Go 1.18+ |

> ⏱ **On freshness:** these projects move fast. Always cross-check against the latest upstream release. If you spot an outdated version or a stale conclusion, an Issue or PR is very welcome.

---

## 7. Contributing

Contributions are very welcome, especially:

- **New topics** (Redis, gRPC, Zap, Viper, Prometheus, ClickHouse, MongoDB, Docker/K8s, …)
- **Version bumps** — sync a guide after an upstream release
- **Corrections** — wrong internals, broken code, outdated conclusions
- **HTML rendering** — style or highlighting fixes

```bash
git clone https://github.com/Weisg1999/open-guide.git
cd open-guide
git checkout -b feat/your-topic
# add your-topic/your-topic-guide.md (please follow the 7-section structure)
git commit -m "docs: add xxx guide"
git push origin feat/your-topic
```

**Writing conventions** (consistency is what makes the collection readable):

1. Open with a `>` blockquote: one-line positioning + upstream repo link + documented version & license.
2. Follow the 7-section structure (see [Section 5](#5-the-standard-structure-of-every-guide)).
3. Tag every code block with its language (``` ```go ```, ``` ```bash ```) and make it runnable.
4. Always end with "Pitfalls & Best Practices" and "Comparison".
5. HTML is rendered from the Markdown; keep the naming convention `<topic>/<topic>-guide.{md,html}`.

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
