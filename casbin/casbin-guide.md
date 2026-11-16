# Casbin 技术介绍与使用指南

> 把「谁能对什么做什么」从硬编码里抽出来、用一份 CONF 模型加一组策略规则描述的嵌入式授权判定引擎，同一套语义覆盖 ACL / RBAC / ABAC / RESTful 并跨十余种语言。
> 仓库：<https://github.com/casbin/casbin>　|　当前主线：**v3.11.0**（2026.8.20 发布，Apache-2.0 协议）

---

## 1. 项目简介

Casbin（现为 Apache 孵化器项目，主仓库迁至 `apache/casbin`，旧链接自动跳转）是 Go 生态事实标准的**授权（authorization）库**：模型用 `.conf` 描述请求形状、策略形状、匹配与裁决，策略是一条条 `p, alice, data1, read` 规则。换授权机制 = 改配置，不动业务代码。

**只做授权判定（`sub` 能否对 `obj` 执行 `act`），不做认证**：不校验密码、不签发 token、不管理用户与角色目录（用户表、组织架构由业务系统自己维护），只为 RBAC 保存「用户↔角色」映射。认证交给 JWT / OIDC / Session，Casbin 接在认证之后。核心卖点：

- **PERM 元模型**：五段配置任意组合出 ACL、RBAC、多租户、ABAC、RESTful、拒绝优先、优先级模型；
- **多语言同构**：Go/Java/Node/PHP/Python/.NET/C++/Rust 共享同一套 model 与 policy 语义；
- **存储与同步解耦**：Adapter 管持久化（文件/DB/对象存储数十种），Watcher 管多实例同步；
- **运行时可改策略 + 轻量嵌入**：`AddPolicy` 即时生效，无外部依赖，判定在进程内微秒级完成。

适用：后台 RBAC、多租户 SaaS、API 网关与微服务统一鉴权。**不适用**：关系图谱式授权（Zanzibar/ReBAC）或需独立策略控制面的场景（见第 7 章）。

### 安装与最小示例

```bash
go get github.com/casbin/casbin/v3
# 存量 v2 用 .../v2，API 一致，仅 module path 不同
```

```go
package main
import (
	"fmt"
	"log"
	"github.com/casbin/casbin/v3"
)
func main() {
	e, err := casbin.NewEnforcer("model.conf", "policy.csv")
	if err != nil { log.Fatal(err) }
	ok, err := e.Enforce("alice", "data1", "read") // (bool, error)，别丢 error
	if err != nil { log.Fatal(err) }
	fmt.Println("alice read data1 =>", ok)
}
```

`model.conf` 见 3.1，`policy.csv` 两行：`p, alice, data1, read` 与 `p, bob, data2, write`。

---

## 2. 功能特性总览

| 能力 | 说明 |
|---|---|
| 访问控制模型 | ACL、超级用户、无用户/无资源 ACL、RBAC、资源角色、多域 RBAC、ABAC、RESTful、拒绝优先、优先级；`r`/`p` 字段可任意扩展（`dom`、`ip`） |
| 匹配函数 | 内置 `keyMatch`/`keyMatch2`/`keyMatch3`/`keyMatch4`、`regexMatch`、`ipMatch`、`globMatch`；`AddFunction` 注册自定义函数 |
| 角色管理 | `g` 用户→角色、`g2` 资源层级、多级继承、域隔离、条件角色链接、外部 RoleManager（LDAP/Okta） |
| 策略存储与同步 | 默认文件 Adapter；GORM/MySQL/Pg/Mongo/Redis/S3 等数十种；FilteredAdapter 按需加载；Watcher（etcd/Redis/pub-sub）、WatcherEx、Dispatcher 跨节点同步 |
| 管理与判定 API | `AddPolicy`/`RemovePolicy`/`UpdatePolicy`/`GetPolicy` 全套；`Enforce`、`EnforceWithMatcher`、`EnforceEx`、`BatchEnforce`、`EnforceContext`；`SetLogger`、`Explain()`（v3.10+ 借 LLM 解释判定）、环检测 detector；生态有在线 Editor、Casdoor、Gin/Echo 中间件、Casbin.js |
| 性能与并发 | `CachedEnforcer` 决策缓存、`EnableCache`、`InvalidateCache`、matcher 编译缓存、`SyncedEnforcer` 带锁 |

