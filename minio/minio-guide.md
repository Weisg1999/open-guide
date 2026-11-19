# MinIO 技术介绍与使用指南

> 高性能 S3 兼容对象存储：单二进制、纠删码数据保护、云原生与 AI 数据湖首选的自托管对象存储。
> 仓库：<https://github.com/minio/minio>　|　状态：社区版已归档停维（2026-04；最后版本 RELEASE.2025-10-15，仅源码分发）　|　许可证：AGPLv3

---

## 1. 项目简介

MinIO 是用 Go 编写的**软件定义对象存储**，100% 兼容 AWS S3 API。它把"对象存储"从专用硬件解放出来：任何有磁盘的机器（裸机、VM、K8s、边缘盒子）跑一个 `minio server` 就是生产级对象存储。

核心卖点：

- **S3 兼容**：AWS SDK / aws-cli / S3 生态工具（Spark、Hive、Trino、S3Select）即插即用——迁移成本近乎为零；
- **高性能**：Go 原生实现纠删码与 HighwayHash 校验，实测吞吐可达数百 GB/s 级（JBOD + 万兆网即可）；
- **云原生**：单二进制无依赖、K8s Operator 官方支持、原生 Prometheus 指标；
- **数据保护**：纠删码（Reed-Solomon）+ **bit-rot（比特腐烂）防护**（HighwayHash 每块校验）+ 服务端加密（SSE-S3/SSE-KMS/SSE-C）；
- **企业特性**：版本控制、站点/桶级复制、生命周期管理（ILM）、事件通知、对象锁定（WORM/合规保留）、IAM。

典型用途：云原生应用附件存储、数据湖底座（Parquet/Arrow 生态）、AI/ML 训练数据集、备份归档（Velero 后端）、Kafka tiered storage 目标、替代 S3 的私有云存储。

### 部署形态（决定性选择）

| 形态 | 命令形态 | 适用 |
|---|---|---|
| 单机单盘 (SNDD) | `minio server /data` | 开发 |
| 单机多盘 (SNMD) | `minio server /d1 /d2 /d3 /d4` | 边缘/小规模 |
| 多机多盘 (MNMD) | `minio server http://n{1...4}/d{1...16}` | 生产（分布式纠删码） |

---

## 2. 核心概念

### 2.1 对象存储模型

```text
Bucket（桶，顶层命名空间）
  └── Object（对象 = 数据 + 元数据）
        ├── key:   photos/2026/alice.png   （"路径"只是 key 的字符串形态）
        ├── data:  任意字节流（KB ~ TB 级）
        └── meta:  Content-Type、ETag、自定义 x-amz-* 元数据
```

与传统文件系统的区别：**没有真正的目录层级**（key 是平面字符串）、**不擅长修改**（对象不可变，覆盖即新版本）、**强项是海量 + 一次性读写**。

### 2.2 纠删码（Erasure Coding）：MinIO 的灵魂

传统三副本存储存 1TB 要花 3TB；MinIO 用 Reed-Solomon 纠删码把对象切成 **N 个数据块 + M 个校验块**，分散到独立磁盘：

```text
对象 (1MB) ──EC 编码──► [D1][D2][D3][D4] [P1][P2]   （EC:4+2，共 6 块 6 盘）
                         └── 任意坏 2 块，仍可完整还原 ──┘
```

- 读写比例可配：`EC:4+2`（1.5x 空间开销，容忍 2 盘坏）、`EC:8+4`（1.5x）、`EC:8+8`（2x，容忍一半盘坏）；
- **读写 quorum**：设数据块 K、校验块 M（N=K+M）。读需要 K 块；写也需要 K 块（当 M 恰为 N/2 时需 K+1，防脑裂）——写永远不会比读更宽松；
- 每个数据块附 HighwayHash 校验——磁盘静默损坏（bit-rot）在读时被发现并**用其余块在线修复**，这是 MinIO 相比"rsync + 多副本"方案的本质优势；
- 纠删集（EC set）= 一组独立磁盘（4/6/8/12/16 盘）；集群扩容按 server pool 横向加。

