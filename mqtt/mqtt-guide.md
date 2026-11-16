# MQTT / EMQX 技术指南

> 面向海量设备与弱网链路的发布订阅协议：主题寻址、三级 QoS、会话与遗嘱消息，专为 IoT 设计而不是通用消息队列。
> 服务端：<https://github.com/emqx/emqx> **6.x**（6.3 主线，5.10 维护中；5.9 起 BSL 1.1，5.8 及以前 Apache-2.0）　|　客户端：<https://github.com/eclipse/paho.mqtt.golang> v1.x · <https://github.com/mochi-mqtt/server> v2　|　协议：MQTT 3.1.1 / 5.0

---

## 1. 项目简介

MQTT（MQ Telemetry Transport）1999 年由 Andy Stanford-Clark 与 Arlen Nipper 为**卫星链路遥测**设计，2014 年 3.1.1 成为 OASIS 标准，2019 年发布 5.0。它解决的是 HTTP 解决不了的问题：单 broker 承载百万级长连接、最小报文头仅 2 字节、长连接 + 心跳避免反复建连、订阅模型让云端可主动下发指令、QoS 与会话缓存应对不可靠链路。

### 与 HTTP 轮询的差别

| 维度 | HTTP 轮询 | MQTT 长连接 |
|---|---|---|
| 交互方向 | 只能客户端发起 | 双向，服务端可主动推送 |
| 实时性 | 取决于轮询间隔（秒级～分钟级） | 毫秒级 |
| 单次开销 | 每次完整请求头（数百字节～KB） | 固定头 2 字节起 |
| 服务端成本 | 空闲设备也在消耗 QPS | 空闲连接只占内存与 fd |
| 弱网表现 | 超时重试放大流量 | 心跳 + 会话续传 |
| 移动端耗电 | 频繁唤醒射频 | 一条 TCP 常驻，可休眠 |

### MQTT 不承担什么

**MQTT 是设备接入协议（edge protocol），不是数据管道**。broker 是连接终结者：维护会话、做主题路由、把消息推给在线订阅者；它**不擅长长期存储与回放**——离线消息只在会话有效期内排队，没有按时间/大小保留的日志，也没有 offset。所以生产架构通常是 **设备 ── MQTT ──► EMQX ──桥接──► Kafka / TSDB / 业务服务**：EMQX 负责接入，Kafka 负责回放与再消费。

### 快速启动

```bash
docker run -d --name emqx -p 1883:1883 -p 8083:8083 -p 8883:8883 -p 18083:18083 emqx/emqx:6.3.2
# 1883 MQTT / 8883 TLS / 8083 WebSocket / 18083 Dashboard(http://localhost:18083, admin/public)
# 6.x 为 BSL 1.1，单节点生产免费；需要 Apache-2.0 时用 emqx/emqx:5.8.9（已停止维护）
```

---

## 2. 能力总览

| 能力域 | 具体内容 |
|---|---|
| 连接 | TCP / TLS(8883) / WebSocket / WSS / QUIC(EMQX 5.x)；用户名密码、JWT、X.509 双向认证、PSK |
| 消息模型 | 主题发布订阅、`+`/`#` 通配符、共享订阅 `$share/`、请求-响应（5.0） |
| 交付语义 | QoS 0 至多一次 / QoS 1 至少一次 / QoS 2 恰好一次（逐跳） |
| 会话 | Clean Session(3.1.1) / Clean Start + Session Expiry(5.0)、离线消息排队、inflight 窗口 |
| 保留与遗嘱 | retained 主题快照；Last Will 异常断线代发，5.0 支持 Will Delay |
| 扩展特性 | 主题别名、用户属性、原因码、消息过期、订阅标识符、Receive Maximum 流控 |
| EMQX 平台与可观测 | 集群（core/replicant）、会话持久化、规则引擎 SQL、数据桥接（Kafka/Pulsar/SQL/TSDB）、Dashboard、REST API、`$SYS/` 主题、Prometheus 指标 |

---

## 3. 核心概念与原理

### 3.1 报文类型与一次连接的完整生命周期

MQTT 3.1.1 有 14 种控制报文，5.0 新增 `AUTH` 共 15 种。固定头是 1 字节（4 bit 类型 + 4 bit 标志）加 1~4 字节的变长剩余长度（最短报文头 2 字节）：