---

## 3. 核心概念与原理

### 3.1 PERM 元模型：五段配置

PERM 取 **P**olicy、**E**ffect、**R**equest、**M**atchers 首字母，落地为 `model.conf` 的五个 section：

| section | 记号 | 含义 |
|---|---|---|
| `[request_definition]` | `r` | **Request**：请求字段元组，如 `sub, obj, act` |
| `[policy_definition]` | `p` | **Policy**：规则字段元组，通常与 `r` 同构 |
| `[role_definition]` | `g` | 角色/分组映射（RBAC 才有），`g = _, _` 或带域 `g = _, _, _` |
| `[policy_effect]` | `e` | **Effect**：多条规则命中后如何归约成最终裁决 |
| `[matchers]` | `m` | **Matcher**：请求与规则逐条求值的布尔表达式 |

最简 ACL 模型（即 1 章引用的 `model.conf`）：

```ini
[request_definition]
r = sub, obj, act
[policy_definition]
p = sub, obj, act
[policy_effect]
e = some(where (p.eft == allow))
[matchers]
m = r.sub == p.sub && r.obj == p.obj && r.act == p.act
```

`r` 与 `p` 只是形状声明，值分别来自 `Enforce(...)` 与 policy 文件，`m` 用 `r.xxx`/`p.xxx` 引用。模型与策略分离，是「改配置就换模型」的根本。

### 3.2 一次 Enforce 的求值过程

`e = some(where (p.eft == allow))` 意为「存在一条命中且 effect 为 allow 的规则就放行」。链路：① `Enforce("alice","data1","read")` 把实参绑定到 `r.sub/r.obj/r.act`（个数不符返回 `invalid request size` 错误）；② 遍历 `p` 每行代入 matcher，由 govaluate 求值；③ 命中的行贡献 effect（声明 `p_eft` 时取行尾 `allow`/`deny`，否则恒 `allow`）；④ `e` 经 effector 归约、命中即短路；⑤ 返回 `(bool, error)`。函数未注册、字段类型不符、`in` 数组过短都走 `error` 而非静默失败——这就是别丢第二个返回值的理由。变体：`!some(where (p.eft == deny))`（拒绝优先）、`some(where (p.eft == allow)) && !some(where (p.eft == deny))`（deny-override）、`priority(p.eft) || deny`（按优先级，policy 需带 priority 列）。

### 3.3 五种主流 model 的写法与差异

差异集中在 `[role_definition]` 与 `m` 两行（`r`/`p` 形状随模型调整）：

| 模型 | 关键定义 | matcher 核心 |
|---|---|---|
| ACL | 无 `g` | `r.sub == p.sub && r.obj == p.obj && r.act == p.act` |
| RBAC | `g = _, _` | `g(r.sub, p.sub) && r.obj == p.obj && r.act == p.act` |
| RBAC with domains | `g = _, _, _`，`r = sub, dom, obj, act` | `g(r.sub, p.sub, r.dom) && r.dom == p.dom && r.obj == p.obj && r.act == p.act` |
| ABAC | 可无策略行 | `r.sub.Age >= 18 && r.obj.Owner == r.sub.Name` |
| RESTful | 无 `g` | `r.sub == p.sub && keyMatch2(r.obj, p.obj) && regexMatch(r.act, p.act)` |

