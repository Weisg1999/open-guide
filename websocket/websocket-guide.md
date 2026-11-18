# gorilla/websocket 技术指南

> WebSocket 不是「长连接版 HTTP」：一次握手升级后就是全双工帧协议，并发模型、心跳、背压都得自己定规矩。
> 上游：<https://github.com/gorilla/websocket> **v1.5.3**（维护模式）· 替代：<https://github.com/coder/websocket>　|　协议：RFC 6455　|　许可：BSD-2-Clause

---

## 1. 项目简介

gorilla/websocket 是 Go 生态使用最广的 WebSocket 实现（GitHub 24.9k+ star），几乎所有 Go Web 框架的 WebSocket 集成、绝大多数中文 IM/推送教程，底层都是它。它只做一件事：把 RFC 6455 的握手与帧编解码实现成 `net/http` 之上的 `*websocket.Conn`——消息路由、房间、广播、心跳策略、重连，全部留给使用者。

维护状态要准确理解，别被「归档」两个字误导：

- 2022 年 12 月 gorilla 组织整体归档，项目一度停更；2023 年起社区接手，v1.5.1 / v1.5.2 / v1.5.3 陆续发布；
- **当前仓库未归档，但处于维护模式**：只修严重 bug 与安全类问题，不再加新特性，最后一次提交是 2025 年 3 月的 HTTPS proxy 支持；
- API 自 v1.5 起保持稳定，模块声明 `go 1.12`，可以在任何现代 Go 版本上编译；
- 官方与社区的一致建议：**存量项目继续用 gorilla，新项目优先评估 coder/websocket**（原 nhooyr.io/websocket，2024 年由 Coder 接管，v1.8.15，零依赖、全 context、并发写安全）。

```bash
go get github.com/gorilla/websocket@v1.5.3
```

## 2. 能力总览

| 能力 | 说明 |
|---|---|
| 服务端升级 | `Upgrader.Upgrade(w, r, nil)` 完成 HTTP/1.1 → WebSocket，返回 `*Conn` |
| 客户端拨号 | `Dialer.Dial` / `Dialer.DialContext`，支持自定义 Header、代理、TLS、子协议 |
| 消息读写 | `ReadMessage`/`WriteMessage`、`ReadJSON`/`WriteJSON`、`NextReader`/`NextWriter`（流式分片） |
| 帧级控制 | `WriteControl` 可直接发 ping/pong/close，且**可与其他写方法并发** |
| 心跳 | 默认 ping handler 自动回 pong；`SetPingHandler`/`SetPongHandler` 可接管（常用于重置读 deadline） |
| deadline | `SetReadDeadline`/`SetWriteDeadline`，库不做任何默认超时 |
| 消息上限 | `SetReadLimit`，超限时以 1009 关闭，防单包打爆内存 |
| 关闭语义 | `CloseMessage` + 状态码；`Close()` 只关 TCP，不发关闭帧 |
| 压缩 | `Upgrader.EnableCompression` + `Conn.EnableWriteCompression`，permessage-deflate（no context takeover） |
| 子协议 | `Upgrader.Subprotocols` / `Dialer.Subprotocols`，协商结果用 `Conn.Subprotocol()` 读 |
| 广播优化 | `PreparedMessage` 预编码/预压缩一条消息，供 N 个连接复用 |
| 错误判定 | `IsUnexpectedCloseError`、`IsCloseError`、`CloseError` 类型区分正常/异常断开 |
| 并发约定 | 一读一写：读方法只能一个 goroutine，写方法只能一个 goroutine |

## 3. 核心概念与原理

### 3.1 握手：用一次 HTTP/1.1 请求换一条双工 TCP

客户端发的是一个带升级头的 GET，服务端回 101：

```text
GET /ws HTTP/1.1
Host: example.com
Upgrade: websocket
Connection: Upgrade
Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==
Sec-WebSocket-Version: 13
Sec-WebSocket-Protocol: chat.v1

HTTP/1.1 101 Switching Protocols
Upgrade: websocket
Connection: Upgrade
Sec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=
Sec-WebSocket-Protocol: chat.v1
```

