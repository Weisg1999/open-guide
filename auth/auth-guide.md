# 认证：JWT / OAuth2 / OIDC 技术指南

> 认证回答「你是谁」，授权回答「你能做什么」：JWT 是令牌格式而不是会话方案，OIDC 是在 OAuth2 之上补的一层身份协议。
> 参考实现：<https://github.com/golang-jwt/jwt> v5 · <https://github.com/coreos/go-oidc> v3 · <https://pkg.go.dev/golang.org/x/oauth2>　|　协议：RFC 7519 / RFC 6749 / OpenID Connect Core 1.0

---

## 1. 项目简介

三者不是竞品，而是**一条链路上的三层**：

- **golang-jwt/jwt v5**（MIT）：只做令牌的编解码与签名验签。不定义怎么发令牌、不管用户目录、不管撤销，也没有任何 HTTP 语义——当「带签名的 JSON」用，别当「登录方案」用；
- **golang.org/x/oauth2**（BSD-3-Clause）：OAuth2 **客户端**侧实现——拼授权 URL、换 token、`TokenSource` 自动刷新。它既不是授权服务器（AS），也不校验 ID Token；
- **coreos/go-oidc v3**（Apache-2.0，`.../v3/oidc`）：在 x/oauth2 之上补 OIDC 客户端能力——发现文档、JWKS 缓存、ID Token 校验、UserInfo。

职责边界：**x/oauth2 负责「拿到令牌」，go-oidc 负责「确认令牌代表谁」，golang-jwt 负责「自己签发/校验令牌时的字节级操作」**。接第三方 IdP（Keycloak/Entra/Google）只写客户端；自己当 AS 对内发令牌，则用 golang-jwt 签发、资源服务侧用 JWKS 校验。适用：Web/移动端登录（Authorization Code + PKCE）、服务间 `client_credentials`、网关统一鉴权、多租户声明下发。**不适用**：集中式用户目录与 SSO（Keycloak 的领域）、细粒度资源授权（Casbin/OpenFGA 的领域）、要求「登出即失效」的强一致会话（服务端 session 更合适）。

```bash
go get github.com/golang-jwt/jwt/v5 github.com/coreos/go-oidc/v3/oidc golang.org/x/oauth2
```

---

## 2. 功能特性总览

| 能力 | golang-jwt/jwt v5 | x/oauth2 | coreos/go-oidc v3 |
|---|---|---|---|
| 令牌签发 | ✅ HS/RS/PS/ES/EdDSA | ❌ | ❌ |
| 令牌校验 | ✅ 可插拔 `Keyfunc` | ❌ | ✅（仅 ID Token） |
| claims 校验 | ✅ `iss/aud/exp/nbf/iat` + leeway | ❌ | ✅ `iss/aud/exp`，`nonce` 需自查 |
| 密钥来源 | 自己注入（文件/KMS/JWKS） | ❌ | ✅ 自动拉取并缓存 JWKS |
| 授权码流 + PKCE | ❌ | ✅ `AuthCodeURL`/`Exchange`/`GenerateVerifier` | ✅ 与 `provider.Endpoint()` 组合 |
| 自动刷新 | ❌ | ✅ `TokenSource` / `ReuseTokenSource` | 复用 x/oauth2 |
| 发现文档 | ❌ | ⚠️ 需手填 `Endpoint` | ✅ `oidc.NewProvider` 自动解析 |

---

## 3. 核心概念与原理

### 3.1 三段结构与 claims

`header.payload.signature`，三段各 base64url（无填充）后以 `.` 连接。签名覆盖 `base64url(header) + "." + base64url(payload)` 这串原始字节，改一字节即失效；但 **payload 是明文**，解码就能读。JWT 只保证完整性、不保证机密性。

