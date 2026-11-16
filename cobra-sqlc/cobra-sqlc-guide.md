# Cobra / sqlc 技术指南

> 两个把「约定」变成「代码」的工具：Cobra 把命令树与参数解析标准化，sqlc 把 SQL 语句直接编译成类型安全的 Go 代码。
> 上游：<https://github.com/spf13/cobra> v1.10.2 · <https://github.com/sqlc-dev/sqlc> v1.31.1　|　配套：<https://github.com/pressly/goose> v3.30.1 · pgx v5　|　协议：Apache-2.0 / MIT

---

## 1. 项目简介

Cobra 是 spf13 为 Hugo 写的 CLI 框架，如今是 Go 命令行的事实标准（kubectl、docker、gh、hugo、etcdctl 都在用）。它把「命令树 + POSIX 风格 flag + 帮助/补全」固化成结构体，开发者只需填 `RunE`。sqlc 走另一条路：**不提供 ORM API，而是把 `.sql` 文件当作源码**，解析 DDL 与查询语句，在编译期推导参数与返回列的类型，生成 `Queries` 结构体与 `Querier` 接口。一个治理「程序怎么被调用」，一个治理「SQL 怎么变成函数」，二者组合正好覆盖一个 Go 后端服务的入口层与数据层。

**适用**：需要子命令的 CLI / 运维工具 / 服务二进制（`app serve`、`app migrate`）；SQL 复杂、DBA 参与评审、想要「SQL 就是文档」的数据访问层。**不适合**：只有一两个位置参数的小脚本（标准库 `flag` 足够）；把 sqlc 当 ORM 用（它没有关联、钩子、迁移、懒加载）；运行期动态拼 WHERE 的报表系统（见 3.6）。

```bash
go get github.com/spf13/cobra@v1.10.2
go install github.com/sqlc-dev/sqlc/cmd/sqlc@v1.31.1
go install github.com/pressly/goose/v3/cmd/goose@v3.30.1
go install github.com/spf13/cobra-cli@latest        # 脚手架，可选
```

---

## 2. 能力总览

| 维度 | Cobra（CLI 骨架） | sqlc（数据访问代码生成） |
|---|---|---|
| 输入 | Go 代码里声明的 `Command` 树 + pflag 定义 | `schema`（DDL 文件/目录）+ `queries`（带注解的 `.sql`） |
| 输出 | 可执行程序、帮助文本、补全脚本、man page | `db.go` / `models.go` / `query.sql.go` / `Querier` 接口 |
| 核心机制 | 遍历参数找目标命令 → 解析 flag → 校验 Args → 跑钩子 → `RunE` | 解析 DDL 建 catalog → 语义分析查询 → 推导类型 → 渲染模板 |
| 类型安全 | flag 取值仍是字符串，需 `GetInt` 等强转 | 参数与返回值**编译期**确定，列类型映射到 Go 类型 |
| 错误模型 | `RunE` 返回 `error`，`Execute` 统一出口 | 生成 `error` 返回；`:one` 无行时返回驱动的 ErrNoRows（`database/sql` 为 `sql.ErrNoRows`，pgx 为 `pgx.ErrNoRows`） |
| 扩展点 | `PersistentPreRunE`、`Args`、`FlagErrorFunc`、补全函数 | `overrides`、`sqlc.arg/narg/slice/embed`、Go/JSON 模板插件 |
| 配套 | Viper（配置）、cobra-cli（脚手架） | goose / golang-migrate（迁移）、pgx / database/sql（驱动） |

---

## 3. 核心概念与原理

### 3.1 Cobra 的命令树与执行流水线

`cobra.Command` 是一棵树：`parent` / `commands` 双向可查，`Use` 的第一段是命令名，后面是参数说明（`Use: "get NAME"`）。`ExecuteC` 先 `Find(args)`：用 `stripFlags` 把 `--flag value`、`--flag=value`、`-abc` 从参数里剔除，剩下的第一个词逐层匹配子命令名或 `Aliases`，因此 `app --log-level debug serve` 与 `app serve --log-level debug` 等价（前提是 `--log-level` 是 persistent flag）；匹配不到且当前命令有子命令时，Cobra 会用 `SuggestionsMinimumDistance`（默认 2）给出「你是不是想输入 app server?」。

命中目标命令后，`execute()` 严格按序执行：

