# Viper 技术介绍与使用指南

> Go 生态最流行的配置管理库：把 flag、环境变量、配置文件、远程 KV 与代码内默认值收口成一个「带优先级的配置注册表」，业务只调 `Get*` 而不必关心值来自哪一层。
> 仓库：<https://github.com/spf13/viper>　|　当前主线：**v1.21.0**（2025.09.08 发布，MIT 协议；截至 2026-11 仍为最新稳定版）

---

## 1. 项目简介

Viper 由 spf13（也是 Cobra 作者）2014 年创建，30k+ star，与 Cobra 配对后成为 Go CLI / 服务端的事实标准配置层。它的定位不是「又一个 YAML 解析器」，而是**一层带优先级的配置注册表**：解析交给各格式库，Viper 负责把多个来源叠起来、按既定顺序裁决「同一个 key 用谁的值」，再对外提供统一的 `Get*` 取值面。它把配置拆成五件事：多路径查找与多格式加载、六层优先级覆盖、key 归一化与别名、类型宽松转换与 `Unmarshal` 结构解码、基于 fsnotify 的热加载。

**适用**：CLI（与 Cobra 组合）、12-Factor 应用、「文件给默认 + env 给部署差异 + flag 给临时覆盖」的服务端程序、多格式并存的历史项目。**不适合**：只读一两个环境变量的极小程序；要求编译期强类型、配置即代码且零反射；把 Viper 当配置中心客户端做动态推送；高 QPS 热路径里逐次 `Get`（每次都走一遍优先级链与反射）。

> 与 [etcd 指南](../etcd/etcd-guide.md) 正好位于「配置从哪来」的两端：etcd 讲远端存储与 Watch，Viper 讲本地裁决。重载日志建议统一走 [slog 指南](../slog/slog-guide.md) 的结构化输出，与 [Gin 指南](../gin/gin-guide.md) 的启动日志对齐。

### 安装与最小示例

```bash
go get github.com/spf13/viper@v1.21.0
```

```go
package main

import (
    "fmt"

    "github.com/spf13/viper"
)

func main() {
    v := viper.New()                  // 不用包级全局单例
    v.SetDefault("server.port", 8080) // 第 6 层
    v.Set("server.port", 9090)        // 第 1 层：显式 Set 永远赢
    fmt.Println(v.GetInt("server.port"), v.GetInt("SERVER.PORT")) // 9090 9090，大小写不敏感
    fmt.Println(v.GetString("server.port"))                       // "9090"，cast 隐式转换
}
```

---

## 2. 功能特性总览

| 能力 | 说明 |
|---|---|
| 配置格式 | JSON / TOML / YAML / HCL / INI / envfile / Java properties（v1.20 起部分改为可插拔 codec） |
| 文件查找 | `SetConfigName` + 多个 `AddConfigPath`；或 `SetConfigFile` 指定绝对路径；`SetConfigType` 处理无扩展名 / 流 |
| 优先级链 / 键 | 显式 `Set` > flag > env > config > key/value remote store > default（固定，不可重排）；`RegisterAlias`、key 统一小写且大小写不敏感、`.` 分隔嵌套路径 |
| 默认值 / flag | `SetDefault` 兜底；`BindPFlag` 绑 pflag，v1.21.0 起支持 BoolSlice / UintSlice / Float64Slice |
| 环境变量 | `SetEnvPrefix` / `AutomaticEnv` / `SetEnvKeyReplacer` / `BindEnv` / `AllowEmptyEnv` |
| 远程 KV | etcd / Consul / Firestore，独立 module，不 watch、需显式启用 |
| 取值 / 存在性 | `Get` / `GetString` / `GetInt` / `GetBool` / `GetDuration` / `GetStringSlice` / `GetStringMapString`（底层 `spf13/cast`）；`IsSet`（全链）、`InConfig`（只查 config 层） |
| 结构解码 | `Unmarshal` / `UnmarshalKey` / `UnmarshalExact`，基于 `go-viper/mapstructure/v2`，支持 `DecodeHook` / `,squash` / `,remain`；`Sub`（可能为 `nil`）、`AllKeys` / `AllSettings` 一次性快照 |
| 热加载 / 写入 | `WatchConfig` + `OnConfigChange`（fsnotify）；`WriteConfig` / `SafeWriteConfig` 回写 |
| 实例模型 | `viper.New()` 多实例隔离；包级 `viper.*` 是全局单例，读写不加锁 |

