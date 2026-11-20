# Helm 技术介绍与使用指南

> **一句话定位**：Kubernetes 的包管理器——把「一堆 YAML」变成「可参数化、可版本化、可回滚的发布单元」。
>
> **上游仓库**：[helm/helm](https://github.com/helm/helm) · **版本基线**：Helm 3.19（3.x 主线）/ Helm 4.x · **协议**：Apache-2.0

---

## 1. 项目简介

Helm 解决的不是「怎么写 YAML」，而是「同一套 YAML 怎么在十个环境、二十个服务、上百次发布里活下去」。裸写 manifest 的项目通常在这三件事上翻车：

- **多环境**：dev / staging / prod 各存一份几乎相同的 Deployment，改一个镜像 tag 要改三处，最后靠 `sed -i` 和「我记得 prod 那份多一个 env」维护；
- **多服务**：一个业务 = 8 个微服务 × 每个 5 类资源，装的时候没人说得清「这套东西装到哪个 namespace、装过没有、上次装的是哪版」；
- **版本回滚**：`kubectl apply -f` 没有「上一次是什么」，回滚 = `git revert` + 重新 apply，而集群里被手动 `kubectl edit` 过的字段，git 里根本没有。

Helm 的做法是把一次安装抽象成 **Release**：模板 + 合并后的 values + 渲染出的 manifest + 递增的 revision，全部作为一条记录写回集群。于是「装过没有」「上次是什么」「差异在哪」「回滚到哪一版」都变成有答案的问题。

| 维度 | `kubectl apply -f` | `helm upgrade --install` |
|---|---|---|
| 参数化 | 无，靠复制文件或 sed | values 分层 + Go template |
| 发布状态 | 无，集群里只有最终对象 | Release 记录（含每版 manifest） |
| 回滚 | git revert 后重放，中间态不可知 | `helm rollback`，不依赖 git |
| 多资源一致性 | 逐条 apply，中途失败留半截 | 一次渲染，`--atomic` 失败整体回滚 |
| 依赖管理 | 手写、靠文档 | Chart 依赖 + 版本约束 + 锁文件 |
| 审计 | git log | `helm history` + `helm get values` |

**快速上手**——用现成 chart 跑一遍，感受 Release 的存在：

```bash
brew install helm    # macOS；Linux 用官方 get-helm-3 脚本或包管理器
helm repo add bitnami https://charts.bitnami.com/bitnami
helm repo update
helm install cache bitnami/redis -n demo --create-namespace \
  --set architecture=standalone --set auth.enabled=false
helm list -n demo && helm history cache -n demo

# 本地 chart 的开发闭环
helm create demo
helm template demo ./demo | kubectl apply --dry-run=server -f -
```

---

## 2. 能力总览

Helm 的世界里只有三个名词，关系是：**Repository 存 Chart，Chart 装成 Release**。

| 对象 | 是什么 | 存在哪 | 生命周期 |
|---|---|---|---|
| **Chart** | 模板包（目录或 `.tgz`），带 SemVer 版本 | OCI Registry / Chart 仓库 | 版本化，不可变 |
| **Release** | Chart 的一次安装实例，含 revision 历史 | 目标 namespace 的 Secret | install → upgrade… → uninstall |
| **Repository** | Chart 的索引（`index.yaml`）+ 包地址 | HTTP 服务或 OCI Registry | 由维护者发布 |

| 能力 | 机制 / 命令 | 典型场景 |
|---|---|---|
| 模板渲染 | Go template + Sprig 函数 | 一份 chart 产出多环境 manifest |
| values 分层 | `values.yaml` + `-f` + `--set` | 环境差异收敛到几十行文件 |
| 依赖管理 | `Chart.yaml` 的 `dependencies` | umbrella chart 一键装整套服务 |
| 按环境开关 | `condition` / `tags` | dev 开 Redis，prod 用托管实例 |
| 生命周期钩子 | `helm.sh/hook` 注解 | 装前建 schema、装后跑 smoke test |
| 发布验证 | `templates/tests/` + `helm test` | 发布后从集群内探活 |
| 打包分发 | `helm package` / `helm push` | 私有 OCI Registry 内部分发 |
| 发布编排 | `--wait` / `--timeout` / `--atomic` | CI 里做到「失败即回滚」 |
| 差异预览 | `helm diff`（插件） | 上线前人工审批的门禁 |

> Helm 不是配置管理工具（不装 agent），也不负责 CRD 的日常调谐（那是 Operator 的事）。在 GitOps 里它常只承担「渲染器」角色，由 Argo CD / Flux 决定何时 apply。

---

## 3. 核心概念与原理

### 3.1 Chart 目录结构与四类文件

```text
demo/
├── Chart.yaml          # 元数据：名称、版本、依赖、kubeVersion
├── values.yaml         # 默认值（必须能直接跑起来）
├── values.schema.json  # 可选：values 的 JSON Schema 校验
├── charts/             # 子 chart（helm dependency update 生成）
└── templates/
    ├── _helpers.tpl    # 命名模板，下划线开头，不渲染成 manifest
    ├── deployment.yaml
    ├── NOTES.txt       # 安装后打印的提示，不提交给集群
    └── tests/test-connection.yaml
```

```yaml
apiVersion: v2              # v1 是 Helm 2 的格式，新 chart 一律 v2
name: demo
version: 1.4.0              # chart 自身版本，改模板必须升这个
appVersion: "2.3.1"         # 被部署应用的版本，只是元数据
type: application           # application（默认）| library
kubeVersion: ">=1.27.0-0"   # 低于此版本的集群直接拒绝安装
dependencies: []            # 依赖写法见 3.6
```

四类文件分工明确：**元数据**决定「这是什么、依赖谁」，**默认值**决定「不传参时怎么跑」，**模板**决定「怎么渲染」，**依赖**是可复用的子 chart。只有 `templates/` 下的文件会被渲染成 manifest，`_` 开头与 `NOTES.txt` 例外。

### 3.2 模板上下文

模板里的 `.` 是渲染上下文，能取到六类信息：

| 对象 | 能取到什么 | 常用字段 |
|---|---|---|
| `.Values` | 合并后的 values | 自定义字段，如 `.Values.image.tag` |
| `.Release` | 本次发布的信息 | `Name` `Namespace` `Revision` `IsInstall` `IsUpgrade` `Service` |
| `.Chart` | Chart.yaml 的内容 | `Name` `Version` `AppVersion` `Annotations` |
| `.Capabilities` | 集群能力 | `KubeVersion` `APIVersions.Has` `HelmVersion` |
| `.Files` | chart 内非模板文件 | `Get` `GetBytes` `Glob` `Lines` `AsConfig` |
| `.Template` | 当前模板自身 | `Name` `BasePath`（用于 checksum 注解） |

取值示例：`{{ .Release.Name }}`、`{{ .Release.Namespace }}`、`{{ .Chart.AppVersion | quote }}`、`{{ .Values.image.tag }}`。两个易错点：`range` / `with` 会改变 `.` 的指向，块内访问根上下文要用 `$`（`$.Values`）；`.Capabilities.APIVersions.Has` 只在连接集群时准确，`helm template` 用的是编译进二进制的默认能力集。

### 3.3 values 的合并与覆盖顺序

渲染前 Helm 会把多层 values 深合并（map 逐 key 递归，list 整体替换），优先级从低到高：

1. 子 chart 自己的 `values.yaml`；
2. 父 chart `values.yaml` 中以子 chart 名（或 alias）为 key 的段；
3. 父 chart 的 `global`（下发到所有子 chart，子 chart 只读，自己再定义同名 key 无效）；
4. `-f` 指定的文件，按命令行出现顺序，后者覆盖前者；
5. `--set` / `--set-string` / `--set-json`，最后生效且优先于所有 `-f`。

```yaml
# 父 chart values.yaml
global:
  imageRegistry: registry.example.com   # 所有子 chart 都能读到
redis:
  enabled: true                          # 对应 charts/redis
  auth:
    enabled: false
```

子 chart 里直接写 `{{ .Values.global.imageRegistry }}/redis:7.2` 即可，不需要在子 chart 的 values.yaml 里声明 global。

`--set` 的类型推断是事故高发区：

| 写法 | 实际类型 | 结果 |
|---|---|---|
| `--set replicaCount=3` | int | 正常 |
| `--set enabled=true` | bool | 正常，但 `--set enabled="$FLAG"` 得到字符串 `"true"` |
| `--set image.tag=1.20` | **float 1.2** | tag 变成 `1.2`，镜像拉不到 |
| `--set-string image.tag=1.20` | string | 正确 |
| `--set-json 'ports=[80,443]'` | 数组 | 传数组/对象时用这个 |

结论：**镜像 tag、版本号、密码一律 `--set-string`**；模板侧对不确定类型先用 `quote`，需要比较时用 `toString`。

升级时还有个隐藏开关：`helm upgrade` 默认是 `--reset-values`（以 chart 的 values 为基线重新合并），`--reuse-values` 会把上次 release 的 values 全量带上——删掉某个 key 后它会继续残留，除临时排障外不要用。

### 3.4 命名模板：define / template / include

| 动作 | 是否输出 | 能否被管道 | 用途 |
|---|---|---|---|
| `define` | 否 | — | 定义片段，等待被调用 |
| `template` | 是，直接写入输出 | 否 | 早期写法，已不推荐 |
| `include` | 否，返回字符串 | 是 | 当前标准写法，可接 `nindent` / `tpl` |

```yaml
{{/* templates/_helpers.tpl */}}
{{- define "demo.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "demo.selectorLabels" -}}
app.kubernetes.io/name: {{ default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "demo.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 }}
{{ include "demo.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}
```

调用方必须自己处理缩进，因为 `include` 返回的是裸字符串：`labels: {{- include "demo.labels" . | nindent 4 }}`；若用 `template` 就只能写在行首，这正是它被 `include` 取代的原因。

约定：命名模板统一 `<chart>.<用途>` 前缀（子 chart 的同名 define 会互相覆盖，这是全局命名空间）；`selectorLabels` 只放**不可变**的标签，因为 Deployment 的 selector 一旦发布就不能改；名字统一 `trunc 63 | trimSuffix "-"`，否则长 release 名会被 API Server 直接拒绝。用户的 values 里如果存了模板片段，用 `tpl` 二次渲染：`{{ tpl .Values.configTemplate . }}`。

### 3.5 Hooks：把「一次性动作」插进发布流程

Hook 就是一个带注解的普通资源，Helm 在特定阶段单独 apply 它：

| Hook | 执行时机 |
|---|---|
| `pre-install` | 模板渲染完、任何资源创建前 |
| `post-install` | 所有资源提交后（加了 `--wait` 则等就绪后） |
| `pre-upgrade` / `post-upgrade` | 升级前后，最常用于数据库迁移 |
| `pre-delete` / `post-delete` | 卸载前后 |
| `pre-rollback` / `post-rollback` | 回滚前后 |
| `test` | `helm test` 时执行 |

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: {{ include "demo.fullname" . }}-migrate
  annotations:
    "helm.sh/hook": pre-install,pre-upgrade
    "helm.sh/hook-weight": "-5"
    "helm.sh/hook-delete-policy": before-hook-creation,hook-succeeded
spec:
  backoffLimit: 0
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: migrate
          image: "{{ .Values.image.repository }}:{{ .Values.image.tag | default .Chart.AppVersion }}"
          args: ["/app/migrate", "up"]
```

三条规则必须记住：

- `hook-weight` 决定同阶段内的顺序，**数值小的先跑**（可为负），默认 0；
- `hook-delete-policy` 有 `before-hook-creation`（下次跑前删旧的）、`hook-succeeded`、`hook-failed` 三种，**默认什么都不删**；
- Hook 资源不属于 release 的 manifest，`helm uninstall` 不会清理它们，`helm rollback` 也不会回滚它们。

因此迁移 Job 的标配是 `before-hook-creation,hook-succeeded`：成功即清理，失败留现场给人看。若加了 `hook-failed`，失败也会被删掉，日志就没了——只在 Job 自己会把日志推到外部系统时才这么配。

### 3.6 依赖管理：condition 与 tags

```yaml
# Chart.yaml
dependencies:
  - name: redis
    version: "~20.6.0"
    repository: https://charts.bitnami.com/bitnami
    condition: redis.enabled
    tags:
      - cache
  - name: postgresql
    version: "16.x.x"
    repository: https://charts.bitnami.com/bitnami
    condition: postgresql.enabled
    alias: db            # 同一 chart 装两份时用 alias 区分

# ---- values.yaml ----
tags:
  cache: true          # 该 tag 下所有依赖默认启用
redis:
  enabled: true        # condition 优先于 tags
postgresql:
  enabled: false
db:                    # alias 后的配置段用 alias 名
  auth:
    database: app
```

- `condition` 指向父 values 里的布尔路径，支持逗号分隔多路径（取第一个存在的），值存在时**优先于 tags**；
- `tags` 是「任一为 true 即启用」的或逻辑，适合按「缓存类 / 监控类」整组开关，命令行可覆盖：`--set tags.cache=false`；
- `helm dependency update` 会生成 `Chart.lock` 和 `charts/*.tgz`，**锁文件和 tgz 都要提交**，否则 CI 上每次解析出的版本可能不同。

---

## 4. 使用实战

### 4.1 从零写一个生产级 Chart

先定 values（默认值要能单机跑起来，理由见 4.2）：

```yaml
replicaCount: 2
image:
  repository: ghcr.io/example/api
  tag: ""                       # 空则回落到 Chart.AppVersion
service: { type: ClusterIP, port: 8080 }
config: { logLevel: info, maxBodyMB: 8 }
resources: { requests: { cpu: 100m, memory: 128Mi }, limits: { memory: 512Mi } }
autoscaling:
  enabled: false
  minReplicas: 2
  maxReplicas: 10
  targetCPUUtilizationPercentage: 70
```

`_helpers.tpl` 用 3.4 的版本，然后四个模板：

```yaml
# templates/ 下的四个资源：deployment.yaml / service.yaml / configmap.yaml / hpa.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ include "demo.fullname" . }}
  labels:
    {{- include "demo.labels" . | nindent 4 }}
spec:
  {{- if not .Values.autoscaling.enabled }}
  replicas: {{ .Values.replicaCount }}
  {{- end }}
  selector:
    matchLabels:
      {{- include "demo.selectorLabels" . | nindent 6 }}
  template:
    metadata:
      annotations:
        checksum/config: {{ include (print $.Template.BasePath "/configmap.yaml") . | sha256sum }}
      labels:
        {{- include "demo.selectorLabels" . | nindent 8 }}
    spec:
      containers:
        - name: {{ .Chart.Name }}
          image: "{{ .Values.image.repository }}:{{ .Values.image.tag | default .Chart.AppVersion }}"
          ports:
            - name: http
              containerPort: {{ .Values.service.port }}
          envFrom:
            - configMapRef:
                name: {{ include "demo.fullname" . }}-config
          resources:
            {{- toYaml .Values.resources | nindent 12 }}
---
apiVersion: v1
kind: Service
metadata:
  name: {{ include "demo.fullname" . }}
  labels:
    {{- include "demo.labels" . | nindent 4 }}
spec:
  type: {{ .Values.service.type }}
  ports:
    - name: http
      port: {{ .Values.service.port }}
      targetPort: http
  selector:
    {{- include "demo.selectorLabels" . | nindent 4 }}
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: {{ include "demo.fullname" . }}-config
  labels:
    {{- include "demo.labels" . | nindent 4 }}
data:
  LOG_LEVEL: {{ .Values.config.logLevel | quote }}
  MAX_BODY_MB: {{ .Values.config.maxBodyMB | quote }}
---
{{- if .Values.autoscaling.enabled }}
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: {{ include "demo.fullname" . }}
  labels:
    {{- include "demo.labels" . | nindent 4 }}
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: {{ include "demo.fullname" . }}
  minReplicas: {{ .Values.autoscaling.minReplicas }}
  maxReplicas: {{ .Values.autoscaling.maxReplicas }}
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: {{ .Values.autoscaling.targetCPUUtilizationPercentage }}
{{- end }}
```

两个设计取舍：HPA 开启时**不再写 replicas**（否则每次 apply 都把副本数拉回固定值，和 HPA 打架）；ConfigMap 用 `sha256sum` 注解挂在 Pod 上，内容一变 Pod 模板就变，滚动更新才会被触发——这是「改了配置不生效」的标准解法。生产中再补上 `readinessProbe`、`securityContext` 与 `topologySpreadConstraints`，思路与 [Docker/K8s 篇](../docker-k8s/docker-k8s-guide.md) 一致。

### 4.2 多环境 values 分层

约定布局：`charts/demo/values.yaml` 放默认值（单副本、ClusterIP，kind/minikube 直接能跑），`values/{dev,staging,prod}.yaml` 只写差异（副本数、资源、HPA）。

```bash
helm upgrade --install api ./charts/demo -n prod --create-namespace \
  -f values/prod.yaml \
  --set-string image.tag="$GIT_SHA" \
  --wait --timeout 5m --atomic
```

三条硬规则：**默认值必须可跑**（不传参就能在本地起来）、**环境文件只写差异**（重复写全量等于复制三份）、**敏感信息不进 values**（用 External Secrets / CSI Driver，values 里只放引用名）。

再加一层 `values.schema.json` 做类型与必填校验，把「tag 写成空字符串」「resources 少写 limits」挡在渲染之前。排障时用 `helm get values api -n prod -a` 看线上实际生效的全部 values，比翻 CI 日志快得多。

### 4.3 helm template + Kustomize 混用

两者的强项不重叠，分工要写死：

| 关注点 | 交给 Helm | 交给 Kustomize |
|---|---|---|
| 条件与循环（要不要 HPA、要不要 Ingress） | ✅ | 做不到 |
| 环境差异（副本数、镜像、资源） | ✅ values | 要写 patch |
| 注入 sidecar、改探针、加注解 | 改模板很别扭 | ✅ patch |
| 多团队共用一份上游 chart | ✅ 依赖 | ✅ overlay 覆盖 |

```bash
# Helm 负责参数化，产出「已渲染的 manifest」
helm template api ./charts/demo -n prod -f values/prod.yaml > base/api.yaml

# Kustomize 负责最后的结构性 patch
kustomize build overlays/prod | kubectl apply -f -
```

代价是**同一个字段只能有一个主人**：如果 Helm 模板里写了 `replicas`，Kustomize 又 patch 了 `replicas`，最终值取决于执行顺序，排障非常痛苦。约定「Helm 管参数，Kustomize 管结构」，不交叉。Argo CD 同时支持两者，但一个 Application 里通常只用一种渲染方式。

### 4.4 diff / rollback / history 的日常用法

```bash
helm plugin install https://github.com/databus23/helm-diff

# --detailed-exitcode：0=无差异，1=出错，2=有差异
helm diff upgrade api ./charts/demo -n prod -f values/prod.yaml --detailed-exitcode

helm history api -n prod
helm rollback api 12 -n prod --wait
```

CI 里的门禁写法（在 `set -e` 下不能直接用 `--detailed-exitcode`，非 0 会中断脚本）：

```bash
helm diff upgrade api ./charts/demo -n prod -f values/prod.yaml \
  --detailed-exitcode > /tmp/diff.txt 2>&1 && rc=0 || rc=$?
case "$rc" in
  0) echo "无变更，跳过发布" ;;
  2) cat /tmp/diff.txt; echo "有变更，等待审批" ;;   # 把 diff 贴到 PR 评论
  *) cat /tmp/diff.txt; exit 1 ;;
esac
```

`helm rollback` 恢复的是**上一版渲染结果**，不保证覆盖被手动 `kubectl edit` 的字段（三方合并会尽量保留）。所以发布后要改东西，走新的 revision，别在集群里手改。

### 4.5 用 helm-unittest 写模板单测

```bash
helm plugin install https://github.com/helm-unittest/helm-unittest
helm unittest ./charts/demo -f 'tests/*_test.yaml'
```

```yaml
# tests/deployment_test.yaml
suite: deployment
templates:
  - deployment.yaml
tests:
  - it: 默认使用 Chart.AppVersion 作为镜像 tag
    asserts:
      - equal:
          path: spec.template.spec.containers[0].image
          value: ghcr.io/example/api:1.4.0
  - it: 显式覆盖 tag 与副本数
    set:
      image.tag: "9.9.9"
      replicaCount: 3
    asserts:
      - equal:
          path: spec.template.spec.containers[0].image
          value: ghcr.io/example/api:9.9.9
      - equal:
          path: spec.replicas
          value: 3
```

默认 release 名是 `RELEASE-NAME`，枚举类断言用 `matchRegex` 匹配后缀比写死全名更稳。这套测试的价值在于：模板是代码，改 `_helpers.tpl` 时能立刻知道哪几个资源的名字/标签被连带改了——`helm lint` 只查语法，查不出语义。

---

## 5. 版本与生态

| 阶段 | 关键变化 |
|---|---|
| Helm 2 | 服务端组件 **Tiller**，release 记录存 ConfigMap；`helm init` 初始化 |
| Helm 3（2019） | **移除 Tiller**，release 改存 Secret；引入 `library` chart、`lookup`、JSON Schema 校验 |
| Helm 3.8+ | OCI 支持 GA，`helm push` / `oci://` 成为一等公民；3.1x 进入维护模式，只修 bug |
| Helm 4（2025 年底发布） | 插件改 WASM、server-side apply、kstatus 等待、移除废弃字段 |

**Tiller 为什么必须死**：它是以 cluster-admin 权限跑的集群级服务端，任何能执行 `helm install` 的人都能间接拿到集群最高权限；多租户下只能靠 Tiller 自己的 namespace 隔离，等于没有隔离。v3 把「渲染 + 提交」全放到客户端，用**执行者的 kubeconfig** 走 RBAC，release 记录也存在目标 namespace 的 Secret 里，权限模型终于和 kubectl 一致。另一个变化是升级策略：v3 用**三方合并**（old manifest / new manifest / live state）计算 patch，能保留集群里被别人加上的字段；v2 是两方合并，容易把手改的配置抹掉。

v4 的变化（以官方 release notes 为准；生产建议仍以 3.x 为主，理由是新版本插件生态与 CI 镜像尚在跟进）：插件机制改为 **WASM**（跨平台与沙箱更好，现成插件需重写）；升级默认走 **server-side apply**，字段所有权交给 API Server，与 `kubectl apply` 行为对齐；等待逻辑换成 **kstatus**，对自定义资源就绪状态判断更准；明确 OCI 为默认分发方式，传统 `index.yaml` 仓库进入维护状态。

Release 记录就存在目标 namespace：Secret 名为 `sh.helm.release.v1.<release>.v<revision>`，内容是 gzip + base64 的 JSON（manifest、values、chart 元数据全在里面），所以 `kubectl get secret -l owner=helm` 能看到全部历史。

OCI 分发已是内部 chart 的标准做法：

```bash
helm package ./charts/demo --version 1.4.0
helm registry login registry.example.com
helm push demo-1.4.0.tgz oci://registry.example.com/charts
helm install api oci://registry.example.com/charts/demo --version 1.4.0 -n prod
```

---

## 6. 常见坑与最佳实践

1. **`--set` 把值变成字符串或 float**：`--set enabled="$FLAG"` 得到字符串 `"true"`，模板里 `eq .Values.enabled true` 直接报类型错；`--set image.tag=1.20` 变成 `1.2`。镜像 tag / 版本号一律 `--set-string`，模板侧必要时 `| toString`。
2. **YAML 缩进错位**：`toYaml` 输出不带缩进，必须 `| nindent 4`；`include` 同理。少一个空格就是「渲染成功但字段挂错层级」，`helm template` 不报错，API Server 才报。
3. **`lookup` 在 `helm template` 下返回空**：模板里拿集群现状（如「已存在的 Secret 就复用」）时，`helm template` / `--dry-run` 没有集群连接，`lookup` 返回空 map，模板走 else 分支，渲染结果与真实安装不一致。要么忍（CI 用 `--dry-run=server`），要么把逻辑挪到 hook / Operator。
4. **随机函数造成 diff 噪音**：`randAlphaNum`、`now`、`uuidv4` 每次都变，`helm diff` 永远显示有变更，审批门禁形同虚设。密码类用 `lookup` 保住旧值或走外部密钥，时间戳只在 `NOTES.txt` 里用。
5. **Hook 的 Job 不会自动删除**：默认不配 `hook-delete-policy`，每次升级都堆一个 `Completed` 的 Job，几百个之后 `kubectl get pods` 都刷不动。配 `before-hook-creation,hook-succeeded`。
6. **`--wait` 卡住不是 Helm 的问题**：它等的是 Deployment 可用副本、PVC 绑定、Service 有 endpoint。readinessProbe 写错、镜像拉不下来、资源不够调度，都会一路等到 `--timeout` 再失败。配合 `--atomic` 才会自动回滚，否则就是「卡在中间态」。
7. **子 chart 的 values 覆盖不生效**：父 values 里的 key 必须是**依赖名或 alias**（`redis.auth.password`），不是 `redis.values.auth.password`；开了 `condition` 但父 values 里没写 `redis.enabled: true`，子 chart 可能根本不渲染。
8. **`crds/` 目录的 CRD 不会升级**：`crds/` 只在 install 时安装，`helm upgrade` 完全跳过；放 `templates/` 里又会在卸载时被删掉（连带所有 CR）。CRD 用独立 chart 或 Operator 管理。
9. **改不可变字段**：`Service` 的 `clusterIP`、`Deployment` 的 `selector` 改了会直接被拒绝。`--force` 是「删了重建」，对有状态服务等于事故，宁可变名新建。
10. **history 无限增长**：默认保留 10 个 revision（`--history-max`），大 chart 单个 revision 的 Secret 可能几 MB，etcd 会被撑大。CI 里显式设 `--history-max 5`。
11. **把 Helm 当 GitOps**：`helm upgrade` 是「推」模型，集群漂移无人纠正。要持续收敛就交给 Argo CD / Flux，Helm 只负责渲染。

---

## 7. 选型对比

| 维度 | Helm | Kustomize | 纯 kubectl | Terraform |
|---|---|---|---|---|
| 抽象层次 | 包 + 发布（Chart/Release） | 文件覆盖（base/overlay） | 单文件 | 资源图（云与 K8s 统一） |
| 状态管理 | Release 存集群 Secret | 无状态 | 无状态 | 远端 state 文件 |
| 参数化能力 | 强（模板 + values 分层） | 中（patch / 替换） | 无 | 强（变量 + 模块） |
| 能否表达复杂逻辑 | **能**（条件/循环/函数） | 弱（刻意不支持） | 不能 | 能（HCL 表达式） |
| 学习成本 | 中高（Go template + Sprig） | 低 | 最低 | 高 |
| 回滚 | `helm rollback` 秒级 | 靠 git revert | 无 | 重新 apply 上一版 |
| 适合场景 | 多环境、多服务、对外分发 | 小项目、改上游 manifest | 学习、临时排查 | 集群本身 + 云资源 |
| 生态 | 最广（Artifact Hub 数千 chart） | K8s 内置（`kubectl -k`） | 内置 | Provider 生态最大 |

决策路径：

- **只部署一两个服务、环境差异就是镜像 tag** → Kustomize（或直接 `kubectl -k`），别引入模板语言；
- **同一套服务要装到 5 个环境、几十个客户现场，还要给别人用** → Helm，参数化和版本回滚是刚需；
- **上游 chart 是第三方的，只想改几个字段** → Helm 渲染 + Kustomize patch（4.3），或 fork 上游 chart；
- **要管集群本身、云数据库、DNS、IAM** → Terraform；Helm 只管集群内的工作负载，边界别混；
- **已经在用 Argo CD / Flux** → 仍然需要 Helm（或 Kustomize）作为渲染层，GitOps 工具不替代打包与参数化。

一句话：**小项目 Kustomize 就够，多环境多服务才上 Helm**。等到「改一个镜像 tag 要动三个仓库」的那天再迁也不迟，但成本随服务数量线性增长，超过 5 个服务就该提前定规范。
