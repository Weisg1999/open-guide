# Nightingale（夜莺）技术指南

> 夜莺不采集、不存储指标，只做告警与事件中心：把已有的 Prometheus / VictoriaMetrics / Elasticsearch / Loki / ClickHouse 接进来，统一规则、屏蔽、订阅与通知。
> 上游：<https://github.com/ccfos/nightingale> **v9.3.0**（2026-10）· 文档：<https://n9e.github.io/docs>　|　协议：Apache-2.0（2022 年由滴滴捐赠 CCF 开源发展委员会）

## 1. 项目简介

一句话：**Nightingale 是监控体系里的「告警控制面」**。它不替换你的采集器，也不替换你的时序库，而是把「规则怎么配、告警发给谁、什么时候不该发」从各个 Prometheus 实例里抽出来，做成一个带权限、带审计、带事件处理的平台。

这一定位决定了三件事：

- **指标仍在原来的 TSDB 里**。夜莺通过 PromQL 查询 Prometheus / VictoriaMetrics / Thanos，通过 DSL 查询 Elasticsearch / Loki / ClickHouse。数据不搬家，Grafana 也不用下岗。
- **规则从配置文件搬到数据库**。`rule_files` 变成业务组下的规则条目，改一条规则不用 reload，也不用等配置同步；谁改的、什么时候改的、评估结果是什么，都能查。
- **告警从「发出去」变成「处理过」**。屏蔽、订阅、聚合、恢复通知、事件管道、自愈脚本，这些在 Alertmanager 里要靠组合配置或自研的部分，夜莺给了现成的模型。

### 它不做什么

| 不做 | 说明 |
| --- | --- |
| 不采集指标 | 采集交给 Categraf、node_exporter、OpenTelemetry Collector；夜莺只接收 Remote Write 或查询外部数据源 |
| 不长期存储指标 | v9 起带一个**可选的**内置 TSDB，定位是「小规模单机开箱即用」（约 10 万活跃 series），不是替代 VictoriaMetrics |
| 不做值班排班与升级 | 它负责「把告警送到人」，不负责「谁今晚值班、15 分钟没响应升级给谁」；后者交给 PagerDuty、FlashDuty 或自建 on-call |
| 不替代 Grafana | 自带看板够用，但复杂可视化、大盘拼装仍建议留给 Grafana |

### 什么时候该用它

- 有**多个时序库**（总部 Prometheus、边缘 VictoriaMetrics、业务自建 Thanos），需要一套规则跨库生效；
- 想把监控能力开放给各研发团队**自助配置**，而不是所有人都来找 SRE 改 YAML；
- 需要**告警分级、生效时间段、按业务组隔离**、屏蔽与订阅这类「组织级」能力；
- 机房网络不稳定，需要**边缘告警下沉**（n9e-edge），断了中心连接也能本地告警。

反过来，如果你只有一套 Prometheus、规则不到 20 条、团队三五个人，`Prometheus + Alertmanager` 足够，引入夜莺只会多一套 MySQL/Redis 要维护。

## 2. 能力总览

