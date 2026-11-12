# Docker 与 Kubernetes 技术介绍与使用指南（Go 服务部署视角）

> 容器与编排的事实标准：镜像分层与多阶段构建、Pod / Deployment / Service 抽象、探针与滚动发布、优雅下线——从 Go 服务视角讲清「怎么被正确打包、正确调度、正确停止」。
> 上游：<https://github.com/moby/moby> · <https://github.com/kubernetes/kubernetes>　|　主线：**Docker Engine 27+ / Kubernetes 1.3x**（整理于 2026-11）　|　Go 客户端：[kubernetes/client-go](https://github.com/kubernetes/client-go) v0.3x
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

```
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

```
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

```
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

```
kubectl rollout / 缩容
  → Pod 标记 Terminating，从 Endpoints 摘除（异步！）
  → 执行 preStop hook
  → 发送 SIGTERM 给 PID 1
  → Go 进程收到信号：readiness 置 false → 停止接受新连接 → 等在途请求完成
  → terminationGracePeriodSeconds（默认 30s）内未退出 → SIGKILL
```

**摘流量与 SIGTERM 是异步的**，旧 Pod 可能仍收到新请求。所以 `preStop: exec: sleep 5` 给 Endpoints 收敛留时间，是生产清单的标配。

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
var ready atomic.Bool
ready.Store(true)

mux.HandleFunc("/readyz", func(w http.ResponseWriter, _ *http.Request) {
    if !ready.Load() {
        http.Error(w, "shutting down", http.StatusServiceUnavailable)
        return
    }
    w.WriteHeader(http.StatusOK)
})

// SIGTERM 到达后先摘流量，再等一小段时间让 Endpoints 收敛，最后关服务
<-sigCtx.Done()
ready.Store(false)
time.Sleep(5 * time.Second)
_ = srv.Shutdown(shutdownCtx)
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

---

## 5. 版本演进速览

| 阶段 | 关键变化与迁移注意点 |
|---|---|
| Docker 早期 | Swarm 编排、经典 builder；`docker-compose` 独立命令 |
| Docker 近年 | **BuildKit 成为默认构建器**（`RUN --mount=type=cache` 等语法）、Compose V2 并入 `docker compose`、containerd 从 Docker 中拆出成为 CRI 运行时、镜像清单支持多架构 |
| K8s 1.20~1.24 | **1.24 移除 dockershim**：节点运行时改为 containerd / CRI-O，Docker 镜像仍可用（镜像格式是 OCI 标准），但 kubelet 不再直接调 Docker |
| K8s 1.2x~1.3x | HPA `autoscaling/v2` GA、Ingress API 冻结并由 **Gateway API** 接棒、PodDisruptionBudget / StatefulSet 增强、原地调整 Pod 资源（in-place resize）逐步可用、Sidecar 容器原生支持 |

**迁移注意**：

- `extensions/v1beta1`、`apps/v1beta1` 等旧 API 已移除，老清单必须改 `apiVersion`（`kubectl convert` / `kubectl api-resources` 辅助）
- `client-go` 版本与集群 minor 对齐（如集群 1.3x 用 `v0.3x`），跨太多版本会有字段不兼容
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