---

## 3. 核心概念与原理

### 3.1 六层注册表与 `find()` 的执行路径

一个 `*viper.Viper` 内部不是「一份合并后的大 map」，而是**六张并列的来源表**，取值时才逐层裁决：

```text
*viper.Viper
├── override  map[string]interface{}   ← Set()            优先级 1（最高）
├── pflags    map[string]FlagValue     ← BindPFlag()      优先级 2
├── env       map[string][]string      ← BindEnv()        优先级 3
├── config    map[string]interface{}   ← ReadInConfig()   优先级 4
├── kvstore   map[string]interface{}   ← remote           优先级 5
└── defaults  map[string]interface{}   ← SetDefault()     优先级 6（兜底）
        ↑ aliases map[string]string（入口重定向，不复制数据）
```

核心是内部方法 `find(lcaseKey, flagDefault)`：别名解析后**按上表顺序短路返回第一个命中值**。由此推出几条易忽略的行为：

- **flag 层特殊**：绑定的 pflag 只有 `flag.Changed == true`（用户真传了 `--port`）才算命中；未改动的 flag 先被跳过，让 env / config 生效。只有整条链都没值时才以 `flagDefault=true` 重跑一次取 `DefValue`——所以 flag 默认值**不会**盖住配置文件里显式写的值，这与直觉相反。
- **`Set` 不会被 `ReadInConfig` 冲掉**：重读文件只替换 `config` 表，热加载后 override 依旧最高。
- **每层独立存储、取值才合并**；`AllSettings()` 是「按 `AllKeys()` 逐 key 调 `Get`」的一次性快照，不是实时视图；**短路判定是「非 nil」**，config 里写了 `port: 0`，env 就没机会覆盖它（见 3.4）。

### 3.2 key 归一化、大小写不敏感与嵌套路径

所有 key 进 `find` 前先 `strings.ToLower`，加载配置时 `insensitiviseMap` 还会递归把 `config` / `kvstore` / `defaults` 的 map 键全部小写化。官方 README 明确 **所有 key 大小写不敏感**：这是为兼容 env 变量与不同格式文件的大小写差异，社区多次尝试做大小写敏感都不了了之，只能留到 v2 作可选项。

`keyDelim` 默认 `"."`，`find` 用 `strings.Split` 切段、逐段 `cast.ToStringMap` 下钻嵌套 `map[string]interface{}`；任一层不是 map 就返回 `nil`。两个后果：`db.host` 与 `DB.HOST` 是同一项，但**「含点的扁平 key」与「嵌套 map」在 `Get` 时无法区分**（`AllSettings()` 展开结构却不同）；而 **env 层不做路径拆分**，Viper 把整个 key 连同 `.` 拼前缀后整体大写，`database.host` → `MYAPP_DATABASE.HOST`——这正是 `SetEnvKeyReplacer` 存在的唯一理由。

### 3.3 环境变量的三条路径与「不可枚举」边界

- **`AutomaticEnv()`**：不登记任何 key，只在 `find` 的 env 环节按「前缀 + 大写 key」实时 `os.LookupEnv`。零配置，但 **Viper 无法枚举任意环境变量**——`AllKeys()` / `AllSettings()` / `Unmarshal()` / `Sub()` / `WriteConfig` 都看不到仅靠它注入的项，只有显式 `Get*` / `IsSet` 能取到。
- **`BindEnv(key, envName...)`**：把 key → 具体变量名写进 `v.env` 表，可被 `AllKeys()` 枚举，因而能进 `Unmarshal` 快照；支持多候选名依次回退；`AllowEmptyEnv` 默认 `false`，空串视为未设置继续下探。
- **`SetEnvKeyReplacer(r)`**：纯**名字变换器**，不决定是否启用 env。必须与 `SetEnvPrefix` + `AutomaticEnv`（或 `BindEnv`）同时设置，缺一个就拼出 `MYAPP_DATABASE.HOST` 这种永远匹配不上的名字。

