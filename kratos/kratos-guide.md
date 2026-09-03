# Kratos 技术介绍与使用指南

> Bilibili 开源的云原生 Go 微服务框架：API-first（Protobuf 驱动）、HTTP/gRPC 双协议统一传输、可插拔组件化设计。
> 仓库：<https://github.com/go-kratos/kratos>　|　当前主线：**v3.0.0**（2026.6 发布，Go 1.25+）

---

## 1. 项目简介

Kratos（名字取自希腊神话战神）是 Bilibili 沉淀多年微服务工程实践后开源的 Go 框架，核心哲学是**"框架不绑架业务"**：它不是全家桶式重框架，而是一组小而明确的原语（transport、middleware、registry、config、log、encoding）+ 代码生成工作流，让团队把精力放在业务逻辑上。

与 Spring Cloud 式"大而全"或 go-micro 式"强抽象"不同，Kratos 的取舍是：

- **API-first**：接口即契约。用 Protobuf 定义服务，一次生成 gRPC 与 RESTful HTTP 两套 server/client 代码，proto 文件本身就是文档（注释可生成 OpenAPI）；
- **双协议统一**：HTTP 与 gRPC 走同一套 `transport.Server` 抽象、同一套中间件链、同一套错误/元数据模型；
- **组件可插拔**：注册中心、配置中心、日志、编码器都是接口 + contrib 生态（etcd/consul/nacos/apollo/zap…按需引入）；
- **工程脚手架内置**：CLI 一条命令生成符合社区共识的分层项目骨架（kratos-layout），杜绝"每个团队一个目录结构"。

适用场景：中大型团队的 Go 微服务标准化、需要同时暴露 gRPC（内部）与 HTTP（网关/前端）的服务、追求代码生成而非手写胶水的工程文化。**不适合**：简单的单体 Web 应用（Gin/Echo 更轻）或 CLI 工具。

### 安装与快速开始

```bash
# 要求：Go 1.25+、protoc、protoc-gen-go
# 安装 CLI
go install github.com/go-kratos/kratos/cmd/kratos/v3@latest

# 创建服务（默认拉取 kratos-layout 模板）
kratos new helloworld
cd helloworld
go mod tidy
kratos run
# 访问 http://localhost:8000/helloworld/kratos

# 完整的 proto 工作流
kratos proto add api/helloworld/helloworld.proto    # 生成 proto 骨架
kratos proto client api/helloworld/helloworld.proto # 生成 client 代码
kratos proto server api/helloworld/helloworld.proto -t internal/service # 生成 server 桩
go generate ./...
kratos run
```

最小化应用（不依赖脚手架）：

```go
package main

import (
    "github.com/go-kratos/kratos/v3"
    "github.com/go-kratos/kratos/v3/transport/grpc"
    "github.com/go-kratos/kratos/v3/transport/http"
)

func main() {
    httpSrv := http.NewServer(http.Address(":8000"))
    grpcSrv := grpc.NewServer(grpc.Address(":9000"))

    app := kratos.New(
        kratos.Name("helloworld"),
        kratos.Version("v1.0.0"),
        kratos.Server(httpSrv, grpcSrv), // 一个进程同时挂 HTTP + gRPC
    )
    if err := app.Run(); err != nil {
        panic(err)
    }
}
```

---

## 2. 功能特性总览

