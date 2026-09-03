# Go 反射（Reflection）由浅入深教程

> 反射是 Go 在运行时「审视类型结构与值」的能力，是 `encoding/json`、`fmt`、ORM、依赖注入框架的基石。
> 官方博客：[The Laws of Reflection](https://go.dev/blog/laws-of-reflection)（2011，Rob Pike）

---

## 1. 什么是反射（入门）

### 1.1 从一个日常现象说起

你一定写过这样的代码，却没意识到背后是反射在工作：

```go
type User struct {
    Name string `json:"name"`
    Age  int    `json:"age"`
}

b, _ := json.Marshal(User{Name: "alice", Age: 20})
// 输出：{"name":"alice","age":20}
```

`json.Marshal` 如何知道结构体有哪些字段、字段叫什么名字、tag 是什么？——**答案是在运行时用反射解析了这个结构体**。

反射解决的根本问题：**编译期不知道类型长什么样，运行时才需要知道**。典型场景：

- 序列化/反序列化（json、yaml、protobuf）
- ORM 字段映射（字段名 → 列名）
- 依赖注入（按类型装配）
- `fmt.Println` 万物皆可打印
- 框架做「约定优于配置」

### 1.2 反射的入口：interface 的隐藏结构

Go 的 interface 值在运行时是一个两个字的结构：

```
interface{} = (动态类型 type, 动态值 value)
```

反射就是把这个隐藏结构"掏出来看"的能力。Go 把它包装成两个类型：

- `reflect.Type`：关于**类型**的元信息（叫什么、什么种类、有哪些字段方法）
- `reflect.Value`：关于**值**的运行时视图（可读、部分可写）

---

## 2. 反射三定律（入门 → 初级）

Rob Pike 在官方博客总结的三条定律，是理解反射的纲领：

| 定律 | 内容 | 对应 API |
|---|---|---|
| 1 | 反射从 interface 值转换为反射对象 | `reflect.TypeOf(i)`, `reflect.ValueOf(i)` |
| 2 | 反射从反射对象转换回 interface 值 | `v.Interface()` |
| 3 | 要修改反射对象，其值必须**可设置**（settable） | `reflect.ValueOf(&x).Elem().Set(...)` |

### 2.1 第一定律：interface → 反射对象

```go
var x float64 = 3.4

t := reflect.TypeOf(x)   // float64 —— 携带类型元信息
v := reflect.ValueOf(x)  // 3.4   —— 携带值的视图

fmt.Println(t.Kind())    // float64（Kind 是"种类"，见 3.1）
fmt.Println(v.Float())   // 3.4
```

注意：`TypeOf`/`ValueOf` 的参数是 `interface{}`——**传进去的那一刻就发生了装箱**，这也是反射性能成本的第一来源。

### 2.2 第二定律：反射对象 → interface

```go
v := reflect.ValueOf(3.4)
y := v.Interface().(float64)   // 类型断言还原
fmt.Println(y)                 // 3.4
```

`v.Interface()` 拿回 `interface{}`，再断言成具体类型。一来一回，正好构成闭环。

### 2.3 第三定律：可设置性（最常见的坑）

```go
var x float64 = 3.4
v := reflect.ValueOf(x)
v.SetFloat(7.1)   // ❌ panic: reflect.Value.SetFloat using unaddressable value
```

原因：`ValueOf(x)` 拷贝了 x 的**副本**，改副本没有意义，所以 Go 直接禁止。正确姿势——**传指针，再 Elem() 取指针对象**：

```go
v := reflect.ValueOf(&x).Elem()  // 解引用，得到"指向 x 的可设值视图"
fmt.Println(v.CanSet())          // true
v.SetFloat(7.1)                  // ✅ x 变为 7.1
```

口诀：**想通过反射改值，必须传地址**。`CanSet()` 可预先检查。

---

## 3. reflect.Type：类型的显微镜（初级）

### 3.1 Type 与 Kind 的区别

```go
type MyInt int
type User struct{ Name string }

reflect.TypeOf(MyInt(1)).Name()  // "MyInt" —— Type：声明的名字
reflect.TypeOf(MyInt(1)).Kind()  // reflect.Int —— Kind：底层种类

reflect.TypeOf(User{}).Name()    // "User"
reflect.TypeOf(User{}).Kind()    // reflect.Struct
```

`Kind` 是一个枚举（`reflect.Int`、`reflect.String`、`reflect.Slice`、`reflect.Struct`、`reflect.Ptr`、`reflect.Map`、`reflect.Func`、`reflect.Chan`、`reflect.Interface`…），反射分支逻辑几乎总基于 Kind 而非 Type。

### 3.2 常用 Type 方法速查

| 方法 | 作用 |
|---|---|
| `Name()` / `String()` | 类型名 |
| `Kind()` | 种类枚举 |
| `NumField()` / `Field(i)` | 结构体字段（仅 Struct） |
| `NumMethod()` / `Method(i)` | 方法列表 |
| `Elem()` | 容器/指针/chan 的元素类型 |
| `Key()` | map 的 key 类型 |
| `In(i)` / `NumIn()` / `Out(i)` / `NumOut()` | 函数签名 |
| `AssignableTo(u)` / `ConvertibleTo(u)` / `Implements(u)` | 类型间关系判断 |
| `PkgPath()` | 所在包路径 |

### 3.3 遍历结构体字段与 Tag

```go
type User struct {
    Name string `json:"name" validate:"required"`
    Age  int    `json:"age" validate:"gte=0,lte=150"`
}

u := User{Name: "alice"}
t := reflect.TypeOf(u)
for i := 0; i < t.NumField(); i++ {
    f := t.Field(i)
    fmt.Printf("字段:%-6s 类型:%-8s json tag:%-10s validate:%s\n",
        f.Name, f.Type, f.Tag.Get("json"), f.Tag.Get("validate"))
}
// 字段:Name   类型:string  json tag:name       validate:required
// 字段:Age    类型:int     json tag:age        validate:gte=0,lte=150
```

`f.Tag.Get(key)` 就是 json 库做字段重命名、validator 库做校验的核心机制。`f.Index`（`[]int`）支持嵌套字段寻址，`f.Anonymous` 标记嵌入字段。

---

## 4. reflect.Value：值的操作台（初级 → 中级）

### 4.1 读取各类值

```go
v := reflect.ValueOf(x)
v.Int() / v.Uint() / v.Float() / v.Bool() / v.String()   // 按 Kind 取
v.Len()            // slice/map/chan/array/string 长度
v.Index(i)         // slice/array 元素 → reflect.Value
v.MapIndex(key)    // map 取值
v.Field(i)         // 结构体字段 → reflect.Value
```

**注意**：`v.Int()` 返回 `int64`，赋回 int 需要 `int(v.Int())`——访问器一律返回最大宽度的类型。

### 4.2 判断空值与有效性

```go
v := reflect.ValueOf(x)
v.IsValid()   // 零值 Value（nil 返回值）为 false
v.IsNil()     // 判断 nil（仅对 chan/func/interface/map/pointer/slice 有意义）
v.IsZero()    // 是否零值（Go 1.13+）
```

`IsValid` 很重要：`Field(i)` 越界、`MapIndex` 未命中等都会返回零值 `Value{}`，直接调用方法会 panic。

### 4.3 创建新值：reflect.New 与 MakeMap/MakeSlice

```go
t := reflect.TypeOf(User{})
pv := reflect.New(t)              // *User，指向新分配的零值 User
u := pv.Elem().Interface().(User) // 取回

m := reflect.MakeMap(reflect.TypeOf(map[string]int{}))
m.SetMapIndex(reflect.ValueOf("k"), reflect.ValueOf(1))
```

`reflect.New(T)` 等价于 `new(T)`，是**在运行时按类型信息分配值**的标准手段（json 反序列化的第一步就是把目标 `reflect.New` 出来）。

---

## 5. 进阶：动态调用与修改（中级）

### 5.1 动态调用函数

```go
func Add(a, b int) int { return a + b }

fv := reflect.ValueOf(Add)
args := []reflect.Value{reflect.ValueOf(1), reflect.ValueOf(2)}
out := fv.Call(args)               // []reflect.Value{3}
fmt.Println(out[0].Int())          // 3
```

变长参数用 `CallSlice`；先用 `fv.Type().NumIn()` 检查参数个数可避免 panic。

### 5.2 动态调用方法

```go
type Greeter struct{ Name string }
func (g *Greeter) Hello(who string) string { return "hi " + who + ", I'm " + g.Name }

g := &Greeter{Name: "alice"}
v := reflect.ValueOf(g)
m := v.MethodByName("Hello")        // 方法也要是"导出"的才能取到
out := m.Call([]reflect.Value{reflect.ValueOf("bob")})
fmt.Println(out[0].String())        // hi bob, I'm alice
```

### 5.3 通过反射修改结构体字段

```go
type Config struct {
    Host string
    Port int
}

func SetField(obj any, name string, value any) error {
    v := reflect.ValueOf(obj).Elem()      // 传指针 + Elem 得到可设视图
    f := v.FieldByName(name)
    if !f.IsValid() || !f.CanSet() {
        return fmt.Errorf("field %s not settable", name)
    }
    fv := reflect.ValueOf(value)
    if fv.Type().AssignableTo(f.Type()) {  // 类型必须可赋值
        f.Set(fv)
        return nil
    }
    return fmt.Errorf("type mismatch: %s -> %s", fv.Type(), f.Type())
}

c := &Config{}
_ = SetField(c, "Host", "localhost")
_ = SetField(c, "Port", 8080)
// c = &{localhost 8080}
```

这个模式是配置热加载、ORM `Update`、依赖注入的通用骨架。

### 5.4 递归处理嵌套结构（DeepCopy 示例）

```go
func DeepCopy(src, dst any) error {
    s := reflect.ValueOf(src).Elem()
    d := reflect.ValueOf(dst).Elem()
    return copyValue(s, d)
}

func copyValue(s, d reflect.Value) error {
    if !d.CanSet() { return nil }
    switch s.Kind() {
    case reflect.Struct:                       // 结构体：逐字段递归
        for i := 0; i < s.NumField(); i++ {
            if err := copyValue(s.Field(i), d.Field(i)); err != nil {
                return err
            }
        }
    case reflect.Slice:                        // 切片：重建后逐元素拷贝
        nd := reflect.MakeSlice(s.Type(), s.Len(), s.Len())
        for i := 0; i < s.Len(); i++ {
            copyValue(s.Index(i), nd.Index(i))
        }
        d.Set(nd)
    default:
        if s.Type().AssignableTo(d.Type()) {
            d.Set(s)
        }
    }
    return nil
}
```

这是反射的典型复杂度形态：**按 Kind 递归分派**。读懂它，就读懂了 json.Marshal 的主干。

---

## 6. 结构体 Tag 深入（中级）

### 6.1 Tag 的结构

```go
`key1:"v1" key2:"v2,opt,opt2=v2.2" key3:"-" json:"omitempty"`
```

- `Get(key)`：取第一个值（无则空串）
- `Lookup(key)`：带 ok 的版本，可区分「没有 tag」和「tag 为空」
- 值内的选项由库自行解析（如 json 的 `omitempty`、`-` 跳过）

### 6.2 反射 + tag 实现 mini json 序列化（实战）

```go
func Marshal(v any) (string, error) {
    rv := reflect.ValueOf(v)
    var sb strings.Builder
    if err := encodeValue(rv, &sb); err != nil {
        return "", err
    }
    return sb.String(), nil
}

func encodeValue(rv reflect.Value, sb *strings.Builder) error {
    switch rv.Kind() {
    case reflect.Struct:
        t := rv.Type()
        sb.WriteByte('{')
        first := true
        for i := 0; i < t.NumField(); i++ {
            f := t.Field(i)
            if f.Tag.Get("json") == "-" {          // "-" 表示跳过
                continue
            }
            if !first {
                sb.WriteByte(',')
            }
            first = false
            name := f.Tag.Get("json")
            if name == "" {
                name = f.Name                       // 无 tag 用字段名
            }
            fmt.Fprintf(sb, "%q:", name)
            if err := encodeValue(rv.Field(i), sb); err != nil {
                return err
            }
        }
        sb.WriteByte('}')
    case reflect.Slice, reflect.Array:
        sb.WriteByte('[')
        for i := 0; i < rv.Len(); i++ {
            if i > 0 { sb.WriteByte(',') }
            encodeValue(rv.Index(i), sb)
        }
        sb.WriteByte(']')
    case reflect.String:
        fmt.Fprintf(sb, "%q", rv.String())
    case reflect.Int, reflect.Int64:
        fmt.Fprintf(sb, "%d", rv.Int())
    case reflect.Bool:
        fmt.Fprintf(sb, "%v", rv.Bool())
    default:
        return fmt.Errorf("unsupported kind: %s", rv.Kind())
    }
    return nil
}
```

约 40 行实现了 `json.Marshal` 的核心骨架（真实标准库还要处理 map、指针、interface、浮点转义、方法 Marshaler…但骨架完全一致）。

---

## 7. 性能与最佳实践（高级）

### 7.1 反射的性能代价

| 成本来源 | 说明 |
|---|---|
| 装箱 | 值 → interface{} 逃逸到堆 |
| 间接访问 | 每次读字段/调方法都要经过 Value 结构 |
| 无法内联 | 动态分发天然阻止编译器优化 |
| 运行时检查 | AssignableTo、CanSet 等校验 |

典型量级：反射字段读取比直接访问慢 **一个数量级以上**（每次几十 ns vs 1~2 ns）。

### 7.2 缓解手段

1. **缓存类型信息**：`reflect.Type`/`Field` 元信息按类型缓存一次，别每帧每请求重算（`encoding/json` 内部就有类型缓存池）；
2. **预生成代码**：极端性能场景用 codegen（easyjson、protobuf 早期方案）替代反射；
3. **泛型替代**：Go 1.18+ 后，很多"对 []T 的操作"用泛型写可获得编译期绑定，零反射成本；
4. **反射只用在边界**：框架边界（序列化、注入）用反射，核心热路径用具体类型。

### 7.3 安全清单

- 修改值：必须 `ValueOf(&x).Elem()`，先查 `CanSet()`；
- 取元素/字段：先查 `IsValid()` 或判 `Kind()`；
- 未导出字段：可读类型信息，`CanSet()` 为 false（除非用 `unsafe`，不推荐）；
- panic 三大来源：Kind 不匹配的方法调用（对 int 调 `String()`）、不可设值的 Set、nil Value 的任何操作——**边界处先检查再操作**。

---

## 8. 反射 vs 泛型：如何选择（高级）

| 维度 | 反射 | 泛型 |
|---|---|---|
| 绑定时机 | 运行时 | 编译期 |
| 类型安全 | 无（运行时才炸） | 完整 |
| 性能 | 慢（装箱+间接） | 快（静态绑定） |
| 能力上限 | **任意**类型结构（字段名、tag、动态调用） | 只能用约束表达的操作 |
| 典型场景 | 序列化、ORM、DI、fmt | 容器、算法、工具函数 |

组合套路：**泛型定骨架 + 反射看内部**。例如泛型缓存 `[K, V]` 编译期绑定类型，内部用反射解析 V 的 tag 实现 ORM 字段映射——两者是互补而非替代。

## 9. 学习路线建议

1. **第 1 步**：跑通三定律示例，理解"传地址才能改"；
2. **第 2 步**：用 `TypeOf` + `Field` 写一个打印任意结构体的调试函数；
3. **第 3 步**：手写 mini-json（本文第 6 节），对照标准库行为找差距；
4. **第 4 步**：阅读 `fmt` 包的 `printValue`、`encoding/json` 的 `encode.go` 源码；
5. **第 5 步**：用反射 + unsafe 挑战一次零拷贝优化（选学）。

参考：[The Laws of Reflection](https://go.dev/blog/laws-of-reflection)、[reflect 包文档](https://pkg.go.dev/reflect)、[Go 语言设计与实现：反射](https://draveness.me/golang/)
