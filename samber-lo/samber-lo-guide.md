# samber/lo 技术介绍与使用指南

> 基于 Go 1.18+ 泛型的 Lodash 风格工具库，让 slice / map / channel / string / function 的日常操作一行搞定。
> 仓库：<https://github.com/samber/lo>　|　文档：<https://lo.samber.dev>　|　主线版本：v1（严格 SemVer）

---

## 1. 库简介

`samber/lo` 是一个**纯泛型工具函数库**，灵感来自 JavaScript 生态的 Lodash。它不引入任何框架概念，只做一件事：把 Go 里那些"写了几百遍的 for 循环样板代码"封装成一个个可复用的泛型函数。

```go
// 没有 lo 的日子
names := make([]string, 0, len(users))
for _, u := range users {
    if u.Age >= 18 {
        names = append(names, u.Name)
    }
}

// 有 lo 的日子
names := lo.Map(lo.Filter(users, func(u User, _ int) bool {
    return u.Age >= 18
}), func(u User, _ int) string {
    return u.Name
})
```

核心特征：

- **零第三方依赖**：只依赖 Go 标准库；
- **Go 1.18+ 泛型**：一套函数适配所有类型，无反射、无 `interface{}` 装箱（性能好于 reflect 方案）；
- **函数式风格**：Map / Filter / Reduce / GroupBy 等经典高阶函数；
- **API 海量且按域分组**：slice、map、string、tuple、channel、并发、错误处理、类型操作……200+ 帮助函数；
- 与标准库 `slices` / `maps` 有少量重叠（作者认为其余抽象仍有独特价值）。

### 安装与导入

```bash
go get github.com/samber/lo@v1
```

```go
import (
    "github.com/samber/lo"
    lop "github.com/samber/lo/parallel" // 并行版本
    lom "github.com/samber/lo/mutable"  // 原地修改版本
)
```

同系列生态：`samber/mo`（Monad：Option/Result/Either）、`samber/do`（依赖注入）、`samber/ro`（响应式/无限流）。定位区分：**lo 处理有限集合的同步计算，ro 处理无限事件流**。

---

## 2. 设计原理

### 2.1 泛型签名：一套代码适配所有类型

以 `Filter` 为例的典型签名：

```go
func Filter[T any, R any](collection []T, predicate func(item T, index int) bool) []T
```

- `T any` / `R any`：任意类型参数，编译期单态化，无运行时开销；
- 回调统一携带 `(item, index)` 两个参数，与 JS 的 `map((v, i) => ...)` 对齐；
- 返回**新切片**，不修改入参（`mutable` 子包才提供原地版本）。

### 2.2 Err 变体：错误传播的函数式方案

大部分转换函数都有 `XxxErr` 兄弟，回调多返回一个 `error`，遇到错误立即短路返回：

```go
result, err := lo.MapErr([]int{1, 2, 3}, func(x int, _ int) (string, error) {
    if x == 3 {
        return "", fmt.Errorf("number 3 is not allowed")
    }
    return strconv.Itoa(x), nil
})
// result == nil, err == "number 3 is not allowed"
```

这把"循环里逐个 if err != nil"的模板也省掉了。

### 2.3 子包矩阵

| 导入 | 定位 | 说明 |
|---|---|---|
| `lo` | 同步、不可变 | 主包，返回新集合 |
| `lo/parallel` | 并发版 Map/ForEach | 每个元素一个 goroutine，结果顺序保持 |
| `lo/mutable` | 原地修改 | 复用底层数组，减少分配（注意副作用） |
| `lo/it` | 迭代器实验包 | 基于 Go 1.23 `iter.Seq` 的惰性版本（`exp` 精神） |

### 2.4 性能画像

- 所有函数都是**急切求值（eager）**：调用即完整遍历，中间会产生临时切片。链式 `Map(Filter(xs))` 会遍历两次、分配两次；
- 极热路径上手动 for 循环仍最快（省去回调开销），但绝大多数业务代码的收益是**可读性与正确性**；
- `mutable` 子包原地更新可省一次分配；`parallel` 子包适合单元素处理耗时（I/O 密集）的场景。