| 报文 | 方向 | 作用 |
|---|---|---|
| `CONNECT` / `CONNACK` | C→S / S→C | 建连：协议版本、ClientID、Clean Start、KeepAlive、遗嘱、用户名密码 |
| `PUBLISH` | 双向 | 发布消息，携带 QoS、retain、topic、packet id（QoS>0） |
| `PUBACK` | 双向 | QoS 1 确认 |
| `PUBREC` / `PUBREL` / `PUBCOMP` | 双向 | QoS 2 四步握手 |
| `SUBSCRIBE` / `SUBACK` | C→S / S→C | 订阅（可一次多个过滤器），SUBACK 返回每个过滤器的授予 QoS |
| `UNSUBSCRIBE` / `UNSUBACK` | C→S / S→C | 取消订阅 |
| `PINGREQ` / `PINGRESP` | C→S / S→C | 心跳保活 |
| `DISCONNECT` | 双向 | 正常断开；5.0 可携带原因码与会话过期覆盖值 |
| `AUTH` | 双向 | 5.0 增强认证（多轮挑战应答） |

```text
CONNECT ──► CONNACK（session present = 0/1）
   ├─ SUBSCRIBE(devices/+/cmd, qos1) ──► SUBACK(0x01)
   ├─ PUBLISH(qos1, pid=10) ──► PUBACK(pid=10)
   ├─ PINGREQ ──► PINGRESP      （每 KeepAlive 周期一次）
   └─ DISCONNECT(0x00)
```

**session present** 是重连时最该看的一位：为 1 表示 broker 复用了旧会话（订阅还在、离线消息会补发），为 0 表示会话是新建的——客户端必须重新 SUBSCRIBE 并考虑补拉。

### 3.2 主题树、通配符与共享订阅

主题是 `/` 分隔的层级字符串，**大小写敏感**，不以 `/` 开头也合法（`/a` 与 `a` 是两个不同主题）。broker 内部用 Trie 存储订阅，匹配复杂度与层级深度相关。

```text
home/floor1/room2/temperature     精确
home/+/room2/temperature          + 匹配恰好一层：floor1、floor2 都命中
home/floor1/#                     # 匹配剩余所有层，含父层 home/floor1 本身
$share/analytics/home/+/temp      共享订阅：组 analytics 内只有一个成员收到
```

- `+`、`#` 必须独占一层：`home/floor+/temp` 非法；`#` 只能是最后一段；
- 以 `$` 开头的主题（`$SYS/`、`$share/`）不被 `#` 或 `+` 匹配，避免误订阅系统主题；
- **共享订阅** `$share/<group>/<filter>` 是 broker 侧扩展（EMQX、HiveMQ、Mosquitto 等各自约定，3.1.1 与 5.0 都未纳入规范），同组内负载均衡、跨组广播——这是 MQTT 做"消费组"的唯一方式，也是订阅端水平扩容的关键；
- 实际投递 QoS 取 `min(发布 QoS, 授予 QoS)`。

### 3.3 QoS 三级握手与投递语义

```text
QoS 0：PUBLISH ───────────────────────────────►            至多一次，可能丢
QoS 1：PUBLISH(pid) ──►  ◄── PUBACK(pid)                   至少一次，可能重复
QoS 2：PUBLISH(pid) ──►  ◄── PUBREC(pid)
       PUBREL(pid)  ──►  ◄── PUBCOMP(pid)                  恰好一次，2 个往返
```

- **QoS 0**：fire-and-forget，不占 packet id，不过会话队列。适合高频遥测（1s 一次的温度），丢一条无所谓。
- **QoS 1**：接收方回 PUBACK 即删；发送方未收到 PUBACK 会重发（DUP=1）。**重复是常态**，消费端必须幂等（按 message id / 业务主键 upsert）。
- **QoS 2**：PUBREC/PUBREL/PUBCOMP 四步，接收方用 packet id 去重，保证"不重复"。

**QoS 2 的真实成本**：报文量翻 4 倍、需持久化 packet id 状态、每条消息等 2 个 RTT 才释放 inflight 槽位，broker 还要为每个客户端维护去重表。10 万设备 × 每秒 1 条时这是可观的 CPU 与内存开销。**绝大多数 IoT 场景 QoS 1 + 幂等就够了**；QoS 2 只用于"重复会造成真实损失"的控制指令（计费扣款、阀门动作）。