| 能力 | 具体内容 | 与原生方案的关系 |
| --- | --- | --- |
| 规则管理 | 业务组隔离、规则导入（Prometheus rule 文件）、克隆、试运行（test-fire）、评估记录回看 | 替代 `rule_files` + reload |
| 多数据源 | Prometheus / VictoriaMetrics / Thanos、Elasticsearch / OpenSearch、Loki、ClickHouse、PostgreSQL / MySQL、TDengine、IoTDB | Prometheus 只能查自己 |
| 告警引擎 | 按 `cron_pattern` 或评估间隔执行、多实例分片、心跳注册、故障接管 | 替代 `prometheus rule evaluation` |
| 事件处理 | 屏蔽（mute）、订阅（subscribe）、事件管道（pipeline：relabel / enrich / 丢弃）、恢复通知 | 部分对应 Alertmanager 的 silence / inhibition |
| 通知 | 钉钉、飞书、企微、Slack、Teams、邮件、短信、电话、PagerDuty、Jira、Discord、ntfy 等 20+ 渠道，支持通知模板与测试发送 | Alertmanager 只有 webhook / email 等少数原生渠道 |
| 自愈 | Ibex 执行脚本（`[Ibex] RPCListen`，默认 20090），告警触发后下发到目标机器 | 原生方案没有 |
| 边缘模式 | n9e-edge 在边缘机房独立评估规则，断网时本地告警，恢复后同步事件 | 原生方案没有 |
| AI 与 MCP | v9 内置 AI 助手（自然语言查告警、生成规则）与 MCP Server，可被 Cursor 等客户端调用 | 原生方案没有 |
| 看板与机器管理 | 内置仪表盘、Grafana 大盘导入、机器列表与标签、采集器安装命令 | 部分替代 Grafana + 自建 CMDB |

## 3. 核心概念与原理

### 3.1 一个进程，三个角色

默认部署只有一个二进制 `n9e`，进程内跑三件事：

```text
                    ┌──────────────────────────── n9e ────────────────────────────┐
 Categraf / agent ──►  Pushgw（Remote Write 接收、label 重写、转发 TSDB）           │
                    │                                                             │
 浏览器 / API ──────►  Web/API（规则、机器、事件、看板、权限）──► MySQL/SQLite      │
                    │                                      └──► Redis             │
                    │  AlertingEngine（拉规则、查数据源、算阈值、生成事件）        │
                    └─────────────────────────────────────────────────────────────┘
                                             │
                                             ▼
                              外部 TSDB：Prometheus / VictoriaMetrics / Thanos
```

- **Pushgw**：接收 Prometheus Remote Write。它会把 series 里的 `ident` 标签与机器表关联，做 label 重写，然后转发给外部 TSDB（`[[Pushgw.Writers]]`）；v9 也可以直接写内置 TSDB。
- **Web/API**：规则、机器、业务组、屏蔽、订阅、通知、看板全部走这套 API。前端是编译进二进制的静态资源，所以升级只换一个文件。
- **AlertingEngine**：按规则的 `cron_pattern` 或评估间隔，去数据源执行查询，比较阈值，生成/恢复事件。

大规模时可以把角色拆开部署：`n9e-alert`（只跑告警引擎）、`n9e-pushgw`（只做数据转发）、`n9e-edge`（边缘节点）。**升级时必须一起升**，v9 release notes 明确提示了这一点，版本不一致会出现规则不同步或事件重复。

### 3.2 元数据：业务组、机器、标签

夜莺的所有规则都挂在**业务组（busi group）**下，这是它的多租户边界：

| 概念 | 作用 | 关键点 |
| --- | --- | --- |
| 业务组 | 规则、机器、事件、看板的隔离单位 | 团队成员按业务组授权；规则默认只对组内机器生效（`enable_in_bg=1`） |
| 机器（target） | 被监控对象，用 `ident` 唯一标识 | `ident` 通常就是 hostname；Categraf 上报时自带，手工 Remote Write 必须带上 |
| 标签 | 机器的键值对，用于规则筛选与事件富化 | 规则里的 `append_tags` 会写进事件，通知模板可直接引用 |
| 数据源 | 被查询的 TSDB / 日志库 | 一个规则可以绑定多个数据源（`datasource_queries`），实现「同一规则跨集群生效」 |

规则里有一个容易忽略的开关：`enable_in_bg`。置 0 表示**全局生效**（对所有机器的指标都算），置 1 表示只对业务组内的机器生效。多租户环境建议保持 1，否则 A 组的规则可能被 B 组的指标触发。

### 3.3 告警规则：从 PromQL 到事件