### 2.3 元数据内联

对象元数据（连同纠删配置、版本信息）**与数据块一起原子地写进纠删组**，不依赖外部元数据数据库——没有"元数据服务器"这个单点/瓶颈，这是 MinIO 能横向扩展到 EB 级的架构根基。

### 2.4 IAM 与安全

- 策略语法兼容 AWS IAM（按 ARN/动作授权，可细化到 bucket/prefix）；
- 用户/组/服务账号；LDAP/OIDC 外部身份源（企业/AIStor 能力）；
- STS 临时凭证（assume role、client certificate 等多种模式）；
- 加密：SSE-S3（KMS 托管）、SSE-C（客户密钥）、SSE-KMS；TLS 全链路。

### 2.5 数据生命周期能力

- **版本控制**：bucket 开版本后，删除/覆盖产生 delete marker 与历史版本，可回滚；
- **ILM（生命周期）**：按天数/版本数/标签做过期、转 Tier（远程 S3/Azure/GCS）；
- **复制（Replication）**：桶级/站点级（active-active），含元数据与删除同步；
- **对象锁定**：WORM 合规（金融留痕）、法定保留；
- **事件通知**：put/delete 等事件推到 Kafka/NATS/AMQP/Redis/Webhook——可作事件驱动架构的源头。

### 2.6 2026 年的现实：社区版归档与替代选型

事实先摊开（完整时间线见第 6 章）：社区版最后版本 `RELEASE.2025-10-15T17-29-55Z`；2025-10 起不再发布预编译二进制与 Docker 镜像；2026-04 仓库归档为只读。结论很直接——**新部署不要再选社区版**，存量集群的问题则从"怎么用"变成"怎么退"。

四类可行替代的定位差别很大：

| 方案 | 形态 | 最适规模 | 优势 | 代价 |
|---|---|---|---|---|
| **Garage** | Rust 单二进制 | 中小规模（TB ~ 百 TB） | 部署极简、地理分布、资源占用低 | 企业特性少（无冷热分层、对象锁弱）、生态工具少 |
| **SeaweedFS** | Go，master/volume/filer | 海量小文件（亿级对象） | 小文件元数据高效、可 POSIX 挂载、内置 S3 网关 | 组件多、运维模型不同、大对象吞吐需调优 |
| **Ceph RGW** | C++，需完整 Ceph 集群 | 已有 Ceph / PB 级 | 块/文件/对象一体、生态成熟、有商业支持 | 部署重（MON/OSD/MGR）、要专职存储运维 |
| **云厂商 S3 / OSS / COS** | 托管服务 | 任意，含爆发增长 | 零运维、弹性、功能最全 | 长期单价高、**出口流量费**、数据主权与锁定 |

选型不是比谁跑分高，而是看**你的存量用法落在哪些维度上**。先把用法盘出来，这份输出就是迁移需求清单：

```bash
mc admin info local                                     # 节点/磁盘/容量
mc ls --recursive local/mybucket | wc -l                # 当前对象数
mc ilm ls local/mybucket                                # 生命周期规则
mc version info local/mybucket                          # 是否开了版本控制
mc event list local/mybucket                            # 事件通知目标
mc admin policy list local                              # 自定义 IAM 策略
```

迁移成本按四个维度评估，缺一项就会在上线后才发现功能没了：

- **S3 API 兼容度**：`PutObject`/`GetObject` 能跑不等于兼容。重点验 `ListObjectsV2` 分页、`CopyObject` 大对象（>5GB 必须走分片复制）、预签名 URL 的 header 签名、`x-amz-meta-*` 自定义元数据保真；
- **IAM 模型**：Garage/SeaweedFS 多为"静态 key + 粗粒度策略"，MinIO 那种 ARN 级策略与 STS 临时凭证常无对应物——**依赖 STS 的应用改造成本最高**；
- **版本控制**：不是所有实现都有"删除标记 + 历史版本"完整语义，开了版本再迁会把差异放大；
- **事件通知**：Kafka/NATS 目标通常可替代，但事件字段与 delete marker 事件的触发时机不完全一致，消费端要做兼容。