| 能力 | 说明 |
|---|---|
| API-first 工作流 | proto 定义接口 → 自动生成 HTTP/gRPC 双协议代码；proto 注释可产出 OpenAPI 文档 |
| 统一传输层 | `transport/http` 与 `transport/grpc` 同一抽象：Filter（中间件）、Router、Metadata、Codec 对等 |
| 中间件（洋葱模型） | `middleware.Middleware` 签名统一，Server/路由/Match 级别挂载；内置 recovery/logging/validate/tracing/metrics |
| 服务注册发现 | `registry.Registrar/Discovery` 接口；contrib 支持 etcd、consul、nacos、zookeeper、kubernetes 等 |
| 配置中心 | `config.Config`：多来源（file/env/apollo/nacos/consul）按 key 合并、热更新 Watch |
| 日志 | v3 基于标准库 `log/slog`，Kratos 提供 `log.NewLogger/NewHandler` 与过滤能力；OTel 扩展在 contrib |
| 错误模型 | proto 定义错误码 `errors.Errorf(500, "USER_NOT_FOUND", ...)`，HTTP status 与 gRPC code 自动双向映射 |
| 元数据传递 | `metadata` 跨 HTTP header / gRPC metadata 的统一键值对透传（如 x-md-global-） |
| 校验 | protovalidate（PGV 继任者）注解驱动 + v3 支持自定义 validator |
| 弹性组件 | selector（负载均衡：round_robin/p2c/weighted）、circuitbreaker（熔断）、rate（限流） |
| 可观测 | OpenTelemetry tracing/metrics 一等公民（middleware + contrib handler） |
| WebSocket | http.Server 原生支持 ws 升级与流式接口绑定 |
| 代码生成 | protoc-gen-go-http / protoc-gen-go-errors / protoc-gen-go-grpc 生成器家族 |

---

## 3. 核心概念与原理

### 3.1 kratos-layout：约定优于配置的分层

