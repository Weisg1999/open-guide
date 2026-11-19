# Docker 与 Kubernetes 技术介绍与使用指南（Go 服务部署视角）

> 容器与编排的事实标准：镜像分层与多阶段构建、Pod / Deployment / Service 抽象、探针与滚动发布、优雅下线——从 Go 服务视角讲清「怎么被正确打包、正确调度、正确停止」。
> 上游：<https://github.com/moby/moby> · <https://github.com/kubernetes/kubernetes>　|　主线：**Docker Engine 29+ / Kubernetes 1.37**（整理于 2026-11）　|　Go 客户端：[kubernetes/client-go](https://github.com/kubernetes/client-go) v0.37
> 协议：Apache-2.0；client-go 版本须与集群 minor 版本匹配，具体以上游最新 release 为准。

---

## 1. 项目简介

这篇在仓库里位置特殊：其余篇目讲的是「Go 代码里 import 什么库」，容器与编排讲的是「Go 进程运行在哪里、被谁管理」。它仍是后端工程师的必备知识——线上 80% 的「服务莫名重启」「发布期间 502」「容器被 OOMKilled」都出在这一层，而不是业务代码。

- **Docker**：把应用与依赖打包成**镜像**，运行时用 namespace 隔离 + cgroups 限流，得到一致的「进程」
- **Kubernetes**：**声明式编排**——你描述期望状态（3 个副本、镜像版本、资源与探针），控制器持续把实际状态收敛过去

- **适用场景**：多服务部署、弹性扩缩、滚动发布与回滚、统一资源与配置管理、多环境一致性
- **不适合**：单进程小工具/CLI（systemd + 二进制就够）、低流量单体且团队没有运维能力（先用 Compose 或托管容器服务）、有状态数据库盲目上 K8s（需 Operator + 周全的存储方案）

> 与 [Gin 篇](../gin/gin-guide.md) 的 `http.Server.Shutdown`、[Zap 篇](../zap/zap-guide.md) 的 stdout 日志、[Prometheus 篇](../prometheus/prometheus-guide.md) 的服务发现、[gRPC 篇](../grpc/grpc-guide.md) 的 `GracefulStop` 都直接相关——本文给出它们在容器里的落地方式。

### 安装与本地集群

```bash
# Docker Desktop / Engine + kubectl + kind（本地 K8s）
go install sigs.k8s.io/kind@latest
kind create cluster --name dev
kubectl cluster-info --context kind-dev
```

### 最小可运行示例

一个能正确响应 SIGTERM 的 Go 服务：

```go
package main

import (
    "context"
    "errors"
    "net/http"
    "os"
    "os/signal"
    "syscall"
    "time"
)

func main() {
    mux := http.NewServeMux()
    mux.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) {
        w.WriteHeader(http.StatusOK)
        _, _ = w.Write([]byte("ok"))
    })
    srv := &http.Server{Addr: ":8080", Handler: mux,
        ReadHeaderTimeout: 5 * time.Second}

    go func() { _ = srv.ListenAndServe() }()

    ctx, stop := signal.NotifyContext(context.Background(),
        syscall.SIGTERM, syscall.SIGINT)
    defer stop()
    <-ctx.Done() // 收到 SIGTERM 才开始收尾

    shutdownCtx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
    defer cancel()
    if err := srv.Shutdown(shutdownCtx); err != nil && !errors.Is(err, http.ErrServerClosed) {
        os.Exit(1)
    }
}
```

```dockerfile
FROM golang:1.25-alpine AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /out/app ./cmd/app

FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /out/app /app
USER nonroot:nonroot
EXPOSE 8080
ENTRYPOINT ["/app"]
```

```bash
docker build -t app:v1 .
docker run --rm -p 8080:8080 app:v1
curl localhost:8080/healthz
```

---

## 2. 功能特性总览

| 层 | 能力 | 说明 |
|---|---|---|
| Docker | 分层镜像 | UnionFS + 写时复制；指令顺序决定构建缓存命中率 |
| Docker | 多阶段构建 | 编译环境与运行环境分离，`distroless` / `scratch` 可把镜像压到 10~20MB |
| Docker | 隔离与限制 | namespace（PID/NET/MNT/USER…）+ cgroups（CPU/内存/IO） |
| Docker | 网络与存储 | bridge / host 网络、Volume 与 bind mount、Compose 编排本地多服务 |
| K8s | 工作负载 | Pod（最小调度单元）、Deployment（无状态）、StatefulSet（有状态）、DaemonSet、Job / CronJob |
| K8s | 服务暴露 | Service（ClusterIP / NodePort / LoadBalancer）、Ingress / Gateway API、DNS |
| K8s | 配置 | ConfigMap、Secret、Downward API、环境变量与卷挂载 |
| K8s | 自愈 | liveness / readiness / startup 探针、重启策略、节点驱逐与重调度 |
| K8s | 发布 | 滚动更新（maxSurge / maxUnavailable）、回滚、PDB、HPA / VPA |
| K8s | 权限与配额 | RBAC、ServiceAccount、ResourceQuota、LimitRange、NetworkPolicy |
| Go 生态 | client-go | 在程序里 List / Watch / 创建资源；Operator / 控制器开发基础 |

---

## 3. 核心概念与原理

### 3.1 镜像分层与容器本质

