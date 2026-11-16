# Go 测试与可测性技术指南

> 测试不是覆盖率的数字游戏：能稳定复现失败、能安全重构、能在 CI 里快速给出信号，才算可测。
> 标准库：<https://pkg.go.dev/testing> · <https://pkg.go.dev/testing/fuzz>　|　生态：testify v1.x · testcontainers-go v0.4x　|　适用 **Go 1.27.4**（2026-11-10）

---

## 1. 项目简介

Go 的测试能力不是某个框架给的，而是**工具链的一等公民**：`go test` 负责编译、运行、缓存与报告，`testing` 提供生命周期与组织方式，`httptest`、`testing/fuzz`、`-race`、覆盖率各自补齐一类信号。设计取向很明确——测试就是普通 Go 代码，不引入运行时发现，因此测试与业务代码同构，能一起重构、一起调试。

由此得到两个判断：

- **可测性是设计属性**。函数内部直接 `time.Now()`、`rand.Int()`、`http.Get()`，外部只能靠 sleep、真网络去凑；把环境收进接口、在装配层注入，测试才有确定性。
- **分层按"失败能定位到哪里"切**。单元测试回答"决策对不对"，组件测试回答"这层协议/路由/序列化对不对"，集成测试回答"真实依赖的约束与方言对不对"。三层写成同一种东西，只会得到又慢又不准的套件。

```bash
go test ./...                       # 单元测试（默认）
go test -race -shuffle=on ./...     # CI 推荐：竞态 + 顺序随机
go test -tags=integration ./...     # 集成测试（需 Docker）
```

---

## 2. 能力总览

| 能力 | 入口 | 解决的问题 | 成本/边界 |
|---|---|---|---|
| 子测试与生命周期 | `t.Run` / `t.Cleanup` / `t.TempDir` | 用例分组、资源释放、失败定位到 case | 子测试名要唯一可读 |
| 并行 | `t.Parallel` / `-parallel` | 缩短墙钟时间 | 共享状态、`t.Setenv` 会冲突 |
| 表驱动 | `[]struct{...}` + `t.Run` | 同逻辑多组输入，新增 case 只加一行 | 别把所有层塞进一张表 |
| HTTP 测试 | `NewRecorder` / `NewServer` | handler 单测 / 真实客户端联调 | Recorder 不经过真实网络 |
| 断言 | `assert` / `require` | 可读失败信息、JSON/集合比较 | require 失败即终止 |
| SQL 单测 | `go-sqlmock` | 锁 SQL 文本、参数、事务边界 | 不校验方言与约束 |
| 真实依赖 | `testcontainers-go` | PostgreSQL/Redis 真实行为 | 需 Docker，秒级启动 |
| 模糊测试 | `testing.F` + `-fuzz` | 找 panic、越界、不变量破坏 | 只适合纯函数 |
| 竞态检测 | `-race` | 数据竞争、并行测试互相污染 | 2–20 倍减速 |
| 覆盖率 | `-coverprofile` | 找从未执行的分支 | 语句覆盖≠分支覆盖 |

---

## 3. 核心概念与原理

### 3.1 `testing.T` 的生命周期：Run、Cleanup、Helper

执行顺序是：**函数体 → 子测试（含并行子测试）全部结束 → 本层 Cleanup 逆序执行**。`t.Cleanup` 是 LIFO，父测试的 Cleanup 晚于子测试，正好匹配"先建后拆"的资源栈。

```go
func TestOrderService(t *testing.T) {
    t.Cleanup(func() { t.Log("父测试收尾") })   // 最后执行
    t.Run("create", func(t *testing.T) {
        t.Cleanup(func() { t.Log("create 收尾") }) // 先于父 Cleanup
    })
}
```

三个易错语义：`t.Fatal`/`require` 内部的 `FailNow` **只能在测试 goroutine 调用**；`t.Helper()` 把失败行号指向调用者，封装断言必加；`t.Setenv` 与 `t.Parallel` 互斥（环境变量是进程级的，会 panic）。

### 3.2 表驱动：结构与 `t.Run` 命名

表驱动的价值不在少写代码，而在**把差异压成数据**：输入、期望、期望错误三列覆盖多数分支。每个 case 给一个能当测试名用的 `name`，失败时 `go test -run 'TestAtoi/overflow'` 能直接命中。