CLI 生成的项目骨架（社区模板 [kratos-layout](https://github.com/go-kratos/kratos-layout)）：

```
helloworld/
├── api/helloworld/          # proto 契约 + 生成的 *.pb.go / *_http.pb.go / *_grpc.pb.go
├── cmd/helloworld/main.go   # 唯一入口：装配 wire 注入
├── configs/                 # 本地配置文件
└── internal/
    ├── server/              # 装配层：newHTTPServer/newGRPCServer（挂中间件、路由）
    ├── service/             # 接口实现层：实现生成的 Service 接口，做参数组装
    ├── biz/                 # 业务核心层：Entity + Repo 接口定义（依赖倒置核心）
    ├── data/                # 数据层：Repo 接口的实现（DB/缓存/外部调用）
    └── conf/                # 配置结构（proto 生成）
```

依赖方向严格单向：`service → biz ← data`。**biz 定义 Repo 接口、data 实现它**——业务层不依赖任何具体存储，这是脚手架里最重要的工程决策（依赖倒置）。`cmd` 使用 [google/wire](https://github.com/google/wire) 在编译期完成依赖注入。

### 3.2 App：生命周期管理器

`kratos.New()` 返回的 App 是进程级生命周期容器：

- 聚合任意多个 `transport.Server`（HTTP/gRPC 可并存，也可多个实例）；
- 生命周期钩子：`BeforeStart / AfterStart / BeforeStop / AfterStop`（清理资源、预热的挂点）；
- 监听系统信号（默认 SIGINT/SIGTERM，`kratos.Signal()` 可改），触发**优雅退出**：先注销注册中心 → 再停止接收新请求 → 等待存量请求完成；
- 若配置了 `Registrar`，启动时自动注册、退出时自动反注册。

### 3.3 统一传输层：一套代码，双协议

proto 里的 `google.api.http` 注解声明 REST 映射：

```protobuf
service Greeter {
  rpc SayHello (HelloRequest) returns (HelloReply) {
    option (google.api.http) = {
      get: "/helloworld/{name}"
    };
  }
}
```

protoc-gen-go-http 据此生成 HTTP server 代码：GET `/helloworld/kratos` 的 path 参数自动绑定到 `HelloRequest.Name`，同一服务同时提供 gRPC `SayHello`。**路由、参数绑定、编解码全部生成，不写胶水**。v3 还支持 `google.api.HttpBody` 作为响应（直接控制原始 HTTP body，用于返回 HTML/文件）。

### 3.4 中间件：洋葱模型

```go
type Middleware func(Handler) Handler
type Handler func(ctx context.Context, req any) (any, error)
```

请求像穿透洋葱一样经过各层中间件（鉴权 → 日志 → tracing → validate → recovery），每层可前后夹逻辑。挂载级别：

```go
// 全局（该 server 所有路由）
http.Server(http.Filter(middleware.Recovery(), middleware.Logging(...)))
// 路由级
http.Server(http.Middleware(
    match.New().Route("/api.v1.Greeter/.*", jwt.Server(...)).Build(), // 仅匹配路由走 JWT
    middleware.Recovery(),
))
```

gRPC 同样用 `grpc.Middleware/StreamInterceptor/UnaryInterceptor` 挂载——**中间件跨协议复用**是统一传输层的直接收益。

### 3.5 服务注册发现与 RPC

服务端把 endpoint 注册到 registry；客户端用 discovery 构建自定义 gRPC resolver / HTTP 客户端，配合 **selector** 做负载均衡（p2c/round_robin/weighted、subset），并可用 circuitbreaker/rate 中间件包住出站调用：

```go
conn, err := grpc.DialInsecure(ctx,
    grpc.WithEndpoint("discovery:///'+serviceName"),
    grpc.WithDiscovery(r),          // registry.Discovery
    grpc.WithMiddleware(
        circuitbreaker.Client(),    // 熔断
        selector.Client(selector.NewRoundRobin()), // 负载均衡
    ),
)
```

### 3.6 配置系统

`config.New(config.Source(file.NewSource(path), ...))` 拉取多来源并按键合并（后加载覆盖先加载）；`config.Watch(ctx, "service.name", func(...))` 热更新。v3 新增**泛型 Get**：`conf.Get[T](...)` 直接反序列化为目标类型。结构由 proto 定义（configs 里是 yaml，`conf.pb.go` 是结构）。

### 3.7 错误模型

```go
// 定义（通常 proto 生成错误码定义）
err := errors.Errorf(500, "USER_NOT_FOUND", "user %d not found", id)

// HTTP 响应：status 500 + body {"code":500,"reason":"USER_NOT_FOUND","message":"..."}
// gRPC 响应：自动映射 status details
// 客户端还原
if errors.IsForbiddenException(err) { ... }
```

业务错误带 **reason 机器码**（区别于裸 HTTP status），双协议自动映射——网关/前端拿 reason 做国际化文案。

---

## 4. 使用实战

### 4.1 proto 契约与生成（完整流程）

```protobuf
// api/helloworld/v1/greeter.proto
syntax = "proto3";
package helloworld.v1;

import "google/api/annotations.proto";
import "validate/validate.proto";   // protovalidate

message CreateGreetingRequest {
  string name = 1 [(validate.rules).string.min_len = 1];
}
message CreateGreetingReply { string greeting = 1; }

service GreeterService {
  rpc CreateGreeting (CreateGreetingRequest) returns (CreateGreetingReply) {
    option (google.api.http) = { post: "/v1/greetings" body: "*" };
  }
}
```

```bash
kratos proto client api/helloworld/v1/greeter.proto  # 生成 pb/grpc/http client+server
kratos proto server api/helloworld/v1/greeter.proto -t internal/service
```

### 4.2 业务实现（三层穿透）

```go
// internal/service/greeter.go —— 接口实现层
type GreeterService struct {
    v1.UnimplementedGreeterServiceServer
    uc *biz.GreeterUsecase
}

func (s *GreeterService) CreateGreeting(ctx context.Context, req *v1.CreateGreetingRequest) (*v1.CreateGreetingReply, error) {
    g, err := s.uc.CreateGreeting(ctx, req.GetName()) // 调 biz
    if err != nil {
        return nil, errors.Errorf(500, "CREATE_FAILED", "create greeting: %v", err)
    }
    return &v1.CreateGreetingReply{Greeting: g.Text}, nil
}

// internal/biz/greeter.go —— 业务核心（定义 Repo 接口）
type GreeterRepo interface {
    Save(ctx context.Context, g *Greeter) (*Greeter, error) // 接口在 biz，实现在 data
}

type GreeterUsecase struct { repo GreeterRepo }

func (uc *GreeterUsecase) CreateGreeting(ctx context.Context, name string) (*Greeter, error) {
    if name == "forbidden" {
        return nil, errors.Forbidden("NAME_FORBIDDEN", "name is forbidden")
    }
    return uc.repo.Save(ctx, &Greeter{Name: name, Text: "hello " + name})
}

// internal/data/greeter.go —— 数据层（实现 Repo）
func (r *greeterRepo) Save(ctx context.Context, g *biz.Greeter) (*biz.Greeter, error) {
    // GORM ent or redis ...
    return g, nil
}
```

### 4.3 装配 server 与中间件

```go
// internal/server/http.go
func NewHTTPServer(c *conf.Server, greeter *service.GreeterService, logger *slog.Logger) *http.Server {
    var opts = []http.ServerOption{
        http.Middleware(
            recovery.Recovery(),                       // panic 恢复
            selector.Server(                            // 按路由白名单跳过鉴权
                selector.Match(selectSkipPaths(), nil),
                selector.Not(selectSkipPaths(), jwt.Server(func(token *jwt.Token) (any, error) {
                    return auth.VerifyToken(token)      // v3: contrib/middleware/jwt
                })),
            ),
            logging.Server(logger),
            validate.Validator(),                       // protovalidate 注解校验
            tracing.Server(),
            metrics.Server(),
        ),
    }
    if c.Http.Network != "" { opts = append(opts, http.Network(c.Http.Network)) }
    srv := http.NewServer(opts...)
    v1.RegisterGreeterServiceHTTPServer(srv, greeter)
    return srv
}
```

### 4.4 注册发现（以 etcd 为例）

```go
import etcdreg "github.com/go-kratos/kratos/contrib/registry/etcd/v3"

client, _ := clientv3.New(clientv3.Config{Endpoints: []string{"127.0.0.1:2379"}})
r := etcdreg.New(client)

app := kratos.New(
    kratos.Name("helloworld"),
    kratos.Server(httpSrv, grpcSrv),
    kratos.Registrar(r),   // 启动注册 / 退出注销
)
```

### 4.5 配置加载与使用

```go
// cmd 里加载 configs/config.yaml
c := config.New(
    config.WithSource(
        file.NewSource("configs"),
        env.NewSource(), // 环境变量覆盖
    ),
)
defer c.Close()

if err := c.Load(); err != nil { panic(err) }

var bc conf.Bootstrap
if err := c.Scan(&bc); err != nil { panic(err) }

// 热更新
_ = c.Watch(ctx, "server", func(key string, value config.Value) {
    // 拉新值重建连接等
})
```

---

## 5. v3.0 版本要点与 v2 迁移

v3 主旨：**削减核心依赖、把隐式行为显式化**（详见官方 v2-to-v3 迁移指南）：

| 变化 | v2 | v3 |
|---|---|---|
| 模块路径 | `/v2` | `/v3`（Go 1.25+） |
| 日志 | 自研 `log.Logger/Helper/Valuer` | 标准库 **`log/slog`**：`log.NewLogger(slog.NewJSONHandler(...))`；OTel handler 在 contrib |
| JSON 编码 | `encoding/json` 混处理 Go 值与 proto | **拆分**：`encoding/json`（标准 JSON）+ `encoding/protojson`（proto JSON）；兼容行为走 contrib |
| JWT 中间件 | 核心仓 `middleware/auth/jwt` | 移到 `contrib/middleware/jwt/v3`（不用 JWT 就不拉 golang-jwt 依赖） |
| 熔断器 | 默认依赖 aegis | 内置默认实现；自定义才引 aegis（`WithBreakerFactory`） |
| HTTP binding | `transport/http/binding` 包 | 移除：`http.BuildPath` + `transport/http.Context` 的 `Bind/BindVars/BindQuery/BindForm` |

v3 新特性：errors 标准库 wrappers、`google.api.HttpBody` 响应、config 泛型 `Get`、自定义 validator、JWT `WithParserOptions`、`TooManyRequests` 错误类型、HTTP ServerStream 读写超时、`kratos new --repo` 等。

**迁移步骤**：改 import `/v2`→`/v3`（contrib 同理）→ 选定 JSON codec（新代码显式 import `encoding/json` + `encoding/protojson`）→ 日志换 slog API → JWT 改 contrib 路径 → 检查自定义熔断 → 替换 binding 直用 → `go generate ./...` 重新生成 → `go mod tidy && go test ./...`。

v2（2.9.x）仍是存量主流：2.8.4 起 validate 中间件已从 PGV 迁移到 **protovalidate**（PGV 进入维护期），v3 全面延续 protovalidate 路线。

---

## 6. 常见坑与最佳实践

1. **不要绕过分层**：service 里直接写 SQL 是最常见堕落——biz 是业务复用单元（用例）、data 才知道存储；保持 `service → biz ← data` 依赖方向，用 wire 组织装配；
2. **proto 即契约，早评审**：接口变更的成本在 proto 评审时就该付掉；generate 之后不要手改 `*.pb.go`；
3. **错误必须带 reason**：只返回 HTTP 500 数字码会让前端无法区分场景；`errors.Errorf(code, reason, msg)` 的 reason 是团队共享枚举，收口在 proto 生成；
4. **JWT/白名单用 selector**：全局 auth 中间件 + `selector.Match` 白名单（健康检查、登录接口）是标准姿势；
5. **注册中心的 endpoint 广告地址**：容器里 0.0.0.0 不可达，需要 `kratos.Endpoint` 或 `ADVERTISE_URI` 暴露可路由地址（K8s 场景注意 Pod IP）；
6. **graceful stop 超时**：`kratos.StopTimeout` 默认 30s，K8s `terminationGracePeriodSeconds` 要比它大，否则 SIGKILL 掐断在途请求；
7. **配置优先级**：多 Source 后者覆盖前者，推荐 file（基线）→ env（部署差异）→ 远程配置中心（动态）；
8. **v3 升级先跑通编码**：JSON codec 拆分是最容易踩的破坏点——依赖 proto JSON 语义（int64 字符串化、enum 名）的服务必须显式引 protojson 或 contrib 兼容包，且**不要同时注册两个 json codec**；
9. **生成工具链版本对齐**：protoc/protoc-gen-go/kratos CLI 版本不一致会产生诡异编译错，团队统一 `kratos upgrade` + Makefile 固定版本；
10. **hot reload 用于开发**：`kratos run` 自带热重载（air）；生产环境是普通二进制，无热更新概念。

---

## 7. 选型对比

| 维度 | Kratos | go-zero | Gin/Echo | go-micro |
|---|---|---|---|---|
| 定位 | 微服务全家桶（轻内核+contrib） | 微服务全家桶（单体工具链） | Web 框架 | 微服务 RPC 框架 |
| 接口契约 | **proto-first，双协议生成** | api 文件（自定义 DSL） | 手写路由 | proto-first |
| HTTP+gRPC 同源 | ✅ 核心卖点 | ✅ | ❌（仅 HTTP） | gRPC 为主 |
| 学习曲线 | 中（需 proto/wire/layout 心智） | 低-中 | 低 | 中-高（抽象重） |
| 工程规范 | **强**（layout+CLI 强约束） | 强 | 无 | 弱 |
| 生态背景 | B 站生产验证 + CNCF 周边丰富 | 国内社区活跃 | 最广泛 | 海外社区 |

一句话：**团队要 proto 契约驱动 + HTTP/gRPC 双协议 + 强工程规范，选 Kratos；要更白盒的工具化全家桶选 go-zero；只是写 Web API 用 Gin/Echo 就够**。

参考：[官方文档 go-kratos.dev](https://go-kratos.dev/)、[kratos-layout 模板](https://github.com/go-kratos/kratos-layout)、[v2→v3 迁移指南](https://github.com/go-kratos/kratos/blob/main/docs/migration/v2-to-v3.md)、[examples 仓库](https://github.com/go-kratos/examples)。