- `Sec-WebSocket-Accept` = base64(SHA-1(key + 固定 GUID))，用于证明服务端真的懂协议，不是普通 HTTP 服务器误接受；
- 101 之后这条 TCP 上再没有 HTTP 语义，`http.Server` 也不再管理它（连接被 hijack），所以 `Shutdown` 不会替你关连接；
- `Upgrade`/`Connection` 是逐跳头，任何反向代理都必须显式转发，否则握手直接失败（见第 6 章 Nginx 一条）。

**Origin 校验是唯一的一道门。** 浏览器发起 WebSocket 时不受同源策略限制（没有预检、没有 CORS 响应头可读），跨站页面可以带着用户的 Cookie 连你的服务——这就是 CSWSH（跨站 WebSocket 劫持）。`Upgrader.CheckOrigin` 为 nil 时默认实现是「Origin 的 host 与 `r.Host` 不区分大小写比较；Origin 缺失则放行」，这个默认值对同源部署是安全的。**返回 `true` 等于关闭这道门**，生产必须改成域名白名单，并且对无 Origin 的请求单独决策（移动端/服务端客户端通常不带 Origin）。

**子协议**是应用层契约（如 `chat.v1`、`graphql-ws`），与压缩扩展正交。客户端给候选列表，服务端只能从中挑一个回显；`Upgrader.Subprotocols` 按服务端顺序取第一个匹配项，没匹配上就返回空——此时 `Conn.Subprotocol()` 为空字符串，业务要自己决定是降级还是拒绝。

### 3.2 帧：消息是帧拼出来的

| opcode | 类型 | 约束 |
|---|---|---|
| 0x0 | continuation | 分片消息的后续帧，首帧的 FIN=0 表示未完 |
| 0x1 | text | 负载必须是合法 UTF-8 |
| 0x2 | binary | 任意字节 |
| 0x8 | close | 控制帧，≤125 字节，可带 2 字节状态码 + UTF-8 reason |
| 0x9 | ping | 控制帧，≤125 字节，对端必须回同 payload 的 pong |
| 0xA | pong | 控制帧，≤125 字节 |

关键约束：**客户端→服务端的帧必须掩码，服务端→客户端必须不掩码**；控制帧不可分片，但可以插在分片消息中间（心跳不被大消息阻塞）；状态码 1005/1006 只能在本地报告，**永远不能上线**。协议本身不提供 ack、不提供跨连接顺序、不提供断线续传——这些是应用层协议的事。

### 3.3 并发模型：一个 reader + 一个 writer

官方文档的原话是「Connections support one concurrent reader and one concurrent writer」，并且明确列出写方法：`NextWriter`、`SetWriteDeadline`、`WriteMessage`、`WriteJSON`、`EnableWriteCompression`、`SetCompressionLevel`；读方法：`NextReader`、`SetReadDeadline`、`ReadMessage`、`ReadJSON`、`SetPongHandler`、`SetPingHandler`。**例外是 `Close` 和 `WriteControl`，它们可以与所有方法并发调用。**

`WriteMessage` 不能并发的根本原因：一条消息在线上是「帧头 + 长度 + 掩码 + payload」多段写，两个 goroutine 同时进入会把两帧的字节交错写进同一个 `bufio.Writer`，轻则对端解析出乱码，重则库直接 `panic: concurrent write to websocket connection`。读侧同理：两个 goroutine 同时 `ReadMessage` 会争夺同一个帧解析状态机。

生产范式固定为两个 goroutine：

```text
readPump  ── ReadMessage ──► hub.broadcast（只读，不写 conn）
writePump ── select send / ticker ──► conn.WriteMessage（唯一写者）
业务代码 ──► client.send <- msg（永远不直接碰 conn）
```

广播时向每个连接的 `send` 投递必须用 `select + default`，否则一个慢客户端就能把整个 hub 卡死。ping 也必须由 `writePump` 发，或者用并发安全的 `WriteControl`——用 `WriteMessage` 发 ping 是经典事故。

### 3.4 deadline 与心跳

库不设任何默认超时，**不设 deadline 的读会永久阻塞**，这是连接与 goroutine 泄漏的第一大来源。标准配置：