```text
ParseFlags(args)          →  解析本命令可见的全部 flag（local + 继承的 persistent）
help / version 短路      →  -h、--help 直接打印并返回
ValidateArgs(args)        →  Args 校验器
PersistentPreRunE         →  沿父链只执行**最近一个**；之后 PreRunE
ValidateRequiredFlags()   →  MarkFlagRequired 的检查（在 PreRun 之后）
ValidateFlagGroups()      →  mutually exclusive / one required 组校验
RunE → PostRunE
```

注意两点：**`Args` 在 flag 解析之后**，所以校验器里可以 `cmd.Flags().GetString("env")`；`PersistentPreRunE` 不会叠加执行，若需父子都跑，必须在子命令里显式调用父逻辑。Cobra 还会自动挂 `help` 与 `completion` 两个子命令，可通过 `CompletionOptions.DisableDefaultCmd` 关掉。

### 3.2 `RunE`、错误传播与 `SilenceUsage` / `SilenceErrors`

`RunE` 的返回值是整个 CLI 唯一的错误通道：`execute()` 把它原样上抛，`ExecuteC` 再决定打印什么。两个开关控制两条输出：`SilenceErrors=false`（默认）打印 `Error: <err>` 到 stderr；`SilenceUsage=false`（默认）打印**整页 Usage / Flags / 子命令列表**。对服务型 CLI 后者几乎总是错的——数据库连不上时刷屏三十行帮助，真正的错误被埋掉。

生产写法是根命令同时置 `true`，错误由 `main` 统一处理，退出码自己给（Cobra **不会**根据错误类型设置退出码）：

```go
func main() {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	if err := NewRoot(&App{v: viper.New()}).ExecuteContext(ctx); err != nil {
		fmt.Fprintln(os.Stderr, "错误:", err)
		os.Exit(1) // Execute 返回 error 时进程默认仍是 0
	}
}
```

需要区分「用法错误」与「运行错误」时，别改全局开关，而是在 `Args` 校验器返回错误时**显式** `cmd.Usage()`，或用 `cmd.SetFlagErrorFunc` 统一包装。`ExecuteContext` 把 `cmd.Context()` 贯穿到 `RunE`，配合 `signal.NotifyContext` 才让 Ctrl-C 能中断长任务；`cmd.SetOut` / `SetErr` 可替换输出流，这是命令可测的关键（见第 6 章第 1 条）。错误包装用 `%w`，让 `main` 能 `errors.Is` 映射退出码（2 用法错误、3 未找到）。

### 3.3 Flags：`PersistentFlags` vs `LocalFlags`，Args 与必填校验

几套 flag 集合的关系是新手最容易绕晕的地方：

| 方法 | 内容 |
|---|---|
| `cmd.Flags()` | 执行期合并视图：本命令 local flag + 从祖先继承的 persistent flag |
| `cmd.PersistentFlags()` | 只有本命令声明、会被所有后代继承的 flag |
| `cmd.LocalFlags()` / `InheritedFlags()` | 本命令声明的全部（含自己的 persistent）/ 仅从祖先继承的 |
| `cmd.LocalNonPersistentFlags()` | 只在本命令生效、不向下传的 |

`--dsn` 这种「所有子命令都要用」的放 `PersistentFlags`；`serve --addr` 这种只属于一个命令的放 `Flags`。副作用是**兄弟命令会被 persistent flag 隐式耦合**：在 root 上加了 `--dry-run`，`migrate` 也会接受它，很容易出现「flag 被解析了但没被读取」的静默失效。

`Args` 是一组可组合的校验器：`cobra.NoArgs`、`ExactArgs(n)`、`MinimumNArgs(n)`、`RangeArgs(min, max)`、`OnlyValidArgs`（配合 `ValidArgs` 或 `ValidArgsFunction`），以及 `MatchAll(cobra.MinimumNArgs(1), myValidator)`。**不写 `Args` 时多余位置参数会被静默丢弃**，这是「用户敲错命令却成功退出」的头号原因。

必填校验有两层，别混用：`MarkFlagRequired` 只检查「用户是否显式传了这个 flag」，在 `PreRun` 之后、`RunE` 之前执行；但**它不看配置文件或环境变量**，即使 `PreRun` 已经从 `config.yaml` 读到 DSN 也不算数。所以**凡是配置文件 / 环境变量也能提供的项（DSN、端口）都不要标 required**，否则 `app serve` 不带 `--dsn` 仍会失败，正确做法是配置合并后在 `RunE` 里校验（见 4.2 的 `a.cfg.DSN == ""`）。继承 flag 要在**定义它的命令**上调 `MarkPersistentFlagRequired`，在子命令上调 `MarkFlagRequired` 会因找不到 flag 而返回 error——忽略这个 error 就得到「看起来设了必填、其实没生效」。互斥/依赖用 `MarkFlagsMutuallyExclusive`、`MarkFlagsOneRequired`、`MarkFlagsRequiredTogether`（v1.5+），它们同样走 `ValidateFlagGroups`。