**存量 MinIO 还能用多久？** 实话是：软件不会自己过期。归档版本没有 license 到期机制，硬件不坏、依赖不变，跑三年五年都正常。真正的风险只有三条——**没有安全补丁**（S3 网关暴露在网上的，新 CVE 只能自己评估）、**依赖腐化**（Go 版本、基础镜像、TLS 库逐渐与 OS 脱节，某次系统升级后可能编不起来）、**无人应答**（出问题只能读源码）。

所以务实策略是：**存量不动、新量不添、常备退路**。把归档版本当"冻结的稳定设施"运营（锁定镜像 digest、收紧网络与审计），同时用 `mc mirror` 维持一条到新集群的冷备通道——退出成本最低的状态，是数据始终在两个地方。

### 2.7 一致性与并发：读己之写、覆盖与锁

**语义演进**：AWS S3 在 2020-12 之前，覆盖写与删除是最终一致（写完立刻 GET 可能读到旧值），之后改为强一致。MinIO 从第一天就是**强一致**：PUT 返回 200 后，任何节点上的 GET 都能读到新数据——这正是它当年被当作"本地 S3"的主要原因。仍维护的替代品（Garage/SeaweedFS/Ceph RGW）核心路径同样强一致，但**跨站点复制是异步的**，异地读到旧值属于预期行为。

**同一 key 并发 PUT：最后写入者胜（LWW）**。对象存储没有文件锁，两个客户端同时写 `a.txt`，服务端既不报错也不合并，谁最后完成谁留下，另一份静默消失。这不是 bug，是"对象不可变 + 整体替换"模型的必然结果。

```text
t0  A: PUT a.txt (v1)  ─────────► 200 OK
t1  B: PUT a.txt (v2)        ─────────► 200 OK     最终 a.txt = v2，v1 无痕迹
t2  A: GET a.txt  ──► 读到 v2（A 以为自己的写生效了）
```

要避免静默覆盖，用**条件写**：

| 请求头 | 语义 | 典型用途 |
|---|---|---|
| `If-None-Match: *` | 仅当对象不存在才写入 | 创建型写、去重、防覆盖 |
| `If-Match: <etag>` | 仅当当前 ETag 匹配才写入 | 乐观锁 / CAS（读-改-写） |
| `x-amz-copy-source-if-match` | 校验复制源版本 | 服务端复制时防源被并发改 |

兼容性务必实测：`If-None-Match` 创建语义 AWS 在 2024 年才正式支持，各家实现程度不一，**不支持时服务端会静默忽略该头**，等于没有保护。真不支持，只能退回应用层用 Redis/etcd 串行化，代价是多一个依赖。

**分片上传的生命周期**：`CreateMultipartUpload` 返回 `uploadId`，之后 `UploadPart` 可任意并发、乱序，`CompleteMultipartUpload` 时按 part number 组装。要点：

- `uploadId` 不是永久资源，未完成上传的 part **一直占着容量并计入存储**；
- 必须配 `AbortIncompleteMultipartUpload` 规则（如 7 天）自动清理，否则孤儿分片会缓慢吃光空间；
- 完成之后 `uploadId` 立即失效，重试返回 `NoSuchUpload`——所以完成操作自身要幂等（先 HEAD 确认对象是否已存在）。

**删除标记与版本控制的交互**：未开版本时 `DELETE` 是真删；开了版本后，不带 `versionId` 的 `DELETE` 只插入一个 **delete marker**，数据仍在，`GET` 返回 404、`LIST` 默认看不到。要真删必须指定 `versionId`。这意味着**"删了"不等于"空间释放了"**——清理得靠生命周期按天数/版本数过期非当前版本，否则桶只会单向变大。

---

## 3. 架构与数据路径

