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

```text
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

```text
Unary:  client interceptor → 网络 → server interceptor 1 → interceptor 2 → Handler
```

拦截器是`func(ctx, req, info, handler) (resp, err)` 的链式包装，用来放鉴权、日志、指标、限流、panic 恢复、重试。**顺序即嵌套顺序**：第一个拦截器在最外层，它的耗时统计会包含后续所有拦截器。

### 3.5 名称解析与负载均衡

```text
target "dns:///svc:50051"
   → Resolver 解析出 []Address
   → Balancer 建立 SubConn 并维护状态（CONNECTING / READY / TRANSIENT_FAILURE）
   → Picker 在每次 RPC 时选一条 READY 连接
```

默认 `pick_first`（只用第一条可用地址，故障才切换），生产通常显式配 `round_robin` 或接入 xDS / 自定义 etcd resolver。注意：**gRPC 的负载均衡是客户端侧、每连接复用**，与 L4/L7 代理的行为不同。

### 3.6 deadline 与取消：跨进程怎么传、为什么总超时

客户端的 `context.WithTimeout` 不只是本地定时器。发起 RPC 时 gRPC 读取 ctx 里的 deadline，把它塞进 HEADERS 的 `grpc-timeout` 头：

```text
grpc-timeout: 800m
              ^^^^ 最多 8 位十进制数字
                  ^ 单位：H 时 / M 分 / S 秒 / m 毫秒 / u 微秒 / n 纳秒
```

编码取「能放进 8 位数字的最粗单位」，800ms 走 `800m`、2s 走 `2S`、50µs 走 `50u` 而不是 `50000n`。这带来两个副作用：一是**精度被单位截断**，1500ms 会被写成 `1S`（小数部分直接丢），服务端拿到的是 1s 而不是 1.5s；二是抓包看到的数字通常比客户端设的略小，因为中间还有 RTT、连接排队与实现层的少量扣减。所以「客户端设 800ms、服务端 `ctx.Deadline()` 只剩 780ms」是正常现象，不是 bug。

```text
Client（预算 800ms）                      Server                    下游
  │ ctx.WithTimeout(800ms)                 │                          │
  │ ── HEADERS grpc-timeout: 800m ───────► │                          │
  │                                        │ ctx.Deadline() = T-Δ     │
  │                                        │ ── 复用同一 ctx ───────► │ 剩余 T-Δ-Δ'
  │                                        │                          │
  │ ◄── 本地 deadline 先触发（DeadlineExceeded）                        │
  │ ── RST_STREAM(CANCEL) ───────────────► │ ctx.Done() → Canceled    │
```

```go
package biz

import (
    "context"
    "log"
    "time"

    "google.golang.org/grpc/status"

    pb "example.com/app/gen/greeter/v1"
)

type GreetingStore interface {
    Greeting(ctx context.Context, name string) (string, error)
}

type Server struct{ store GreetingStore }

