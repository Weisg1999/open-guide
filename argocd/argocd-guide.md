# Argo CD 技术指南（Kubernetes GitOps 持续交付）

> **一句话定位**：GitOps 持续交付控制器——把「集群应该长什么样」写进 Git，由控制器持续把现实拉回期望。
>
> **上游仓库**：[argoproj/argo-cd](https://github.com/argoproj/argo-cd) · **版本基线**：ArgoCD v3.x · **协议**：Apache-2.0

---

## 1. 项目简介

传统 CD 是「CI 推」：流水线拿着生产集群的 kubeconfig，构建完镜像后执行 `kubectl apply` 或 `helm upgrade`。这条路能跑，但留下三个回答不了的问题——**谁改的、改了什么、能不能回滚**。集群的实际状态没有版本记录，凭据散落在每个 CI 项目里，有人手工 `kubectl edit` 造成的漂移无人知晓。

GitOps 把方向反过来，变成「CD 拉」：CI 只负责构建镜像、把新 tag 写回 Git；部署由集群内的 ArgoCD 主动拉取 Git、渲染清单、与实时状态对比、再同步。于是「集群里跑的是什么」等于「Git 某个 commit 的渲染结果」，上面三个问题分别对应 commit 作者、`argocd app diff`、revert 一个 commit。

```text
CI：代码 → 测试 → 构建镜像 → 修改 Git 中的 image tag（产生一个 commit）
                                      │
CD：ArgoCD 轮询 Git ──► 渲染（Helm / Kustomize / 纯 YAML）──► diff vs 集群 ──► sync
                                      ▲                                        │
                                      └────────── 状态回写、事件通知 ◄──────────┘
```

- **适用**：Kubernetes 上的应用与平台组件交付、多集群统一发布、需要审计与一键回滚的生产环境、把「环境差异」收敛成目录/values 差异
- **不适用**：非 K8s 资源（虚拟机、裸机进程、数据库实例）为主的部署、需要复杂编排与人工审批节点的流程（那是工作流引擎的活）、无 Git 可用的遗留系统
- **与 CI 的分工**：Jenkins / GitLab CI / GitHub Actions 负责编译、测试、构建并推送镜像；Argo Workflows 是 DAG 工作流引擎，同样属于 CI/批处理侧；**ArgoCD 不构建镜像、不跑测试，只负责「把 Git 里的清单变成集群里的对象」**

> 最常见的误用是让 Jenkins 继续持有生产 kubeconfig，只在最后一步调用 `argocd app sync`。这样 ArgoCD 退化成一个远程 `kubectl`，Git 不是唯一事实来源，审计链断在 CI 里。

---

## 2. 能力总览

| 能力 | 承载对象 / 组件 | 说明 |
|---|---|---|
| 部署单元 | `Application` | 一个 Git 路径 + 一个目标集群/namespace 的绑定，是同步与健康状态的最小单位 |
| 多租户边界 | `AppProject` | 限定允许的 Git 仓库、目标集群/namespace、可创建的资源类型与 RBAC 角色 |
| 批量生成 | `ApplicationSet` | 用 list / git / cluster / matrix 生成器批量产出 Application |
| 同步 | application-controller | 手动或自动 sync，支持 `prune`、`selfHeal`、`ApplyOutOfSyncOnly` |
| 差异计算 | repo-server + 控制器 | 渲染 desired，与 live 做结构化 diff；支持 `ignoreDifferences` |
| 健康评估 | 内置 health check + Lua | Deployment / StatefulSet / Job / PVC 等有内置规则，CRD 可自定义 |
| 多集群 | cluster Secret | 用独立 ServiceAccount 凭据注册外部集群，集中从一个控制面管理 |
| 发布编排 | Sync Waves + Hooks | 按注解排序，配合 `PreSync` / `PostSync` Job 做迁移、校验、通知 |
| 认证与授权 | Dex / OIDC + RBAC | SSO 登录，`argocd-rbac-cm` 按 role 映射 policy |
| 回滚 | 同步历史 | 保留已同步 revision，可回滚到任一历史点；推荐用 Git revert |
| 通知 | argocd-notifications | 订阅 sync / health 状态变化，推送到 Slack、Webhook、邮件 |
| 声明式自管理 | App of Apps / ApplicationSet | ArgoCD 自己也可以由 Git 中的 Application 管理 |

---

## 3. 核心概念与原理

### 3.1 控制循环：desired state 与 live state

ArgoCD 的核心是一个**对比—收敛**循环，但要注意它和 controller-runtime 的紧循环不同：默认每 3 分钟才拉一次 Git（`argocd-cm` 的 `timeout.reconciliation: 180s`），配置 webhook 后可被 push 事件提前触发。因此「提交了代码但页面还没变」通常只是没到下一个周期，不是故障。

| 概念 | 含义 |
|---|---|
| desired state | repo-server 把 Git 中的目录渲染成一组 K8s 对象（Helm 渲染、Kustomize build、纯 YAML 直读） |
| live state | 通过目标集群 API 实时读取的对象，含控制器回填的 `status`、默认值、`resourceVersion` |
| diff | 两者按字段比较；`OutOfSync` 表示存在差异，`Synced` 表示一致 |
| sync | 把 desired 写进集群（apply / create / prune），并记录 revision |

三个容易踩的认知偏差：

1. **diff 比的是「渲染结果」而不是「Git 文本」**。Helm 的 `lookup`、集群版本相关的 Capabilities、Kustomize 的 hash 后缀都会让渲染结果与 `helm template` 本地输出不同。
2. **`OutOfSync` 不等于服务坏了**。它只说明有差异；服务是否可用看 `health.status`（`Healthy` / `Progressing` / `Degraded` / `Missing`）。
3. **同步成功也不等于收敛完成**。同步命令返回成功只代表对象已被 API Server 接受，Pod 是否 Ready 要看 `health.status`；需要阻塞等待时用 `argocd app wait --health`。

### 3.2 三层对象：Application、AppProject、ApplicationSet

```text
AppProject（边界：能用哪些 repo / 集群 / namespace / 资源类型）
   └── Application（一个部署单元：repo+path+revision → cluster+namespace）
          └── 渲染出的 K8s 对象（Deployment / Service / ConfigMap ...）

ApplicationSet（生成器）──► 批量创建 N 个 Application（每个环境/集群一个）
```

- **Application** 是操作单位，删除时是否级联删除集群资源取决于 finalizer：`resources-finalizer.argocd.argoproj.io` 会删资源，`resources-finalizer.argocd.argoproj.io/background` 异步删；不加 finalizer 只删 CR，集群资源成为孤儿。
- **AppProject** 是安全边界而非目录。默认 `default` 项目几乎不设限，生产必须自建 Project 并写死 `sourceRepos`、`destinations`、`clusterResourceWhitelist`。`orphanedResources` 警告能发现「不在 Git 里但还活着」的资源。
- **ApplicationSet** 解决的是 N×M 的组合爆炸：3 个环境 × 2 个集群不必手写 6 份 YAML。它由独立控制器负责生成与回收 Application，删除 ApplicationSet 时通过 `preserveResourcesOnDeletion` 决定是否连带删除 Application。

### 3.3 同步策略：手动、自动、selfHeal 与 prune

| 配置 | 行为 | 风险 |
|---|---|---|
| 默认（无 `automated`） | 只在 UI/CLI 触发的 sync 时写集群 | 漂移会一直存在，直到有人手动同步 |
| `automated: {}` | 检测到 `OutOfSync` 自动 sync | 一次错误提交会被立即放大到集群 |
| `automated.prune: true` | 同步时删除「Git 里已不存在」的资源 | 删 namespace / CRD 会连带清空数据，最危险的一项 |
| `automated.selfHeal: true` | 集群对象被改动后自动改回 Git 版本 | 与 HPA、其他控制器争夺字段时会反复打架 |
| `syncOptions: [PruneLast=true]` | 其他资源都健康后才执行 prune | 降低「先删后建」导致的中断 |
| `syncOptions: [CreateNamespace=true]` | 目标 namespace 不存在时自动创建 | 方便，但也掩盖了 namespace 归属问题 |

生产上的常用组合是**自动同步开启、prune 开启、selfHeal 开启，但用 AppProject 与 `Prune=false` 注解保护关键资源**：

```yaml
metadata:
  annotations:
    argocd.argoproj.io/sync-options: Prune=false,Delete=false
```

`Prune=false` 让该对象在 Git 删除后仍保留在集群里；`Delete=false` 阻止删除 Application 时级联删它。适合 CRD、namespace、PV、外部创建的 Secret。

`ignoreDifferences` 与 `RespectIgnoreDifferences=true` 解决的是另一类问题：某些字段由别的控制器写，不该被 ArgoCD 覆盖。两者必须成对使用，只配 `ignoreDifferences` 只能让状态显示为 `Synced`，同步时仍会把字段改回去。

### 3.4 Sync Waves 与资源钩子

同一批资源不是同时创建的：`argocd.argoproj.io/sync-wave` 注解把同步分成若干波次，**数值小的先执行**（可为负），同 wave 内再按资源 kind 的内置顺序排。典型编排是「namespace / CRD（-2）→ 配置与 Secret（-1）→ 工作负载（0）→ 校验 Job（1）」。

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: demo-migrate
  annotations:
    argocd.argoproj.io/hook: PreSync
    argocd.argoproj.io/hook-delete-policy: BeforeHookCreation,HookSucceeded
    argocd.argoproj.io/sync-wave: "-1"
spec:
  backoffLimit: 1
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: migrate
          image: ghcr.io/acme/demo:v1.4.0
          command: ["/app/migrate", "--up"]
```

| 钩子 | 执行时机 | 典型用途 |
|---|---|---|
| `PreSync` | 同步主资源之前 | 数据库迁移、备份、前置校验 |
| `Sync` | 与主资源同批（默认） | 需要与工作负载同时存在的辅助 Job |
| `PostSync` | 所有资源健康之后 | 冒烟测试、注册、通知 |
| `SyncFail` | 同步失败时 | 回滚外部系统、发告警 |
| `Skip` | 永不随 sync 应用 | 只在手动 `kubectl apply` 时使用的一次性对象 |

钩子对象**不属于 Application 的常规管理资源**，不会因为 Git 里删掉就被 prune；靠 `hook-delete-policy` 清理，否则 Job 会一直留着，甚至因 `ttlSecondsAfterFinished` 缺失而堆积。钩子失败的 Application 会停在 `Degraded`，先看 `kubectl -n <ns> logs job/<name>`。

### 3.5 健康评估：内置规则与 Lua 自定义

健康状态由控制器对每个对象求值后聚合：任一对象 `Degraded` → Application `Degraded`；任一 `Progressing` → `Progressing`；全部 `Healthy` 才算 `Healthy`。Deployment 看 `observedGeneration`、可用副本与更新完成；StatefulSet 看 partition 与 ready 副本；Job 看 `succeeded` / `failed`；PVC 看 phase。

CRD 默认没有健康规则，会长期停在 `Progressing`，这是「自定义资源永远不 Healthy」的根因。用 `argocd-cm` 的 Lua 补一条：

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: argocd-cm
  namespace: argocd
data:
  resource.customizations.health.example.com_MyApp: |
    hs = {}
    if obj.status ~= nil and obj.status.conditions ~= nil then
      for _, c in ipairs(obj.status.conditions) do
        if c.type == "Ready" and c.status == "True" then
          hs.status, hs.message = "Healthy", c.message
          return hs
        end
        if c.type == "Ready" and c.status == "False" then
          hs.status, hs.message = "Degraded", c.message
          return hs
        end
      end
    end
    hs.status, hs.message = "Progressing", "waiting for Ready condition"
    return hs
```

key 的格式固定为 `<group>_<kind>`，group 为空时写 `_<kind>`（如 `_ConfigMap`）。Lua 在控制器里执行，**不要写循环等待或网络调用**，只读 `obj` 返回状态。

### 3.6 密钥管理：SOPS、Sealed Secrets、External Secrets

三者的差别不在「能不能加密」，而在**信任边界放在哪**。

| 方案 | 密文存放 | 解密者 / 信任边界 | 轮换与多集群 |
|---|---|---|---|
| SOPS | Git 中的密文文件 | 持有 KMS / age 私钥的一方；ArgoCD 需装 `ksops` 或 helm-secrets 插件 | 重新加密提交；同一密文可被多集群解，取决于 key 分发 |
| Sealed Secrets | Git 中的 `SealedSecret` CR | 集群内 controller 的私钥，密文与 namespace/name 绑定 | 换集群要重新 seal；私钥必须备份，丢了无法解 |
| External Secrets Operator | Git 中只有 `ExternalSecret` 引用 | 集群内 ESO 用 Workload Identity / SecretStore 向 Vault、云 Secrets Manager 取 | 外部系统轮换，ESO 定时刷新；多集群各自拉取，Git 里没有密文 |

判断方法：**谁能读到明文**。SOPS 意味着任何拿到私钥的人都能在集群外解密；Sealed Secrets 把解密能力锁在单个集群；ESO 让明文根本不进 Git，但把信任交给了外部密钥系统与云 IAM。没有一种方案能同时做到「Git 里有完整密文、跨集群通用、且无需管理密钥」。

> 不要把「CI 里解密后再提交明文」当成方案——那只是把密钥搬到了 Git 历史里，且永久可追溯。

---

## 4. 使用实战

### 4.1 安装与第一个 Application

```bash
kubectl create namespace argocd
kubectl apply -n argocd -f https://raw.githubusercontent.com/argoproj/argo-cd/v3.0.0/manifests/install.yaml
kubectl -n argocd rollout status deploy/argocd-server
kubectl -n argocd port-forward svc/argocd-server 8080:443

# CLI 登录；initial-password 读取 argocd-initial-admin-secret 中的初始密码
argocd login localhost:8080 --username admin --password "$(argocd admin initial-password -n argocd | head -1)" --insecure
```

私有仓库先配凭据，推荐声明式 Secret，便于随 Git 一起恢复：

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: repo-platform
  namespace: argocd
  labels:
    argocd.argoproj.io/secret-type: repository   # repo-creds 表示按 URL 前缀通配
stringData:
  type: git
  url: https://git.example.com/platform/deploy.git
  username: ci-bot
  password: glpat-xxxxxxxx
  insecure: "false"                              # 自签证书时才置 "true"
```

先建 Project 再建 Application，否则 `spec.project: demo` 会报 `project not found`：

```yaml
apiVersion: argoproj.io/v1alpha1
kind: AppProject
metadata: {name: demo, namespace: argocd}
spec:
  sourceRepos:
    - https://git.example.com/platform/deploy.git
    - https://charts.example.com        # 多源模式里的 Helm chart 仓库
  destinations:
    - {server: https://kubernetes.default.svc, namespace: 'demo-*'}
    - {server: https://dr.example.com:6443, namespace: 'demo-*'}
  clusterResourceWhitelist:
    - {group: '', kind: Namespace}     # CreateNamespace=true 需要这一条
```

第一个 Application，把「渲染 → 对比 → 同步」跑通：

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: demo-dev
  namespace: argocd
  finalizers:
    - resources-finalizer.argocd.argoproj.io
spec:
  project: demo
  source:
    repoURL: https://git.example.com/platform/deploy.git
    targetRevision: main
    path: apps/demo/overlays/dev
  destination:
    server: https://kubernetes.default.svc
    namespace: demo-dev
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
    syncOptions:
      - CreateNamespace=true
      - PruneLast=true
    retry:
      limit: 5
      backoff: {duration: 5s, factor: 2, maxDuration: 3m}
```

```bash
kubectl apply -f apps/demo/project.yaml -f apps/demo/application.yaml
argocd app get demo-dev
argocd app diff demo-dev
argocd app sync demo-dev --prune
argocd app wait demo-dev --health --timeout 300
```

`retry` 是必须配的：首次同步常因 CRD 尚未建立、webhook 未就绪而失败，没有退避就只能人工重试。

### 4.2 ApplicationSet 做「3 环境 × 2 集群」矩阵

`matrix` 生成器把两个 list 做笛卡尔积，得到 6 个 Application：3 个环境铺到 2 个同构集群（主集群 + 灾备集群，两边都跑 dev/staging/prod）。`goTemplate: true` 后模板用 `{{.env}}` 这类变量，prod 通过条件关闭自动同步：

```yaml
apiVersion: argoproj.io/v1alpha1
kind: ApplicationSet
metadata: {name: demo, namespace: argocd}
spec:
  goTemplate: true
  goTemplateOptions: ["missingkey=error"]
  generators:
    - matrix:
        generators:
          - list:
              elements:
                - {env: dev, autoSync: "true"}
                - {env: staging, autoSync: "true"}
                - {env: prod, autoSync: "false"}
          - list:
              elements:
                - {cluster: https://kubernetes.default.svc, clusterName: primary}
                - {cluster: https://dr.example.com:6443, clusterName: dr}
  template:
    metadata:
      name: 'demo-{{.env}}-{{.clusterName}}'
      labels: {env: '{{.env}}'}
    spec:
      project: demo
      source:
        repoURL: https://git.example.com/platform/deploy.git
        targetRevision: main
        path: 'apps/demo/overlays/{{.env}}'
      destination:
        server: '{{.cluster}}'
        namespace: 'demo-{{.env}}'
      {{- if eq .autoSync "true" }}
      syncPolicy:
        automated: {prune: true, selfHeal: true}
        syncOptions: [CreateNamespace=true, PruneLast=true]
      {{- end }}
```

用 git 目录生成器可以省掉手写环境列表，新增目录即新增环境；`{{.path.basename}}` 就是目录名：

```yaml
  generators:
    - git:
        repoURL: https://git.example.com/platform/deploy.git
        revision: main
        directories:
          - path: apps/demo/overlays/*
```

`matrix` 的组合数等于各生成器元素数之积。如果第二个集群专用于生产、不该跑 dev，**不要用 matrix 硬乘**——改用两个 ApplicationSet，或用 cluster Secret 的 label 配合 `clusters` 生成器的 `selector` 限定组合。把 dev 铺到生产集群是这套模板最容易出的事故。

### 4.3 与 Helm / Kustomize 集成

Helm 的 values 常放在另一个仓库，用 `sources` 多源模式：第二个 source 只做 `ref`，供第一个 source 以 `$values` 引用。

```yaml
spec:
  project: demo
  sources:
    - repoURL: https://charts.example.com
      chart: demo
      targetRevision: 1.8.2
      helm:
        releaseName: demo
        valueFiles:
          - $values/apps/demo/values-prod.yaml
        parameters:
          - name: image.tag
            value: v1.4.0
    - repoURL: https://git.example.com/platform/deploy.git
      targetRevision: main
      ref: values
  destination: {server: https://kubernetes.default.svc, namespace: demo-prod}
```

`helm.parameters` 的优先级高于 `valueFiles`，适合由 CI 只改 tag；`helm.releaseName` 必须显式指定，否则不同 Application 可能算出同一个 release 名而互相覆盖。`helm.skipCrds` 要慎用：CRD 不随 chart 升级时，控制器与 CRD 版本错配会出现难以定位的字段丢失。

Kustomize 侧不需要改 base，用 override 字段换镜像与命名：

```yaml
  source:
    repoURL: https://git.example.com/platform/deploy.git
    targetRevision: main
    path: apps/demo/base
    kustomize:
      namePrefix: prod-
      nameSuffix: -v2
      images:
        - ghcr.io/acme/demo:v1.4.0     # 等价于 kustomize edit set image
```

`kustomize.images` 的元素等价于 `kustomize edit set image` 的参数：`demo:v1.4.0` 只改 tag，`demo=ghcr.io/acme/demo:v1.4.0` 连仓库地址一起改。**不要同时用 Kustomize 的 `images` 和 Helm 的 `image.tag`** 去管同一个 Deployment，diff 会在两者之间来回跳。

### 4.4 漂移检测与告警

三种视角互补：`argocd app diff` 看渲染差异，`kubectl diff -f` 看服务端 apply 预演，`OutOfSync` 通知看持续状态。

```bash
argocd app list -o wide                       # SYNC / HEALTH 两列
argocd app diff demo-prod                     # 服务端渲染结果 vs live
kubectl diff -k apps/demo/overlays/prod       # 本地 kustomize build 的服务端 dry-run
```

通知由 `argocd-notifications-cm` 定义 service / template / trigger，Application 用注解订阅：

```yaml
apiVersion: v1
kind: ConfigMap
metadata: {name: argocd-notifications-cm, namespace: argocd}
data:
  service.webhook.alert: |
    url: https://alert.internal/hooks/argocd
    headers:
      - {name: Content-Type, value: application/json}
  template.app-sync-status: |
    webhook:
      alert:
        method: POST
        body: |
          {"app":"{{.app.metadata.name}}","sync":"{{.app.status.sync.status}}","health":"{{.app.status.health.status}}"}
  trigger.on-out-of-sync: |
    - when: app.status.sync.status == 'OutOfSync'
      send: [app-sync-status]
```

```yaml
# Application 上订阅
metadata:
  annotations:
    notifications.argoproj.io/subscribe.on-out-of-sync.alert: ""
```

真正的漂移治理不是「通知更多」，而是**关闭人工写集群的通道**：把 `kubectl edit` 从 runbook 里删掉，只留 `git revert` 与 PR。`selfHeal` 只是兜底，不是权限模型。

### 4.5 回滚与灾难恢复

| 方式 | 操作 | 结果 | 适用 |
|---|---|---|---|
| Git revert | 回退提交 → 自动/手动 sync | 产生新的 revision，Git 与集群一致 | **默认选择** |
| `argocd app rollback` | 回滚到历史 revision | 集群回到旧版本，但 Git HEAD 仍是新的 | 紧急止血、CI 不可用时 |
| `kubectl rollout undo` | 绕过 ArgoCD 改 Deployment | 下一次 selfHeal 会被改回，且无审计 | 不推荐 |

```bash
argocd app history demo-prod            # 列出已同步的 revision
argocd app rollback demo-prod 12 --prune
argocd app get demo-prod                # 确认 sync/health 与当前 revision
```

推荐 Git revert 的核心原因是**状态一致性**：`argocd app rollback` 只改集群，Git 仍指向坏版本，一旦开启 `selfHeal` 或下一次自动同步，坏版本会立刻回来，形成「回滚成功 → 几分钟后又坏了」的循环。只有 CI 挂了、必须立刻恢复时才用 rollback，并把它当作临时措施，随后补一个 revert commit。

灾难恢复分两层：

- **控制面**：`argocd admin export > argocd-backup.yaml` 与 `argocd admin import - < argocd-backup.yaml` 可以搬运 Application、AppProject、仓库与集群 Secret；更稳的做法是用 Velero 备份整个 `argocd` namespace（含 etcd 对象）。
- **数据面**：ArgoCD 不备份业务数据。PVC、数据库、Sealed Secrets 私钥、SOPS 的 KMS key 必须独立备份；**私钥丢了，Git 里的密文就是不可恢复的二进制**。

重建顺序是：装 ArgoCD → 恢复 repo/cluster 凭据与私钥 → 恢复 AppProject 与 Application → 让它们自己 sync。Application 只是指针，真正的恢复能力来自 Git 与密钥。

---

## 5. 版本与生态

| 阶段 | 关键变化与迁移注意点 |
|---|---|
| 2.x | 引入 ApplicationSet（独立控制器）、多集群、SSO/RBAC、Sync Waves/Hooks、Lua health check；`argocd app rollback` 与通知体系成型 |
| **3.0** | ApplicationSet 合并进主仓库并默认随 `install.yaml` 安装；**默认实例跟踪标签由 `app.kubernetes.io/instance` 改为 `argocd.argoproj.io/instance`**；清理一批废弃配置与 CLI 参数 |
| 3.x 后续 | Server-Side Apply 同步路径完善、diff 归一化与 UI 改进，Helm/Kustomize/K8s 支持版本随 minor 上移 |

**升级注意**：

- 3.0 的 tracking label 变更会让已有资源被判定为「不属于当前 Application」，表现为重复创建或 `OutOfSync` 抖动。升级前确认 `argocd-cm` 的 `application.instanceLabelKey`，要么显式保留旧值，要么先迁移资源标签。
- 从 2.x 升 3.x 先读官方 upgrade notes，逐项核对 `argocd-cm` / `argocd-rbac-cm` 中已废弃的 key；控制器与 repo-server 必须同版本，混跑会出现渲染结果不一致。
- ApplicationSet 从独立部署变为内置后，旧的 `applicationset-controller` Deployment 与 RBAC 需要清理，否则两个控制器会争抢同一批 Application。

生态边界要分清，这四个项目经常被混为一谈：

- **Argo CD**：GitOps 持续交付，负责「集群里应该有什么」。它不构建镜像、不跑测试、不做流量切分。
- **Argo Workflows**：Kubernetes 原生 DAG 工作流引擎，做 CI、批处理、数据管道。它的「workflow」是任务编排，不是部署状态收敛。
- **Argo Rollouts**：用 `Rollout` CRD 替代 Deployment，做金丝雀/蓝绿与基于指标的自动分析。ArgoCD 可以管理 `Rollout` 对象并读取其健康状态，但**发布策略由 Rollouts 执行，不是 ArgoCD 的 sync 策略**。
- **Argo Events**：事件源到触发器的路由，常用于「对象存储有新文件 → 触发 Workflow」。

此外 **argocd-image-updater** 是独立组件，负责把镜像仓库的新 tag 写回 Git 或参数覆盖。它解决的是「谁改 Git 里的 tag」，不要指望 ArgoCD 自己监控镜像仓库。

---

## 6. 常见坑与最佳实践

1. **`prune` 误删 CRD 与 namespace**：`prune: true` 下把 namespace 从 Git 删掉，会连带删除其中所有对象；CRD 被删则所有 CR 一起消失。namespace 删除还会卡在 `Terminating`——因为 finalizer 依赖的 CRD/控制器已经没了。保护手段：关键对象加 `argocd.argoproj.io/sync-options: Prune=false`，namespace 与 CRD 放独立的 Application/Project，并开启 `PruneLast`。
2. **Application 永远 `Progressing`**：90% 是 CRD 没有 health check（见 3.5），其余是 Job 没有完成、PVC 一直 Pending、Deployment 卡在 `progressDeadlineSeconds`。先 `argocd app get <app>` 逐层展开资源树定位具体对象，再补 Lua 或修探针。
3. **`selfHeal` 与 HPA 互相打架**：Git 里写 `replicas: 2`，HPA 扩到 10，selfHeal 每 3 分钟改回 2，HPA 再扩——副本数锯齿、指标失真。正确做法是从 Git 移除 `replicas`（只保留 HPA 的 `minReplicas`），或在 `ignoreDifferences` 里忽略 `/spec/replicas`，并确保 `RespectIgnoreDifferences=true`。
4. **多集群 cluster Secret 权限过大**：用 `cluster-admin` 的 token 注册集群，等于把 ArgoCD 的任一 Project 配置错误放大成全集群写权限。为每个集群建专用 ServiceAccount + 受限 ClusterRole，能用 namespace 级 Role 就不用 ClusterRole；AppProject 的 `destinations` 不要写 `*`。
5. **Helm chart 生成随机值导致永久 `OutOfSync`**：密码、`randAlphaNum`、`now` 时间戳每次渲染都不同，diff 永不收敛。把随机值移出 chart 改用 External Secrets，或对相应字段配 `ignoreDifferences`（jsonPointers 精确到字段，别整对象忽略）。
6. **服务端 apply 的注解超限**：对象较大时 `kubectl apply` 报 `metadata.annotations: Too long`（`last-applied-configuration` 上限 262144 字节）。此时必须开 `syncOptions: [ServerSideApply=true]`，让 ArgoCD 走 SSA 的字段所有权模型；共享对象同时要评估 `ForceOwnership` 的冲突风险。
7. **`selfHeal` 掩盖了真实故障**：控制器不断把被改坏的资源改回去，表面上「一直很健康」，实际是有人在手工改、或有另一个 operator 在争字段。开 `selfHeal` 的同时必须监控 `OutOfSync` 次数，否则等于把告警静音。
8. **用 Application 管理集群级引导资源**：CRD、webhook、CNI、metrics-server 这类「ArgoCD 依赖它才能工作」的资源，一旦被 prune 会连控制器一起挂掉。拆到独立的 bootstrap Application，并禁止自动 prune。

---

## 7. 选型对比

| 维度 | Argo CD | Flux CD | Jenkins / GitLab CI 部署阶段 |
|---|---|---|---|
| 模型 | 集中式控制面拉 Git，推送对象到各集群 | 每集群常驻 agent，各自拉 Git 收敛 | 流水线推送（push），CI 持有集群凭据 |
| 部署方式 | controller + repo-server + server/UI | 一组 controller（source/kustomize/helm/notification） | 在 CI runner 里执行 `kubectl` / `helm` |
| UI 与可视化 | **强**，资源树、diff、回滚、日志 | 弱（靠 CLI 与 Grafana） | 取决于 CI 平台，无资源拓扑 |
| 多集群 | 一个控制面管多集群，需注册 cluster Secret | 每集群 bootstrap，天然去中心 | 每个环境一套凭据与 job |
| 多租户 | AppProject + RBAC，成熟 | 依赖 K8s RBAC 与 namespace 隔离 | 靠 CI 项目权限，粒度粗 |
| 漂移检测 | 内置 diff + `OutOfSync` 通知 | 内置 reconciliation，默认强制收敛 | 无（除非另接工具） |
| 回滚 | 历史 revision + Git revert | Git revert / 重新 apply | 重跑旧流水线 |
| 学习曲线 | 中（概念多：Project/ApplicationSet/Hook） | 中高（CRD 多、调试靠日志） | 低（会写脚本即可） |
| 适用 | 平台团队统一交付、需要审计与 UI | 强 SRE 团队、追求去中心与 composability | 非 K8s 部署、简单脚本化发布 |

一句话决策：

- **K8s 原生 GitOps、要可视化与多租户** → ArgoCD。Application/AppProject/ApplicationSet 三件套覆盖了 90% 的交付场景，UI 与 diff 是排障效率的关键差异。
- **多集群、强隔离、团队偏好 CLI 与组合式设计** → Flux。它没有中心控制面，单集群故障不影响其他集群，但需要自己搭可观测与通知。
- **非 K8s 场景（虚拟机、裸机、传统中间件）** → 别硬套 GitOps 控制器。Jenkins / GitLab CI 的部署阶段仍然合理，GitOps 的前提是「有声明式 API 可以收敛」。
- **CI 与 CD 的边界** → 构建、测试、镜像扫描留在 CI；部署与漂移收敛交给 ArgoCD。两者通过「CI 改 Git 里的 image tag」交接，而不是通过 kubeconfig 交接。

> 落地顺序：先把 Git 目录结构定好（base + overlays，环境差异只放 values）→ 建 AppProject 划边界 → 用 ApplicationSet 批量生成 → 开自动同步与通知 → 最后才引入 Sync Waves、Hooks 与密钥方案。**目录结构错了，后面每个环境都在复制粘贴。**
