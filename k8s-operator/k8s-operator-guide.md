# Kubernetes Operator 技术指南

> Operator 不是「用 Go 写 YAML」：它把运维知识编码进控制器，用声明式 API 把「期望状态」持续收敛成现实。
> 上游：<https://github.com/kubernetes-sigs/controller-runtime> v0.25 · <https://github.com/kubernetes/client-go> v0.37 · <https://github.com/kubernetes-sigs/kubebuilder> v4.16　|　对应 Kubernetes 1.37

---

## 1. 项目简介

[Docker 与 K8s 篇](../docker-k8s/docker-k8s-guide.md) 解决「进程怎么被正确打包、调度、停止」；本篇解决下一个问题：**当一件事需要「看状态 → 决定做什么 → 做完再检查」时，怎么交给集群自动做**。答案是 CRD（扩展 API）+ 控制器（协调循环），合称 Operator。基础对象（Pod / Deployment / 探针 / 优雅下线）不再重复。

- **本质**：用自定义资源描述期望（`replicas: 3`、`intervalHours: 6`、`version: 16`），控制器把它翻译成 Deployment / Service / Job / 云 API 调用，并持续纠偏
- **适用**：有状态服务（数据库、MQ、存储）、多步编排（主从切换、分片再平衡、证书轮转）、把内部平台能力（备份、灰度、发号）暴露成 K8s 原生 API
- **不适用**：无状态服务（Deployment + Helm 足够）、差异只在 values 里的配置、运维流程还没跑通（先写成人能执行的 runbook 再编码）
- **代价**：你写的是**长期运行的控制平面代码**——幂等、并发、RBAC、版本升级、故障注入都要自己扛，复杂度比一个 Helm chart 高一个数量级

```bash
# 版本锚点（截至 2026-11）：kubebuilder v4.16.0 / controller-runtime v0.25 / k8s.io/* v0.37
curl -L -o kubebuilder "https://github.com/kubernetes-sigs/kubebuilder/releases/download/v4.16.0/kubebuilder_$(go env GOOS)_$(go env GOARCH)"
chmod +x kubebuilder && sudo mv kubebuilder /usr/local/bin/
go install sigs.k8s.io/controller-runtime/tools/setup-envtest@latest   # envtest 依赖

mkdir backup-operator && cd backup-operator
kubebuilder init --domain example.com --repo example.com/backup-operator
kubebuilder create api --group backup --version v1alpha1 --kind BackupSchedule --resource --controller
make manifests generate     # controller-gen 生成 CRD 与 DeepCopy
make install run            # 装 CRD 并把控制器跑在本地，迭代最快
```

> `make install` / `make deploy` 用当前 context。先 `kubectl config current-context`——有团队因此把 CRD 装进了生产。

---

## 2. 能力总览

| 能力 | 由谁提供 | 说明 |
|---|---|---|
| 扩展 API | CRD（`apiextensions.k8s.io/v1`） | group/version/kind、OpenAPI schema、CEL 校验、printer columns |
| 类型注册 | `Scheme` + `runtime.Object` | Go struct ↔ GVK 映射；client、cache、序列化全靠它 |
| 协调框架 | controller-runtime `Manager` + `Reconciler` | 统一管理 cache、client、workqueue、webhook、metrics 生命周期 |
| 事件与排队 | informer（List-Watch cache）+ workqueue | 本地缓存 + 增量事件；key 去重、指数退避、可配并发 worker |
| 写入语义 | Create / Update / Patch / Server-Side Apply | 从「读改写」到「声明字段所有权」 |
| 所有权与清理 | ownerReference + finalizer | 级联删除、外部资源回收 |
| 准入控制 | validating/mutating webhook、CEL | 默认值、跨字段校验、版本转换 |
| 权限 | `+kubebuilder:rbac` → Role | 按 verbs/resources 生成最小权限 |
| 测试与高可用 | fake client / envtest / kind；leader election | 三层测试；多副本只有一个实例在协调 |

---

## 3. 核心概念与原理

### 3.1 声明式 API 与 level-triggered 协调