---

## 3. 功能分类详解（代码实战）

### 3.1 Slice（切片）——最常用的一组

```go
// Map：类型转换
lo.Map([]int64{1, 2, 3, 4}, func(x int64, _ int) string {
    return strconv.FormatInt(x, 10)
}) // []string{"1", "2", "3", "4"}

// Filter：过滤偶数
lo.Filter([]int{1, 2, 3, 4}, func(x int, _ int) bool { return x%2 == 0 })
// []int{2, 4}

// FilterMap：过滤 + 转换一步到位（回调返回 (结果, 是否保留)）
lo.FilterMap([]string{"cpu", "gpu", "mouse"}, func(x string, _ int) (string, bool) {
    if strings.HasSuffix(x, "pu") {
        return "xpu", true
    }
    return "", false
}) // []string{"xpu", "xpu"}

// FlatMap：转换后拍平
lo.FlatMap([]int64{0, 1, 2}, func(x int64, _ int) []string {
    return []string{strconv.FormatInt(x, 10), strconv.FormatInt(x, 10)}
}) // []string{"0","0","1","1","2","2"}

// Reduce：聚合
lo.Reduce([]int{1, 2, 3, 4}, func(agg, item int, _ int) int { return agg + item }, 0) // 10

// ReduceRight：从右向左聚合
lo.ReduceRight([][]int{{0, 1}, {2, 3}}, func(agg, item []int, _ int) []int {
    return append(agg, item...)
}, []int{}) // []int{2, 3, 0, 1}
```

**去重与分组**：

```go
// Uniq / UniqBy / IsUniq
lo.Uniq([]int{1, 2, 2, 3})                       // []int{1, 2, 3}
lo.UniqMap(users, func(u User, _ int) string { return u.Name })
// Map + 去重一步完成：[]string{"Alex", "Bob", "Alice"}

// GroupBy：按 key 分桶，返回 map[K][]T
lo.GroupBy([]int{0, 1, 2, 3, 4, 5}, func(item int, _ int) int {
    return item % 3
}) // map[int][]int{0: [0,3], 1: [1,4], 2: [2,5]}

// Chunk：切分固定大小子块（常用于分批处理）
lo.Chunk([]int{0, 1, 2, 3, 4, 5}, 2) // [][]int{{0,1}, {2,3}, {4,5}}

// PartitionBy：按连续 key 切段
lo.PartitionBy([]int{1, 1, 2, 2, 3}, func(x int, _ int) string {
    if x < 2 { return "low" } else { return "high" }
}) // [][]int{{1,1}, {2,2,3}}

// Flatten / Concat
lo.Flatten([][]int{{0, 1}, {2, 3, 4}}) // []int{0,1,2,3,4}
lo.Concat([]int{1, 2}, []int{3}, []int{4}) // []int{1,2,3,4}

// KeyBy / SliceToMap(Associate)：切片转 map
lo.SliceToMap(users, func(u User, _ int) (string, int) {
    return u.Name, u.Age
}) // map[string]int{"Alex": 10, ...}
```

**取舍与统计**：

```go
lo.Take([]int{0, 1, 2, 3}, 2)         // []int{0, 1}   取前 n
lo.Drop([]int{0, 1, 2, 3}, 2)         // []int{2, 3}   丢前 n
lo.DropRight([]int{0, 1, 2, 3}, 2)    // []int{0, 1}
lo.TakeWhile / lo.DropWhile           // 按谓词取/丢
lo.Reject(xs, pred)                   // Filter 的反义
lo.Count(xs, 1)                       // 元素出现次数
lo.CountValues([]string{"a", "b", "a"}) // map[string]int{"a":2, "b":1}
lo.Compact([]string{"a", "", "b"})    // 去零值 → []string{"a", "b"}
lo.Reverse([]int{1, 2, 3})            // 注意：v1 新版 Reverse 不再修改原切片
lo.Shuffle([]int{0, 1, 2, 3})         // 随机打乱（返回新切片）
lo.Fill([]bool{false, false}, true)   // 填充
lo.Repeat(5, "a")                     // 重复 n 份
lo.Subset([]int{0, 1, 2, 3}, 1, 2)    // 区间取子集
lo.Replace([]int{1, 2, 1}, 1, 0, 1)   // 替换前 n 个匹配；ReplaceAll 全换
```