```go
tests := []struct {
    name, in string
    want     int
    wantErr  bool
}{
    {"plain", "42", 42, false},
    {"space", " 7", 0, true},
}
for _, tt := range tests {
    t.Run(tt.name, func(t *testing.T) { /* ... */ })
}
```

命名建议 `方法_场景_期望`，子测试名只写场景；名字里避免 `/` 与空格，它们会被转成下划线，影响 `-run` 过滤。

### 3.3 `t.Parallel` 与循环变量捕获

`t.Parallel()` 的语义是：**当前测试暂停、交还控制权；父测试串行部分跑完后，所有并行子测试同时恢复**。因此并行子测试之间没有顺序，共享切片、map、全局计数器、环境变量都会变成竞争。

循环变量是另一个坑。Go 1.22 起（且 `go.mod` 的 `go` 指令 ≥ 1.22），每轮迭代创建新变量，闭包捕获当轮副本；**在此之前**所有闭包共享同一个变量，并行子测试恢复时往往已经走到最后一个 case，经典修复是循环体首行 `tt := tt`。判断项目是否真启用新语义看 `go.mod`，而不是 `go version`——模块的 `go` 指令决定语言版本。

```go
for _, tt := range tests {
    tt := tt        // Go 1.22 前必需；1.22+ 冗余但无害
    t.Run(tt.name, func(t *testing.T) {
        t.Parallel()
        _ = tt
    })
}
```

### 3.4 `httptest` 的两种形态与边界

`httptest.NewRecorder()` 在进程内直接调用 `handler.ServeHTTP(w, req)`，不启 socket、不做真实超时与连接管理，**适合 handler/中间件的行为测试**（状态码、JSON、Header、错误分支），快且确定。`httptest.NewServer()` 启动真实 `http.Server` 监听 `127.0.0.1:0`，返回可用 `Client()`，**适合测试客户端代码**（重试、超时、重定向、TLS、连接复用），测完必须 `Close()`。

边界要清楚：Recorder 测不到"服务端读 body 超限""客户端取消请求""HTTP/2""真实 TLS"，这些必须上 `NewServer`/`NewTLSServer`；反过来，用真服务器测纯业务分支会引入端口、时序和 CI 防火墙的不确定性。Gin 场景默认 `gin.SetMode(gin.TestMode)` + Recorder，`c.Request.Context()` 也能正常工作。

### 3.5 测试替身：fake / stub / mock 的分工

| 类型 | 行为 | 适合 | 不适合 |
|---|---|---|---|
| Stub | 返回预置值 | 固定成功/失败分支 | 验证调用过程 |
| Fake | 可工作的轻量实现（内存 map/队列） | repository、缓存、消息总线 | 需要真实事务/约束 |
| Mock | 记录并断言调用 | 你**不拥有**的外部协议边界 | 自己定义的领域接口 |

关键原则：**接口由使用方定义，且尽量小**。handler 只需要 `UserGetter`，就不要注入整个 `UserService`；repository 的 fake 用内存 map 实现，比一堆 `EXPECT().Get().Return()` 更能承受重构。`gomock`/`mockery` 的代价是生成代码进仓库、接口一变就要重生成、期望顺序把实现细节写进测试、`AnyTimes()` 泛滥后断言形同虚设——只该用在第三方 SDK、gRPC client、支付网关这类边界上。

### 3.6 真实依赖：sqlmock、真库与容器的成本模型

三者是不同保真度，不是替代关系：

- `go-sqlmock` 替换 `database/sql/driver`，断言"发出了什么 SQL、传了什么参数、事务是否成对"。它**不解析 SQL、不校验方言、不执行约束**；默认正则匹配器会把期望串当正则，括号、`+`、`.` 都可能匹配过宽，建议 `regexp.QuoteMeta` 或 `QueryMatcherEqual`。
- 本地真库保真度高，但**状态不可控**：上一个测试留下的数据、版本差异、并行分支互相污染，都是 flaky 来源。
- `testcontainers-go` 每个测试进程拉起一次性容器，镜像可钉死、结束自动清理，隔离性最好；代价是 Docker 依赖、镜像拉取、2–5 秒启动与 CI 资源。

结论：**SQL 构造与错误分支用 sqlmock，约束/方言/迁移/并发用容器**。为了"快"把集成测试全改成 sqlmock，只是在测试你对 SQL 的想象。

---

## 4. 使用实战

### 4.1 表驱动 + testify

