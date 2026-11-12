# gRPC-Go 技术介绍与使用指南

> 基于 HTTP/2 + Protobuf 的高性能 RPC 框架：`.proto` 定义契约、代码生成 stub、四种流模式、拦截器与 deadline 内建，是 Go 微服务内部通信的默认选项。
> 仓库：<https://github.com/grpc/grpc-go>　|　主线版本：**v1.x**（持续迭代，API 稳定；整理于 2026-11）　|　协议：Apache-2.0
> 生态：Protobuf `google.golang.org/protobuf`、`protoc-gen-go-grpc`、[grpcurl](https://github.com/fullstorydev/grpcurl)

---

## 1. 项目简介

gRPC 不是「另一种 HTTP 客户端」，而是**契约驱动的 RPC 体系**：先用 `.proto` 描述服务方法与消息结构，再由代码生成器产出强类型的客户端与服务端骨架，运行时用 Protobuf 二进制编码在 HTTP/2 上传输。Go 实现即 `grpc-go`。

核心卖点：

- **契约即文档**：`.proto` 是唯一的接口真相，服务端/客户端/多语言 SDK 从同一份文件生成，杜绝「字段对不上」；
- **性能**：Protobuf 体积远小于 JSON，HTTP/2 单连接多路复用，头部压缩（HPACK），无队头阻塞；
- **四种流模式**：一元（Unary）、服务端流、客户端流、双向流，长连接推送不用再自己造 WebSocket；
- **工程内建**：deadline/取消传播、结构化状态码、拦截器链、TLS/mTLS、名称解析与负载均衡、健康检查、反射、压缩。
- **跨语言**：同一份 proto 可生成 Go / Java / Python / Rust / Node 等客户端。

适用场景：微服务内部同步调用、需要流式传输（日志/实时推送/大文件分片）、多语言混部、对延迟与带宽敏感的内网通信。**不适合**：浏览器直连（需 grpc-web 或网关）、对外开放且需要人工可读/可调试的 API（REST 更合适）、纯事件驱动的异步解耦（用 [Kafka](../kafka/kafka-guide.md) / [NATS](../nats/nats-guide.md)）。

### 安装与代码生成

```bash
# 1. Go 依赖
go get google.golang.org/grpc
go get google.golang.org/protobuf

# 2. 代码生成插件
go install google.golang.org/protobuf/cmd/protoc-gen-go@latest
go install google.golang.org/grpc/cmd/protoc-gen-go-grpc@latest
# 确保 $(go env GOPATH)/bin 在 PATH 中

# 3. 生成（proto 放在 proto/ 下）
protoc --go_out=. --go_opt=paths=source_relative \
       --go-grpc_out=. --go-grpc_opt=paths=source_relative \
       proto/greeter.proto
```

### 最小契约

```protobuf
syntax = "proto3";
package greeter.v1;
option go_package = "example.com/app/gen/greeter/v1;greeterv1";

service GreeterService {
  rpc SayHello (HelloRequest) returns (HelloReply);
  rpc Watch (WatchRequest) returns (stream HelloReply); // 服务端流
}

message HelloRequest { string name = 1; }
message HelloReply    { string message = 1; }
message WatchRequest  { string name = 1; }
```

---

## 2. 功能特性总览

| 能力 | 说明 |
|---|---|
| 四种流模式 | Unary / Server streaming / Client streaming / Bidirectional streaming |
| 代码生成 | `protoc-gen-go`（消息）+ `protoc-gen-go-grpc`（Client/Server 接口） |
| 错误模型 | `status.Status` + 标准 `codes`（`NotFound` / `InvalidArgument` / `Unavailable`…），可带 `details` |
| 截止时间 | `context.WithTimeout` 自动转成 `grpc-timeout` 头，跨进程传播并逐跳扣减 |
| 元数据 | `metadata.MD`，类似 HTTP header；`-bin` 后缀自动 base64 编码二进制值 |
| 拦截器 | `UnaryClientInterceptor` / `StreamClientInterceptor` / 服务端对应版本，可组合成链 |
| 传输安全 | TLS / mTLS、`grpc.WithTransportCredentials`、ALPN 协商 h2 |
| 服务治理 | Resolver（DNS / etcd / xDS）→ Balancer（`round_robin` / `pick_first`）→ Picker |
| 可运维 | health check 协议、server reflection（grpcurl 可免 proto 调试）、channelz 连接诊断 |
| 流控与压缩 | HTTP/2 WINDOW_UPDATE 背压；gzip 等 compressor 可插拔 |
| 消息边界 | 默认单条消息 4 MiB 上限，可配置（但不建议放大到几十 MB） |

---

## 3. 核心概念与原理

### 3.1 一次 Unary 调用发生了什么

```
Client                                             Server
  │ ① hello.SayHello(ctx, req)                       │
  │ ② proto.Marshal(req) → []byte                   │
  │ ③ HTTP/2 HEADERS(:method POST, :path /pkg.Svc/M,│
  │     content-type application/grpc, grpc-timeout) │
  │ ④ DATA(压缩标志 1B + 长度 4B + payload) ────────►│ ⑤ 解析 HEADERS，查方法注册表
  │                                                  │ ⑥ proto.Unmarshal → req
  │                                                  │ ⑦ 拦截器链 → Handler
  │◄───────────────────────── DATA(reply) ──────────│ ⑧ proto.Marshal(reply)
  │◄───────── HEADERS(grpc-status: 0, grpc-message) │
  │ ⑨ Unmarshal → resp；非 0 状态转 status.Error     │
```

关键点：**gRPC 的错误不在 HTTP 状态码里**（几乎总是 200），而在响应末尾的 trailer `grpc-status` 中。这解释了为什么用普通 curl / 浏览器抓 gRPC 请求会「看起来成功但没数据」。

### 3.2 Protobuf 为什么小

字段按 `tag = field_number << 3 | wire_type` 编码，整数用 varint（小数字仅 1 字节），字段号 `1~15` 占 1 字节应留给高频字段；字符串是 length-delimited。**字段名不进线上数据**，靠 `.proto` 双方约定——所以「改字段名」兼容，「改字段号 / 改类型」是破坏性变更。

### 3.3 HTTP/2 多路复用与流控

一条 TCP 连接上可并行多个 stream，每个 RPC 对应一个 stream。接收方通过 `WINDOW_UPDATE` 通告窗口，发送方窗口耗尽就阻塞——这就是**流式调用的天然背压**。因此 `ClientConn` 必须长期复用（它管理连接、重连、子连接），每次 RPC 都 `Dial` 会退化成「每请求一次 TCP + TLS 握手」。

### 3.4 拦截器链

```
Unary:  client interceptor → 网络 → server interceptor 1 → interceptor 2 → Handler
```

拦截器是`func(ctx, req, info, handler) (resp, err)` 的链式包装，用来放鉴权、日志、指标、限流、panic 恢复、重试。**顺序即嵌套顺序**：第一个拦截器在最外层，它的耗时统计会包含后续所有拦截器。

### 3.5 名称解析与负载均衡

```
target "dns:///svc:50051"
   → Resolver 解析出 []Address
   → Balancer 建立 SubConn 并维护状态（CONNECTING / READY / TRANSIENT_FAILURE）
   → Picker 在每次 RPC 时选一条 READY 连接
```

默认 `pick_first`（只用第一条可用地址，故障才切换），生产通常显式配 `round_robin` 或接入 xDS / 自定义 etcd resolver。注意：**gRPC 的负载均衡是客户端侧、每连接复用**，与 L4/L7 代理的行为不同。

---

## 4. 使用实战

### 4.1 服务端实现

```go
package main

import (
    "context"
    "log"
    "net"

    "google.golang.org/grpc"
    "google.golang.org/grpc/codes"
    "google.golang.org/grpc/reflection"
    "google.golang.org/grpc/status"
    pb "example.com/app/gen/greeter/v1"
)

type server struct{ pb.UnimplementedGreeterServiceServer }

func (s *server) SayHello(ctx context.Context, req *pb.HelloRequest) (*pb.HelloReply, error) {
    if req.GetName() == "" {
        return nil, status.Error(codes.InvalidArgument, "name is required")
    }
    return &pb.HelloReply{Message: "hello, " + req.GetName()}, nil
}

func main() {
    lis, err := net.Listen("tcp", ":50051")
    if err != nil { log.Fatal(err) }

    s := grpc.NewServer(
        grpc.ChainUnaryInterceptor(RecoveryUnary(), LoggingUnary()),
        grpc.MaxRecvMsgSize(4<<20), // 4 MiB，显式声明边界
    )
    pb.RegisterGreeterServiceServer(s, &server{})
    reflection.Register(s) // 允许 grpcurl 免 proto 探测
    log.Println("listening :50051")
    if err := s.Serve(lis); err != nil { log.Fatal(err) }
}
```

> 嵌入 `UnimplementedGreeterServiceServer` 是**兼容性护栏**：proto 新增方法后旧实现仍能编译，未实现的方法返回 `Unimplemented`，而不是编译失败。

### 4.2 客户端调用（复用连接 + 超时）

```go
var conn *grpc.ClientConn // 全局单例，进程退出时 Close

func InitClient(addr string) error {
    c, err := grpc.NewClient(addr,
        grpc.WithTransportCredentials(insecure.NewCredentials()),
        grpc.WithDefaultServiceConfig(`{"loadBalancingConfig":[{"round_robin":{}}]}`),
        grpc.WithChainUnaryInterceptor(TimeoutUnary(), TraceUnary()),
    )
    if err != nil { return err }
    conn = c
    return nil
}

func Call(ctx context.Context, name string) (string, error) {
    ctx, cancel := context.WithTimeout(ctx, 2*time.Second) // 必须设 deadline
    defer cancel()

    cli := pb.NewGreeterServiceClient(conn)
    resp, err := cli.SayHello(ctx, &pb.HelloRequest{Name: name})
    if err != nil {
        // 用 status.Code 判断，不要用 errors.Is / 字符串比较
        if status.Code(err) == codes.DeadlineExceeded {
            return "", fmt.Errorf("upstream timeout: %w", err)
        }
        return "", err
    }
    return resp.GetMessage(), nil
}
```

### 4.3 拦截器：panic 恢复 + 耗时日志

```go
func RecoveryUnary() grpc.UnaryServerInterceptor {
    return func(ctx context.Context, req any, info *grpc.UnaryServerInfo,
                handler grpc.UnaryHandler) (resp any, err error) {
        defer func() {
            if r := recover(); r != nil {
                log.Printf("panic in %s: %v\n%s", info.FullMethod, r, debug.Stack())
                err = status.Error(codes.Internal, "internal error")
            }
        }()
        return handler(ctx, req)
    }
}

func LoggingUnary() grpc.UnaryServerInterceptor {
    return func(ctx context.Context, req any, info *grpc.UnaryServerInfo,
                handler grpc.UnaryHandler) (any, error) {
        start := time.Now()
        resp, err := handler(ctx, req)
        log.Printf("%s code=%s cost=%s", info.FullMethod,
            status.Code(err), time.Since(start))
        return resp, err
    }
}
```

> gRPC 的 handler panic **不会**被 net/http 风格的 recover 兜住，未捕获的 panic 会直接终止进程；生产环境必须有 Recovery 拦截器。

### 4.4 服务端流

```go
func (s *server) Watch(req *pb.WatchRequest, stream pb.GreeterService_WatchServer) error {
    ticker := time.NewTicker(time.Second)
    defer ticker.Stop()
    for i := 0; i < 5; i++ {
        select {
        case <-stream.Context().Done(): // 客户端取消，及时退出
            return stream.Context().Err()
        case <-ticker.C:
            if err := stream.Send(&pb.HelloReply{Message: "tick"}); err != nil {
                return err
            }
        }
    }
    return nil // 正常结束；客户端 Recv 收到 io.EOF
}
```

```go
// 客户端接收流
stream, err := cli.Watch(ctx, &pb.WatchRequest{Name: "a"})
for {
    reply, err := stream.Recv()
    if errors.Is(err, io.EOF) { break } // 流正常结束
    if err != nil { return err }
    handle(reply)
}
```

### 4.5 Metadata 与调试

```go
// 客户端附加元数据
md := metadata.Pairs("authorization", "Bearer "+token, "request-id", rid)
ctx = metadata.NewOutgoingContext(ctx, md)

// 服务端读取
md, _ := metadata.FromIncomingContext(ctx)
token := md.Get("authorization")
```

```bash
# 免 proto 调试（依赖服务端开启 reflection）
grpcurl -plaintext localhost:50051 list
grpcurl -plaintext -d '{"name":"world"}' localhost:50051 greeter.v1.GreeterService/SayHello
```

---

## 5. 版本演进速览

| 阶段 | 关键变化 |
|---|---|
| 早期 v1.x | Unary/Streaming、拦截器、TLS、resolver/balancer 骨架成型 |
| v1.3x 起 | `grpc.DialContext` 成为主流；`balancer` API 重构、支持 `round_robin` 与优先级 |
| v1.4x~1.5x | 与 `google.golang.org/protobuf`（APIv2）对齐，`protoc-gen-go-grpc` **拆成独立插件**（旧 `plugins=grpcl` 已废弃） |
| 近年 v1.6x+ | **`grpc.NewClient` 取代 `grpc.Dial`**（不再默认立即建连）、xDS 支持完善、健康检查/反射/重试策略稳定、Go 版本基线上移 |

**迁移要点**：

- 老教程里的 `protoc --go_out=plugins=grpc:.` 已失效，改为两条 `--go_out` + `--go-grpc_out`；
- `grpc.Dial` 逐步被 `grpc.NewClient` 取代，后者惰性连接，语义更清晰；
- 生成代码要求服务端嵌入 `UnimplementedXxxServer`，旧实现需补上；
- grpc-go 与 Protobuf 运行时版本要匹配，升级时一起升，避免 `proto: message X is not a message` 一类错误。

---

## 6. 常见坑与最佳实践

1. **不设 deadline**：客户端不设超时，服务端挂住就会永久占用 goroutine 与连接，级联拖垮调用方。每个 RPC 都 `context.WithTimeout`，并由入口统一传递剩余预算。
2. **每请求 `Dial`**：`ClientConn` 是长连接资源，全局复用；重复 Dial 会让 HTTP/2 多路复用彻底失效。
3. **用 `errors.Is` 判断 gRPC 错误**：一律用 `status.Code(err)` / `status.FromError(err)`；`codes.Unavailable` 通常可重试，`InvalidArgument` 不能。
4. **忘加 Recovery 拦截器**：handler panic 直接杀进程。
5. **消息超过 4 MiB**：默认 `MaxRecvMsgSize` 会报 `ResourceExhausted`。正确做法是分片/流式传输或改用对象存储传 URL，而不是无脑调大上限。
6. **Proto 兼容性**：字段号**永不复用**，删除字段要 `reserved 3, 5;` + `reserved "old_name";`；改类型、改字段号、把 `singular` 改成 `repeated` 都是破坏性变更。新增字段用新号，读取侧一律用 `GetXxx()` 拿零值安全。
7. **流式调用并发 Send**：一个 stream 上**不允许并发 `SendMsg`**（会数据竞争），也不允许同时一个 goroutine Send、另一个 Recv 之外的复杂组合；需要双向并发时各用一个 goroutine 并串行化 Send。
8. **忽略 `io.EOF`**：客户端 `Recv()` 返回 `io.EOF` 是流正常结束，不是错误；把它当错误上报会污染监控。
9. **Metadata key 非法**：key 只能含小写字母、数字、`-`、`_`、`.`，二进制值必须用 `-bin` 后缀。大写 key 会被静默丢弃。
10. **直接关停**：用 `GracefulStop()` 等待在途 RPC 结束（配超时后降级 `Stop()`），否则发布期间会出现大量 `Unavailable`。
11. **只看 HTTP 状态码**：gRPC 几乎总是 200，真正的状态在 trailer。抓包/网关排障要看 `grpc-status`。
12. **重试放大**：拦截器里无节流地重试会在故障时形成重试风暴。用 gRPC 官方 retry policy + token bucket 节流，且只对幂等方法重试。
13. **浏览器直连**：浏览器无法直接发 gRPC（需要 HTTP/2 trailer 控制），要用 grpc-web / Connect 或 [Gin](../gin/gin-guide.md) 等网关做协议转换。

---

## 7. 选型对比

| 维度 | gRPC | REST / HTTP + JSON | Thrift | GraphQL | 消息队列（Kafka / NATS） |
|---|---|---|---|---|---|
| 契约 | `.proto` 强契约 + 代码生成 | OpenAPI（可选，常缺失） | IDL 强契约 | Schema 强契约 | 无强契约（schema registry 可选） |
| 编码 | Protobuf 二进制 | JSON 文本 | 二进制/Compact | JSON | 任意 |
| 传输 | HTTP/2 多路复用 | HTTP/1.1 或 2 | 自定义 TCP | HTTP | Broker |
| 流式 | **四种流原生** | SSE / WebSocket 另建 | 有限 | Subscription | 天然流 |
| 时延 / 带宽 | **优** | 中 | 优 | 中（易 N+1） | 异步，不适用同步调用 |
| 浏览器友好度 | 差（需 grpc-web） | **优** | 差 | 优 | 不适用 |
| 调试可读性 | 差（需 grpcurl/reflection） | **优**（curl 即可） | 差 | 中 | 中 |
| 耦合 | 同步、调用方需知道被调方 | 同步 | 同步 | 同步 | **异步、解耦** |
| 典型场景 | 内网微服务、多语言、流式 | 对外开放 API、BFF | 存量跨语言 RPC | 前端聚合查询 | 事件驱动、削峰 |

一句话决策：

- **内网服务间同步调用、多语言、或需要流式** → **gRPC**，配 deadline + 拦截器 + reflection 三件套；
- **面向浏览器 / 第三方 / 需要人肉 curl 调试** → REST + JSON，或用网关做 gRPC ↔ HTTP 转换（[Kratos](../kratos/kratos-guide.md) / [go-zero](../go-zero/go-zero-guide.md) 已内建双协议）；
- **前端字段需求变化频繁、多资源聚合** → GraphQL，但要防 N+1 与复杂查询打爆后端；
- **不需要即时结果、要削峰/解耦/可回放** → 消息队列，别用同步 RPC 硬扛；
- **老系统已经是 Thrift** → 不必为「更现代」而迁移，网关层做协议转换即可。

> 一个常见误区：把「微服务化」等同于「全链路 gRPC」。真正决定成败的是超时预算、幂等、重试节流与可观测性——换了协议不会自动获得这些，反而因为二进制不可读提高了排障门槛。