### 3.4 sqlc 的编译模型：从 DDL 到类型

sqlc 是一次**离线编译**，分三段：

1. **建 catalog**：读 `schema` 指定的 DDL 文件（或 goose/golang-migrate 的迁移目录），解析 `CREATE TABLE / ALTER TABLE / CREATE TYPE`，得到「表 → 列 → 类型 + 是否 NOT NULL」的目录，作为全局状态。
2. **语义分析查询**：逐个 `queries` 文件按 `-- name: X :one` 切分，解析 SQL AST（PostgreSQL 走 libpg_query），用 catalog 做名字解析：`WHERE id = $1` 里 `$1` 的类型来自 `users.id`；`SELECT *` 展开成具体列；`JOIN` 后同名列去重并加表名前缀。
3. **渲染**：按 `engine`（`postgresql` / `mysql` / `sqlite`）与 `sql_package` 选类型映射表，输出三件套——`db.go`（`DBTX` 接口 + `New` + `WithTx`）、`models.go`（表结构体与枚举）、`query.sql.go`（`const getUser` + `Params`/`Row` + 方法 + `Querier` 接口）。

由此推出两个硬性质：**SQL 文本在生成期固定**，运行期不可能改写；**类型来自 schema**，schema 变了必须重新 generate，否则生成代码与真实库漂移。日常命令：`sqlc compile` 只做类型检查不落盘（CI 最快的一步）、`sqlc generate` 写文件、`sqlc diff` 比对生成代码差异、`sqlc vet` 跑配置里的 `rules`（如禁止全表扫描）。`sqlc.yaml` 的 `version: "2"` 是现行格式（`"1"` 已废弃但长期兼容）：顶层 `sql` 是一个**包列表**，每项即旧版的 `packages`，各自带 `engine`、`schema`、`queries` 与 `gen.go`，因此一个仓库可以同时生成 `store` 与 `report` 两个包。

### 3.5 查询注解、命名参数与类型映射

注解决定生成的方法签名，选错会丢掉关键信息：

| 注解 | 生成签名要点 | 适用 |
|---|---|---|
| `:one` | `(Row, error)`，无行返回 `sql.ErrNoRows` | 主键查询、`RETURNING` |
| `:many` | `([]Row, error)` | 列表；`emit_empty_slices` 决定空结果是否 `[]` |
| `:exec` | `error`，**丢弃影响行数** | DDL、确定会命中的写入 |
| `:execrows` | `(int64, error)` | 需要判断「是否真的改了」的 UPDATE/DELETE |
| `:execresult` / `:execlastid` | `(sql.Result, error)` / `(int64, error)` | 要 LastInsertId（MySQL） |
| `:copyfrom` | `(ctx, []Params) (int64, error)`，走 `pgx.CopyFrom` | 大批量 INSERT |
| `:batchexec` | `FooBatch` + `QueueAdd` + 回调 | 一次往返发多条语句 |

参数写法有三种：位置参数 `$1`（PostgreSQL）/ `?`（MySQL、SQLite）；`sqlc.arg('name')` 生成命名字段且**非空**；`sqlc.narg('name')` 生成可空类型（`pgtype.Text` / `sql.NullString` / 指针，取决于 `sql_package` 与选项）。`sqlc.embed(users)` 把整行结构体嵌进结果，省掉手写字段映射；`sqlc.slice('ids')` 只在 MySQL/SQLite 可用（展开成 `IN (?, ?, ...)`），PostgreSQL 应写 `= ANY(sqlc.arg('ids')::bigint[])`。

`NULL` 是类型映射的主战场。默认策略：**NOT NULL 列 → 原生类型（`string`、`int64`、`time.Time`），可空列 → 包装类型**。`sql_package: "pgx/v5"` 下可空列是 `pgtype.Text` / `pgtype.Int8` / `pgtype.Timestamptz`，取值要 `if v.Valid { ... }`；`database/sql` 下是 `sql.NullString` 一类。嫌麻烦可以开 `emit_pointers_for_null_types: true` 得到 `*string`，或用 `overrides` 把某个 `db_type` 映射到自定义类型（`uuid → uuid.UUID`、`numeric → decimal.Decimal`）。注意 `overrides` 要区分可空与否，加 `nullable: true` 才能覆盖可空列，只写一条往往只对 NOT NULL 生效。

### 3.6 动态 SQL 的边界与 goose 的配合

