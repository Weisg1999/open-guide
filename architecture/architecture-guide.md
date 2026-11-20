# Go 项目级分层技术指南

> **一句话定位**：把「业务逻辑」从「框架、数据库、HTTP」里摘出来——分层不是为了好看，是为了让改动能只改一处。
>
> **社区实践**：DDD（Domain-Driven Design）、Clean Architecture、Hexagonal（端口-适配器）· **版本基线**：与框架无关，示例基于 Go 1.24+ · **协议**：文档 MIT

---

## 1. 项目简介

分层讨论的不是目录好不好看，而是**一处需求变更会波及多少个文件**。没分层时，三个症状会同时出现：

- **换 ORM 改 200 个文件**：`gorm.DB` 出现在 handler、定时任务、消息消费者里，数据库选型成了全仓库的公共依赖。
- **测试要连数据库**：一条「满 199 减 20」的规则，验证它得先起 MySQL、插数据、清表，单测跑一次 8 秒。
- **业务规则散落**：金额校验在 handler，状态机在 service，库存判断在 consumer，同一个「订单能否支付」有三份实现。

分层的做法是按**依赖方向**切圈：最内圈是业务规则（domain），只依赖语言本身；外面是用例编排（application），依赖 domain 的抽象；再外面是数据库、RPC、HTTP（infrastructure / interface）。依赖箭头永远指向内圈，于是「换 ORM」只动最外圈，「改规则」只动最内圈。

代价也要说清：每个仓储一个接口、每个接口一套 mock，一个用例跨 4 个目录 6 个文件，还要做 `orderPO` ↔ `domain.Order` ↔ `OrderDTO` 三次映射；10 人以下、CRUD 占 80% 的项目，样板成本会盖过收益。

所以本篇的立场是：**分层是复杂性管理工具，不是代码规范**。领域简单时，`internal/order/service.go` 一个文件足够；只有当「规则比流程复杂」时，四层才开始还本。

---

## 2. 能力总览

同样是「分」，有四种切法，区别在**切的方向**和**约束的强度**。

| 组织方式 | 依赖方向 | 适用规模 | 典型问题 |
|---|---|---|---|
| 按技术分层 | handler → service → dao，单向 | 任意，最常见 | service 变上帝类；dao 里出现业务判断 |
| 按业务模块 | `internal/order`、`internal/pay` 平级，模块内再分 | 中大型、多团队 | 模块间直接 import，退化成大泥球 |
| DDD 四层 | interface → application → domain ← infrastructure | 领域复杂、生命周期长 | 样板重；domain 被框架类型污染 |
| 六边形（端口-适配器） | 核心只依赖端口，适配器在边缘 | 多端接入 HTTP/gRPC/MQ | 端口定义过细，一个用例五个接口 |

四者不互斥：实际项目常见「按业务模块分包，模块内用 DDD 四层」；六边形则强调**端口由核心定义**，和依赖倒置是同一件事的两种说法——它只是把 interface 层改叫「入站适配器」、infrastructure 改叫「出站适配器」。

判断用哪种，看两个变量：**领域规则密度**（一次改动平均涉及几条业务约束）和**团队规模**（同时改同一模块的人数）。两者都低，按技术分层就够；两者都高，才需要四层加模块边界。

---

## 3. 核心概念与原理

### 3.1 依赖倒置：谁定义接口

**原理**：传统分层里 service 依赖 dao 的具体实现，改数据库就要改 service。倒置是让**使用方**定义它需要的抽象：application 说「我需要一个能按 ID 存取订单的东西」，infrastructure 去满足它。

Go 的隐式接口让这件事零成本：`infra.OrderRepo` 不用声明 `implements app.OrderRepo`，方法集匹配就自动满足。因此接口应写在**调用方所在的包**：

```go
package app

import (
	"context"

	"example.com/shop/internal/order/domain"
)

// OrderRepo 由使用方定义：application 只关心「能存、能取」，不关心 GORM 还是 MongoDB。
type OrderRepo interface {
	Save(ctx context.Context, o *domain.Order) error
	FindByID(ctx context.Context, id domain.OrderID) (*domain.Order, error)
}
```

