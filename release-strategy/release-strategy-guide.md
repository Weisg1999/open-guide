# 渐进式发布技术指南（灰度 / 金丝雀 / 回滚）

> **一句话定位**：把「一次性全量替换」拆成「可控放量 + 可观测判定 + 可快速回滚」的发布工程。
>
> **涉及组件**：Nginx Ingress / Apache APISIX / Argo Rollouts / Istio
>
> **上游仓库**：[apache/apisix](https://github.com/apache/apisix) · [argoproj/argo-rollouts](https://github.com/argoproj/argo-rollouts) · **版本基线**：APISIX 3.x、Rollouts 1.7+、Ingress-Nginx 1.11+ · **协议**：Apache-2.0

---

## 1. 项目简介

发布是线上事故最集中的一段。Google SRE 与 DORA 多年的统计都指向同一个结论：**大多数故障由变更引入**，而不是硬件或基础依赖自己坏掉。原因并不神秘——新代码只在测试环境的假流量下跑过，真实的参数组合、缓存状态、数据分布、下游超时都没被验证；再加上配置、镜像、依赖版本一起变，出问题时很难判断是哪一处引起的。

「全量替换」把这个风险做成了二元结果：要么全好，要么全坏，爆炸半径等于 100%。滚动更新稍好一点，但它优化的是**可用性**（不一次性杀掉所有 Pod），不是**风险控制**——第一个新 Pod 起来就开始接真实流量，错误同样会按比例扩散。

渐进式发布的本质是把一次大赌注拆成若干小赌注：

- **可控放量**：先 1%、5%、10%，每一档都停一会儿，让真实流量把问题暴露出来；
- **可观测判定**：用错误率、P99、业务转化这些指标，而不是等用户投诉，来决定继续还是停；
- **可快速回滚**：流量切回旧版本是一条命令、秒级完成，且数据不能因为新旧并存而损坏。

三者缺一不可。只有放量没有判定，是「慢一点的全量发布」；只有判定没有回滚，是把事故现场保留得更久；只有回滚没有数据兼容，回滚本身会制造二次事故。

还要接受一个现实：**「能回滚」比「不出错」更现实**。再严格的评审也拦不住所有问题，发布工程的目标不是零故障，而是把 MTBF 和 MTTR 同时做好——绝大多数时候，5 分钟内切回上一版，比现场 debug 半小时体面得多。本文覆盖四层：入口层的流量切分、编排层的自动判定、应用层的流量染色、数据层的向后兼容。

---

## 2. 能力总览

| 策略 | 机制 | 适用场景 | 回滚速度 |
|---|---|---|---|
| 蓝绿 | 两套完整环境，入口/DNS 一次性切换 100% | 大版本、依赖变更、需要秒级整体切换 | 秒级（切回旧环境） |
| 金丝雀（按权重） | 按百分比随机分流，逐步抬高 | 常规服务发布、容量验证 | 秒级（权重改 0） |
| 金丝雀（按内容） | header / cookie / 用户 ID 取模命中 | 内部员工、特定租户、特定地域先行 | 秒级（摘掉规则） |
| A/B 测试 | 按业务维度分流，对比两组指标 | 产品实验、算法/推荐策略对比 | 分钟级（要清理实验数据） |
| 流量镜像（shadow） | 复制真实请求到影子版本，响应丢弃 | 新版本压测、兼容性验证、性能基线 | 不需要（不承载真实流量） |
| 特性开关 | 代码内开关，部署与放量解耦 | 功能级灰度、kill switch、按租户开白 | 秒级（改配置中心） |

几条常被混淆的边界：

- **蓝绿 vs 金丝雀**：蓝绿是「全有或全无」的切换，适合无法共存的两套依赖；金丝雀是「部分共存」，要求新旧版本能同时处理同一份数据。
- **流量镜像不是灰度**：影子版本的返回被丢弃，用户不会感知，所以它验证不了「业务是否正确」，只能验证「会不会崩、够不够快」。
- **特性开关不是发布的替代品**：它控制的是代码路径，不是部署单元。开关能瞬间止血，但旧代码仍然要带着新开关逻辑跑，技术债会累积。

---

## 3. 核心概念与原理

### 3.1 流量入口的三个控制点

按请求在链路上被拦下的位置，灰度能力分布在三个层次：

| 控制点 | 代表实现 | 能力 | 代价 |
|---|---|---|---|
| Ingress Controller | ingress-nginx 的 canary 注解 | 按权重 / header / cookie 分流，零额外组件 | 仅 HTTP，规则较粗，注解绑定在 Ingress 资源上 |
| Gateway API | `HTTPRoute.backendRefs[].weight`、`matches` | 标准化、多实现、原生支持 header 匹配与权重 | 生态较新，各控制器对流量切分的精度不一致 |
| Service Mesh | Istio `VirtualService` + `DestinationRule` | L4/L7 全量能力：subset、权重、镜像、精细匹配、mTLS | sidecar/ambient 的资源与运维开销，排障链路变长 |

选择顺序通常是：**已经有 mesh 就用 mesh，没有就用入口层**。为了一次灰度专门引入 Istio，运维成本远高于收益；反过来，已经有 sidecar 的集群再在 Ingress 上做灰度，会得到两套互相干扰的规则。

入口层的关键限制是「它只能看见 HTTP 语义」：gRPC、Dubbo、MQ 这些流量，Ingress 管不到。多协议场景要么上 mesh，要么在网关（APISIX 这类）统一处理。

### 3.2 权重分流 vs 内容分流

**权重分流**对每个请求独立抽样：`weight: 10` 意味着每个请求有 10% 概率进新版本。它的比例最准确，但同一个用户会在两个版本之间来回跳——刚在旧版加购，刷新后购物车在新版是空的。对无状态 API 没问题，对面向用户的页面很糟。

**内容分流**按请求特征做稳定映射：

- header：`X-Canary: true`，适合内部测试、自动化回归；
- cookie：写一个灰度标记，适合「让一批用户持续用新版」；
- 用户 ID 取模：`hash(uid) % 100 < 10`，适合按自然用户灰度，且能保证同一用户始终命中同一侧。

内容分流的实际比例取决于分流键的分布。10% 灰度在 100 个用户上可能只命中 3 个，样本太小，指标没有统计意义；所以按用户灰度时，要么保证灰度池足够大（至少几千 DAU），要么先按权重放量、再用内容分流锁定问题用户。

最容易被忽略的是**会话粘滞**对比例的破坏。一旦入口开启 cookie affinity，用户会被粘在第一次命中的后端：老用户全部留在 stable，权重只对新会话生效，实际灰度比例远低于设定值；反过来，已经进入灰度池的用户会一直留在新版本，出问题时影响面被「锁定」在这批人身上，不会随权重下调而缩小。结论：

- 面向用户的灰度用「内容分流 + 一致性哈希」，并按用户维度统计指标；
- 面向容量的验证用「权重」，且关闭会话粘滞；
- 两者不要同时开，否则你既得不到准确比例，也得不到稳定体验。

### 3.3 金丝雀的判定：看什么、阈值怎么定、谁来触发

判定不是「看一眼监控」，而是一组事先写好的、可自动执行的规则。常用四类指标：

| 类别 | 具体指标 | 建议判定方式 |
|---|---|---|
| 错误率 | 5xx / 总请求、gRPC `Internal` 比例 | 比 stable 高 0.5 个百分点，或绝对值 > 1% 持续 2 分钟 |
| 延迟 | P99、P999，按接口拆分 | 超过 stable 同时段 P99 的 1.2~1.5 倍，持续 5 分钟 |
| 饱和度 | CPU、内存、GC pause、goroutine 数 | 接近 limit 的 80%，或随流量线性上涨 |
| 业务 | 下单成功率、支付转化、搜索无结果率 | 比同时段历史基线下降超过 2% |

阈值不要拍脑袋。先取 stable 最近 7~14 天的同时间段分布，用 P99 的 1.2~1.5 倍作为上限；绝对阈值只做兜底，因为不同接口的基线差异极大。判定窗口至少要覆盖一个完整的业务周期（≥10 分钟），并避开整点秒杀、定时任务这些天然尖刺。

谁触发回滚，取决于「指标是否可信、回滚是否昂贵」：

- **自动（AnalysisRun）**：指标明确、回滚只是切流量、无数据副作用，适合大多数无状态服务；
- **人工**：业务指标滞后（转化率要几小时才稳定）、涉及资金/库存、回滚需要 DBA 配合，适合让机器只报警、人来按按钮。

混合模式最常见：错误率和延迟自动判定，业务指标人工确认；`autoPromotionEnabled: false` 让每一档都停下来等人 promote。

```promql
# 按 version 维度看 5xx 占比：没有这个维度就无法判定（见第 6 节）
sum by (version) (rate(http_requests_total{job="order", code=~"5.."}[2m]))
/
sum by (version) (rate(http_requests_total{job="order"}[2m]))

# 按 version 维度看 P99
histogram_quantile(0.99,
  sum by (version, le) (rate(http_request_duration_seconds_bucket{job="order"}[5m]))
)
```

### 3.4 流量染色：让请求在全链路都带着「我是谁」

灰度要能判定，前提是每条日志、每个指标、每次下游调用都知道自己属于哪个版本。做法是从入口打一个标记，然后一路透传：

```text
Client
  │  X-Canary: true（入口按 header/uid 判定后写入）
  ▼
Ingress / Gateway ──► 服务 A（日志 version=canary，指标 label version=canary）
                          │  HTTP: X-Canary 透传
                          │  gRPC: metadata x-canary=true
                          ▼
                      服务 B ──► 服务 C
                          │
                          └──► MQ: message header dye=canary
```

三个要点：

- **入口负责归一化**：外部传进来的 header 不可信，入口按规则重新计算一次，避免用户自己加 `X-Canary: true` 蹭新版本；
- **日志与指标必须有 `version` 字段**：日志用结构化字段，指标用 label；否则出了事只能靠 Pod 名猜；
- **异步链路不能丢染色**：消息头要带 `dye`，消费者据此决定走新逻辑还是旧逻辑，或者在影子队列里双消费。MQ 的消费是竞争式的，「10% 消息进新版本」无法靠权重实现（见第 6 节）。

OpenTelemetry 的 baggage 可以把染色自动带过进程边界，但它只解决「传递」，不解决「消费端怎么用」；关键路径上仍建议显式读写 header，避免隐式传播在序列化/重试时丢失。

### 3.5 数据库兼容：expand-contract 三步法

灰度期间新旧版本同时在线，意味着**同一份数据要能被两代代码同时读写**。这是回滚能否成功的前提：如果新版本写入了旧版本不认识的必填字段，回滚后旧代码直接报错，事故从「新版本有问题」升级成「两个版本都不可用」。

标准做法是 expand-contract，把一次 schema 变更拆到三个发布周期：

```text
expand    加新列/新表（可空、有默认值），新旧代码都双写，读仍走旧列
    │     灰度期间：v1 与 v2 并存，任何一方写入的数据另一方都能读
    ▼
migrate   后台分批回填历史数据 + 校验，读路径用开关逐步切到新列
    │     观察一个完整发布周期，确认不需要回滚
    ▼
contract  停止双写，删旧列/旧索引，收紧 NOT NULL 等约束
```

```sql
-- 1) expand：只做加法，旧代码完全无感知
ALTER TABLE orders ADD COLUMN channel VARCHAR(32) NULL;

-- 2) migrate：分批回填，避免长事务锁表（每批 1000 行， sleep 一下）
UPDATE orders
SET channel = 'web'
WHERE channel IS NULL AND id > ? AND id <= ?;

-- 3) contract：确认旧版本实例已全部下线、回填 100% 后再收紧
ALTER TABLE orders ALTER COLUMN channel SET NOT NULL;
```

灰度期间的 schema 纪律：

- 只做**加法**：加列、加表、加索引（注意在线 DDL）；不要重命名、不要删列、不要改类型；
- 新列必须可空或有默认值，旧代码的 INSERT 不带它也能成功；
- 枚举值只增不减，旧代码读到未知枚举要能降级处理而不是 panic；
- 索引变更用 `CONCURRENTLY`（PostgreSQL）或 online DDL（MySQL），否则灰度窗口里会锁表。

---

## 4. 使用实战

### 4.1 Nginx Ingress 的 canary 注解

ingress-nginx 的做法是**再建一份 Ingress**，与稳定版同 host、同 path，靠注解声明它是 canary：

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: order-stable
spec:
  ingressClassName: nginx
  rules:
    - host: api.example.com
      http:
        paths:
          - path: /api/order
            pathType: Prefix
            backend:
              service:
                name: order-stable
                port:
                  number: 80
---
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: order-canary
  annotations:
    nginx.ingress.kubernetes.io/canary: "true"
    nginx.ingress.kubernetes.io/canary-weight: "10"
    nginx.ingress.kubernetes.io/canary-by-header: "X-Canary"
    nginx.ingress.kubernetes.io/canary-by-header-value: "true"
spec:
  ingressClassName: nginx
  rules:
    - host: api.example.com
      http:
        paths:
          - path: /api/order
            pathType: Prefix
            backend:
              service:
                name: order-canary
                port:
                  number: 80
```

规则优先级是 **header > cookie > weight**：

- `canary-by-header: X-Canary` + `canary-by-header-value: "true"`：头匹配才进 canary，头存在但值不匹配则走 stable；
- `canary-by-cookie: canary=always`：cookie 命中进 canary；
- 两者都没命中时，才按 `canary-weight` 做概率分流；`canary-weight-total` 默认 100，权重是相对它的比例。

1.11 之后的变化：

- 新增 `canary-by-header-pattern`，支持正则匹配（此前只能精确匹配 value），适合按租户前缀、灰度批次号分流；
- 注解语义基本冻结，官方把新能力投向 Gateway API；上游已宣布 Ingress-NGINX 于 2026 年 3 月进入停止维护状态，新集群应优先选择 Gateway API 实现（Envoy Gateway、Istio、APISIX Ingress 等）；
- 同一 host/path 只能有一份 canary Ingress，多份会互相覆盖；注解只写在 canary 那份上，写在 stable 上不生效。

### 4.2 APISIX 的 traffic-split 插件

APISIX 把分流放在路由插件里，不依赖额外的 Ingress 对象，规则可以按顺序求值：

```yaml
# conf/apisix.yaml（standalone 模式；Admin API 提交时结构相同）
upstreams:
  - id: order-stable
    type: roundrobin
    nodes:
      "order-stable.default.svc.cluster.local:80": 1
  - id: order-canary
    type: roundrobin
    nodes:
      "order-canary.default.svc.cluster.local:80": 1

routes:
  - id: order
    uri: /api/order/*
    upstream_id: order-stable
    plugins:
      traffic-split:
        rules:
          # 规则 1：命中染色头，100% 进灰度
          - match:
              - vars:
                  - - http_x_canary
                    - "=="
                    - "true"
            weighted_upstreams:
              - upstream_id: order-canary
                weight: 100
          # 规则 2：兜底，按 90/10 权重分流
          - weighted_upstreams:
              - upstream_id: order-stable
                weight: 90
              - upstream_id: order-canary
                weight: 10
```

要点：

- `rules` 按数组顺序匹配，**第一条命中的规则生效**，所以把精确规则放前面、兜底权重放最后；
- `weighted_upstreams` 的 weight 是相对值，不需要凑满 100；
- 规则里的 `vars` 用的是 lua-resty-expr 语法，`http_` 前缀读请求头、`arg_` 读 query、`cookie_` 读 cookie；
- 只想按比例时，可以只留一条无 `match` 的规则；APISIX 另有更简单的 `canary` 插件，但复杂规则仍推荐 `traffic-split`。

改完配置后，用 `curl -H 'X-Canary: true' ...` 验证命中，再摘掉 header 验证权重分流，最后看 APISIX 的 access log 里 upstream 是否符合预期。

### 4.3 Argo Rollouts：Rollout + AnalysisTemplate

前面两节解决「怎么切流量」，这一节解决「切完怎么自动判定」。Argo Rollouts 用 `Rollout` 替代 `Deployment`，把发布过程声明成 steps，并由控制器驱动 Ingress/网关改权重：

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Rollout
metadata:
  name: order
spec:
  replicas: 10
  selector:
    matchLabels:
      app: order
  template:
    metadata:
      labels:
        app: order
    spec:
      containers:
        - name: order
          image: registry.example.com/order:v1.4.0
          ports:
            - containerPort: 8080
  strategy:
    canary:
      stableService: order-stable
      canaryService: order-canary
      trafficRouting:
        nginx:
          stableIngress: order-stable
      autoPromotionEnabled: false
      steps:
        - setWeight: 10
        - pause: { duration: 5m }
        - analysis:
            templates:
              - templateName: order-success-rate
            args:
              - name: version
                value: canary
        - setWeight: 50
        - pause: { duration: 10m }
        - setWeight: 100
```

`AnalysisTemplate` 定义判定规则。失败次数超过 `failureLimit`，AnalysisRun 进入 Failed，Rollout 自动 abort：

```yaml
apiVersion: argoproj.io/v1alpha1
kind: AnalysisTemplate
metadata:
  name: order-success-rate
spec:
  args:
    - name: version
  metrics:
    - name: success-rate
      interval: 1m
      successCondition: result[0] >= 0.99
      failureLimit: 2
      provider:
        prometheus:
          address: http://prometheus.monitoring.svc:9090
          query: |
            sum(rate(http_requests_total{app="order", version="{{args.version}}", code!~"5.."}[2m]))
            /
            sum(rate(http_requests_total{app="order", version="{{args.version}}"}[2m]))
    - name: p99-latency
      interval: 1m
      successCondition: result[0] < 0.5
      failureLimit: 3
      provider:
        prometheus:
          address: http://prometheus.monitoring.svc:9090
          query: |
            histogram_quantile(0.99,
              sum by (le) (rate(http_request_duration_seconds_bucket{app="order", version="{{args.version}}"}[5m]))
            )
```

注意 `version` label 必须真实存在于指标上，否则查询返回空，`successCondition` 对空结果的判定会让 AnalysisRun 直接失败——这既是保护，也是常见的「误 abort」来源。

观察与操作：

```bash
kubectl argo rollouts get rollout order --watch
kubectl argo rollouts promote order          # 人工放行到下一档
kubectl argo rollouts abort order            # 立即停止，流量回 stable
kubectl argo rollouts undo order             # 回滚到上一版本
```

### 4.4 Go 侧：把染色透传到下游

入口打的标记只在第一个进程有效，往下走要靠应用自己传。一个最小但完整的中间件：

```go
package release

import (
	"context"
	"log/slog"
	"net/http"

	"google.golang.org/grpc/metadata"
)

const (
	HeaderDye = "X-Canary"
	HeaderVer = "X-Served-By"
)

// Version 由构建时注入：-ldflags "-X .../release.Version=v1.4.0"
var Version = "unknown"

type dyeKey struct{}

func DyeFrom(ctx context.Context) string {
	if v, ok := ctx.Value(dyeKey{}).(string); ok {
		return v
	}
	return "stable"
}

// DyeMiddleware 归一化入口标记，并把自己的版本回写给客户端便于排查。
func DyeMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		dye := "stable"
		if r.Header.Get(HeaderDye) == "true" {
			dye = "canary"
		}
		ctx := context.WithValue(r.Context(), dyeKey{}, dye)
		w.Header().Set(HeaderVer, Version)
		next.ServeHTTP(w, r.WithContext(ctx))
	})
}

// InjectHTTP 给下游 HTTP 请求带上染色。
func InjectHTTP(ctx context.Context, req *http.Request) {
	if DyeFrom(ctx) == "canary" {
		req.Header.Set(HeaderDye, "true")
	}
}

// InjectGRPC 给下游 gRPC 调用带上染色。
func InjectGRPC(ctx context.Context) context.Context {
	if DyeFrom(ctx) == "canary" {
		return metadata.AppendToOutgoingContext(ctx, "x-canary", "true")
	}
	return ctx
}

// Log 保证每条日志都带 version 与 dye，便于按版本聚合。
func Log(ctx context.Context, msg string, args ...any) {
	args = append(args, "version", Version, "dye", DyeFrom(ctx))
	slog.InfoContext(ctx, msg, args...)
}
```

配套的两个约定：

- gRPC 服务端用 interceptor 从 `metadata` 读 `x-canary` 写回 context，和 HTTP 侧保持同一套 `dyeKey`；
- 日志采集端（Loki/ES）把 `version`、`dye` 提取成字段，指标端在 exporter 里把 `version` 作为 label，二者缺一，3.3 的判定就无从谈起。

### 4.5 一次完整的回滚演练

回滚不是「出了问题再说」，而是一条预先验证过的路径。演练按下面的顺序走，每一步都能独立停下：

```bash
# 1) 冻结放量：确认当前权重与 AnalysisRun 状态
kubectl argo rollouts get rollout order
kubectl argo rollouts status order

# 2) 中止发布：流量切回 stable，Rollout 标记 Degraded
kubectl argo rollouts abort order

# 3) 观察 stable：错误率、P99 是否在 1~2 分钟内回到基线
kubectl argo rollouts get rollout order --watch

# 4) 决定是否回退镜像：abort 只切流量，undo 才换回上一版 ReplicaSet
kubectl argo rollouts undo order

# 5) 数据兼容检查：灰度期间新版本写的数据，旧版本能不能读
```

第 5 步是演练里最容易形式化的一步，必须用 SQL 实际验证，而不是「应该没问题」：

```sql
-- 新版本写入的行，旧代码依赖的列是否都有值
SELECT count(*) FROM orders WHERE created_at >= now() - interval '1 hour' AND channel IS NULL;

-- 新版本是否写入了旧代码不认识的枚举值
SELECT DISTINCT status FROM orders WHERE created_at >= now() - interval '1 hour';
```

演练的验收标准：**从决定回滚到流量归零 ≤ 1 分钟，旧版本错误率在 5 分钟内回到基线，灰度期间写入的数据 100% 可被旧版本读取**。任何一条不满足，都要在下次发布前修掉——回滚路径的 bug 比发布本身的 bug 更致命，因为它出现在最慌的时候。

---

## 5. 版本与生态

**Ingress-NGINX**。canary 注解从 0.2x 时代就存在，长期是「没有 mesh 时最便宜的选择」。1.11 补齐了 `canary-by-header-pattern`（正则匹配），此后注解能力基本冻结；上游已宣布项目于 2026 年 3 月停止维护，推荐迁移到 Gateway API 实现。迁移时注意：Gateway API 的权重在 `HTTPRoute.backendRefs[].weight`，语义是相对权重，且没有「canary Ingress」这个概念——同一份 HTTPRoute 里列两个 backend 即可，规则更清晰，但需要控制器支持（Envoy Gateway、Istio、APISIX Ingress 都已支持）。

**Argo Rollouts 与 ArgoCD**。两者经常一起出现，但职责不同：ArgoCD 是 GitOps 同步器，负责「集群状态 = Git 里的期望状态」；Rollouts 是渐进式发布控制器，负责「从旧版本到新版本怎么走」。ArgoCD 把 `Rollout` 当作普通 CR 同步，不关心 steps；真正的放量、暂停、判定由 Rollouts 控制器完成。1.7 起 `trafficRouting` 支持插件化，NGINX、Istio、APISIX、ALB 等为内置实现，升级时留意 CRD 版本与控制器版本必须匹配。

**Istio**。`VirtualService` 的 `http[].route[].weight` 是流量切分的主入口，配合 `DestinationRule` 的 `subsets` 按版本分组；`mirror` + `mirrorPercentage` 做流量镜像。Istio 的优势是协议无关（HTTP/gRPC/TCP）和规则精细，代价是 sidecar/ambient 的资源开销与排障复杂度。已有 mesh 的集群没必要再在 Ingress 上叠一层灰度规则，两套规则冲突时很难定位。

**APISIX 3.x**。`traffic-split` 同时覆盖权重与内容规则，且能在网关层处理多协议、限流、鉴权，适合「没有 mesh 但需要比 Ingress 更细规则」的场景。它不负责自动判定，需要配合外部指标系统或 Argo Rollouts 的 APISIX 流量路由插件。

---

## 6. 常见坑

1. **会话保持让灰度比例失真**。cookie affinity 一开，老用户全被粘在 stable，权重只对新会话生效；灰度池里的用户又不会随权重下调而释放。要么关粘滞，要么用用户 ID 取模并接受「比例由分布决定」。
2. **缓存把旧版本响应喂给新版本**。CDN/浏览器缓存了旧 HTML，里面引用的是旧 chunk；或者 API 响应被缓存，灰度用户读到 stable 的数据。做法：HTML 一律 `no-store`，静态资源带内容哈希且长缓存，发布时主动 purge；API 响应按版本区分缓存键，或直接不缓存。
3. **MQ 消费者无法按比例灰度**。消息由消费组竞争消费，你无法保证「10% 的消息进新版本」；新旧消费者同时在线时，同一条消息只会被其中一个消费，行为不可控。可行方案：新版本用独立 consumer group + 独立 topic/tag，按消息头的 `dye` 路由；或者只灰度生产者，消费者保持全量兼容。
4. **数据库不兼容导致回滚失败**。新版本写了旧版本不认识的必填列、新枚举、新索引，回滚后旧代码直接报错。灰度期只做扩展性变更（加列、加表、加可空默认值），禁止重命名/删除/改类型；contract 阶段必须等旧版本完全下线。
5. **监控指标没有 `version` 维度**。只有聚合后的总错误率，无法判断是新版本还是旧版本贡献的，AnalysisRun 也查不到数据。上线前先确认 exporter 带 `version`/`rollout` label；同时控制基数，用 `major.minor` 而不是完整 git sha。
6. **灰度期间定时任务双跑**。canary 副本也会启动进程内的 cron，导致重复扣款、重复发券。用分布式锁/leader election（Redis、etcd、K8s Lease）保证全局单实例；或者把定时任务拆成独立 Deployment，按版本门控，不随业务 Pod 一起灰度。
7. **长连接不退出，流量切了但请求没切**。WebSocket、gRPC stream、长轮询会绕过权重，旧 Pod 在 `terminationGracePeriodSeconds` 内继续服务。配置 `preStop` 先摘流量再 sleep，客户端实现重连与超时，必要时主动断开旧连接。
8. **回滚路径从未演练**。镜像被 GC、回滚脚本引用了已删除的 ConfigMap、migration 不可逆——真出事时才发现回滚比修复更慢。每季度做一次 4.5 的演练，把回滚当成一等公民。

---

## 7. 选型对比

| 维度 | Ingress canary 注解 | APISIX traffic-split | Istio VirtualService | Argo Rollouts |
|---|---|---|---|---|
| 流量切分 | 权重 + header/cookie | 权重 + 任意表达式规则 | 权重 + 精细匹配 + 镜像 | 依赖下层入口/网关 |
| 协议 | HTTP | HTTP/gRPC/TCP（网关层） | HTTP/gRPC/TCP | 跟随 trafficRouting |
| 自动判定 | 无 | 无（需外部配合） | 无 | 内置 AnalysisRun |
| 运维成本 | 低（已装 Ingress 即可） | 中（多一个网关组件） | 高（mesh/sidecar） | 中（多一个控制器 + CRD） |
| 额外组件 | 无 | APISIX | Istiod + sidecar/ambient | Argo Rollouts |
| 适合 | 单集群、规则简单的 HTTP 服务 | 多协议、需要网关统一治理 | 已有 mesh、跨语言多协议 | 需要自动放量与自动回滚 |

结论按现状选，不要为了「先进」堆组件：

- **只有 Ingress**：注解就够，权重 + header 覆盖 80% 的场景，配合 Prometheus 告警和人工 abort 即可；
- **多协议或需要精细规则**：上 APISIX，`traffic-split` 一个插件同时解决权重与内容分流，还能顺便做限流鉴权；
- **已有 mesh**：直接用 Istio 的 `VirtualService`，不要再在入口层叠一套规则；
- **需要自动判定与自动回滚**：用 Argo Rollouts，下层的 Ingress/APISIX/Istio 只负责执行权重变更，判定交给 AnalysisRun。

最后一句：**灰度比例不是目的，缩短「发现问题」到「止血」的时间才是**。如果指标不全、回滚没演练，再精细的流量切分也只是把事故拉长。