| claim | 含义 | 校验要点 |
|---|---|---|
| `iss` | 签发者 | 与预期值**全等**比较（含 scheme 与路径，Keycloak 精确到 `/realms/{realm}`） |
| `sub` | 主体 | 用户/工作负载的稳定标识；别当数据库外键（换 IdP 就失效） |
| `aud` | 受众 | 必须是**本服务**的标识，单字符串或数组 |
| `exp` | 过期时间 | 必须存在且必须校验；缺失视为无效 |
| `nbf` | 生效时间 | 未来时间戳应拒绝 |
| `iat` | 签发时间 | 显著晚于本机时间说明时钟错乱或伪造 |

自定义 claims（`tid`、`roles`、`scope`）没有标准语义，别用它做授权决策的唯一依据——除非签名密钥只在你手里，且你接受「令牌有效期内权限不可变更」。

### 3.2 签名算法与密钥轮换

| 算法 | 密钥 | 谁能签发 | 适用 |
|---|---|---|---|
| HS256 | 共享密钥（≥32 字节） | **任何**持有密钥者 | 单体、同一信任域的短链路 |
| RS256 | RSA 2048+ 私钥/公钥 | 仅私钥持有者 | 默认选择，JWKS 生态最成熟 |
| PS256 | RSA-PSS | 同上 | 有 PSS 合规要求时 |
| ES256 | ECDSA P-256 | 同上 | 令牌更短、验签更快；老网关支持略差 |

对称算法的真正问题不是强度，而是**信任扩散**：校验方必须持有同一把密钥，也就等于拥有了签发权。20 个微服务共享一个 HS256 密钥，等于 20 个 AS。非对称下必须解决「用哪把公钥」，这就是 `kid`：写进 header，校验方拿它去 JWKS 挑。轮换固定四步：**① 新密钥发布到 JWKS（旧密钥保留）→ ② 等过 ≥ 最大令牌寿命的宽限期 → ③ 切换签发密钥 → ④ 确认无旧令牌在途后移除旧公钥**。宽限期取 `max(access TTL, ID Token TTL) + 时钟偏移`，通常 1–24 小时。同时必须防**算法混淆**：攻击者把 `alg` 从 `RS256` 改成 `HS256`，再用**公开的 RSA 公钥**当 HMAC 密钥重签；校验方若按 header 里的 `alg` 选算法就会中招，因此算法必须由校验方写死。

### 3.3 校验清单：少一项就是一个洞

1. **签名**：按 `kid` 定位密钥验签；`kid` 缺失或未知 → 拒绝（可能是轮换未生效，也可能是伪造）；
2. **`alg` 白名单**：`WithValidMethods([]string{"RS256"})`，同时挡住 `alg: none` 与 HS/RS 混淆；
3. **`iss`**：全等比较。多 IdP 时必须按 issuer 路由到对应密钥集，否则 A 租户令牌能打 B 租户；
4. **`aud`**：**最常被漏掉的一项**。只验签名和 `exp` 的话，同一 AS 发给「订单服务」的令牌能原样打「管理服务」——同 issuer、同密钥。每个资源服务必须校验 `aud` 含自己；
5. **`exp` / `nbf` / `iat`**：`WithExpirationRequired()` 强制 `exp` 存在，leeway 30–60 秒；别用 5 分钟 leeway 当「容错」，那等于送给攻击者 5 分钟窗口；
6. **`jti`**（可选）：需即时吊销时存 Redis 黑名单，TTL 设为令牌剩余寿命。

**`alg: none` 攻击**值得单独说：header 改成 `{"alg":"none"}`、签名段清空，早期库或手写解析器看到「无签名」就放行，等于任意伪造身份。golang-jwt v5 默认拒绝 `none`，但显式白名单仍不可省。时钟偏移是分布式常态（NTP 漂移、跨区延迟），leeway 30 秒是工程折中；若大量 `ErrTokenNotValidYet`，先查 NTP 而不是调大 leeway。

### 3.4 OAuth2 的四种模式与 PKCE

| 模式 | 谁用 | 状态 | 说明 |
|---|---|---|---|
| `authorization_code` + PKCE | Web、SPA、移动端 | **唯一推荐** | code 经前端回跳，token 由后端/BFF 换 |
| `client_credentials` | 服务间 | 可用 | 无用户上下文，`sub` 是客户端自身 |
| `implicit` | 纯前端 | **已弃用** | token 直接放 URL fragment |
| `password`（ROPC） | 第一方迁移期 | **已弃用** | 客户端直接经手用户密码 |