一句话：**Replacer 管「怎么拼名」，BindEnv 管「登记哪些 key 可枚举」，AutomaticEnv 管「是否对所有 key 自动按规则找」**。生产最稳组合是 prefix + replacer + AutomaticEnv 负责通用覆盖，再对**需要进结构体的敏感键**补一条 `BindEnv`。

### 3.4 `Get*` / cast 隐式转换与 `IsSet` 的零值语义

`Get` 返回 `interface{}`，`GetString` / `GetInt` / `GetBool` / `GetDuration` / `GetStringSlice` 全委托 `spf13/cast`（v1.21.0 为 cast 1.9.2）。于是 `port: "8080"` 能被 `GetInt` 读出 8080、`debug: "false"` 能被 `GetBool` 读成 false；但 `"abc"` 交给 `GetInt` 会**静默得到 0 不报错**，`GetString` 又把 `nil`→`""`、数字→十进制串、bool→`"true"/"false"`。这种「跨来源一律宽松转换」极易把脏配置、空值与合法零值混在一起。

`IsSet(key)` 的实现就是 `Get(key) != nil`：`enabled: false`、`port: 0`、`list: []` 都算**已设置**（非 nil）并短路低层；但 AutomaticEnv + `AllowEmptyEnv=false` 下环境变量为空串时 `IsSet` 为 false 并继续下探到 default；不存在的嵌套父键 `Get` 返回 nil。**别用 `IsSet` 判断「配置文件里有没有这一节」**，那要用 `InConfig(key)`（只查 config 层，不看 env / override）；判断「用户是否显式传了 flag」应看 `flag.Changed`。

### 3.5 `Unmarshal`：AllSettings 快照 + mapstructure 解码链

`Unmarshal(&cfg, opts...)` 不直接读 config：先 `AllSettings()` 生成 `map[string]interface{}` 快照，再交给 `github.com/go-viper/mapstructure/v2` 解码。**快照是全部坑的源头**：`AllKeys()` 只来自 override + 已绑定 flag + 显式 `BindEnv` + config + kvstore + defaults 的并集，AutomaticEnv 的项不在其中——「环境变量明明覆盖了文件，但 `Unmarshal` 出来的结构体还是旧值」是最高频事故。

- **key 匹配**：mapstructure 默认按字段名不区分大小写比较，但**不会把下划线转驼峰**，`log_level` 不会自动映射到 `LogLevel`；嵌套结构每一层都要显式 `mapstructure:"..."` tag。
- **零值覆盖**：解码是「用快照的值覆盖目标」，快照缺失的字段**保留目标原有值而非清零**。热加载复用同一个 `&cfg` 反复 `Unmarshal`，被删掉的键会残留旧值；须每次 `var next Config` 全新解码、校验通过后整体替换。
- **解码扩展 / 包路径**：`DecoderConfigOption` 可注入 `DecodeHook`（`time.Duration` / `net.IP` / 逗号串转切片）、开 `ErrorUnused` 拒绝多余键、`,squash` 展开内嵌、`,remain` 收集未知键；v1.20 起从 `mitchellh/mapstructure` 迁到 fork `go-viper/mapstructure/v2`（v1.21.0 用 v2.4.0），自定义 hook 的 import 必须跟着改，否则类型不匹配编译失败。

### 3.6 热加载：fsnotify 监听 inode 与并发模型

`WatchConfig()` 起一个后台 goroutine，用 `fsnotify.NewWatcher()` **监听配置文件所在目录**，事件到达后先内部 `ReadInConfig()` 刷新 `config` 表，再调 `OnConfigChange` 回调。关键在 **fsnotify 监听的是 inode 上的事件**：vim / neovim / 多数编辑器与配置下发工具采用「写临时文件 + `rename` 覆盖」的原子写，`rename` 换掉被监听的 inode，监听随之失效，事件可能表现为 `Rename` / `Remove` 而非 `Write`，或一次保存触发两次——典型现象是「改了文件，回调再也不触发」。K8s ConfigMap 用「目录 + `..data` 符号链接轮换」正是绕开这点；自研下发应监听目录并对 `Create` / `Rename` 重新 `Add`，测试一律用 `os.WriteFile` 直接覆写，别用编辑器 `:w`。