```text
镜像（只读层）
  ├─ base 层（distroless / alpine）
  ├─ COPY go.mod → 依赖层（缓存关键：先 COPY 依赖文件再下载）
  ├─ COPY 源码 → 编译层
  └─ 最终二进制层
容器 = 镜像层 + 一个可写层（Container Layer）+ namespace + cgroups
```

- **容器不是虚拟机**：它和宿主机共享内核，只是一个被隔离、被限流的进程；所以 `scratch` 镜像里没有 shell、没有时区文件、没有 CA 证书，需要显式拷入
- 每条 Dockerfile 指令生成一层，**把变化频繁的指令放最后**才能用好缓存
- `CGO_ENABLED=0` 生成静态二进制，是能跑在 `scratch` / `distroless` 里的前提

### 3.2 Pod 与声明式协调循环

```text
用户提交 Deployment (期望: replicas=3, image=v2)
   │
   ▼
kube-apiserver 写入 etcd（集群唯一状态源）
   │
   ▼
Controller Manager 的 Deployment 控制器 ──► 创建/更新 ReplicaSet
   │                                          │
   │  持续比对「期望 vs 实际」= Reconcile Loop  ▼
   │                                      ReplicaSet 控制器创建 Pod
   ▼
Scheduler 按资源/亲和性把 Pod 绑到 Node ──► kubelet 调 CRI 起容器
```

**Pod 是最小调度单元**：同一 Pod 内容器共享网络命名空间（localhost 互通）与 Volume，由 `pause` 容器持有网络。这也解释了 sidecar（日志采集、服务网格代理）为什么能工作。

声明式的含义是：你**不直接操作 Pod**，改的是期望状态；控制器负责收敛，包括 Pod 挂了自动重建。

### 3.3 Service 与流量路径

```text
Client ──► Service (稳定 ClusterIP + DNS: svc.namespace.svc.cluster.local)
              │ kube-proxy 维护的转发规则（iptables / IPVS / eBPF）
              ▼
        Endpoints / EndpointSlice（就绪 Pod 的 IP 列表）
              ▼
             Pod
```

**readiness 探针失败的 Pod 会从 Endpoints 摘除**——这是发布期间不把流量打到新/旧实例的关键机制。Ingress / Gateway API 则负责集群外 HTTP 路由（域名、路径、TLS 终止）。

### 3.4 资源、QoS 与 OOMKilled

```yaml
resources:
  requests: {cpu: "100m", memory: "128Mi"}   # 调度依据：节点必须有这么多可分配量
  limits:   {memory: "256Mi"}                # 上限：超限触发 OOMKilled（不是 GC）
```

| QoS | 条件 | 节点资源紧张时被驱逐的优先级 |
|---|---|---|
| Guaranteed | requests == limits（全部设且相等） | 最后 |
| Burstable | 设了 requests 但不等 | 中间 |
| BestEffort | 都没设 | **最先** |

> Go 服务尤其要注意：容器内存上限由 cgroups 决定，Go runtime 默认按**宿主机**内存判断 GC 目标，容易撞到 limit 被 OOMKilled。用 `GOMEMLIMIT`（Go 1.19+）设为略低于 memory limit，或设 `GOGC` 调优。

### 3.5 三种探针的区别

| 探针 | 失败后果 | 该检查什么 |
|---|---|---|
| `startupProbe` | 阻止另外两个探针启动 | 慢启动服务给足 `failureThreshold × periodSeconds` |
| `readinessProbe` | **从 Service 摘流量**（不重启） | 本进程是否可服务；可包含必要依赖 |
| `livenessProbe` | **重启容器** | 只检查本进程是否卡死，**绝不要检查 DB / 下游** |

liveness 依赖外部服务是经典事故：数据库一抖，所有 Pod 同时重启，故障被放大成雪崩。

### 3.6 优雅下线的时序

```text
kubectl rollout / 缩容
  → Pod 标记 Terminating，从 Endpoints 摘除（异步！）
  → 执行 preStop hook
  → 发送 SIGTERM 给 PID 1
  → Go 进程收到信号：readiness 置 false → 停止接受新连接 → 等在途请求完成
  → terminationGracePeriodSeconds（默认 30s）内未退出 → SIGKILL
```

**摘流量与 SIGTERM 是异步的**，旧 Pod 可能仍收到新请求。所以 `preStop: exec: sleep 5` 给 Endpoints 收敛留时间，是生产清单的标配。

### 3.7 调度：为什么 Pod 一直 Pending

`Pending` 的含义很明确：**调度器没能给这个 Pod 找到节点，容器根本没被创建**。它和 `CrashLoopBackOff`（容器起来了又挂）是两类完全不同的问题，排查方向不要混。

```text
kube-scheduler
  ├─ Filter（过滤）：排除不满足硬性条件的节点
  │    资源余量、nodeSelector / 亲和性、污点、PVC 可用区、端口冲突
  └─ Score（打分）：在剩余节点里挑最优
       ImageLocality（镜像已在节点上）、LeastAllocated（最空闲）、
       topologySpread（打散是否均匀）
```

**`requests` 决定调度，`limits` 决定运行时**——这条区别必须刻在脑子里：

| 字段 | 生效时机 | 后果 |
|---|---|---|
| `requests.cpu` / `requests.memory` | **调度时** | 节点可分配量必须 ≥ 该节点所有 Pod 的 requests 之和，决定 Pod 落到哪 |
| `limits.cpu` | 运行时 | cgroup 配额，超了被**节流**（throttling），不会 OOM |
| `limits.memory` | 运行时 | 超了内核 OOMKill，容器 `terminated.reason: OOMKilled` |

