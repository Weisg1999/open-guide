# open-guide

[![Docs](https://img.shields.io/badge/docs-18%20篇-16a34a)](./README.md)
[![Language](https://img.shields.io/badge/topic-Go%20%E7%94%9F%E6%80%81-00ADD8?logo=go)](https://go.dev)
[![Format](https://img.shields.io/badge/format-Markdown%20%2B%20HTML-orange)](./README.md)
[![License](https://img.shields.io/badge/license-MIT-green)](./LICENSE)
[![English](https://img.shields.io/badge/README-English-blue)](./README_EN.md)

> **Go 后端技术栈中文深度指南合集** —— 一份能读、能查、能直接抄代码的「技术手册」，而不是又一份 Hello World。
>
> 18 篇长文 · 约 6900 行 Markdown · 每篇同时提供 **Markdown** 与**单文件 HTML**（侧边栏导航 + 代码高亮，浏览器直接打开）。

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
| **gRPC-Go** | HTTP/2 多路复用与 Protobuf 编码、`.proto` 契约与代码生成、四种流模式、拦截器链、deadline 跨进程传播、resolver/balancer | [Markdown](./grpc/grpc-guide.md) · [HTML](./grpc/grpc-guide.html) |

### 🗄️ 数据存储

| 主题 | 覆盖要点 | 文档 |
| --- | --- | --- |
| **GORM** | 链式 API、关联与预加载、事务与 SavePoint、Hook、迁移器、软删除、读写分离插件、泛型接口 | [Markdown](./gorm/gorm-guide.md) · [HTML](./gorm/gorm-guide.html) |
| **go-redis** | 连接池与 `Cmdable`、Pipeline vs TxPipeline、`redis.Nil` 语义、Lua 与分布式锁、Stream 消费组、Cluster slot 与 hash tag、缓存三件套 | [Markdown](./redis/redis-guide.md) · [HTML](./redis/redis-guide.html) |
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

### 🔭 可观测性

| 主题 | 覆盖要点 | 文档 |
| --- | --- | --- |
| **Zap** | 零分配热路径与强类型 Field、`Logger` vs `SugaredLogger`、`zapcore` 四层管线（Encoder / WriteSyncer / LevelEnabler）、Tee 多目的地分流、采样与 `AtomicLevel`、与标准库 `log/slog` 的关系 | [Markdown](./zap/zap-guide.md) · [HTML](./zap/zap-guide.html) |

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

> 🔭 **横切全路径**：`gRPC` 属「服务开发方向」的通信层（Gin 之后读），`go-redis` 属「数据 / IoT 方向」的缓存层（GORM 之后读），`Zap` 是三条路径都该在写第一个服务时同步接入的日志基座。

| 你的目标 | 建议顺序 |
| --- | --- |
| 打牢 Go 基本功 | 泛型 → 反射 → samber/lo |
| 快速写出第一个 API 服务 | Gin → GORM → robfig/cron → **Zap**（把日志规范先立起来） |
| 做微服务 / 中台 | Gin → **gRPC-Go** → Kratos 与 go-zero（对比读）→ etcd → asynq |
| 加缓存 / 分布式锁 | **go-redis** → 再读 asynq / etcd，理解「缓存、任务队列、强一致协调」的边界 |
| 做 IoT / 监控 / 时序数据 | InfluxDB 2.x → InfluxDB 3（看演进）→ MinIO → Kafka / NATS |
| 建立可观测性 | **Zap** → 配合 Prometheus 指标与 OpenTelemetry 链路（日志/指标/链路三者不互相替代） |
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

> HTML 是**构建产物**，由 `tools/build_html.py` 从同名 Markdown 生成，请勿直接编辑。若发现不一致，一律以 **Markdown 为准**。

### 3. 重新生成 HTML（维护者）

生成脚本只用 Python 3 标准库，零第三方依赖。**正文 100% 来自 Markdown**；下面三份文件只承载「展示层」数据，且全部纳入版本控制，因此 HTML 可完整复现：

| 文件 | 作用 |
|---|---|
| [`tools/themes.json`](./tools/themes.json) | 每篇的配色、Hero 副标题与标签 |
| [`tools/fragments.json`](./tools/fragments.json) | SVG 架构图、要点卡片、提示框等视觉增强（Markdown 无对应源），按章节锚点注入，共 274 块 / 72 张图 |
| [`tools/anchor_map.json`](./tools/anchor_map.json) | 旧版手工 HTML 的语义锚点 → 标题 slug 映射，保证增强块内的跳转不断链 |

```bash
python tools/build_html.py                 # 重新生成全部指南
python tools/build_html.py --topic gin     # 只生成某一篇
python tools/build_html.py --check         # 校验是否漂移（CI 使用，漂移则退出码 1）
python tools/build_html.py --stats         # 各篇章节/代码块/表格统计 + 内链锚点自检
```

新增主题无需改构建配置：脚本会自动发现 `*/ *.md`，未配置配色的主题按目录名分配一组稳定色板。CI（[`.github/workflows/check-html.yml`](./.github/workflows/check-html.yml)）会在每次 push / PR 执行 `--check`，从机制上杜绝 MD/HTML 漂移。

---

## 五、每篇文档的必备要素

早期约定是严格的「7 段式」，但实践中发现：asynq 这类深度手册更适合按「是什么 → 架构 → 概念 → 功能详解 → 完整示例 → 部署 → 坑 → 演进 → 对比」九章展开，语言教程则天然按「入门 → 高级」的难度递进。硬凑段数反而伤害内容。因此约定改为 **「7 个必备要素」**：编号与标题形式自由，要素不能少。

| # | 要素 | 判定标准 | 常见等价写法 |
|---|---|---|---|
| ① | 定位与简介 | 是什么、解决什么问题、核心卖点、适用/不适用场景 | 项目简介 / 库简介 / 是什么 / 为什么需要 |
| ② | 能力清单 | 一张能扫读的能力总览表 | 功能特性总览 / 能力总览 / 功能分类详解 / 数据模型 |
| ③ | 原理与核心概念 | 内部机制：架构、数据结构、执行流程，而非 API 罗列 | 核心概念与原理 / 架构原理 / 实现原理 / 设计原理 |
| ④ | 可运行实战 | 标注语言的 Go 代码 + 命令，能直接抄 | 使用实战 / 完整可运行示例 / 部署指南 |
| ⑤ | 版本与演进 | 对应上游版本、近期大版本变化、迁移注意点 | 版本演进速览 / 版本要点 / 产品线变化 |
| ⑥ | 坑与最佳实践 | 生产环境真正会咬人的地方 + 对策 | 常见坑与最佳实践 / 最佳实践与常见陷阱 / 注意事项 |
| ⑦ | 横向选型对比 | 与同类方案的对照表 + 一句话决策结论（含「何时不该用」） | 选型对比 / 选型小结 / 如何取舍 / 与 X 的关系 |

**形式规则**

- 章号可用 `1.` / `一、` / 无编号；标题文字不必字面一致；一个要素可拆成多章（如 asynq 把 ④ 拆为「功能详解 / 完整示例 / 部署指南」），也可与相邻要素合并。
- **⑥ ⑦ 必须有独立章节且置于文末**——它们最常被直接跳读，也是本仓库区别于「API 翻译」的核心价值。
- 语言特性类教程（Go 泛型、Go 反射）没有上游 release，⑤ 可用「适用 Go 版本 + 语言演进说明」替代；其余要素仍需覆盖。
- 文首 `>` 引用块写明「一句话定位 + 上游仓库 + 对应版本 + 协议」，是 ⑤ 的最低要求。

**自检**：`python tools/build_html.py --audit` 会输出逐篇的要素覆盖矩阵并列出缺失项，CI 同时执行 `--check` 与 `--audit`。

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
| go-redis | [redis/go-redis](https://github.com/redis/go-redis) | v9（模块路径 `/v9`） |
| gRPC-Go | [grpc/grpc-go](https://github.com/grpc/grpc-go) | v1.x（API 稳定，持续迭代） |
| Zap | [uber-go/zap](https://github.com/uber-go/zap) | v1.27.x |
| Go 泛型 / 反射 | [Go 官方提案与博客](https://go.dev/blog/laws-of-reflection) | Go 1.18+ |

> ⏱ **关于时效性**：开源项目迭代很快，阅读时请以上游最新 release 为准。若发现版本落后或结论过期，欢迎提 Issue / PR 修正。

---

## 七、贡献

非常欢迎补充与纠错，尤其是这几类：

- **新增主题**（Viper、Prometheus、ClickHouse、MongoDB、Docker/K8s、OpenTelemetry、slog 实践……；Redis / gRPC / Zap 已补齐）
- **版本更新**：上游发布新版本后同步文档
- **纠错**：原理描述、代码错误、过期结论
- **构建脚本**：改进 `tools/build_html.py` 的解析与渲染（请勿直接手改 HTML）

贡献方式：

```bash
git clone https://github.com/Weisg1999/open-guide.git
cd open-guide
git checkout -b feat/your-topic
# 新增 your-topic/your-topic-guide.md（请覆盖第五节的 7 个必备要素）
# 生成 HTML：python tools/build_html.py --topic your-topic
git commit -m "docs: add xxx guide"
git push origin feat/your-topic
```

**写作约定**（保持一致，读者才不会割裂）：

1. 文首用 `>` 引用块写明：一句话定位 + 上游仓库链接 + 对应版本与协议。
2. 覆盖[第五节](#五每篇文档的必备要素)的 7 个必备要素（章号/标题形式可灵活，要素不可省）。
3. 代码块标注语言（` ```go `、` ```bash `），保证可直接运行。
4. 文末必须有两章分别覆盖「常见坑与最佳实践」与「选型对比」，后者要有横向对照表和一句话决策结论。
5. HTML 由 `python tools/build_html.py` 从 Markdown 生成，**不要手改 HTML**；目录命名保持 `<topic>/<topic>-guide.{md,html}`（新增主题未配色时会自动分配）。

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