```text
edge-triggered ：事件 → 处理一次 → 忘记（漏一个就永久偏航，重启后不知道错过了什么）
level-triggered：读现状 → 与期望比对 → 收敛 → 再读（每次都重新算，不依赖历史）
```

Deployment 控制器不会因为「收到一个 Pod 删除事件」就补 Pod，而是每次数一遍「现在几个、我要几个」。两个直接结论：**Reconcile 必须可重复执行**，结果只取决于读到的对象状态；**事件只是「可能有事」的提示**，丢了、合并了、重复了都不影响正确性，只影响收敛速度。

### 3.2 CRD、Scheme 与标记

`apiVersion: backup.example.com/v1alpha1` 的 group/version 来自 `create api`，kind 来自 Go struct，三者构成 **GVK**；`Scheme` 负责 GVK ↔ Go 类型双向映射。漏了 `AddToScheme` 的典型报错是 `no kind is registered for the type v1alpha1.BackupSchedule`。

| 标记 | 作用 |
|---|---|
| `+kubebuilder:object:root=true` | 生成 DeepCopy、注册为顶层对象 |
| `+kubebuilder:subresource:status` | 开启 status 子资源：`kubectl apply` 改不动，控制器用 `Status().Update` 单独写 |
| `+kubebuilder:validation:Minimum/Maximum/Enum` | 写进 CRD 的 OpenAPI schema，由 API Server 校验 |
| `+kubebuilder:default=6` | 默认值由 **API Server** 填充，不是控制器 |
| `+kubebuilder:rbac:...` | 生成 `config/rbac/role.yaml`，别手写 Role |
| `+kubebuilder:validation:XValidation:rule="..."` | CEL 跨字段校验，表达力强于 OpenAPI |

status 子资源不只是「好看」：它与 spec 分开存储，控制器高频回写 status 不会和用户的 `kubectl apply` 抢同一个 `resourceVersion`，也避免用户改 status 造成状态漂移。

### 3.3 Reconcile 的输入是 NamespacedName

```text
watch 事件 → informer 更新本地 cache → 入队 key = "namespace/name"（自动去重）
         → rate limiter（失败指数退避 5ms→1000s）→ worker 取 key → Get 最新对象 → Reconcile
```

签名只有 `ctrl.Request{NamespacedName}`，没有事件类型、没有旧对象——框架刻意不让你依赖「为什么被叫醒」，只能去读现状。

一次逻辑变更常触发多次协调：`Update` 加 finalizer → 自身事件入队；创建 Deployment → `Owns()` 让子资源变化也入队；写 status → 又一次事件；失败返回 error → 退避重试；cache resync（默认 10h）→ 周期兜底；短时间 10 个事件合并成 1 个 key。

所以**绝不在 Reconcile 里 sleep 等资源就绪**：占住 worker、阻塞队列，等待期间对象还可能已变。要「过会儿再看」返回 `ctrl.Result{RequeueAfter: 30 * time.Second}`；要「立刻重试」返回 error 交给限速器。

### 3.4 幂等设计：先查再建、CreateOrUpdate、SSA

| 写法 | 语义 | 风险 |
|---|---|---|
| `Get` 后 `Create` | 不存在才建 | 竞态下两个 worker 都 404，第二个 `AlreadyExists`；需显式视为成功 |
| `controllerutil.CreateOrUpdate` | 读 → 改 → 写 | 内部是 Update，会用完整对象覆盖别人改的字段；冲突要重试 |
| `client.Apply`（SSA） | 只声明「我拥有的字段」 | 需正确 `FieldOwner`；所有权冲突要 `ForceOwnership`，慎用 |

判断标准是**字段归属**：CR 完全拥有的从属对象（它建的 Deployment）两种都行；与他人共享的对象（往公共 ConfigMap 加 key）优先 SSA，避免抹掉别人的 key。

### 3.5 乐观并发与 resourceVersion

K8s 没有悲观锁：`Update` 带上读到的 `resourceVersion`，服务端发现已被改过就返回 **409 Conflict**。正确姿势是「重读 → 重算 → 再写」：判断 `apierrors.IsConflict(err)` 后返回 `ctrl.Result{Requeue: true}, nil`，交给队列退避重试，而不是把旧对象硬写回去。