还要记住：**QoS 2 是逐跳（hop-by-hop）语义**。设备→broker 恰好一次，不代表 broker→订阅者、更不代表桥接到 Kafka 之后仍恰好一次；端到端恰好一次必须靠业务幂等或事务性 sink。

### 3.4 会话状态：Clean Session / Clean Start 与 Session Expiry

会话（Session）包含四类状态：订阅列表、未确认的 QoS1/2 消息（inflight）、排队中的离线消息、以及（5.0）未完成的接收流控配额。

| 版本 | 字段 | 语义 |
|---|---|---|
| 3.1.1 | Clean Session = 1 | 每次连接都是新会话，断开即丢弃全部状态（多数客户端的默认，最省 broker 内存） |
| 3.1.1 | Clean Session = 0 | 持久会话：断线后订阅保留、QoS1/2 消息排队，直到客户端以同 ClientID 重连 |
| 5.0 | Clean Start = 1 | 丢弃旧会话，从零开始（等价旧 clean session=1） |
| 5.0 | Clean Start = 0 + Session Expiry Interval = N | 断线后会话保留 N 秒，N=0 立即删除，`0xFFFFFFFF` 永不过期 |
| 5.0 | DISCONNECT 携带 Session Expiry | 连接结束时动态覆盖建连时声明的过期时间 |

关键差别：**3.1.1 的持久会话没有过期时间**——只要客户端不回来，broker 就得一直替它攒消息，这是"内存被离线会话打爆"最常见的根因。5.0 用 Session Expiry 给出回收边界，还能在 DISCONNECT 时改成 0 立即释放。

选型：状态上报类设备用 Clean Start=1（无状态，重连重新订阅即可）；只有"必须收到下发指令"的设备才开持久会话，且**必须设 Session Expiry**（如 1 小时）+ 队列上限（EMQX `max_mqueue_len`）。

### 3.5 Retained 消息与遗嘱消息

**Retained（保留消息）**：发布时 retain=1，broker 为该主题保存**最后一条**消息；之后任何新订阅者订阅该主题，立刻收到这条快照（retain 标志置 1）。向同一主题发空 payload 即可清除。它解决"订阅者来晚了看不到当前状态"的问题，是设备状态、配置下发的标准做法。

风险：**保留消息没有天然 TTL（3.1.1）**，一条三年前的状态会一直发给每个新订阅者；主题过细（`devices/{id}/state` × 百万设备）等于在 broker 里放一百万条常驻数据。5.0 的 Message Expiry 可给保留消息设过期，3.1.1 只能靠运维定期清理。

**Last Will（遗嘱消息）**：建连时在 CONNECT 里声明（topic、payload、QoS、retain）。当 broker 检测到**异常断开**——TCP 断开、KeepAlive 超时、协议错误、客户端被踢——时代为发布这条消息。

- 正常 `DISCONNECT` **不会**触发遗嘱（5.0 可用原因码 `0x04 Disconnect with Will Message` 显式要求发送）；
- 5.0 的 **Will Delay Interval** 让 broker 延迟 N 秒再发遗嘱，给"短暂断网后迅速重连"留出窗口，避免抖动误报离线；
- 遗嘱 + retained 是设备在线状态的经典实现：上线发 `{"online":true}`（retain=1），遗嘱写 `{"online":false}`（retain=1），任何时刻订阅 `devices/+/state` 都能拿到正确状态。

风险：遗嘱主题若允许设备任意指定，恶意设备可以给别人的主题发遗嘱。生产上要**在 ACL 里限制 will topic 只能是本设备命名空间**。

### 3.6 保活、断线重连与 5.0 的请求-响应

**KeepAlive** 是客户端在 CONNECT 里声明的秒数：客户端承诺"空闲时每 KeepAlive 秒发一次 PINGREQ"，broker 在 **1.5 × KeepAlive** 内没收到任何报文就判定连接死亡、触发遗嘱、释放连接。设太大（>300s）会被 NAT/防火墙的表项空闲超时（常见 60–350s）先静默丢连接，双方都以为还活着；设太小（<10s）则心跳本身成为负载。经验值 **30–60s**，且必须小于链路上最短的 NAT idle timeout。

