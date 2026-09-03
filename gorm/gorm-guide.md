# GORM 技术介绍与使用指南

> Go 生态最流行的全功能 ORM：链式 API、关联（预加载/联接）、事务/SavePoint、钩子、迁移器、软删除、读写分离插件、v1.30 起支持泛型接口。
> 仓库：<https://github.com/go-gorm/gorm>　|　当前主线：**v1.31.2**（2026.6.25 发布，MIT 协议，模块路径 `gorm.io/gorm`）

---

## 1. 项目简介

GORM（Go + ORM 谐音"凝胶"）是 Go 使用最广的 ORM 库。设计哲学：**开发者友好的全功能 ORM**——常见的数据库操作全部抽象为链式 API，同时保留逃生舱（Raw SQL / SQL 表达式 / Hint）。

核心卖点：

- **全功能 ORM**：CRUD、关联（belong to / has one / has many / many2many + 多态 + 预加载）、事务（嵌套/SavePoint）、钩子（BeforeCreate 等）、迁移器、软删除、乐观锁……
- **链式 API + 方法链安全**：`db.Where(...).Order(...).Find(...)`；`Session`/`Scopes` 控制复用；
- **多数据库方言**：MySQL / PostgreSQL / SQLite / SQL Server / TiDB，方言以独立 driver 模块发布；
- **插件体系**：官方 dbresolver（读写分离/多数据源）、Prometheus 指标、慢查询审计、分库分表（sharding）；
- **v1.30+ 泛型接口**：`gorm.G[User](db).Find(ctx)` 带来编译期类型安全，与旧 API 可混用。

适用场景：业务系统开发（快速建模 + 常规 CRUD）、需要迁移器管理 schema、需要关联查询的领域模型。**不适合**：报表级复杂 SQL（写起来绕，用 Raw/查询构造器或 sqlx 更直接）、极致性能的批量管道（database/sql 手写更可控）。

### 安装

```bash
go get -u gorm.io/gorm
go get -u gorm.io/driver/mysql      # 按需选择方言驱动
# postgres: gorm.io/driver/postgres
# sqlite:   gorm.io/driver/sqlite
# sqlserver: gorm.io/driver/sqlserver
```

### 快速开始

```go
package main

import (
    "gorm.io/driver/sqlite"
    "gorm.io/gorm"
)

type Product struct {
    gorm.Model            // ID + CreatedAt + UpdatedAt + DeletedAt（软删除）
    Code  string
    Price uint
}

func main() {
    db, err := gorm.Open(sqlite.Open("test.db"), &gorm.Config{})
    if err != nil {
        panic("failed to connect database")
    }

    db.AutoMigrate(&Product{})               // 迁移表结构

    db.Create(&Product{Code: "D42", Price: 100})

    var product Product
    db.First(&product, "code = ?", "D42")    // 单条查询
    db.Model(&product).Update("Price", 200)  // 更新单字段
    db.Delete(&product)                      // 软删除（UPDATE deleted_at）
}
```

---

## 2. 功能特性总览

| 能力 | 说明 |
|---|---|
| 模型定义 | struct → 表：主键（ID 约定）、`gorm:"column:type:size:index;..."` tag 定制、嵌入式结构 |
| CRUD | Create/CreateInBatches（批量+回填主键）、First/Take/Last/Find/FindInBatches、Update/Updates、Delete |
| 查询构造 | Where/Or/Not/Order/Limit/Offset/Group/Having/Distinct/Joins/子查询/命名参数/Group Conditions |
| 关联 | 四种关系 + 预加载（Preload/Joins/Association 模式）、多态、自引用、关联标签（foreignKey/references） |
| 事务 | `db.Transaction(func(tx) error)` 闭包自动提交回滚、嵌套事务（SavePoint/RollbackTo）、手动 Begin/Commit |
| 钩子 | Before/After + Create/Update/Find/Save/Delete；另有插件级的 `AfterInitialize` |
| 软删除 | `gorm.DeletedAt` 字段自动过滤；`Unscoped()` 含已删数据/物理删除 |
| 乐观锁 | 版本字段 + `WHERE version = ?` 原子更新 |
| 迁移器 | AutoMigrate（增量加列加索引）/ Migrator 手动 DDL（AddColumn/CreateIndex/HasTable...） |
| 上下文 | `db.WithContext(ctx)` 贯穿链路（超时/取消/tracing） |
| 高级模式 | DryRun（生成 SQL 不执行）、PrepareStmt（预编译缓存）、Find To Map、SQL Expr、Upsert（OnConflict） |
| 读写分离 | dbresolver 插件：多数据源、读写分离、强制走写库（`dbresolver.Write`） |
| **泛型接口** | v1.30+：`gorm.G[T](db)` 类型安全的 CRUD，避免 SQL 污染；Joins/Preload 增强（LimitPerRecord） |
| 日志 | 分级 Logger（SlowThreshold 慢查询）、Trace 支持 |