RBAC 把 `p` 写成「角色→权限」、`g` 写成「用户→角色」，用户数与资源数解耦；domains 让 `alice` 在 `tenant1` 是 admin、在 `tenant2` 只是 viewer；ABAC 请求直接传结构体、matcher 反射取属性，策略可为空；RESTful 用 `keyMatch2` 支持 `/api/orders/:id` 占位与 `*`、`regexMatch` 让 `p.act` 写 `(GET)|(POST)` 正则——`keyMatch` 只认 `*`，路径参数必须用 2/3/4 代。

### 3.4 `g` 角色继承与 `g2` 资源层级

`[role_definition]` 可声明多组：`g = _, _`（用户→角色）、`g2 = _, _`（资源→资源组），matcher 里分别调用 `g(r.sub, p.sub)` 与 `g2(r.obj, p.obj)`。角色支持多级继承：`g, alice, admin` 加 `g, admin, super` 使 alice 继承 super 全部权限（默认 RoleManager 有层级上限，可 `SetRoleManager` 替换）；资源层级同理，`g2, data1, dataset_a` 后对 `dataset_a` 授权即覆盖其下资源。带域用 `g = _, _, _`，第三列为 domain，继承只在同域内生效。

### 3.5 matcher 的表达力：内置函数、自定义函数、ABAC 结构体

`m` 跑在 govaluate 上，除 `==`/`&&`/`||`/`in` 还能调内置与自定义函数。`e.AddFunction(name, func(args ...interface{}) (interface{}, error))` 把无法用表达式书写的逻辑（网段判断、外部查询）封装进 matcher，**必须在首次 `Enforce` 前注册**——matcher 表达式会被缓存。ABAC 更进一步：`Enforce` 直接传结构体，matcher 用 `r.sub.Age`、`r.obj.Owner` 点号路径取字段，策略退化为零行；代价是决策缓存失效（`GetCacheKey` 只接受 `string` 或实现 `CacheableParam` 的类型），字段类型错误要到运行时才暴露。

### 3.6 策略爆炸：判据写进 matcher 还是 policy

这是最重要的设计权衡。判据写 **policy**（`p, alice, /api/orders, GET`）是数据驱动：运营可运行时改、无需发版，但行数随「用户 × 资源 × 动作」线性膨胀；写 **matcher**（`r.obj.Owner == r.sub.Name`）是模型驱动：策略行数 O(1)，但改逻辑要改 `.conf` 并回归测试。三条收敛手段：RBAC 用 `g` 把 N×M 压成 N+M；RESTful 用 `keyMatch2` 把 `/orders/1`、`/orders/2` 收敛成一条 `/orders/:id`；ABAC 把「只能改自己的单」「成年才能下单」等稳定不变量上移 matcher。准则：**会随运营变动的放 policy，稳定的业务不变量放 matcher**，二者可混用（RBAC + ABAC 条件同处一个模型）。

---

## 4. 使用实战

### 4.1 RBAC + 角色继承（完整可运行）

用 `model.NewModelFromString` 内联模型；无 Adapter 时必须 `EnableAutoSave(false)`，否则 `autoSave` 默认 true 会向 nil Adapter 落盘而 panic。

```go
package main
import (
	"fmt"
	"log"
	"github.com/casbin/casbin/v3"
	"github.com/casbin/casbin/v3/model"
)
const mText = `
[request_definition]
r = sub, obj, act
[policy_definition]
p = sub, obj, act
[role_definition]
g = _, _
[policy_effect]
e = some(where (p.eft == allow))
[matchers]
m = g(r.sub, p.sub) && r.obj == p.obj && r.act == p.act
`
func main() {
	m, err := model.NewModelFromString(mText)
	if err != nil { log.Fatal(err) }
	e, err := casbin.NewEnforcer(m)
	if err != nil { log.Fatal(err) }
	e.EnableAutoSave(false)
	if _, err = e.AddPolicy("admin", "data1", "read"); err != nil { log.Fatal(err) }
	if _, err = e.AddGroupingPolicy("alice", "admin"); err != nil { log.Fatal(err) }
	ok, err := e.Enforce("alice", "data1", "read") // 经 g 继承拿到 admin 权限
	if err != nil { log.Fatal(err) }
	fmt.Println("alice read data1 =>", ok)
}
```

