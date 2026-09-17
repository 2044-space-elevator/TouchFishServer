# TouchFish V5 Api 文档（文件存储相关部分）

**请确保在阅读本文档前阅读了[主文档](main.md)。**

TFV5 的文件存储系统基于哈希去重和用户配额管理。同一文件可被多个用户上传，但分别计入各自的存储额度。

## 概念说明

- 哈希：上传时对文件内容计算 SHA256，相同内容的文件共享同一物理存储。
- 引用计数 (`ref_count`)：文件的总保留引用数，包括有效的用户所有权引用，以及聊天消息、论坛帖子等内容引用。
- 上传用户计数 (`upload_user_count`)：拥有该文件有效所有权的用户数，仅统计所有权，不包含聊天或论坛内容引用。用户主动删除所有权时，仍有内容引用的文件会被保留。
- 初始上传者 (`sender`)：首次将该哈希登记到服务器的用户。后续其他用户上传相同内容时共享物理文件，但不会改变 `sender`。
- 存储配额 (`user_storage_quota`)：每个用户的存储空间上限，单位为字节。`-1` 表示不限。

## 公开 API

- `GET /file/get_file/<hashes>` 下载文件。

无需加密，直接访问。文件不存在或已被清理时返回 404。


新版本支持 重定向下载。

当服务器启用 OSS2 对象存储且 `file_download_mode` 为 `redirect`（默认值）时，该接口返回 `307 Temporary Redirect`，`Location` 指向 OSS2 的预签名直链，客户端应跟随重定向直接从对象存储下载（字节不再经过 TFV5 服务器）。预签名直链有效期 15 分钟，且已携带与代理下载一致的 `Content-Disposition`（含原始文件名，中文等非 ASCII 文件名使用 RFC 5987 编码）与 `Content-Type`。

重定向模式的已知差异：

- 响应不再携带服务端生成的 `ETag` / `Last-Modified` / `Cache-Control` 头（由对象存储侧决定）。
- 若文件仍登记在数据库中但对象已在存储桶中被外部删除，将返回对象存储的 404 响应（XML 格式），而非服务端的空 404。
- 未启用 OSS2 的本地存储模式永远走服务端直接发送（不重定向）。

`file_download_mode` 设为 `proxy` 时和旧版相同。

- `GET /file/get_file_info/<hashes>` 获取文件基本信息。

无需加密。返回体：

```json
{
    "sender" : <sender_uid>,
    "file_name" : <file_name>,
    "filename" : <file_name>,
    "send_time" : <send_time>,
    "hash" : <hash>,
    "ref_count" : <ref_count>,
    "last_ref_time" : <last_ref_time>,
    "size" : <size>,
    "mime_type" : <mime_type>,
    "extension" : <extension>,
    "upload_user_count" : <upload_user_count>,
    "download_url" : "/file/get_file/<hash>"
}
```

## Secret API

> 因为这些接口属于 secret 类型，所以请求体仍然需要按照[主文档](main.md)中的 RSA + AES 方式加密。

### 上传文件

- `^ POST /file/upload_file` 上传文件。

请求体：

```json
{
    "filename" : <filename>,
    "file_b64" : <file_b64>
}
```

其中 `<filename>` 是文件名，`<file_b64>` 是文件内容的 Base64 编码。

返回：若成功，返回文件哈希及下载地址：

```json
{
    "success" : true,
    "result" : "<timestamp>True",
    "hash" : <hash>,
    "download_url" : "/file/get_file/<hash>",
    "info_url" : "/file/get_file_info/<hash>",
    "file" : {
        "hash" : <hash>,
        "file_name" : <file_name>,
        "filename" : <file_name>,
        "size" : <size>,
        "mime_type" : <mime_type>,
        "extension" : <extension>,
        "download_url" : "/file/get_file/<hash>",
        "send_time" : <send_time>
    }
}
```