---

## 3. 核心概念与原理

### 3.1 架构：gorm.DB 的双重身份

```
*gorm.DB
  ├── 全局模式（初始化后）── 安全共享，链式方法克隆出新会话
  └── 会话/事务模式（Session/WithContext/Begin 之后）── newDB 标记，链式方法原地生效
```

关键机制（面试高频）：**链式方法会克隆 Statement**。`db.Where(...)` 返回新的 `*gorm.DB`（带条件副本），因此并发使用同一个全局 `db` 是安全的——但** finisher 方法（Find/Create...）执行后的实例不可再复用**做新查询。

```go
// 安全：并发复用全局 db
tx := db.Where("age > ?", 18).Session(&gorm.Session{})   // 显式新会话

// 危险：finisher 之后的 db 带着污染的条件
result := db.Where("a = 1").Find(&users)
db.Find(&all)          // ❌ 可能仍带 a=1 的条件（旧版本行为，勿这样写）
```

### 3.2 模型与约定

| 约定 | 规则 | 覆盖方式 |
|---|---|---|
| 主键 | 名为 `ID` 的字段 | `gorm:"primaryKey"` |
| 表名 | 结构体名复数蛇形 `UserInfo` → `user_infos` | `TableName()` 方法 / NamingStrategy |
| 列名 | 字段名蛇形 | `gorm:"column:xxx"` |
| 时间 | `CreatedAt/UpdatedAt` 自动维护 | `autoCreateTime/autoUpdateTime`（支持 unix 秒/毫秒/纳秒） |
| 软删 | `DeletedAt gorm.DeletedAt` | — |

### 3.3 关联模式与加载策略

```
User ──belongs to──► Company     （外键在 User）
User ──has one──►    CreditCard  （外键在 CreditCard）
User ──has many──►   Pets        （外键在 Pet）
User ──many2many──►  Languages   （中间表 user_languages）
```

三种加载方式（性能差异巨大）：

- **Preload**：单独发一条 `IN (...)` 查询加载关联——适合一对多/集合；
- **Joins**：单条 JOIN 查询带出 belongs to/has one——少一次往返；
- **Association 模式**：`db.Model(&user).Association("Pets")` 增删改查关联本身。

N+1 问题自查：`PrepareStmt + Logger` 打开 SQL 日志，循环里查关联就是 N+1，改 Preload 一次拉回。

### 3.4 事务：闭包 + SavePoint

```go
db.Transaction(func(tx *gorm.DB) error {
    if err := tx.Create(&order).Error; err != nil {
        return err                       // 返回 error 自动 ROLLBACK
    }
    if err := tx.Model(&stock).Update("qty", gorm.Expr("qty - ?", n)).Error; err != nil {
        return err
    }
    return nil                           // 返回 nil 自动 COMMIT
})

// 嵌套事务：内部 Transaction 创建 SavePoint，内层失败只回滚到 SavePoint
db.Transaction(func(tx *gorm.DB) error {
    tx.Create(&a)
    tx.Transaction(func(tx2 *gorm.DB) error {   // SAVEPOINT
        tx2.Create(&b)
        return errors.New("rollback inner")     // 只回滚 b
    })
    return nil                                  // a 正常提交
})
```

### 3.5 Hooks 与插件体系

```go
func (u *User) BeforeCreate(tx *gorm.DB) error {
    if u.Name == "" { return errors.New("name required") }   // 返回 error 中止创建
    u.UUID = uuid.New()
    return nil
}
```

钩子按"实体方法存在与否"分发，多个实体同一钩子按创建顺序执行；插件（如 dbresolver/prometheus/sharding）通过 `Callbacks()` 注册到 Create/Query/Update/Delete/RawQuery 五条链。

### 3.6 泛型接口（v1.30+，新代码推荐）

```go
// 旧：Find 需要传入目标切片，类型不匹配只能运行时发现
// 新：编译期绑定模型类型
user, err := gorm.G[User](db).Where("name = ?", "Jinzhu").First(ctx)
users, err := gorm.G[User](db).Where("age <= ?", 18).Find(ctx)
gorm.G[User](db).Create(ctx, &User{Name: "Alice"})

// 高级特性经可选参数（clause/plugin）透传
gorm.G[User](db, clause.Locking{Strength: "UPDATE"}).Find(ctx)
gorm.G[User](db, dbresolver.Write).Find(ctx)      // 强制读主库

// Preload 增强：每个主记录限载 2 个宠物
gorm.G[User](db).Preload("Friends.Pets", func(db gorm.PreloadBuilder) error {
    db.LimitPerRecord(2)
    return nil
}).Find(ctx)
```