并发上两条硬约束：**回调运行在 watcher goroutine**，与业务 goroutine 共享的状态必须 `sync.RWMutex` / `atomic.Value` 整体替换；回调只代表「文件变了」，**不代表新配置已生效**，必须先 `Unmarshal` 到临时变量并校验，失败保留旧值。Viper 实例本身**不并发安全**（README 明确并发读写会 panic），所以别在去抖定时器里异步回读 Viper，应在回调内同步取完快照。`WatchConfig` 没有公开的 Stop，监听生命周期应与进程一致。

---

## 4. 使用实战

### 4.1 配置文件与初始化

```yaml
# conf/config.yaml
port: 8080
log_level: info
timeout: 5s
```

下面是一份完整可编译的装配代码，涵盖默认值、env、flag、文件、`ConfigFileNotFoundError`、强类型解码与启动校验：

```go
package main

import (
    "errors"
    "fmt"
    "log"
    "strings"
    "time"

    "github.com/go-viper/mapstructure/v2"
    "github.com/spf13/pflag"
    "github.com/spf13/viper"
)

type Config struct {
    Port     int           `mapstructure:"port"`
    LogLevel string        `mapstructure:"log_level"` // 下划线必须显式 tag
    Timeout  time.Duration `mapstructure:"timeout"`   // "5s" 由 Viper 默认钩子转 Duration
    Secret   string        `mapstructure:"secret"`    // 只由 BindEnv 登记，才能进 Unmarshal 快照
}

// newConfig 返回隔离实例 + 已校验配置，绝不使用包级 viper 单例。
func newConfig(args []string) (*viper.Viper, Config, error) {
    v := viper.New()
    v.SetDefault("log_level", "info") // 第 6 层
    v.SetDefault("timeout", "5s")
    // 第 3 层：prefix + replacer + AutomaticEnv 成组；只在 env 里存在的键必须 BindEnv 才可被枚举。
    v.SetEnvPrefix("MYAPP")
    v.SetEnvKeyReplacer(strings.NewReplacer(".", "_", "-", "_"))
    v.AutomaticEnv()
    if err := v.BindEnv("secret", "MYAPP_SECRET"); err != nil {
        return nil, Config{}, err
    }
    // 第 2 层：flag 必须在 Parse / Cobra Execute 之前绑定。
    fs := pflag.NewFlagSet("app", pflag.ContinueOnError)
    fs.Int("port", 8080, "listen port")
    if err := fs.Parse(args); err != nil { return nil, Config{}, err }
    if err := v.BindPFlag("port", fs.Lookup("port")); err != nil {
        return nil, Config{}, err
    }
    // 第 4 层：只有「找不到文件」可降级，语法错 / 权限错必须暴露。
    v.SetConfigName("config")
    v.AddConfigPath("./conf")
    if err := v.ReadInConfig(); err != nil {
        var notFound viper.ConfigFileNotFoundError
        if !errors.As(err, &notFound) {
            return nil, Config{}, fmt.Errorf("读取配置失败: %w", err)
        }
        log.Println("未找到配置文件，改用 flag / env / default")
    }
    var cfg Config // 全新变量，避免 3.5 的零值残留
    err := v.Unmarshal(&cfg, func(dc *mapstructure.DecoderConfig) {
        dc.ErrorUnused = true
    })
    if err != nil { return nil, Config{}, err }
    if cfg.Port <= 0 || cfg.Port > 65535 {
        return nil, Config{}, fmt.Errorf("非法 port: %d", cfg.Port)
    }
    return v, cfg, nil
}

func main() {
    v, cfg, err := newConfig([]string{"--port=9090"})
    if err != nil { log.Fatal(err) }
    fmt.Println("port =", v.GetInt("port"), "file =", v.ConfigFileUsed())
    fmt.Printf("cfg  = %+v\n", cfg)
}
```