同一对象不会被两个 worker 并发处理（workqueue 保证 key 独占），但**你的控制器 + 用户的 kubectl + 另一个控制器**随时并发写同一对象，冲突是常态而非异常。

### 3.6 ownerReference 与 finalizer

- `controllerutil.SetControllerReference(cr, child, scheme)`：子对象变动触发 owner 入队（配合 `Owns()`），owner 删除时子对象被 GC 级联删除（默认 background）
- **owner 必须与 child 同 namespace**（或 owner 是集群级），跨 namespace 只能靠 label + 手动清理
- 级联删除对**集群外资源无效**：云快照、DNS 记录、对象存储桶，GC 一无所知 → 用 finalizer

```text
kubectl delete → 打 deletionTimestamp（对象 Terminating）→ 控制器清外部资源
              → RemoveFinalizer → finalizers 清空 → 从 etcd 真正消失
```

**finalizer 卡死排查**：`kubectl get bsched nightly -o jsonpath='{.metadata.finalizers}'` 看是谁挂的 → 找对应控制器日志（没在跑 / panic / 权限不足）→ 确认 RBAC 有 `<resource>/finalizers` 的 `update` → 最后才考虑 `kubectl patch ... --type=merge -p '{"metadata":{"finalizers":null}}'` 强摘（会泄漏外部资源，仅限确认无害时）。

---

## 4. 使用实战

目标：`BackupSchedule` CR 声明「每 N 小时备份一次」，控制器维护 Deployment + Service 并回写 status。

### 4.1 API 类型与标记

```go
// api/v1alpha1/backupschedule_types.go
package v1alpha1

import metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

type BackupScheduleSpec struct {
	// +kubebuilder:validation:Minimum=1
	// +kubebuilder:validation:Maximum=24
	// +kubebuilder:default=6
	IntervalHours int32 `json:"intervalHours,omitempty"`
	// +kubebuilder:validation:MinLength=1
	Image string `json:"image"`
}

type BackupScheduleStatus struct {
	// +listType=map
	// +listMapKey=type
	Conditions         []metav1.Condition `json:"conditions,omitempty"`
	ObservedGeneration int64              `json:"observedGeneration,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status
// +kubebuilder:printcolumn:name="Ready",type=string,JSONPath=`.status.conditions[?(@.type=="Ready")].status`
type BackupSchedule struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`
	Spec              BackupScheduleSpec   `json:"spec,omitempty"`
	Status            BackupScheduleStatus `json:"status,omitempty"`
}

// BackupScheduleList 与 init() 里的 SchemeBuilder.Register 由 scaffold 生成，此处省略。
```

### 4.2 CRD 与 CR

`make manifests` 生成完整 schema；下面是等价可用的精简版（status 用 `preserve-unknown-fields` 省去 conditions 的 schema 细节）：

```yaml
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata: {name: backupschedules.backup.example.com}
spec:
  group: backup.example.com
  scope: Namespaced
  names: {kind: BackupSchedule, listKind: BackupScheduleList, plural: backupschedules, singular: backupschedule, shortNames: [bsched]}
  versions:
    - name: v1alpha1
      served: true
      storage: true
      subresources: {status: {}}        # 必须开启，控制器才能单独写 status
      schema:
        openAPIV3Schema:
          type: object
          properties:
            spec: {type: object, required: [image], properties: {intervalHours: {type: integer, minimum: 1, maximum: 24, default: 6}, image: {type: string, minLength: 1}}}
            status: {type: object, x-kubernetes-preserve-unknown-fields: true}
```

```bash
# 示例 CR（config/samples/）：spec: {intervalHours: 6, image: "registry.example.com/pg-backup:v1.4.0"}
kubectl apply -f config/crd/bases/backup.example.com_backupschedules.yaml -f config/samples/backup_v1alpha1_backupschedule.yaml
kubectl get bsched && kubectl get bsched nightly -o yaml   # READY 列由 §4.1 的 printcolumn 生成
```