一条规则的核心是「查什么、怎么判、判多久、发给谁」。v9 的规则主体是 `rule_config`（JSON），里面包含 `queries` 与 `triggers`；老字段 `prom_ql` 仍兼容但已标记废弃。

| 字段 | 含义 | 备注 |
| --- | --- | --- |
| `rule_config.queries` | 查询列表：数据源、表达式、时间范围 | 支持 PromQL、LogQL、ES DSL、SQL |
| `rule_config.triggers` | 判定条件：阈值、比较符、持续次数/时长 | 多 query 时可做 A/B 运算 |
| `cron_pattern` | 评估节奏，如 `@every 15s` | 替代已废弃的 `prom_for_duration` |
| `prom_eval_interval` | 评估间隔（秒） | 与 cron 二选一 |
| `recover_duration` | 恢复判定窗口（秒） | 避免抖动导致反复恢复/触发 |
| `severity` | 1 Emergency / 2 Warning / 3 Notice / 4 Lowest | 决定通知模板与升级策略 |
| `time_zone` | 生效时间所用时区 | 空表示服务端默认；跨时区团队必须显式设置 |
| `enable_stimes` / `enable_etimes` / `enable_days_of_weeks` | 生效时间段与星期 | 支持多组时段，不是单一窗口 |
| `notify_rule_ids` | 关联的通知规则 | v9 推荐用通知规则，不再直接挂渠道 |
| `disabled` | 0 启用 / 1 禁用 | 禁用后引擎不再评估 |

「持续多久才告警」在夜莺里有两层：查询区间（看多长的数据）和触发条件（连续几个点满足）。**只配持续时长、不配恢复窗口**，是抖动误报的主要来源。

### 3.4 告警引擎的高可用：分片，而不是复制

这是夜莺与 Prometheus 最大的架构差异。Prometheus 每个实例评估自己的规则，天然无协调；夜莺的规则在数据库里，多个 `n9e-alert` 实例必须**分工**，否则同一条规则会被评估多次、事件重复。

机制是「按业务组分配 + 心跳续约」：

```text
n9e-alert-1  ──┐
n9e-alert-2  ──┼──► 读取 [Alert.Heartbeat] EngineName，注册存活
n9e-alert-3  ──┘         │
                         ▼
              规则按 group_id 哈希分配到存活实例
                         │
              实例宕机 → 心跳超时 → 规则被其他实例接管
```

由此得到两条运维结论：

1. **扩副本提升的是容量与可用性，不是单条规则的评估速度**。规则再多，也只是分散到更多实例；单条规则的查询仍然受数据源限制。
2. **必须监控「规则分配」本身**。如果某个实例心跳异常但没退出，规则可能出现「以为有人在算，其实没人算」的空窗。夜莺的规则页会显示评估记录，v9 还提供 `[Alert.EvalLog]` 把每次评估的查询与判定落盘，排障时先看这里，而不是先怀疑数据源。

### 3.5 事件处理链：屏蔽 → 订阅 → 通知

一条事件产生后，依次经过：

```text
规则命中 → 事件（含 tags/severity/rule_id）
   │
   ├─► 屏蔽（mute）：匹配标签/规则/时间窗 → 直接抑制，不进通知
   ├─► 事件管道（pipeline）：relabel、富化（补 owner/runbook）、丢弃
   ├─► 订阅（subscribe）：按业务组/标签决定「谁该收到」
   └─► 通知（notify）：模板渲染 → 渠道发送 → 记录投递结果
```

三者的分工容易混淆：

- **屏蔽**回答「现在不要发」（维护窗口、已知故障）；
- **订阅**回答「这类事件归谁」（团队/值班组）；
- **通知规则**回答「用什么渠道、什么模板、多久重复」。

把「屏蔽」当「订阅」用，会出现「规则配了但没人收到」；把「订阅」当「屏蔽」用，则会在维护窗口被电话叫醒。夜莺的屏蔽支持按标签匹配，粒度可以细到「只屏蔽某个集群的某条规则」，这是 Alertmanager silence 的等价能力。

