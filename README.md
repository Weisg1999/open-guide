# open-guide

[![Docs](https://img.shields.io/badge/docs-15%20篇-16a34a)](./README.md)
[![Language](https://img.shields.io/badge/topic-Go%20%E7%94%9F%E6%80%81-00ADD8?logo=go)](https://go.dev)
[![Format](https://img.shields.io/badge/format-Markdown%20%2B%20HTML-orange)](./README.md)
[![License](https://img.shields.io/badge/license-MIT-green)](./LICENSE)
[![English](https://img.shields.io/badge/README-English-blue)](./README_EN.md)

> **Go 后端技术栈中文深度指南合集** —— 一份能读、能查、能直接抄代码的「技术手册」，而不是又一份 Hello World。
>
> 15 篇长文 · 约 5800 行 Markdown · 每篇同时提供 **Markdown** 与**单文件 HTML**（侧边栏导航 + 代码高亮，浏览器直接打开）。

[English README](./README_EN.md) | [English 说明](#english-readme)

---

## 一、这是什么

`open-guide` 是一个**纯文档仓库**，收录 Go 后端开发中最常用、最容易踩坑的一批开源项目与语言特性。每一篇都不是对 README 的机翻，而是按同一套结构重写的深度指南：

**项目简介 → 功能特性总览 → 核心概念与原理 → 使用实战（Go 代码） → 版本演进速览 → 常见坑与最佳实践 → 选型对比**

目标是解决三个真实痛点：

| 痛点 | 本仓库的做法 |
| --- | --- |
| 官方文档散、旧、只有 API 列表 | 按「原理 → 代码 → 坑 → 选型」重组，一篇读完能上手 |
| 博客文章版本过时、结论不敢用 | 每篇文首标注**对应的上游版本与发布时间**，结论可溯源 |
| 只有用法，不知道为什么 | 每个特性都配原理说明（如 etcd 的 Raft/MVCC、asynq 的 Redis 数据结构、泛型 GC Shape） |

仓库中**不包含任何第三方源码**，只做整理、讲解与二次组织；所有版权归各自上游项目所有（详见 [声明](#八声明与许可证)）。

---

## 二、文档目录

### 🔤 Go 语言核心

| 主题 | 覆盖要点 | 文档 |
| --- | --- | --- |
| **Go 泛型** | 类型参数、约束（constraint）、泛型类型与方法、GC Shape Stenciling 实现原理、泛型的能力边界与坑、实战：类型安全通用缓存 | [Markdown](./go-generics/go-generics-tutorial.md) · [HTML](./go-generics/go-generics-tutorial.html) |
| **Go 反射** | 反射三定律、`reflect.Type` / `reflect.Value`、动态调用与修改、结构体 Tag 深入（`encoding/json` / ORM 原理）、性能代价、反射 vs 泛型怎么选 | [Markdown](./go-reflection/go-reflection-tutorial.md) · [HTML](./go-reflection/go-reflection-tutorial.html) |
| **samber/lo** | 基于泛型的 Lodash 风格工具库：slice / map / channel / string / 函数式helper 全量分类讲解，与标准库 `slices`/`maps` 的关系 | [Markdown](./samber-lo/samber-lo-guide.md) · [HTML](./samber-lo/samber-lo-guide.html) |

### 🌐 Web 与微服务框架

| 主题 | 覆盖要点 | 文档 |
| --- | --- | --- |
| **Gin** | 基数树路由、中间件链、参数绑定与校验、响应渲染、优雅启停，v1.10 → v1.12 演进 | [Markdown](./gin/gin-guide.md) · [HTML](./gin/gin-guide.html) |
| **Kratos**（Bilibili） | API-first / Protobuf 驱动、HTTP + gRPC 双协议统一、可插拔组件（registry / config / log / middleware），v3.0 要点与 v2 迁移 | [Markdown](./kratos/kratos-guide.md) · [HTML](./kratos/kratos-guide.html) |
| **go-zero**（好未来） | `.api` DSL + `goctl` 代码生成、自适应熔断 / 限流 / 负载卸载、web 与 rpc 一体 | [Markdown](./go-zero/go-zero-guide.md) · [HTML](./go-zero/go-zero-guide.html) |

### 🗄️ 数据存储

| 主题 | 覆盖要点 | 文档 |
| --- | --- | --- |
| **GORM** | 链式 API、关联与预加载、事务与 SavePoint、Hook、迁移器、软删除、读写分离插件、泛型接口 | [Markdown](./gorm/gorm-guide.md) · [HTML](./gorm/gorm-guide.html) |
| **etcd** | Raft 共识、MVCC 多版本、bbolt B+ 树、Lease / Watch / 分布式锁、clientv3 实战、v3.6 要点 | [Markdown](./etcd/etcd-guide.md) · [HTML](./etcd/etcd-guide.html) |
| **InfluxDB 2.x** | Bucket / Token 模型、TSM 存储引擎、Flux 查询、任务与告警、Go 客户端写入查询 | [Markdown](./influxdb-v2/influxdb-v2-guide.md) · [HTML](./influxdb-v2/influxdb-v2-guide.html) |
| **InfluxDB 3** | Rust 重写的 FDAP 栈（Flight + DataFusion + Arrow + Parquet）、SQL 查询、对象存储优先、无限基数、Core vs Enterprise 选型 | [Markdown](./influxdb-v3/influxdb-v3-guide.md) · [HTML](./influxdb-v3/influxdb-v3-guide.html) |
| **MinIO** | S3 兼容对象存储、纠删码、Bucket / Object 模型、分片上传、预签名 URL、2025 起产品线变化 | [Markdown](./minio/minio-guide.md) · [HTML](./minio/minio-guide.html) |

### 📨 消息、任务与调度

| 主题 | 覆盖要点 | 文档 |
| --- | --- | --- |
| **asynq** ⭐ | 本仓库最完整的一篇（1300+ 行）。Redis 分布式任务队列：任务生命周期、延时/重试/死信、优先级与加权队列、任务去重、任务聚合、超时与截止、Prometheus 指标、Asynqmon Web UI 部署 | [Markdown](./asynq/asynq技术文档与使用手册.md) · [HTML](./asynq/asynq技术文档与使用手册.html) |
| **Apache Kafka** | 分区与副本、消费者组与 offset、ISR 与 ACK 语义、Go 客户端（segmentio/kafka-go）实战、Kafka 4.x 移除 ZooKeeper | [Markdown](./kafka/kafka-guide.md) · [HTML](./kafka/kafka-guide.html) |
| **NATS / JetStream** | Subject 寻址、发布订阅与请求响应、队列组、JetStream 持久化流与消费者、nats.go 实战 | [Markdown](./nats/nats-guide.md) · [HTML](./nats/nats-guide.html) |
| **robfig/cron** | 标准 5 字段 / 6 字段 cron 表达式、标准解析器扩展（`@every` / `@daily`）、Job 包装器（`SkipIfStillRunning` 等）、时区与并发、自定义 Job | [Markdown](./robfig-cron/robfig-cron-guide.md) · [HTML](./robfig-cron/robfig-cron-guide.html) |

> 💡 三篇消息/队列文档（Kafka / NATS / asynq）末尾都有**横向选型对比表**，可直接用来做技术选型。

---

## 三、推荐的阅读路径

```
                    ┌─────────────────────────────┐
                    │  Go 泛型 → Go 反射 → lo      │  语言基础
                    └──────────────┬──────────────┘
                                   │
        ┌──────────────────────────┼──────────────────────────┐
        ▼                          ▼                          ▼
┌───────────────┐        ┌──────────────────┐       ┌──────────────────┐
│ Gin           │        │ GORM             │       │ robfig/cron      │
│   ↓           │        │   ↓              │       │   ↓              │
│ Kratos/go-zero│        │ InfluxDB 2/3     │       │ asynq            │
│   ↓           │        │   ↓              │       │   ↓              │
│ etcd          │        │ MinIO            │       │ Kafka / NATS     │
└───────────────┘        └──────────────────┘       └──────────────────┘
   服务开发方向              数据 / IoT 方向             异步与调度方向
```

| 你的目标 | 建议顺序 |
| --- | --- |
| 打牢 Go 基本功 | 泛型 → 反射 → samber/lo |
| 快速写出第一个 API 服务 | Gin → GORM → robfig/cron |
| 做微服务 / 中台 | Gin → Kratos 与 go-zero（对比读）→ etcd → asynq |
| 做 IoT / 监控 / 时序数据 | InfluxDB 2.x → InfluxDB 3（看演进）→ MinIO → Kafka / NATS |
| 正在做技术选型 | 直接跳到每篇末尾的「选型对比」章节，以及 Kafka / NATS / asynq 三篇横向对照 |

---

## 四、两种阅读方式

### 1. Markdown（推荐：便于搜索、复制代码、提 PR）

直接在 GitHub 上打开对应 `.md` 即可，所有代码块均标注语言类型，可一键复制。

### 2. HTML（推荐：便于通读、做离线手册）

每个目录下都有一份**零依赖单文件 HTML**：自带侧边栏目录、顶部 Hero 区、代码块语言标签与语法着色，无需构建、无需联网。

```bash
# 方式一：直接双击打开
#   在文件管理器中打开任意 *-guide.html

# 方式二：起一个本地静态服务（推荐，便于跳转与搜索）
cd open-guide
python -m http.server 8000
# 浏览器访问 http://localhost:8000
```

> HTML 与 Markdown 内容同源，若发现不一致，以 **Markdown 为准**（HTML 为渲染产物）。

---

## 五、每篇文档的标准结构

```
1. 项目简介        —— 它是什么、解决什么问题、核心卖点
2. 功能特性总览     —— 能力清单表格
3. 核心概念与原理   —— 内部机制（架构图 / 数据结构 / 执行流程）
4. 使用实战        —— 可直接运行的 Go 代码 + 命令
5. 版本演进速览     —— 近几个大版本的变化与迁移注意点
6. 常见坑与最佳实践 —— 生产环境里真正会咬人的地方
7. 选型对比        —— 与同类方案的横向对照表
```

---

## 六、上游项目与版本对照

| 文档 | 上游仓库 | 文档对应版本 |
| --- | --- | --- |
| asynq | [hibiken/asynq](https://github.com/hibiken/asynq) | v0.26.0 · asynqmon v0.7.x |
| etcd | [etcd-io/etcd](https://github.com/etcd-io/etcd) | v3.6 |
| Gin | [gin-gonic/gin](https://github.com/gin-gonic/gin) | v1.12.0 |
| go-zero | [zeromicro/go-zero](https://github.com/zeromicro/go-zero) | v1.10.3 |
| GORM | [go-gorm/gorm](https://github.com/go-gorm/gorm) | v1.31.2 |
| InfluxDB | [influxdata/influxdb](https://github.com/influxdata/influxdb) | 2.7 / 3 Core（GA 2025.4） |
| Kafka | [apache/kafka](https://github.com/apache/kafka) | 4.x |
| Kratos | [go-kratos/kratos](https://github.com/go-kratos/kratos) | v3.0.0 |
| MinIO | [minio/minio](https://github.com/minio/minio) | 社区版（AGPLv3） |
| NATS | [nats-io/nats-server](https://github.com/nats-io/nats-server) | v2.11 |
| robfig/cron | [robfig/cron](https://github.com/robfig/cron) | v3 |
| samber/lo | [samber/lo](https://github.com/samber/lo) | v1 |
| Go 泛型 / 反射 | [Go 官方提案与博客](https://go.dev/blog/laws-of-reflection) | Go 1.18+ |

> ⏱ **关于时效性**：开源项目迭代很快，阅读时请以上游最新 release 为准。若发现版本落后或结论过期，欢迎提 Issue / PR 修正。

---

## 七、贡献

非常欢迎补充与纠错，尤其是这几类：

- **新增主题**（Redis、gRPC、Zap、Viper、Prometheus、ClickHouse、MongoDB、Docker/K8s……）
- **版本更新**：上游发布新版本后同步文档
- **纠错**：原理描述、代码错误、过期结论
- **HTML 渲染**：修复样式或高亮问题

贡献方式：

```bash
git clone https://github.com/Weisg1999/open-guide.git
cd open-guide
git checkout -b feat/your-topic
# 新增 your-topic/your-topic-guide.md（建议沿用现有 7 段式结构）
git commit -m "docs: add xxx guide"
git push origin feat/your-topic
```

**写作约定**（保持一致，读者才不会割裂）：

1. 文首用 `>` 引用块写明：一句话定位 + 上游仓库链接 + 对应版本与协议。
2. 统一使用 7 段式结构（见 [第五节](#五每篇文档的标准结构)）。
3. 代码块标注语言（` ```go `、` ```bash `），保证可直接运行。
4. 结尾必须有「常见坑与最佳实践」与「选型对比」。
5. HTML 版本由 Markdown 渲染生成，目录命名保持 `<topic>/<topic>-guide.{md,html}`。

---

## 八、许可证与声明

### 本仓库

本仓库的**文档内容**（Markdown 与 HTML 中的原创讲解、结构组织、图表示例）采用 **MIT 许可证** 发布，详见 [LICENSE](./LICENSE)。

```
MIT License

Copyright (c) 2026 Weisg1999
```

你可以自由地使用、复制、修改、合并、发布、分发这些文档（包括商业用途），只需保留版权声明与许可声明即可。

### 关于上游项目

- 本仓库**不包含任何上游项目的源代码**，所有内容均为面向学习的技术整理与讲解。
- 各篇文档对应的上游项目版权归各自作者所有，其开源协议（多为 MIT / Apache-2.0，MinIO 社区版为 AGPLv3）以各项目官方仓库为准——**MIT 许可仅覆盖本仓库的原创整理部分，不覆盖上游项目本身**。
- 文档中的代码示例、架构说明整理自各项目的官方 README、Wiki、godoc 与公开源码。
- 内容仅供参考，生产环境落地请结合官方文档与实际压测结果验证。

---

<div align="center">

如果这些文档帮到了你，欢迎点个 ⭐ Star 支持一下，也欢迎把它分享给一起写 Go 的同事。

</div>