### 4.3 Reconcile 实现、Manager 组装与 RBAC

```go
// internal/controller/backupschedule_controller.go
package controller

import (
	"context"
	"fmt"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"

	backupv1 "example.com/backup-operator/api/v1alpha1"
)

const finalizer = "backup.example.com/cleanup"

type BackupScheduleReconciler struct {
	client.Client
	Scheme *runtime.Scheme
}

// +kubebuilder:rbac:groups=backup.example.com,resources=backupschedules,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=backup.example.com,resources=backupschedules/status,verbs=get;update;patch
// +kubebuilder:rbac:groups=backup.example.com,resources=backupschedules/finalizers,verbs=update
// +kubebuilder:rbac:groups=apps,resources=deployments,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups="",resources=services,verbs=get;list;watch;create;update;patch;delete

func (r *BackupScheduleReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	// 事件只是提示：每次都从 cache 读最新对象
	var bs backupv1.BackupSchedule
	if err := r.Get(ctx, req.NamespacedName, &bs); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err) // 已删除不是错误
	}
	// 删除路径：先清外部资源，再摘 finalizer；清不掉就保留，交给退避重试
	if !bs.DeletionTimestamp.IsZero() {
		if controllerutil.ContainsFinalizer(&bs, finalizer) {
			if err := r.cleanupRemote(ctx, &bs); err != nil {
				return ctrl.Result{}, err
			}
			controllerutil.RemoveFinalizer(&bs, finalizer)
			return ctrl.Result{}, r.Update(ctx, &bs)
		}
		return ctrl.Result{}, nil
	}
	// 首次进入：AddFinalizer 返回 true 表示真的加上了，写完立刻返回让新版本重新入队
	if controllerutil.AddFinalizer(&bs, finalizer) {
		return ctrl.Result{}, r.Update(ctx, &bs)
	}
	// 收敛从属对象；每次协调都走一遍，因此必须幂等
	if err := r.reconcileWorkload(ctx, &bs); err != nil {
		r.markReady(ctx, &bs, metav1.ConditionFalse, "WorkloadFailed", err.Error())
		return ctrl.Result{}, err
	}
	r.markReady(ctx, &bs, metav1.ConditionTrue, "Reconciled", "backup worker is ready")
	return ctrl.Result{RequeueAfter: time.Duration(bs.Spec.IntervalHours) * time.Hour}, nil // 不是 sleep
}

func (r *BackupScheduleReconciler) reconcileWorkload(ctx context.Context, bs *backupv1.BackupSchedule) error {
	labels := map[string]string{"app.kubernetes.io/name": "pg-backup", "app.kubernetes.io/instance": bs.Name}

	dep := &appsv1.Deployment{ObjectMeta: metav1.ObjectMeta{Name: bs.Name, Namespace: bs.Namespace}}
	if _, err := controllerutil.CreateOrUpdate(ctx, r.Client, dep, func() error {
		replicas := int32(1)
		dep.Spec.Replicas = &replicas
		dep.Spec.Selector = &metav1.LabelSelector{MatchLabels: labels}
		dep.Spec.Template.ObjectMeta.Labels = labels
		dep.Spec.Template.Spec.Containers = []corev1.Container{{
			Name: "worker", Image: bs.Spec.Image,
			Args:  []string{"--interval", fmt.Sprint(bs.Spec.IntervalHours)},
			Ports: []corev1.ContainerPort{{Name: "metrics", ContainerPort: 9090}},
		}}
		return controllerutil.SetControllerReference(bs, dep, r.Scheme) // 级联删除 + Owns 触发
	}); err != nil {
		return fmt.Errorf("deployment: %w", err)
	}

	svc := &corev1.Service{ObjectMeta: metav1.ObjectMeta{Name: bs.Name, Namespace: bs.Namespace}}
	if _, err := controllerutil.CreateOrUpdate(ctx, r.Client, svc, func() error {
		svc.Spec.Selector, svc.Spec.Ports = labels, []corev1.ServicePort{{Name: "metrics", Port: 9090}}
		return controllerutil.SetControllerReference(bs, svc, r.Scheme)
	}); err != nil {
		return fmt.Errorf("service: %w", err)
	}
	return nil
}

// markReady 只在状态真变化时写 status，避免「写 status → 事件 → 再写」的热循环
func (r *BackupScheduleReconciler) markReady(ctx context.Context, bs *backupv1.BackupSchedule,
	status metav1.ConditionStatus, reason, msg string) {
	changed := meta.SetStatusCondition(&bs.Status.Conditions, metav1.Condition{Type: "Ready",
		Status: status, Reason: reason, Message: msg, ObservedGeneration: bs.Generation})
	if changed || bs.Status.ObservedGeneration != bs.Generation {
		bs.Status.ObservedGeneration = bs.Generation
		_ = r.Status().Update(ctx, bs) // 失败由下一次协调兜底
	}
}

func (r *BackupScheduleReconciler) cleanupRemote(context.Context, *backupv1.BackupSchedule) error {
	return nil // 真实实现：删除对象存储/云上的备份目录
}

// SetupWithManager 注册 watch：主资源 + 自己创建的 Deployment，子资源变化也会入队。
func (r *BackupScheduleReconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&backupv1.BackupSchedule{}).
		Owns(&appsv1.Deployment{}).
		Complete(r)
}
```