> `errors.As` 必须用具体类型变量（`ConfigFileNotFoundError` 是 struct），值比较是错的。`GetInt` 的隐式 cast 只用于日志 / 展示，业务值一律走上面的强类型结构体。`AutomaticEnv` / `SetEnvKeyReplacer` / `BindEnv` 的差别见 3.3：前者实时按规则查但不可枚举，中者只改名字，后者登记的 key 才会进 `Unmarshal` 快照。Viper 默认已注入 `StringToTimeDurationHookFunc` + `StringToSliceHookFunc(",")`，所以 `"5s"` 能直接进 `time.Duration`；自定义钩子要用 `viper.DecodeHook(mapstructure.ComposeDecodeHookFunc(...))` **叠加**，直接赋值 `dc.DecodeHook` 会把默认钩子覆盖掉。

### 4.2 热加载：去抖 + 校验 + 原子替换

```go
package main

import (
    "fmt"
    "log"
    "os"
    "sync"
    "time"

    "github.com/fsnotify/fsnotify"
    "github.com/spf13/viper"
)

type Config struct {
    LogLevel string `mapstructure:"log_level"`
    MaxConn  int    `mapstructure:"max_conn"`
}

func main() {
    path := "config.yaml"
    if err := os.WriteFile(path, []byte("log_level: info\nmax_conn: 10\n"), 0o644); err != nil {
        log.Fatal(err)
    }
    defer os.Remove(path)
    v := viper.New()
    v.SetConfigFile(path)
    v.SetDefault("max_conn", 10)
    if err := v.ReadInConfig(); err != nil { log.Fatal(err) }

    var mu sync.Mutex // 同时保护 current 与去抖状态
    var current Config
    if err := v.Unmarshal(&current); err != nil { log.Fatal(err) }
    var timer *time.Timer
    var pending Config
    v.OnConfigChange(func(e fsnotify.Event) {
        // Viper 不并发安全：同步取快照，别把 Unmarshal 丢进定时器 goroutine。
        var next Config // 全新变量，避免删键残留旧值（3.5）
        if err := v.Unmarshal(&next); err != nil {
            log.Println("解码失败，保留旧值:", err); return
        }
        if next.MaxConn <= 0 || next.MaxConn > 10000 {
            log.Println("校验失败，保留旧值: max_conn 越界"); return
        }
        mu.Lock()
        pending = next
        if timer != nil { timer.Stop() }
        timer = time.AfterFunc(100*time.Millisecond, func() {
            mu.Lock()
            c := pending
            current = c
            mu.Unlock()
            log.Printf("配置已生效: %+v (event=%s)", c, e.Op)
        })
        mu.Unlock()
    })
    v.WatchConfig()
    time.Sleep(50 * time.Millisecond) // 等 watcher 就绪
    // os.WriteFile 直接覆写保留 inode；编辑器 :w 的原子 rename 会丢监听。
    if err := os.WriteFile(path, []byte("log_level: debug\nmax_conn: 50\n"), 0o644); err != nil {
        log.Fatal(err)
    }
    time.Sleep(400 * time.Millisecond)
    mu.Lock()
    fmt.Println("当前生效配置:", current)
    mu.Unlock()
}
```

> 测试隔离：每个用例 / 每个包都用 `viper.New()`，把 `*viper.Viper` 作为依赖显式传入；全局单例虽还有 `Reset()`，但清不掉已启动的 watcher 与其它 goroutine 的引用，库代码绝不隐式依赖它。

---

## 5. 版本演进与要点

已核对上游 Releases / Tags 页：**v1.21.0 于 2025-09-08 发布（tag 签名 09-09），是当前最新稳定版，MIT 协议**；截至 2026-11 主线未再发新版，README 正公开征集 v2 反馈。v1.21.0 最低 Go 版本 1.23。