### 3.2 Map（字典）

```go
lo.Keys(m) / lo.Values(m)             // 键/值切片
lo.PickBy(m, func(k string, v int) bool { return v > 10 }) // 保留满足项
lo.OmitBy(m, pred)                    // PickBy 反义（剔除）
lo.PickByKeys / lo.OmitByKeys         // 按键名集合筛
lo.Entries(map[string]int{"a": 1})    // []lo.Tuple2[string,int]{{"a",1}}
lo.FromEntries([]lo.Tuple2[string,int]{...}) // 逆向：pairs → map
lo.Invert(m)                          // 键值互换
lo.Assign(m1, m2, m3)                 // 合并（后者覆盖前者）
lo.MapKeys(m, fn) / lo.MapValues(m, fn) // 变换键/值
lo.MapEntries(m, fn)                  // 同时变换键值
lo.MapToSlice(m, fn)                  // map → slice
lo.ValueOr(m, "k", 42)                // 带默认值取值（等价 m["k"] 不存在则 42）
```

### 3.3 查找与集合运算

```go
lo.IndexOf([]int{5, 6, 7}, 6)         // 1（不存在返回 -1）
lo.Find(xs, pred)                     // 返回 (值, 是否找到)
lo.FindOrElse(xs, fallback, pred)     // 找不到给默认值
lo.FindKey(m, 20)                     // 按值反查键
lo.Contains(xs, 5)                    // 包含
lo.Every / lo.Some / lo.None          // 全部/任一/都不满足（均有 By 变体）
lo.Min / lo.Max / lo.MinBy / lo.MaxBy // 极值（Ordered 约束或自定义比较）
lo.First / lo.Last / lo.Nth(xs, -1)   // 首尾与负索引
lo.FirstOr(xs, def)                   // 空集合安全取首
lo.Intersect(a, b) / lo.Difference(a, b) / lo.Union(a, b) // 交/差/并
lo.Without(xs, 2, 3)                  // 剔除指定元素
lo.FindUniques / lo.FindDuplicates    // 找只出现一次的 / 找重复的
lo.Sample(xs) / lo.Samples(xs, 3)     // 随机取样
```

### 3.4 Tuple（多元组）：多返回值的容器化

```go
// Zip：把多个等长切片"拉链"成元组切片
lo.Zip2([]string{"a", "b"}, []int{1, 2})
// []lo.Tuple2[string,int]{{A:"a",B:1},{A:"b",B:2}}，最高支持 Zip9

// Unpack：元组解包成多返回值
a, b := lo.Unpack2(lo.Tuple2[string,int]{A: "x", B: 1})

// CrossJoin：笛卡尔积
lo.CrossJoin2([]string{"a"}, []int{1, 2})
// []lo.Tuple2[string,int]{{"a",1},{"a",2}}
```

### 3.5 条件表达式：让 Go 拥有"三元运算符"

```go
// Ternary：真·三元（注意两分支都会求值）
lo.Ternary(true, "yes", "no") // "yes"

// TernaryF：惰性求值版本（仅计算命中分支）
lo.TernaryF(ok, func() T { return expensive() }, func() T { return cheap() })

// 链式 if
lo.If(ok, "a").ElseIf(ok2, "b").Else("c")

// 链式 switch
lo.Switch(value).
    Case(1, "one").
    Case(2, "two").
    Default("many")
```

### 3.6 类型与指针操作：消灭 nil 判空样板

```go
lo.ToPtr(42)             // *int，字面量直接取地址
lo.FromPtr(p)            // 解引用；nil → 零值
lo.FromPtrOr(p, 99)      // 解引用；nil → 默认值
lo.ToSlicePtr([]int{1,2})  // []*int
lo.FromSlicePtr(ps)        // []T（遇 nil 元素报 panic）；FromSlicePtrOr 带默认
lo.IsEmpty / lo.IsNotEmpty // 零值判断
lo.Coalesce(0, 1, 2)       // 返回第一个非零值 → 1（默认值串联神器）
lo.CoalesceOrEmpty(...)    // 全为零值时返回零值
lo.IsNil / lo.IsNotNil     // 任意类型的 nil 判断（反射实现）
```