sqlc 的边界很硬：**WHERE 子句、排序列、表名都不能在运行期拼**。工程上有四种正当出路：写多条专用查询在 Go 里 switch（最推荐，可读可测）；用「可选过滤」模式 `WHERE (sqlc.narg('status')::text IS NULL OR status = sqlc.narg('status')::text)`，一条查询覆盖有无筛选；用 `sqlc.slice` / `ANY` 处理变长 `IN`；确实要动态排序列时，**只允许白名单**映射：`ORDER BY CASE WHEN sqlc.arg('sort')::text = 'created' THEN created_at END`，或退化到手写 pgx 查询。绝不接受把用户输入拼进 SQL 字符串——那等于同时放弃类型安全和注入防护。

迁移工具与 sqlc 的分工是「**迁移拥有 schema，sqlc 只读不写**」：goose 用版本化文件管理真实库演进，文件里 `-- +goose Up` / `-- +goose Down` 分段；sqlc 的 `schema` 直接指向 `db/migrations` 目录，它只解析 Up 段（遇到 `Down` 会跳过），因此**一份 DDL 同时喂给迁移和代码生成**，从根上消除「手写 schema.sql 与迁移不一致」的漂移。代价是迁移文件必须**可被解析**：sqlc 按文件名顺序应用，`ALTER TABLE ... RENAME` 这类语句要写全；用 squash / out-of-order 迁移时务必在 CI 里从空库 `goose up` 后再 `sqlc generate && git diff --exit-code`，让漂移变成红灯而不是线上事故。goose 的 `-- +goose StatementBegin/End` 用于函数、触发器这类含分号的语句，漏写会被切成两半。

---

## 4. 使用实战

### 4.1 目录布局与脚手架

```text
app/
├── cmd/app/main.go          # 只做 main、信号、退出码
├── internal/cli/            # root.go / serve.go / migrate.go
├── internal/service/        # 业务：组合 sqlc 的 Queries
├── internal/store/          # sqlc 输出，勿手改
├── db/                      # migrations/（goose）+ queries/ + embed.go
└── sqlc.yaml
```

`cobra-cli init --author "you" --license mit` 能生成 `cmd/root.go` + `main.go`，`cobra-cli add serve` 追加子命令；但它默认把 `rootCmd` 写成包级变量、在 `init()` 里 `AddCommand`，这正是第 6 章第 1 条要避免的形态。建议只借它的目录骨架，命令一律用构造函数产出。

### 4.2 root：flag、Viper 绑定与配置装载

`App` 是持有 `*viper.Viper`、`Config`、`*slog.Logger` 的轻量容器，由 `main` 构造后注入命令树；`Config` 字段用 `mapstructure` tag 与 Viper key 对齐。

```go
// internal/cli/root.go
package cli

import (
	"errors"
	"fmt"
	"io/fs"
	"log/slog"
	"os"
	"strings"

	"github.com/spf13/cobra"
	"github.com/spf13/viper"
)

type Config struct {
	DSN      string `mapstructure:"dsn"`
	LogLevel string `mapstructure:"log_level"`
	PoolSize int32  `mapstructure:"pool_size"`
}

func NewRoot(app *App) *cobra.Command {
	root := &cobra.Command{
		Use: "app", Short: "用户服务：HTTP + 迁移",
		SilenceUsage: true, SilenceErrors: true, // 错误统一由 main 打印一行
		PersistentPreRunE: func(cmd *cobra.Command, _ []string) error { return app.load(cmd) },
	}
	pf := root.PersistentFlags()
	pf.String("dsn", "", "PostgreSQL DSN（也可用 APP_DSN 或配置文件）")
	pf.String("log-level", "info", "debug|info|warn|error")
	pf.Int32("pool-size", 10, "连接池大小")
	pf.String("config", "./config.yaml", "配置文件路径")
	_ = app.v.BindPFlag("dsn", pf.Lookup("dsn")) // 返回 error 只可能是 flag 名写错
	_ = app.v.BindPFlag("log_level", pf.Lookup("log-level"))
	_ = app.v.BindPFlag("pool_size", pf.Lookup("pool-size"))
	root.AddCommand(newServe(app), newMigrate(app))
	return root
}

func (a *App) load(cmd *cobra.Command) error {
	v := a.v
	v.SetEnvPrefix("APP") // APP_DSN / APP_LOG_LEVEL
	v.SetEnvKeyReplacer(strings.NewReplacer(".", "_", "-", "_"))
	v.AutomaticEnv()
	cfgPath, _ := cmd.Flags().GetString("config")
	v.SetConfigFile(cfgPath) // 未指定时用 flag 默认的 ./config.yaml
	if err := v.ReadInConfig(); err != nil {
		// SetConfigFile 指定具体路径后，文件不存在返回 *fs.PathError，不是 ConfigFileNotFoundError
		var notFound viper.ConfigFileNotFoundError
		missing := errors.As(err, &notFound) || errors.Is(err, fs.ErrNotExist)
		// 显式 --config 指定的文件必须存在；默认路径缺失才允许降级到 env/flag
		if cmd.Flags().Changed("config") || !missing {
			return fmt.Errorf("读取配置: %w", err)
		}
	}
	if err := v.Unmarshal(&a.cfg); err != nil {
		return fmt.Errorf("解码配置: %w", err)
	}
	if a.cfg.DSN == "" { // 必填项在这里校验，而不是 MarkFlagRequired
		return errors.New("缺少 dsn：--dsn / APP_DSN / config.yaml 任选其一")
	}
	var lvl slog.Level
	_ = lvl.UnmarshalText([]byte(a.cfg.LogLevel))
	a.log = slog.New(slog.NewJSONHandler(os.Stderr, &slog.HandlerOptions{Level: lvl}))
	return nil
}
```