- 读：握手后立刻 `SetReadDeadline(now + pongWait)`；在 `SetPongHandler` 里把 deadline 重置为 `now + pongWait`。pong handler 是在 `ReadMessage` 内部被调用的，所以「读超时 → 没收到 pong → 连接已死」这条链路成立；
- 写：每次写前 `SetWriteDeadline(now + writeWait)`。TCP 发送窗口满时 `WriteMessage` 会阻塞，没有写超时就会永久挂住写 goroutine；
- ping：`pingPeriod = pongWait * 9 / 10`，由服务端主动发。客户端也应周期性发 ping，或至少保证持续读——浏览器不会自动回 pong 之外的任何东西，但会由协议栈自动回 pong。

只有 ping 没有 pong 校验等于没做心跳：判活靠的是「deadline 到期」，ping 只是制造 pong 流量。反过来只有 deadline 不发 ping，空闲连接会被误杀。

### 3.5 关闭握手

WebSocket 的优雅关闭是双方各发一个 close 帧，然后关 TCP。gorilla 的 API 很容易用错：

- `conn.Close()` **只关闭底层 TCP，不发送也不等待关闭帧**，对端只能看到 1006（abnormal closure）；
- 正确姿势：`WriteControl(CloseMessage, FormatCloseMessage(code, reason), deadline)` 发关闭帧，继续 `ReadMessage` 直到返回错误（等对端回 close），再 `Close()`；
- 收到 close 时，默认 close handler 会自动回一个 close 帧；业务侧读到 `*websocket.CloseError` 后正常返回即可；
- 主动下线的状态码用 **1001 Going Away**，正常业务结束用 1000，策略拒绝用 1008，消息过大用 1009，内部错误用 1011。

### 3.6 读上限、背压与压缩

`SetReadLimit(n)` 限制**单条消息**总大小，超限时库以 1009 关闭连接。它同时约束了读缓冲的增长上限——不设的话，一个 2 GB 的帧头就能让单连接吃掉几个 GB。

背压发生在两个位置：TCP 发送缓冲（`WriteMessage` 阻塞，靠写 deadline 兜底）和应用层的 `send` channel。channel 满时有三种策略：

| 策略 | 行为 | 代价 |
|---|---|---|
| 阻塞 | `c.send <- msg` 一直等 | 一个慢客户端拖住整个广播循环，雪崩 |
| 丢弃 | `select default` 丢掉这条 | 丢消息，但服务整体可用（行情/光标类场景可接受） |
| 断开 | 缓冲满即 close 并移出 hub | 最稳，客户端重连后走全量同步 |

压缩用 `EnableCompression: true`（Upgrader 与 Dialer 都要开），协商 RFC 7692 的 permessage-deflate。gorilla 只实现 no context takeover 模式，每条消息独立压缩，压缩率不如共享字典，但实现简单。CPU 是实打实的成本：小消息（<1 KB）压缩后可能更大，高扇出场景更划算的做法是用 `PreparedMessage` 压缩一次、广播 N 次。

## 4. 使用实战

一个可直接 `go run` 的最小生产骨架：hub 广播、鉴权、心跳、单写者、读上限、优雅关闭。

### 4.1 完整服务

