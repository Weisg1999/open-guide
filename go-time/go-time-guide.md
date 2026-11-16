# 时区与时间处理技术指南

> 时间处理的三个坑：把「时刻」当「墙上时间」，把本地时区当 UTC，把夏令时当成固定偏移。
> 标准库：<https://pkg.go.dev/time> · <https://pkg.go.dev/time/tzdata>（内嵌 IANA 数据）　|　适用 **Go 1.27.4**（2026-11-10），IANA tzdata 2026 版

---

## 1. 项目简介

这篇讲的不是第三方库，而是标准库 `time` 与它背后的 IANA 时区数据库。它是所有 Go 服务的地基——日志、订单、审计、定时任务、报表日切都建在它上面，也因此「差 8 小时」「少一天」「任务没跑」的事故，几乎都能回溯到这里的某个误解。三篇的分工：cron 篇回答「到点怎么触发」，只用到 `time.Location` 一个参数；MySQL 篇回答「时间字段怎么存」，只用到 DSN 里的 `loc` 一个参数；本篇回答**这两个参数背后是什么**，为什么同一个 `time.Time` 能打印出两个钟点、为什么夏令时那天任务少跑一次。

`time` 包的定位可以概括为一句话：**它同时表示「时刻」和「墙上时间」，允许你自由转换，但不会替你判断该用哪个**。判断错了标准库不报错，只会安静地给你一个偏移 8 小时的结果。

```go
package main

import (
	"fmt"
	"time"
)

func main() {
	loc, err := time.LoadLocation("Europe/Berlin")
	if err != nil {
		panic(err) // 容器缺 tzdata 时在这里暴露
	}
	now := time.Now()
	fmt.Println(now.UTC().Format(time.RFC3339))
	fmt.Println(now.In(loc).Format(time.RFC3339))
}
```

---

## 2. 能力总览

| 能力 | 说明 |
|---|---|
| 时刻表示 | `time.Time` 同时携带墙钟读数、单调读数与 `*Location` |
| 时区加载 | `LoadLocation` 按 `ZONEINFO` → 系统目录 → 内嵌数据 → `$GOROOT` 顺序查找 |
| IANA 数据 | `time/tzdata` 把完整数据库编进二进制（约 +450 KB），也可 `-tags timetzdata` |
| 夏令时 | 内置过渡规则；`Zone()` 给偏移与缩写，`IsDST()` 判断是否处于夏令时 |
| 格式化 | layout 是参考时刻 `Mon Jan 2 15:04:05 MST 2006`，不是格式串 |
| 解析 | `Parse` 无时区信息时返回 UTC；`ParseInLocation` 指定时区 |

---

## 3. 核心概念与原理

### 3.1 `time.Time` 的内部表示：为什么 `==` 不可靠

```text
type Time struct {
    wall uint64   // 1 bit 有无单调读数 | 33 bit 秒 | 30 bit 纳秒
    ext  int64    // 有单调读数：进程启动以来的秒；否则：公元 1 年起的秒
    loc  *Location
}
```

三个直接后果：

1. **`==` 比的是三个字段，不是「同一瞬间」**。`UTC` 与 `FixedZone("UTC", 0)` 指向不同 `loc`，同一瞬间也不相等。
2. **单调读数让 `==` 更不可靠**：`time.Now()` 带单调读数，数据库读回来的不带，墙钟一致也 `!=`。同理 `time.Time` 不适合做 map key，必须做键时先 `t.UTC().Round(0).Format(time.RFC3339Nano)`。
3. **`Equal` 才是「同一瞬间」**，它忽略 `loc` 与单调读数；`Compare`（Go 1.20+）可直接排序。

```go
a := time.Now()
b := a.Round(0)                      // Round(0) 剥掉单调读数，Add(0) 不会
fmt.Println(a == b, a.Equal(b))      // false true
c := a.In(time.FixedZone("UTC", 0))
fmt.Println(a == c, a.Equal(c))      // false true
```

> 单调读数是进程私有的，无跨进程意义，序列化、写日志、入库时都会丢。**跨进程边界的时间一律用 `Equal`/`Before`/`After`，绝不用 `==`**；需要显式剥离时用 `t.Round(0)`。

### 3.2 时刻（instant）与墙上时间（civil time）

这是全文的地基，两者混淆是所有时区 bug 的源头。