```go
package parse

import (
    "strconv"
    "testing"

    "github.com/stretchr/testify/assert"
    "github.com/stretchr/testify/require"
)

func TestAtoi(t *testing.T) {
    tests := []struct {
        name, in string
        want     int
        wantErr  bool
    }{
        {"plain", "42", 42, false},
        {"space", " 7", 0, true},
        {"overflow", "99999999999999999999", 0, true},
    }
    for _, tt := range tests {
        t.Run(tt.name, func(t *testing.T) {
            t.Parallel()
            got, err := strconv.Atoi(tt.in)
            if tt.wantErr {
                require.Error(t, err)
                return
            }
            require.NoError(t, err)
            assert.Equal(t, tt.want, got)
        })
    }
}
```

`require` 用于"后续断言已无意义"的前置条件（err 非空、对象非 nil），`assert` 用于可继续收集的字段比较；别在循环里用 require 吞掉后续 case 的信息。

### 4.2 httptest 测 Gin handler

```go
package handler

import (
    "net/http"
    "net/http/httptest"
    "testing"

    "github.com/gin-gonic/gin"
    "github.com/stretchr/testify/require"
)

func TestGetUser(t *testing.T) {
    gin.SetMode(gin.TestMode)
    r := gin.New()
    r.GET("/users/:id", func(c *gin.Context) {
        c.JSON(http.StatusOK, gin.H{"id": c.Param("id"), "name": "alice"})
    })
    w := httptest.NewRecorder()
    r.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/users/7", nil))
    require.Equal(t, http.StatusOK, w.Code)
    require.JSONEq(t, `{"id":"7","name":"alice"}`, w.Body.String())
}
```

真实项目把 handler 的依赖（repository、下游 client）声明成小接口，测试注入手写 stub；`httptest` 只负责把请求送进路由，不改变这层替身策略。

### 4.3 sqlmock 测 repository

```go
package repo

import (
    "context"
    "database/sql"
    "regexp"
    "testing"

    "github.com/DATA-DOG/go-sqlmock"
    "github.com/stretchr/testify/require"
)

type UserRepo struct{ db *sql.DB }

func (r *UserRepo) Name(ctx context.Context, id int64) (string, error) {
    var name string
    err := r.db.QueryRowContext(ctx, `SELECT name FROM users WHERE id = $1`, id).Scan(&name)
    if err == sql.ErrNoRows {
        return "", nil
    }
    return name, err
}

func TestUserRepoName(t *testing.T) {
    db, mock, err := sqlmock.New()
    require.NoError(t, err)
    t.Cleanup(func() { _ = db.Close() })
    rows := sqlmock.NewRows([]string{"name"}).AddRow("alice")
    mock.ExpectQuery(regexp.QuoteMeta(`SELECT name FROM users WHERE id = $1`)).
        WithArgs(int64(7)).WillReturnRows(rows)
    got, err := (&UserRepo{db: db}).Name(context.Background(), 7)
    require.NoError(t, err)
    require.Equal(t, "alice", got)
    require.NoError(t, mock.ExpectationsWereMet())
}
```

测试价值在于锁定 SQL 文本、占位符与参数类型：有人把 `$1` 改成字符串拼接，测试立刻失败。但它不会告诉你 PostgreSQL 是否接受这条语句——那是 4.4 的职责。

### 4.4 testcontainers 起 PostgreSQL / Redis

```go
package repo_test

import (
    "context"
    "database/sql"
    "testing"

    _ "github.com/jackc/pgx/v5/stdlib"
    "github.com/stretchr/testify/require"
    "github.com/testcontainers/testcontainers-go"
    "github.com/testcontainers/testcontainers-go/modules/postgres"
    "github.com/testcontainers/testcontainers-go/modules/redis"
)

func startPostgres(t *testing.T) *sql.DB {
    ctx := context.Background()
    ctr, err := postgres.Run(ctx, "postgres:17-alpine",
        postgres.WithDatabase("app"),
        postgres.WithUsername("app"),
        postgres.WithPassword("secret"),
    )
    require.NoError(t, err)
    testcontainers.CleanupContainer(t, ctr)
    dsn, err := ctr.ConnectionString(ctx, "sslmode=disable")
    require.NoError(t, err)
    db, err := sql.Open("pgx", dsn)
    require.NoError(t, err)
    t.Cleanup(func() { _ = db.Close() })
    return db
}

func TestRedis(t *testing.T) {
    ctr, err := redis.Run(context.Background(), "redis:7-alpine")
    require.NoError(t, err)
    testcontainers.CleanupContainer(t, ctr)
}
```