**重连**必须带抖动的指数退避：`1s, 2s, 4s ... 上限 30s`，加 ±20% 随机。否则 broker 重启后百万设备在同一秒重连（thundering herd），把刚起来的集群再打挂。paho 的 `SetAutoReconnect` + `SetMaxReconnectInterval` 已内置退避，但**重连成功后订阅不会自动恢复**（会话被清掉时），必须在 `OnConnectHandler` 里重新订阅。

**5.0 带来的四件实用武器**：

1. **原因码**：CONNACK/PUBACK/DISCONNECT 都带 1 字节原因码（`0x87 Not authorized`、`0x97 Quota exceeded`），排障不再靠猜；
2. **用户属性**：每报文可带任意 KV 对，等价于可编程 header（trace id、型号、固件版本），不必污染 payload；
3. **主题别名**：连接内先用一次完整主题 + alias id，后续只发 2 字节别名，长主题省下大量带宽；
4. **请求-响应**：`Response Topic` + `Correlation Data` 两个属性，让 MQTT 原生支持 RPC 语义，不必自己拼 reply 主题。

---

## 4. 使用实战（Go：paho.mqtt.golang v1.5）

```bash
go get github.com/eclipse/paho.mqtt.golang   # v1.5.1，仅 MQTT 3.1.1；5.0 用 github.com/eclipse/paho.golang
```

### 4.1 连接选项：TLS、ClientID、KeepAlive、自动重连

```go
package main

import (
	"crypto/tls"
	"crypto/x509"
	"fmt"
	"os"
	"os/signal"
	"syscall"
	"time"

	mqtt "github.com/eclipse/paho.mqtt.golang"
)

func newClient(clientID string) (mqtt.Client, error) {
	caPEM, err := os.ReadFile("certs/ca.crt")
	if err != nil {
		return nil, fmt.Errorf("read ca: %w", err)
	}
	pool := x509.NewCertPool()
	pool.AppendCertsFromPEM(caPEM) // 生产环境应检查返回值，失败即拒绝启动

	opts := mqtt.NewClientOptions()
	opts.AddBroker("ssl://broker.example.com:8883") // 1883 明文 / 8083 ws / 8084 wss
	opts.SetClientID(clientID)                     // 必须全局唯一，重复会互踢
	opts.SetUsername("device-001")
	opts.SetPassword("s3cret")
	opts.SetKeepAlive(30 * time.Second)  // 小于链路上最短 NAT 空闲超时
	opts.SetPingTimeout(5 * time.Second) // 等 PINGRESP 的上限
	opts.SetConnectTimeout(5 * time.Second)
	opts.SetCleanSession(false) // 持久会话：断线期间 QoS1 消息排队
	opts.SetAutoReconnect(true)
	opts.SetMaxReconnectInterval(30 * time.Second) // 指数退避上限
	opts.SetTLSConfig(&tls.Config{
		RootCAs:    pool,
		ServerName: "broker.example.com",
		MinVersion: tls.VersionTLS12,
	})

	// 每次（重）连成功都会回调：订阅必须在这里重建
	opts.SetOnConnectHandler(func(c mqtt.Client) {
		if err := subscribe(c); err != nil {
			fmt.Println("resubscribe failed:", err)
		}
	})
	opts.SetConnectionLostHandler(func(_ mqtt.Client, err error) {
		fmt.Println("connection lost:", err) // 此后库会自行退避重连
	})

	c := mqtt.NewClient(opts)
	tok := c.Connect()
	if !tok.WaitTimeout(10 * time.Second) {
		return nil, fmt.Errorf("connect timeout")
	}
	return c, tok.Error()
}
```

要点：`SetCleanSession(false)` 与 `SetClientID` 必须配对——同 ClientID 的第二个连接会让第一个被 broker 踢掉（"随机掉线"的头号原因）；TLS 场景下 `ServerName` 必须与证书 SAN 一致，否则校验失败。

### 4.2 订阅回调与 QoS 1 发布