### 4.2 matcher 表达力：自定义函数与 ABAC 结构体

`net_model.conf` 的 matcher 为 `m = r.sub == p.sub && ipInRange(r.ip, p.cidr) && r.obj == p.obj && r.act == p.act`（`r = sub, ip, obj, act`），`net_policy.csv` 一行 `p, alice, 10.0.0.0/8, /admin, read`；`abac_model.conf` 的 matcher 为 `m = r.sub.Age >= 18 && r.obj.Owner == r.sub.Name && r.act == "read"`，策略留空。`AddFunction` 无返回值，注册须早于首次判定。

```go
package main
import (
	"fmt"
	"log"
	"net"
	"github.com/casbin/casbin/v3"
)
type User struct { Name string; Age int }
type Doc struct{ Owner string }
func main() {
	e1, err := casbin.NewEnforcer("net_model.conf", "net_policy.csv")
	if err != nil { log.Fatal(err) }
	e1.AddFunction("ipInRange", func(args ...interface{}) (interface{}, error) {
		ip := net.ParseIP(args[0].(string))
		_, cidr, err := net.ParseCIDR(args[1].(string))
		if err != nil { return false, err }
		return ip != nil && cidr.Contains(ip), nil
	})
	ok1, err := e1.Enforce("alice", "10.1.2.3", "/admin", "read")
	if err != nil { log.Fatal(err) }
	e2, err := casbin.NewEnforcer("abac_model.conf", "empty.csv")
	if err != nil { log.Fatal(err) }
	ok2, err := e2.Enforce(User{Name: "alice", Age: 20}, Doc{Owner: "alice"}, "read")
	if err != nil { log.Fatal(err) }
	fmt.Println("custom func =>", ok1, "| abac struct =>", ok2)
}
```

### 4.3 Adapter 与 Watcher

Adapter 只管策略**存取**，不含模型与判定。默认文件 Adapter（`NewEnforcer("m.conf","p.csv")`）适合单机；生产用数据库 Adapter。注意 v2 与 v3 的 `model.Model` 是不同 Go 类型，**Adapter 的 major 版本必须与 casbin 主版本一致**（`gorm-adapter/v3` 配 `casbin/v3`）。

```go
package main
import (
	"log"
	"github.com/casbin/casbin/v3"
	gormadapter "github.com/casbin/gorm-adapter/v3"
	_ "github.com/go-sql-driver/mysql"
)
func main() {
	a, err := gormadapter.NewAdapter("mysql", "root:pass@tcp(127.0.0.1:3306)/casbin")
	if err != nil { log.Fatal(err) }
	e, err := casbin.NewEnforcer("rbac_model.conf", a)
	if err != nil { log.Fatal(err) }
	if _, err = e.AddPolicy("alice", "data1", "read"); err != nil { log.Fatal(err) }
	if err = e.SavePolicy(); err != nil { log.Fatal(err) }
}
```

自定义 Adapter 实现 `persist.Adapter`（`Add`/`Remove`/`RemoveFiltered` 增量写，`SavePolicy` 全量覆盖）；大数据量用 `FilteredAdapter` + `LoadFilteredPolicy` 只载入本租户策略，代价是该实例不能再 `SavePolicy`。Watcher 解决多实例同步：**任一实例 `AddPolicy`/`SavePolicy` 时广播消息，其余实例回调 `LoadPolicy()` 重载**；etcd-watcher 监听 revision/租约、redis-watcher 走 Pub/Sub。`SetWatcher(w)` 自动注册 `LoadPolicy` 回调（WatcherEx 需自行 `SetUpdateCallback`），`EnableAutoNotifyWatcher(false)` 可改手工 `Update()`；Watcher 是**最终一致**，短暂窗口内各实例判定可能不同。两个接口：