**隐式模式为什么被弃用**：token 出现在 `#fragment`，会进入浏览器历史、`Referer`、截图、前端日志；没有 code 交换环节因此**无法使用 PKCE**，也无法证明换 token 的就是发起请求的客户端；没有 refresh，access token 寿命被迫拉长，泄露窗口更大。RFC 9700 不建议，OAuth 2.1 草案直接删除。**密码模式为什么不适合第三方**：它要求客户端**经手用户明文密码**，等于让用户把凭证交给不受信任的 RP，一次泄露波及该用户在 AS 上的全部权限；MFA、SSO、联邦登录、风控全部失效。只有「自己是第一方客户端 + 正在迁移」才有短暂存在理由。

**PKCE（RFC 7636）**解决授权码被拦截：客户端生成随机 `code_verifier`（43–128 字符），`code_challenge = BASE64URL(SHA256(verifier))`；authorize 带 challenge，token 带 verifier，AS 比对哈希。攻击者截获 code 也换不到 token。public client 必选、confidential 也建议（OAuth 2.1 要求全覆盖）。配套还有 `state` 防 CSRF（一次一用、回调比对即删）与 `redirect_uri` **精确字符串匹配**（不做前缀匹配，防开放重定向）。

### 3.5 OIDC 在 OAuth2 之上补了什么

OAuth2 只解决「拿到能访问资源的令牌」，**不告诉你用户是谁**。OIDC 补了四样：

- **ID Token**：一个 JWT，声明本次登录的主体——`iss`/`sub`/`aud`(=client_id)/`exp`/`iat`/`nonce`/`auth_time`/`acr`/`amr`；
- **UserInfo 端点**：用 access token 换用户属性（`name`/`email`/`picture`），适合属性多或需实时的场景；
- **发现文档**：`{issuer}/.well-known/openid-configuration`，给出 authorize/token/jwks/end_session 各端点；
- **`nonce`**：授权请求带的一次性随机串，ID Token 里原样返回，绑定「这次登录」与「这次回调」。

三个必须记住的区别：① **ID Token 的 `aud` 是 `client_id`，不是 API 标识**，拿它调资源服务是典型错误；② **access token 的格式 OIDC 不管**，可以是不透明串（配 introspection），也可以是 JWT，它面向资源服务而非前端；③ **`nonce` 库不会替你比对**，go-oidc 只校验签名、`iss`、`aud`、`exp`，`idToken.Nonce` 要自己和服务端会话比。OIDC 不改变重定向流程：scope 加 `openid`，token 响应多一个 `id_token`，仅此而已。

### 3.6 生命周期、撤销与传播

access token 的典型寿命是 5–15 分钟，放内存或 BFF 的 `HttpOnly` Cookie，短到「泄露也只能用一会儿」；refresh token 7–30 天滑动，只存服务端、绝不给浏览器 JS，且一用一换。

**refresh 轮换与重放检测**：每次刷新签发新 refresh 并作废旧的（同一 `family_id`）。若某个**已使用**的 refresh 再次出现，说明两条链路都持有它——无法判断谁是攻击者，因此**整条 family 全部撤销**，强制重新登录（RFC 9700 §4.14.2）。不轮换的 refresh 一旦泄露可无限续期，且永远发现不了。**无状态令牌的撤销难题**：JWT 校验只依赖签名与时间，AS「拉黑」不会自动生效。四条路径：① access token 压到分钟级，靠 refresh 撤销间接生效（成本最低，首选）；② `jti` 黑名单（Redis `SETEX`，TTL = 剩余寿命），每请求一次 Redis；③ 用户级 `token_version`，改密码/强制登出时 +1，令牌带 `ver` 校验时比对；④ introspection（RFC 7662），每请求问 AS——等于退回有状态。**登出**要同时做：清本地会话、吊销 refresh、可选调用 `end_session_endpoint`。