由此可得两个常见误判：节点 `kubectl top` 显示 CPU 只用了 20%，Pod 却 Pending——因为 requests 已经把节点「账面」占满（资源碎片化）；反过来 requests 写得太小，节点严重超卖，几个 Pod 挤在一起互相抢 CPU，P99 抖得莫名其妙。

三种「定向调度」手段的适用场景：

| 手段 | 表达什么 | 典型用途 |
|---|---|---|
| `nodeSelector` / `nodeAffinity` | 我要去这类节点 | GPU 节点、本地 SSD 节点、专用节点池 |
| `podAntiAffinity` | 我不要和谁待在一起 | 同一服务的多个副本打散到不同节点 |
| `taints` + `tolerations` | 节点拒绝谁（有容忍才准进） | 节点池隔离、标记有问题的节点 |

> 方向别搞反：**污点是节点上的「禁止入内」，容忍是 Pod 上的「通行证」**。给节点打上污点却没给工作负载加容忍，那台机器就再也调度不上新 Pod——很多「扩容不生效」都是这么来的。

跨可用区打散用 `topologySpreadConstraints`，比 `podAntiAffinity` 更精细（它管的是「差值」而不是「绝对不同」）：

```yaml
spec:
  topologySpreadConstraints:
    - maxSkew: 1                        # 各可用区副本数最大差值
      topologyKey: topology.kubernetes.io/zone
      whenUnsatisfiable: DoNotSchedule  # 硬约束；ScheduleAnyway 为软约束（尽量打散）
      labelSelector:
        matchLabels: {app: app}
  affinity:
    podAntiAffinity:
      preferredDuringSchedulingIgnoredDuringExecution:   # 用 preferred，避免副本数 > 节点数时死锁
        - weight: 100
          podAffinityTerm:
            topologyKey: kubernetes.io/hostname
            labelSelector:
              matchLabels: {app: app}
```

Pending 的排查顺序（按出现频率，逐条排除）：

```bash
kubectl describe pod <pod> | sed -n '/Events/,$p'   # 先看事件，多数答案在这里
```

1. **资源不足**：`0/6 nodes are available: 6 Insufficient cpu`。降 requests、扩节点池，或让 Cluster Autoscaler 加机器。注意大规格 Pod（如 8C16G）在小节点池里可能**永远**放不下。
2. **亲和 / 反亲和冲突**：`node(s) didn't match Pod's node affinity` 或 `didn't satisfy existing pods anti-affinity rules`。用了 `requiredDuringScheduling` 的反亲和且副本数大于节点数时，多出来的副本会永久 Pending。
3. **污点未容忍**：`node(s) had untolerated taint {dedicated: gpu}`。补 toleration，或确认这个负载是否真该进那个池。
4. **PVC 未绑定**：`pod has unbound immediate PersistentVolumeClaims`。查 `kubectl get pvc`：StorageClass 不存在、容量不足，或 `WaitForFirstConsumer` 模式下必须等 Pod 先调度才会创建卷。
5. **镜像拉取失败**：此时通常已是 `ContainerCreating` / `ImagePullBackOff` 而非 Pending——`Failed to pull image`、`unauthorized`，查镜像地址、`imagePullSecrets`、私有仓库证书。
6. **配额与准入**：`exceeded quota: compute-resources`。ResourceQuota 打满，清理闲置负载或提配额；LimitRange 的默认值过大也会让 Pod 创建直接被拒。

`describe` 的 Events 只需看最后 5~10 行（按时间正序排列，末尾最新），重点认三类关键字：`Insufficient`（资源）、`didn't match`（亲和）、`untolerated taint`（污点）。

### 3.8 配置与密钥：ConfigMap / Secret 的更新语义

「改了 ConfigMap 怎么没生效」是配置类问题里最高频的一个。答案完全取决于**注入方式**：

| 注入方式 | 更新后是否生效 | 原因 / 延迟 |
|---|---|---|
| `env` / `envFrom` | **不会** | 进程启动时值已固化进环境变量，必须重启容器 |
| `volumeMounts`（挂整个 ConfigMap） | 会 | kubelet 同步周期 + 缓存 TTL，通常 30s~2min |
| `volumeMounts` + `subPath` | **不会** | 已知限制：kubelet 不更新 subPath 挂载的文件 |
| `immutable: true` 的 ConfigMap | 不能改 | 只能新建一个再切换引用 |

`subPath` 是最常见的坑：为了「只挂一个文件、不覆盖目录里已有内容」而用了 `subPath`，结果配置永远停在启动那一刻的版本。需要热更新的配置**不要用 subPath**。

```yaml
spec:
  containers:
    - name: app
      volumeMounts:
        - name: config
          mountPath: /etc/app            # 挂整个目录 → 可以热更新
          readOnly: true
        # - mountPath: /etc/app/config.yaml
        #   subPath: config.yaml         # 挂单个文件 → 永远不更新，别用于需热更新的配置
  volumes:
    - name: config
      configMap:
        name: app-config
        items: [{key: config.yaml, path: config.yaml}]
```

Secret 支持 `stringData`（写明文，apiserver 自动 base64）与 `data`（自己编码）：

```yaml
apiVersion: v1
kind: Secret
metadata: {name: app-secret}
type: Opaque
immutable: true                # 不可变：降低 apiserver watch 压力，也防误改
stringData:
  DB_PASSWORD: "s3cr3t"
```

