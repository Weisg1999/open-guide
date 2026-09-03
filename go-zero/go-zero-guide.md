# go-zero 技术介绍与使用指南

> 好未来开源的云原生 Go 微服务框架（CNCF Landscape 收录）：.api DSL + goctl 代码生成、内置弹性设计（自适应熔断/限流/负载卸载）、web 与 rpc 一体。
> 仓库：<https://github.com/zeromicro/go-zero>　|　当前主线：**v1.10.3**（2026.8.1 发布，MIT 协议）

---

## 1. 项目简介

go-zero 起源于 2018 年好未来从 Java+MongoDB 单体向微服务的迁移（作者 Kevin Wan / kevwan），在千万级日活业务上打磨多年后开源。它的定位一句话讲清：

> **一个自带大量工程实践（engineering practices）的 web + rpc 框架**——把微服务的稳定性设施（超时、熔断、限流、负载卸载、缓存）内建到框架里，让业务开发者"想不稳定都难"。

核心设计原则：**简单（一条路做一件事）、高可用（高并发下稳定）、弹性（面向失败的编程，自适应保护）、工具化（goctl 消灭样板代码）**。

与其他 Go 微服务框架最鲜明的差异：

| 差异点 | 说明 |
|---|---|
| **.api DSL 而非 proto** | 用自定义的简洁语法描述 HTTP API，一条命令生成 Go 服务端 + 多语言客户端（TypeScript/Dart/Kotlin/Java/iOS/Android） |
| **弹性内建、零配置** | 自适应熔断（按错误统计自动开合）、自适应负载卸载（按 CPU/并发丢弃低优先级请求）、链路级超时控制——**默认开启无需配置** |
| **model 层也生成** | goctl 从 MySQL DDL 生成 CRUD 代码，内置缓存一致性方案（单飞 singleflight + 缓存自动失效） |
| **单体友好** | API 服务可独立部署（无需注册中心），也可以走完整微服务模式（etcd 服务发现 + zrpc） |

适用场景：高并发 C 端接口（教育/电商/直播类业务是它的主场）、需要前后端多端客户端生成的团队、追求"框架兜底稳定性"的中大型微服务集群。

### 安装与快速开始

```bash
# 安装 goctl（代码生成工具，go-zero 的灵魂）
go install github.com/zeromicro/go-zero/tools/goctl@latest
# 或 brew install goctl / docker pull kevinwan/goctl

# 项目中引入框架
go get -u github.com/zeromicro/go-zero
```

---

## 2. 功能特性总览

| 能力 | 说明 |
|---|---|
| API 服务（rest） | 自定义 .api DSL 描述路由/参数/校验，net/http 兼容，自动参数校验（options 枚举、range 等） |
| RPC 服务（zrpc） | 基于 gRPC 的封装：服务发现（etcd/k8s 直连）、负载均衡（p2c/round_robin/一致性哈希）、中间件链 |
| goctl 代码生成 | api→服务端/多语言客户端；proto→zrpc；DDL→model CRUD+缓存；k8s 部署 yaml；Dockerfile |
| 弹性设计 | 链式超时控制、并发控制（maxConns/maxThreads）、自适应熔断、自适应 load shedding、限流（内置并发/权重两种算法） |
| 缓存体系 | 内置 redis 缓存（cacheRedis）+ sqlc/sqlx 模型层缓存（singleflight 防击穿、时间衰减过期） |
| 服务治理 | 服务发现（discov: etcd 直连/k8s endpoint）、健康检查、prometheus 指标内置 |
| 可观测 | logx 结构化日志（敏感数据脱敏）、OpenTelemetry tracing、内置 metrics、SSE |
| 微服务网关 | gateway 组件：HTTP→gRPC 协议转换、洋葱模型自定义中间件、trace 头透传 |
| **MCP Server SDK** | v1.8.3+ 内置 Model Context Protocol 服务端：SSE 通道、工具注册、动态 Prompt——把 Go 服务直接挂进 AI Agent 生态 |
| AI 原生开发 | ai-context / zero-skills / mcp-zero 三件套：让 Claude/Copilot/Cursor 生成符合框架规范的代码 |
| 工具箱（core） | 300+ 实用工具：sqlx/mapreduce/threading/collection/stringx/bloom/rolling window 等 |

---

## 3. 核心概念与原理

### 3.1 .api DSL：比 proto 更直白的接口描述

```
type (
    Request {
        Name string `path:"name,options=[you,me]"`   // 路径参数 + 枚举自动校验
    }
    Response {
        Message string `json:"message"`
    }
)

service greet-api {
    @handler GreetHandler
    get /greet/from/:name(Request) returns (Response)
}
```