**JWT vs 服务端 session**：前者无状态、天然横向扩展、跨域/移动端友好，但撤销难、体积大、权限变更要等令牌过期；后者每请求查存储、撤销即时、体积只有一个 id，但要集中式存储、跨域受 Cookie 限制。**多服务传播**两种范式：网关统一校验（边缘验签后注入 `X-User-Id`，内部信任网段，简单但内网被突破即失守）与每服务自校验（零信任，各自缓存 JWKS，需处理时钟与轮换）。折中做法是网关校验 + 内部 mTLS + 透传**原始令牌**让下游按需再验；无论哪种，令牌都**不得写入日志、URL query 或透传给第三方**。

---

## 4. 使用实战

### 4.1 golang-jwt v5：签发与校验

`jwt.RegisteredClaims` 提供标准字段与内置校验，自定义字段直接嵌入。`WithValidMethods` 与 `WithAudience` 是两行不能省的代码。

```go
package main

import (
	"crypto/rand"
	"crypto/rsa"
	"fmt"
	"log"
	"time"

	"github.com/golang-jwt/jwt/v5"
)

type AccessClaims struct {
	TenantID string `json:"tid"`
	jwt.RegisteredClaims
}

func issue(priv *rsa.PrivateKey, kid string) (string, error) {
	now := time.Now()
	t := jwt.NewWithClaims(jwt.SigningMethodRS256, AccessClaims{
		TenantID: "t-42",
		RegisteredClaims: jwt.RegisteredClaims{
			Issuer: "https://auth.example.com", Subject: "user-1001",
			Audience:  jwt.ClaimStrings{"orders-api"},
			ExpiresAt: jwt.NewNumericDate(now.Add(15 * time.Minute)),
			NotBefore: jwt.NewNumericDate(now), IssuedAt: jwt.NewNumericDate(now),
			ID: fmt.Sprintf("%x", now.UnixNano()), // jti：生产用 UUID
		},
	})
	t.Header["kid"] = kid // 校验方靠它选公钥，轮换的前提
	return t.SignedString(priv)
}

func verify(pub *rsa.PublicKey, raw string) (*AccessClaims, error) {
	c := &AccessClaims{}
	_, err := jwt.ParseWithClaims(raw, c, func(*jwt.Token) (any, error) { return pub, nil },
		jwt.WithValidMethods([]string{"RS256"}), // 白名单：堵死 alg:none 与 HS/RS 混淆
		jwt.WithIssuer("https://auth.example.com"),
		jwt.WithAudience("orders-api"), // 不校验 aud → 令牌可跨服务通用
		jwt.WithExpirationRequired(),
		jwt.WithLeeway(30*time.Second), // 容忍节点间时钟偏移
		jwt.WithIssuedAt(),
	)
	if err != nil { return nil, err }
	return c, nil
}

func main() {
	priv, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil { log.Fatal(err) }
	raw, err := issue(priv, "k-2026-11")
	if err != nil { log.Fatal(err) }
	fmt.Println(verify(&priv.PublicKey, raw)) // 返回 claims 与 error
}
```

### 4.2 JWKS 端点、缓存与轮换宽限

AS 侧暴露 `GET /.well-known/jwks.json`：`{"keys":[{"kty":"RSA","kid":"k-2026-11","use":"sig","alg":"RS256","n":"...","e":"AQAB"}]}`。资源服务侧的关键设计是**缓存 + 未知 `kid` 强制刷新一次 + 刷新失败降级用旧密钥**：轮换期间新旧密钥并存，「`kid` 未命中」通常是正常过渡态，不该直接 401。