### 3.7 错误处理与断言

```go
// Validate：条件不满足即返回错误（服务端参数校验常用）
err := lo.Validate(user.Age >= 18, "user must be adult, got %d", user.Age)

// Must："我保证不出错"，出错 panic（初始化期使用）
val := lo.Must(strconv.Atoi("42"))

// Try / TryCatch：函数式 recover
err := lo.Try(func() error { return doSomething() })
err, val := lo.TryWithValue(...)
lo.TryCatch(fn, catchFunc)

// ErrorsAs：一行完成 errors.As
if lo.ErrorsAs[*MyError](err) { ... }

// Assert / Assertf：断言（内部用 panic）
lo.Assert(condition, "message %d", 42)
```

### 3.8 并发控制

```go
// Attempt：带重试的执行（返回最终错误）
err := lo.Attempt(3, func(index int) error {
    return callFlakyAPI()
})

// AttemptWithDelay：带退避延迟的重试
err := lo.AttemptWithDelay(5, 2*time.Second, func(i int, d time.Duration) error {
    return retryableOp()
})

// Debounce / Throttle：防抖与节流
debounced := lo.Debounce(200*time.Millisecond, func() { search() })
debounced() // 200ms 内多次调用只执行最后一次

throttled := lo.Throttle(1*time.Second, fn)
throttled() // 1 秒内最多执行一次，其余被丢弃

// Synchronize：把任意函数变成并发安全版本
safeFn := lo.Synchronize(fn)

// Async：立即起 goroutine 执行，返回 channel 收结果
ch := lo.Async(func() int { return heavyCompute() })
result := <-ch
// 还有 Async0~Async6 支持多参数

// WaitFor：轮询等待条件成立
err := lo.WaitFor(func() bool { return ready() }, 3*time.Second, 100*time.Millisecond)

// Transaction：把多个函数打包成事务式顺序执行（遇错停止）
err := lo.Transaction(fn1, fn2, fn3)
```

### 3.9 Channel 工具箱

```go
// Generator：把切片生产为 channel（带缓冲）
ch := lo.Generator(10, []int{1, 2, 3})

// SliceToChannel / ChannelToSlice：切片⇄channel 互转
ch := lo.SliceToChannel(10, []int{1, 2, 3})
// for v := range ch { ... }
xs := lo.ChannelToSlice(ch) // []int{1, 2, 3}

// Buffer / BufferWithContext：攒批消费
// 每 10 个元素或超时 1s 输出一批
for batch := range lo.BufferWithTimeout(ctx, ch, 10, time.Second) { ... }

// FanIn：多 channel 合一
merged := lo.FanIn(ch1, ch2, ch3)

// FanOut：一 channel 派发到多 channel（负载扇出）
chs := lo.FanOut(ch, 5)

// ChannelDispatcher：按策略（轮询/最少连接/哈希）把输入分发到多个输出 channel
```

### 3.10 数学与字符串

```go
// 数学
lo.Range(5)                 // []int{0,1,2,3,4}
lo.RangeFrom(1, 5)          // []int{1..5}
lo.RangeWithSteps(0, 10, 3) // []int{0,3,6,9}
lo.Clamp(42, 0, 100)        // 区间钳制
lo.Sum(xs) / lo.SumBy(xs, fn)
lo.Product / lo.Mean / lo.MeanBy
lo.Mode([]int{1, 2, 2, 3})  // 众数 → 2

// 字符串
lo.RandomString(16, lo.LowerCaseLettersCharset) // 随机串（可定字符集）
lo.Substring("hello", 0, 3)      // "hel"（支持负索引）
lo.ChunkString("abcdefgh", 3)    // []string{"abc","def","gh"}
lo.RuneLength("你好")            // 2（按 rune，非 byte）
lo.Capitalize("hello")           // "Hello"
lo.Ellipsis("abcdef", 3)         // "abc..."（截断加省略号）

// 命名风格转换
lo.PascalCase("hello_world") // "HelloWorld"
lo.CamelCase("http_server")  // "httpServer"
lo.KebabCase("HelloWorld")   // "hello-world"
lo.SnakeCase("MyFunc")       // "my_func"
lo.Words("camelCaseString")  // []string{"camel","Case","String"}
```