> `meta.SetStatusCondition` 只在条件内容变化时返回 true；但 spec 变更（generation 递增）不一定改变条件，所以再用 `ObservedGeneration` 兜住一次写入。watch 由 `SetupWithManager` 里的 `For(&BackupSchedule{}).Owns(&appsv1.Deployment{})` 建立，子资源变化同样会入队。

```go
// cmd/main.go 关键片段
mgr, err := ctrl.NewManager(ctrl.GetConfigOrDie(), ctrl.Options{
	Scheme: scheme, LeaderElection: true,                   // 多副本只让一个实例协调
	LeaderElectionID:       "backup-operator.backup.example.com", // 抢 Lease，默认 15s 续约
	HealthProbeBindAddress: ":8081",
})
if err != nil { return 1 }
if err := (&controller.BackupScheduleReconciler{Client: mgr.GetClient(), Scheme: mgr.GetScheme()}).SetupWithManager(mgr); err != nil { return 1 }
if err := mgr.Start(ctrl.SetupSignalHandler()); err != nil { return 1 } // SIGTERM 优雅退出
```

`make manifests` 由上面的 marker 生成 `config/rbac/role.yaml`：控制器必须能 `list`/`watch` 才能建 informer cache，只给 `get` 会在启动时报 `cannot list resource`——这是 RBAC 报错最常见的一行；单 namespace 部署把 `ClusterRole` 换成 `Role` + `RoleBinding`。控制器自身以 Deployment 部署（2 副本 + PDB，靠上面的 Lease 选主），`kubectl -n backup-system get lease backup-operator.backup.example.com -o yaml` 可看谁在协调。

### 4.4 测试三层

```go
// 第一层：fake client 单测，不启 apiserver，毫秒级，覆盖分支与幂等
func TestReconcileCreatesDeployment(t *testing.T) {
	sch := runtime.NewScheme()
	_ = clientgoscheme.AddToScheme(sch)
	_ = backupv1.AddToScheme(sch)
	key := types.NamespacedName{Name: "nightly", Namespace: "default"}
	bs := &backupv1.BackupSchedule{ObjectMeta: metav1.ObjectMeta{Name: key.Name, Namespace: key.Namespace},
		Spec: backupv1.BackupScheduleSpec{Image: "pg-backup:v1", IntervalHours: 6}}
	c := fake.NewClientBuilder().WithScheme(sch).WithStatusSubresource(bs).WithObjects(bs).Build()
	r := &BackupScheduleReconciler{Client: c, Scheme: sch}
	for i := 0; i < 3; i++ { // 第一次挂 finalizer，后两次验证幂等（不应 AlreadyExists）
		_, err := r.Reconcile(t.Context(), ctrl.Request{NamespacedName: key})
		require.NoError(t, err)
	}
	var dep appsv1.Deployment
	require.NoError(t, c.Get(t.Context(), key, &dep))
	require.Equal(t, "pg-backup:v1", dep.Spec.Template.Spec.Containers[0].Image)
}
```