```go
// 依赖：net/http、sync、crypto/rsa、encoding/base64、encoding/json、errors、fmt、math/big、time、jwt/v5
type keyStore struct {
	url  string
	hc   *http.Client
	mu   sync.RWMutex
	keys map[string]*rsa.PublicKey
	exp  time.Time
}

// Keyfunc：命中缓存直接用；未命中（可能是轮换中）刷新一次后重试。
func (s *keyStore) Keyfunc(t *jwt.Token) (any, error) {
	kid, _ := t.Header["kid"].(string)
	if kid == "" { return nil, errors.New("missing kid") }
	if k, ok := s.cached(kid, true); ok { return k, nil }
	if err := s.reload(); err != nil {
		if k, ok := s.cached(kid, false); ok { return k, nil } // 降级：旧密钥仍能验旧令牌
		return nil, err
	}
	if k, ok := s.cached(kid, true); ok { return k, nil }
	return nil, fmt.Errorf("unknown kid %q", kid)
}

func (s *keyStore) cached(kid string, requireFresh bool) (*rsa.PublicKey, bool) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	k, ok := s.keys[kid]
	if !ok || (requireFresh && time.Now().After(s.exp)) { return nil, false }
	return k, true
}

func (s *keyStore) reload() error {
	resp, err := s.hc.Get(s.url)
	if err != nil { return err }
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK { return fmt.Errorf("jwks: %d", resp.StatusCode) }
	var set struct{ Keys []struct{ Kty, Kid, Use, N, E string } `json:"keys"` }
	if err = json.NewDecoder(resp.Body).Decode(&set); err != nil { return err }
	next := make(map[string]*rsa.PublicKey, len(set.Keys))
	for _, k := range set.Keys {
		if k.Kty != "RSA" || (k.Use != "" && k.Use != "sig") { continue }
		nb, errN := base64.RawURLEncoding.DecodeString(k.N)
		eb, errE := base64.RawURLEncoding.DecodeString(k.E)
		if errN != nil || errE != nil { return errors.New("bad jwk") }
		next[k.Kid] = &rsa.PublicKey{N: new(big.Int).SetBytes(nb), E: int(new(big.Int).SetBytes(eb).Int64())}
	}
	s.mu.Lock()
	s.keys, s.exp = next, time.Now().Add(10*time.Minute) // TTL 别超过 1 小时
	s.mu.Unlock()
	return nil
}
```

`E` 的转换只适用于标准 65537，极端场景需读 `big.Int`。生产可直接用 `MicahParks/keyfunc` 或 `go-jose`，但**缓存 TTL、`kid` 未命中重试、失败降级**这三点必须自己想清楚；多副本高频拉取会被 IdP 限流，TTL 加抖动（10 分钟 ± 60 秒）即可。

### 4.3 go-oidc 对接 Keycloak

Keycloak 的 issuer 精确到 realm；发现文档、JWKS 地址、token 端点全由 `oidc.NewProvider` 解析，不要手抄 URL。

```go
// 依赖：context、net/http、strings、go-oidc/v3/oidc、x/oauth2
type idp struct {
	cfg      *oauth2.Config
	verifier *oidc.IDTokenVerifier
}

func newIDP(ctx context.Context) (*idp, error) {
	p, err := oidc.NewProvider(ctx, "https://kc.example.com/realms/main")
	if err != nil { return nil, err }
	return &idp{
		cfg: &oauth2.Config{
			ClientID: "orders-web", ClientSecret: "change-me",
			RedirectURL: "https://orders.example.com/callback",
			Endpoint:    p.Endpoint(), // 端点全来自发现文档
			Scopes:      []string{oidc.ScopeOpenID, "profile", "email"},
		},
		verifier: p.Verifier(&oidc.Config{ClientID: "orders-web"}),
	}, nil
}

func (i *idp) login(w http.ResponseWriter, r *http.Request) {
	state, nonce, v := oauth2.GenerateVerifier(), oauth2.GenerateVerifier(), oauth2.GenerateVerifier()
	// 最小实现：生产应加密签名后再进 Cookie，或放 Redis
	http.SetCookie(w, &http.Cookie{Name: "flow", Value: state + "|" + nonce + "|" + v, Path: "/",
		HttpOnly: true, Secure: true, SameSite: http.SameSiteLaxMode, MaxAge: 600})
	http.Redirect(w, r, i.cfg.AuthCodeURL(state, oauth2.SetAuthURLParam("nonce", nonce), oauth2.S256ChallengeOption(v)), http.StatusFound)
}

func (i *idp) callback(w http.ResponseWriter, r *http.Request) {
	var state, nonce, v string
	if ck, err := r.Cookie("flow"); err == nil {
		if p := strings.SplitN(ck.Value, "|", 3); len(p) == 3 { state, nonce, v = p[0], p[1], p[2] }
	}
	if state == "" || r.URL.Query().Get("state") != state {
		http.Error(w, "state mismatch", http.StatusBadRequest)
		return
	}
	// 一次性使用：校验通过立即清掉 flow cookie，避免同一 state 被重放
	http.SetCookie(w, &http.Cookie{Name: "flow", Value: "", Path: "/", MaxAge: -1,
		HttpOnly: true, Secure: true, SameSite: http.SameSiteLaxMode})
	tok, err := i.cfg.Exchange(r.Context(), r.URL.Query().Get("code"), oauth2.VerifierOption(v))
	if err != nil { http.Error(w, err.Error(), http.StatusUnauthorized); return }
	raw, ok := tok.Extra("id_token").(string)
	if !ok { http.Error(w, "no id_token", http.StatusUnauthorized); return }
	idt, err := i.verifier.Verify(r.Context(), raw) // 验签 + iss + aud + exp
	if err != nil { http.Error(w, err.Error(), http.StatusUnauthorized); return }
	if idt.Nonce != nonce { http.Error(w, "nonce mismatch", http.StatusUnauthorized); return } // 库不比对 nonce
	// 通过：用 idt.Subject 建本地会话，tok.AccessToken 调资源服务
	http.Redirect(w, r, "/", http.StatusFound)
}
```