优先级来自 Viper 的固定链：**显式 `Set` > 用户改过的 flag > 环境变量 > 配置文件 > flag 默认值**。绑定 flag 时若用户没传，Viper 会跳过该 flag 继续下探，只有整条链都没值才回落到 `DefValue`——所以配置文件里的 `dsn` 不会被 `--dsn` 的空默认值盖掉；业务默认值用 `SetDefault` 表达，别塞进 flag 默认值。细节见 [Viper 指南](../viper/viper-guide.md)。

### 4.3 serve 与 migrate：LocalFlags 与子命令分工

```go
// internal/cli/serve.go
func newServe(app *App) *cobra.Command {
	var addr string
	cmd := &cobra.Command{
		Use: "serve", Short: "启动 HTTP 服务", Args: cobra.NoArgs,
		RunE: func(cmd *cobra.Command, _ []string) error {
			ctx := cmd.Context()
			pcfg, err := pgxpool.ParseConfig(app.cfg.DSN)
			if err != nil {
				return fmt.Errorf("解析 dsn: %w", err)
			}
			pcfg.MaxConns = app.cfg.PoolSize
			pool, err := pgxpool.NewWithConfig(ctx, pcfg)
			if err != nil {
				return fmt.Errorf("连接数据库: %w", err)
			}
			defer pool.Close()
			srv := &http.Server{Addr: addr, Handler: newRouter(store.New(pool), app.log)}
			go func() { _ = srv.ListenAndServe() }()
			<-ctx.Done() // ExecuteContext 收到 SIGINT/SIGTERM
			sctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
			defer cancel()
			return srv.Shutdown(sctx)
		},
	}
	cmd.Flags().StringVar(&addr, "addr", ":8080", "HTTP 监听地址") // 只属于 serve
	return cmd
}
```

```go
// internal/cli/migrate.go —— 需要 up / status 时把它们拆成子命令，复用 openDB
func newMigrate(app *App) *cobra.Command {
	return &cobra.Command{
		Use: "migrate", Short: "数据库迁移（goose）", Args: cobra.NoArgs,
		RunE: func(cmd *cobra.Command, _ []string) error {
			sqlDB, err := sql.Open("pgx", app.cfg.DSN) // 需空导入 pgx/v5/stdlib
			if err != nil {
				return fmt.Errorf("打开数据库: %w", err)
			}
			defer sqlDB.Close()
			goose.SetBaseFS(db.FS) // //go:embed migrations/*.sql
			if err := goose.SetDialect("postgres"); err != nil {
				return err
			}
			return goose.UpContext(cmd.Context(), sqlDB, "migrations")
		},
	}
}
```

### 4.4 `sqlc.yaml`

```yaml
version: "2"
sql:
  - engine: "postgresql"
    schema: "db/migrations"      # 直接吃 goose 迁移，单一事实来源
    queries: "db/queries"
    gen:
      go:
        package: "store"
        out: "internal/store"
        sql_package: "pgx/v5"     # 生成 pgx 原生签名，而非 database/sql
        emit_json_tags: true
        emit_interface: true      # 生成 Querier 接口，便于 mock
        emit_empty_slices: true   # 空结果返回 []T{} 而非 nil
        overrides:
          - db_type: "uuid"
            go_type: "github.com/google/uuid.UUID"
          - db_type: "uuid"
            nullable: true        # 可空列必须单独写一条
            go_type: "*github.com/google/uuid.UUID"
```