```text
Adapter: LoadPolicy(model.Model) error / SavePolicy(model.Model) error
         AddPolicy(sec, ptype string, rule []string) error
         RemovePolicy(sec, ptype string, rule []string) error
         RemoveFilteredPolicy(sec, ptype string, idx int, values ...string) error
Watcher: SetUpdateCallback(func(string)) error / Update() error / Close()
```

### 4.4 LoadPolicy / SavePolicy / AutoSave 与性能

`LoadPolicy()` 从 Adapter **全量重载**并重建角色继承链（清空旧策略，Watcher 回调默认就是它）；`SavePolicy()` **全量覆盖写回**并触发 Watcher 广播，过滤加载的实例调用报 `cannot save a filtered policy`；`AutoSave`（`EnableAutoSave`，默认 true）让每次增删改立即增量写库，批量导入时应先关、改内存、最后一次性 `SavePolicy`。并发上：普通 `*Enforcer` 的只读 `Enforce` 并发安全（matcher 表达式缓存在 `sync.Map`），写 API 与读并发需外部锁或换 `NewSyncedEnforcer`；`Enforce` 遍历全部匹配策略、复杂度 O(策略行数)、单次约微秒量级，策略上万行应靠 RBAC/过滤加载收敛。`NewCachedEnforcer` 缓存 `(请求参数 → bool)`，用内部 `policyVersion` 自动失效，外部直接改库时版本号不变，需 Watcher 触发 `LoadPolicy` 或手动 `InvalidateCache()`。批量判定用 `BatchEnforce`；高基数入参（UUID、动态路径）让 `g` 函数缓存无界增长，可 `EnableGFunctionCache(false)` 关闭。

```go
package main
import (
	"fmt"
	"log"
	"github.com/casbin/casbin/v3"
)
func main() {
	e, err := casbin.NewCachedEnforcer("rbac_model.conf", "policy.csv")
	if err != nil { log.Fatal(err) }
	e.EnableAutoSave(false)                        // 批量导入不逐条落盘
	if _, err = e.AddPolicy("bob", "data2", "write"); err != nil { log.Fatal(err) }
	if err = e.SavePolicy(); err != nil { log.Fatal(err) } // 全量写回
	if err = e.LoadPolicy(); err != nil { log.Fatal(err) } // 重载并重建 g
	e.EnableCache(true)                           // 决策缓存，默认已开
	ok, err := e.Enforce("alice", "data1", "read")
	if err != nil { log.Fatal(err) }
	fmt.Println("single =>", ok)
	res, err := e.BatchEnforce([][]interface{}{
		{"alice", "data1", "read"}, {"bob", "data2", "write"},
	})
	if err != nil { log.Fatal(err) }
	fmt.Println("batch =>", res)
	if err = e.InvalidateCache(); err != nil { log.Fatal(err) }
}
```

### 4.5 API 权限中间件与 Gin 集成

链路：认证中间件解析 token → `c.Set` 注入用户 → 授权中间件取 `sub`、`URL.Path` 作 `obj`、`Method` 作 `act` → `Enforce` → 403 短路。配 3.3 的 RESTful 模型，一条 `/api/orders/:id` 规则覆盖所有订单详情；**认证由上游负责，Casbin 只接最后一棒**。

```go
package main
import (
	"log"
	"github.com/casbin/casbin/v3"
	"github.com/gin-gonic/gin"
)
func Authz(e *casbin.Enforcer) gin.HandlerFunc {
	return func(c *gin.Context) {
		ok, err := e.Enforce(c.GetString("user"), c.Request.URL.Path, c.Request.Method)
		if err != nil { c.AbortWithStatusJSON(500, gin.H{"code": "AUTHZ_ERROR"}); return }
		if !ok { c.AbortWithStatusJSON(403, gin.H{"code": "FORBIDDEN"}); return }
		c.Next()
	}
}
func main() {
	e, err := casbin.NewEnforcer("restful_model.conf", "policy.csv")
	if err != nil { log.Fatal(err) }
	r := gin.New()
	r.Use(gin.Recovery())
	r.Use(func(c *gin.Context) { c.Set("user", c.GetHeader("X-User")); c.Next() }) // 占位：换成 JWT/OIDC
	r.Use(Authz(e))
	r.GET("/api/orders/:id", func(c *gin.Context) { c.JSON(200, gin.H{"ok": true}) })
	if err := r.Run(":8080"); err != nil { log.Fatal(err) }
}
```