> **Secret 只是 base64，不是加密**。能 `exec` 进 Pod、或能 `get secrets` 的人都能读到明文。真正的防护靠：etcd 静态加密（`EncryptionConfiguration`）、RBAC 收紧 `get/list/watch secrets`、按需关闭 `automountServiceAccountToken`，以及引入外部密钥管理——**External Secrets Operator** 从 Vault / 云 KMS 拉取并生成 Secret（密文不进 Git），**Sealed Secrets** 则把加密后的密文直接提交到仓库（适合 GitOps 但轮换较弱）。它们解决的是「密钥从哪来、谁能看」，不改变 K8s 里 Secret 本身的语义。

`immutable: true` 的收益容易低估：kubelet 不必为它维护 watch，大规模集群里成千上万个不会被修改的配置能省下可观的 apiserver 连接与内存。代价是升级配置要「新建 + 改引用」。

让配置变更生效的两种方式：

```bash
# 一、直接滚动重启：环境变量注入场景下唯一可行的办法
kubectl rollout restart deploy/app
```

```yaml
# 二、checksum 注解：配置内容变 → Pod 模板变 → 自动触发滚动（Helm 常用）
metadata:
  annotations:
    checksum/config: {{ include (print $.Template.BasePath "/configmap.yaml") . | sha256sum }}
```

Kustomize 的 `configMapGenerator` 默认就会追加内容哈希后缀（`disableNameSuffixHash` 可关闭）：名字变了，引用跟着变，滚动自然发生。`nameSuffix` 是自定义的固定后缀，本身不含哈希。

---

## 4. 使用实战

### 4.1 生产级 Dockerfile 要点

```dockerfile
# syntax=docker/dockerfile:1
FROM golang:1.25-alpine AS build
WORKDIR /src
COPY go.mod go.sum ./          # 先拷依赖文件，源码变动不会让下载缓存失效
RUN --mount=type=cache,target=/go/pkg/mod go mod download
COPY . .
RUN --mount=type=cache,target=/root/.cache/go-build \
    CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /out/app ./cmd/app

FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/  # 需要 HTTPS 时
COPY --from=build /out/app /app
USER nonroot:nonroot
EXPOSE 8080
ENTRYPOINT ["/app"]   # exec 形式，保证 app 是 PID 1 能直接收到 SIGTERM
```

```gitignore
# .dockerignore —— 不写会把 .git / 构建产物 / 本地密钥打进镜像
.git
.workbuddy
*.log
bin/
.env
```

> `ENTRYPOINT ["/app"]`（exec 形式）而不是 `ENTRYPOINT /app`（shell 形式）：后者 PID 1 是 `/bin/sh`，**信号不会转发给 Go 进程**，优雅关闭直接失效。

### 4.2 健康检查与优雅关闭（Go 侧）

```go
// 启动阶段：注册就绪探针，进程就绪后把 ready 置为 true
var ready atomic.Bool

func registerHealth(mux *http.ServeMux) {
    mux.HandleFunc("/readyz", func(w http.ResponseWriter, _ *http.Request) {
        if !ready.Load() {
            http.Error(w, "shutting down", http.StatusServiceUnavailable)
            return
        }
        w.WriteHeader(http.StatusOK)
    })
}

// 关闭阶段：SIGTERM 到达后先摘流量，等 Endpoints 收敛，再关服务
func gracefulShutdown(sigCtx, shutdownCtx context.Context, srv *http.Server) {
    <-sigCtx.Done()
    ready.Store(false)
    time.Sleep(5 * time.Second)
    _ = srv.Shutdown(shutdownCtx)
}
```

[gRPC 服务](../grpc/grpc-guide.md) 同理，把 `Shutdown` 换成 `GracefulStop()`，并配超时后降级 `Stop()`。

### 4.3 Deployment / Service / HPA 清单

```yaml
apiVersion: apps/v1
kind: Deployment
metadata: {name: app}
spec:
  replicas: 3
  strategy:
    type: RollingUpdate
    rollingUpdate: {maxSurge: 1, maxUnavailable: 0}  # 保证发布期容量不降
  selector:
    matchLabels: {app: app}
  template:
    metadata:
      labels: {app: app}
      annotations:
        checksum/config: "{{ .Values.configHash }}"   # Helm：配置变更触发滚动
    spec:
      terminationGracePeriodSeconds: 45
      securityContext:
        runAsNonRoot: true
        runAsUser: 65532
      containers:
        - name: app
          image: registry.example.com/app:v1.2.3   # 钉版本，别用 latest
          ports: [{containerPort: 8080}]
          env:
            - name: GOMEMLIMIT
              value: "200MiB"
          envFrom:
            - configMapRef: {name: app-config}
          resources:
            requests: {cpu: "100m", memory: "128Mi"}
            limits:   {memory: "256Mi"}
          startupProbe:
            httpGet: {path: /healthz, port: 8080}
            failureThreshold: 30
            periodSeconds: 2
          readinessProbe:
            httpGet: {path: /readyz, port: 8080}
            periodSeconds: 5
          livenessProbe:                          # 只看本进程，不查 DB
            httpGet: {path: /healthz, port: 8080}
            periodSeconds: 10
            timeoutSeconds: 2
          lifecycle:
            preStop:
              exec: {command: ["sh", "-c", "sleep 5"]}
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
---
apiVersion: v1
kind: Service
metadata: {name: app}
spec:
  selector: {app: app}
  ports: [{port: 80, targetPort: 8080}]
---
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata: {name: app}
spec:
  scaleTargetRef: {apiVersion: apps/v1, kind: Deployment, name: app}
  minReplicas: 3
  maxReplicas: 20
  metrics:
    - type: Resource
      resource: {name: cpu, target: {type: Utilization, averageUtilization: 70}}
```