| 版本 | 时间 | 要点 |
|---|---|---|
| v1.19.0 | 2024 | v1.18 系列稳定性收尾，弃用过时内部 API |
| **v1.20.0** | 2025.03 | **引入可插拔 encoding 层**；HCL / INI / Java properties 从内置解码器移除；**remote 拆为独立 Go module**；Finder 取代旧文件搜索 API；新增 `DecodeHook` 选项、实验性 `BindStruct`；放弃 Go 1.20 |
| **v1.21.0** | **2025.09.08** | 支持 `pflag.BoolSlice/UintSlice/Float64Slice`；切换到维护中的 YAML 库 `go.yaml.in/yaml/v3`；修复 `configType` 与扩展名判定；升级指南补 mapstructure 包替换；依赖升至 fsnotify 1.9.0 / cast 1.9.2 / mapstructure v2.4.0 / pflag 1.0.10 |
| v2（规划中） | 未发布 | 方向：大小写敏感可选项、配置源插件化、远程层彻底剥离 |

升级要点：v1.19 → v1.20 是最需读 changelog 的一跳——自定义 `DecodeHook` 的 import 要从 `mitchellh/mapstructure` 改为 `go-viper/mapstructure/v2`；用到 INI / HCL / properties 的项目按新 encoding 层注册 codec；远程配置需单独 `go get github.com/spf13/viper/remote` 并空导入注册 provider（该模块有独立 `remote/v1.21.0` 标签）；CI 工具链升到 Go 1.23+。除这些 breaking changes 外 v1 API 保守，升级基本无痛。

---

## 6. 常见坑与最佳实践

1. **只设 `AutomaticEnv` 忘了 `SetEnvKeyReplacer`**：`database.host` 拼成 `MYAPP_DATABASE.HOST` 永远匹配不上；→ 对策：`SetEnvPrefix` + `strings.NewReplacer(".", "_", "-", "_")` + `AutomaticEnv` 成组出现。
2. **以为环境变量的值会进 `Unmarshal`**：AutomaticEnv 项不可枚举，`AllSettings` 快照里没有它；→ 对策：需进结构体的键补 `BindEnv`，或 `SetDefault` 占位后启动显式 `Get*` 覆盖。
3. **结构体字段漏 `mapstructure` tag**：`log_level` 不会自动映射到 `LogLevel`，嵌套每级同理；→ 对策：每字段都写 tag，开 `ErrorUnused` 暴露拼写错误。
4. **复用同一个 `&cfg` 反复 `Unmarshal`**：删掉的键残留旧值（缺失字段不清零）；→ 对策：每次 `var next Config` 全新解码 + `validate` 通过后整体替换。
5. **全局 `viper.*` 单例并发读写 / 测试串味**：官方明确并发读写会 panic，包级状态在用例间泄漏；→ 对策：一律 `viper.New()`，写侧加锁或 `atomic.Value` 存整份配置，测试每例新实例。
6. **用 `IsSet` 判断「配置里有没有这一节」或混淆零值**：`port: 0` / `enabled: false` 都算已设置并短路低层，空串在 `AllowEmptyEnv=false` 下又算未设置；→ 对策：查文件用 `InConfig`，查 flag 用 `flag.Changed`，必填项解码后单独 `validate`。
7. **依赖 `GetString` / `GetInt` 的隐式 cast**：`"abc"` → `GetInt` 静默变 0，脏配置隐藏到运行期；→ 对策：启动即 `Unmarshal` 到强类型结构体、关闭 `WeaklyTypedInput`，把类型错误挡在启动阶段。
8. **`ReadInConfig` 报错就 `log.Fatal`**：纯 flag / env 部署没有配置文件是合法的；→ 对策：`var e viper.ConfigFileNotFoundError; errors.As(err, &e)` 只降级「找不到文件」，语法错 / 权限错照常抛。
9. **`WatchConfig` 在编辑器保存后不再触发**：vim 等原子写 `rename` 换掉 inode，fsnotify 监听失效；→ 对策：监听目录并对 `Create` / `Rename` 重新 `Add`，配置下发用「目录 + 符号链接轮换」，测试用 `os.WriteFile` 而非 `:w`。
10. **`OnConfigChange` 里裸改共享变量、不校验、不去抖**：与业务 goroutine 数据竞争，一次保存触发多次重载，非法配置直接生效；→ 对策：`RWMutex` 整体替换、校验失败保留旧值、`time.AfterFunc` 100ms 去抖，且**同步**取 Viper 快照后再进定时器。
11. **flag 在 `RunE` 里才 `BindPFlag`**：Cobra 进入 `RunE` 前已解析完 flag，绑定太晚取值全落空；未 `Changed` 的 flag 默认值也不会盖住配置文件（3.1）；→ 对策：在 `init()` / `fs.Parse` 之前绑定，理解「显式 flag > env > config > flag 默认值」。
12. **照旧教程直接调 `AddRemoteProvider` 连 etcd/Consul**：v1.20 起 remote 已是独立 module，Viper **不 watch 远端变更**，`Unmarshal` 也不响应后续 `Set`；→ 对策：用官方 SDK 主动拉 + Watch，回调里 `viper.ReadConfig(bytes.NewReader(data))` 注入后重新解码（参见 [etcd 指南](../etcd/etcd-guide.md)），别把配置中心做成业务链路的同步依赖。