由此得到可检查的规则：`domain` 不 import 项目内其他包，`app` 只 import `domain`，`infra` 与 `interface` 可以 import 前两者。违反时编译器不一定报错，但 `go list -deps ./internal/order/domain` 能查出反向依赖。

反例是把接口定义在 `infra` 包里：`app` 要 import `infra` 才能拿到接口类型，依赖方向立刻反过来，换实现时 `app` 跟着重编译——接口白抽了。

### 3.2 四层职责

| 层 | 可以 import | 职责 | 不该出现 |
|---|---|---|---|
| domain | 标准库 | 实体、值对象、领域服务、领域错误 | `gorm`、`gin`、`json` tag、SQL |
| application | domain | 用例编排、事务边界、权限校验、DTO 转换 | SQL、HTTP 状态码、具体驱动 |
| infrastructure | domain（+ 驱动库） | 仓储实现、缓存、外部 HTTP、消息发送 | 业务规则判断 |
| interface | application（+ 框架） | 参数解析、鉴权、渲染、错误码映射 | 事务、直接调仓储 |

一条经验：**如果需要用「因为业务上……」来解释，它就不属于 infrastructure**。`if order.Status == "paid" { return error }` 写在仓储里，等于把规则藏进了最外圈。

### 3.3 领域模型 vs 贫血模型

贫血模型是「结构体只有字段，逻辑都在 service」，任何包都能写 `order.Status = "paid"`，跳过校验。领域模型把不变量关进类型：字段不导出，`Pay()` 内部先判断 `status == pending` 再改状态，非法转换返回 `ErrInvalidTransition`——外部拿不到字段，就绕不过这条规则（完整代码见 4.2）。

代价是映射代码，收益是「非法状态无法被构造」。注意集合字段要做拷贝：`items` 若直接持有调用方切片，对方仍能绕过方法改内部状态；`Items()` 返回副本，否则「不导出字段」只是装饰。

### 3.4 仓储的边界

仓储不是 DAO。DAO 面向表，仓储面向**聚合**：一个聚合一个仓储，方法用业务语言命名（`FindByID`、`FindPendingBefore`），而不是 `SelectByStatusAndCreatedAt`。

| 该做 | 不该做 |
|---|---|
| 以聚合根为粒度存取，一次拿全 | 为每张表建一个 Repository |
| 返回 domain 类型 | 返回 `*gorm.DB`、`sql.Rows`、PO |
| 暴露少量业务化查询 | 通用 `Find(where map[string]any)` |
| 分页用 `Limit/Offset` 参数对象 | 把 `LIMIT` 拼进字符串 |

查询条件放哪：**用例需要的固定查询写进接口**（`FindOverdue(ctx, before time.Time)`），**可变筛选做成查询对象**（`OrderQuery{Status, From, To, Page, Size}`）。不要做能表达任意 SQL 的通用仓储，那等于把 ORM 又包一层——调用方仍要懂 SQL，却失去了类型检查。

分页与排序必须显式：`Page` 从 1 开始、`Size` 设上限（如 100），排序字段用白名单枚举，禁止把用户输入拼进 `ORDER BY`。

### 3.5 用例与事务

事务边界在 **application** 层，一个用例一个事务。只有用例知道「这次操作要保证哪些写一起成功」；仓储只看见单条 SQL，handler 只看见一次请求。

```go
type TxManager interface {
	WithinTx(ctx context.Context, fn func(ctx context.Context) error) error
}
```

实现把 `*gorm.DB` 塞进 `context`，仓储从 `context` 取连接（见 4.4）。用例因此不需要知道任何数据库类型，测试时换成「直接执行、不回滚」的假实现即可。

跨聚合的一致性不要塞进一个本地事务：订单和库存分属两个库时，本地事务保证不了原子性。做法是**一个事务只改一个聚合**，另一个改动通过 Outbox 或领域事件异步推进，补偿与幂等由消费方负责——具体模式见 [分布式事务篇](../distributed-transaction/distributed-transaction-guide.md)。把两个聚合硬写进一个 `WithinTx`，只是把分布式问题藏进单机代码。

### 3.6 模块化单体

分包的第一层应该是**业务**：`internal/{order,payment}/` 各自独立成上下文，模块内再分 `domain/app/infra/httpapi`，`internal/shared/` 只放日志、ID 生成、时钟这类真正的公共设施。

