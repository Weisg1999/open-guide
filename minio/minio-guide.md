# MinIO 技术介绍与使用指南

> 高性能 S3 兼容对象存储：单二进制、纠删码数据保护、云原生与 AI 数据湖首选的自托管对象存储。
> 仓库：<https://github.com/minio/minio>　|　主线：社区版（`RELEASE.YYYY-MM-DD` 时间戳版本，持续发布；整理于 2026-11）　|　许可证：AGPLv3（2025 起双产品线，见 §6）

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

```
Bucket（桶，顶层命名空间）
  └── Object（对象 = 数据 + 元数据）
        ├── key:   photos/2026/alice.png   （"路径"只是 key 的字符串形态）
        ├── data:  任意字节流（KB ~ TB 级）
        └── meta:  Content-Type、ETag、自定义 x-amz-* 元数据
```

与传统文件系统的区别：**没有真正的目录层级**（key 是平面字符串）、**不擅长修改**（对象不可变，覆盖即新版本）、**强项是海量 + 一次性读写**。

### 2.2 纠删码（Erasure Coding）：MinIO 的灵魂

传统三副本存储存 1TB 要花 3TB；MinIO 用 Reed-Solomon 纠删码把对象切成 **N 个数据块 + M 个校验块**，分散到独立磁盘：

```
对象 (1MB) ──EC 编码──► [D1][D2][D3][D4] [P1][P2]   （EC:4+2，共 6 块 6 盘）
                         └── 任意坏 2 块，仍可完整还原 ──┘
```

- 读写比例可配：`EC:4+2`（1.5x 空间开销，容忍 2 盘坏）、`EC:8+4`（1.5x）、`EC:8+8`（2x，容忍一半盘坏）；
- **读写 quorum**：写需 ≥ N/2 块盘可用；读只需任意 N 块；
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

---

## 3. 架构与数据路径

```
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
mc ilm add --expiry-days 90 local/mybucket # 生命周期
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

---

## 5. 与 S3 的兼容性说明

核心 API（bucket/object/acl/versioning/lifecycle/replication/encryption/STS/Select）高度兼容；受 S3 自身演进与 MinIO 商业策略影响，少数新 AWS 特性（如某些 S3 Express、Table 系特性）不在社区版。**AWS SDK（Java/Python/Go v2 等）与 aws cli 均可直接对 MinIO 使用**——只需改 endpoint。

---

## 6. 版本与产品线（2025 起重要变化）

| 产品 | 许可证 | 定位 |
|---|---|---|
| **MinIO Object Store（社区版）** | AGPLv3 | 开源主体，S3 兼容核心能力完整（EC、版本、复制、ILM、加密） |
| **MinIO AIStor（商业）** | 商业许可 | promptObject（LLM 问对象）、AIHub、S3-over-RDMA、Catalog/Firewall/KMS/Cache/全局控制台等企业特性 |

社区版 2025 年的调整（务必知悉）：

- **内置 Web Console 收缩为只读 object-browser**；LDAP/OIDC 外部身份源登录移入商业版，社区版 UI 主要用于浏览与基础操作（IAM 等经 mc/API 管理）；
- 功能迭代重心转向商业版；社区版继续接收安全与缺陷修复（例如 2025.10 修复的 service-account 权限提升 CVE，社区版同步可用）；
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
8. **升级前读 release note**：社区版个别版本标记 Breaking（如 2025-05-24），滚动升级前备份数据目录元信息。

---

## 8. 选型小结

| 场景 | 建议 |
|---|---|
| K8s/云原生应用附件、备份（Velero） | MinIO 社区版，几乎默认选择 |
| 数据湖 / Parquet / DuckDB-Trino 查询底座 | MinIO（S3 协议 + 高吞吐） |
| AI 训练数据集分发 | MinIO / AIStor（看重 promptObject 等则商业版） |
| 超大规模多协议（块/文件/对象一体） | Ceph |
| 极轻量边缘 | SeaweedFS / Garage |