```text
Client ──S3 API──► MinIO 节点（无主节点，对等）
                      │ 对 key 一致性哈希定位到 EC set
                      ▼
                 ┌── 纠删集：D1..DN, P1..PM 分布在多机多盘 ──┐
                 │  PUT：并行写 N+M 块（+HighwayHash）        │
                 │  GET：并行读最快返回的 N 块               │
                 └──────────────────────────────────────────┘
```

- **对等集群**：任何节点接收任何请求，无元数据服务器、无 leader——节点数只受网络与运维约束；
- **写路径**：节点内并行地把数据+校验块写到 EC set 的各盘，多数成功即成功；
- **读路径**：并行读、先到先得（healing 读：发现坏块异步重建回写）；
- **负载**：网关与纠删计算都在用户进程空间，无上下文切换——配合 Go 运行时实现高吞吐。

---

## 4. 使用实战

### 4.1 启动与 mc

```bash
# 分布式 4 节点 × 4 盘
minio server http://node{1...4}/data{1...4} --console-address ":9001"

# mc（MinIO Client）
mc alias set local http://127.0.0.1:9000 minioadmin minioadmin
mc mb local/mybucket
mc cp ./report.pdf local/mybucket/docs/
mc ls local/mybucket --recursive
mc mirror local/mybucket ./backup-dir      # 目录/桶互相同步
mc admin info local                        # 集群与磁盘状态
mc ilm rule add --expiry-days 90 local/mybucket # 生命周期（旧写法 mc ilm add 已废弃）
```

### 4.2 Go SDK（minio-go，S3 风格）

```bash
go get github.com/minio/minio-go/v7
```

```go
package main

import (
    "context"
    "github.com/minio/minio-go/v7"
    "github.com/minio/minio-go/v7/pkg/credentials"
)

func main() {
    cli, err := minio.New("127.0.0.1:9000", &minio.Options{
        Creds: credentials.NewStaticV4("minioadmin", "minioadmin", ""),
        Secure: false,
    })
    ctx := context.Background()

    // 建桶
    cli.MakeBucket(ctx, "photos", minio.MakeBucketOptions{Region: "us-east-1"})

    // 上传（服务端自动分片纠删码）
    cli.FPutObject(ctx, "photos", "2026/alice.png", "./alice.png",
        minio.PutObjectOptions{ContentType: "image/png"})

    // 下载
    cli.FGetObject(ctx, "photos", "2026/alice.png", "./out.png",
        minio.GetObjectOptions{})

    // 流式上传（适合 HTTP 转存场景）
    cli.PutObject(ctx, "photos", "stream.bin", reader, size,
        minio.PutObjectOptions{})
}
```

### 4.3 Presigned URL（最常见的集成模式）

```go
// 生成 7 天有效的下载链接，应用不代理流量、客户端直连 MinIO
url, _ := cli.PresignedGetObject(ctx, "photos", "2026/alice.png",
    7*24*time.Hour, nil)

// 生成上传链接（让客户端直传，如浏览器上传大文件）
upURL, _ := cli.PresignedPutObject(ctx, "photos", "user-upload.bin", time.Hour)
```

### 4.4 版本控制 + 事件通知

```bash
mc version enable local/mybucket          # 开版本（防误删）
mc admin config set local notify_kafka:1 \
  brokers="kafka:9092" topic="minio-events"
mc event add local/mybucket arn:minio:sqs::1:kafka \
  --event put,delete                       # 对象事件推 Kafka
```

事件负载（JSON）包含 bucket/key/size/etag 等——配合 Kafka/NATS 即"对象落库 → 触发处理"的流水线。

### 4.5 分片上传与断点续传

单个 `PutObject` 是一次 HTTP 请求，中途断网就得从头再来；超过百 MB 的对象应走**分片上传（Multipart Upload）**：拆成 N 片并发送，服务端按 part number 拼接。