// 服务端：沿用上游给的剩余预算，而不是自己再拍一个 3s
func (s *Server) SayHello(ctx context.Context, req *pb.HelloRequest) (*pb.HelloReply, error) {
    if dl, ok := ctx.Deadline(); ok {
        log.Printf("remaining budget: %s", time.Until(dl))
    }

    // 检查点：贵操作之前先看是否已取消，避免白算
    if err := ctx.Err(); err != nil {
        return nil, status.FromContextError(err).Err()
    }

    // 关键：下游复用同一个 ctx，deadline 与取消才会逐跳传递
    msg, err := s.store.Greeting(ctx, req.GetName())
    if err != nil {
        return nil, err
    }
    return &pb.HelloReply{Message: msg}, nil
}
```

- **逐跳递减必须做**：服务端若用 `context.Background()` 另起一个「5s」调下游，就丢掉了上游只剩 200ms 的事实。上游早已超时返回，下游还在跑满 5s——这就是**幽灵请求**（goroutine、连接、数据库连接池全被无效占用）。正确做法是全程复用入参 ctx，需要留余量就在其基础上 `context.WithTimeout(ctx, min(剩余, 预期))` 收窄。
- **两个超时谁生效**：gRPC 只看发起 RPC 那一刻 ctx 链上的 deadline。ctx 只能收窄不能放宽，所以「更短的那个」赢；在拦截器里再 `WithTimeout` 只会在原预算内进一步收紧。想放大预算只能改调用方，改服务端无效。
- **取消如何传播**：客户端 `cancel()` 或超时 → gRPC 发 `RST_STREAM(CANCEL)` → 服务端 ctx 立刻 `Done()`，`ctx.Err()` 为 `context.Canceled`（超时则是 `DeadlineExceeded`）。取消是**尽力而为**的异步信号，靠 handler 主动检查才生效。
- **检查点放在哪**：长循环每轮、每次外部 IO 前、每批处理之间。已经在阻塞 IO 里的 goroutine 收不到信号，所以给下游库也要传 ctx（`db.QueryContext`、`http.NewRequestWithContext`），别用不带 ctx 的版本。
- **重试与 deadline 的关系**：所有重试共享同一个总预算，重试不会延长 deadline。`maxAttempts=3` + 总预算 300ms，意味着三次尝试共用 300ms，单次实际只有约 100ms。要么把预算按尝试次数预留，要么只让最后一次尝试吃满。

### 3.7 错误模型：status code、details 与 HTTP 映射

Go 的 `error` 在 gRPC 里被压成 `Status{code, message, details}`，线上落在 trailer 的 `grpc-status` 与 `grpc-message`（后者是 percent-encoded 的 UTF-8）。**code 是给程序判断的，message 是给人看的**，任何基于 message 字符串的匹配都会在下次改文案时崩掉。

| code | HTTP | 含义与典型用法 |
|---|---|---|
| `Canceled` | 499 | 调用方主动取消（客户端超时也常表现成它） |
| `Unknown` | 500 | 服务端直接 `return err`，语义已丢失 |
| `InvalidArgument` | 400 | 参数本身不合法，重试无意义 |
| `DeadlineExceeded` | 504 | 超出 deadline |
| `NotFound` | 404 | 资源不存在（含「无权查看」时的掩蔽） |
| `AlreadyExists` | 409 | 唯一键冲突，创建型接口 |
| `PermissionDenied` | 403 | 已认证但无权限 |
| `ResourceExhausted` | 429 | 配额/限流/消息过大 |
| `FailedPrecondition` | 400 | 参数格式没错，但当前状态不允许（账号未激活、库存不足） |
| `Aborted` | 409 | 并发冲突（乐观锁版本不匹配），可立即重试 |
| `OutOfRange` | 400 | 参数越界（分页 offset 超总长） |
| `Unimplemented` | 501 | 方法未实现（旧版本服务端收到新方法） |
| `Internal` | 500 | 服务端 bug 或不可恢复状态，**不该重试** |
| `Unavailable` | 503 | 暂时不可达/过载，**可重试** |
| `DataLoss` | 500 | 数据损坏，通常要人介入 |
| `Unauthenticated` | 401 | 没带/带错凭证 |

最容易搞混的两组：

- `InvalidArgument` vs `FailedPrecondition`：前者「这个参数永远不对」（负数年龄），后者「这次不行，改状态后可能行」（余额不足）。前者改代码，后者改数据。
- `Unavailable` vs `Internal`：前者是「现在联系不上/忙」，重试有意义；后者是「我算错了」，重试只会重复错一次。把 `Internal` 标成可重试是重试风暴的常见源头。

结构化错误用 `WithDetails`，让客户端能拿到字段级信息而不是解析文案：

```go
package biz

import (
    "google.golang.org/genproto/googleapis/rpc/errdetails"
    "google.golang.org/grpc/codes"
    "google.golang.org/grpc/status"
)

// 服务端：带回字段级信息，而不是让客户端解析文案
func invalidRequest() error {
    st, err := status.New(codes.InvalidArgument, "invalid request").WithDetails(
        &errdetails.BadRequest{FieldViolations: []*errdetails.BadRequest_FieldViolation{
            {Field: "name", Description: "must be 1~64 chars"},
            {Field: "email", Description: "not a valid address"},
        }})
    if err != nil { // 打包失败（该 proto 类型未注册）
        return status.Error(codes.InvalidArgument, "invalid request")
    }
    return st.Err()
}