```go
func subscribe(c mqtt.Client) error {
	tok := c.Subscribe("devices/001/cmd", 1, func(_ mqtt.Client, m mqtt.Message) {
		// 回调在库的投递 goroutine 中执行：不要阻塞，重活丢给 channel/worker
		fmt.Printf("cmd topic=%s qos=%d payload=%s\n", m.Topic(), m.Qos(), m.Payload())
	})
	tok.WaitTimeout(5 * time.Second)
	return tok.Error()
}

func publish(c mqtt.Client, payload []byte) error {
	tok := c.Publish("devices/001/telemetry", 1, false, payload) // qos=1, retained=false
	if !tok.WaitTimeout(3 * time.Second) {
		return fmt.Errorf("publish timeout")
	}
	return tok.Error()
}
```

注意 paho v3 语义：**回调返回即 PUBACK**，业务失败不会触发重投。要"至少一次"地处理业务，得自己在回调里落盘/入库后再算成功，或改用 5.0 客户端手动 ack（paho.golang 的 `Message.Ack()`）。

### 4.3 完整 main：优雅断开与信号处理

```go
func main() {
	c, err := newClient("gw-001")
	if err != nil {
		panic(err)
	}
	defer c.Disconnect(250) // quiesce 250ms：等在途报文发完再断

	if err := publish(c, []byte(`{"temp":21.5,"ts":1763300000}`)); err != nil {
		fmt.Println("publish:", err)
	}

	sig := make(chan os.Signal, 1)
	signal.Notify(sig, syscall.SIGINT, syscall.SIGTERM)
	<-sig
}
```

`Disconnect(quiesce)` 与 `Close()` 的区别：前者发 DISCONNECT 并等在途 QoS 消息完成，后者是硬断——硬断会让 broker 认为异常、触发遗嘱，线上表现为"服务优雅重启却刷出一批设备离线告警"。

### 4.4 设备上下线：遗嘱 + retained 状态

```go
func newDeviceClient(id string) (mqtt.Client, error) {
	stateTopic := "devices/" + id + "/state"
	opts := mqtt.NewClientOptions()
	opts.AddBroker("ssl://broker.example.com:8883")
	opts.SetClientID(id)
	opts.SetKeepAlive(30 * time.Second)
	opts.SetCleanSession(true) // 状态类设备不需要离线队列
	opts.SetAutoReconnect(true)
	// 异常断线时由 broker 代发：retained 保证新订阅者也能读到"离线"
	opts.SetWill(stateTopic, `{"online":false}`, 1, true)

	c := mqtt.NewClient(opts)
	if tok := c.Connect(); tok.Wait() && tok.Error() != nil {
		return nil, tok.Error()
	}
	// 上线：覆盖遗嘱留下的离线快照
	c.Publish(stateTopic, 1, true, []byte(`{"online":true}`)).Wait()
	return c, nil
}
```

订阅端只需 `Subscribe("devices/+/state", 1, ...)`，无论启动时还是设备上线后都能立即拿到当前状态——这就是 retained + will 的价值。遗嘱的 QoS 与 retain 必须在 CONNECT 时定好，运行期不能改。

### 4.5 EMQX 侧运维

```bash
# 集群/连接概况（REST API v5；默认账号 admin/public）
curl -s -u admin:public http://localhost:18083/api/v5/nodes | jq '.[] | {node,connections,load1}'
curl -s -u admin:public 'http://localhost:18083/api/v5/clients?limit=10' | jq '.data[] | {clientid,clean_start}'

# 发一条保留消息，再看本地 CLI
curl -s -u admin:public -X POST http://localhost:18083/api/v5/publish \
  -H 'Content-Type: application/json' \
  -d '{"topic":"devices/001/state","qos":1,"retain":true,"payload":"{\"online\":true}"}'
emqx ctl status && emqx ctl clients list && emqx ctl retainer clean
```

Prometheus 抓取 `http://<node>:18083/api/v5/prometheus/stats`，重点看 `emqx_connections_count`、`emqx_sessions_count`、`emqx_messages_dropped`、`emqx_session_mqueue_len`。

---

## 5. 版本演进（MQTT 5.0 与 EMQX 5.x / 6.x）

### MQTT 协议

| 版本 | 时间 | 关键变化 |
|---|---|---|
| 3.1 | 1999/2010 | 原始版本，ClientID 限 23 字节 |
| 3.1.1 | 2014（OASIS；ISO/IEC 20922:2016） | 明确会话/遗嘱/错误处理，事实标准 |
| 5.0 | 2019（OASIS Standard） | 原因码、用户属性、主题别名、会话与消息过期、请求-响应、Receive Maximum 流控、Will Delay、AUTH 增强认证、服务器主动 DISCONNECT |