上传前会检查：
- 用户是否被 ban
- 文件大小是否超过 `max_file_size` 限制
- 文件名和扩展名是否合法
- 用户存储配额是否足够（若文件内容已存在且用户已有该文件则不重复计费）

### 秒传

- `^ POST /file/instant_upload` 使用文件的 SHA256 哈希登记用户文件，避免重复上传已经存在于服务器上的文件。

请求体：

```json
{
    "filename": "example.png",
    "file_hash": "<64 位十六进制 SHA256>",
    "detail_error": true
}
```

也可以使用兼容字段 `hash` 代替 `file_hash`。请求还需要按照主文档携带认证信息：旧版认证使用 `uid` 和 `password`，JWT 认证使用 `token`。

服务端会依次检查用户身份、封禁状态、文件名和扩展名、哈希格式，以及文件是否已在本地或对象存储中存在。文件不存在时不会报错，而是返回 `instant: false`，调用方应改用普通上传或分块上传。

文件已存在并成功登记时返回：

```json
{
    "success": true,
    "instant": true,
    "file_hash": "<sha256>",
    "hash": "<sha256>",
    "download_url": "/file/get_file/<sha256>",
    "info_url": "/file/get_file_info/<sha256>",
    "file": {
        "hash": "<sha256>",
        "file_name": "example.png",
        "size": 1024,
        "mime_type": "image/png",
        "extension": ".png"
    }
}
```

文件不存在，或文件登记失败但没有发生参数、权限或配额错误时返回：

```json
{
    "success": true,
    "instant": false
}
```

可能的详细错误包括：`AUTH_FAILED`、`AUTH_INVALID_PASSWORD`、`RESOURCE_USER_NOT_FOUND`、`RESOURCE_USER_BANNED`、`VALIDATION_INVALID_FILENAME`、`VALIDATION_EXTENSION_NOT_ALLOWED`、`VALIDATION_INVALID_FILE_HASH`、`FILE_STORAGE_QUOTA_EXCEEDED`、`VALIDATION_MISSING_PARAMETER` 和 `SERVER_ERROR`。详细错误格式参见[错误码文档](errors.md)。

### 查看用户文件

- `^ POST /file/get_user_files` 查看当前用户的所有已上传文件（仍存在的）。

请求体：无（仅需 `uid` 和 `password`）。

返回体：

```json
[
    {
        "hash" : <hash>,
        "file_name" : <file_name>,
        "upload_time" : <upload_time>,
        "size" : <size>,
        "ref_count" : <ref_count>,
        "upload_user_count" : <upload_user_count>,
        "mime_type" : <mime_type>,
        "extension" : <extension>,
        "download_url" : "/file/get_file/<hash>"
    }
]
```

### 查看存储信息

- `^ POST /file/get_storage_info` 查看当前用户的存储使用情况。

请求体：无（仅需 `uid` 和 `password`）。

返回体：

```json
{
    "quota" : <quota>,
    "used" : <used>,
    "remaining" : <remaining>
}
```

其中 `<quota>` 为配置的用户存储配额（-1 表示不限），`<used>` 为已用字节数，`<remaining>` 为剩余可用字节数。

### 删除用户文件

- `^ POST /file/delete_file` 从当前用户的存储中删除文件。

请求体：

```json
{
    "hash" : <hash>
}
```

返回：成功返回时间戳加 `True`。

删除行为：
- 将用户与该文件的关联标记为无效（不再计入存储额度）
- 移除一条有效所有权引用，同时减少该文件的 `upload_user_count` 和 `ref_count`
- 若 `upload_user_count` 和 `ref_count` 均降为 0，文件可从服务器磁盘移除
- 聊天消息或论坛帖子仍引用文件时，即使用户删除个人所有权，物理文件仍会保留
- 若删除者是该哈希的初始上传者，已有消息和附件引用仍可保留物理文件，但不能再从消息创建新的文件转发；其他用户即使仍拥有相同哈希，也不会取代初始上传者身份