| | 时刻 instant | 墙上时间 civil |
|---|---|---|
| 本质 | 时间轴上的一个点 | 「年月日时分秒 + 时区规则」 |
| 唯一性 | 全局唯一 | 同一钟点可能对应 0 个或 2 个时刻 |
| 表达 | Unix 纳秒、`2026-11-16T03:00:00Z` | `2026-11-16 09:00 Europe/Berlin` |
| 换时区 | 不变 | 变成另一个时刻 |

判断规则只有一条：**问「宇宙中哪一刻」用 instant，问「当地钟表指到几点」用 civil**。事件发生时间存 UTC，展示时转 civil；定时规则必须带 IANA 时区名，不能用固定偏移。

### 3.3 `Location` 与 IANA 数据库的查找顺序

`*Location` 的本质是一张**过渡表**：若干 `zone{名称, 偏移, 是否 DST, 起止时刻}` 加未来规则。它不是「当前偏移」一个数字，所以 `FixedZone("CST", 8*3600)` 永远替代不了 `Asia/Shanghai`——后者在 1986–1991 年实行过夏令时，前者不知道。

`LoadLocation(name)` 的顺序（`""` 返回 UTC，`"Local"` 返回进程本地时区）：

1. `$ZONEINFO` 指向的目录或 zip；
2. 系统目录 `/usr/share/zoneinfo`、`/usr/share/lib/zoneinfo`、`/usr/lib/locale/TZ`、`/etc/zoneinfo`；
3. 导入了 `time/tzdata` 时的**内嵌数据**；
4. `$GOROOT/lib/time/zoneinfo.zip`。

两个容易踩反的点：

- **内嵌数据是兜底，不是优先**。镜像里有 `/usr/share/zoneinfo` 就用镜像那份，哪怕它是三年前的；升级 Go 不会让运行中的容器换数据。IANA 2026 年就改过加拿大几个省（Manitoba 自 2026-10-31 永久 -05，西北地区自 2026-08-21 永久 -06），旧数据会让「本地 9 点」的任务整整偏一小时，而 Go 不报任何错。
- **Go 没有 API 告诉你加载的是哪版 tzdata**（Node 有 `process.versions.tz`，Python 有 `tzdata.IANA_VERSION`）。`Local` 也不是常量：Linux 来自 `TZ` 或 `/etc/localtime`，Windows 来自注册表。它是「部署机时区」，不是业务时区。

### 3.4 夏令时：不存在的钟点与重复的钟点

| 本地日期（Europe/Berlin） | 变化 | 结果 |
|---|---|---|
| 2026-03-29 02:00 CET | 拨快到 03:00 CEST | `02:00–02:59` **不存在** |
| 2026-10-25 03:00 CEST | 拨慢到 02:00 CET | `02:00–02:59` **出现两次** |

于是 `2026-03-29 02:30` 对应 **0 个** instant，`2026-10-25 02:30` 对应 **2 个**（先 +02:00 后 +01:00）。`time.Date` 会归一化，但官方明确「时区选择与最终时刻不做保证」：

```go
loc, _ := time.LoadLocation("Europe/Berlin")
// 不存在的 02:30：当前实现向后落到 03:30 CEST
fmt.Println(time.Date(2026, 3, 29, 2, 30, 0, 0, loc)) // 2026-03-29 03:30:00 +0200 CEST
// 重复的 02:30：Go 1.27 实测取第二次，即标准时间那次
fmt.Println(time.Date(2026, 10, 25, 2, 30, 0, 0, loc)) // 2026-10-25 02:30:00 +0100 CET
```

工程做法：定时任务避开 `02:00–03:00`；必须在此窗口执行的先判断 civil 值是否存在；重复钟点靠幂等键去重，而不是假设「不会跑两次」；绝不用 `FixedZone("CET", 3600)` 表示柏林，它没有 DST 规则，半年全错。另外，偏移不一定整小时（`Asia/Kathmandu` +05:45、`Australia/Eucla` +08:45），历史 LMT 甚至带秒，任何 `offset/3600` 取整的代码都是错的。

### 3.5 格式化与解析：layout 是参考时刻

Go 用固定参考时刻做模板（`Mon Jan _2 15:04:05 MST 2006`），常用常量有 `RFC3339`、`RFC3339Nano`、`DateOnly`、`TimeOnly`、`DateTime`（后三者 Go 1.20+）。解析有三条铁律：