| 约束 | 数值 | 说明 |
|---|---|---|
| 单片大小 | 5 MiB ~ 5 GiB | 最后一片可小于 5 MiB |
| 分片数量 | ≤ 10000 | 决定单对象上限：5 GiB × 10000 ≈ 48.8 TiB（实现上限多为 5 TiB） |
| 建议分片 | 8 ~ 64 MiB（GB 级）；64 ~ 256 MiB（百 GB 级） | **100 MB 以下的对象不必分片**，并发收益抵不过三次往返 |

下面的例子覆盖完整链路：开启上传 → 查 `ListParts` 续传 → 并发上传（片内重试）→ 按序提交，失败时显式 Abort。

```go
package main

import (
	"context"
	"fmt"
	"io"
	"log"
	"os"
	"sync"

	"github.com/minio/minio-go/v7"
	"github.com/minio/minio-go/v7/pkg/credentials"
)

const (
	bucket, object = "backup", "db/full-2026-11.dump"
	partSize       = 64 << 20 // 64 MiB
	workers        = 8
)

func main() {
	ctx := context.Background()
	cli, err := minio.New("127.0.0.1:9000", &minio.Options{
		Creds: credentials.NewStaticV4("minioadmin", "minioadmin", ""), Secure: false,
	})
	if err != nil {
		log.Fatal(err)
	}
	f, err := os.Open("./full-2026-11.dump")
	if err != nil {
		log.Fatal(err)
	}
	defer f.Close()
	st, _ := f.Stat()
	size := st.Size()
	total := int((size + partSize - 1) / partSize)

	// 1) 复用上次崩溃遗留的 uploadId 即断点续传，否则新建
	uploadID := ""
	for info := range cli.ListIncompleteUploads(ctx, bucket, object, true) {
		if info.Err == nil && info.Key == object {
			uploadID = info.UploadID
			break
		}
	}
	if uploadID == "" {
		if uploadID, err = cli.NewMultipartUpload(ctx, bucket, object, minio.PutObjectOptions{}); err != nil {
			log.Fatal(err)
		}
	}
	abort := func(msg string, e error) {
		_ = cli.AbortMultipartUpload(context.Background(), bucket, object, uploadID)
		log.Fatalf("%s: %v", msg, e) // 不 Abort 就留下永久计费的孤儿分片
	}

	// 2) ListParts 拿到续传基线
	parts := map[int]minio.CompletePart{}
	marker := 0
	for {
		res, err := cli.ListObjectParts(ctx, bucket, object, uploadID, marker, 1000)
		if err != nil {
			abort("list parts", err)
		}
		for _, p := range res.Parts {
			parts[p.PartNumber] = minio.CompletePart{PartNumber: p.PartNumber, ETag: p.ETag}
		}
		// 单页最多 1000 片；超过时必须翻页，否则已上传的分片会被当成「未上传」重复传
		if !res.IsTruncated {
			break
		}
		marker = res.NextPartNumberMarker
	}

	// 3) 并发补传缺失分片，每片重试 3 次
	jobs := make(chan int)
	var mu sync.Mutex
	var wg sync.WaitGroup
	for w := 0; w < workers; w++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for num := range jobs {
				off := int64(num-1) * partSize
				n := min(int64(partSize), size-off)
				r := io.NewSectionReader(f, off, n) // ReadAt 并发安全，可多 goroutine 共享
				var pi minio.PartInfo
				var perr error
				for i := 0; i < 3; i++ {
					r.Seek(0, io.SeekStart) // 重试只需回卷，不必重开文件
					if pi, perr = cli.PutObjectPart(ctx, bucket, object, uploadID, num,
						r, n, minio.PutObjectPartOptions{}); perr == nil {
						break
					}
				}
				if perr != nil {
					abort("upload part", fmt.Errorf("part %d: %w", num, perr))
				}
				mu.Lock()
				parts[num] = minio.CompletePart{PartNumber: pi.PartNumber, ETag: pi.ETag}
				mu.Unlock()
			}
		}()
	}
	for i := 1; i <= total; i++ {
		if _, ok := parts[i]; !ok { // 已传的分片直接跳过
			jobs <- i
		}
	}
	close(jobs)
	wg.Wait()

	// 4) 按 part number 升序提交，ETag 用服务端返回值
	list := make([]minio.CompletePart, 0, total)
	for i := 1; i <= total; i++ {
		if _, ok := parts[i]; !ok {
			abort("missing part", fmt.Errorf("part %d not uploaded", i))
		}
		list = append(list, parts[i])
	}
	info, err := cli.CompleteMultipartUpload(ctx, bucket, object, uploadID, list, minio.PutObjectOptions{})
	if err != nil {
		if minio.ToErrorResponse(err).Code == "NoSuchUpload" {
			return // 上一次其实已成功，幂等返回
		}
		abort("complete", err)
	}
	log.Printf("uploaded %s, %d bytes, etag=%s", object, info.Size, info.ETag)
}
```