```go
package main

import (
	"context"
	"errors"
	"log"
	"net/http"
	"net/url"
	"os/signal"
	"sync"
	"syscall"
	"time"

	"github.com/gorilla/websocket"
)

const (
	writeWait  = 10 * time.Second
	pongWait   = 60 * time.Second
	pingPeriod = pongWait * 9 / 10
	maxMessage = 4 << 10 // 单条消息上限 4 KiB
	sendBuffer = 64
)

var allowedOrigins = map[string]bool{"example.com": true, "www.example.com": true}

var upgrader = websocket.Upgrader{
	ReadBufferSize:  1024,
	WriteBufferSize: 1024,
	Subprotocols:    []string{"chat.v1"},
	CheckOrigin: func(r *http.Request) bool {
		origin := r.Header.Get("Origin")
		if origin == "" {
			return true // 非浏览器客户端不带 Origin；需要更严就返回 false
		}
		u, err := url.Parse(origin)
		return err == nil && allowedOrigins[u.Host]
	},
}

type Client struct {
	hub  *Hub
	conn *websocket.Conn
	send chan []byte // 只由 writePump 消费，由 hub 负责关闭
}

type Hub struct {
	register   chan *Client
	unregister chan *Client
	broadcast  chan []byte
	clients    map[*Client]struct{}
	done       chan struct{}
	wg         sync.WaitGroup
}

func newHub() *Hub {
	return &Hub{
		register:   make(chan *Client),
		unregister: make(chan *Client),
		broadcast:  make(chan []byte, 256),
		clients:    make(map[*Client]struct{}),
		done:       make(chan struct{}),
	}
}

// run 是唯一读写 clients 的 goroutine，因此不需要锁。
func (h *Hub) run() {
	for {
		select {
		case c := <-h.register:
			h.clients[c] = struct{}{}
		case c := <-h.unregister:
			if _, ok := h.clients[c]; ok {
				delete(h.clients, c)
				close(c.send)
			}
		case msg := <-h.broadcast:
			for c := range h.clients {
				select {
				case c.send <- msg: // 入队，由各自的 writePump 串行落帧
				default: // 缓冲满：断开慢消费者，绝不阻塞 hub
					delete(h.clients, c)
					close(c.send)
				}
			}
		case <-h.done:
			for c := range h.clients {
				delete(h.clients, c)
				close(c.send) // writePump 见到已关闭 channel 后发 close 帧
			}
			return
		}
	}
}

func (c *Client) readPump() {
	defer func() {
		select {
		case c.hub.unregister <- c:
		case <-c.hub.done:
		}
		c.conn.Close()
	}()

	c.conn.SetReadLimit(maxMessage) // 必须在读循环之前
	_ = c.conn.SetReadDeadline(time.Now().Add(pongWait))
	c.conn.SetPongHandler(func(string) error {
		return c.conn.SetReadDeadline(time.Now().Add(pongWait))
	})

	for {
		_, data, err := c.conn.ReadMessage()
		if err != nil {
			if websocket.IsUnexpectedCloseError(err,
				websocket.CloseNormalClosure, websocket.CloseGoingAway) {
				log.Printf("read: %v", err)
			}
			return
		}
		select {
		case c.hub.broadcast <- data:
		case <-c.hub.done:
			return
		}
	}
}

func (c *Client) writePump() {
	ticker := time.NewTicker(pingPeriod)
	defer func() {
		ticker.Stop()
		c.conn.Close()
	}()

	for {
		select {
		case msg, ok := <-c.send:
			_ = c.conn.SetWriteDeadline(time.Now().Add(writeWait))
			if !ok {
				_ = c.conn.WriteMessage(websocket.CloseMessage,
					websocket.FormatCloseMessage(websocket.CloseGoingAway, "shutdown"))
				return
			}
			if err := c.conn.WriteMessage(websocket.TextMessage, msg); err != nil {
				return
			}
		case <-ticker.C:
			_ = c.conn.SetWriteDeadline(time.Now().Add(writeWait))
			// 心跳走同一个写者，避免与业务写并发
			if err := c.conn.WriteMessage(websocket.PingMessage, nil); err != nil {
				return
			}
		}
	}
}

func serveWS(h *Hub, w http.ResponseWriter, r *http.Request) {
	// 鉴权放在 Upgrade 之前：失败就是普通 401，不留下半条连接
	if _, err := r.Cookie("session"); err != nil {
		http.Error(w, "unauthorized", http.StatusUnauthorized)
		return
	}
	conn, err := upgrader.Upgrade(w, r, nil)
	if err != nil {
		return // Upgrade 已写过 HTTP 错误响应，别重复写
	}

	c := &Client{hub: h, conn: conn, send: make(chan []byte, sendBuffer)}
	select {
	case h.register <- c:
	case <-h.done:
		conn.Close()
		return
	}

	h.wg.Add(2)
	go func() { defer h.wg.Done(); c.writePump() }()
	go func() { defer h.wg.Done(); c.readPump() }()
}

func main() {
	hub := newHub()
	go hub.run()

	mux := http.NewServeMux()
	mux.HandleFunc("/ws", func(w http.ResponseWriter, r *http.Request) { serveWS(hub, w, r) })
	srv := &http.Server{Addr: ":8080", Handler: mux, ReadHeaderTimeout: 5 * time.Second}

	go func() {
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fatalf("listen: %v", err)
		}
	}()

	go func() { // 演示：每 3 秒广播一次
		for range time.Tick(3 * time.Second) {
			select {
			case hub.broadcast <- []byte(`{"type":"tick"}`):
			case <-hub.done:
				return
			}
		}
	}()

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	<-ctx.Done()

	close(hub.done) // 1. 通知 hub 关闭所有 send channel
	sdCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_ = srv.Shutdown(sdCtx) // 2. 停收新连接；已升级的连接不归 Shutdown 管
	hub.wg.Wait()           // 3. 等每个连接发完 close 帧后退出
	log.Println("server stopped")
}
```