- 语法就是"Go 结构体 + service 声明"，心智成本接近零；tag 即校验规则（path/header/form/json + options/range）；
- `goctl api go` 生成完整服务端骨架，`goctl api ts/dart/java/...` 生成各端客户端——**前后端从同一契约出发**；
- 生成的目录结构是框架强约束的工程规范（见 3.2）。

### 3.2 生成项目的分层结构

```
greet/
├── etc/greet-api.yaml        # 配置（结构由 conf 生成器/手写）
├── greet.go                  # 入口
└── internal/
    ├── config/config.go      # 配置定义（强类型）
    ├── handler/              # 路由 handler：参数绑定→调 logic（薄层）
    │   ├── greethandler.go
    │   └── routes.go
    ├── logic/greetlogic.go   # 业务逻辑唯一去处（框架给的正道）
    ├── svc/servicecontext.go # 依赖容器：mysql/redis/rpc client 在此装配
    └── types/types.go        # 请求/响应结构
```

关键约定：**handler 永远薄、logic 装业务、svc 集中注入依赖**。所有请求走 `handler → logic → (svc 里的依赖)` 单向流动，杜绝业务散落。

### 3.3 弹性设计（go-zero 的灵魂）

框架内置的防护体系，**默认开启、多数无需配置**：

```
请求进入 rest/zrpc
  → 并发控制（CPU 高时拒绝新请求）
  → 自适应负载卸载 load shedding（按 CPU/响应时间丢弃，保护过载节点）
  → 限流（内置算法，可按 API 粒度配置）
  → 熔断器 breaker（滑动窗口统计错误率，自动熔断/半开探测恢复）
  → 链式超时 timeout control（上游剩余时间沿 context 传递给下游）
```

- **自适应**是关键词：熔断按统计自动开合，load shedding 按 CPU 负载动态调节，不需要为每个依赖手配阈值；
- 超时是**链式**的：API 入口设 800ms，调了 3 个下游，剩余预算通过 context 自动传递——避免"上游已超时、下游还在跑"的资源浪费。

### 3.4 服务发现与负载均衡（zrpc + discov）

- **discov**：基于 etcd 的服务发现（`discov://etcd-hosts/key`）；K8s 环境可直连 endpoint（无需 etcd）；
- **负载均衡**：默认 **p2c**（power of two choices，随机两节点选负载低者，比轮询更抗抖动）；v1.9.3 新增**一致性哈希**（session affinity：`zrpc.SetHashKey(ctx, "user_123")` 同 key 路由到同后端，提升缓存命中）；
- 客户端内置熔断与超时传递，出站调用默认被保护。

### 3.5 model 层生成与缓存一致性（goctl model）

```bash
goctl model mysql ddl -src user.sql -dir internal/model -c   # -c 开启缓存
```

从 DDL 生成：CRUD 方法、`FindOne`（**优先查缓存**）、`Insert/Update/Delete`（**自动失效缓存**）。缓存一致性方案：

- 读：cache miss → singleflight 合并并发击穿 → 回源 DB → 写缓存（带过期时间 + 随机衰减防雪崩）；
- 写：先更新 DB，再删除/更新缓存；
- 业务代码不碰缓存细节，model 接口天然带缓存。

### 3.6 微服务网关（gateway）

HTTP API → gRPC 后端的协议转换网关：在 .api 里直接引用 proto 定义的服务方法，自动做 HTTP/JSON ↔ gRPC/protobuf 转换；v1.9.0 起支持洋葱模型自定义中间件，并修复了 W3C trace 头（traceparent/baggage）向 gRPC metadata 的透传。

### 3.7 MCP：把 Go 服务挂进 AI 生态（v1.8.3+ 重磅）

go-zero 内置 **Model Context Protocol Server SDK**：

```go
server := mcp.NewMcpServer(c)
server.AddTool(tool, func(ctx context.Context, req *mcp.ServerRequest) (*mcp.ToolResult, error) {
    // 业务逻辑，直接复用 go-zero 的 svc/logic 体系
    return &mcp.ToolResult{...}, nil
})
```

- 完整的 SSE 推送通道、JSON-RPC 协议、能力协商、错误码规范；
- 支持工具（Schema 校验入参）、静态 Prompt 模板 + 动态 Prompt Handler、资源（文件/图片/二进制）注册与推送；
- v1.10.2 增加 `WithRequestMetadataExtractor`：HTTP 请求头/query/path 注入工具 handler 的 context（多租户透传）。