工程要点：**每个包只起一次**（放 `TestMain` 或 `sync.Once`），不要每个 `TestXxx` 拉一个容器；**钉镜像版本**（`latest` 会让 CI 在某个周一突然变红）；CI 首次拉镜像 10–60 秒、之后启动 2–5 秒，私有 runner 要确认 Docker daemon 权限与磁盘配额；显式 `CleanupContainer` 之外还有 Ryuk 兜底，避免 Ctrl-C 后残留。

### 4.5 fuzz 一个解析函数

```go
package ports

import (
    "errors"
    "strconv"
    "strings"
    "testing"
)

func ParsePort(s string) (int, error) {
    n, err := strconv.Atoi(strings.TrimSpace(s))
    if err != nil || n < 1 || n > 65535 {
        return 0, errors.New("invalid port")
    }
    return n, nil
}

func FuzzParsePort(f *testing.F) {
    for _, s := range []string{"80", "443", " 8080 ", "0", "65536", "-1", "abc"} {
        f.Add(s)
    }
    f.Fuzz(func(t *testing.T, s string) {
        if n, err := ParsePort(s); err == nil && (n < 1 || n > 65535) {
            t.Fatalf("out of range: %d for %q", n, s)
        }
    })
}
```

种子语料写在 `f.Add`（编译进测试二进制，不写入 testdata）；`testdata/fuzz/FuzzParsePort/` 是可手工提交的额外语料，fuzz 失败时工具也会把最小化用例写进去。`-run=FuzzParsePort` 只跑种子，`-fuzz=... -fuzztime=30s` 才变异。fuzz 只断言**不变量**（不 panic、不越界、解析成功必在合法区间），体内不要连数据库或发网络请求。

### 4.6 目录组织与 build tag

```text
internal/order/
├── service.go
├── service_test.go                    # 单元测试，默认执行
├── repository.go
└── repository_integration_test.go     # //go:build integration
```

`testdata/` 放 fixture（go 工具链忽略）。`_test.go` 不参与正常构建；包名用 `order_test` 是外部测试包，只能调用导出 API，能反过来检验 API 是否够用，需要白盒访问未导出字段时才用同包。build tag 必须在文件最前（前面只能有空行或 `// Copyright`），与 `package` 之间空一行；跑集成测试用 `go test -tags=integration -count=1 ./internal/order`。

### 4.7 可测性：注入 clock 与随机源

```go
package token

import "time"

type Clock interface{ Now() time.Time }

type Cache struct {
    clock Clock
    ttl   time.Duration
}

func (c *Cache) Fresh(at time.Time) bool { return c.clock.Now().Sub(at) < c.ttl }
```

测试里注入手写 `fakeClock`（`now time.Time` 字段 + `Now()` 返回它），推进时间就是改字段，**不需要 sleep**。同样的套路适用于随机数（注入 `io.Reader` 或 `func() int64`）、UUID 生成器、重试退避。原则：**领域代码不直接触碰 `time.Now`/`rand`/`os.Getenv`**，这些调用只允许出现在 `main` 或装配层。

---

## 5. 版本演进

| 版本 | 时间 | 与测试相关的要点 |
|---|---|---|
| Go 1.18 | 2022.3 | 原生模糊测试（`testing.F`、`go test -fuzz`）与泛型 |
| Go 1.22 | 2024.2 | **循环变量按迭代创建**（需 `go.mod` ≥ 1.22），并行捕获问题大幅减少 |
| Go 1.24 | 2025.2 | **`testing.B.Loop`** 取代 `b.N` 循环；新增 `T.Context`、`T.Chdir`；`synctest` 实验 |
| Go 1.25 | 2025.8 | `testing/synctest` 转正，假时钟下测并发时序；`T.Attr`、`T.Output` |
| Go 1.27.4 | 2026.11.10 | 本文基准；`testing` 无移除性变更，`B.Loop`/`T.Context` 已是推荐写法（截至 2026-11） |

`B.Loop` 的收益值得单独说：首次调用重置计时器、返回 false 时停止计时，setup/cleanup 不计入测量；编译器不会把循环体内的调用优化掉，也不用手动 `b.ResetTimer()`。老代码的 `b.N` 写法仍可用，新基准统一 `for b.Loop()`。