约束手段有三档，从弱到强：

1. **命名约定**：模块外只走 `app` 暴露的用例，不 import 别人的 `domain` 内部细节。
2. **静态检查**：`golangci-lint` 的 `depguard` 配「payment 不得 import order/infra」，CI 拦截。
3. **物理拆分**：模块间只通过接口或消息通信，需要时拆成独立服务——模块化单体是微服务的前置形态，边界没理清就拆服务，只是把耦合搬到网络上。

`internal/` 本身由编译器保证：其下包只能被父目录树内的代码 import，外部模块拿到也编译不过。这是 Go 免费给的模块边界。

---

## 4. 使用实战

### 4.1 目录结构

订单模块按四层落地，每个文件职责单一：

```text
internal/order/
├── domain/
│   ├── order.go           # 聚合根：状态机、不变量
│   └── errors.go          # ErrEmptyItems、ErrInvalidTransition
├── app/
│   ├── port.go            # OrderRepo / TxManager / IDGenerator
│   ├── place_order.go     # 用例：创建订单
│   └── pay_order.go       # 用例：支付订单
├── infra/
│   ├── order_repo.go      # GORM 实现，PO ↔ domain 映射
│   └── tx_manager.go      # context 携带 *gorm.DB，事务边界
├── httpapi/
│   └── handler.go         # Gin：解析 → 调用用例 → 渲染
└── grpcapi/
    └── server.go          # gRPC：复用同一批用例
```

依赖方向：`domain` ← `app` ← `httpapi`/`grpcapi`，`infra` → `domain`，`main` 负责装配。

### 4.2 domain 层：聚合根与不变量

值对象 `Item` 的校验放在聚合构造里，避免出现「订单存在、但某行数量为 0」的半合法状态。

```go
package domain

import (
	"errors"
	"fmt"
)

var (
	ErrEmptyID           = errors.New("order id is required")
	ErrEmptyItems        = errors.New("order must contain at least one item")
	ErrInvalidItem       = errors.New("item sku/quantity/price is invalid")
	ErrInvalidTransition = errors.New("invalid status transition")
	ErrOrderNotFound     = errors.New("order not found")
)

type OrderID string
type Status string

const (
	StatusPending  Status = "pending"
	StatusPaid     Status = "paid"
	StatusCanceled Status = "canceled"
)

type Item struct {
	SKU      string
	Quantity int
	Price    int64
}

type Order struct {
	id      OrderID
	items   []Item
	status  Status
	version int
}

func NewOrder(id OrderID, items []Item) (*Order, error) {
	if id == "" {
		return nil, ErrEmptyID
	}
	if len(items) == 0 {
		return nil, ErrEmptyItems
	}
	for _, it := range items {
		if it.SKU == "" || it.Quantity <= 0 || it.Price < 0 {
			return nil, fmt.Errorf("%w: %+v", ErrInvalidItem, it)
		}
	}
	return &Order{id: id, items: append([]Item(nil), items...), status: StatusPending}, nil
}

// Rehydrate 只给仓储用：从持久化状态恢复，不再跑业务校验。
func Rehydrate(id OrderID, items []Item, status Status, version int) (*Order, error) {
	if len(items) == 0 {
		return nil, ErrEmptyItems
	}
	return &Order{id: id, items: items, status: status, version: version}, nil
}

func (o *Order) ID() OrderID    { return o.id }
func (o *Order) Status() Status { return o.status }
func (o *Order) Version() int   { return o.version }
func (o *Order) Items() []Item  { return append([]Item(nil), o.items...) }

func (o *Order) Pay() error {
	if o.status != StatusPending {
		return fmt.Errorf("%w: %s -> paid", ErrInvalidTransition, o.status)
	}
	o.status = StatusPaid
	return nil
}
```

`version` 留给乐观锁：仓储 `UPDATE ... WHERE version = ?`，影响 0 行就返回冲突，由用例决定重试还是上抛。领域层不关心这个字段怎么用，只负责携带。

### 4.3 application 层：用例编排与事务

`OrderRepo`、`TxManager` 的接口定义见 3.1 与 3.5，用例只依赖它们。