5.0 的落地建议：**新项目直接用 5.0**，尤其需要原因码排障、会话过期回收、请求-响应的地方；但要注意客户端库分叉——`paho.mqtt.golang`（v1.x）只做 3.1.1，5.0 要用 `github.com/eclipse/paho.golang`。混合集群里 broker 会自动降级：给 3.1.1 客户端投递时丢弃用户属性等 5.0 特性。

### EMQX 服务端

| 版本 | 时间 | 说明 |
|---|---|---|
| 5.8 | 2024-08 | 最后一个 Apache-2.0 系列（LTS，EOL 2027-08），社区使用最广 |
| 5.9 | 2025-05 | 许可转为 BSL 1.1：单节点生产免费，多节点集群需商业 License |
| 5.10 | 2025-06 → 5.10.4（2026-06） | 5.x 末版，进入维护阶段 |
| 6.0 | 2025-09-30 | 新主线（面向 AI/数据集成），架构与 5.x 延续 |
| 6.3 | 2026-10 | 当前主线（6.3.2，截至 2026-11） |

两个必须知道的迁移事实：

1. **5.x → 6.x 持久会话会丢失**：官方滚动升级文档明确说明，升级后旧节点上的会话不再可用，客户端重连时表现为全新（clean）会话——依赖离线队列的设备要在升级窗口内主动重连并重建会话；
2. **许可边界**：5.8 及以前是 Apache-2.0；5.9 起为 BSL 1.1，源码可见、单节点生产可用、四年后转 Apache-2.0，但**集群部署需要商业 License**。选型时把这条算进成本，而不是假设"开源即免费"。

EMQX 5.x 的架构要点：节点分 **core**（承担路由与会话，需奇数个做一致性）与 **replicant**（无状态接入层，可弹性扩缩）；集群发现支持 static/dns/etcd/kubernetes；**会话持久化**（durable session）把会话与消息落盘，节点重启不丢；**规则引擎**用 SQL 抽取、过滤、富化消息，再经**数据桥接**写入 Kafka/Pulsar/MySQL/TimescaleDB/HTTP；Dashboard(18083) 提供连接、主题、慢订阅、丢弃统计。

---

## 6. 常见坑与最佳实践

1. **ClientID 冲突互踢**：两台设备用同一个 ClientID（或都用了默认随机值撞车），broker 只保留后连的那条，前一条被静默断开，表现为"连接随机丢失"。ClientID 用设备唯一标识（SN/IMEI），服务端限制 `max_clientid_len` 并禁止空 ClientID。
2. **QoS 2 滥用**：以为"QoS 2 = 端到端精确一次"就给所有消息上 QoS 2，结果吞吐掉到 1/4、inflight 长期占满。遥测用 QoS 0/1，控制指令用 QoS 1 + 业务幂等键，只有极少数不可重复的动作才用 QoS 2。
3. **retained 消息永久残留**：三年前的设备状态一直发给新订阅者，已删除的设备还在"在线"。3.1.1 没有 TTL，必须靠运维清理（`emqx ctl retainer clean`）或改用 5.0 的 Message Expiry；约定"retain 只用于状态类主题，不用于事件流"。
4. **通配符订阅放大**：一个后端服务订阅 `#` 想"收全量"，结果百万设备的每条消息都复制一份给它，单连接带宽打满还拖慢整个 broker 的匹配。订阅要收敛到具体层级（`tenant/+/telemetry`），跨租户统计走规则引擎或桥接到 Kafka 再算。
5. **离线会话堆积打爆内存**：3.1.1 下 `clean session=0` 且不设上限，设备离线三个月，broker 替它攒了几十万条 QoS1 消息。必须设 `max_mqueue_len`（如 1000）、`max_inflight`，5.0 再配 Session Expiry；只上报的设备直接 clean session=1。
6. **KeepAlive 大于 NAT 超时**：客户端设 300s，云上 LB/NAT 60s 就清表项，连接变成"半开"——双方都以为在线，消息全丢，直到下次发报文才发现。KeepAlive 取 30–60s，并用业务心跳（如 5 分钟一次上报）兜底。
7. **重连没有退避**：broker 重启后百万设备同时重连，TLS 握手 + 认证把 CPU 打满，集群起不来。客户端必须指数退避 + 抖动；服务端配 `max_conn_rate` 限流，并接受"重连风暴需要几分钟消化"。
8. **遗嘱误报离线**：地铁隧道断网 10 秒就发遗嘱 + retained，监控里设备疯狂上下线。5.0 用 Will Delay（如 30s）过滤抖动；3.1.1 只能在业务侧去抖（连续 N 次离线才告警）。
9. **重连后忘了重新订阅**：`SetAutoReconnect` 只恢复 TCP 连接，Clean Session=1 时 broker 端订阅已清空，客户端却以为还在收消息。所有订阅逻辑必须放在 `OnConnectHandler` 里，且要幂等。
10. **持久会话与共享订阅混用**：共享订阅成员掉线后，其会话里排队的消息可能永久卡住（没有其他成员接手）。共享订阅组应全部使用 clean session=1，让未确认消息回到组内其他成员。
11. **监控只看连接数**：连接数正常但业务已挂——要看**消息速率**、**丢弃数**（`emqx_messages_dropped`，通常是队列满或 ACL 拒绝）、**会话队列长度**、**inflight 饱和度**、**慢订阅**。丢弃率是最灵敏的故障信号。