1. **`time.Parse` 在 layout 不含时区时返回 UTC**，不是本地时间；
2. **要按某时区解释必须用 `ParseInLocation`**；
3. **`Z07:00` 同时接受 `Z` 与 `+08:00`，`-07:00` 只接受数字偏移**。

```go
t1, _ := time.Parse("2006-01-02 15:04:05", "2026-11-16 09:00:00") // UTC 9 点
loc, _ := time.LoadLocation("Europe/Berlin")
t2, _ := time.ParseInLocation("2006-01-02 15:04:05", "2026-11-16 09:00:00", loc)
fmt.Println(t1.Format(time.RFC3339)) // 2026-11-16T09:00:00Z
fmt.Println(t2.Format(time.RFC3339)) // 2026-11-16T09:00:00+01:00
```

`RFC3339Nano` 会去掉小数末尾的零，既非定宽也不可按字典序排序：整秒时间不带小数段，而 `'Z'(0x5A)` 大于 `'.'(0x2E)`，所以 `...09:00:00Z` 会排在 `...09:00:00.5Z` 之后。需要排序就转 UTC 固定 9 位，或直接存整数。

### 3.6 数据库里的时间：语义由列类型决定

**MySQL**

| | `TIMESTAMP` | `DATETIME` |
|---|---|---|
| 存什么 | 内部按 UTC 存，读写用**会话 `time_zone`** 换算 | 原样存字面墙上时间 |
| 范围 | 1970-01-01 ~ **2038-01-19** UTC | 1000-01-01 ~ 9999-12-31 |
| 语义 | 接近 instant | 接近 civil，但不带时区名 |

驱动的两个参数分工常被搞混：`parseTime=true` 决定结果集是否扫成 `time.Time`；`loc=UTC` 决定驱动**编码参数、解析结果字符串**时用的时区。而 `TIMESTAMP` 的换算发生在**服务端**，由会话 `time_zone` 决定——只设 `loc` 不设 `time_zone`，就会出现「写进去对、读出来差 8 小时」。正确姿势是两边钉死：

```text
# %27%2B00%3A00%27 是 '+00:00' 的 URL 编码
user:pass@tcp(127.0.0.1:3306)/app?parseTime=true&loc=UTC&time_zone=%27%2B00%3A00%27
```

**PostgreSQL**：`timestamptz` 存绝对时刻（微秒），**不保存**你写入时的时区，只保存瞬间；`timestamp` 存字面墙上时间，无时区语义。`timestamptz` 的文本输出按会话 `TimeZone` 渲染，所以 PgBouncer 事务池下不要依赖会话级 `SET TIME ZONE`，只存 `timestamptz`。驱动扫描出的 `Location` 只是约定（pgx v5 可用 `TimestamptzCodec.ScanLocation` 指定），**不能当业务时区**，更不能对 `timestamp` 调 `.In()`——那只是给无时区的数硬贴标签。

---

## 4. 使用实战

场景：同一服务同时服务中国站（`Asia/Shanghai`）与德国站（`Europe/Berlin`）。事件存 UTC，报表按**站点本地日**统计，任务按**站点本地时间**触发。

### 4.1 站点模型与「本地日 → UTC 区间」

以下 4.1–4.4 同属一个 `timesvc` 包，后三节只列新增函数，省略 package 与 import。

```go
package timesvc

import (
	"context"
	"database/sql"
	"time"
)

type Site struct {
	Code string
	Loc  *time.Location
}

func NewSite(code, tz string) (Site, error) {
	loc, err := time.LoadLocation(tz)
	return Site{Code: code, Loc: loc}, err
}

// DayRange 把站点本地的某一天翻译成 UTC 半开区间 [start, end)。
func (s Site) DayRange(day string) (time.Time, time.Time, error) {
	d, err := time.ParseInLocation(time.DateOnly, day, s.Loc)
	if err != nil {
		return time.Time{}, time.Time{}, err
	}
	return d.UTC(), d.AddDate(0, 0, 1).UTC(), nil
}
```

`AddDate(0, 0, 1)` 加的是**日历日**，跨 DST 仍落在次日 00:00 本地；用 `Add(24*time.Hour)` 在切换日会偏一小时。柏林站点 `DayRange("2026-11-16")` 得到 `[2026-11-15T23:00Z, 2026-11-16T23:00Z)`，上海站点则是 `[2026-11-15T16:00Z, 2026-11-16T16:00Z)`。

### 4.2 写入：UTC 时刻 + 冗余本地日期