### 4.5 迁移与查询

```sql
-- db/migrations/0001_init.sql
-- +goose Up
CREATE TABLE users (
    id bigserial PRIMARY KEY, email text NOT NULL UNIQUE,
    name text NOT NULL, status text NOT NULL DEFAULT 'active',
    created_at timestamptz NOT NULL DEFAULT now()
);
-- +goose Down
DROP TABLE users;
```

```sql
-- db/queries/user.sql
-- name: ListUsers :many
SELECT * FROM users
WHERE (sqlc.narg('status')::text IS NULL OR status = sqlc.narg('status')::text)
ORDER BY id DESC LIMIT sqlc.arg('limit') OFFSET sqlc.arg('offset');

-- name: CountUsers :one
SELECT count(*) FROM users
WHERE (sqlc.narg('status')::text IS NULL OR status = sqlc.narg('status')::text);

-- name: CreateUser :one
INSERT INTO users (email, name) VALUES ($1, $2) RETURNING *;

-- name: BatchCreateUsers :copyfrom
INSERT INTO users (email, name) VALUES ($1, $2);

-- name: DeleteUser :execrows
DELETE FROM users WHERE id = $1;
```

```bash
sqlc compile && sqlc generate        # 先类型检查，再落盘
sqlc vet && sqlc diff && git diff --exit-code internal/store  # rules + 漂移检查
```

### 4.6 调用生成代码：分页、批量、错误分支

```go
// internal/service/user.go
type UserService struct{ q *store.Queries }

func (s *UserService) Page(ctx context.Context, status *string, page, size int32) ([]store.User, int64, error) {
	var st pgtype.Text // sqlc.narg → 可空类型，NULL 即 Valid=false
	if status != nil {
		st = pgtype.Text{String: *status, Valid: true}
	}
	total, err := s.q.CountUsers(ctx, store.CountUsersParams{Status: st})
	if err != nil {
		return nil, 0, fmt.Errorf("统计: %w", err)
	}
	rows, err := s.q.ListUsers(ctx, store.ListUsersParams{Status: st, Limit: size, Offset: (page - 1) * size})
	if err != nil {
		return nil, 0, fmt.Errorf("列表: %w", err)
	}
	return rows, total, nil // emit_empty_slices：空结果是 []，可安全 JSON 序列化
}

func (s *UserService) Import(ctx context.Context, pool *pgxpool.Pool, in []store.BatchCreateUsersParams) (int64, error) {
	tx, err := pool.Begin(ctx) // CopyFrom 必须在事务内
	if err != nil {
		return 0, err
	}
	defer tx.Rollback(ctx) // 已 Commit 时返回 ErrTxClosed，可忽略
	n, err := s.q.WithTx(tx).BatchCreateUsers(ctx, in)
	if err != nil {
		return 0, fmt.Errorf("批量写入: %w", err)
	}
	return n, tx.Commit(ctx)
}
```

`store.New(pool)` 能直接接 `*pgxpool.Pool`，因为生成的 `DBTX` 接口只要求 `Exec/Query/QueryRow`；`WithTx` 返回同一套方法绑定到 `pgx.Tx`，因此「一个事务里跨多个生成查询」不需要额外抽象层。`:execrows` 返回的 `RowsAffected` 要显式判 0——UPDATE 影响 0 行却当成功，是 `:exec` 最常见的线上事故。

### 4.7 补全脚本与动态补全

生成脚本是三条重定向：`go build -o bin/app ./cmd/app` 之后，`bin/app completion bash > /usr/local/etc/bash_completion.d/app`（zsh 写 `${fpath[1]}/_app`、fish 写 `~/.config/fish/completions/app.fish`），`bin/app __complete migrate ""` 可查看补全输出与 `ShellCompDirective`。静态补全只能补命令名与 flag；要补「数据库里已有的租户 ID」，给命令挂 `ValidArgsFunction`（或 `RegisterFlagCompletionFunc`），返回候选值 + `cobra.ShellCompDirectiveNoFileComp` 表示别再补文件名。补全函数跑在独立进程里，**必须快**：别连数据库做全表扫描，超过几百毫秒用户会明显卡顿。

---

## 5. 版本演进