几个容易踩的点：

- **part 顺序**：`CompleteMultipartUpload` 只认 part number 升序，乱序提交会被拒；但 `UploadPart` 本身可以完全乱序并发；
- **ETag 校验**：分片对象的 ETag 形如 `<md5>-<part 数>`，**不是整个文件的 MD5**，拿它和本地 md5sum 对比必然不等。要端到端校验，自己算全文件 MD5 塞进 `x-amz-meta-*` 元数据，下载后重算比对；
- **进程被杀 ≠ 上传失败**：这正是续传的价值——重启后 `ListIncompleteUploads` 找到同一个 `uploadId`，`ListParts` 跳过已传分片，只补缺失的；
- **显式放弃**：业务判定不再续传时立刻 `AbortMultipartUpload`，配合桶级 `AbortIncompleteMultipartUpload`（7 天）兜底，两件事都要做。

### 4.6 生命周期、对象锁与成本

ILM 规则既可以用 `mc ilm rule add` 逐条加，也可以直接导入一份 AWS lifecycle JSON（团队协作时更易 review）：

```bash
# 常用三条：转低频存储、过期删除、清理历史版本与孤儿分片
mc ilm rule add local/mybucket --prefix "logs/" \
  --transition-days 30 --transition-tier COLDTIER \
  --expire-days 180 --noncurrent-expire-days 30 \
  --expire-delete-marker

mc ilm import local/mybucket < lifecycle.json    # 等价于上面的声明式写法
mc ilm ls local/mybucket --json                  # 核对生效规则
```

```json
{
  "Rules": [
    {
      "ID": "logs-tier-then-expire",
      "Status": "Enabled",
      "Filter": { "Prefix": "logs/" },
      "Transitions": [{ "Days": 30, "StorageClass": "COLDTIER" }],
      "Expiration": { "Days": 180, "ExpiredObjectDeleteMarker": true },
      "NoncurrentVersionExpiration": { "NoncurrentDays": 30 },
      "AbortIncompleteMultipartUpload": { "DaysAfterInitiation": 7 }
    }
  ]
}
```

`StorageClass` 必须对应 `mc admin tier add` 注册过的远程层（S3/Azure/GCS），否则规则导入成功但转换静默失败。

**版本控制下的"删除"语义**（这是最容易出事故的一张表）：

| 操作 | 未开版本 | 已开版本 |
|---|---|---|
| `DELETE object` | 真删，空间释放 | 插入 delete marker，数据仍在 |
| `DELETE object?versionId=X` | 不适用 | 真删该版本，**不可恢复** |
| `GET object` | 正常 | 若最新是 delete marker 则 404 |
| `LIST` | 只看到现存对象 | 默认只列当前版本，`--versions` 才见全量 |
| 空间回收 | 删即回收 | 必须靠 `NoncurrentVersionExpiration` 或 `--expire-all-object-versions` |

**对象锁（WORM）**：开启版本控制是前提，之后可设保留期——`GOVERNANCE` 模式特权用户可提前解除，`COMPLIANCE` 模式到期前**任何人也删不掉**（含 root），选错模式等于把桶焊死。合规留痕（金融、医疗影像、审计日志）用 `COMPLIANCE`；防误删用 `GOVERNANCE` 更灵活。两者都能叠加 legal hold（无到期时间，需显式解除）。