---

## 5. 版本演进与要点

截至 2026-11 主线为 **v3.11.0（2026.8.20 发布，Apache-2.0 协议）**。项目 2025 年捐赠 Apache 孵化器，仓库由 `casbin/casbin` 迁至 `apache/casbin`，Go module path 随之从 `/v2` 升到 `/v3`。

| 版本 | 时间 | 要点 |
|---|---|---|
| v1.x | 2017–2019 | 无版本后缀的初始形态，PERM 元模型与 ACL/RBAC 定型 |
| v2.x（v2.1–v2.13x） | 2019–2025 | module `github.com/casbin/casbin/v2`，minor 号滚动累加；Adapter/Watcher/FilteredAdapter、ABAC、多域、缓存、GORM 生态在此期间成熟，是存量最大的主线 |
| v3.0.0 | 2025.12 | 捐赠后首个大版本。**破坏性变更只有 module path 改为 `github.com/casbin/casbin/v3`**：核心 API、model/policy 语法、Adapter 接口、性能特征全部与 v2 一致，迁移仅改 import 与 go.mod |
| v3.7–v3.9 | 2026 | `SetLogger` 自定义日志；`GetAllUsers()` 区分用户与角色；内置角色层级环检测防循环授权 |
| v3.10.0 | 2026.1.26 | `Explain()` API，调用 LLM 用自然语言解释授权结论 |
| v3.11.0 | 2026.8.20 | 当前主线：RBAC + ABAC 条件示例、`SavePolicy` CSV 逗号字段转义、域条件角色链接传递修复、Apache 孵化合规文件（DISCLAIMER/NOTICE/.asf.yaml） |

升级提示：v2 与 v3 的 `model.Model` 是不同 Go 类型，Adapter、Watcher、中间件必须同步换对应 major 版本，否则编译期类型不兼容。新项目直接用 v3；存量 v2 可长期不动，官方保持兼容承诺。

---

## 6. 常见坑与最佳实践

1. **丢弃 `Enforce` 的 error**：`ok, _ :=` 会把函数未注册、字段类型不符等错误吞成 `false`，表现为「莫名没权限」。对策：全链路检查 `err`，失败记日志返回 500，不与「拒绝」混淆；
2. **把 Casbin 当认证用**：它不校验密码、不存用户表、不管 token。对策：认证用 JWT/OIDC/Session，认证后把用户标识注入上下文，Casbin 只接最后一棒；
3. **自定义函数注册太晚**：matcher 表达式在首次 `Enforce` 时编译并缓存，之后再 `AddFunction` 对已缓存表达式无效。对策：初始化阶段、任何判定前完成全部注册；
4. **`in` 数组长度为 1 会 panic**：`r.obj in ('data2')` 在 Go 版触发 panic。对策：数组至少两元素，或改写成 `r.obj == 'data2' || ...`；
5. **无 Adapter 时调 `AddPolicy`**：`autoSave` 默认 true，会向 nil Adapter 增量写而 panic。对策：挂 Adapter，或先 `EnableAutoSave(false)`；
6. **多实例策略不同步**：A 改了库，B/C 仍是旧内存副本。对策：接 Watcher（etcd/redis），确认 `autoNotifyWatcher` 开启，变更走 `SavePolicy`/`AddPolicy` 而非直接改表；
7. **决策缓存与外部改库**：直接 UPDATE 策略表不触发 `policyVersion` 变化，缓存不失效。对策：变更经 Enforcer API + Watcher 广播 `LoadPolicy`，必要时 `InvalidateCache()`；
8. **策略爆炸**：给每用户逐条写 `p, alice, /menu/x, GET`，十万用户 × 百菜单压垮判定与存储。对策：RBAC 用 `g` 收敛、RESTful 用 `keyMatch2` 收敛路径、稳定判据上移 matcher，大租户上 `FilteredAdapter`；
9. **`keyMatch` 与 `keyMatch2` 混用**：`keyMatch` 只认 `*`，`/res/:id` 必须 `keyMatch2`（或 3/4），否则路径参数永远匹配不上。对策：RESTful 模型统一 `keyMatch2`；
10. **读写并发无保护**：普通 Enforcer 的写 API 与 `Enforce` 并发会数据竞争。对策：写操作集中到初始化或加外部锁，或改用 `NewSyncedEnforcer`；只读 `Enforce` 可放心并发。另注意 `EnableEnforce(false)` 会让 `Enforce` 恒返回 true（仅测试用），过滤加载的实例 `SavePolicy` 会报 `cannot save a filtered policy`（需读写分离）；