配套的 **AI 原生三件套**：`ai-context`（AI 助手的工作流指引，喂给 Copilot/Cursor）、`zero-skills`（模式库与示例）、`mcp-zero`（通过 MCP 实时调 goctl 生成代码）——让 AI 编码助手产出符合框架规范的代码。

---

## 4. 使用实战

### 4.1 API 服务：从 DSL 到跑起来

```bash
goctl api -o greet.api                                    # 生成 api 模板
goctl api go -api greet.api -dir greet                    # 生成 Go 服务端
goctl api ts -api greet.api -dir ./frontend/src/api       # 生成 TS 客户端
cd greet && go mod tidy
go run greet.go -f etc/greet-api.yaml                     # 默认 8888 端口
curl -i http://localhost:8888/greet/from/you
```

写业务（只改 logic）：

```go
// internal/logic/greetlogic.go
func (l *GreetLogic) Greet(req *types.Request) (*types.Response, error) {
    // 业务逻辑写这里；svc 里的依赖通过 l.svcCtx 访问
    return &types.Response{Message: "hello " + req.Name}, nil
}
```

### 4.2 带依赖注入的完整服务

```go
// internal/svc/servicecontext.go —— 依赖集中装配
type ServiceContext struct {
    Config    config.Config
    UserModel model.UserModel   // goctl model 生成（带缓存）
    OrderRpc  order.Order       // goctl rpc 生成的客户端
}

func NewServiceContext(c config.Config) *ServiceContext {
    conn := sqlx.NewMysql(c.Mysql.DataSource)
    return &ServiceContext{
        Config:    c,
        UserModel: model.NewUserModel(conn, c.CacheRedis),
        OrderRpc:  order.NewOrder(zrpc.MustNewClient(c.OrderRpc)),
    }
}

// internal/logic/userlogic.go —— logic 里用依赖
func (l *GetUserLogic) GetUser(req *types.GetUserReq) (*types.UserResp, error) {
    u, err := l.svcCtx.UserModel.FindOne(l.ctx, req.Id)  // 自动走缓存
    if err != nil {
        return nil, err
    }
    orders, err := l.svcCtx.OrderRpc.ListByUser(l.ctx, &order.ListReq{UserId: req.Id})
    // ...
}
```

### 4.3 RPC 服务（zrpc）

```bash
goctl rpc new order                                    # 生成 zrpc 服务骨架
goctl rpc protoc order.proto --go_out=. --go-grpc_out=. --zrpc_out=.
```

```yaml
# etc/order.yaml
Name: order-rpc
ListenOn: 0.0.0.0:9000
Etcd:
  Hosts:
    - 127.0.0.1:2379
  Key: order.rpc
```

```go
// 调用方
conf := zrpc.RpcClientConf{
    Endpoints:  []string{"127.0.0.1:9000"},   // 直连（小规模/测试）
    // Etcd: discov配置（生产），或 K8s 直连
    BalancerName: "consistent_hash",           // 可选：一致性哈希
}
client := zrpc.MustNewClient(conf)
orderCli := order.NewOrder(client)
```

### 4.4 model 生成（DDL → CRUD + 缓存）

```bash
# DDL 建表后
goctl model mysql ddl -src schema/user.sql -dir internal/model -c
```

```go
um := model.NewUserModel(sqlx.NewMysql(dsn), cache.CacheConf{
    {RedisConf: redis.RedisConf{Host: "127.0.0.1:6379"}, Weight: 100},
})
u, err := um.FindOne(ctx, 42)    // 命中缓存直接返回；miss 自动 singleflight + 回源
err = um.Update(ctx, u)          // 更新后自动失效缓存
```

### 4.5 中间件与配置

```go
// 自定义中间件（洋葱模型，签名简单）
func AuthMiddleware(next http.HandlerFunc) http.HandlerFunc {
    return func(w http.ResponseWriter, r *http.Request) {
        if r.Header.Get("X-Token") == "" {
            httpx.Error(w, errors.New("unauthorized"))
            return
        }
        next(w, r)
    }
}

// .api 里声明
// @middleware AuthMiddleware
```

```yaml
# etc/greet-api.yaml —— 配置即治理开关
Name: greet-api
Host: 0.0.0.0
Port: 8888
Timeout: 8000           # 链式超时入口预算
MaxConns: 1000          # 并发控制
CPUBusyThreshold: 800   # load shedding CPU 阈值（1/1000）
Prometheus:
  Host: 0.0.0.0
  Port: 9091
Telemetry:
  Endpoint: http://jaeger:14268/api/traces
```