// 客户端：先按 code 分支，再解 details 做字段级提示
func fieldErrs(err error) map[string]string {
    out := map[string]string{}
    if st, ok := status.FromError(err); ok && st.Code() == codes.InvalidArgument {
        for _, d := range st.Details() {
            if br, ok := d.(*errdetails.BadRequest); ok {
                for _, v := range br.GetFieldViolations() {
                    out[v.GetField()] = v.GetDescription()
                }
            }
        }
    }
    return out
}
```

- **判等不要用 `==`**：`status.Error(codes.NotFound, "x") != status.Error(codes.NotFound, "x")`，两次调用是两个指针。统一用 `status.Code(err)` 或 `status.FromError` 后的 `st.Code()`；`errors.Is(err, context.DeadlineExceeded)` 对 gRPC 错误只在部分路径成立，别依赖。
- **details 是 proto**：`WithDetails` 只接受 `proto.Message`，且解码要靠全局 proto registry 还原类型；裁剪过 registry 的二进制解不出具体类型，此时 `Details()` 里对应的那一项是 `error`（如 `protoregistry.NotFound`），要用 `if _, ok := d.(error); ok` 兜底。
- **HTTP 映射可改，且可重试判定要集中**：上表是 grpc-gateway 的默认表，要统一错误体就用 `runtime.WithErrorHandler` 自己转。客户端把「哪些 code 可重试」（通常 `Unavailable` / `Aborted` / `ResourceExhausted` + 幂等方法）收进一个函数复用，别散落在各处。

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

### 4.6 重试与幂等：重试策略的坑

gRPC 的重试有两种完全不同的实现：

- **内置重试**：写在 service config 里，由传输层执行。重试发生在「收到 trailer 之后、错误返回应用之前」，同一个 RPC 在应用看来只调用了一次，拦截器也只走一次（日志里不会出现三条记录）；同时受内建的 token bucket 节流保护。
- **拦截器重试**：业务代码里 for 循环重调。灵活（能改请求、能换节点），但绕开了节流，也容易在日志/指标里放大调用量。

能用内置就用内置：

```go
package client

import (
    "google.golang.org/grpc"
    "google.golang.org/grpc/credentials/insecure"
)

// timeout 是这条 RPC 的总预算，重试共享它，不是每次尝试各一份
const retryConfig = `{
  "methodConfig": [{
    "name": [{"service": "greeter.v1.GreeterService", "method": "SayHello"}],
    "timeout": "1s",
    "retryPolicy": {
      "maxAttempts": 3,
      "initialBackoff": "0.1s",
      "maxBackoff": "1s",
      "backoffMultiplier": 2.0,
      "retryableStatusCodes": ["UNAVAILABLE"]
    }
  }]
}`

func NewConn(addr string) (*grpc.ClientConn, error) {
    return grpc.NewClient(addr,
        grpc.WithTransportCredentials(insecure.NewCredentials()),
        grpc.WithDefaultServiceConfig(retryConfig),
    )
}
```

- **`maxAttempts` 默认上限是 5**（含首次），写 10 不会生效，超过会被截断到 5。真需要更多次重试，说明该换的是熔断/降级，而不是重试。
- **只对幂等方法重试**。`SayHello` 重试安全，`CreateOrder` 重试可能下两单。服务端保证不了幂等时，客户端就不要配 `retryableStatusCodes`——配置按 `methodConfig.name` 生效：示例写的是 `method: "SayHello"`，只影响这个方法；只有 name 省略 method 时才覆盖整个 service。
- **重试风暴**：故障时若所有客户端同时重试，QPS 会翻倍砸向正在恢复的服务，把它彻底压死。内置重试可以配 `retryThrottling` 做令牌桶节流，但**需要显式配置**；示例没配，等于没有刹车。自研重试必须自带抖动（jitter）+ 上限。
- **`WaitForReady` 与快速失败**：默认 `grpc.WaitForReady(false)`，连接未就绪立刻返回 `Unavailable`，由调用方决定重试；`WaitForReady(true)` 会把 RPC 挂到连接就绪或 deadline 到期为止。前者适合在线请求（快速失败 + 上层降级），后者适合后台任务。
- **幂等键放请求体还是 metadata**：放请求体（如 `request_id`）才进得了业务语义与唯一索引，能真正做「已存在则返回原结果」；放 metadata 只适合做链路追踪/去重窗口，服务端一旦没读到就完全失效。两者都做也行，但**幂等的真相在存储层**，不在传输层。
- **超时预算怎么分**：总预算 1s、最多 3 次，别让第一次就吃掉 900ms。给单次尝试设上限（`timeout` 或每轮 `WithTimeout`），并用 `initialBackoff` 留出退避空间，否则重试还没发出预算就没了。也**别把 `DEADLINE_EXCEEDED` 列进 `retryableStatusCodes`**——预算已耗尽，重试只会立刻再超时一次。

### 4.7 grpc-gateway：一份 proto 两种协议

对外要给 REST，对内要保留 gRPC，不必写两套 handler。在 proto 上加 HTTP 注解，`protoc-gen-grpc-gateway` 会生成一个反向代理：对外收 JSON、对内转成 gRPC 调你的服务。

```protobuf
import "google/api/annotations.proto";