```go
package app

import (
	"context"
	"fmt"

	"example.com/shop/internal/order/domain"
)

type IDGenerator interface{ NewOrderID() domain.OrderID }

type PlaceOrder struct {
	repo OrderRepo
	tx   TxManager
	ids  IDGenerator
}

func NewPlaceOrder(repo OrderRepo, tx TxManager, ids IDGenerator) *PlaceOrder {
	return &PlaceOrder{repo: repo, tx: tx, ids: ids}
}

type Line struct {
	SKU      string
	Quantity int
	Price    int64
}

func (uc *PlaceOrder) Do(ctx context.Context, lines []Line) (domain.OrderID, error) {
	items := make([]domain.Item, 0, len(lines))
	for _, l := range lines {
		items = append(items, domain.Item{SKU: l.SKU, Quantity: l.Quantity, Price: l.Price})
	}
	order, err := domain.NewOrder(uc.ids.NewOrderID(), items)
	if err != nil {
		return "", err
	}
	err = uc.tx.WithinTx(ctx, func(ctx context.Context) error {
		return uc.repo.Save(ctx, order)
	})
	if err != nil {
		return "", fmt.Errorf("place order: %w", err)
	}
	return order.ID(), nil
}

type PayOrder struct {
	repo OrderRepo
	tx   TxManager
}

func (uc *PayOrder) Do(ctx context.Context, id domain.OrderID) error {
	return uc.tx.WithinTx(ctx, func(ctx context.Context) error {
		order, err := uc.repo.FindByID(ctx, id)
		if err != nil {
			return err
		}
		if err := order.Pay(); err != nil {
			return err
		}
		return uc.repo.Save(ctx, order)
	})
}
```

读-改-写必须在**同一个事务**里，否则并发支付会双写。用例只返回 ID 或错误，不返回实体——返回实体等于把内圈类型暴露给 handler，DTO 转换会被迫在外圈做。

### 4.4 infrastructure 层：GORM 仓储与事务

```go
package infra

import (
	"context"
	"errors"
	"fmt"

	"gorm.io/gorm"

	"example.com/shop/internal/order/domain"
)

type orderPO struct {
	ID      string        `gorm:"primaryKey;size:36"`
	Status  string        `gorm:"size:16;index"`
	Version int           `gorm:"not null"`
	Items   []orderItemPO `gorm:"foreignKey:OrderID"`
}

type orderItemPO struct {
	ID       uint   `gorm:"primaryKey"`
	OrderID  string `gorm:"size:36;index"`
	SKU      string `gorm:"size:64"`
	Quantity int
	Price    int64
}

type txKey struct{}
type OrderRepo struct{ db *gorm.DB }

func NewOrderRepo(db *gorm.DB) *OrderRepo { return &OrderRepo{db: db} }
func (r *OrderRepo) conn(ctx context.Context) *gorm.DB {
	if tx, ok := ctx.Value(txKey{}).(*gorm.DB); ok {
		return tx.WithContext(ctx)
	}
	return r.db.WithContext(ctx)
}

func (r *OrderRepo) Save(ctx context.Context, o *domain.Order) error {
	po := orderPO{ID: string(o.ID()), Status: string(o.Status()), Version: o.Version()}
	for _, it := range o.Items() {
		po.Items = append(po.Items, orderItemPO{
			OrderID: po.ID, SKU: it.SKU, Quantity: it.Quantity, Price: it.Price,
		})
	}
	return r.conn(ctx).Save(&po).Error
}

func (r *OrderRepo) FindByID(ctx context.Context, id domain.OrderID) (*domain.Order, error) {
	var po orderPO
	err := r.conn(ctx).Preload("Items").First(&po, "id = ?", string(id)).Error
	if errors.Is(err, gorm.ErrRecordNotFound) {
		return nil, domain.ErrOrderNotFound
	}
	if err != nil {
		return nil, fmt.Errorf("find order %s: %w", id, err)
	}
	items := make([]domain.Item, 0, len(po.Items))
	for _, it := range po.Items {
		items = append(items, domain.Item{SKU: it.SKU, Quantity: it.Quantity, Price: it.Price})
	}
	return domain.Rehydrate(domain.OrderID(po.ID), items, domain.Status(po.Status), po.Version)
}

type GormTxManager struct{ db *gorm.DB }
func NewGormTxManager(db *gorm.DB) *GormTxManager { return &GormTxManager{db: db} }

func (m *GormTxManager) WithinTx(ctx context.Context, fn func(context.Context) error) error {
	return m.db.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		return fn(context.WithValue(ctx, txKey{}, tx))
	})
}
```