```bash
mc retention set --default GOVERNANCE 30d local/mybucket/audit/
mc legalhold set local/mybucket/audit/2026-11.log
mc rm --incomplete --recursive --force local/mybucket/tmp/   # 清孤儿分片
```

**"看起来便宜实际很贵"的三个坑**：

| 坑 | 机制 | 对策 |
|---|---|---|
| 请求费 | 小文件多 → PUT/GET/LIST 次数爆炸，请求费可能超过存储费 | 合并小文件（打包成 tar/parquet）、客户端批量、减少 LIST 轮询 |
| 跨区/出口流量 | 跨 AZ 复制、公网下载、跨区读都单独计费 | 计算与存储同区、用预签名 URL 让客户端直连、CDN 回源收敛 |
| 小文件元数据 | 每对象有最小计费单元（IA 类常按 128 KB 起算）与元数据开销 | 别把 KB 级文件直接丢对象存储；<1 MB 的对象先聚合 |

再加一条常被忘的：低频/归档层的**取回费**与**最短存储期**——存 10 天就删，账单按 30/90 天算。分层之前先算清访问频率，别为了省存储费换来更贵的取回费。

### 4.7 迁移：从 MinIO 到其他 S3 实现

迁移按"先搬数据、再切流量、最后拆旧"三步走，任何时候都保留可回滚的旧集群。

```bash
# 0) 目标端别名（任何 S3 兼容实现都可以）
mc alias set target https://s3.new.example.com ACCESS_KEY SECRET_KEY

# 1) 首轮全量（在线，业务不停；--preserve 保留元数据与时间戳）
mc mirror --overwrite --preserve --retry local/mybucket target/mybucket

# 2) 增量追平（--watch 持续监听，直到切换窗口）
mc mirror --overwrite --preserve --watch --exclude "tmp/*" local/mybucket target/mybucket

# 3) 校验：对象数 + 抽样大小/ETag
mc ls --recursive --json local/mybucket  | wc -l
mc ls --recursive --json target/mybucket | wc -l
mc stat local/mybucket/db/full.dump  target/mybucket/db/full.dump
```

**双写过渡有两种做法**，选哪种取决于你能改多少代码：

- **应用层双写**：写成功一处再写另一处，读仍走旧集群。实现简单，但要处理"一处成功一处失败"的不一致（重试 + 对账任务），且延迟翻倍；
- **网关层双写**：在应用与存储之间放一个 S3 代理（或带 mirror 的 sidecar），应用只改 endpoint。对业务透明，代价是多一跳与一个需要高可用的组件。

**校验别只看 ETag**：不同实现对分片对象的 ETag 计算方式不同（MinIO 是 `md5-N`，Ceph 也类似但分片阈值不同），直接比对会大面积"不一致"。可靠做法是**比对象数 + 比 size + 抽样下载算 MD5**（优先比 `x-amz-meta-md5` 这类自定义元数据）。

**切换顺序与回滚**：

1. 进入窗口，暂停写入（或让双写只写旧集群）；
2. `mc mirror` 最后一轮追平，确认无 pending；
3. 改 DNS / 配置中心的 endpoint，灰度一批应用；
4. 观察错误率与 P99 延迟，稳定后再扩全量；
5. **旧集群转为只读并保留至少一个业务周期**，别急着下线。

回滚就是把 endpoint 改回去——这正是"旧集群只读保留"的意义。**迁移期的一致性边界要说清**：切换前写入只在一个集群，切换后也只在一个集群，中间靠 mirror 单向追平；不要双向 mirror，否则同一个 key 在两边被并发更新会产生互删/回滚的循环。

---

## 5. 与 S3 的兼容性说明