泛型版有意移除了易误用的 `FirstOrCreate/Save`（歧义与并发问题），并新增事务超时机制缓解连接泄漏。

---

## 4. 使用实战

### 4.1 连接池与健康（生产必配）

```go
import (
    "gorm.io/driver/mysql"
    "gorm.io/gorm"
    "gorm.io/gorm/logger"
)

dsn := "user:pass@tcp(127.0.0.1:3306)/app?charset=utf8mb4&parseTime=True&loc=Local"
db, err := gorm.Open(mysql.Open(dsn), &gorm.Config{
    Logger: logger.Default.LogMode(logger.Warn),   // 慢查询按 Warn 输出
    PrepareStmt: true,                             // 预编译缓存（高 QPS 显著提速）
})

sqlDB, _ := db.DB()                                // 拿到 *sql.DB 管理连接池
sqlDB.SetMaxIdleConns(10)
sqlDB.SetMaxOpenConns(100)
sqlDB.SetConnMaxLifetime(time.Hour)
```

### 4.2 查询实战

```go
// 条件组合
db.Where("name IN ?", []string{"a", "b"}).
   Where("age BETWEEN ? AND ?", 18, 30).
   Where("role = ?", "admin").Or("vip = ?", true).
   Order("created_at DESC").
   Limit(20).Offset(40).
   Find(&users)

// 命名参数 + Group Conditions
db.Where("name = @name OR nickname = @name", sql.Named("name", "jinzhu")).Find(&users)
db.Where(db.Where("a = 1").Or("b = 2")).Where(db.Where("c = 3").Or("d = 4")).Find(&users)

// 选列 / 去 map
db.Model(&User{}).Select("name, age").Find(&results)
db.Model(&User{}).FindInBatches(&results, 100, func(tx *gorm.DB, batch int) error {
    return nil                                     // 分批处理，内存友好
})

// 子查询
db.Where("amount > (?)", db.Table("orders").Select("AVG(amount)")).Find(&orders)

// 行锁（事务内）
tx.Clauses(clause.Locking{Strength: "UPDATE"}).Find(&users, "id = ?", 1)

// Upsert
db.Clauses(clause.OnConflict{
    Columns:   []clause.Column{{Name: "id"}},
    DoUpdates: clause.AssignmentColumns([]string{"name", "age"}),
}).Create(&user)                                   // INSERT ... ON DUPLICATE KEY UPDATE
```

### 4.3 关联实战

```go
type User struct {
    ID        uint
    Name      string
    CompanyID uint
    Company   Company    `gorm:"foreignKey:CompanyID"`
    Pets      []Pet      `gorm:"foreignKey:OwnerID"`
    Languages []Language `gorm:"many2many:user_languages;"`
}

// 预加载（两条 SQL：users + WHERE id IN (...)）
db.Preload("Company").Preload("Pets").Find(&users)

// 嵌套预加载 + 条件
db.Preload("Pets", "species = ?", "dog").Preload("Company.CEO").Find(&users)

// 单条 belongs to 用 Joins 一条 SQL 搞定
db.Joins("Company").Find(&users)

// Association 模式：只操作关联
db.Model(&user).Association("Languages").Append([]Language{go, java})
db.Model(&user).Association("Pets").Count()
```

### 4.4 软删除与乐观锁

```go
// 软删除：DELETE → UPDATE ... SET deleted_at = now()
db.Delete(&user)                       // 普通 Where 查询自动加 deleted_at IS NULL
db.Unscoped().Find(&users)            // 含已删
db.Unscoped().Delete(&user)           // 物理删除

// 乐观锁
type Article struct {
    ID      uint
    Content string
    Version int       `gorm:"default:1"`
}
res := db.Model(&Article{}).Where("id = ? AND version = ?", id, ver).
    Updates(map[string]any{"content": newContent, "version": ver + 1})
if res.RowsAffected == 0 {
    // 并发冲突，重试或报错
}
```

### 4.5 读写分离（dbresolver 插件）

```bash
go get gorm.io/plugin/dbresolver
```

```go
import "gorm.io/plugin/dbresolver"

db.Use(dbresolver.Register(dbresolver.Config{
    Replicas: []gorm.Dialector{mysql.Open(replicaDSN)},   // 读
}).SetMaxOpenConns(50))

// 指定表路由到独立数据源（分库场景）
db.Use(dbresolver.Register(dbresolver.Config{
    Sources:  []gorm.Dialector{mysql.Open(ordersDSN)},
}, "orders"))
db.Scopes(dbresolver.Use("orders")).Find(&orders)

// 写后立读：强制主库
db.Clauses(dbresolver.Write).First(&user, id)
```