三个关键点：仓储**返回 domain 类型**，`gorm` 的表名、tag 全关在 `infra`；读回时逐项映射，空明细由 `Rehydrate` 拒绝，数据损坏在边界暴露；事务靠 `context` 传递，仓储不需要额外参数就能加入当前事务。

### 4.5 interface 层：只做翻译

```go
package httpapi

import (
	"errors"
	"net/http"

	"github.com/gin-gonic/gin"

	"example.com/shop/internal/order/app"
	"example.com/shop/internal/order/domain"
)

type OrderHandler struct {
	place *app.PlaceOrder
	pay   *app.PayOrder
}

func NewOrderHandler(place *app.PlaceOrder, pay *app.PayOrder) *OrderHandler {
	return &OrderHandler{place: place, pay: pay}
}

func (h *OrderHandler) Register(r gin.IRouter) {
	r.POST("/orders", h.create)
	r.POST("/orders/:id/pay", h.payOrder)
}

func (h *OrderHandler) create(c *gin.Context) {
	var req struct {
		Lines []app.Line `json:"lines" binding:"required,min=1"`
	}
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}
	id, err := h.place.Do(c.Request.Context(), req.Lines)
	if err != nil {
		writeErr(c, err)
		return
	}
	c.JSON(http.StatusCreated, gin.H{"id": id})
}

func (h *OrderHandler) payOrder(c *gin.Context) {
	if err := h.pay.Do(c.Request.Context(), domain.OrderID(c.Param("id"))); err != nil {
		writeErr(c, err)
		return
	}
	c.Status(http.StatusNoContent)
}

func writeErr(c *gin.Context, err error) {
	code := http.StatusInternalServerError
	if errors.Is(err, domain.ErrOrderNotFound) {
		code = http.StatusNotFound
	} else if errors.Is(err, domain.ErrInvalidTransition) || errors.Is(err, domain.ErrInvalidItem) {
		code = http.StatusUnprocessableEntity
	}
	c.JSON(code, gin.H{"error": err.Error()})
}
```

handler 里没有事务、没有 SQL、没有状态判断，只有三段：解析、调用、渲染。gRPC 侧复用同一批用例：proto 生成 `PlaceOrderRequest`，server 转成 `[]app.Line` 后调 `h.place.Do`。用例不感知调用方是 HTTP 还是 gRPC，这正是「入站适配器可替换」的含义。

### 4.6 测试策略

分层最直接的回报是测试成本下降：

| 层 | 手段 | 依赖 |
|---|---|---|
| domain | 纯单测，无 mock | 无 |
| application | 假仓储（内存 map）+ 假事务 | 无 |
| infrastructure | testcontainers 起真 MySQL | Docker |
| interface | `httptest` 打 handler | 假仓储 + 真用例 |

domain 的测试不需要任何替身：`NewOrder` 造一个待支付订单，连续调两次 `Pay()`，断言第二次返回 `ErrInvalidTransition`、状态仍是 `paid`。这类用例几十行代码就能覆盖全部状态迁移，跑一次几毫秒。

仓储只测「SQL 真的对」：建表、插入聚合、按 ID 读回、比对字段。这类测试慢但数量少（一个仓储三五个用例），用 testcontainers 起真 MySQL，以 `-tags=integration` 单独跑，不拖慢日常 `go test ./...`。

application 的测试重点是**编排顺序**：假仓储记录调用次数，断言「读 → 改 → 写」都在同一事务内发生。用真数据库测用例，失败时很难分清是规则错还是 SQL 错。

---

## 5. 版本与生态

社区对 DDD 一直有两派意见，且都有道理，取舍见 7 的结论：

- **「Go 不需要 DDD」**：Go 没有注解与 AOP，框架侵入性低，一个 `service.go` 加 `sqlc` 生成的代码就能覆盖大多数业务。四层加映射，写起来像 Java 的译本。
- **「DDD 是复杂度管理」**：规则一旦超过增删改查，贫血模型就会把状态机拆散到多个 handler，此时分层是唯一能局部化改动的手段。