核心 API（bucket/object/acl/versioning/lifecycle/replication/encryption/STS/Select）高度兼容；受 S3 自身演进与 MinIO 商业策略影响，少数新 AWS 特性（如某些 S3 Express、Table 系特性）不在社区版。**AWS SDK（Java/Python/Go v2 等）与 aws cli 均可直接对 MinIO 使用**——只需改 endpoint。

---

## 6. 版本与产品线（2025–2026 重大变化）

| 产品 | 许可证 | 定位 |
|---|---|---|
| **MinIO Object Store（社区版）** | AGPLv3 | 开源主体，S3 兼容核心能力完整（EC、版本、复制、ILM、加密） |
| **MinIO AIStor（商业）** | 商业许可 | promptObject（LLM 问对象）、AIHub、S3-over-RDMA、Catalog/Firewall/KMS/Cache/全局控制台等企业特性 |

2025–2026 年的三步收缩（务必知悉）：

- **2025.05**：内置 Web Console 收缩为只读 object-browser；LDAP/OIDC 外部身份源登录移入商业版，社区版 UI 只用于浏览与基础操作（IAM 等经 mc/API 管理）；
- **2025.10**：停止发布社区版 Docker 镜像与预编译二进制，最后一个社区版本为 `RELEASE.2025-10-15T17-29-55Z`（安全修复版，需自行从源码构建镜像）；
- **2026.04**：`minio/minio` 仓库正式归档为只读，README 标注 "THIS REPOSITORY IS NO LONGER MAINTAINED"——不再有新特性、评审补丁或安全更新；
- 商业版 **AIStor** 继续演进；自建场景应评估 Garage、SeaweedFS、RustFS、Ceph RGW 等仍在维护的 S3 兼容替代，别把归档版本当作长期基础设施。
- **AGPLv3 的含义**：网络服务化使用也需开源派生作品，商业集成前评估合规；S3 API 兼容使替换/共存容易。

替代生态（选型时对照）：Ceph RGW（一体化存储平台、重）、SeaweedFS（轻量、性能好）、Garage（地理分布式轻量）、OpenIO/Scality 等。

---

## 7. 常见坑与最佳实践

1. **生产别用单节点**：EC 需要 ≥4 块盘；单盘模式无冗余，磁盘坏=数据丢；
2. **EC set 内磁盘要"独立"**：同 EC set 的盘挂同一 HBA/同一故障域，容错就是纸面数字——机架/主机感知布局；
3. **扩容按 pool，别混盘**：新 pool 建议整组节点加入；不同容量的盘混布会倾斜；
4. **时钟同步**：分布式部署对时间敏感（证书与复制），NTP 必备；
5. **大文件分片并行**：SDK 默认 part size 16MiB；超 GB 级对象调大 partSize/并发，吞吐翻倍；
6. **别拿 MinIO 当文件系统**：高频小文件随机改写是反模式；真正需要 POSIX 挂载时评估 S3-FS 网关的延迟代价；
7. **监控磁盘与 healing**：`mc admin heal`、Prometheus 指标（磁盘离线、healing 队列）告警必须有；
8. **先确认版本是否还在维护**：社区版已于 2026-04 归档，最后版本为 `RELEASE.2025-10-15`；新部署请直接选仍维护的替代，存量升级前备份数据目录元信息。

---

## 8. 选型小结

| 场景 | 建议 |
|---|---|
| K8s/云原生应用附件、备份（Velero） | MinIO 存量集群可继续用；**新部署选 Garage / SeaweedFS / Ceph RGW** |
| 数据湖 / Parquet / DuckDB-Trino 查询底座 | MinIO 存量 / AIStor；新项目评估 SeaweedFS、RustFS |
| AI 训练数据集分发 | AIStor（商业支持）；自建评估 RustFS / SeaweedFS |
| 超大规模多协议（块/文件/对象一体） | Ceph |
| 极轻量边缘 | SeaweedFS / Garage |
| 新自建对象存储（无历史包袱） | **Garage**（轻量、地理分布）/ SeaweedFS / Ceph RGW |