### 4.4 Gin 认证中间件

中间件只做「验签 + 注入身份」，授权交给下一层。`Bearer` 前缀、`Abort`、`WWW-Authenticate` 三件事别省。

```go
// 依赖：net/http、strings、time、gin、jwt/v5；AccessClaims 与 4.1 同包
func Auth(keyfunc jwt.Keyfunc, issuer, audience string) gin.HandlerFunc {
	return func(c *gin.Context) {
		h := c.GetHeader("Authorization")
		if !strings.HasPrefix(h, "Bearer ") {
			c.Header("WWW-Authenticate", `Bearer realm="api"`)
			c.AbortWithStatusJSON(http.StatusUnauthorized, gin.H{"code": "NO_TOKEN"})
			return
		}
		claims := &AccessClaims{} // 结构体见 4.1
		_, err := jwt.ParseWithClaims(strings.TrimPrefix(h, "Bearer "), claims, keyfunc,
			jwt.WithValidMethods([]string{"RS256"}),
			jwt.WithIssuer(issuer), jwt.WithAudience(audience),
			jwt.WithExpirationRequired(), jwt.WithLeeway(30*time.Second),
		)
		if err != nil {
			c.AbortWithStatusJSON(http.StatusUnauthorized, gin.H{"code": "INVALID_TOKEN"}) // 需要时用 errors.Is 区分 TOKEN_EXPIRED
			return
		}
		c.Set("sub", claims.Subject) // 下游 handler 用 c.GetString("sub")
		c.Set("tid", claims.TenantID)
		c.Next()
	}
}
```

### 4.5 refresh token 轮换与重放检测

只存哈希、不存明文；`family_id` 串起一代代令牌；`FOR UPDATE` 保证并发刷新不会双发。

```sql
CREATE TABLE refresh_token (id BIGSERIAL PRIMARY KEY, family_id UUID NOT NULL, user_id BIGINT NOT NULL,
    token_hash BYTEA NOT NULL UNIQUE, expires_at TIMESTAMPTZ NOT NULL, used_at TIMESTAMPTZ, revoked_at TIMESTAMPTZ);
```