生态侧：`stretchr/testify` 主线 **v1.12.x**（v1.12.1，2026-08），API 长期稳定；`testcontainers-go` 主线 **v0.45.0**（2026-11），模块拆为 `modules/postgres`、`modules/redis` 等；`go-sqlmock` 停留在 **v1.5.2**（2024-01），作者明示"功能完整、寻找维护者"，属低维护但可用的稳定件。

---

## 6. 常见坑与最佳实践

1. **并行测试共享状态**：`t.Parallel()` 后所有子测试同时恢复，共享 slice/map/全局计数器必然竞争。要么每个 case 自建数据，要么并行段内只读；`-race` 必须进 CI，本地不跑等于没测。
2. **mock 过度导致测试与实现耦合**：对领域接口生成 mock，会把"调用几次、顺序如何"写进测试，重构内部实现就红一片。领域内用 fake，mock 只放第三方边界。
3. **测试依赖执行顺序**：Go 不保证同包内顺序，`-shuffle=on` 会打乱，`-count=1` 则会绕过结果缓存。任何"上个测试建的数据下个测试用"都是定时炸弹；共享 fixture 放 `TestMain`，每个测试自己清理。`-timeout=60s` 让卡死用例尽早带 goroutine 栈退出。
4. **集成测试污染本地库**：连开发库跑测试，轻则脏数据，重则 `TRUNCATE` 误伤。集成测试只连容器或专用库，连接串从环境变量读，默认跳过（build tag 或 `testing.Short()`）。
5. **用 sleep 等异步**：`time.Sleep(100*time.Millisecond)` 在 CI 上必然偶发失败。用 channel/WaitGroup 同步，或 `assert.Eventually` 轮询条件，超时给足但不要靠猜。
6. **覆盖率造假**：调用函数但不写断言、`t.Log` 代替 `t.Error`、`_ = f()` 当覆盖，都能把数字刷到 90%。而且 `-cover` 统计的是**语句是否执行**，不判断 `if` 两个方向是否都走到，语句覆盖不等于分支覆盖；覆盖率只用来找"从未执行的分支"，门禁建议"关键包不低于基线"而非全仓 80%。
7. **sqlmock 正则过宽**：`ExpectQuery("SELECT")` 能匹配任何 SELECT；用 `regexp.QuoteMeta` 锁全句或 `QueryMatcherEqual`。别忘了 `ExpectationsWereMet()`，否则未满足的期望会被静默忽略。
8. **在 goroutine 里 `t.Fatal`**：`FailNow` 只退出当前 goroutine，主 goroutine 不会停在失败点，后续代码继续执行，容易留下 panic 或泄漏。goroutine 内用 `t.Errorf`，或把 error 发回 channel 由主 goroutine 断言。
9. **时间与随机数不可控**：直接 `time.Now()` 让 TTL/过期逻辑只能 sleep 测，直接 `rand` 让用例不可复现。注入 clock 与随机源，测试用假实现推进时间。

---

## 7. 选型对比

| 维度 | 标准库 `testing` | testify | gomock / mockery | go-sqlmock | testcontainers-go |
|---|---|---|---|---|---|
| 层次 | 组织与生命周期 | 断言/suite | 接口替身生成 | SQL driver 边界 | 真实依赖 |
| 额外依赖 | 无 | 低（纯 Go） | 中（生成器+运行时） | 低 | 高（Docker） |
| 运行成本 | 最低 | 最低 | 低 | 低 | 秒级启动+镜像 |
| 保真度 | — | — | 只验证调用 | 中（不校验方言） | 高（真实引擎） |
| 主要风险 | 断言啰嗦 | 滥用 require 掩盖信息 | 与实现耦合、生成物维护 | 正则过宽、假通过 | CI 资源、镜像漂移 |
| 建议位置 | 所有层 | 所有层 | 第三方边界 | repository 单测 | 集成/契约测试 |

一句话：**默认只用标准库 + testify 把单元层写扎实，repository 的 SQL 构造用 sqlmock 锁住，真实约束与方言交给 testcontainers；mock 生成器留给不归你管的边界**。

参考：[testing 包文档](https://pkg.go.dev/testing)、[Go 模糊测试教程](https://go.dev/doc/fuzz/)、[testify](https://github.com/stretchr/testify)、[testcontainers-go](https://golang.testcontainers.org/)、[go-sqlmock](https://github.com/DATA-DOG/go-sqlmock)。