### 4.6 迁移

```go
// 增量迁移：建表、加列、加索引（不删列不改类型——保守策略）
db.AutoMigrate(&User{}, &Order{}, &Article{})

// Migrator 手动控制
db.Migrator().HasTable(&User{})
db.Migrator().AddColumn(&User{}, "Age")
db.Migrator().CreateIndex(&User{}, "idx_name")
db.Migrator().DropColumn(&User{}, "Nickname")
```

生产建议：迁移走版本化工具（golang-migrate/atlas/ent 的 atlas），AutoMigrate 用于开发与测试环境。

---

## 5. 版本演进速览

| 版本 | 时间 | 要点 |
|---|---|---|
| v2.0（模块 v1.20） | 2020.8 | 从零重写：Context/批量插入/嵌套事务/关联增强/插件系统/Migrator，模块路径 `gorm.io/gorm` |
| v1.21–v1.25 | 2020–2023 | 方言与关联持续打磨、性能修复、JSON 支持完善 |
| v1.26 | 2025.5 | 稳定性与 32 位环境修复 |
| v1.30.0 | 2025 末–2026 初 | **泛型接口 `gorm.G[T]`**（编译期类型安全）、Joins/Preload 增强（LimitPerRecord、自定义 Join 条件）、事务超时处理（连接泄漏防护） |
| v1.31.x | 2026.6 | rows 泄漏修复（panic 场景 defer Close）、ExplainSQL 性能、migrator nil 防护、泛型 WithResult 错误填充 |

生态配套：`gen`（代码生成/类型安全 DAO）、`sharding`（分表）、`prometheus`、`sqlite`/`clickhouse` 等第三方方言。

---

## 6. 常见坑与最佳实践

1. **链式复用污染**：finisher（Find/Create）执行后的 `*gorm.DB` 不再复用；需要复用条件用 `Session(&gorm.Session{})` 或 `Scopes` 显式建会话；
2. **Updates 的零值陷阱**：`Updates(User{Name: "", Age: 18})` 用 struct 更新会**忽略零值字段**——要更新为零值用 `map[string]any{"name": ""}` 或 `Select("name").Updates(...)`；
3. **AutoMigrate 不删列**：删列/改类型/收窄范围都不做（防丢数据），DDL 变更走版本化迁移工具；
4. **N+1 查询**：循环里访问关联字段就是 N+1；打开 SQL 日志自查，改 Preload/Joins；
5. **`First` 的隐式主键排序**：`First` 附加 `ORDER BY id LIMIT 1`，`Take` 不排序——语义不同别混用；
6. **ErrRecordNotFound 只有 First/Last/Take 返回**：`Find` 查不到不报错（空切片）——判断存在用 `Count` 或 `First`；
7. **事务里必须用 tx 而非 db**：闭包里误用全局 `db` 的操作不在事务里，且连接会泄漏；
8. **软删除不覆盖唯一索引**：删除后同唯一键再次插入冲突——要么物理删除，要么唯一索引包含 `deleted_at`；
9. **PrepareStmt 与连接池**：预编译缓存按连接失效，频繁 `SetMaxOpenConns` 变更会降低命中率；
10. **大结果集必须分批**：`FindInBatches`/游标分页（`Where("id > ?", lastID).Order("id").Limit(n)`）代替深分页 `Offset`；
11. **WithContext 别忘传**：没有 context 的查询无法被上层超时/取消/追踪——所有业务查询统一 `db.WithContext(ctx)`（或泛型接口直接传 ctx）。

---

## 7. 选型对比

| 维度 | GORM | ent（Facebook） | sqlx | sqlc |
|---|---|---|---|---|
| 定位 | 全功能 ORM | 实体框架（代码生成） | sql 增强库 | SQL→代码生成 |
| 类型安全 | 运行时（v1.30 泛型改善） | **编译期最强** | 手写扫描 | **编译期** |
| 迁移/关联 | 内置 | 内置（图式） | 无 | 无 |
| 学习曲线 | 低-中 | 中（schema DSL） | 低 | 低（要会 SQL） |
| 复杂 SQL | 链式绕，可 Raw | 一般 | **直接** | **原生** |
| 生态 | **最大** | 大 | 大 | 增长中 |

一句话：**业务建模 + 快速开发选 GORM（新代码上泛型接口）；要强类型保障看 ent；SQL 为主、ORM 为辅的团队用 sqlx/sqlc**。

参考：[官方文档 gorm.io](https://gorm.io/zh_CN/docs/)、[泛型用法指南](https://gorm.io/zh_CN/docs/the_generics_way.html)、[官方测试场 playground](https://github.com/go-gorm/playground)。