```sql
CREATE TABLE orders (
  id           BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  site         VARCHAR(8)      NOT NULL,
  amount_cents BIGINT          NOT NULL,
  created_at   DATETIME(6)     NOT NULL COMMENT 'UTC 时刻',
  created_date DATE            NOT NULL COMMENT '站点本地日',
  PRIMARY KEY (id), KEY idx_site_created (site, created_at), KEY idx_site_date (site, created_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

```go
func (s Site) InsertOrder(ctx context.Context, db *sql.DB, amountCents int64, at time.Time) error {
	utc := at.UTC().Truncate(time.Microsecond) // 对齐 DATETIME(6)，避免纳秒被静默截断
	_, err := db.ExecContext(ctx,
		`INSERT INTO orders (site, amount_cents, created_at, created_date) VALUES (?, ?, ?, ?)`,
		s.Code, amountCents, utc, at.In(s.Loc).Format(time.DateOnly))
	return err
}
```

这里 `Format(time.DateOnly)` 是**正确用法**：回答「这个时刻在当地是几号」。`created_date` 是写时冗余——多 3 字节，换掉每次查询的时区换算，并让按日聚合直接走索引；代价是业务日口径变更时需回填。

### 4.3 读取、展示与按本地日统计

```go
func (s Site) LoadOrder(ctx context.Context, db *sql.DB, id int64) (time.Time, int64, error) {
	var at time.Time
	var amount int64
	err := db.QueryRowContext(ctx,
		`SELECT amount_cents, created_at FROM orders WHERE id = ?`, id).Scan(&amount, &at)
	return at.In(s.Loc), amount, err // 驱动按 loc=UTC 解析，这里只转展示
}
```

对外 API 统一输出 RFC3339，并额外给 `"timezone"` 与 `"local_date"` 字段。前端用 `Intl.DateTimeFormat(..., { timeZone })` 渲染，**不要在前端做日期加减**——浏览器时区是用户设备的，不是站点的。统计复用同一套边界，只是把单条查询换成聚合：

```go
func (s Site) DailyTotal(ctx context.Context, db *sql.DB, day string) (int64, error) {
	start, end, err := s.DayRange(day)
	if err != nil {
		return 0, err
	}
	var total int64
	err = db.QueryRowContext(ctx,
		`SELECT COALESCE(SUM(amount_cents), 0) FROM orders
		  WHERE site = ? AND created_at >= ? AND created_at < ?`,
		s.Code, start, end).Scan(&total)
	return total, err
}
```

三条纪律：**半开区间** `>= start AND < end`（`BETWEEN` 闭区间易多算）；**不要 `DATE(created_at) = ?`**（函数包住列，索引失效）；**不要 `CONVERT_TZ(...)`**（依赖 MySQL 时区表，托管实例常没导入，会静默返回 `NULL`，同样无法走索引）。量大时直接聚合冗余列：`SELECT created_date, SUM(amount_cents) ... GROUP BY created_date`。PostgreSQL 侧只需把列定义为 `timestamptz`，`$2/$3` 传 Go 算好的 UTC 边界。

### 4.4 跨时区定时任务：civil 触发 + 去重

**`@every 24h` / `time.Ticker` 是绝对间隔**，跨过 DST 后本地钟点会漂一小时；「每天本地 09:30」是 civil 规则，必须每次重新计算：

```go
// NextDaily 返回 loc 时区中严格晚于 after 的下一个 hour:min。
func NextDaily(after time.Time, loc *time.Location, hour, min int) time.Time {
	local := after.In(loc)
	next := time.Date(local.Year(), local.Month(), local.Day(), hour, min, 0, 0, loc)
	if !next.After(local) { // day+1 由 time.Date 处理月末与年末
		next = time.Date(local.Year(), local.Month(), local.Day()+1, hour, min, 0, 0, loc)
	}
	return next
}
```

循环里用 `time.NewTimer(time.Until(next))`，触发后**重新计算**，不要累加 24 小时。用 robfig/cron 则给每个站点一个带时区的调度器，或写 `CRON_TZ=Europe/Berlin 30 9 * * *`。**多副本才是难点**：N 个 Pod 各跑一份就是 N 次触发，用唯一键在入口抢锁：

```sql
CREATE TABLE job_runs (
  job VARCHAR(64) NOT NULL, biz_date DATE NOT NULL, site VARCHAR(8) NOT NULL,
  started_at DATETIME(6) NOT NULL, PRIMARY KEY (job, biz_date, site)
);
INSERT INTO job_runs VALUES (?, ?, ?, UTC_TIMESTAMP(6))
ON DUPLICATE KEY UPDATE job = job;   -- 已存在则影响行数为 0
```

`RowsAffected() == 1` 才执行，`0` 说明别的副本已经跑过。PostgreSQL 用 `ON CONFLICT ... DO NOTHING`。**漏跑**另说：进程 09:30 挂了、10:00 才起，这次触发就没了。补偿是存「上次成功水位」，启动时扫应跑未跑的日期补齐；补跑同样走幂等表，天然不重复。

### 4.5 DST 边界的表驱动测试

```go
package timesvc