`internal/` 的规则值得单独记：只有 `internal` 父目录下的代码能 import 它。放在仓库根，等于「本模块可用、外部不可用」；放在 `internal/order/` 下，连兄弟模块都进不来——比 lint 更硬的边界。

依赖注入工具与分层**正交**：`wire` 生成 `NewOrderHandler(NewPlaceOrder(NewOrderRepo(db), ...))` 这样的装配代码，`fx` 用运行时容器做同样的事。它们只解决「谁 new 谁」，不解决「谁能 import 谁」。若 `wire.go` 里 `domain` 还依赖 `*gorm.DB`，注入只是把耦合搬到了构造函数签名上。

生态参考：`go-kratos/kratos` 的 `internal/biz`、`internal/service`、`internal/data` 是三层变体；`gin-gonic/examples` 的 `clean-architecture` 目录是四层最小示例。读它们时重点看 import 关系，而不是目录名。

---

## 6. 常见坑

1. **为了分层而分层**：为每张表建一个 Repository 接口。仓储以聚合为粒度，`order_items` 没有独立仓储；表结构变化不该引起接口变化。
2. **domain 里出现 `gorm.Model` 或 `json` tag**。一旦 domain 依赖框架，换 ORM 就要改业务代码，收益归零。
3. **事务开在 handler 里**。handler 拿不到「这次请求包含哪些业务写」，只能按请求包一层，跨用例复用就失效。
4. **跨层直接传 DTO**。把 HTTP 请求结构体传进 domain，业务规则就依赖 JSON 字段名；反过来直接 `c.JSON(domain.Order)`，不导出字段无法序列化，最后被迫加 tag。
5. **循环依赖用 interface 硬解**。`domain` 需要回调时，把接口定义在 domain 侧（如 `OrderPaidNotifier`）、由 app 实现；放到 app 再让 domain import，方向就反了。
6. **领域事件滥用**。一个「创建订单」发五个事件、三个消费者，任何一环失败都难排查。事件只用于**跨聚合、跨模块**解耦。
7. **`internal` 滥用**。所有包都塞进 `internal/`，连工具包也进不来，最后只能复制代码。库代码应放在可被外部 import 的路径。
8. **四层但零测试**。分层的主要收益是可测试性；domain 仍要连库才能测，说明依赖没有真正倒置。

一条自查：把 `infra` 整个删掉，`go build ./internal/order/domain ./internal/order/app` 还能过吗？能，边界成立；不能，说明反向依赖藏在某处。

---

## 7. 选型对比

| 维度 | 不分层 | 按技术分层 | DDD 四层 | 六边形 |
|---|---|---|---|---|
| 上手成本 | 最低 | 低 | 高 | 中高 |
| 可测试性 | 差（要连库） | 中（service 可测） | 高（domain 纯单测） | 高 |
| 换存储成本 | 全仓库改 | 改 dao 层 | 改 infra 层 | 改出站适配器 |
| 适合团队规模 | 1–3 人 | 3–15 人 | 10 人以上 | 多端接入团队 |
| 适合领域 | 纯 CRUD、原型 | 业务简单但要多端 | 规则密集、生命周期长 | 同一核心多种入口 |

结论按三个条件给：

- **10 人以下、领域简单**：别上 DDD。「按业务分包 + 每包一个 service 层 + sqlc」已能拿到 80% 的收益，剩下 20% 的抽象成本会拖慢交付。
- **领域复杂、系统要活三年以上**：值得四层。规则会持续变，每次变更的影响面被 domain 边界限制住，这才是分层真正付费的地方。
- **多端接入同一核心**（HTTP + gRPC + 消息消费）：用六边形思路，把用例当核心、入口当适配器。它与四层的区别只是**措辞**：端口就是 application 定义的接口，适配器就是 interface / infrastructure。

一句话判断：**改动频率 × 规则密度 × 团队人数**，三者相乘超过阈值才分层；否则先写直白代码，等第一次「改一处坏三处」发生时再抽——那时你已经知道边界该画在哪。
