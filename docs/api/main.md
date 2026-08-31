# TouchFishServer V5 Api 文档，主页面

欢迎参阅 TouchFish V5 Api 文档（对于 TouchFishServer V5，下简称 TFV5），对于查阅你需要使用的 Api 前，请仔细阅读该文档，以免造成不必要的误解和麻烦。

本文章主要介绍 TFV5 api 中的约定俗成以及加密规定，作为所有 API 的基础。

因为 TFV5 API 体系较为庞大，如有问题可在 Github 上或 TF 群里咨询。

**错误访问和访问失败可能会返回 API 规定的结果，也可能返回空 JSON 或字符串 `Wrong Requests!`。**

**请不要频繁访问 API，遵循服主要求。**

## 文档目录

- [auth.md](auth.md) 账号相关 API
- [forum.md](forum.md) 论坛相关 API
- [info.md](info.md) 服务器信息 API
- [file.md](file.md) 文件存储 API
- [friend.md](friend.md) 好友相关 API
- [group.md](group.md) 群聊相关 API
- [message.md](message.md) 消息与聊天相关 API
- [notification.md](notification.md) 通知系统与实时推送 API
- [sticker.md](sticker.md) 表情包相关（为什么要单开呢？我也不知道） API

## 文档约定

约定 1：secret 类型的 API 条目前面**不加**星号，public 类型的 API 条目前面**加**星号。

> 例如：
> `* /get_rsa_pub` 获取该服务器的 RSA 公钥。