```go
// 第二层：envtest 集成，真 apiserver + etcd，验证 CRD schema、默认值、status 子资源、finalizer
testEnv := &envtest.Environment{CRDDirectoryPaths: []string{"../../config/crd/bases"}, ErrorIfCRDPathMissing: true}
_, err := testEnv.Start() // 返回的 cfg 交给 ctrl.NewManager；启动即校验 CRD 能装进 apiserver
require.NoError(t, err)
```

```bash
# 第三层：kind e2e，验证真实调度、真实删除与级联
kind create cluster --image kindest/node:v1.37.0 && make docker-build deploy IMG=registry.example.com/backup-operator:v0.1.0
kubectl apply -f config/samples/ && kubectl get bsched nightly -w
kubectl delete bsched nightly && kubectl get deploy nightly   # 应被级联删除
```

排障入口：`kubectl -n backup-system get lease backup-operator.backup.example.com -o yaml` 看 leader；`kubectl get events --field-selector involvedObject.name=nightly` 看 Event；`curl -s localhost:8080/metrics | grep controller_runtime_reconcile` 看错误数与耗时。

---

## 5. 版本演进速览

| 阶段 | 关键变化与迁移注意点 |
|---|---|
| K8s 1.16 | **CRD `v1` GA**：必须提供完整 OpenAPI schema，不再允许 `preserveUnknownFields: true` |
| K8s 1.22 | CRD `v1beta1` 移除；**Server-Side Apply GA**，字段所有权模型稳定 |
| K8s 1.25 | CRD 的 **CEL 校验 GA**（`x-kubernetes-validations`），跨字段/不可变规则不必再写 webhook |
| K8s 1.30 | **ValidatingAdmissionPolicy GA**，部分 webhook 场景可由 CEL 策略替代；CRD 校验 ratcheting 改善升级兼容 |
| kubebuilder v3 → v4 | 布局改 `go/v4`：`main.go` 移到 `cmd/`、控制器进 `internal/controller/`、Kustomize v5、`PROJECT` 文件记录 layout 版本 |
| controller-runtime v0.22 | 原生 SSA 客户端（`client.Apply`）、controller warmup、依赖 k8s 1.34 |
| controller-runtime v0.23 | 优先级队列默认启用；webhook builder 改为 `WebhookManagedBy(mgr, obj)`；Event 迁到 `events.k8s.io`，RBAC 需同步改 |

**迁移注意**：

- controller-runtime 每个 minor 对应一个 client-go minor，**绑在一起升级**；跨版本混搭不受支持（v0.25 对齐 client-go v0.37 / K8s 1.37）
- v0.23 起不再用 `mgr.GetEventRecorderFor`，RBAC 里 core 的 `events` 要换成 `events.k8s.io`；CRD 删字段、改类型、改 storage version 需要 conversion webhook 或新版本 + 数据迁移

---

## 6. 常见坑与最佳实践