Cobra 长期处于「小步稳定」：v1.8 / v1.9 以 bug 修复和 Go 版本跟进为主，v1.10.0（2025-08）把 pflag 提到 1.0.8，顺带引入 `ParseErrorsWhitelist → ParseErrorsAllowlist` 的重命名（同时依赖 cobra 与 pflag 的项目会编译失败），并允许自定义默认 `ShellCompDirective`；v1.10.1（2025-09）把 pflag 退回兼容版 1.0.9，修复了这次破坏性变更。sqlc 节奏更快，1.30 → 1.31 引入了 `sqlc parse`（输出 AST JSON）、完整 SQL 格式化、analyzer v2 实验特性；goose 在 2026 年保持月度发版。以下版本号均已核对上游 Releases（截至 2026-11）。

| 项目 | 版本 | 时间 | 要点 |
|---|---|---|---|
| Cobra | v1.10.0 | 2025-08 | pflag 1.0.8；`SetHelpFunc` 可拿 context；默认补全指令可定制 |
| Cobra | **v1.10.2** | 2025-09 起 | v1.10.1 回退 pflag 1.0.9 恢复 `ParseErrorsWhitelist` 兼容，v1.10.2 为后续补丁；当前最新 |
| sqlc | v1.18 前后 | 2023 | 引入 `sql_package: "pgx/v5"`，可空列改映射 `pgtype.*` |
| sqlc | **v1.31.1** | **2026-04-22** | 1.31.0 加 `parse` 子命令、AST 格式化、strip psql 元命令；1.31.1 修 `go install` 的 replace 指令 |
| goose | v3.28.0 | 2026-09-02 | 最低 Go 1.26；MySQL 表级 Locker；ClickHouse 版本表排序修正 |
| goose | **v3.30.1** | **2026-11-12** | 当前最新；v3.29.0 起支持 YDB 连接串凭证 |

升级提示：Cobra 1.10 与 pflag 必须同向升级，`go mod graph | grep pflag` 确认没有两个版本共存；sqlc 升级后先 `sqlc compile` 再 `sqlc generate`，重点看 `pgtype` 相关字段是否变化（v1.18 的 pgx/v5 迁移会改一批结构体字段类型，属于跨大版本改造）；goose 3.28 起 `tstamp` 改 `DATETIME`，老库需要手动 `ALTER TABLE goose_db_version`。

---

## 6. 常见坑与最佳实践

1. **命令写在 `init()` 里、用包级 `rootCmd`**：无法在一个测试进程里构造多份命令树，flag 值在用例间互相污染，`init` 还让「谁注册了这个 flag」不可追踪。→ 一律 `NewRoot(app)` 返回新树，依赖（Viper、logger、pool）作为参数注入，测试里 `root.SetArgs([]string{...}); root.SetOut(&buf)` 即可断言输出。
2. **没设 `SilenceUsage` / 忘了退出码**：`RunE` 返回「数据库连接失败」，用户看到三十行帮助 + 一条被淹没的错误；或者 `Execute()` 的返回值被丢弃，进程永远退出 0，CI 与 k8s 探针全部失灵。→ 根命令置 `SilenceUsage/SilenceErrors = true`，`main` 打印一行、按错误分级映射退出码（用法 2 / 未找到 3 / 其它 1）。
3. **persistent 与 local flag 混用**：把 `--addr` 放到 root 的 `PersistentFlags`，于是 `migrate` 也接受它却没人读；或子命令里 `Flags().String("dsn")` 另建同名 flag，与 root 的 persistent flag 相互覆盖。→ 全局项才 persistent，且只在定义处 `BindPFlag`、`MarkPersistentFlagRequired`。
4. **`MarkFlagRequired` 用错位置**：在子命令上标记继承来的 flag，返回的 error 被 `_ =` 吞掉，实际没生效；或把「配置里能给」的 DSN 标成必填，导致纯配置文件启动直接失败。→ 必填校验放在配置合并之后，required 只用于「只能从命令行给」的项。
5. **不写 `Args`**：`app serve extra-arg` 静默成功，参数被丢掉；`app get` 漏参数却进入 `RunE` 后 panic。→ 每个命令都显式选 `NoArgs` / `ExactArgs(n)` / `MinimumNArgs(1)`，值集合用 `OnlyValidArgs`。
6. **flag 与配置优先级错乱**：以为「flag 默认值 > 配置文件」，结果配置文件里的 `log_level` 被 flag 的 `"info"` 盖住；或在 `RunE` 里才 `BindPFlag`，Cobra 早已解析完，绑定形同虚设。→ 绑定在构造函数里做，理解「改过的 flag > env > config > flag 默认值」，默认值用 `SetDefault` 而非 flag 表达。
7. **手改生成代码**：在 `internal/store/query.sql.go` 里加了个判断，下次 `sqlc generate` 全没；或把 `out` 目录提交后又手动重命名方法，CI 的 `sqlc diff` 天天红。→ 生成目录只读，改 SQL 或改 `overrides`；把 `sqlc compile && sqlc generate && git diff --exit-code` 放进 CI。
8. **schema 与迁移漂移**：`sqlc.yaml` 指向一份手抄的 `schema.sql`，迁移却在另一个目录演进，生成代码查了不存在的列，运行期才报 `column does not exist`。→ schema 直指 `db/migrations`，CI 从空库 `goose up` 后再 generate；本地用 `sqlc vet` + `goose status` 双向确认。
9. **`NULL` 映射踩坑**：可空列生成 `pgtype.Text`，业务代码直接 `row.Name.String` 拿到 `""`、`row.CreatedAt.Time` 拿到零值时间，看起来「有值」实则是 NULL；`emit_pointers_for_null_types` 打开后又要处处判 `nil`。→ 建表尽量 `NOT NULL DEFAULT`，确实可空就显式查 `Valid`/`nil`，或在 `overrides` 里统一映射为 `*T`。
10. **把大 SQL 拼成字符串 + 用 `:exec`**：`WHERE` 用 `fmt.Sprintf` 拼、参数顺序靠人肉对齐，注入风险与类型错误双杀；UPDATE 走 `:exec` 又把 0 行更新当成成功。→ 拆成多条静态查询、可选条件用 `sqlc.narg`、`IN` 用 `ANY`/`sqlc.slice`、需要行数就用 `:execrows`。
11. **忽略 goose 的注解规则**：漏 `-- +goose Up`、含分号的函数体没包 `StatementBegin/End`、`-- +goose NO TRANSACTION` 与并发索引混用，表现为迁移只执行了一半。→ 一个文件一个版本、每条语句带分号、复杂 DDL 用 `StatementBegin/End`，上线前在空库跑一遍。