至于何为 secret 类型 API，何为 public 类型 API，详见[API类型和对应请求方法](#api-类型和对应请求方法)。

约定 2：要带~~用户 uid 和密码~~ 验证信息的 API 条目前面**加** ^ 号，不带~~用户 uid 和密码~~ 验证信息的 API 条目前面**不加** ^ 号。

**~~用户密码~~ 验证信息要以明文字段放在真正的请求体里**，但保证带有 ^ 号的一定不加 * 号。以确保密码传输时仍走 secret 类型加密。

对于约定 2 中如何上传请求体，详见[API类型和对应请求方法](#api-类型和对应请求方法)。

约定 3：API 中 `<uid>` 表示填入用户编码的占位符，`<fid>` 表示填入论坛编码的占位符，`<gid>` 表示填入群聊编码的占位符，`<aid>` 表示填入公告编码的占位符。

## 获取服务器的 RSA 公钥

持有 RSA 公钥是访问 TFV5 api 的必要前提，否则你将无法访问大部分的 TFV5 api（因为它们都需要加密）。

以下是获取 RSA 公钥的 API：

- `* GET /get_rsa_pub` 获取该服务器的 RSA 公钥。

请求体：无。

返回值：PEM 格式的 RSA 公钥文件，文件名为 `<port_api>.pem`，其中 `port_api` 是服务器的 API 端口。

服务器在部署成功后会自动生成公钥哈希值，TFV5 要求部署者将哈希值通过可靠的方式公布于用户中。**为防止中间人攻击，务必对公钥文件进行 SHA256 哈希并校对部署者提供的公钥哈希值**。

## Api 类型和对应请求方法

TFV5 api 分为两类：secret 和 public。

约定4：TFV5 Api 文档中，secret 请求类型一般为 POST，public 请求类型一般为 GET。

对于 secret 请求，请使用 RSA + AES 混合加密，服务器将返回使用你的 AES 密钥加密后的值。对于 public 请求，请明文上传请求体。

### 对于 secret 类型

**务必先获取服务器的 RSA 密钥并校对**。

标准请求体如下：

```json
{
    "iv": <iv>,
    "key": <key>,
    "content" : <content>
}
```

AES 加密需要有初始向量（IV），在 Python 中，它的生成代码是（意味着 iv 的 bytes 形式长度为 16）：

```python
os.urandom(16)
```

**IV 是不需要保密的**，请直接将 IV Base64 编码后再用 UTF-8 解码成字符串并传入 `<iv>`。但请保证每次使用不同的 IV。

再随机生成一个 AES 密钥，AES 鉴于其快速的特点**强烈建议每一次使用不同的 AES 密钥**，在 Python 中，它的生成代码是（意味着 AES 密钥的 bytes 形式长度为 32）：

```python
os.urandom(32)
```

**将你生成的 AES 密钥使用服务器的 RSA 公钥进行 RSA 加密，得到 bytes 串，将该 bytes 串使用 Base64 编码后再用 UTF-8 解码成字符串并传入 `<key>`**。

接着 `Content` 是 secret API 真正要求的请求体，格式为以字符串形式表示的 JSON。

**请将真正的请求体的字符串形式使用你的 AES key 进行 AES 加密，加密后将加密后的 bytes 串使用 Base64 编码后再用 UTF-8 解码成字符串并传入 <content>**。

### 对于 public 类型

依照 api 要求，直接明文传输请求体即可。

### 对于用户令牌类型（要求请求体包含用户的密码）

请按照[对于 secret 类型](#对于-secret-类型)的方法中，将你的请求体做混合加密。

请求体中，除了 API 文档要求的那些，再包含两个键值对，内容为：

```json
{
    "uid" : <uid>,
    "password" : <password>,
    ...
}
```

### JWT 认证（推荐）

在 TFV5 的 5.0.0 以上版本，**推荐使用 JWT 替代在每次请求中携带 `uid` + `password`**。

可极大减少服务器负担并支持更多功能！

1. **登录**：在 `/auth/login` 的加密请求体中加入 `"jwt": true`：

   ```json
   {
       "uid": 0,
       "password": "xxx",
       "jwt": true
   }
   ```

   成功后返回（同样为 AES 加密后的 JSON）：

   ```json
   {
       "token": "<jwt>",
       "expires_in": 604800,
       "expires_at": 1788748172
   }
   ```

   - `expires_in` / `expires_at` 为 token 有效期（秒）与过期时间戳（秒）。
   - 若达到该用户的最大 token 数（服务器配置 `jwt_max_per_user`），返回 `{"error": "token_limit_reached"}`。
   - 凭据错误返回 `{"error": "auth_failed"}`。
   - **注意**：旧版服务器无法识别 `jwt` 字段，会照旧返回 `"<timestamp>True/False"` 字符串——客户端可据此自动降级为旧版认证。

2. **请求携带 token**：所有需要认证的 secret API，请求体中用 `token` 字段代替 `uid` + `password`：

   ```json
   {
       "token": "<jwt>",
       ...
   }
   ```

   token 与 `uid`/`password` 同时存在时，服务器优先校验 token。token 无效或过期时返回 `{"error": "token_expired"}`。

3. **会话探活**：`POST /auth/validate`（加密，携带 token）返回 `{"valid": true, "uid": 0, "stat": "root"}`，用于客户端启动时恢复会话。

4. **吊销**：用户修改密码、被封禁或删除时，已签发的全部 JWT 立即失效（`auth_version` 计数），客户端将收到 `{"error": "token_expired"}` 并需要重新登录。单台设备可随时通过 `/auth/tokens/revoke` 单独吊销（见下文"设备管理"）。

5. **会话数量**：服务器配置 `jwt_max_per_user`（默认 5）限制每用户可同时持有的 token 数量；`jwt_expires_seconds`（默认 604800，即 7 天）控制有效期。两者均可通过 `/auth/server_settings/update` 修改。

6. **设备（Token）管理**：签发时服务器会记录来源 IP 与 User-Agent。

   - `POST /auth/tokens/list`（需认证）列出当前用户的全部活跃 token：

     ```json
     {
         "tokens": [
             {
                 "jti": "<jti>",
                 "issued_at": 1788748172,
                 "expires_at": 1789352972,
                 "ip": "1.2.3.4",
                 "ua": "Mozilla/5.0 ...",
                 "is_current": true
             }
         ],
         "max_per_user": 5
     }
     ```

   - `POST /auth/tokens/revoke`（需认证），请求体 `{"jti": "<jti>"}` 移除指定 token：该 token 立即失效（后续请求返回 `{"error": "token_expired"}`），且其已建立的 WebSocket 连接会被主动断开（即"踢出设备"）。不能移除当前请求所用的 token（返回 `{"error": "current_token"}`）。

### 旧版认证（uid + password）与兼容模式

- 服务器配置 `legacy_auth_enabled`（默认开启）控制是否接受旧版认证。
- 开启时，请求体携带 `uid` + `password` 的 secret API 照常工作；**若返回体为 JSON，将额外包含一个 `note` 字段**：

  ```json
  {
      "note": "using uid and password for general authorization is deprecated, please use the jwt auth (see details in docs)",
      ...
  }
  ```

  > 字符串形式的返回体（如 `"<timestamp>True/False"`）无法携带 `note`。
- 关闭时，旧版认证请求返回 `{"error": "auth_failed"}`；JWT 登录不受影响。

### 对于 secret 类型 API 的返回值

其返回值为：

```
{
    "iv" : <iv>,
    "content": <content>
}
```

服务器会生成一个长度 16 位的 Bytes iv，**并将其 Base64 编码后用 UTF-8 解码成字符串，传入 `<iv>`**。

服务器还会用这个 iv 和请求体中的 AES key 加密 API 文档中规定的返回体的字符串格式，生成的 Bytes **用 Base64 编码后再用 UTF-8 解码成字符串，传入 `<content>`**。

## 对于 secret 类型 API 的测试用例

加密流程有一点复杂，因此举个例子，请将 TFV5 代码包下载，解压，使用终端切换到 `TFV5_server` 目录。

确保你的 Python 环境中有 `cryptography` 库、`Flask` 库与 `requests` 库。

请先启动一个 TFV5 服务端，并确认 `test2.py` 中的 `pub_path` 与 `url` 指向你要测试的服务器；默认示例使用的是本地 `7001` 端口。

运行 `test2.py`，输入所求。

如果在 `test2.py` 的输出看到了发出的请求体，并且看到了解密后的返回体，证明加密 API 通信成功。

开发者可以通过阅读 `test2.py` 的代码来理解 secret API 请求的原理。若需要测试 TCP WebSocket，可参考 `test3.py`。