1. **Reconcile 不幂等**：`Create` 前不 `Get`，第二次协调就 `AlreadyExists` 刷屏；或用 `GenerateName` 每次造新对象，越协调越多。先读后写或直接 `CreateOrUpdate`；把 `IsAlreadyExists` 当作成功，也应当是显式决定而非碰巧。
2. **忽略 409 Conflict**：拿陈旧对象 `Update` 覆盖，丢用户修改。判断 `apierrors.IsConflict` → 重读重算 → 重试，或直接返回 error 交给限速队列。
3. **写 status 触发自身再入队的热循环**：无条件 `Status().Update`，即使内容没变 `resourceVersion` 也会变 → 新事件 → 再协调，CPU 与 API QPS 一起打满。写前比对，并给 `For()` 加 `predicate.GenerationChangedPredicate{}` 过滤纯 status 事件。
4. **在 Reconcile 里 sleep 等就绪**：占住 worker、队头阻塞，吞吐塌陷。用 `RequeueAfter`，或 `Owns` 子资源让它 Ready 时把你叫醒。
5. **finalizer 没删导致资源删不掉**：清理失败却摘了 finalizer（泄漏外部资源），或异常时永不摘（卡死）。失败必须返回 error 保留 finalizer，并确认 RBAC 有 `<resource>/finalizers` 的 `update`。
6. **RBAC 直接给 cluster-admin**：控制器从此能删任意 namespace 的任意资源。用 marker 生成最小权限；`Owns` 的资源必须有 `watch`；单 namespace 场景用 `Role` 而非 `ClusterRole`。
7. **业务逻辑全塞进 Reconcile**：几百行 `if/else` 混着 API 调用与状态机，无法测试。拆成纯函数（spec + 现状 → 计划）+ 薄副作用层，纯函数用表驱动测试。
8. **过度 watch**：watch 全集群 Pod/Secret，或每个 CR 建一个 informer，把 API Server 和内存打爆。按 namespace 收敛 cache（`cache.Options.DefaultNamespaces`）、用 selector 过滤、限制 `MaxConcurrentReconciles`。
9. **跨 namespace 设 ownerReference**：API Server 直接拒绝（`OwnerRefInvalidNamespace`），或被 GC 误删。跨 ns 用 label 关联 + 显式 finalizer 清理。
10. **忘记 `AddToScheme`**：`no kind is registered` 或 decode 出空对象。新 API 包要在 `main.go` 和测试里各注册一次。
11. **控制器自身无资源限制**：manager 被 OOMKilled 会让「所有 CR 停止收敛」，但集群表面看起来正常。给 requests/limits、PDB，并监控 `controller_runtime_reconcile_errors_total`；也别把 Operator 当 Helm 用——只「创建一次资源」而不纠偏、不写 status、不管 finalizer，那不如用 Helm。

---

## 7. 选型对比

| 维度 | kubebuilder + controller-runtime | Operator SDK（Ansible/Helm） | Helm Chart | Kustomize | Crossplane |
|---|---|---|---|---|---|
| 表达力 | **最强**，任意 Go 逻辑 | 中（playbook/模板） | 低（模板渲染） | 低（patch 叠加） | 中高（Composition） |
| 持续纠偏 | **原生** level-triggered | 有（周期运行） | 无（一次性 apply） | 无 | 有 |
| status / conditions | **完整** | 弱 | 无 | 无 | 有 |
| finalizer / 外部清理 | **完整** | 有限 | 无（hook 易失败） | 无 | 有 |
| 学习成本 | 高（Go + 控制器模型） | 中（会 Ansible 即可） | **低** | 低 | 中 |
| 典型场景 | 有状态服务、复杂生命周期 | 已有 Ansible 资产 | 无状态应用发布 | 环境差异化部署 | 云资源编排 |

一句话决策：

- **要「持续收敛 + 生命周期 + 外部资源清理」** → kubebuilder + controller-runtime，生态标准、可测试、可维护。
- **只是把一组 YAML 发上去** → Helm（配 Kustomize 做环境差异），别写 Operator。
- **团队已有大量 Ansible playbook、逻辑不复杂** → Operator SDK 的 Ansible 插件，复用资产。
- **主要编排云资源（RDS、OSS、VPC）或不想自研** → Crossplane、云厂商托管 Operator，或用 PaaS 托底。

> 落地顺序：先把 CRD 的 API 设计对（字段、默认值、校验、conditions）→ 再写幂等 Reconcile → 补 finalizer 与 RBAC → 最后才是 webhook、多版本转换、HA 与可观测。**API 设计错了，后面每一层都在还债。**

参考：<https://book.kubebuilder.io/> · <https://sdk.operatorframework.io/docs/building-operators/golang/> · <https://kubernetes.io/docs/concepts/extend-kubernetes/api-extension/custom-resources/> · <https://kubernetes.io/docs/reference/using-api/server-side-apply/>（版本与特性整理于 2026-11，具体以上游最新 release 为准）