### 3.6 v9 的形态变化：默认「单机开箱即用」

如果你看过 v6/v7 的部署文档，会记得「必须 MySQL + Redis + 外部 TSDB」。v9 的默认配置变了：

```toml
[DB]
DBType = "sqlite"        # 支持 mysql / postgres / sqlite
DSN = "n9e.db"

[Redis]
RedisType = "miniredis"  # standalone / cluster / sentinel / miniredis

[EmbeddedTSDB]
Enable = true            # 内置 TSDB，自动注册名为 embedded-tsdb 的数据源
Dir = "data/tsdb"
RetentionDuration = "15d"
MaxBytes = "10GiB"
```

这让「下载二进制 → 启动 → 打开 17000」就能看到数据与告警，适合评估与小规模单机。但配置注释写得很清楚：**内置 TSDB 存在本机磁盘，只适合单实例**；多副本部署时每个副本只持有一部分数据，必须关掉它并配置 `[[Pushgw.Writers]]` 指向外部 TSDB。SQLite 同理，多副本必须换 MySQL/PostgreSQL。

## 4. 使用实战

### 4.1 用 Compose 起一套完整环境

官方仓库自带评估栈，包含夜莺、MySQL、Redis、VictoriaMetrics 与 Categraf：

```bash
git clone https://github.com/ccfos/nightingale.git
cd nightingale/docker/compose-bridge
docker compose up -d
# 打开 http://localhost:17000，初始账号 root / root.2020
```

`compose-bridge` 里的服务名就是容器内可用的地址。配数据源时**不要填 localhost**——那是 n9e 容器自己的回环地址，填 `http://victoriametrics:8428` 才能连通。

### 4.2 配数据源

登录后进入「数据源」，新增 Prometheus 类型：

| 字段 | 值 | 说明 |
| --- | --- | --- |
| 名称 | `vm-local` | 自定义，规则里按 ID 引用 |
| URL | `http://victoriametrics:8428` | Compose 内用服务名；生产用可路由地址 |
| 类型 | Prometheus | VictoriaMetrics 兼容 Prometheus 查询 API |
| 超时 | 10s | 小于规则评估间隔，避免查询堆积 |

保存后点「测试」，能看到 `up` 之类的指标才算通。

### 4.3 建业务组与机器

1. 「业务组」新建 `demo`，把当前账号加进去；
2. 「机器列表」会显示 Categraf 上报的主机，`ident` 就是 hostname；
3. 给机器打标签，例如 `env=prod`、`region=cn-north`，后续规则用标签筛选。

如果没有机器，说明 Categraf 没连上，或者 `ident` 没对上。夜莺的「机器列表」页提供采集器安装命令，可以直接复制。

### 4.4 写第一条规则

最快验证链路的方式是用一条**必然触发**的规则：

```promql
vector(1) > 0
```

步骤：

1. 业务组 `demo` → 告警规则 → 新建；
2. 数据源选 `vm-local`，查询填上面的表达式；
3. 触发条件设为「大于 0」，持续时长 `0`（立即触发）；
4. 严重级别选 `Warning`；
5. 先不挂通知，保存并启用。

几十秒内「活跃告警」就会出现一条事件。确认能看到事件、能点开详情（含查询语句与触发值），再去做通知——**先让事件跑通，再让通知跑通**，能省掉一半排障时间。

确认无误后把这条规则禁用或删掉，否则它会一直告警。

### 4.5 通知：渠道 → 通知规则 → 关联

v9 的推荐路径是三层：

```text
通知渠道（钉钉机器人 / 飞书 / 邮件 / 短信…）
      ↑ 被引用
通知规则（哪些级别、什么模板、重复间隔、恢复是否通知）
      ↑ 被关联
告警规则（notify_rule_ids）
```