```go
// 依赖：context、database/sql、crypto/sha256、crypto/rand、encoding/hex、errors、time
func rotateRefresh(ctx context.Context, db *sql.DB, raw string) (string, error) {
	now, sum := time.Now(), hash(raw)
	tx, err := db.BeginTx(ctx, nil)
	if err != nil { return "", err }
	defer tx.Rollback() // Commit 后返回 ErrTxDone，可忽略

	var family string
	var uid int64
	var used, revoked sql.NullTime
	if err = tx.QueryRowContext(ctx, `SELECT family_id, user_id, used_at, revoked_at FROM refresh_token
		WHERE token_hash = $1 AND expires_at > $2 FOR UPDATE`, sum, now).Scan(&family, &uid, &used, &revoked); err != nil {
		return "", err
	}
	if used.Valid || revoked.Valid { // 已用令牌再次出现：整族作废
		_, err = tx.ExecContext(ctx, `UPDATE refresh_token SET revoked_at = $1
			WHERE family_id = $2 AND revoked_at IS NULL`, now, family)
		if err != nil { return "", err }
		return "", errors.Join(errors.New("refresh token reused"), tx.Commit())
	}
	next, err := newOpaqueToken()
	if err != nil { return "", err }
	if _, err = tx.ExecContext(ctx, `UPDATE refresh_token SET used_at = $1 WHERE token_hash = $2`, now, sum); err != nil {
		return "", err
	}
	_, err = tx.ExecContext(ctx, `INSERT INTO refresh_token (family_id, token_hash, user_id, expires_at)
		VALUES ($1, $2, $3, $4)`, family, hash(next), uid, now.Add(30*24*time.Hour))
	if err != nil { return "", err }
	return next, tx.Commit()
}

func hash(s string) []byte { b := sha256.Sum256([]byte(s)); return b[:] }

func newOpaqueToken() (string, error) {
	b := make([]byte, 32)
	if _, err := rand.Read(b); err != nil { return "", err }
	return hex.EncodeToString(b), nil
}
```

access token 用 4.1 的 `issue()` 现场签，refresh 只给 BFF/移动端安全存储；`errors.Join(业务错误, tx.Commit())` 保证撤销失败与提交失败都不会被吞。

---

## 5. 版本演进

截至 2026-11，三条主线为 **golang-jwt/jwt v5.3.1**（2026-01-28，MIT）、**coreos/go-oidc v3.21.0**（2026-09-01，Apache-2.0）、**x/oauth2 v0.37.0**（2026-08-25，BSD-3-Clause）。

| 时间 | 事件 | 影响 |
|---|---|---|
| 2015 | RFC 7519 发布 | JWT claim 语义与 `alg` 注册表定型 |
| 2023.11 | golang-jwt **v5.0.0**（社区从 `dgrijalva/jwt-go` 分叉而来） | `Claims` 接口化、默认严格校验、错误链可 `errors.Is`；移除 `StandardClaims` |
| 2025.1 | **RFC 9700（BCP 240）** | 安全基线：强制 PKCE、禁 password/implicit、redirect 精确匹配、refresh 轮换或发送方约束 |
| 2025–2026 | OAuth 2.1 仍为草案 | 截至 2026-11 为 `draft-ietf-oauth-v2-1-17`（2026-10-30），未成为 RFC；落地对齐 RFC 9700 |
| 2026.1 | golang-jwt **v5.3.1** | 新增 `WithNotBeforeRequired`，修复 header 解析类问题（5.2.2 起） |

升级提示：v4 → v5 破坏性（claims 结构与解析选项全改），别跨大版本盲升；go-oidc v3 依赖 go-jose v4 与 x/oauth2，升级后同步 `go mod tidy` 并确认 Go 版本下限（当前要求 1.25）。**不要自己实现 JWT 验签**——历史漏洞几乎都出在手写解析与算法选择上。

---

## 6. 常见坑与最佳实践