---

## 7. 选型对比

| 维度 | Casbin | OPA / Rego | Casdoor | 自研 RBAC | Keycloak | Zanzibar 风格（OpenFGA/SpiceDB） |
|---|---|---|---|---|---|---|
| 定位 | 嵌入式授权库 | 通用策略引擎 | IAM（认证+授权+后台） | 业务代码里的权限表 | 企业 IAM / SSO | 关系型授权（ReBAC）服务 |
| 策略表达 | PERM CONF + CSV/DB 规则 | Rego 声明式语言 | 内嵌 Casbin 模型 | SQL / if-else | 策略 + 权限服务 | 关系元组 `user:alice viewer doc:1` |
| 部署形态 | 进程内，零依赖 | sidecar / 嵌入 / 服务 | 独立服务 | 无 | 独立服务（较重） | 独立服务 / 云 |
| 是否做认证 | **否（只授权）** | 否 | **是** | 常混在一起 | **是** | 否 |
| 关系图谱/传递闭包 | 有限（`g` 层级） | 可表达 | 有限 | 弱 | 一般 | **最强** |
| 学习成本 | 低-中 | 中-高（要学 Rego） | 低（开箱即用） | 低 | 高 | 高 |
| 适合 | 应用内 RBAC/ABAC、多租户、网关鉴权 | 跨系统、K8s 准入、复杂策略 | 要现成登录/用户管理 | 权限极简且不演化 | 企业级 SSO/联邦身份 | 十亿级细粒度关系（文档共享、组织继承） |

一句话决策：**应用内嵌、模型清晰、要快速落地的 RBAC/ABAC 选 Casbin；策略逻辑复杂且要跨服务统一上 OPA；要现成登录与用户管理看 Casdoor/Keycloak；权限本质是「谁能看谁的文档」这类关系图谱时上 ReBAC（OpenFGA/SpiceDB）**。

何时不该用 Casbin：需要认证、用户目录或 SSO（它是授权层，不是身份系统）；授权依赖大规模关系传递与反向查询（「列出 alice 能看的所有文档」）——这是 ReBAC 的领域；要求集中式策略控制面、跨多语言服务统一审计灰度；以及策略一致性必须强一致而非 Watcher 的最终一致。反之，只要判据能收敛成「模型 + 少量规则」，Casbin 的成本几乎总是最低的。

参考：[Apache Casbin 官网](https://casbin.org/)、[GitHub 仓库与 Releases](https://github.com/apache/casbin/releases)、[v2 → v3 迁移指南](https://casbin.org/docs/upgrading-casbin-go-v2-to-v3/)、[pkg.go.dev API](https://pkg.go.dev/github.com/casbin/casbin/v3)。
