# Go 泛型（Generics）由浅入深教程

> Go 1.18（2022.3）正式引入类型参数（Type Parameters），这是 Go 语言十年来最大的语法变革。
> 官方提案：[Type Parameters Proposal](https://go.googlesource.com/proposal/+/refs/heads/master/design/43651-type-parameters.md)

---

## 1. 为什么需要泛型（入门）

### 1.1 没有泛型的痛苦

Go 1.18 之前，写一个「返回两数中较大者」的函数，每种类型都要复制一遍：

```go
func MaxInt(a, b int) int { if a > b { return a }; return b }
func MaxInt64(a, b int64) int64 { if a > b { return a }; return b }
func MaxFloat64(a, b float64) float64 { if a > b { return a }; return b }
// ...字符串、uint、自定义类型……
```

另一种「通用」做法是 `interface{}`：

```go
func Max(a, b interface{}) interface{} {
    switch a.(type) { // 运行时断言，分支爆炸
    case int:
        if a.(int) > b.(int) { return a }
    ...
    }
    return b
}
```

两大问题：

| 方案 | 问题 |
|---|---|
| 复制粘贴 | 代码重复、维护成本、漏改 bug |
| `interface{}` | **失去编译期类型检查**（传错类型运行时才炸）、**装箱开销**（值要逃逸到堆）、调用处到处类型断言 |

### 1.2 泛型：编译期的多态

```go
func Max[T cmp.Ordered](a, b T) T {
    if a > b {
        return a
    }
    return b
}

Max(1, 2)          // T 推导为 int
Max(3.14, 2.71)    // T 推导为 float64
Max("a", "b")      // T 推导为 string
```

一次编写，所有支持 `>` 比较的类型都能用，且**编译期即可发现类型错误、零装箱开销**。

### 1.3 语法速览

```go
func Name[T, U any](a T, b U) U   // 类型参数列表放在函数名后
//     ^^^^^^^^^^
//     方括号内声明类型参数，逗号分隔
```

| 术语 | 说明 | 示例 |
|---|---|---|
| 类型参数（type parameter） | 函数/类型声明中的"占位类型" | `T`、`K`、`V` |
| 类型实参（type argument） | 调用时传入的具体类型 | `Max[int](1,2)` 中的 `int` |
| 约束（constraint） | 限定类型参数必须满足的要求 | `any`、`cmp.Ordered` |
| 实例化（instantiation） | 用实参替换形参生成具体版本 | `Max[int]` |

---

## 2. 类型约束（初级）

约束是泛型的核心。它回答：「T 可以是什么类型？能对 T 做什么操作？」

### 2.1 内置约束

```go
any            // = interface{}，任意类型，什么都不能做（只能赋值、传参）
comparable     // 可用 == 和 != 比较的类型（int、string、指针、struct…，不含 slice/map/func）
```

### 2.2 标准库约束

`golang.org/x/exp/constraints`（1.21 起推荐用标准库 `cmp` 替代部分）：

```go
cmp.Ordered               // ~int | ~int64 | ~uint | ~float32 | ~float64 | ~string
constraints.Integer       // 所有整数类型
constraints.Float         // 所有浮点类型
constraints.Complex       // 所有复数类型
constraints.Signed        // 有符号整数
constraints.Unsigned      // 无符号整数
```

### 2.3 自定义约束：接口即约束

Go 泛型最优雅的设计：**约束就是接口**。

```go
// 方法约束：T 必须实现 String() 方法
type Stringer interface {
    String() string
}

func Join[T Stringer](items []T) string {
    var sb strings.Builder
    for _, v := range items {
        sb.WriteString(v.String())
    }
    return sb.String()
}
```

### 2.4 联合类型（union）与底层类型（~）

```go
type Number interface {
    int | int64 | float64   // 联合：T 只能是这三种「确切」类型
}

type MyInt int   // MyInt 的底层类型是 int，但它 ≠ int

type Number2 interface {
    ~int | ~float64        // ~：底层类型是 int 或 float64 的「任何」类型（含 MyInt）
}
```

`~` 的必要性：Go 中 `type MyInt int` 定义了**新类型**，与 `int` 不互通。不用 `~` 的话 `Number` 不接受 `MyInt`。

### 2.5 混合约束：类型集合 + 方法

```go
// T 必须是整数类型，且实现 fmt.Stringer
type StringableInt interface {
    ~int | ~int64 | ~int32
    String() string
}
```

约束本质是**类型集合（type set）**：约束代表"所有满足条件的类型组成的集合"，类型实参必须落在这个集合里。

---

## 3. 泛型类型与方法（初级 → 中级）

### 3.1 泛型类型（容器）

```go
type Stack[T any] struct {
    items []T
}

func (s *Stack[T]) Push(v T) {          // 注意：方法的 receiver 必须带 [T]
    s.items = append(s.items, v)
}

func (s *Stack[T]) Pop() (T, bool) {
    if len(s.items) == 0 {
        var zero T                       // var 声明得到 T 的零值
        return zero, false
    }
    n := len(s.items) - 1
    v := s.items[n]
    s.items = s.items[:n]
    return v, true
}

// 使用
s := &Stack[int]{}
s.Push(1)
v, ok := s.Pop()   // v=1, ok=true
```

### 3.2 泛型 map / 函数类型 / 结构体多参数

```go
type Map[K comparable, V any] map[K]V    // map 的 key 必须可比较

type Transformer[T, U any] func(T) U     // 泛型函数类型

type Pair[K comparable, V any] struct {  // 多类型参数
    Key K
    Val V
}
```

### 3.3 零值技巧

泛型代码里没有 `T{}`、`nil`（不确定类型时），获取零值的标准姿势：

```go
var zero T      // 方式一：var 声明
zero := *new(T) // 方式二：new + 解引用
```

### 3.4 类型推导（type inference）

```go
func Map[T, U any](xs []T, f func(T) U) []U

xs := []int{1, 2, 3}
ys := Map(xs, func(i int) string { return "" })  // T=int, U=string 全部自动推导
zs := Map[int, string](xs, f)                    // 显式指定也可以
```

推导失败时（约束含联合、无参数可依据等）需要显式写出类型实参。

---

## 4. 进阶技巧（中级）

### 4.1 指针约束：接收者方法调用的关键坑

**泛型代码中，`T` 不会自动拥有 `*T` 的方法，反之亦然**：

```go
type Speaker interface{ Speak() string }
type Cat struct{}
func (c Cat) Speak() string   { return "meow" }   // 值接收者
func (c *Cat) Speak2() string { return "meow" }   // 指针接收者

func MakeSound[T Speaker](t T) string { return t.Speak() }

MakeSound(Cat{})       // ✅ Cat 实现了 Speaker（值方法集）
MakeSound(&Cat{})      // ✅ *Cat 的方法集包含值方法
// 但若 Speaker 要求 Speak2（指针接收者），则 MakeSound(Cat{}) 编译失败
```

经验法则：**约束里声明 `T`，就传 `T`；声明 `[T Speaker]` 并想用指针方法时，让调用方传 `*T`**。

### 4.2 约束里引用自身 / 递归约束

经典的 `Clone` 问题——让方法返回"与接收者完全相同的类型"：

```go
type Clonable[T any] interface {
    Clone() T          // 约束引用类型参数自身
}

func CloneAll[T Clonable[T]](xs []T) []T {
    out := make([]T, len(xs))
    for i, x := range xs {
        out[i] = x.Clone()
    }
    return out
}
```

`Clonable[T]` 中出现 `T`，编译器要求实参满足 `Clone() X` 且返回类型 X 与自身一致。

### 4.3 泛型与接口配合：给接口"加方法"

```go
// 传统做法：类型断言满天飞
func Process(v any) {
    if s, ok := v.(fmt.Stringer); ok { ... }
}

// 泛型做法：编译期绑定
func Process[T any, PT interface{ *T; String() string }](v PT) {
    // PT 约束：既是 *T，又有 String 方法 —— 声明的类型必须用指针实现 String
}
```

`interface{ *T; ... }` 中嵌入 `*T` 是 1.18+ 特殊支持（"constraint with type in it"），专门用于把值和其指针类型关联起来。

### 4.4 泛型 goroutine 工具与 channel

```go
func FanIn[T any](chans ...<-chan T) <-chan T {
    out := make(chan T)
    var wg sync.WaitGroup
    wg.Add(len(chans))
    for _, ch := range chans {
        go func(c <-chan T) {
            defer wg.Done()
            for v := range c {
                out <- v
            }
        }(ch)
    }
    go func() { wg.Wait(); close(out) }()
    return out
}
```

### 4.5 用泛型实现类型安全的「可选值」

```go
type Option[T any] struct {
    val   T
    valid bool
}

func Some[T any](v T) Option[T] { return Option[T]{v, true} }
func None[T any]() Option[T]    { return Option[T]{} }

func (o Option[T]) UnwrapOr(fallback T) T {
    if o.valid { return o.val }
    return fallback
}

name := Some("alice").UnwrapOr("anonymous")
```

---

## 5. 实现原理：GC Shape Stenciling（高级）

Go 没有采用 C++ 的全量单态化（为每个实参生成独立机器码），也没有采用 Java 式的完全擦除，而是**混合方案**：

1. **同 GC shape 共享代码**：所有指针类型共享一份实例（指针的内存布局相同，都是 8 字节）；`int`/`uint`/`float64` 等每种形状各一份；
2. **字典（dictionary）传递差异**：共享代码中，类型相关的操作（大小、判等、方法调用）通过编译期生成的**字典**间接完成，字典作为隐藏参数传入。

```
Max[int]  ─┐
Max[MyInt]─┴─► 同一形状 → 共享一份机器码 + 字典A
Max[string]──► 不同形状 → 另一份机器码 + 字典B
Max[*User]───► 指针形状 → 与所有指针共享 + 字典C
```

推论（重要）：

- 泛型函数里**两个不同名字的类型参数 `T`、`U` 即使形状相同也不能互转**（编译器视角语义不同）；
- 指针/接口形状的操作走字典，比单态化代码**略慢**（多一层间接寻址），但代码体积不膨胀；
- 数值类型场景下差距通常可忽略。

### 何时该用接口、何时该用泛型？

| 维度 | 接口（多态） | 泛型 |
|---|---|---|
| 绑定时机 | 运行时 | 编译期 |
| 异构集合（[]any 混装） | ✅ | ❌ |
| 同构算法（对 []T 统一处理） | 一般 | ✅ 性能与类型安全双优 |
| 需要存储/传递"未知类型" | ✅ | ❌ |
| 算子类需求（T op T） | ❌ 接口无法表达 | ✅ 约束直接表达 |

---

## 6. 限制与常见坑（高级）

1. **方法不能有额外的类型参数**：
   ```go
   func (s Stack[T]) Map[U any](f func(T) U) []U  // ❌ 编译错误
   ```
   变通：写成顶层泛型函数 `func MapStack[T, U any](s Stack[T], f func(T) U) []U`。提案 #49085 讨论多年仍未放开。

2. **泛型类型不能做类型断言/类型 switch 的 case**（只能对 `any` 断言）：
   ```go
   switch v := x.(type) {   // x 必须是接口类型
   case T:                  // ❌ T 不是具体类型
   }
   ```

3. **约束里不能引用未定义的类型参数**、联合里不能带方法（带方法的类型必须单独一行）。

4. **不要为了泛型而泛型**：只用一次的类型、`any` 约束 + 内部各种断言的写法，不如直接 `any` 或具体类型。泛型的前置条件是**算法与类型无关**。

5. **零值接口陷阱**：`var zero T` 当 T 被实例化为接口类型时 zero 是 nil，判断空要用类型信息而非简单比较。

6. **`comparable` 的边界**：可比较 ≠ 可排序。slice/map/func 不满足 comparable（运行时 panic 的可比较字段边界，1.20 后才放宽为"普通可比较"）。

---

## 7. 综合实战：类型安全的通用缓存（高级）

```go
package cache

import (
    "sync"
    "time"
)

type entry[V any] struct {
    val       V
    expiresAt time.Time
}

// Cache 是一个并发安全、带 TTL、key/value 类型在编译期绑定的内存缓存
type Cache[K comparable, V any] struct {
    mu       sync.RWMutex
    data     map[K]entry[V]
    ttl      time.Duration
    loader   func(K) (V, error) // 可选的回源函数
}

func New[K comparable, V any](ttl time.Duration, loader func(K) (V, error)) *Cache[K, V] {
    return &Cache[K, V]{
        data:   make(map[K]entry[V]),
        ttl:    ttl,
        loader: loader,
    }
}

func (c *Cache[K, V]) Get(k K) (V, bool) {
    c.mu.RLock()
    e, ok := c.data[k]
    c.mu.RUnlock()
    if !ok || time.Now().After(e.expiresAt) {
        var zero V
        return zero, false
    }
    return e.val, true
}

func (c *Cache[K, V]) GetOrLoad(k K) (V, error) {
    if v, ok := c.Get(k); ok {
        return v, nil
    }
    v, err := c.loader(k)
    if err != nil {
        var zero V
        return zero, err
    }
    c.Set(k, v)
    return v, nil
}

func (c *Cache[K, V]) Set(k K, v V) {
    c.mu.Lock()
    c.data[k] = entry[V]{v, time.Now().Add(c.ttl)}
    c.mu.Unlock()
}
```

要点：`K comparable` 保证能做 map 键、`V any` 任意值类型、`var zero V` 处理失败路径、方法 receiver 带 `[K, V]`。对比 1.18 之前的 `map[interface{}]interface{}` 版本：调用处零断言、编译期防错。

## 8. 学习路线建议

1. **第 1 周**：掌握 `[T any]` 语法、`comparable`/`cmp.Ordered`，重写自己项目里的 2~3 个工具函数；
2. **第 2 周**：写泛型容器（Stack/Queue/Set），理解 receiver 的 `[T]`；
3. **第 3 周**：阅读 `golang.org/x/exp/constraints` 与 `slices`/`maps` 标准库源码；
4. **第 4 周**：研究递归约束（Clonable）、指针约束、GC shape 原理，阅读提案文档。

参考：[Go 官方泛型教程](https://go.dev/doc/tutorial/generics)、[Type Parameters Proposal](https://go.googlesource.com/proposal/+/refs/heads/master/design/43651-type-parameters.md)、标准库 `slices`/`maps` 源码。