service GreeterService {
  rpc SayHello (HelloRequest) returns (HelloReply) {
    option (google.api.http) = { post: "/v1/greeter:sayHello" body: "*" };
  }
}
```

```yaml
# buf.gen.yaml：一次生成消息、stub 与网关（buf generate）
version: v2
plugins:
  - local: protoc-gen-go
    out: gen
    opt: paths=source_relative
  - local: protoc-gen-go-grpc
    out: gen
    opt: paths=source_relative
  - local: protoc-gen-grpc-gateway
    out: gen
    opt: paths=source_relative
```

```go
package main

import (
    "context"
    "log"
    "net/http"

    "github.com/grpc-ecosystem/grpc-gateway/v2/runtime"
    "google.golang.org/grpc"
    "google.golang.org/grpc/credentials/insecure"
    "google.golang.org/protobuf/encoding/protojson"

    pb "example.com/app/gen/greeter/v1"
)

func main() {
    conn, err := grpc.NewClient("localhost:50051",
        grpc.WithTransportCredentials(insecure.NewCredentials()))
    if err != nil {
        log.Fatal(err)
    }
    defer conn.Close()

    mux := runtime.NewServeMux(
        runtime.WithMarshalerOption(runtime.MIMEWildcard, &runtime.JSONPb{
            MarshalOptions: protojson.MarshalOptions{
                UseProtoNames:   true,  // JSON 用 snake_case，与 proto 字段名一致
                EmitUnpopulated: false, // 零值字段不输出，响应更小
            },
            UnmarshalOptions: protojson.UnmarshalOptions{DiscardUnknown: true},
        }),
    )
    if err := pb.RegisterGreeterServiceHandler(context.Background(), mux, conn); err != nil {
        log.Fatal(err)
    }
    log.Fatal(http.ListenAndServe(":8080", mux)) // 对外 :8080 → 对内 :50051
}
```

- **`UseProtoNames` 决定兼容性**：默认 false 会把 `user_id` 输出成 `userId`。对外开放后必须固定一种，混用会让前端字段时有时无。
- **`EmitUnpopulated`**：默认不输出零值字段，前端拿到的是「字段不存在」而不是 `0`/`""`，移动端常因此空指针。打开它换来可预测的结构，代价是响应变大。
- **错误体是数字 code**：`Status` 会被转成 `{"code":14,"message":"...","details":[...]}`，前端按 `"UNAVAILABLE"` 判断会永远失败；要换格式用 `runtime.WithErrorHandler` 改写。
- **取舍**：网关多一层序列化与一跳网络，延迟增加 0.5~2ms，流式在 HTTP/1.1 下只能 chunked。纯内部调用别绕网关，只有对外暴露的接口才值得；OpenAPI 也从 proto 注解生成（`protoc-gen-openapiv2`），比手写 YAML 可靠。

### 4.8 测试：bufconn 与 mock

单测里 `net.Listen(":0")` 起真端口既慢又不稳（端口冲突、防火墙、CI 沙箱）。`bufconn` 提供一条纯内存的双向管道，`grpc.Server` 与 `ClientConn` 直接对接，**无端口、无网络栈、无 TLS**。

```go
package greeter_test