---

## 5. 版本演进速览（v1.8 → v1.10）

| 版本 | 时间 | 要点 |
|---|---|---|
| v1.8.3 | 2024 末 | **MCP Server SDK**（SSE、工具系统、动态 Prompt、资源推送） |
| v1.8.4 | 2025 初 | 持续性能剖析（对接 pyroscope）、embed.FS 文件服务 |
| v1.8.5 | 2025 | **SQL 读写分离**、REST serverless 模式支持 |
| v1.9.0 | 2025.8 | mongo-driver v2、logx 敏感数据脱敏、gateway 洋葱中间件、redis 消费者组 |
| v1.9.3 | 2025 | **一致性哈希负载均衡**（session affinity）、gateway trace 头透传修复、trace agent 单次初始化 |
| v1.10.x | 2026 | MCP 元数据桥（请求头/query 注入 context）、Go 1.26 etcd URI 兼容修复、JSON5 配置、stringx 安全修复、Redis XGroupSetID |

版本策略保守稳定（大版本内无破坏性变更），goctl 与框架分开发布（当前 goctl v1.10.2）。

---

## 6. 常见坑与最佳实践

1. **别改生成物**：handler/types/routes 是生成物，重新生成会覆盖；自定义中间件/业务逻辑放 logic 与独立文件，`goctl api go --style go_zero` 控制命名风格要一开始定好；
2. **api DSL 的校验是入口级**：options/range 只覆盖简单校验，复杂规则在 logic 里用 `github.com/go-playground/validator` 或手写；
3. **svc 不是全局变量仓库**：依赖按需注入，跨服务共享 client 用 package 级单例 + conf 驱动，避免循环依赖；
4. **模型缓存的一致性边界**：goctl 缓存方案是"先 DB 后删缓存"，对"读己之写"敏感的流程要显式绕过缓存或用 `CacheNull`/版本号策略；
5. **超时预算要合理分级**：入口 Timeout 应小于网关/SLB 超时；下游默认继承 context 剩余时间，单独设置时不能超过上游预算；
6. **load shedding 会拒绝请求**：CPUBusyThreshold 调试期看日志 `dropped` 计数，别把阈值调太低误伤流量；被拒请求返回 503，前端要有降级文案；
7. **etcd 依赖是可选的**：单体/小规模用 Endpoints 直连即可；K8s 内用 k8s 直连省一套 etcd 运维；
8. **升级注意 goctl 与框架版本对齐**：生成代码风格随 goctl 版本变化，团队统一版本（Makefile 固定），避免 diff 噪音；
9. **Go 1.26+ 用户注意**：v1.10.2 修复了 etcd 多地址 URI 的解析兼容，老版本在 Go 1.26 下服务发现会挂——保持框架更新；
10. **日志脱敏**：logx v1.9+ 支持敏感字段掩码（`logx.SetUp` 配置 masking），涉及手机号/身份证的服务尽早启用。

---

## 7. 选型对比

| 维度 | go-zero | Kratos | Gin/Echo | go-micro |
|---|---|---|---|---|
| 接口契约 | **.api DSL（自研，极简）** | proto（google.api.http 注解） | 手写路由 | proto-first |
| 代码生成 | **服务端 + 6 类语言客户端 + model + 部署物** | 服务端 + HTTP/gRPC + OpenAPI | 无 | 少 |
| 弹性内建 | **自适应熔断/卸载/链路超时，默认开启** | 中间件按需组装 | 无 | 部分 |
| 数据层 | **DDL→CRUD+缓存一体生成** | 不涉及（自选 GORM/ent） | 不涉及 | 不涉及 |
| AI 生态 | **MCP SDK + AI 编码三件套** | 无 | 无 | 无 |
| 学习曲线 | 低（DSL 直白，约定多） | 中（proto/wire/layout） | 低 | 中-高 |
| 社区 | 国内活跃、文档完善 | 国内活跃 | 全球最广 | 海外 |

一句话：**要"框架兜底稳定性 + 生成到客户端和 model 层"的工具流选 go-zero；要 proto 契约与更自由的架构组装选 Kratos；两者都比裸 Gin 更适合长期演进的微服务集群**。

参考：[官方文档 go-zero.dev](https://go-zero.dev/)、[go-zero-examples](https://github.com/zeromicro/go-zero-examples)、[zero-doc 快速教程（shorturl/bookstore）](https://github.com/zeromicro/zero-doc)、[zero-contrib 扩展仓库](https://github.com/zeromicro/zero-contrib)。