1. **不校验 `alg`，中 `alg: none`**：header 改成 `{"alg":"none"}`、签名段清空，手写解析器直接放行。对策：`WithValidMethods` 白名单，永不从令牌头推导算法；
2. **HS256/RS256 混淆**：把 `RS256` 改成 `HS256`，用公开的公钥当 HMAC 密钥重签即可伪造。对策：校验方固定算法，对外只发布公钥；
3. **只验签名不验 `aud`/`iss`**：同一 AS 发给订单服务的令牌能直接打管理服务。对策：每个资源服务写死自己的 `aud`，多 IdP 时按 `iss` 选密钥集；
4. **密钥硬编码/入库明文**：`const secret = "..."`、配置提交到 Git。对策：KMS/Vault/环境注入，至少 `0600` 文件 + 启动校验长度；HS256 密钥 ≥32 字节；
5. **忘记校验 `exp` 或允许无 `exp`**：不同版本默认行为有差异，无 `exp` 的令牌可能永不过期。对策：`WithExpirationRequired()`，并断言 `exp - iat ≤ 预期上限`；
6. **时钟偏移导致误判**：多节点 NTP 漂移，正常令牌被判 `not valid yet`。对策：leeway 30–60 秒 + 监控偏移，别用加大 leeway 掩盖问题；
7. **JWT 放 localStorage 被 XSS 读取**：同源任一 XSS 都能把令牌带走。对策：BFF 模式放 `HttpOnly; Secure; SameSite=Lax` Cookie；纯 SPA 至少放内存 + refresh 轮换；
8. **refresh token 不轮换**：泄露后可无限续期且无感知。对策：一用一换 + family 重放检测，已用令牌再次出现即全族撤销；
9. **把敏感信息塞进 payload**：payload 只是 base64，不是加密。对策：只放标识与最小授权声明，PII 走 UserInfo；
10. **把 ID Token 当 access token 用**：`aud` 是 `client_id`，资源服务应拒绝。对策：两条令牌分离，ID Token 只用于建会话；
11. **JWKS 缓存策略粗暴**：每次拉会被 IdP 限流，永不刷新则轮换后全网 401。对策：TTL 缓存 + 未知 `kid` 强制刷新一次 + 失败降级旧密钥 + TTL 加抖动。

---

## 7. 选型对比

| 维度 | 自建 JWT + 自研 AS | Keycloak（OIDC） | 云 IdP（Entra/Auth0/Cognito） | 服务端 Session |
|---|---|---|---|---|
| 认证能力 | 自己实现（密码/MFA/联邦都要写） | 完整（OIDC/SAML/LDAP、MFA） | 完整，开箱即用 | 自己实现登录 |
| 令牌 | 自签自轮换 | 标准 OIDC，JWKS 现成 | 标准 OIDC | 无令牌，只有 session id |
| 撤销 | 需自建（短 TTL/黑名单/版本号） | 会话与令牌管理内建 | 内建 | 即时 |
| 适合 | 内部服务间、令牌格式可控 | 私有化、合规、已有 LDAP | 快速上线、B2C 规模 | 单体、强一致会话 |

一句话决策：**对外部用户、要 SSO/MFA，选 Keycloak 或云 IdP，别自己写 AS；只在签发内部令牌时用 golang-jwt，且必须配 JWKS 与轮换；要「登出即失效」的强一致会话，服务端 session 仍是最简单正确的答案**。与 Casbin 的分工：**本篇负责「你是谁」——校验令牌、确认主体、把 `sub`/租户/角色注入上下文；Casbin 负责「你能做什么」——拿主体判定 `(sub, obj, act)` 是否放行**。两者在 Gin 里是相邻的两个中间件：认证 `Abort` 于 401，授权 `Abort` 于 403；JWT 里的 `roles` 只是 Casbin 的输入之一，**不要**用令牌里的角色数组替代策略引擎（有效期内无法回收权限）。Casbin 的模型与策略细节见 [casbin-guide](../casbin/casbin-guide.md)，此处不重复。

参考：[golang-jwt 文档](https://pkg.go.dev/github.com/golang-jwt/jwt/v5)、[go-oidc 仓库](https://github.com/coreos/go-oidc)、[x/oauth2 文档](https://pkg.go.dev/golang.org/x/oauth2)、[RFC 9700 OAuth 2.0 Security BCP](https://www.rfc-editor.org/rfc/rfc9700.html)、[OpenID Connect Core 1.0](https://openid.net/specs/openid-connect-core-1_0.html)、[RFC 8725 JWT BCP](https://www.rfc-editor.org/rfc/rfc8725.html)。