import (
    "context"
    "net"
    "testing"
    "time"

    "google.golang.org/grpc"
    "google.golang.org/grpc/codes"
    "google.golang.org/grpc/credentials/insecure"
    "google.golang.org/grpc/status"
    "google.golang.org/grpc/test/bufconn"

    pb "example.com/app/gen/greeter/v1"
)

type greeter struct{ pb.UnimplementedGreeterServiceServer }

func (g *greeter) SayHello(ctx context.Context, req *pb.HelloRequest) (*pb.HelloReply, error) {
    if req.GetName() == "" {
        return nil, status.Error(codes.InvalidArgument, "name is required")
    }
    return &pb.HelloReply{Message: "hello, " + req.GetName()}, nil
}

// 起一个进程内 server，返回已连好的 client
func newTestClient(t *testing.T) pb.GreeterServiceClient {
    t.Helper()

    lis := bufconn.Listen(1 << 20) // 1 MiB 内存缓冲
    s := grpc.NewServer()
    pb.RegisterGreeterServiceServer(s, &greeter{})
    go func() { _ = s.Serve(lis) }() // Stop/GracefulStop 后 Serve 返回 nil；只有非 nil 才需要处理
    t.Cleanup(s.Stop)

    conn, err := grpc.NewClient("passthrough:///bufnet",
        grpc.WithContextDialer(func(ctx context.Context, _ string) (net.Conn, error) {
            return lis.DialContext(ctx)
        }),
        grpc.WithTransportCredentials(insecure.NewCredentials()),
    )
    if err != nil {
        t.Fatalf("new client: %v", err)
    }
    t.Cleanup(func() { _ = conn.Close() })

    return pb.NewGreeterServiceClient(conn)
}

func TestSayHello(t *testing.T) {
    cli := newTestClient(t)
    cases := map[string]codes.Code{"alice": codes.OK, "": codes.InvalidArgument}
    for name, want := range cases {
        t.Run(name, func(t *testing.T) {
            t.Parallel()
            ctx, cancel := context.WithTimeout(context.Background(), time.Second)
            defer cancel()

            _, err := cli.SayHello(ctx, &pb.HelloRequest{Name: name})
            if got := status.Code(err); got != want {
                t.Fatalf("code = %v, want %v (err: %v)", got, want, err)
            }
        })
    }
}
```

- **target 写 `passthrough:///bufnet`**：`grpc.NewClient` 必须解析 scheme，`passthrough` 表示「原样交给 dialer」，名字随意但要非空。少了 scheme 会直接报 `missing port in address`。
- **每个子测试自带 `context.WithTimeout`**：bufconn 也会卡死（handler 死循环、忘记 `Send`），没有 deadline 的测试会在 CI 上挂到全局超时。
- **并发与数据竞争**：同一个 `ClientConn` 可被多个 goroutine 共用，`t.Parallel()` 直接并发调是合法用法；跑 `go test -race ./...` 才能抓到 handler 里共享 map / 计数器的问题，这是 gRPC 服务最常见的线上事故来源。
- **mock 业务依赖，不要 mock gRPC 层**：给 handler 注入 `UserStore` 之类的接口，测试里传内存实现即可。用 gomock 生成 `GreeterServiceClient` 的假实现去「测 handler」是错位的——它测不到序列化、拦截器、状态码转换，恰恰是 gRPC 最容易出错的部分。
- **集成测试复用同一个 server**：`TestMain` 里起一个 bufconn server 给所有用例共享，用完统一 `Stop`；只有需要验证「服务端重启」「连接断开」的用例才单独起。`grpc.NewClient` 是惰性的，重复创建 ClientConn 开销很小，但 server 不必重建。

---

## 5. 版本演进速览

| 阶段 | 关键变化 |
|---|---|
| 早期 v1.x | Unary/Streaming、拦截器、TLS、resolver/balancer 骨架成型 |
| v1.3x 起 | `grpc.DialContext` 成为主流；`balancer` API 重构、支持 `round_robin` 与优先级 |
| v1.4x~1.5x | 与 `google.golang.org/protobuf`（APIv2）对齐，`protoc-gen-go-grpc` **拆成独立插件**（旧 `--go_out=plugins=grpc` 写法已废弃） |
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