### 4.2 验证与观察

```bash
# 命令行客户端（npm i -g wscat）
wscat -c ws://127.0.0.1:8080/ws -H "Cookie: session=xxx" -s chat.v1

# 连接数、fd、内存三条曲线一起看
ss -s | head -5
ls /proc/$(pgrep -f myapp)/fd | wc -l
```

### 4.3 鉴权方式取舍

浏览器 `new WebSocket(url)` 不能设置自定义 Header，于是只有三条路：

| 方式 | 优点 | 代价 |
|---|---|---|
| Cookie | 浏览器自动携带，`HttpOnly` 抗 XSS 窃取 | 依赖 SameSite/CORS 策略，跨站场景要防 CSWSH；移动端未必有 cookie |
| Query token | 任何客户端都能用，实现最简 | **URL 会进 Nginx access log、浏览器历史、Referer、链路追踪**，等于长期泄露凭证 |
| 子协议传 token | 能进 Header 位置 | 污染协议协商语义，长度受限，非标准 |

结论：浏览器优先 Cookie（`Secure; HttpOnly; SameSite=Lax`），原生客户端用 Header；query token 只接受**一次性、分钟级、绑定 IP/UA** 的短票据，并且日志里必须脱敏。

### 4.4 水平扩展

单机的 hub 只能广播到本进程的连接。多实例时每个节点都要订阅同一条总线：业务侧把事件 publish 到 Redis Pub/Sub、NATS 或 Kafka，各节点消费后只投递给本地连接。**粘性会话不是必需品**——连接一旦建立就固定在某个节点上，真正需要外置的是「用户 → 节点/连接」的会话表（定向推送时用），以及离线消息与序号。广播类场景用主题订阅即可，无需会话表。

## 5. 版本演进

| 版本 | 时间 | 要点 |
|---|---|---|
| v1.4.2 | 2019 | 老项目的常见基线，`Upgrader`/`Dialer` 形态已定型 |
| v1.5.0 | 2021.7 | 现代 API 基线：模块化、代理与 Dialer 能力完善 |
| v1.5.1 | 2023.11 | 社区接手后的首个维护版，修边界 bug、补文档 |
| v1.5.2 | 2024.2 | 小版本修复，API 无变化 |
| v1.5.3 | 2024.6 | **当前版本**，仅维护模式 |
| 2025.3 | 最后一次提交 | HTTPS proxy 支持；此后无功能开发 |

兼容性策略保守：升级平缓，但不会再有新特性（HTTP/2 上的 WebSocket、并发写、context 取消只能靠替代库）。

## 6. 常见坑与最佳实践