> `readOnlyRootFilesystem: true` 需要应用不写本地文件；[Zap 篇](../zap/zap-guide.md) 建议的「容器里只写 stdout」正好满足这一点。若确实要写临时文件，挂 `emptyDir` 到 `/tmp`。

### 4.4 client-go：在 Go 程序里操作集群

```go
package main

import (
    "context"
    "fmt"

    metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
    "k8s.io/client-go/kubernetes"
    "k8s.io/client-go/rest"
    "k8s.io/client-go/tools/clientcmd"
)

func NewClient(kubeconfig string) (*kubernetes.Clientset, error) {
    var cfg *rest.Config
    var err error
    if kubeconfig == "" {
        cfg, err = rest.InClusterConfig() // 集群内：用 ServiceAccount token
    } else {
        cfg, err = clientcmd.BuildConfigFromFlags("", kubeconfig)
    }
    if err != nil { return nil, err }
    return kubernetes.NewForConfig(cfg)
}

func ListPods(ctx context.Context, cs *kubernetes.Clientset, ns string) error {
    pods, err := cs.CoreV1().Pods(ns).List(ctx, metav1.ListOptions{
        LabelSelector: "app=app",
    })
    if err != nil { return err }
    for _, p := range pods.Items {
        fmt.Printf("%s %s\n", p.Name, p.Status.Phase)
    }
    return nil
}
```

Watch 资源变化（控制器 / Operator 的基础）：

```go
w, err := cs.AppsV1().Deployments("default").Watch(ctx, metav1.ListOptions{})
if err != nil { return err }
defer w.Stop()
for ev := range w.ResultChan() {
    fmt.Printf("%s: %T\n", ev.Type, ev.Object)
}
```