---

## 7. 选型对比

CLI 框架：

| 维度 | Cobra | urfave/cli v3 | kong | 标准库 `flag` |
|---|---|---|---|---|
| 命令树 | **最成熟**，任意嵌套 + 别名 + 建议 | 成熟，语法更「声明式」 | 由 struct tag 推导 | 无 |
| flag 解析 | pflag（POSIX 短/长、组合、slice） | 自带，支持环境变量绑定 | 反射建 flag，类型丰富 | 仅 `-flag` |
| 与 Viper | 官方级配合（`BindPFlag`） | 可用但需手写 | 可用但需手写 | 需自己桥接 |
| 适合 | 多子命令的服务与运维工具 | 中小型 CLI，函数式风格 | 配置项多的单命令工具 | 一次性脚本 |

数据访问：

| 维度 | sqlc | GORM | sqlx | ent |
|---|---|---|---|---|
| 输入 | 手写 SQL + DDL | Go struct | 手写 SQL | Schema DSL |
| 类型安全 | **编译期**（从 schema 推导） | 运行时（v1.30 泛型改善） | 手写扫描，易错 | **编译期最强** |
| 复杂 SQL | **原生，随便写** | 链式绕，需 Raw | 原生 | 图式查询，报表别扭 |
| 关联 / 钩子 / 迁移 | 无（交给 goose） | 内置 | 无 | 内置（图式） |
| 动态查询 | 弱（生成期固定） | **强** | **强** | 中 |
| 适合 | SQL 为主、DBA 参与评审的服务 | 业务建模、快速交付 | 报表 / 动态 SQL 逃生舱 | 实体关系复杂的领域 |

**分工建议**：入口层 Cobra + Viper 是默认组合；数据层以 sqlc 为**主干**——固定查询、事务、批量写入全部走生成代码，schema 由 goose 迁移独占。GORM 留给「后台管理 CRUD、字段多、要 AutoMigrate 快速起步」的模块或既有项目；它和 sqlc 可以共存于同一个库（同一份表结构，两套访问方式），但**同一张表只选一个写入口**，否则钩子与时间戳维护会互相打架。真正需要运行期拼 WHERE、动态排序、跨表 union 的报表，用 sqlx 写在独立的 `report` 包里，并强制参数化与列名白名单——这是 sqlc 有意不覆盖的边界，而不是它的缺陷。若团队人手少、SQL 能力不均，先用 GORM 起步再逐步把热点查询迁到 sqlc，比一步到位更现实。

参考：[Cobra 官方文档](https://cobra.dev/)、[sqlc 文档](https://docs.sqlc.dev/)、[goose README](https://github.com/pressly/goose)、[pgx v5 类型映射](https://github.com/jackc/pgx)。
