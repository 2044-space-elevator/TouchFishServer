# TFV5 JWT 签名密钥、JWKS 与外部平台接入

> 面向服务器运维与外部平台接入方。接口细节见 [API 账号文档](api/auth.md)；




| 密钥 | 位置 | 用途 | 对外暴露方式 |
| --- | --- | --- | --- |
| 请求信封密钥 | `res/<port>/secret/pri.pem` / `pub.pem` | TFV5 加密信封：客户端用 `pub.pem` 加密 AES key，服务端用 `pri.pem` 解密 | `GET /get_rsa_pub` |
| JWT 签名密钥 | `res/<port>/secret/jwt_keys/<kid>.pem` | 本服签发 access token（RS256/ES256 等），外部平台验签 | `GET /.well-known/jwks.json` |

两者**完全独立**

## 让本服签发 RS256 并由外部平台验签

### 通过管理接口

以 root 身份登录拿到 access token 后，调用加密信封接口（客户端 / SDK 的通用加密请求即可）：

```json
{
  "token": "<root access token>",
  "algorithm": "RS256",
  "issuer": "https://tf.example.com",
  "activate": true
}
```

POST `/auth/manage/signing_keys/rotate`。成功返回 `kid` 与 `signing_algorithm: "RS256"`。
之后新签发的 access token header 会带 `kid`，`GET /.well-known/jwks.json` 立即可见公钥。
存量 HS256 token 继续有效，直到过期或被吊销。

### 通过配置文件

编辑 `res/<port>/config.json`：

```json
{
  "jwt_signing_algorithm": "RS256",
  "jwt_issuer": "https://tf.example.com",
  "jwt_public_base_url": "https://tf.example.com",
  "jwt_key_retention_days": 30,
  "jwt_rsa_key_size": 2048
}
```

重启服务端，或等待下一次服务器设置刷新。首次签发时才会生成 `jwt_keys/` 下的密钥，
因此刚改完配置、还没人登录时 JWKS 可能是空的；登录一次后即有钥。

### 反向代理

`jwt_public_base_url` 为空时，discovery 的 `jwks_uri` 使用请求的 `url_root`。
如果服务端在 Nginx / Caddy 后面，且没有正确配置 `reverse_proxy_enabled` / `proxy_count`，
请显式设置 `jwt_public_base_url`，否则外部平台可能拿到 `http://127.0.0.1:...` 之类的地址。

### 验证

```bash
curl -s https://tf.example.com/.well-known/jwks.json | jq .
curl -s https://tf.example.com/.well-known/openid-configuration | jq .
```

Python 验签示例（只需要公钥，不需要 `jwt_secret`）：

```python
import requests
from jwt import PyJWKClient, decode

jwks_client = PyJWKClient("https://tf.example.com/.well-known/jwks.json")
signing_key = jwks_client.get_signing_key_from_jwt(access_token)
claims = decode(
    access_token,
    signing_key.key,
    algorithms=["RS256"],
    issuer="https://tf.example.touchfish.xin",
    audience="touchfish",   # 仅当服务端配置了 jwt_audience
)
print(claims["sub"], claims["sid"])
```

### 密钥轮换与保留期

- `POST /auth/manage/signing_keys/rotate` 生成新钥并切换；旧钥标记 `retired_at`。
- 旧钥在 `jwt_key_retention_days`（默认 30 天）内仍出现在 JWKS 并可验签；
  超过保留期后，旧私钥文件与索引条目会在下一次轮换/清理时删除。
- 轮换不会踢人；现有会话继续用原 token，直到自然过期。
- 备份：至少备份 `res/<port>/secret/jwt_keys/`（尤其 `index.json` 与 `<kid>.pem`）。
  只备份公钥不够——丢失私钥后旧 token 无法再签发新 token，但已签发 token 在过期前
  仍可被外部平台用已发布的公钥验证（前提是对方缓存了 JWKS）。
- 恢复：把整个 `jwt_keys/` 目录放回原位即可；`index.json` 损坏时会从 `*.pem` 自动重建
  （算法按密钥类型推断：RSA → RS256，EC P-256/P-384/P-521 → ES256/384/512）。

## 外部平台以密钥签发、本服验签

### 最简配置：远程 JWKS

```json
{
  "jwt_external_issuers": [
    {
      "iss": "https://idp.example.touchfish.xin",
      "algorithms": ["RS256", "ES256"],
      "audience": "touchfish",
      "jwks_uri": "https://idp.example.touchfish.xin/.well-known/jwks.json",
      "require_exp": true
    }
  ]
}
```

### 只给 issuer，自动 discovery

```json
{
  "jwt_external_issuers": [
    {
      "iss": "https://login.example.touchfish.xin",
      "algorithms": ["RS256"],
      "discovery": true
    }
  ]
}
```

本服会请求 `https://login.example.touchfish.xin/.well-known/openid-configuration` 并读取 `jwks_uri`。

### 静态公钥（老做法仍兼容）

```json
{
  "jwt_external_issuers": [
    {
      "iss": "https://legacy.example.touchfish.xin",
      "algorithms": ["RS256"],
      "public_key_pem": "res/<port>/secret/legacy_idp.pem"
    }
  ]
}
```

也支持 `public_key_jwk`（单把 JWK）与 `jwks`（静态 JWKS 文档）。优先级：
`public_key_jwk` > `jwks` > `public_key_pem` > `jwks_uri` > `discovery`。

### 接入流程

1. 外部平台用自己的私钥签发 JWT，至少带 `iss`、`sub`、`exp`（建议同时带 `aud`、`kid`）；
2. 客户端把 token 作为 `external_token` 调 `POST /auth/external/login`；
3. 未绑定且未开启 `allow_auto_create` 时返回 `external_not_linked`；可先用本地 token 调
   `POST /auth/external/bind` 绑定；
4. 登录成功后拿到本服 access/refresh token，后续 HTTP 与 WebSocket 都用它。

推荐 `require_exp: true`。默认值 `false` 只是为了兼容升级前没有 `exp` 的老 issuer。

## 未实现（下一阶段）

- 完整 OAuth
- 面向外部应用的 `client_id/client_secret`、scope、API key 与配额
- 自动定时轮换（真的有必要吗