---

## 7. 选型对比

| 维度 | Viper | 标准库 `flag` + `encoding/json` | koanf | envconfig | Go 1.x 配置实践 |
|---|---|---|---|---|---|
| 定位 | 多来源优先级注册表 | 单来源，自己拼装 | 可组合的配置管线 | 只做 env → struct | `os.Getenv` + struct tag + 校验 |
| 优先级链 / 格式 | **内建 6 层，固定顺序**；JSON/TOML/YAML/envfile 等 | 手写 if/else；自己接格式 | Provider 顺序自定义 + 插件化 | 无格式支持 | 手写 |
| 环境变量 | 强（prefix/replacer/BindEnv） | `os.Getenv` 手写 | provider | **最专注** | 原生 |
| 热加载 | `WatchConfig`（fsnotify） | 无 | 可插拔 watch | 无 | 无 |
| 结构体解码 | mapstructure，宽松 | `json.Unmarshal`，严格 | 可换 decoder | struct tag | 手写 / 第三方校验 |
| 类型安全 / 依赖体积 | 弱（cast 隐式转换）/ 较大（17+ 间接依赖） | **强 / 零** | 中～强 / 小～中 | 强 / 极小 | 强 / 零 |
| 并发安全 / 维护 | **否**（需外部同步）；v1 稳定、v2 筹备中 | 取决于自己（标准库） | 取决于 provider（活跃） | 只读结构体安全（维护期） | 自己保证（标准库） |
| 适合 | 多来源、多格式、CLI + 服务 | 简单工具、只想零依赖 | 想精确控制加载管线 | 纯 12-Factor / 容器 | 配置极少的小程序 |

**一句话决策：按「来源数量」选——只有环境变量就 `envconfig` 或标准库，要「文件 + env + flag」分层覆盖且接受宽松类型就 Viper，想自己精确编排加载管线且在意依赖体积就 koanf；Viper 的价值在优先级链与 Cobra 生态，不在解析能力。**

何时不该用：配置只有几个环境变量（容器化 12-Factor，`envconfig` 更直白）；要求编译期强类型、配置即代码（Viper 的隐式转换与大小写不敏感会掩盖拼写 / 类型错误，改走 `encoding/json` + `validator`）；把 Viper 当配置中心客户端做动态推送（它不 watch 远端，远程能力还在被讨论剥离）；超高 QPS 热路径逐次取值（应启动时解码一次并缓存）；库代码隐式依赖全局单例（污染宿主进程状态）；需要大小写敏感的 key（当前做不到，见 3.2，只能等 v2 或换库）。

参考：[官方 README 与 FAQ](https://github.com/spf13/viper)、[Releases / Changelog](https://github.com/spf13/viper/releases)、[go-viper/mapstructure v2](https://github.com/go-viper/mapstructure)。