### 消息文件转发约束

文件消息转发由消息接口处理，参见[消息文档](message.md)。服务端不会把转发者登记为文件上传者，也不会绕过普通文件发送的所有权检查：

- 普通发送文件时，发送者必须在 `user_file` 中拥有该哈希的有效记录。
- 转发已有文件消息时，服务端根据来源消息读取文件哈希，并检查 `file.sender` 对应初始上传者的 `user_file` 记录仍为有效状态。
- 初始上传者删除文件后，新的转发请求失败，即使其他用户仍拥有相同哈希。
- 校验初始上传者状态和增加 `ref_count` 在同一个数据库锁与事务中完成。
- 已经发送或转发成功的历史消息引用不会因为初始上传者随后删除个人所有权而立即移除；只禁止创建新的转发引用。

### 减少引用

- `^ POST /file/dereference_file` 减少文件引用计数。

请求体：

```json
{
    "hash" : <hash>
}
```

返回：成功返回时间戳加 `True`。

*调用者必须仍然拥有该文件；不能减少其他用户文件的引用计数。*

## 管理员 API

> 以下接口仅 admin 或 root 用户可访问。

### 查看所有用户文件

- `^ POST /file/admin_get_all_files` 查看所有用户的已上传文件（或指定用户的文件）。

请求体：

```json
{
    "uid" : <operator_uid>,
    "password" : <operator_password>,
    "target_uid" : <optional, 不填则查询所有用户>
}
```

返回体：

```json
[
    {
        "uid" : <file_owner_uid>,
        "username" : <file_owner_username>,
        "hash" : <hash>,
        "file_name" : <file_name>,
        "upload_time" : <upload_time>,
        "size" : <size>,
        "ref_count" : <ref_count>,
        "upload_user_count" : <upload_user_count>,
        "sender" : <original_sender_uid>,
        "mime_type" : <mime_type>,
        "extension" : <extension>,
        "download_url" : "/file/get_file/<hash>"
    }
]
```

### 强行删除文件

- `^ POST /file/admin_force_delete_file` 强行删除文件，忽略引用计数和上传用户计数。

请求体：

```json
{
    "uid" : <operator_uid>,
    "password" : <operator_password>,
    "hash" : <hash>
}
```

返回：成功返回时间戳加 `True`。

删除行为：
- 立即从服务器磁盘删除文件
- 删除数据库中的文件记录
- 删除所有用户与该文件的关联记录（`user_file` 表）
- **不检查引用计数**，即使文件仍被帖子或消息引用也会被删除

## 管理员配置

管理员可通过 `^ POST /auth/server_settings/update` 配置存储相关参数：

- `max_file_size`：单个文件最大上传大小（字节），`-1` 不限。
- `user_storage_quota`：每个用户的存储配额（字节），`-1` 不限。新增于 TFV5。
- `file_download_mode`：下载模式，`redirect`（默认，OSS2 模式下返回 307 预签名直链）或 `proxy`（服务端中转）。仅在启用 OSS2 对象存储时生效；可随时切换，无需重启。

此外 `file_last_time` 控制引用计数为 0 且超时的文件自动清理（单位：小时）。

## 自动清理机制

两种情况下文件会被清理：

1. **无拥有者且无引用**：当 `upload_user_count = 0` 且 `ref_count = 0` 时，文件可立即从服务器删除。
2. **总保留引用计数为 0 且超时**：当 `ref_count = 0` 且超过 `file_last_time` 小时，服务端可清理文件，并将仍残留的关联用户文件标记为无效。

消息撤回不会减少文件引用，因为 root 仍可查看撤回记录。帖子删除、用户内容清理等真正移除引用关系的操作才会减少对应引用计数。管理员强制删除是例外，会忽略所有引用。

服务器启动时会根据有效用户所有权、聊天消息和论坛附件重新校准 `upload_user_count` 与 `ref_count`，以兼容旧版数据库并防止历史附件被误清理。