1. 「通知渠道」新增钉钉或飞书机器人，填 webhook 与签名，点「测试」；
2. 「通知规则」新建：选渠道、选级别（如 Warning + Emergency）、选模板；
3. 回到告警规则，把通知规则挂上。

顺序不能反：先建渠道再建规则，否则通知规则里选不到渠道。测试发送失败时，先看渠道的签名/关键词配置，再看网络出口——夜莺不会替你重试到成功。

### 4.6 Go：把指标推进夜莺

夜莺的 Pushgw 接收标准的 Prometheus Remote Write。下面这段代码构造一个带 `ident` 标签的样本，推给夜莺；`ident` 必须与机器表里的标识一致，否则数据能入库但关联不到机器。

```go
package main

import (
	"bytes"
	"context"
	"fmt"
	"net/http"
	"time"

	"github.com/gogo/protobuf/proto"
	"github.com/golang/snappy"
	"github.com/prometheus/prometheus/prompb"
)

// push 向夜莺 Pushgw 发送一个样本，路径与内置 TSDB 的 remote write 端点一致。
func push(ctx context.Context, endpoint, ident string, value float64) error {
	req := &prompb.WriteRequest{
		Timeseries: []prompb.TimeSeries{{
			Labels: []prompb.Label{
				{Name: "__name__", Value: "demo_job_duration_seconds"},
				{Name: "ident", Value: ident}, // 夜莺靠 ident 关联机器
				{Name: "job", Value: "demo"},
			},
			Samples: []prompb.Sample{{
				Value:     value,
				Timestamp: time.Now().UnixMilli(),
			}},
		}},
	}
	raw, err := proto.Marshal(req)
	if err != nil {
		return err
	}
	body := snappy.Encode(nil, raw)

	httpReq, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, bytes.NewReader(body))
	if err != nil {
		return err
	}
	httpReq.Header.Set("Content-Type", "application/x-protobuf")
	httpReq.Header.Set("Content-Encoding", "snappy")
	httpReq.Header.Set("X-Prometheus-Remote-Write-Version", "0.1.0")

	resp, err := http.DefaultClient.Do(httpReq)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode/100 != 2 {
		return fmt.Errorf("remote write failed: %s", resp.Status)
	}
	return nil
}

func main() {
	// 生产环境从配置读取，不要硬编码
	err := push(context.Background(), "http://127.0.0.1:17000/prometheus/api/v1/write", "demo-host-01", 0.42)
	fmt.Println("push err:", err)
}
```

两个细节：`ident` 标签会被 Pushgw 重写（`LabelRewrite = true`），最终入库的标签以机器表为准；`ForceUseServerTS = true` 时服务端会覆盖客户端时间戳，跨时区机器不需要自己校准。

### 4.7 生产配置片段

从默认的「单机模式」切到「生产模式」，需要改的是这三段：

```toml
[DB]
DBType = "mysql"
DSN = "n9e:pass@tcp(mysql.internal:3306)/n9e?charset=utf8mb4&parseTime=True&loc=Local"
MaxOpenConns = 150
MaxIdleConns = 50

[Redis]
Address = "redis.internal:6379"
RedisType = "standalone"
Password = "***"
DB = 0

[EmbeddedTSDB]
Enable = false            # 多副本必须关掉，改用外部 TSDB

[[Pushgw.Writers]]
Url = "http://victoriametrics.internal:8480/insert/0/prometheus/api/v1/write"
Timeout = 10000
DialTimeout = 3000
```

迁移期可以「双写」：`EmbeddedTSDB.Enable = true` 与 `[[Pushgw.Writers]]` 同时开，数据既进本地也进外部库，确认外部库查询正常后再关掉内置 TSDB。

### 4.8 用 API 查当前告警

夜莺的 API 用个人 Token 鉴权（Profile → Token 管理），请求头是 `X-User-Token`：