---

## 7. 选型对比

### Broker

| 维度 | EMQX | Mosquitto | NanoMQ | mochi-mqtt |
|---|---|---|---|---|
| 实现 | Erlang/OTP | C | C（NNG） | Go |
| 集群 | 原生（core/replicant，5.9+ 需商业 License） | 无（需自建桥接） | 有限（边缘为主） | 无（roadmap） |
| 单机连接量级 | 百万级 | 十万级 | 十万级 | 十万级 |
| MQTT 5.0 | 完整 | 完整 | 完整 | 完整 |
| 规则引擎/桥接 | 内置（SQL + 数十种 sink） | 无（靠外部） | 有限（QUIC、边缘转发） | 无（Hook 自研） |
| 运维复杂度 | 中（Dashboard/REST 完善） | 低（单进程） | 低 | 极低（可嵌入进程） |
| 许可 | 5.8 及以前 Apache-2.0；5.9+ BSL 1.1 | EPL-2.0 | Apache-2.0 | MIT |

一句话：**大规模 IoT 平台选 EMQX；单机/边缘小规模选 Mosquitto 或 NanoMQ；Go 服务里想内嵌 broker（测试、边缘网关、单机产品）选 mochi-mqtt**——它是库不是集群产品。

### 协议

| 维度 | MQTT | Kafka | NATS | HTTP 轮询 |
|---|---|---|---|---|
| 定位 | 设备接入 | 事件流存储/回放 | 云原生消息总线 | 请求-响应 |
| 连接规模 | 百万级长连接 | 数百客户端（连接重） | 十万级 | 受 QPS 限制 |
| 消息保留 | 仅会话期内排队 | 按策略长期保留、可重放 | Core 无 / JetStream 有 | 无 |
| 投递语义 | QoS 0/1/2（逐跳） | at-least/exactly-once | at-most/at-least | 至多一次（靠重试） |
| 服务端推送 | 原生 | 需消费组拉取 | 原生 | 不支持（需 SSE/WS） |
| 弱网适应 | 强（心跳 + 会话续传） | 弱 | 中 | 差 |

边界总结：**MQTT 负责把百万设备接进来，Kafka 负责把数据存下来并重放给多个下游，NATS 负责服务之间的低延迟总线**。三者不是替代关系而是分层：设备 → EMQX →（桥接）→ Kafka → 流处理/数仓；内部微服务间再用 NATS/gRPC。若只有几千台设备且不做历史回放，EMQX + 规则引擎直写 TSDB 就够了，不必上 Kafka。

参考：MQTT 5.0 规范 <https://docs.oasis-open.org/mqtt/mqtt/v5.0/mqtt-v5.0.html>；EMQX 文档 <https://docs.emqx.com/zh/emqx/latest/>；paho.mqtt.golang <https://github.com/eclipse/paho.mqtt.golang>（v1.5.1，2025-09，EPL-2.0）；EMQX 许可说明 <https://www.emqx.com/zh/content/license-faq>（截至 2026-11）。