集群内运行时需要 RBAC：

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata: {name: pod-reader}
rules:
  - apiGroups: [""]
    resources: ["pods"]
    verbs: ["get", "list", "watch"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata: {name: app-pod-reader}
subjects:
  - {kind: ServiceAccount, name: default, namespace: default}
roleRef: {kind: Role, name: pod-reader, apiGroup: rbac.authorization.k8s.io}
```

> **client-go 模块很多，只导入用到的包**；否则 `go mod tidy` 会拉入大量间接依赖。集群内用 `rest.InClusterConfig()`，本地调试才用 `BuildConfigFromFlags` 读 kubeconfig。

### 4.5 发布与排障速查

```bash
kubectl apply -f deploy.yaml
kubectl rollout status deploy/app --timeout=120s
kubectl rollout history deploy/app
kubectl rollout undo deploy/app          # 回滚上一版

kubectl get pods -l app=app -o wide
kubectl describe pod <pod>              # Events 里看调度/探针/OOMKilled
kubectl logs <pod> --previous           # 重启前的日志
kubectl top pod
kubectl exec -it <pod> -- sh            # distroless 没有 shell，改用 kubectl debug

kubectl get endpoints app               # 确认就绪 Pod 是否登记
kubectl get pdb                         # 是否有 PodDisruptionBudget
```

### 4.6 镜像：瘦身、缓存与漏洞

三个目标其实是同一件事：**构建快（缓存命中）、拉取快（体积小）、上线安全（漏洞少）**。

```dockerfile
# syntax=docker/dockerfile:1
FROM --platform=$BUILDPLATFORM golang:1.25-alpine AS build
WORKDIR /src
# 第一层：只碰依赖文件，改代码时这层缓存不失效
COPY go.mod go.sum ./
RUN --mount=type=cache,target=/go/pkg/mod go mod download
# 第二层：源码 + 编译，只有代码变动才重跑
COPY . .
ARG TARGETOS TARGETARCH
RUN --mount=type=cache,target=/go/pkg/mod \
    --mount=type=cache,target=/root/.cache/go-build \
    CGO_ENABLED=0 GOOS=$TARGETOS GOARCH=$TARGETARCH \
    go build -trimpath -ldflags="-s -w" -o /out/app ./cmd/app

FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /out/app /app
USER nonroot:nonroot
ENTRYPOINT ["/app"]
```

层次的划分逻辑是「**变动频率**」：`go.mod` 极少变 → 依赖下载层几乎永久命中；源码天天变 → 只重跑编译层（有 `--mount=type=cache` 时编译缓存也复用）。反过来写（先 `COPY . .` 再 `go mod download`）会让每次改一行代码都重新下载全部依赖。

基础镜像的取舍：

| 基础镜像 | 体积 | 有 shell | CA 证书 / 时区 | 适用 |
|---|---|---|---|---|
| `scratch` | 最小（×仅二进制） | 无 | **都没有，须手工拷入** | 纯静态 Go 二进制、极致体积 |
| `distroless/static` | ~2MB + 应用 | 无（另有 `:debug` 变体） | 有 CA、有时区（tzdata 内置） | 大多数 Go 服务的默认选择 |
| `alpine` | ~5MB | 有（`/bin/sh`） | 有 CA，`tzdata` 需装 | 需要进容器排查、跑启动脚本 |
| `debian-slim` | ~30MB | 有 | 齐全 | 依赖 glibc / CGO 的程序 |

- 没有 CA 证书 → HTTPS 调用报 `x509: certificate signed by unknown authority`；没有时区 → 日志时间全变 UTC，`time.LoadLocation("Asia/Shanghai")` 直接失败。
- `distroless:debug` 是折中方案：平时用 `static`，排查时临时切 `debug`（带 busybox）再切回。

`.dockerignore` 不只是「省体积」，它首先影响**构建上下文**：`COPY . .` 会把 `.git`（常上百 MB）、`node_modules`、本地 `.env` 一起上传给 BuildKit，CI 上这项经常占掉一半构建时间。

体积最终换算成两点：**拉取时间**与**冷启动**。节点首次起 Pod 要拉镜像——1GB 镜像跨区拉可能几十秒，HPA 扩容、节点故障转移、批量重启全被拖慢；压到 20MB 后通常 1~2s 完成。

漏洞扫描接进 CI（构建后、推送前）：

```bash
# 只报 HIGH/CRITICAL，有则退出码非 0 → 阻断流水线
trivy image --severity HIGH,CRITICAL --exit-code 1 \
  --ignore-unfixed registry.example.com/app:v1.2.3

# 产出 SBOM，事后某个 CVE 爆出来时能快速反查影响面
trivy image --format cyclonedx --output sbom.json registry.example.com/app:v1.2.3
```

> 「高危阻断」务必区分**可修复**与**上游未修**：`--ignore-unfixed` 让流水线只对「有补丁但你没升级」的漏洞报红。否则基础镜像里一堆上游尚未修复的 CVE 会长期卡住发布，团队最后一定会把门禁整个绕过——那比不扫更糟。

多架构构建：

```bash
docker buildx create --use --name multi
docker buildx build --platform linux/amd64,linux/arm64 \
  -t registry.example.com/app:v1.2.3 --push .
```

> 产出的是 manifest list：同一个 tag 下按节点架构自动选层。Apple Silicon 本地开发、amd64 上生产时，漏了 `--platform` 会得到一个跑不起来的镜像（`exec format error`）。瘦身的另一面是**调试能力**：镜像越小，容器里能用的工具越少，这正是 4.8 节 `kubectl debug` 存在的原因。

### 4.7 发布：maxSurge / maxUnavailable 与 PDB

```yaml
spec:
  replicas: 4
  minReadySeconds: 10          # 就绪后再观察 10s 才算可用，挡掉「起来就崩」的假就绪
  progressDeadlineSeconds: 600 # 600s 无进展 → ProgressDeadlineExceeded
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxSurge: 1              # 允许超出 replicas 的数量（可为数量或百分比，向上取整）
      maxUnavailable: 0        # 滚动期间可用副本不得低于 replicas
```

以 `replicas: 4` 为例，两个参数的组合效果：

| maxSurge | maxUnavailable | 滚动期 Pod 总数 | 可用副本下限 | 特点 |
|---|---|---|---|---|
| 1 | 0 | 4~5 | 4 | **不降容**，最稳，峰值多占 25% 资源 |
| 0 | 1 | 3~4 | 3 | 不超卖，但发布期容量下降 |
| 1 | 1 | 3~5 | 3 | 快，容量波动最大 |
| 25% | 25%（默认） | 3~5 | 3 | 通用默认值 |
| 0 | 0 | — | — | **非法**，两者不能同时为 0 |

> `maxUnavailable: 0` 是「发布不 502」的前提之一，但它要求集群有富余资源容纳 surge 出来的 Pod。节点资源吃紧时新 Pod 卡在 Pending，发布就永远走不完——这是「发布卡住」最常见的非代码原因。

「就绪」由 readiness 探针判定，`minReadySeconds` 在其后再加一道时间闸：探针通过、但容器随即因配置错误 panic 的场景能被它拦住，避免流量切过去又切回来。

**PDB 保护的是「自愿中断」，而且只管 Eviction API 那条路径**：`kubectl drain`（节点维护、集群升级）与 Cluster Autoscaler 缩容会先查 PDB 再动手；Deployment 的滚动更新**不看 PDB**，它由 `maxSurge` / `maxUnavailable` 控制。节点宕机这类非自愿中断，PDB 也管不了。

```yaml
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata: {name: app}
spec:
  minAvailable: 3        # 或 maxUnavailable: 1，二选一，不要都写
  selector:
    matchLabels: {app: app}
```

- `minAvailable` 表达「至少留几个」，适合容量不能降的服务；`maxUnavailable` 表达「最多停几个」，适合副本多、可按比例容忍的服务。
- `minAvailable` 设为等于（或 `100%`）replicas 会让 `kubectl drain` **永久卡住**：没有可驱逐的余量。单副本服务配 PDB 等于禁止一切节点维护。
- `drain` 被挡住时的信号：`Cannot evict pod as it would violate the pod's disruption budget`，同时 `kubectl get pdb` 的 `ALLOWED DISRUPTIONS` 为 0。

```bash
kubectl get pdb                                  # ALLOWED DISRUPTIONS=0 就先扩容，再 drain
kubectl drain <node> --ignore-daemonsets --delete-emptydir-data
kubectl rollout status deploy/app --timeout=300s # CI 里返回非 0 即判失败
kubectl rollout undo deploy/app                  # 异常时的第一反应
kubectl rollout history deploy/app --revision=3
```

`progressDeadlineSeconds` 只是**标记失败**，不会自动回滚：需要 CI 里 `rollout status` 返回非 0 后触发 `rollout undo`，或交给 Argo Rollouts / Flagger 做指标分析式自动回滚。

发布失败的判定信号，按发现顺序：`rollout status` 超时 → 新 Pod `CrashLoopBackOff` / `ImagePullBackOff` → `kubectl get rs` 里新旧 ReplicaSet 长时间共存且新 RS 的 `READY` 不涨 → 切流后 5xx 率与 P99 抬升（说明新版本「就绪但功能坏」，探针查不出来，只能靠指标与金丝雀）。

### 4.8 排障：kubectl debug 与临时容器

`kubectl exec -it <pod> -- sh` 在 distroless 上直接失败（里面没有 shell）。三种进入方式，用途完全不同：

```bash
# 1) 临时容器：注入现有 Pod，共享网络栈；--target 还能共享目标容器的进程命名空间
kubectl debug -it <pod> --image=nicolaka/netshoot --target=app -- bash

# 2) 复制 Pod：原 Pod 不动，起一个改了镜像/命令的副本（排查启动即退出）
kubectl debug <pod> -it --copy-to=app-debug --container=app \
  --image=golang:1.25 -- sh

# 3) 节点调试：起一个带 host 命名空间的 Pod，宿主机根目录挂在 /host
kubectl debug node/<node> -it --image=nicolaka/netshoot
```

| 模式 | 影响原 Pod | 能看到什么 | 典型场景 |
|---|---|---|---|
| 临时容器 | 无（只加入，之后不可移除） | 目标容器进程、共享网络栈 | 无 shell 镜像里 `curl` / `ss` / `tcpdump` |
| 复制 Pod | 无 | 一个能改 entrypoint 的新 Pod | 容器启动即退出，来不及 exec |
| 节点调试 | 无 | 宿主机 `/host`、节点进程 | 节点 NotReady、Pod 网络不通、磁盘满 |

> 临时容器是**一次性**的：加进去删不掉，只能重建 Pod。别在生产长期留着，用完滚动一次清掉（`EphemeralContainers` 自 1.23 起默认可用）。

事件与指标：

```bash
kubectl events --for pod/app-xxxx          # 单对象事件流，比 describe 更好翻
kubectl events -A --types=Warning          # 全集群只看告警
kubectl get events --field-selector involvedObject.name=app-xxxx,reason=FailedMount
kubectl top pod -l app=app                 # 报 "Metrics API not available" 即未装 metrics-server
kubectl get pod app-xxxx -o jsonpath='{.status.containerStatuses[*].restartCount}'
kubectl get pod app-xxxx -o jsonpath='{.status.containerStatuses[*].lastState}'
```

`restartCount` 持续上涨 = 容器反复退出；`lastState.terminated` 是最有信息量的字段：`reason: OOMKilled`（超 memory limit）、`reason: Error` + `exitCode: 1`（应用自己退的，看 `--previous` 日志）、`reason: Completed`（正常结束——多为命令写错，进程跑完就退）。

**「Pod 是 Running，但服务就是不可用」的四步顺序**（由近及远，别跳步）：

1. **就绪探针**：`kubectl describe pod` 看 `Readiness` 是否为 `False`。readiness 失败 → 该 Pod 不在 Endpoints 里，Service 自然没有后端。
2. **Service 与 Endpoints**：`kubectl get endpoints <svc>` 是否为空。为空先查 selector 是否匹配 Pod 标签，再查 `targetPort` 与应用实际监听端口是否一致——「应用听 8080、targetPort 写 80」是出现率最高的一处配置错误。
3. **网络策略**：`kubectl get networkpolicy -A` 是否有默认拒绝。被 NetworkPolicy 拦掉时 `curl` 表现为**超时**而不是 connection refused，很容易被误判成服务没起。
4. **应用日志**：`kubectl logs <pod> --tail=200`，必要时 `--previous` 看上一个实例。走到这一步才是业务代码问题。

---

## 5. 版本演进速览

| 阶段 | 关键变化与迁移注意点 |
|---|---|
| Docker 早期 | Swarm 编排、经典 builder；`docker-compose` 独立命令 |
| Docker 近年 | **BuildKit 成为默认构建器**（`RUN --mount=type=cache` 等语法）、Compose V2 并入 `docker compose`、containerd 从 Docker 中拆出成为 CRI 运行时、镜像清单支持多架构 |
| K8s 1.20~1.24 | **1.24 移除 dockershim**：节点运行时改为 containerd / CRI-O，Docker 镜像仍可用（镜像格式是 OCI 标准），但 kubelet 不再直接调 Docker |
| K8s 1.3x~1.37 | HPA `autoscaling/v2` GA、Ingress API 冻结并由 **Gateway API** 接棒、PodDisruptionBudget / StatefulSet 增强、原地调整 Pod 资源（in-place resize）逐步可用、Sidecar 容器原生支持 |

**迁移注意**：

- `extensions/v1beta1`、`apps/v1beta1` 等旧 API 已移除，老清单必须改 `apiVersion`（`kubectl convert` / `kubectl api-resources` 辅助）
- `client-go` 版本与集群 minor 对齐（如集群 1.37 用 `v0.37`），跨太多版本会有字段不兼容
- Ingress 新项目建议直接上 Gateway API；存量 Ingress 可继续用但不再有新特性
- 从 `docker-compose` 迁 K8s 时，先确认有状态组件（DB、MQ）是否真的适合上 K8s，别为了「全栈 K8s」强行迁

---

## 6. 常见坑与最佳实践

1. **`latest` 标签**：镜像内容变了但 K8s 不会重新拉取（配合 `imagePullPolicy: Always` 才有机会），且无法回滚。钉语义化版本，生产用 digest。
2. **`ENTRYPOINT` 用 shell 形式**：PID 1 是 shell，SIGTERM 不转发 → 优雅关闭失效、每次发布都有 502。一律用 exec 数组形式。
3. **不处理 SIGTERM**：Go 进程被直接 SIGKILL，在途请求丢失。用 `signal.NotifyContext` + `Server.Shutdown` + `readiness` 置 false。
4. **缺 `preStop` / readiness 收敛时间**：Endpoints 摘除是异步的，旧 Pod 仍会收到请求。加 `sleep 5` 并把 `terminationGracePeriodSeconds` 设得比「sleep + 最长请求」更长。
5. **liveness 探针查下游**：DB 抖动 → 全体 Pod 重启 → 雪崩。liveness 只查本进程存活。
6. **没配 `startupProbe`**：启动要 30s 的服务被 liveness 反复杀掉，陷入 CrashLoopBackOff。
7. **不设 resources**：BestEffort QoS 最先被驱逐；或超 limit 被 OOMKilled。Go 服务额外设 `GOMEMLIMIT` 略低于 memory limit。
8. **单副本 + `maxUnavailable: 1`**：滚动更新期间容量归零。至少 2 副本并设 `maxUnavailable: 0`，再配 PDB 防节点驱逐。
9. **日志写文件**：容器重启日志即丢，采集器也拿不到。[Zap 篇](../zap/zap-guide.md) 明确建议容器里只输出 stdout。
10. **Secret 当加密用**：Secret 只是 base64，需启用 etcd 静态加密、RBAC 最小权限，敏感信息优先接外部密钥管理。
11. **ConfigMap 改了不生效**：挂载为环境变量的不会热更新；用卷挂载（会同步但有延迟）或 checksum 注解触发滚动。
12. **`scratch` 镜像缺 CA 证书 / 时区**：HTTPS 调用报 `x509: certificate signed by unknown authority`。从构建阶段拷 `ca-certificates.crt` 与 `zoneinfo`。
13. **Dockerfile 层顺序拖慢构建**：先 `COPY . .` 再 `go mod download`，源码一改就重下依赖。先拷 `go.mod`/`go.sum`。
14. **镜像里带 shell / 包管理器 / 源码**：攻击面与体积都变大。多阶段构建 + distroless + 非 root + `readOnlyRootFilesystem`。
15. **容器里跑多进程**：PID 1 不会转发信号、子进程变僵尸。一个容器一个主进程，或引入 `tini` 作 init。
16. **用 Deployment 跑有状态服务**：Pod 名与存储不稳定。用 StatefulSet + PVC，或干脆用托管数据库。
17. **连接串写死 IP**：Pod IP 会变，一律用 Service DNS 名；并注意 [gRPC](../grpc/grpc-guide.md) 的客户端负载均衡需要配 headless Service 或 resolver。
18. **client-go 权限过大**：直接用 `cluster-admin` 的 ServiceAccount。按 `verbs` 最小授权，并注意 Watch 需要 `watch` 权限。

---

## 7. 选型对比

| 维度 | Docker Compose | Kubernetes | Nomad | 托管容器（云厂商 Serverless 容器） | systemd 裸机 | 虚拟机 |
|---|---|---|---|---|---|---|
| 定位 | 单机多容器 | 集群编排 | 轻量调度器 | 免运维容器 | 进程管理 | 强隔离 |
| 学习成本 | **极低** | 高 | 中 | 低 | 低 | 中 |
| 高可用 / 自愈 | 无 | **强** | 中 | 由平台提供 | 无 | 依赖上层 |
| 弹性伸缩 | 手动 | HPA / VPA / Cluster Autoscaler | 有 | **自动** | 无 | 慢 |
| 声明式与生态 | 弱 | **最强**（Operator / Helm / CNCF） | 一般 | 厂商锁定 | 弱 | 弱 |
| 适用规模 | 本地开发、单机小服务 | 多服务、多环境、中大规模 | 混合工作负载 | 团队小、想免运维 | 单进程工具 | 强隔离/遗留系统 |

一句话决策：

- **本地开发、CI、单机部署几个容器** → Docker Compose，别一上来就 K8s。
- **多服务、需要自愈/滚动发布/弹性、团队有运维能力** → Kubernetes，生态与可移植性最好。
- **团队小、不想管控制面** → 托管容器服务（Cloud Run / 容器实例）或 PaaS，用成本换人力。
- **既要调度容器又要调度非容器任务、嫌 K8s 重** → Nomad，组件少但生态小。
- **只是一个 CLI / 单进程后台服务** → systemd + 静态二进制，最省事。
- **有状态数据库** → 优先托管服务；必须自建时再用 StatefulSet + Operator，[MySQL](../mysql/mysql-guide.md) / [etcd](../etcd/etcd-guide.md) 篇的运维要点同样适用。

> 落地顺序建议：先把镜像做小、做安全、把优雅关闭写对 → 再上探针与资源配额 → 然后滚动发布与 PDB → 最后才谈 HPA、Operator 和服务网格。**前面四步没做对，加自动化只会让故障更快发生。**