### 3.11 parallel 子包：一行并发

```go
import lop "github.com/samber/lo/parallel"

// 每个元素一个 goroutine 并行处理，结果顺序与输入一致
urls := []string{"https://a.com", "https://b.com", "https://c.com"}
bodies := lop.Map(urls, func(url string, _ int) string {
    resp, _ := http.Get(url)
    defer resp.Body.Close()
    b, _ := io.ReadAll(resp.Body)
    return string(b)
})

lop.ForEach(xs, func(x T, _ int) { process(x) }) // 并行副作用
```

实现原理：为每个索引起 goroutine，结果按下标写入预分配切片——**保序但非惰性**，适合 I/O 密集而非 CPU 密集（Go GOMAXPROCS 限制下纯计算并行收益有限）。

### 3.12 mutable 子包：原地修改

```go
import lom "github.com/samber/lo/mutable"

list := []int{1, 2, 3, 4}
newList := lom.Filter(list, func(x int) bool { return x%2 == 0 })
// list 底层数组被改写（内容不再可信），newList == []int{2, 4}
// 省一次内存分配，代价是共享底层数组的副作用
```

---

## 4. 与标准库 slices/maps 的关系

Go 1.21 起 `slices` / `maps` 进入标准库，功能有交集：

| 能力 | 标准库 | samber/lo |
|---|---|---|
| Contains / Index / Max / Min | `slices` | lo 均有，另有 By/Err 变体 |
| Clone / Sort / BinarySearch | `slices` | lo 不做排序（保持不可变定位） |
| Map / Filter / Reduce | 无 | **lo 核心优势** |
| GroupBy / Chunk / KeyBy | 无 | lo 独有 |
| Ternary / Coalesce / Attempt / Channel 系列 | 无 | lo 独有 |

结论：排序查找优先标准库（编译器深度优化）；转换、分组、并发、错误处理类样板代码用 lo。两者可在同一项目共存。

---

## 5. 最佳实践与注意事项

1. **不要在超热路径滥用链式调用**：`Map(Filter(Map(xs)))` 会多次遍历+分配，可用 `FilterMap` 合并，或回到手写 for；
2. **警惕 eager 求值**：所有函数立即执行完毕，不存在惰性序列；需要惰性看 `lo/it` 或标准库 `iter`；
3. **`lo.Reverse` 等函数历史上曾原地修改**（v1.47 后改为返回新切片），升级版本前查 changelog，避免共享底层数组的坑；
4. **`lo.Must` 只用于初始化期**（配置解析、正则编译等"出错即致命"的场景），业务路径禁用；
5. **`Ternary` 两分支都会求值**：含副作用或高成本时改用 `TernaryF`；
6. **`parallel.Map` 注意回调安全性**：回调会被并发调用，闭包里的共享变量需自行加锁；
7. **团队约定 import 别名**：`lop`/`lom` 与主包区分清楚，避免误用原地版本引发数据竞争；
8. **可读性优先**：lo 的价值是让代码意图（"我在做映射/过滤/分组"）一目了然，而不是炫技——三层以上的嵌套回调不如拆成命名函数。

---

## 6. 选型小结

| 场景 | 建议 |
|---|---|
| 业务代码里的切片/字典转换、分组、去重 | 强烈推荐 lo |
| 参数校验、重试、防抖节流 | lo.Validate / Attempt / Debounce 一行解决 |
| 需要 channel 扇入扇出、攒批 | lo 的 channel 工具箱 |
| 纯计算超大规模数据、排序 | 标准库 slices 或手写 |
| 无限流/响应式 | 换 samber/ro |
| Maybe/Result 等 Monad | 换 samber/mo |