```bash
# 拉取活跃告警，按严重级别过滤
curl -s -H "X-User-Token: $N9E_TOKEN" \
  "http://127.0.0.1:17000/api/n9e/alert-cur-events/list?severity=2&limit=20" | jq '.dat'

# 查某条规则最近一次评估记录（v9 的 EvalLog 能力）
curl -s -H "X-User-Token: $N9E_TOKEN" \
  "http://127.0.0.1:17000/api/n9e/alert-rule/1/eval-records?limit=5" | jq '.dat'
```

把这两个接口接进自己的值班看板，比在夜莺页面里刷新更适合做「跨系统统一视图」。

## 5. 版本演进

| 版本 | 时间 | 关键变化 | 迁移注意 |
| --- | --- | --- | --- |
| v6 | 2022–2023 | 模块化拆分：`n9e-alert`、`n9e-pushgw`、`n9e-edge` 可独立部署；业务组与权限模型成型 | 旧版配置文件需按新段名调整 |
| v7 / v8 | 2024–2025 | 多数据源扩展（ES / Loki / ClickHouse）、通知渠道增加、看板与 Grafana 导入增强 | 规则字段逐步从 `prom_ql` 迁到 `rule_config` |
| **v9** | **2026-10（v9.3.0）** | **内置 TSDB、默认 SQLite + miniredis、内置 AI 助手与 MCP Server、全新导航与表格 UI、重写规则/屏蔽/订阅/通知表单、事件管道执行追踪、新一代日志浏览器（ES / Loki / VictoriaLogs）** | 替换二进制即可，v9.3.0 无 schema 变更；`n9e-edge` / `n9e-pushgw` 必须同步升级 |

v9 的三个变化值得单独记：

1. **默认形态从「三件套」变成「单文件」**。SQLite、miniredis、内置 TSDB 都是默认开启的，评估成本极低，但生产多副本必须换回 MySQL/PostgreSQL + 外部 TSDB。
2. **AI 助手依赖 Redis Streams**，要求 Redis 5.0+，Cluster 用户建议 7.0+（分片 Pub/Sub）。只用基础告警可以不管，但升级前要确认 Redis 版本。
3. **规则字段新旧并存**。`prom_ql`、`prom_for_duration`、`datasource_ids`、`enable_stime/etime` 都标记为 Deprecated，新规则应使用 `rule_config`、`cron_pattern`、`datasource_queries`、`enable_stimes/etimes`。

## 6. 常见坑与最佳实践

1. **容器里填 localhost 当数据源**。n9e 容器内的 `localhost:9090` 是它自己，不是 Prometheus。Compose 用服务名，K8s 用 Service DNS，宿主机才用 127.0.0.1。
2. **多副本仍开内置 TSDB**。内置 TSDB 是单实例本地盘，多副本时每个副本只持有一部分数据，查询结果会「时有时无」。多副本必须 `Enable = false` 并配外部 TSDB。
3. **以为加副本能加快规则评估**。规则是按业务组分片到实例的，加副本只提升总容量与容灾，单条规则的查询延迟仍取决于数据源。要提速，先优化 PromQL 与数据源。
4. **不监控规则分配**。某个 `n9e-alert` 心跳异常但进程还在，规则可能无人评估。盯「规则评估记录」与实例心跳，比盯 CPU 更有用。
5. **把屏蔽当订阅用**。屏蔽是「现在不要发」，订阅是「归谁」。维护窗口结束后忘了删屏蔽，是「告警消失」的常见原因；反之，用订阅做抑制，则会在维护期被叫醒。
6. **只配持续时长、不配恢复窗口**。阈值在边界抖动时，事件会反复触发/恢复，通知渠道被刷屏。给 `recover_duration` 留出至少 2–3 个评估周期。
7. **时区不显式设置**。`time_zone` 为空时用服务端默认；跨国团队配「只在工作时间告警」，不写时区就会出现「德国团队凌晨收到电话」。
8. **规则里 `enable_in_bg` 配错**。置 0 是全局生效，多租户环境下 A 组规则会被 B 组指标触发。除非明确要跨组，否则保持 1。
9. **升级只升 n9e**。`n9e-edge` / `n9e-pushgw` 与主程序协议耦合，版本不一致会出现规则不同步或事件重复。release notes 每年都在提醒这件事。
10. **用 SQLite 跑生产多副本**。SQLite 是单文件库，多副本并发写会锁库；评估环境无所谓，生产必须换 MySQL/PostgreSQL。
11. **忘记内置 TSDB 的保留期**。默认 15 天 / 10 GiB，超了会删最旧的块；把它当长期存储用，会在某天发现历史数据「凭空消失」。
12. **通知渠道只配不测**。钉钉的关键词、飞书的签名、企微的 IP 白名单，任一项错都只是「没收到」。上线前必须点一次测试发送。