1. **并发写直接 panic**：两个 goroutine 同时 `WriteMessage` 会触发 `concurrent write to websocket connection`，进程级崩溃且不可 recover；所有写收敛到 `writePump`，业务只投 channel。
2. **ping 用 `WriteMessage` 发**：这是并发写的隐藏来源（定时器 goroutine + 业务写）。要么在 `writePump` 里发，要么用并发安全的 `WriteControl(PingMessage, ...)`。
3. **忘记 deadline**：`ReadMessage` 在没有 deadline 时会永久阻塞，客户端拔网线后 goroutine 与 fd 都不释放；读 deadline + pong handler 重置是标配。
4. **`CheckOrigin` 直接 `return true`**：任意站点都能带着用户 Cookie 连上来，等于把 API 开放给全互联网；用域名白名单，并对无 Origin 请求显式决策。
5. **不设 `SetReadLimit`**：单个超大帧就能吃掉数 GB 内存（读缓冲按需增长），必须按业务上限设置，超限让库以 1009 关闭。
6. **`conn.Close()` 当优雅关闭**：它不发关闭帧，对端只看到 1006；要 `WriteControl(CloseMessage, ...)` 后等对端回 close 再关 TCP。
7. **收到 close 不回**：读循环里读到 close 错误直接 return 而不让库回帧，对端会一直等到超时；依赖默认 close handler，或自己回一个同状态码的 close。
8. **只有读 goroutine 退出**：读出错返回了，写 goroutine 还阻塞在 `send`/`ticker` 上，连接对象永远不回收；两个 pump 必须成对退出，且 `Close` 放在 defer。
9. **Nginx 没配 Upgrade**：`Upgrade`/`Connection` 是逐跳头，不显式转发就 400；同时 `proxy_read_timeout` 默认 60s，必须大于心跳周期，否则空闲连接被代理静默切断。
10. **广播复用同一个 `[]byte` 后原地修改**：所有连接共享同一片底层数组，谁改谁污染；要么只读共享，要么每条 copy。
11. **开压缩不看场景**：小消息压缩后更大、CPU 翻倍；高扇出先用 `PreparedMessage` 压一次，再分发给 N 个连接。
12. **连接数规划缺失**：每连接至少 2 个 goroutine + 读缓冲 + `send` 缓冲；fd 上限（`ulimit -n`）、`somaxconn`、端口范围、内存都要提前压测，别等 `too many open files`。
13. **query token 进日志**：`/ws?token=...` 会出现在 access log、APM、Referer 里；短票据 + 日志脱敏，或改用 Cookie/Header。
14. **客户端没有重连策略**：1006 断线是常态，必须指数退避 + jitter，否则服务重启瞬间会被全量客户端打爆。

## 7. 选型对比

| 方案 | 方向 | 传输 | 浏览器 | 运维复杂度 | 适用 |
|---|---|---|---|---|---|
| gorilla/websocket | 全双工 | TCP（HTTP/1.1 Upgrade） | ✅ | 低 | 存量项目、生态/教程最全 |
| coder/websocket | 全双工 | 同上 | ✅ | 低 | 新项目首选：context、并发写、零依赖 |
| gobwas/ws | 全双工 | 同上 | ✅ | 中 | 极致连接密度、自定义帧处理 |
| SSE（`net/http` + `Flusher`） | 单向服务端推送 | HTTP/1.1 或 HTTP/2 长响应 | ✅（EventSource） | 低 | 行情、通知、日志流；自带断线重连语义 |
| 长轮询（`net/http`） | 伪推送 | 普通 HTTP 请求 | ✅ | 最低 | 低频、弱网、代理不友好环境；延迟与开销最大 |
| WebTransport（quic-go/webtransport-go） | 全双工 + 多流 | HTTP/3 over QUIC | 部分（Chromium 系） | 高 | 弱网、多路复用、低延迟游戏/音视频 |

- 只需要服务端推、客户端偶尔 POST：**SSE 更省事**，不用自己写重连、不受代理 Upgrade 限制；
- 需要双向、低延迟、二进制：WebSocket；新项目直接 `coder/websocket`，存量 gorilla 不必迁；
- 延迟要求宽松、基础设施老旧：长轮询最不容易出错；
- 真要上 WebTransport，先确认 CDN/LB 支持 HTTP/3，否则运维成本远超收益。

参考：[gorilla/websocket 文档](https://pkg.go.dev/github.com/gorilla/websocket)、[RFC 6455](https://www.rfc-editor.org/rfc/rfc6455)、[coder/websocket](https://github.com/coder/websocket)、[RFC 7692 permessage-deflate](https://www.rfc-editor.org/rfc/rfc7692)。
