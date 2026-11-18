# Go 设计模式技术指南

> Go 没有继承，很多 GoF 模式失去了存在理由；真正该学的是「用接口、函数值与组合替代继承」之后，剩下的那几种形状。
> 参考实现：<https://github.com/senghoo/golang-design-pattern>（GoF 23 种的 Go 版本）　|　适用 **Go 1.22+**（含泛型与 range-over-func）　|　许可：MIT

---

## 1. 项目简介

上游 [senghoo/golang-design-pattern](https://github.com/senghoo/golang-design-pattern) 是《研磨设计模式》的读书笔记：23 个编号目录，每个目录几十行最小 Go 实现，MIT 协议。价值是**完整**，局限也是完整——把 Java 的 23 个模子逐个倒进 Go，会得到一批只有一个实现、只调用一次、只为对齐目录而存在的接口。

本文不逐条翻译，而是回答三个问题：

1. **哪些消失了**：模板方法、抽象工厂、原型、迭代器中的大部分，被函数值、嵌入、泛型、`range-over-func` 吸收，硬写只剩形式。
2. **哪些变形了**：建造者变成 functional options；策略变成 `func` 类型；装饰器与责任链都变成「包装同签名函数」；观察者变成 channel 或回调表；代理变成 `sync.Once` 惰性加载。
3. **哪些必须新造**：functional options、HTTP/gRPC 中间件、`errgroup` 编排、pipeline、`context` 取消树——GoF 书里没有，工程里天天用。

所以本文按「Go 的语言事实」重排，而不是按 GoF 分类重排。上游只作出处引用：想查某个经典模式的最小写法，去那里翻目录更快。

---

## 2. 能力总览

| 模式 | Go 中的形态 | 标准库 / 官方库实例 | 何时用 |
|---|---|---|---|
| 单例 | `sync.Once` + 包级变量 | `http.DefaultClient`、`sql.Register` | 进程内唯一、有状态 |
| 简单工厂 / 工厂方法 | 返回接口的构造函数、`map[string]func()` 注册表 | `errors.New`、`sql.Open` | 调用方只关心行为 |
| 抽象工厂 | 一族构造函数收进接口或配置结构体 | `database/sql/driver` | 需要整族替换（DB、云 SDK） |
| 建造者 | functional options | `grpc.NewServer(opts...)`、`zap.New` | 参数多、大多有默认值 |
| 原型 | 值拷贝、`Clone()` | `slices.Clone`、`maps.Clone` | 复制比重新构造便宜 |
| 适配器 | 包一层、实现目标接口 | `http.HandlerFunc`、`io.NopCloser` | 复用签名不兼容的第三方 |
| 装饰器 | 同签名函数包同签名函数 | `http.Handler` 中间件、`gzip.Writer` | 正交能力叠加 |
| 代理 | 惰性初始化包装 + 访问控制 | `singleflight.Group` | 昂贵资源、远程调用 |
| 外观 | 一个函数包住多个子系统 | `os/exec`、`log/slog` | 给复杂依赖一个稳定入口 |
| 组合 | 树形结构 + 统一接口 | `io/fs.FS`、`go/ast.Node` | 递归结构（文件树、菜单） |
| 策略 | 函数类型，而非接口 | `slices.SortFunc`、`http.RoundTripper` | 算法运行时可变 |
| 责任链 | 中间件链 | `net/http` 的 handler 包装 | 顺序处理、可插拔环节 |
| 观察者 | channel 广播、回调注册表 | `context` 取消广播、`signal.Notify` | 一对多、弱耦合通知 |
| 命令 | 闭包或带 `Do()` 的结构体 | `exec.Command`、`cobra.Command` | 延迟执行、可排队 |
| 迭代器 | `func(func(T) bool)` | `iter.Seq`、`maps.Keys` | 惰性遍历、大数据流 |
| 模板方法 | 固定流程函数 + 接口钩子 | `sort.Sort`（旧）、`http.Handler` | 骨架不变、单步可变 |

解释器、访问者、桥接、享元、备忘录、中介者不在表里：不是 Go 不能写，而是业务代码里出现频率低到不值得默认掌握。真遇到 AST 求值或编辑器撤销栈时再回上游查。

---

## 3. 核心概念与原理

### 3.1 创建型：从「工厂」到「函数返回接口」

**模式**：工厂方法把 `new` 藏起来，抽象工厂保证产品族一致，建造者解决参数爆炸，单例保证唯一。**Go 形态**：构造函数就是工厂——`NewX(...) *X`。没有类层次，就没有「工厂子类」；按名字选择时用 `map[string]func(Config) (T, error)` 注册表。建造者退化为 functional options（`Option func(*Config)`），单例退化为 `sync.Once`。

**标准库实例**：`errors.New` 是最朴素的工厂；`sql.Open(driver, dsn)` 是按名字查驱动表的抽象工厂。**反例**：为「以后可能换实现」写 `UserRepo` 接口 + `userRepo` 结构体 + `NewUserRepo` 三件套，而全仓库只有一个实现、一个调用点。这不是工厂，是三层转发。

```go
type Registry struct{ data map[string]string }
var (
	once     sync.Once
	registry *Registry
)
func GetRegistry() *Registry {
	once.Do(func() { registry = &Registry{data: map[string]string{}} })
	return registry
}
```

为什么不用 `init()`：无法返回错误，失败只能 `panic`；包被导入就执行，哪怕这次运行用不到；测试里无法重置或替换。`sync.Once` 把初始化推迟到首次使用，且失败可重试（把错误存下来，下次判断）。

构造函数返回什么，是最容易被滥用的决策：**默认返回具体类型**，调用方能用全部方法，日后加方法不破坏兼容；只有确实想让调用方只看见行为、或实现必须藏在包内时，才返回接口。`func NewMemStore() Store { return &memStore{} }` 的代价是调用方拿不到额外能力，也无法在不改接口的前提下扩展。

functional options 的形状（完整用法见 4.1）：每个选项是 `func(*Server)`，`NewServer` 先填默认值再逐个应用，因此零值永远是安全的；与 builder 相比不需要 `Build()` 终态，也不可能「忘了 Build」，代价是表达不了「A 与 B 互斥」这类约束。

对象池用 `sync.Pool`，但它是**临时对象复用**而不是缓存：GC 随时清空，取不到就调 `New`；适合短命、可重建、分配频繁的缓冲（`bytes.Buffer`、编解码临时结构），不适合连接池、会话、任何有外部资源或需要保序的对象。放回前必须 `Reset`，否则会把上一次的数据泄漏给下一个使用者。

### 3.2 结构型：适配器与装饰器就是「包装同一个接口」

**模式**：适配器改变接口，装饰器保持接口并叠加行为，代理保持接口并控制访问，外观简化一组接口，组合把树当叶子用。**Go 形态**：前四者都归结为一个动作——写一个结构体，字段里持有同签名的东西，方法里转发并加料。没有抽象类，包装就是组合；接口是隐式的，实现目标接口不需要声明。

**标准库实例**：`io.Reader` 生态（`bufio.Reader`、`gzip.Reader`、`io.TeeReader`、`io.LimitReader`）是装饰器教科书，`io.Writer` 侧有 `bufio.Writer`、`gzip.Writer`、`io.MultiWriter`；`http.Handler` 中间件同理。**反例**：为一个 `io.Reader` 包装器定义 `ReaderDecorator`、`ReaderDecoratorFactory`、`AbstractReaderDecorator` 三个类型，而实际只包了 `bufio`。

```go
type countingReader struct {
	r io.Reader
	n int64
}
func (c *countingReader) Read(p []byte) (int, error) {
	n, err := c.r.Read(p)
	c.n += int64(n)
	return n, err
}
func CountBytes(r io.Reader) (io.Reader, *int64) {
	cr := &countingReader{r: r}
	return cr, &cr.n
}
```

`http.Handler` 中间件完全同构，只是方法叫 `ServeHTTP`（完整链见 4.2）。「保持签名、包一层」便宜到不需要命名模式，真正要想清楚的是**顺序**：日志在恢复之内还是之外、超时包住整条链还是只包业务。顺序错了，观测数据就会骗人。

`sort.Interface` 是把比较抽成接口的旧写法：三个方法 `Len`/`Less`/`Swap`，为每种排序维度定义一个类型。Go 1.21 后应直接写 `slices.SortFunc(files, func(a, b File) int { return cmp.Compare(a.Size, b.Size) })`——接口消失，只剩函数值。

嵌入（embedding）常被当作继承用，但它只是**字段提升**：`type User struct { Base; Name string }` 会让 `User` 拥有 `Base` 的 `Describe()`，但 `Base` 里的代码永远不知道 `User` 的存在——没有虚函数分发，`Base` 调不到 `User` 的「覆盖」版本。要可变行为，只能用接口注入钩子，或在 `User` 上重写同名方法（那是遮蔽，不是覆盖）。

代理在 Go 里通常是惰性加载：结构体持有 `sync.Once`、路径与结果，`Get()` 首次调用时加载，之后直接返回缓存（含错误）。外观是**多对一**（把 DB、缓存、日志收进一个 `Service`），代理是**一对一**（在同一接口前后加控制）；外观不实现被包者的接口，代理实现。

### 3.3 行为型：策略 = 函数值，责任链 = 中间件，观察者 = channel

**模式**：策略把算法抽成对象；责任链把处理者串成链；观察者维护订阅表并广播；模板方法把骨架放父类；命令把请求封装成对象。**Go 形态**：策略用函数类型（无状态）或单方法接口（有状态）；责任链就是中间件；观察者用 channel 或回调注册表；模板方法退化为「普通函数 + 接口参数」；命令用闭包。

**标准库实例**：`slices.SortFunc` 是策略；`http.Handler` 链是责任链；`context.Done()` 是取消广播；`signal.Notify` 是观察者。**反例**：五个策略各写一个 interface + 一个 struct，而每个 `Do()` 只有一行、没有字段。此时接口只是给函数加了四行样板。

```go
type PricingFunc func(amount int64) int64
func NoDiscount(amount int64) int64 { return amount }
func PercentOff(pct int64) PricingFunc {
	return func(amount int64) int64 { return amount * (100 - pct) / 100 }
}
type Checkout struct{ price PricingFunc }
func (c *Checkout) Total(amount int64) int64 {
	if c.price == nil {
		return NoDiscount(amount)
	}
	return c.price(amount)
}
```

只有当策略需要配置以外的状态（连接、计数器、缓存）时，才升级成 `interface{ Apply(int64) int64 }`——此时结构体字段就是那个状态。

责任链与中间件是同一个东西：`Chain(h, A, B)` 等价于 `A(B(h))`，倒序应用，请求先过 A。没有处理者类，也没有 `SetNext`；链就是嵌套的闭包，环节就是 `func(http.Handler) http.Handler`。写反不会报错，只会让「最外层」变成「最内层」。

观察者用 channel 时，必须决定**发布方是否阻塞**：无缓冲 channel + 阻塞发送，会把一次发布变成「等所有订阅者消费完」，一个卡住的消费者能拖垮整个服务；正确做法是带缓冲 + `select/default`，把丢弃数记进指标。fan-out 时每个订阅者一条 goroutine，退订要能终止它，完整实现见 4.4。

模板方法在 Go 里没有父类，只剩「接口 + 组合」：固定流程函数接收一个接口参数，`func Import(src io.Reader, p Parser) (int, error)` 里 `Import` 是骨架，`Parser.Parse` 是可变步骤；不需要 `AbstractImporter`，因为复用流程靠函数调用，不靠继承。命令用闭包同理：`type Command func(ctx context.Context) error`，队列只需 `[]Command` 和 `for` 循环；需要撤销时才升级成带 `Do`/`Undo` 的结构体。

### 3.4 Go 特有的模式：options、middleware、errgroup、pipeline

**模式**：这四个不在 GoF 里，却是 Go 代码里出现频率最高的结构。共同点是**用语言原语替代类型层次**：函数值、接口、goroutine、channel。**Go 形态**：options 是 `func(*Config)` 变参；middleware 在 HTTP 是 `func(http.Handler) http.Handler`，在 gRPC 是 `grpc.UnaryServerInterceptor`；`errgroup` 是 `WaitGroup` + 错误传播 + 取消；pipeline 是 channel 串阶段 + `close` 传播结束。

**标准库实例**：`http.Handler`、`grpc.UnaryServerInterceptor`、`golang.org/x/sync/errgroup`、`context`。**反例**：自己写 `TaskGroup`，内部 `WaitGroup` + `error` 切片 + 手写取消，行为和 `errgroup` 一样，却少了「第一个错误取消其余」的语义。

```go
func FetchAll(ctx context.Context, urls []string) ([]string, error) {
	g, ctx := errgroup.WithContext(ctx)
	results := make([]string, len(urls))
	for i, u := range urls {
		g.Go(func() error {
			body, err := fetch(ctx, u) // fetch 为项目内的抓取函数
			if err != nil {
				return fmt.Errorf("fetch %s: %w", u, err)
			}
			results[i] = body
			return nil
		})
	}
	if err := g.Wait(); err != nil {
		return nil, err
	}
	return results, nil
}
```

Go 1.22 起循环变量每轮独立，`i`、`u` 可直接在闭包里用；1.21 及以前必须 `i, u := i, u`。写不同下标是安全的，但**读 `results` 之前必须 `Wait`**。

pipeline 的每个阶段都要能被取消，否则上游退出后下游会永久阻塞：

```go
func gen(ctx context.Context, nums ...int) <-chan int {
	out := make(chan int)
	go func() {
		defer close(out)
		for _, n := range nums {
			select {
			case out <- n:
			case <-ctx.Done():
				return
			}
		}
	}()
	return out
}
```

`square(in <-chan int) <-chan int` 的结构完全相同，只把 `n` 换成 `n*n`；最后 `for v := range square(ctx, gen(ctx, 1, 2, 3))` 消费。`context` 本身也是一种模式：**取消树**。每个请求派生一个 `ctx`，下游共享；客户端断开或超时，整棵树同时收到信号——比任何「观察者 + 生命周期管理器」都可靠，因为运行时和标准库共同保证。

### 3.5 并发模式与设计模式的区别

**模式**：设计模式解决「代码怎么组织」，并发模式解决「多个执行流怎么协作」。前者是静态结构，后者是时序契约。**Go 形态**：worker pool、fan-in/fan-out、pipeline、超时与取消；通常**不需要接口**，只需要 channel 和 `context`。

**标准库实例**：`net/http` 的每连接 goroutine、`errgroup` 的扇出、`runtime/pprof` 的采样。**反例**：把 worker pool 写成 `WorkerPoolFactory` + `AbstractWorker` + `TaskQueue`，然后在 `Start()` 里 `go func()`——抽象层没解决任何时序问题，只增加了阅读距离。

worker pool 的骨架只有三行：`for i := 0; i < n; i++ { go worker() }`、`wg.Wait()`、每个 worker 里一个 `select` 监听 `ctx.Done()`。没有任何接口，因为要解决的问题是「谁在什么时候退出」，不是「谁替换谁」。

| 维度 | 设计模式 | 并发模式 |
|---|---|---|
| 解决的问题 | 依赖方向、替换、复用 | 顺序、取消、背压、竞态 |
| 失效表现 | 改一处要改很多文件 | 偶发死锁、数据竞争、goroutine 泄漏 |
| 验证方式 | code review、依赖图 | `-race`、压力测试、超时注入 |
| 常见错误 | 过度抽象 | 忘记 `close`、无缓冲阻塞、丢 `ctx` |

一句话：**并发模式解决时序，设计模式解决结构**。混为一谈的典型产物是「用接口包了一个 `go func()`」，既没有抽象收益，也说不清谁负责关闭 channel。

### 3.6 什么时候不该用模式

**模式**：模式是对反复出现的结构性问题的命名。没有问题，就没有模式。**Go 形态**：「不该用」的信号很具体——接口只有一个实现且没有测试替身需求；抽象层只是转发（`Service.Create` → `Repo.Create` → `DAO.Create`，三层都没逻辑）；为「未来可能换存储」预留扩展点，而路线图上没有这件事；工厂只 `return &impl{}`，没有选择逻辑；用 `any` 做「通用」参数，调用方每次都要断言。

**标准库实例**：`net/http.Handler` 只有一个方法却值得存在——因为它有大量实现和中间件生态。这说明接口的正当性来自**多实现或边界**，不是「看起来解耦」。**反例**：全仓库只有一个 `userRepo`、只有一处调用，却定义了 `UserRepo` 接口。它唯一的作用是让 IDE 的「跳转定义」多跳一次。

Go 的隐式接口意味着：**等真需要第二个实现时再抽，成本是零**；提前抽，成本是永久的间接层。它也更危险——任何类型只要方法凑巧相同就满足接口，重构时可能悄悄绑错实现，所以接口应尽量小、尽量定义在使用方。YAGNI 在这里格外适用：加方法，所有实现编译期报错；改签名，`go build` 全量指出。语言已经做了静态检查，不需要用抽象层再买一份保险。

---

## 4. 使用实战

### 4.1 functional options 配置 HTTP server

默认值先填，再应用 options；顺序反了，未传的选项会变成危险的零值（`ReadHeaderTimeout=0` 表示不超时）。

```go
package main
import (
	"fmt"
	"log"
	"net/http"
	"time"
)
type Option func(*http.Server)
func WithAddr(a string) Option { return func(s *http.Server) { s.Addr = a } }
func WithTimeout(d time.Duration) Option {
	return func(s *http.Server) { s.ReadHeaderTimeout, s.WriteTimeout = d, d }
}
func NewServer(h http.Handler, opts ...Option) *http.Server {
	s := &http.Server{Addr: ":8080", Handler: h, ReadHeaderTimeout: 5 * time.Second}
	for _, opt := range opts { opt(s) }
	return s
}
func main() {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprintln(w, "ok")
	})
	srv := NewServer(mux, WithAddr(":9090"), WithTimeout(3*time.Second))
	log.Printf("listening on %s", srv.Addr)
	log.Fatal(srv.ListenAndServe())
}
```

### 4.2 中间件链：日志 + 恢复 + 超时

三个关注点各自独立，组合顺序决定语义：`Recover` 必须在最外层，才能兜住后面所有 panic。

```go
package main
import (
	"context"
	"log/slog"
	"net/http"
	"time"
)
type Middleware func(http.Handler) http.Handler
func Chain(h http.Handler, mws ...Middleware) http.Handler {
	for i := len(mws) - 1; i >= 0; i-- { h = mws[i](h) }
	return h
}
func Logging(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		next.ServeHTTP(w, r)
		slog.Info("req", "path", r.URL.Path, "cost", time.Since(start))
	})
}
func Recover(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		defer func() {
			if v := recover(); v != nil { http.Error(w, "internal error", 500) }
		}()
		next.ServeHTTP(w, r)
	})
}
func Timeout(d time.Duration) Middleware {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			ctx, cancel := context.WithTimeout(r.Context(), d)
			defer cancel()
			next.ServeHTTP(w, r.WithContext(ctx))
		})
	}
}
func main() {
	h := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.Write([]byte("ok")) })
	http.Handle("/", Chain(h, Recover, Logging, Timeout(time.Second)))
	http.ListenAndServe(":8080", nil)
}
```

`Timeout` 只设置 `ctx` 截止时间，**不会**自动中断 handler：handler 必须把 `ctx` 传给下游，或在长循环里检查 `ctx.Err()`。要硬中断，得配合 `http.TimeoutHandler` 或 `ResponseController`。

### 4.3 策略 + 工厂：按渠道选择通知发送器

渠道到实现的映射在启动时注册，运行时不加锁；新增渠道只加一行 `Register`。

```go
package main
import (
	"context"
	"errors"
	"fmt"
)
type Notifier interface {
	Notify(ctx context.Context, to, msg string) error
}
type FuncNotifier func(ctx context.Context, to, msg string) error
func (f FuncNotifier) Notify(ctx context.Context, to, msg string) error { return f(ctx, to, msg) }
var registry = map[string]Notifier{}
func Register(channel string, n Notifier) { registry[channel] = n }
func Send(ctx context.Context, channel, to, msg string) error {
	n, ok := registry[channel]
	if !ok {
		return fmt.Errorf("unknown channel %q", channel)
	}
	return n.Notify(ctx, to, msg)
}
func main() {
	Register("sms", FuncNotifier(func(ctx context.Context, to, msg string) error {
		fmt.Println("sms", to, msg)
		return nil
	}))
	Register("email", FuncNotifier(func(ctx context.Context, to, msg string) error {
		if to == "" {
			return errors.New("empty recipient")
		}
		fmt.Println("email", to, msg)
		return nil
	}))
	_ = Send(context.Background(), "sms", "138****0000", "hello")
}
```

`FuncNotifier` 是「函数 → 接口」的适配器，标准库同款是 `http.HandlerFunc`。注册表只在 `main` 初始化阶段写，所以省掉 mutex；若运行时要动态注册，必须加 `sync.RWMutex`。

### 4.4 观察者：事件总线（channel + 退订 + 非阻塞发布）

订阅返回只读 channel 和幂等取消函数；发布用 `select/default` 丢弃慢订阅者的事件，而不是阻塞。

```go
package main
import (
	"fmt"
	"sync"
)
type Event struct{ Topic, Payload string }
type Bus struct {
	mu     sync.RWMutex
	subs   map[int]chan Event
	nextID int
}
func NewBus() *Bus { return &Bus{subs: map[int]chan Event{}} }
func (b *Bus) Subscribe(buf int) (<-chan Event, func()) {
	b.mu.Lock()
	id := b.nextID
	b.nextID++
	ch := make(chan Event, buf)
	b.subs[id] = ch
	b.mu.Unlock()
	var once sync.Once
	return ch, func() {
		once.Do(func() {
			b.mu.Lock()
			delete(b.subs, id)
			close(ch)
			b.mu.Unlock()
		})
	}
}
func (b *Bus) Publish(e Event) (dropped int) {
	b.mu.RLock()
	defer b.mu.RUnlock()
	for _, ch := range b.subs {
		select {
		case ch <- e:
		default:
			dropped++
		}
	}
	return dropped
}
func main() {
	bus := NewBus()
	ch, cancel := bus.Subscribe(2)
	defer cancel()
	go func() {
		for e := range ch {
			fmt.Println(e.Topic, e.Payload)
		}
	}()
	for i := 0; i < 5; i++ {
		bus.Publish(Event{"order.paid", fmt.Sprint(i)})
	}
}
```

三个细节：`close(ch)` 必须在持写锁时做，否则可能与正在发送的 `Publish`（持读锁）撞上，触发 `send on closed channel`；取消函数用 `sync.Once` 保证重复调用安全；`dropped` 是背压信号，应打进指标而不是静默丢弃。

### 4.5 泛型 + 策略：可组合的 Map / Filter

「遍历方式」固定，「单步逻辑」作为函数值传入，泛型负责类型安全。

```go
package main
import "fmt"
func Map[T, U any](xs []T, f func(T) U) []U {
	out := make([]U, 0, len(xs))
	for _, x := range xs {
		out = append(out, f(x))
	}
	return out
}
func Filter[T any](xs []T, keep func(T) bool) []T {
	out := make([]T, 0, len(xs))
	for _, x := range xs {
		if keep(x) {
			out = append(out, x)
		}
	}
	return out
}
type User struct {
	Name string
	Age  int
}
func main() {
	users := []User{{"alice", 17}, {"bob", 23}, {"carol", 31}}
	adults := Filter(users, func(u User) bool { return u.Age >= 18 })
	fmt.Println(Map(adults, func(u User) string { return u.Name }))
}
```

不要在业务代码里为每个类型重造这套工具：标准库已有 `slices`、`maps`。这里的价值是演示「泛型替代了容器型模式」——`Iterator`、`Composite`、`Flyweight` 的大部分需求被 `Map/Filter` 加 `slices` 吃掉了。

---

## 5. 版本演进：语言如何让模式变得多余

| 版本 | 变化 | 被削弱的模式 |
|---|---|---|
| Go 1.13 | `errors.Is` / `errors.As` / `%w` | 错误码枚举、类型断言链 |
| Go 1.16 | `//go:embed` | 资源加载工厂、`Asset()` 抽象 |
| Go 1.18 | 类型参数 | 容器型模式：`Iterator`、`Flyweight` |
| Go 1.21 | `slices` / `maps` / `cmp`、内置 `min`/`max` | `sort.Interface` 策略、手写查找工具 |
| Go 1.22 | 循环变量每轮独立、路由增强 | 「防御性复制」闭包惯用法 |
| Go 1.23 | range-over-func 正式（1.22 为实验） | `Iterator` 模式彻底函数化 |

`errors` 的演进最典型：以前判错误类型要写 `switch err.(type)` 或自定义 `Is()`；现在哨兵错误 + `errors.Is` 就是标准做法，包装链由 `%w` 维护。`sort.Interface` 的三个方法则被一行函数值取代：`slices.SortFunc(files, func(a, b File) int { return cmp.Compare(a.Size, b.Size) })`。

迭代器从「带 `Next()`/`Value()` 的结构体」变成一次函数调用：

```go
func Backward[T any](xs []T) func(func(T) bool) {
	return func(yield func(T) bool) {
		for i := len(xs) - 1; i >= 0; i-- {
			if !yield(xs[i]) {
				return
			}
		}
	}
}
```

消费端就是 `for v := range Backward(xs) { ... }`。range 版本在 Go 1.23+ 可编译，1.22 需要 `GOEXPERIMENT=rangefunc`。意义不只是语法糖：`yield` 返回 `false` 时立即停止，`break` 能正确传播，不需要 `Stop()`，也不需要调用方记得关闭。`embed` 则让「模板方法 + 资源子类」消失：以前要 `SchemaProvider` 接口、`fileSchema` 实现、测试替身，现在只要 `//go:embed schema.sql` 加一个 `var schema string`。

---

## 6. 常见坑与最佳实践

1. **为单一实现造接口**。接口的正当性来自多实现、测试替身或包边界，不来自「解耦」这个词。等需要时再抽，成本为零。
2. **接口污染**：参数写 `any`，函数体第一行就是类型断言。这是把静态语言当动态语言用，调用方失去编译期检查。
3. **在 `init()` 里做单例**。无法返回错误、无法重置、包被导入就执行。用 `sync.Once` + 显式 `New`，测试才能替换。
4. **用 embedding 模拟继承**。嵌入只提升字段与方法，没有虚函数分发；`Base` 调不到 `Child` 的「覆盖」版本。要多样态就传接口。
5. **装饰器顺序写反**。`Chain(h, A, B)` 得到 `A(B(h))`，A 在最外层。日志包在恢复外面，panic 时就没有访问日志；超时包在最里面，就管不到前面的中间件。
6. **channel 版观察者用无缓冲 channel**。发布方会被最慢的订阅者拖住，一个卡死的消费者能让服务雪崩。用缓冲 + `select/default`，并统计丢弃数。
7. **忘记 `close` 或重复 `close`**。发送方负责关闭，接收方永不关闭；多发送方时用 `sync.Once` 或额外的协调 goroutine。
8. **泛型滥用**。约束比实现还长、推导不出类型参数、每个调用都要显式写 `[T, U]`——此时接口或代码生成更简单。泛型适合容器与算法，不适合「让函数看起来通用」。
9. **把模式名写进函数名**。`BuildUserRepositoryFactory`、`ExecuteNotificationStrategy` 让读者先查模式再看代码。名字应描述**做什么**，不是**用了什么招**。
10. **对象池当缓存用**。`sync.Pool` 会被 GC 清空，不能存连接、会话、任何需要保序或有外部资源的对象。
11. **工厂里藏 I/O**。`NewClient()` 里发网络请求、读配置，导致构造函数可能失败却只返回一个值。构造只做内存初始化，I/O 放到显式 `Start`/`Connect`。
12. **用 `Option` 传必填参数**。必填项应是位置参数：`NewServer(h http.Handler, opts ...Option)`；全塞进 options 会让「忘了传」变成运行时零值错误。
13. **在库代码里 `panic`**。库的边界应返回 `error`；`panic` 只用于状态不可恢复，且必须在服务边界（中间件）统一 recover。
14. **抽象层没有第二个实现，也没有测试**。判断标准：删掉这层，代码是否只变短不变难？如果是，删掉。

元规则：**先写三遍重复代码，再抽模式**。第一次写直白版本，第二次复制，第三次看清变化点后再抽象。Go 的重构成本低（编译期全量检查），过早抽象的成本却很高（间接层永久存在）。

---

## 7. 选型对比

### 7.1 模式 vs 直接写

| 场景 | 直接写 | 引入模式 | 建议 |
|---|---|---|---|
| 一个实现、一个调用点 | 结构体 + 方法 | 接口 + 工厂 + DI | 直接写 |
| 两个实现，运行时切换 | `if/switch` | 策略接口 | 先 `switch`，分支超 5 个再抽 |
| 参数 3 个以内 | 位置参数 | functional options | 位置参数 |
| 参数 8 个、多数有默认值 | 配置结构体 | functional options | options 或配置结构体 |
| 给已有类型加行为 | 改原类型 | 装饰器包装 | 第三方类型才包装 |

判断句式：**「如果删掉这层，会发生什么？」** 答案是「少几个文件」→ 删；是「测试要连真实 DB」或「要支持第二种实现」→ 留。

### 7.2 接口 vs 泛型

| 维度 | 接口 | 泛型 |
|---|---|---|
| 多态时机 | 运行时（动态分发） | 编译期（单态化/字典） |
| 适合 | 行为抽象、异构集合、插件边界 | 容器、算法、类型关系固定 |
| 性能 | 一次间接调用，可能装箱 | 无装箱，可能代码膨胀 |
| 可读性 | 调用方只看方法集 | 约束复杂时骤降 |

规则：**行为用接口，数据用泛型**。`io.Reader` 是行为，`Map[T, U]` 是数据变换。不要用泛型模拟「可插拔实现」，也不要用接口存「任意类型的切片」。

### 7.3 组合 vs 继承

Go 没有继承，只有两种复用：**嵌入（字段与方法提升）** 和 **持有字段（显式转发）**。

| 方式 | 语义 | 风险 |
|---|---|---|
| 嵌入 | 「是一个」的语法错觉，方法集被提升 | 误以为有虚函数分发；导出方法意外泄漏 |
| 持有 + 转发 | 「有一个」，接口边界清晰 | 样板代码略多 |
| 接口注入 | 「依赖一个行为」，可替换 | 接口过多时调用链变长 |

实践：**默认持有**；需要复用一整组方法且确实想暴露时才嵌入；需要多态时永远用接口，不要用嵌入 + 同名方法模拟 override。

### 7.4 一页决策清单

1. 只有一种实现 → 不抽接口。
2. 需要替换 → 抽接口，且定义在**使用方**的包（consumer-defined interface）。
3. 参数多且有默认值 → functional options。
4. 要按名字选实现 → `map[string]func(...)` 注册表，启动时注册。
5. 要叠加正交行为 → 同签名包装（middleware / decorator）。
6. 要一对多通知 → channel + 缓冲 + 非阻塞发送，或回调注册表。
7. 要并发扇出 → `errgroup`，不要自己拼 `WaitGroup` + 错误切片。
8. 要遍历大数据 → `iter.Seq` / range-over-func，不要造 `Iterator` 接口。

设计模式在 Go 里的正确用法，不是把 23 种背下来，而是知道**哪些形状已经被语言吸收**，然后把注意力留给真正难的部分：边界划分、错误语义、并发所有权。模式名只是沟通词汇，不是设计目标。