## 7. 选型对比

| 方案 | 规则管理 | 多数据源 | 多租户/权限 | 通知渠道 | 事件处理 | 值班升级 | 部署成本 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| **Nightingale** | 数据库 + UI + API，支持导入/克隆/试跑 | **强**（Prometheus / VM / ES / Loki / ClickHouse / SQL） | **强**（业务组 + 角色） | **20+ 原生渠道** | 屏蔽 / 订阅 / 管道 / 自愈 | 无（需接 PagerDuty 等） | 中（MySQL + Redis + 外部 TSDB；单机可 SQLite） |
| Prometheus + Alertmanager | 配置文件 + reload | 弱（只查自身） | 无 | 少（webhook / email） | 分组 / 抑制 / 静默 | 无 | 低 |
| Grafana Alerting | UI 配置，跟看板一体 | 中（支持多种数据源） | 中（Grafana 组织/团队） | 中（contact points） | 静默 / 分组 | 无 | 低（已有 Grafana） |
| VictoriaMetrics vmalert | 配置文件 / CRD | 中（VM 集群内） | 无 | 少 | 有限 | 无 | 低（已有 VM） |
| Zabbix | 模板 + 触发器 | 弱（自带采集） | 中 | 中 | 依赖动作/维护期 | 有（升级/值班） | 高（Server + DB + Agent） |

### 一句话决策

- **只有一套 Prometheus、规则少、没人要求自助配置** → 留在 `Prometheus + Alertmanager`，别折腾。
- **已经有 Grafana、告警也想在看板里配** → `Grafana Alerting` 够用，少一个系统。
- **多个时序库 + 多团队 + 需要分级/屏蔽/订阅** → 夜莺的收益最大，它的核心价值是「组织级告警治理」，不是「更强的 PromQL」。
- **需要电话升级、值班表、MTTA/MTTR 统计** → 夜莺只负责「送到」，升级链要接 PagerDuty、FlashDuty 或自建 on-call，别指望它全覆盖。
- **纯 K8s、规则即代码、GitOps 流程** → 优先 `vmalert` 或 Prometheus Operator 的 `PrometheusRule`，夜莺的 UI 优势在 GitOps 下反而变成「配置漂移源」。

### 与相邻篇目的分工

- 指标存储与 PromQL：[Prometheus](./../prometheus/prometheus-guide.md)、[VictoriaMetrics / Thanos](./../victoriametrics/victoriametrics-guide.md)；
- 日志检索：[Loki](./../loki/loki-guide.md)、[Elasticsearch](./../elasticsearch/elasticsearch-guide.md)；
- 链路追踪与埋点：[OpenTelemetry](./../opentelemetry/opentelemetry-guide.md)；
- 告警只是可观测性的「出口」，上游三支柱不稳，规则配得再好也只是把噪声搬到手机上。

参考：<https://github.com/ccfos/nightingale> · <https://n9e.github.io/docs> · <https://github.com/ccfos/nightingale/blob/main/etc/config.toml>
