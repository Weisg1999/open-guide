# GraphQL + gqlgen 技术介绍与使用指南

> **一句话定位**：让客户端自己声明需要哪些字段的查询语言——一次请求取回多份数据，避免 REST 的过度获取与多次往返。
>
> **上游仓库**：[99designs/gqlgen](https://github.com/99designs/gqlgen) · **版本基线**：gqlgen v0.17.x · **协议**：MIT

---

## 1. 项目简介

GraphQL 不是「更快的 REST」，而是把**响应形状的决定权从服务端交给客户端**：服务端用一份 SDL 声明有哪些类型、字段之间怎么关联，客户端在查询文档里写出自己要的字段树，服务端按树逐字段取值。

它解决 REST 在「多端 + 多资源」场景下的三个结构性痛点：

- **过度获取**：`GET /users/1` 返回 30 个字段，首页只用 3 个，移动端弱网下白传 90% 字节。想裁剪就得加 `?fields=id,name`，然后每个接口各写一套裁剪逻辑。
- **多次往返**：订单详情页需要订单、下单人、商品、物流四个资源，REST 要么串行四次（瀑布式延迟），要么硬编码一个 `/order-detail` 聚合接口（每来一个新页面加一个）。
- **版本膨胀**：字段改不动就加新版本，最后 `v1/v2/v3` 并存；或不断追加可选参数，让接口语义变得没人说得清。

答案很直接：**一个端点、一份强类型 schema、一次请求**。前两个问题在查询文档里自然消失，第三个变成「只加字段不删字段」的兼容规则。

代价必须说清楚，否则上线就踩：

- **复杂度转移到服务端**：客户端可以合法要求 12 层嵌套，服务端必须能拒绝、限流、批处理。「一个接口一次查询」的直觉全部失效。
- **HTTP 缓存变难**：所有查询都是 `POST /graphql`，URL 相同，CDN 与浏览器缓存默认失效，只能靠持久化查询（APQ）或应用层缓存补。
- **N+1 是默认结果**：字段级解析意味着 `orders { user { name } }` 天然按行触发 N 次查询，不做 DataLoader 就是 QPS 崩塌。
- **安全面变大**：内省暴露整张 schema，深度未限制的查询能被打穿，错误信息容易泄露内部实现。

适用：面向 Web / App / 小程序 / 开放平台的 **BFF 聚合层**，前端字段需求变化快、页面要跨多个后端取数。**不适合**：内部服务间的强类型调用（用 [gRPC](../grpc/grpc-guide.md)）、只有一两个消费者的简单 CRUD（用 REST）、事件驱动的异步链路（用消息队列）。

```bash
go get github.com/99designs/gqlgen@latest   # v0.17.x
go run github.com/99designs/gqlgen init     # 首次：生成 gqlgen.yml、graph/、server.go
go run github.com/99designs/gqlgen generate # 之后每次改完 schema 重新生成
```

---

## 2. 能力总览

| 能力 | 说明 | gqlgen 中的机制 |
|---|---|---|
| Schema 强类型 | SDL 是唯一契约，字段、参数、可空性可校验 | `graph/*.graphqls` → `generated.go` 执行器 |
| 单端点 | 查询、变更、订阅共用一个 URL，按文档分派 | `handler.Server` + transport 列表 |
| 字段级解析 | 每个字段独立取值，父子关系由 resolver 决定 | 默认 resolver 反射读字段，可逐个覆写 |
| 类型复用 | 绑定已有领域模型，不重复生成 DTO | `gqlgen.yml` 的 `autobind` / `models` |
| 订阅 | 长连接推送事件 | `transport.Websocket` + 返回 `<-chan T` |
| 内省 | 客户端/IDE 可查询 schema 自身 | `extension.Introspection{}` |
| 指令 | 字段级横切逻辑（鉴权、脱敏） | `directive @x on FIELD_DEFINITION` |
| 结构化错误 | 部分成功，错误带 path 与业务码 | `gqlerror.Error` + `Extensions` |
| 防滥用 | 限制深度、复杂度、别名数量 | 自定义 `HandlerExtension` + 复杂度扩展 |

---

## 3. 核心概念与原理

### 3.1 Schema 优先：SDL 到代码的生成流程

gqlgen 只有一种模式：**先写 SDL，再生成代码**。你不写「注册字段」的代码，也不靠反射推 schema——schema 文件是唯一真相，Go 侧要么被生成，要么被 `autobind` 绑定到已有类型。

```text
graph/schema.graphqls + gqlgen.yml ─► gqlgen generate ─┬─► generated.go        执行器（勿手改）
                                                       ├─► model/models_gen.go 未绑定类型的结构体
                                                       └─► schema.resolvers.go 需要你填的空方法
                                       （autobind 命中时直接复用已有 domain struct）
```

重新 generate 时 gqlgen 会**保留已实现的方法体**，只补新增字段的空壳，所以「生成」是幂等的。这是它和 protoc 最大的体感差异：`*.pb.go` 全量重写，而 `schema.resolvers.go` 是增量的。

```yaml
# gqlgen.yml：类型从哪来、代码写哪去
schema: [graph/*.graphqls]
exec:   { filename: graph/generated.go, package: graph }
model:  { filename: graph/model/models_gen.go, package: model }
resolver:
  layout: follow-schema      # 按 schema 拆文件，团队协作冲突更少
  dir: graph
  package: graph
autobind: [example.com/shop/internal/domain]   # 复用领域模型，不生成第二份
```

### 3.2 Resolver 的执行模型

查询被解析成字段树后自顶向下遍历。每个字段有两种取值方式：**默认 resolver**（反射读父对象同名字段，零成本，如 `User.name`）与**自定义 resolver**（生成的方法，可能查库或调下游，如 `User.orders`）。同一层字段并发执行，所以 resolver 里**不能写共享可变状态**，也不能假设执行顺序。

- **mutation 顶层串行**。规范要求按文档顺序执行，`createOrder` 与 `payOrder` 不会并发；其内部子字段仍是普通字段，可以并发。
- **resolver 的 `ctx` 就是 HTTP 请求的 ctx**。客户端断开后 `ctx.Done()` 立即关闭，下游调用必须传它，否则用户早走了查询还在跑。

### 3.3 N+1 问题与 DataLoader

```text
query { orders(first: 20) { id  user { name } } }

无 DataLoader：1 次 SELECT orders LIMIT 20 + 20 次 SELECT users WHERE id = ?   ← N+1
有 DataLoader：1 次 SELECT orders LIMIT 20 + 1 次 SELECT users WHERE id IN (...)
```

DataLoader 的本质是一个**请求级批量队列**：resolver 调 `Load(id)` 时不立刻查询，而是把 key 塞进队列；等当前事件循环这批字段跑完（或攒够 `Wait` 窗口），合并成一次 `IN` 查询，再按原顺序分发回各调用方。

- **loader 必须每请求一个**，放在 `context` 里。全局单例会把不同用户的请求混进同一批，既是数据泄露也是缓存污染。
- **批量大小要封顶**（如 200）。否则 `first: 5000` 会生成 5000 个占位符的 SQL，直接超参数上限。
- **结果按 key 顺序对齐**。`IN` 不保证顺序，缺失的 key 返回 `nil` 而不是报错——「用户被删了」和「查询失败」是两件事。

### 3.4 分页：offset vs Relay Cursor

```graphql
# offset 版：实现简单，深翻页退化成全表扫描 → orders(offset: Int = 0, limit: Int = 20): [Order!]!

# Relay Cursor 连接：多一层 edges/pageInfo，翻页成本恒定
type Query { orders(first: Int = 20, after: String): OrderConnection! }

type OrderEdge { node: Order!, cursor: String! }

type OrderConnection {
  edges: [OrderEdge!]!
  pageInfo: PageInfo!
}

type PageInfo { hasNextPage: Boolean!, endCursor: String }
```

`OFFSET 100000` 要求数据库先数过前十万行再丢掉，越翻越慢；翻页期间若有新数据插入，还会**重复或漏行**。游标分页把位置编码进 cursor，用 keyset 条件直接定位：

```sql
SELECT id, amount, created_at
FROM orders
WHERE user_id = $1
  AND (created_at, id) < ($2, $3)   -- cursor 解出的 (created_at, id)；首页不带这个条件
ORDER BY created_at DESC, id DESC
LIMIT $4;
```

`(created_at, id)` 的**行比较**要求有联合索引，否则照样全扫。cursor 用 `base64("created_at|id")` 编码，客户端只当不透明字符串回传，以后换排序键不必改客户端。代价是**不能随机跳页**，运营后台真要页码就单独给一个 offset 接口。

### 3.5 错误模型：`errors` 数组与 partial success

GraphQL 响应几乎总是 HTTP 200，错误在 body 的 `errors` 数组里，且**允许部分成功**：

```json
{
  "data": { "user": { "id": "1", "name": "alice", "orders": null } },
  "errors": [{ "message": "orders: upstream timeout", "path": ["user", "orders"],
               "extensions": { "code": "UPSTREAM_TIMEOUT", "retryable": true } }]
}
```

语义是：`user` 与 `name` 取到了，`orders` 这个字段失败。前端可选「整页报错」或「局部降级」，比 REST 的「一个 500 全白干」更细，但有两个坑：

- **`data` 可能是 `null`**。顶层字段失败时 `data` 为 `null`；字段级失败时 `data` 有值。两个分支都要处理，不能只看 HTTP 状态码。
- **业务码必须放 `extensions.code`**。`message` 给人看，随时改文案；按字符串匹配 `err.message == "库存不足"` 一定会在某次发布后失效。gqlgen 侧用 `gqlerror.Error{Message, Extensions}` 构造，`path` 由执行器按字段位置自动补全。

### 3.6 订阅：WebSocket 上的长连接推送

订阅的 resolver 返回 `<-chan *model.Order`，gqlgen 把它桥接到 WebSocket 帧：客户端 `subscribe` → 每来一个事件推一条 `next` → 客户端 `complete` 或断开时关 chan。

```text
Client                                   Server
  │ ── connection_init ──────────────────► │
  │ ── subscribe {id:1, orderCreated} ───► │ ack + 建 chan，注册事件总线
  │ ◄────────── next {id:1, data} ──────── │ 每来一条事件推一帧
  │ ── complete {id:1} ──────────────────► │ close(chan)，注销
```

线协议要单独记：**新的是 `graphql-transport-ws`**（`connection_init` / `subscribe` / `next` / `complete` / `ping`），旧的是 `subscriptions-transport-ws`（`start` / `data` / `stop`）。`transport.Websocket` 按子协议头协商，两者都支持；前端库选错会出现「握手成功但一条消息都收不到」。心跳、重连、背压与 [gorilla/websocket](../websocket/websocket-guide.md) 讲的是同一件事——**订阅只是把「谁该收到哪条事件」的路由建在 WebSocket 之上**。

多实例部署最容易错：进程内总线只能推给连在本实例上的客户端，A 实例写的订单，B 实例的订阅者收不到。要么用 Redis Pub/Sub / NATS 跨实例广播，要么在网关做连接粘性。

---

## 4. 使用实战

### 4.1 从零起一个 gqlgen 项目

```text
.
├── gqlgen.yml                # 生成配置
├── server.go                 # 入口：HTTP 服务 + /graphql
└── graph/
    ├── schema.graphqls       # 唯一真相
    ├── generated.go          # 生成，勿手改
    ├── resolver.go           # Resolver 结构体，放依赖
    ├── schema.resolvers.go   # 生成骨架 + 你的实现
    └── model/models_gen.go   # 生成的类型
```

`resolver.go` 是唯一需要手动维护结构体的地方，依赖注入从这里进：

```go
package graph

import (
	"context"

	"example.com/shop/graph/model"
)

//go:generate go run github.com/99designs/gqlgen generate

type Resolver struct{ Store Store }

type Store interface {
	UserByID(ctx context.Context, id string) (*model.User, error)
	UsersByIDs(ctx context.Context, ids []string) ([]*model.User, error)
	OrdersByUser(ctx context.Context, userID string) ([]*model.Order, error)
}
```

### 4.2 写 schema 与 resolver

```graphql
type Query {
  user(id: ID!): User
  orders(userID: ID!, first: Int = 20): [Order!]!
}

type User {
  id: ID!
  name: String!
  orders: [Order!]!
}

type Order {
  id: ID!
  userID: ID!
  amount: Int!
  user: User!
}
```

填生成的空方法即可。注意 `Order.user` 走 DataLoader，而不是逐条查：

```go
package graph

import (
	"context"

	"example.com/shop/graph/model"
)

func (r *queryResolver) User(ctx context.Context, id string) (*model.User, error) {
	return r.Store.UserByID(ctx, id)
}

func (r *queryResolver) Orders(ctx context.Context, userID string, first *int) ([]*model.Order, error) {
	return r.Store.OrdersByUser(ctx, userID) // 生产上先校验 first ∈ [1,100]，见 4.5 末尾
}

// User.orders：父对象已知，直接按 userID 查
func (r *userResolver) Orders(ctx context.Context, obj *model.User) ([]*model.Order, error) {
	return r.Store.OrdersByUser(ctx, obj.ID)
}

// Order.user：父对象只有一个 userID，必须批量合并，否则就是 N+1
func (r *orderResolver) User(ctx context.Context, obj *model.Order) (*model.User, error) {
	return LoadersFrom(ctx).User.Load(obj.UserID)
}
```

### 4.3 DataLoader 消除 N+1

用 `dataloaden` 生成类型安全的 loader，省掉手写 channel 攒批的样板：

```bash
go run github.com/vektah/dataloaden UserLoader string '*example.com/shop/graph/model.User'
```

```go
package graph

import (
	"context"
	"time"

	"example.com/shop/graph/model"
)

// 每请求一个：窗口 1ms，单批最多 200 个 key；ctx 由闭包带入（dataloaden 的 Fetch 不带 ctx）
func newUserLoader(ctx context.Context, store Store) *UserLoader {
	return NewUserLoader(UserLoaderConfig{
		MaxBatch: 200,
		Wait:     1 * time.Millisecond,
		Fetch: func(keys []string) ([]*model.User, []error) {
			users, err := store.UsersByIDs(ctx, keys) // 一条 WHERE id IN (...)，ctx 来自构造时的闭包
			if err != nil {
				errs := make([]error, len(keys))
				for i := range errs {
					errs[i] = err
				}
				return nil, errs
			}
			byID := make(map[string]*model.User, len(users))
			for _, u := range users {
				byID[u.ID] = u
			}
			out := make([]*model.User, len(keys))
			for i, k := range keys {
				out[i] = byID[k] // 已删除的用户返回 nil，不制造错误
			}
			return out, nil
		},
	})
}

type Loaders struct{ User *UserLoader }

type loadersKey struct{}

func LoadersFrom(ctx context.Context) *Loaders {
	l, _ := ctx.Value(loadersKey{}).(*Loaders)
	return l
}
```

中间件里每请求新建一个 `&Loaders{User: newUserLoader(ctx, store)}` 塞进 `ctx` 即可，不要做成全局变量。

`Wait` 太小合并不了，太大会给每个请求加固定延迟，`1ms` 是常见起点。`Fetch` 返回的切片长度必须与 keys 一致、顺序一一对应——`dataloaden` 靠下标回填，长度错了会静默串数据。不想要代码生成时，`github.com/graph-gophers/dataloader/v7` 提供同样语义（`NewBatchedLoader` + thunk），本质都是「收集 key → 等窗口 → 合并查询 → 按 key 分发」。

### 4.4 鉴权：从 context 取用户、字段级权限

**中间件只做认证，鉴权交给字段。** 同一个 `/graphql` 承载所有查询，在中间件里判断「这个 URL 要不要登录」没有意义：校验 token 后把用户塞进 `ctx`（`context.WithValue(ctx, userKey{}, u)`）即可，是否放行由 directive 决定。

```graphql
directive @hasRole(role: Role!) on FIELD_DEFINITION
enum Role { ADMIN USER }

type Query {
  me: User!
  allUsers: [User!]! @hasRole(role: ADMIN)
}
```

```go
package graph

import (
	"context"

	"github.com/99designs/gqlgen/graphql"
	"github.com/vektah/gqlparser/v2/gqlerror"

	"example.com/shop/graph/model"
)

type userKey struct{}

// 中间件认证后把用户放进 ctx，这里只读
func UserFrom(ctx context.Context) (*model.User, bool) {
	u, ok := ctx.Value(userKey{}).(*model.User)
	return u, ok
}

func (r *directiveResolver) HasRole(ctx context.Context, obj interface{},
	next graphql.Resolver, role model.Role) (interface{}, error) {
	u, ok := UserFrom(ctx)
	if !ok {
		return nil, &gqlerror.Error{Message: "unauthenticated",
			Extensions: map[string]interface{}{"code": "UNAUTHENTICATED"}}
	}
	if role == model.RoleAdmin && !u.Admin {
		return nil, &gqlerror.Error{
			Message:    graphql.GetFieldContext(ctx).Field.Name + ": admin required",
			Extensions: map[string]interface{}{"code": "FORBIDDEN"},
		}
	}
	return next(ctx) // 放行：继续执行被装饰的字段
}
```

指令只写一次，敏感字段挂 `@hasRole` 即可。**别在 resolver 里零散写 `if u.Role != "admin"`**——漏一个字段就是越权漏洞，而指令写在 schema 上，评审时肉眼可见。

### 4.5 查询深度与复杂度限制

任意嵌套是 GraphQL 独有的攻击面：`users { orders { user { orders { ... } } } }` 写到 20 层，连接池瞬间抽干。两道闸门都要有。

```go
package graph

import (
	"context"

	"github.com/99designs/gqlgen/graphql"
	"github.com/vektah/gqlparser/v2/ast"
	"github.com/vektah/gqlparser/v2/gqlerror"
)

// DepthLimit：在文档校验后、执行前拦下过深的查询
type DepthLimit struct{ Max int }

func (DepthLimit) ExtensionName() string                   { return "DepthLimit" }
func (DepthLimit) Validate(graphql.ExecutableSchema) error { return nil }
func (d DepthLimit) MutateOperationContext(_ context.Context, rc *graphql.OperationContext) *gqlerror.Error {
	for _, op := range rc.Doc.Operations {
		if n := depthOf(op.SelectionSet, rc.Doc); n > d.Max {
			return gqlerror.Errorf("query depth %d exceeds limit %d", n, d.Max)
		}
	}
	return nil
}

// depthOf 遍历 selection set：Field 层数 +1；FragmentSpread 必须按 doc.Fragments 展开，否则等于没限制。
func depthOf(set ast.SelectionSet, doc *ast.QueryDocument) int {
	max := 0
	for _, sel := range set {
		n := 0
		switch s := sel.(type) {
		case *ast.Field:
			n = 1 + depthOf(s.SelectionSet, doc)
		case *ast.InlineFragment:
			n = depthOf(s.SelectionSet, doc)
		case *ast.FragmentSpread:
			if f := doc.Fragments.ForName(s.Name); f != nil {
				n = depthOf(f.SelectionSet, doc)
			}
		}
		if n > max {
			max = n
		}
	}
	return max
}
```

深度只拦「层数」，拦不住「宽度」：一层 500 个别名的 `user(id:"1"){...}` 依然能放大 500 倍。所以还要限制**别名/字段总数**与 `first` 上限；必要时按字段加权——列表字段记 5 分、单点查询记 1 分，比固定分值更贴近真实成本。注册方式见 4.6 的 `srv.Use`。

### 4.6 与 Gin 集成

```go
package main

import (
	"time"

	"github.com/99designs/gqlgen/graphql/handler"
	"github.com/99designs/gqlgen/graphql/handler/extension"
	"github.com/99designs/gqlgen/graphql/handler/transport"
	"github.com/gin-gonic/gin"

	"example.com/shop/graph"
)

func NewRouter(store graph.Store) *gin.Engine {
	srv := handler.New(graph.NewExecutableSchema(graph.Config{Resolvers: &graph.Resolver{Store: store}}))
	srv.AddTransport(transport.GET{})
	srv.AddTransport(transport.POST{})
	srv.AddTransport(transport.Websocket{KeepAlivePingInterval: 10 * time.Second})
	srv.Use(extension.FixedComplexityLimit(300)) // 每个字段记 1 分，超 300 直接拒
	srv.Use(graph.DepthLimit{Max: 12})
	// 生产不 Use(extension.Introspection{})：内省默认关闭
	r := gin.New()
	r.Use(gin.Recovery())
	// GET 查询 / POST 变更 / WS 升级共用一个入口
	r.Any("/graphql", gin.WrapH(srv))
	return r
}
```

用 `handler.New` 而非 `handler.NewDefaultServer`，是为了**自己决定开哪些 transport**——后者会把内省与 APQ 一起打开。`gin.WrapH` 复用 `net/http` 的 `ResponseWriter`，WebSocket 升级依赖的 `Hijack` 能透传，所以订阅不需要额外路由；`r.Any` 覆盖握手用的 GET，别只注册 `r.POST`。

---

## 5. 版本与生态

gqlgen 的路线很稳：**schema-first + 代码生成**，v0.17 是当前主线，多年没换范式。它和 code-first（如 `graphql-go/graphql` 用 Go 代码拼类型）的取舍很明确：

| 维度 | schema-first（gqlgen） | code-first |
|---|---|---|
| 契约来源 | `.graphqls` 文件，前后端可共同评审 | Go 代码，schema 是副产品 |
| 类型安全 | 编译期生成强类型接口 | 运行时拼装，错误更晚暴露 |
| 灵活性 | 改 schema → 重新生成 | 动态拼 schema 更自由 |

生态上还有三件事绕不开：

- **Apollo Federation**：一张图被十几个团队维护时，按域拆子图，每个子图实现 `_service { sdl }` 与 `_entities`，由 Router 做查询计划。gqlgen **不内建** Federation 指令，需要社区插件或网关侧聚合——所以「要不要拆子图」的门槛是**组织边界**，不是技术偏好。三五个服务以内，一个 BFF 就够。
- **与 REST / gRPC 共存**：GraphQL 只做最外层聚合，内部照样 gRPC 或 REST。不要为了「统一」把内部调用也改成 GraphQL，那会把 N+1 与复杂度控制扩散到每个服务。
- **持久化查询**：生产上让客户端只发查询 ID + 变量（APQ），既省带宽，又把「任意文档」变成「白名单文档」，深度与复杂度限制还能前移到构建期。

```text
Web / App / 小程序 ──GraphQL（一个端点，按需取字段）──► BFF / 网关（gqlgen）
                                                        ├─ gRPC 用户服务
                                                        ├─ gRPC 订单服务
                                                        └─ REST 存量系统
```

---

## 6. 常见坑与最佳实践

1. **N+1 没做 DataLoader**：本地 20 条数据看不出问题，线上 `first: 100` 就是 100 次查询，QPS 直接崩。凡是「列表的子字段是对象」的地方，默认就要有 loader。
2. **`omitempty` 让零值字段消失**：同一个 domain struct 既当 GraphQL model 又当 JSON 缓存/消息体时，`json:",omitempty"` 会把 `0`、`""`、`false` 抹掉，而 schema 里这些字段是 `Int!` / `String!` / `Boolean!`。表现是「直接查有值，命中缓存后字段没了」。给 GraphQL 单独定义 model，或非空标量不加 `omitempty`。
3. **深度与复杂度未限制**：最经典的被打穿方式，一个递归查询就能拖垮数据库。深度上限 + 复杂度上限 + `first` 上限，三层都要。
4. **内省在生产暴露**：`__schema` 把所有类型、字段、参数、废弃标记全吐出来，等于送攻击者一张地图。生产用 `handler.New` 且不注册 `extension.Introspection{}`。
5. **缓存只能按 POST URL**：所有查询都是 `POST /graphql`，CDN 与浏览器缓存全失效。要么改 GET（仅幂等查询，URL 长度受限），要么按「文档 hash + 变量」做应用层缓存，要么上 APQ 白名单。
6. **错误码混在 `errors` 里**：HTTP 200 + `errors[].message` 让客户端只能字符串匹配。业务码统一放 `extensions.code`，网关层收敛成一份枚举。
7. **`context` 超时未传递**：resolver 里用 `db.Query` 而不是 `QueryContext`，客户端断开后 SQL 还在跑，连接池被幽灵请求占满。长循环里也要定期检查 `ctx.Err()`。
8. **resolver 里写共享状态**：同层字段并发执行，往 `Resolver` 的 map 里写、复用可变 buffer 都会数据竞争。`go test -race` 必跑，这是 GraphQL 服务最常见的线上事故来源。
9. **订阅只推本实例**：进程内总线在多副本下必然丢消息，跨实例要用 Redis Pub/Sub、NATS 或粘性会话。

---

## 7. 选型对比

| 维度 | GraphQL | REST / HTTP + JSON | gRPC |
|---|---|---|---|
| 客户端灵活性 | **高**：自选字段、一次取多资源 | 低：响应形状由服务端定 | 低：契约固定 |
| 请求次数 | **1 次**（多资源聚合） | 多次，或加聚合接口 | 1 次（但需先定方法） |
| HTTP 缓存 | 差（POST 单 URL，需 APQ/应用层缓存） | **好**（GET + URL + ETag，CDN 友好） | 差（二进制、非幂等语义） |
| 传输效率 | 中（JSON，字段少时省流量） | 中（JSON，易过度获取） | **优**（Protobuf + HTTP/2） |
| 服务端复杂度 | 高（N+1、深度限制、权限下沉） | **低** | 中（代码生成 + 连接管理） |
| 调试成本 | 中（需 GraphiQL，错误在 200 里） | **低**（curl 即可） | 高（需 grpcurl / reflection） |
| 强类型 | 是（SDL + 代码生成） | 弱（OpenAPI 可选） | **是**（proto） |
| 实时推送 | Subscription（WebSocket） | SSE / WebSocket 另建 | 四种流原生 |
| 典型场景 | 多端 BFF、字段需求多变、页面聚合 | 开放 API、简单 CRUD、静态资源 | 内网微服务、多语言、流式 |

一句话决策：

- **面向 Web / App / 小程序，一个页面要拉四五个服务的数据，且前端字段天天变** → GraphQL 做 BFF，但必须同时上 DataLoader、深度限制、复杂度上限、鉴权指令四件套；
- **内部服务之间的同步调用** → [gRPC](../grpc/grpc-guide.md)，别拿 GraphQL 当 RPC，那只是把 N+1 与复杂度问题扩散到每个服务；
- **简单 CRUD、对外开放、需要 CDN 缓存或被第三方 curl 调用** → REST，别为「技术先进」上 GraphQL，多出来的复杂度没人买单；**只有一两个消费者、字段几乎不变** 时同样选 REST 或 gRPC。

> 一个常见误区：把 GraphQL 当成「性能优化方案」。它优化的是**请求次数与响应体积**，代价是服务端的执行复杂度——单看某个接口的耗时，GraphQL 通常比精心设计的 REST 接口更慢（多一层解析与字段拼装）。它真正省下的是「前端等三个接口串行返回」的那几百毫秒，以及「为每个新页面加一个聚合接口」的沟通成本。