import (
	"testing"
	"time"
)

func TestBerlinDST(t *testing.T) {
	loc, err := time.LoadLocation("Europe/Berlin")
	if err != nil {
		t.Fatalf("load tz: %v", err)
	}
	cases := []struct {
		name       string
		in         time.Time
		wantHour   int
		wantOffset int
	}{
		{"春季跳变前 01:30", time.Date(2026, 3, 29, 1, 30, 0, 0, loc), 1, 3600},
		{"不存在的 02:30", time.Date(2026, 3, 29, 2, 30, 0, 0, loc), 3, 7200},
		{"秋季重复的 02:30", time.Date(2026, 10, 25, 2, 30, 0, 0, loc), 2, 3600},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if _, off := tc.in.Zone(); off != tc.wantOffset || tc.in.Hour() != tc.wantHour {
				t.Fatalf("offset=%d hour=%d, want %d/%d", off, tc.in.Hour(), tc.wantOffset, tc.wantHour)
			}
		})
	}
	// 日切：2026-10-25 在柏林有 25 小时，若改成 Add(24h) 这里会失败
	start, end, _ := (Site{Code: "de", Loc: loc}).DayRange("2026-10-25")
	if got := end.Sub(start); got != 25*time.Hour {
		t.Fatalf("day length = %v, want 25h", got)
	}
}
```

---

## 5. 版本演进

| 版本 | 与时间处理相关的要点 |
|---|---|
| Go 1.15 | 新增 `time/tzdata`，可把 IANA 数据库编进二进制，解决 `scratch` 镜像无法 `LoadLocation` |
| Go 1.17 | `UnixMilli`/`UnixMicro` 落地；`Time.IsDST` 可判断夏令时 |
| Go 1.20 | 新增 `DateOnly`/`TimeOnly`/`DateTime` 三个 layout 常量与 `Time.Compare` |
| Go 1.23 | 定时器通道改为**默认同步（无缓冲）**，旧行为需 `GODEBUG=asynctimerchan=1` 回退 |
| Go 1.25 | `testing/synctest` 转正，可用虚拟时钟测试超时与定时逻辑 |
| **Go 1.27** | **`asynctimerchan` 永久移除**，`time` 创建的通道恒为同步无缓冲 |

Go 1.27 是本文唯一需要改代码的变更：依赖 `timer.C` 缓冲行为、或在 `select` 里假设发送不阻塞的写法会失效，且不一定在编译期报错，运行期表现为卡住。时区数据侧，截至 2026-11 IANA 上游最新为 **2026f（2026-10-27）**（古巴取消 2026-11-01 回拨、闰秒表不再过期）；标准库内嵌的是**发布时快照**，通常滞后且无运行时 API 可查，**不要假设「升级 Go 就换了 tzdata」**。

---

## 6. 常见坑与最佳实践

1. **用 `==` 比较 `time.Time`**：`loc` 指针与单调读数让同一瞬间判不等，DB 往返必踩。用 `Equal`/`Before`/`After`/`Compare`。
2. **用 `t.Format("2006-01-02")` 做业务日切**：它输出 `t` 所在时区的日历日，而 `t` 常是 UTC 或 `Local`。`WHERE created_at >= '2026-11-16'` 在柏林站会多算/少算 1 小时。必须用 `time.Date(..., siteLoc)` 算 UTC 边界。
3. **`time.Parse` 丢时区**：layout 不含 `Z07:00` 时返回 UTC，后续格式化「差 8 小时」。有明确时区语义时用 `ParseInLocation`。
4. **`Local` 当业务时区**：它取决于 `TZ` 或宿主机配置，同一镜像在不同机器可能不同。业务时区写死 IANA 名并显式 `LoadLocation`。
5. **容器缺 tzdata**：`scratch`/`distroless/static` 没有 `/usr/share/zoneinfo`，`LoadLocation("Asia/Shanghai")` 报 `unknown time zone Asia/Shanghai`。主程序 `import _ "time/tzdata"`、`-tags timetzdata`、或镜像装 `tzdata` 并固定 `ZONEINFO`。
6. **以为内嵌 tzdata 会覆盖系统数据**：顺序是 `ZONEINFO` → 系统目录 → 内嵌 → GOROOT zip。镜像自带旧数据时内嵌再新也不用；要么删系统数据，要么在流水线里显式更新并记录版本。
7. **`time.Sleep`/`Ticker` 当「每天几点」**：`24h` 跨 DST 偏一小时，`Ticker` 还会累积漂移；`Sleep` 本身走单调时钟，不受改系统时间影响，但从 DB 读出的 deadline 没有单调读数，`Sub` 会退化成墙钟。定点任务用 `NextDaily` 每次重算 + `Timer`。
8. **`Duration` 溢出**：int64 纳秒上限约 292 年，`days * 24 * time.Hour` 大数下会静默回绕成负数。乘法前判上限，`Add` 后校验结果方向。
9. **数据库存字符串时间**：`VARCHAR` 无时区、无范围校验，`ORDER BY` 只能字典序，混入偏移或毫秒位就乱。用 `DATETIME(6)`/`timestamptz` 或 `BIGINT` epoch 毫秒。
10. **MySQL `TIMESTAMP` 的 2038 与隐式换算**：上限 2038-01-19，读写受会话 `time_zone` 影响；只设 `loc` 不设 `time_zone` 会「写对读错」。两个都设 UTC，或改用 `DATETIME(6)`。
11. **对 `timestamp`（无时区）调 `.In()`**：它存的是墙上时间，扫描结果的 `Location` 只是驱动默认值，转换等于给无时区的数硬贴标签。要时刻就用 `timestamptz`。
12. **精度不对齐**：`time.Now()` 是纳秒，MySQL `DATETIME` 默认秒、`DATETIME(6)` 微秒，PG 微秒。写入前 `Truncate`，否则「刚写就查」也不相等。
13. **`time.Tick` 不 Stop**：无法停止，Go 1.23 前连 GC 都回收不了。用 `NewTicker` + `defer ticker.Stop()`。

---

## 7. 选型对比

时区问题没有「最佳实践」，只有**读性能、可解释性、跨时区能力**之间的取舍。先选存储表示：

| 方案 | 表达方式 | 优点 | 代价 | 适用 |
|---|---|---|---|---|
| **UTC 时刻（推荐）** | `DATETIME(6)` / `timestamptz` 存 UTC | 排序比较一致、索引友好 | 展示需转换；本地日查询要算边界 | 绝大多数事件、日志、审计 |
| **UTC + 冗余本地日** | 上者 + `created_date DATE` | 日切/月结走索引，报表最简单 | 多一列；口径变更需回填 | 多站点订单、流水 |
| **本地时间 + 时区名** | `09:00` + `Europe/Berlin` | 保留原始约定 | 无法直接排序；重复钟点有歧义 | 营业时间、预约、合同 |
| **epoch 整数** | `BIGINT` 毫秒/微秒 | 无歧义、跨语言、体积小 | 可读性差、单位易错 | 高频时序、跨语言协议 |

- **默认存 UTC 时刻，展示时按站点 IANA 时区转换**；只有「本地日聚合」是刚需时才加 `created_date`，并把它当成业务口径写进文档。跨语言、高频事件流用 epoch 毫秒 `BIGINT`，单位写进列名（`created_at_ms`）。
- **要保留「当地钟表约定的那个点」**（营业时间、预约）→ 本地时间 + 时区名两列一起存。
- **时区数据** → 主程序内嵌兜底 + 镜像内显式管理版本，前者保命，后者保鲜。不要用 `carbon`、`now` 之类解决时区问题：它们只是语法糖，底层仍是同一份 `time.Location`，DST 坑一个不少。

> 所有时区事故的复盘几乎都能归成一句话：**某处把一个「时刻」当成了「墙上时间」，或反过来**。写代码时对每个时间变量问一句「instant 还是 civil」，比记住任何 API 都管用。

参考：<https://pkg.go.dev/time> · <https://pkg.go.dev/time/tzdata> · <https://www.iana.org/time-zones>
