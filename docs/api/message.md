# TouchFish V5 Api 文档（消息与聊天相关部分）

**请确保在阅读本文档前阅读了[主文档](main.md)。**

TFV5 的消息系统由两部分组成：

1. **REST API**：发送、回复、转发、撤回消息，查询聊天列表和拉取历史消息。
2. **WebSocket 实时推送**：发送和接收即时消息、输入状态提示。

消息在服务端持久化存储，通过 `mid`（消息ID）唯一标识。客户端可通过 `client_mid` 实现去重和发送确认。WebSocket 发送时 `client_mid` 可选，但服务端仅在其非 `null` 时发送 ACK。

---

## 数据结构

### 消息对象

```json
{
    "mid" : <mid>,
    "client_mid" : <client_mid>,
    "sender_uid" : <sender_uid>,
    "receiver_uid" : <receiver_uid>,
    "group_id" : <group_id>,
    "content" : <content>,
    "content_type" : "plain" | "file",
    "file_hash" : <file_hash>,
    "file_name" : <file_name_or_null>,
    "send_time" : <send_time>,
    "quote" : <quote_mid>,
    "forwarded" : <forwarded_mid>,
    "deleted" : 0 | 1,
    "deleted_at" : <deleted_at>,
    "deleted_by" : <deleted_by_uid>,
    "quote_preview" : <quote_preview_or_null>,
    "forward_preview" : <forward_preview_or_null>,
    "file" : <file_metadata_or_null>,
    "mentioned_uids" : [<uid>, ...],
    "room_key" : <room_key>,
    "room_seq" : <room_seq>
}
```

- `<mid>`：服务端分配的消息唯一ID，自增。
- `<client_mid>`：客户端生成的去重标识（可选）。同一 `client_mid` 的消息不会重复存储。
- `<content_type>`：`"plain"` 表示文本消息，`"file"` 表示文件消息。
- `<file_hash>`：文件消息的取件码（参见[文件文档](file.md)）。
- `<file_name>`：文件消息发送时固化的展示名称，用于发送者后来删除个人文件所有权后仍能正确展示历史消息。
- `<quote>`：回复（引用）的消息 `mid`，`-1` 表示不回复其他消息。被回复消息必须属于同一私聊或群聊，且不能已经被撤回。
- `<quote_preview>`：被回复消息的摘要，包含 `mid`、`sender_uid`、`content_type`、最多 240 字符的 `content`、`file_hash`、`deleted` 和 `deleted_at`。目标不存在或不属于同一会话时为 `null`。
- `<forwarded>`：转发来源消息的 `mid`，`-1` 表示不是转发消息。转发来源必须属于同一私聊或群聊，且不能已经被撤回。
- `<forward_preview>`：转发来源摘要，结构与 `quote_preview` 相同。目标不存在或不属于同一会话时为 `null`。
- `<file>`：文件消息的元数据，包含 `hash`、`file_name`、`filename`、`size`、`mime_type`、`extension`、`download_url` 和 `send_time`。非文件消息通常不包含该字段；若发送者后来删除了自己的文件所有权，按发送者查询的 `file_name` 和 `filename` 可能为 `null`。
- `<deleted_at>`、`<deleted_by_uid>`：撤回时间和执行撤回的用户 uid。未撤回时为 `null`。
- 当 `deleted = 1` 时，普通接口仍返回该消息作为撤回占位，但 `content` 和 `file_hash` 固定为 `null`。服务端不会从数据库中移除原始内容。
- `<mentioned_uids>`：消息正文中通过 `@用户名` 提及的用户 uid 列表。仅在 `content_type` 为 `"plain"` 时有值。
- `<room_id>`：聊天室标识，格式为 `"U<uid>"` 或 `"G<gid>"`。
- `<room_key>`：房间规范化 key（私聊为排序后的 `U<min>U<max>`，群聊为 `G<gid>`），用于增量同步。
- `<room_seq>`：房间内单调递增的消息序号（撤回也会递增），用于缺口检测与 `/message/sync` 增量同步。

### 聊天室对象

```json
{
    "room_id" : <room_id>,
    "room_type" : "direct" | "group",
    "partner_uid" : <partner_uid>,
    "username" : <username>,
    "avatar" : <avatar_url>,
    "last_content" : <last_content>,
    "last_content_type" : <content_type>,
    "last_time" : <last_time>,
    "last_sender_uid" : <last_sender_uid>,
    "last_mid" : <last_mid>,
    "last_deleted" : <true_or_false>,
    "last_deleted_at" : <deleted_at>,
    "last_file" : <file_metadata_or_null>,
    "is_friend" : <true_or_false>,
    "is_pinned" : <true_or_false>,
    "notify_level" : <0_or_1_or_2>
}
```

- `is_friend`：`room_type = "direct"` 时表示当前是否仍为好友关系。
- `is_pinned`：当前用户是否将此聊天室置顶。
- `notify_level`：当前用户对此聊天室的通知级别。`0` = 全部通知，`1` = 仅 @提及，`2` = 静音。首次访问或未设置时返回 `null`。
- `last_deleted`：最后一条消息是否已撤回。为 `true` 时 `last_content` 为 `null`。
- `last_file`：最后一条未撤回消息为文件消息时的文件元数据，否则为 `null`。

---

### @提及候选用户

- `^ POST /auth/mention_candidates` 获取当前服务器上可以被 @提及的用户列表。

请求体：

```json
{

}
```

返回体：

```json
[
    {
        "uid" : <uid>,
        "username" : <username>
    }
]
```

过滤掉当前用户及被封禁用户。结果按 `username` 升序排列。

---

### 聊天室偏好设置

- `^ POST /chat/preferences/update` 更新当前用户对指定聊天室的偏好（置顶、通知级别）。

请求体：

```json
{
    "room_id" : <room_id>,
    "is_pinned" : <true_or_false>,
    "notify_level" : <0_or_1_or_2>
}
```

其中：
- `<room_id>`（必填）聊天室标识，格式为 `"U<uid>"`（私聊）或 `"G<gid>"`（群聊）。
- `<is_pinned>`（可选，布尔类型）是否置顶该聊天室。
- `<notify_level>`（可选，整数类型）通知级别：`0` = 全部通知，`1` = 仅 @提及，`2` = 静音。

以上字段至少传入一个，只更新传入的字段。

权限约束：操作者必须与目标私聊用户为好友关系，或为目标群的成员。

返回：成功返回时间戳加 `True`，否则返回时间戳加 `False`。

---

## Secret API（REST）

> 因为这些接口属于 secret 类型，所以请求体仍然需要按照[主文档](main.md)中的 RSA + AES 方式加密。

### 发送消息（统一接口）

- `^ POST /message/send` 发送文本或文件消息。

*提示：在有 WebSocket 连接时，推荐优先使用 WebSocket 发送消息。*

请求体：

```json
{
    "recipient" : <recipient>,
    "content" : <content>,
    "content_type" : <content_type>,
    "client_mid" : <client_mid>,
    "quote" : <quote_mid>,
    "forwarded" : <forwarded_mid>,
    "file_hash" : <file_hash>
}
```

其中：
- `<recipient>`（必填）是接收方，格式为 `"U<uid>"`（私聊）或 `"G<gid>"`（群聊）。
- `<content>`（必填）是消息正文。文本消息传文本内容，文件消息传文件哈希。
- `<content_type>`（可选，默认 `"plain"`）消息类型：`"plain"` 或 `"file"`。
- `<client_mid>`（可选）客户端去重标识。重复提交相同 `client_mid` 不会创建新消息，直接返回已有 `mid`。
- `<quote>`（可选，默认 `-1`）要回复的消息 `mid`。
- `<forwarded>`（可选，默认 `-1`）要转发的消息 `mid`。`quote` 和 `forwarded` 不能同时大于等于 `0`。
- `<file_hash>`（可选，`content_type = "file"` 时必填）文件的取件码。

当 `forwarded >= 0` 时，服务端以转发来源消息为准读取 `content`、`content_type`、`file_hash` 和 `file_name`，不会信任客户端提交的消息正文或文件哈希。当前接口只支持在来源消息所属的同一会话内转发。

校验：
- 操作者必须与接收方为好友关系（私聊），或为该群成员（群聊）。
- `content_type` 仅允许为 `"plain"` 或 `"file"`，否则请求将被拒绝。
- 文本消息长度不得超过 `max_message_length` 配置（默认 10000 字符）。
- 当 `content_type = "file"` 时，`file_hash` 必须为 64 位十六进制字符串（SHA-256 格式），否则请求将被拒绝。
- 文件必须存在，并且发送者必须拥有有效的该文件记录。
- `quote` 指向的消息必须存在、未撤回并属于目标会话。
- `forwarded` 指向的消息必须存在、未撤回并属于目标会话。
- 转发文件消息时，不要求转发者成为文件上传者，但该哈希的**初始上传者**必须仍保有有效文件所有权；初始上传者删除文件后，新的文件转发会失败。详见[文件文档](file.md)。

返回：成功返回以下对象。重复提交也返回相同格式，其中 `mid` 为已有消息 ID。

```json
{
    "mid" : <mid>,
    "client_mid" : <client_mid>,
    "status" : "sent",
    "message" : <message_object>
}
```

失败返回时间戳加 `False`。


### 聊天列表

- `^ POST /chat/list` 获取当前用户的全部聊天室列表（含最后一条消息）。

请求体：

```json
{

}
```

返回体：

```json
[
    {
        "room_id" : "U<uid>" | "G<gid>",
        "room_type" : "direct" | "group",
        "partner_uid" : <uid>,
        "username" : <username>,
        "avatar" : <avatar_url>,
        "last_content" : <last_content>,
        "last_content_type" : "plain" | "file",
        "last_time" : <last_time>,
        "last_sender_uid" : <last_sender_uid>,
        "last_mid" : <last_mid>,
        "last_deleted" : <true_or_false>,
        "last_deleted_at" : <deleted_at>,
        "last_file" : <file_metadata_or_null>,
        "is_friend" : <true_or_false>,
        "is_pinned" : <true_or_false>,
        "notify_level" : <0_or_1_or_2>
    }
]
```

说明：
- 包含所有私聊和群聊会话，按最后消息时间降序排列。
- 尚无消息的好友也会出现在列表中（`last_mid` 为 `null`）。
- `room_type = "direct"` 时 `is_friend` 表示当前是否仍为好友关系。

---

### 历史消息

- `^ POST /message/history` 拉取历史消息（分页）。

*注意：不应频繁调用历史消息接口获取消息，在有 WebSocket 连接时，推荐优先使用实时推送。*

请求体：

```json
{
    "target_uid" : <target_uid>,
    "group_id" : <group_id>,
    "before_mid" : <before_mid>,
    "limit" : <limit>
}
```

其中：
- `<target_uid>`（私聊时必填）对方用户 uid。
- `<group_id>`（群聊时必填）群 gid。
- `target_uid` 和 `group_id` 通常二选一；若同时传入，当前实现优先按 `group_id` 查询群聊历史。
- `<before_mid>`（可选，默认 `0` 即从最新开始）翻页游标，传上次返回结果中最小的 `mid`。
- `<limit>`（可选，默认 `50`，上限 `200`）每次拉取条数。

返回体：

```json
[
    {
        "mid" : <mid>,
        "client_mid" : <client_mid>,
        "sender_uid" : <sender_uid>,
        "receiver_uid" : <receiver_uid>,
        "group_id" : <group_id>,
        "content" : <content>,
        "content_type" : "plain" | "file",
        "file_hash" : <file_hash>,
        "send_time" : <send_time>,
        "quote" : <quote_mid>,
        "forwarded" : <forwarded_mid>,
        "deleted" : 0 | 1,
        "deleted_at" : <deleted_at>,
        "deleted_by" : <deleted_by_uid>,
        "quote_preview" : <quote_preview_or_null>,
        "forward_preview" : <forward_preview_or_null>,
        "file" : <file_metadata_or_null>,
        "mentioned_uids" : [<uid>, ...]
    }
]
```

消息按 `mid` 降序排列（最新在前）。已撤回消息不会被过滤，而是按照“消息对象”一节中的脱敏规则返回占位。`quote_preview` 和 `forward_preview` 分别给出回复目标与转发来源摘要。

---

### 增量同步（v2）

- `^ POST /message/sync` 按房间序号增量拉取消息（含缺口补拉）。

*推荐在有 WebSocket 连接时优先使用实时推送（`MESSAGE.NEW`），本接口用于断线重连后的补齐与序号缺口恢复。*

请求体：

```json
{
    "room_id" : "<room_id>",
    "last_seq" : <last_seq>,
    "last_mid" : <last_mid>,
    "missing_sequences" : [<seq>, ...],
    "missing_sequence_ranges" : [{"start_seq": <s>, "end_seq": <e>}, ...],
    "limit" : <limit>
}
```

其中：

- `<room_id>`（必填）聊天室标识：私聊 `"U<uid>"`，群聊 `"G<gid>"`。
- `<last_seq>`（可选）上次同步到的房间序号；与 `last_mid` 同时提供时优先按 `last_seq`。
- `<last_mid>`（可选）旧客户端迁移期使用：服务端先校验该 `mid` 属于目标房间，再转换为房间序号；已撤回的旧消息会按其新的墓碑序号返回。不存在或跨房间的 `mid` 会拒绝请求。
- `<missing_sequences>` / `<missing_sequence_ranges>`（可选）精确缺口：客户端检测到序号跳跃后按缺失序号/区间补拉。序号列表最多 200 项，区间最多 50 段，每段最多 200 个序号，展开后的去重总数最多 200；超限或格式无效时请求失败。
- `<limit>`（可选，默认 `100`，上限 `200`）每批条数。

返回体：

```json
{
    "messages" : [<消息对象，含 room_seq>],
    "current_seq" : <当前房间最大序号>,
    "has_more" : <是否还有更多>
}
```

消息按 `room_seq` 升序排列。`has_more` 只表示增量查询（不含额外缺口补拉）是否还有下一页；为 `true` 时客户端应以增量结果的最大序号作为 `last_seq` 继续翻页，直到 `has_more` 为 `false`。已撤回消息（墓碑）也会返回，客户端按 `mid` 覆盖本地记录即可。

---

### 撤回消息

- `^ POST /message/recall` 撤回一条消息。

请求体：

```json
{
    "mid" : <mid>
}
```

权限规则：

- 用户可以撤回自己发送的消息。
- 群主和群管理员可以撤回本群任意消息。
- 服务器 `admin` 和 `root` 可以撤回任意私聊或群聊消息。

成功返回：

```json
{
    "success" : true,
    "message" : {
        "mid" : <mid>,
        "client_mid" : <client_mid_or_null>,
        "sender_uid" : <sender_uid>,
        "receiver_uid" : <receiver_uid>,
        "group_id" : <group_id_or_null>,
        "content" : null,
        "content_type" : "plain" | "file",
        "file_hash" : null,
        "file_name" : <file_name_or_null>,
        "send_time" : <send_time>,
        "quote" : <quote_mid>,
        "deleted" : 1,
        "deleted_at" : <deleted_at>,
        "deleted_by" : <deleted_by_uid>
    }
}
```

这里的 `message` 是脱敏后的原始消息记录，不包含 `quote_preview`、`file` 或 `mentioned_uids`。

失败返回 `success = false`，`error` 为 `invalid_request`、`auth_failed`、`not_found`、`forbidden` 或 `already_recalled`。

撤回是软删除：原始正文和文件哈希仍保留在数据库中；普通历史、聊天列表、回复摘要和实时事件只能看到脱敏后的撤回占位。撤回文件消息不会减少文件引用计数。

### 查看被撤回消息原文（Root）

- `^ POST /message/recalled_original` 查看一条被撤回消息的原始记录。

请求体：

```json
{
    "mid" : <mid>
}
```

仅服务器 `root` 可以访问。成功返回：

```json
{
    "success" : true,
    "message" : {
        "mid" : <mid>,
        "client_mid" : <client_mid_or_null>,
        "sender_uid" : <sender_uid>,
        "receiver_uid" : <receiver_uid>,
        "group_id" : <group_id_or_null>,
        "content" : <original_content>,
        "content_type" : "plain" | "file",
        "file_hash" : <original_file_hash_or_null>,
        "file_name" : <file_name_or_null>,
        "send_time" : <send_time>,
        "quote" : <quote_mid>,
        "deleted" : 1,
        "deleted_at" : <deleted_at>,
        "deleted_by" : <deleted_by_uid>,
        "quote_preview" : <quote_preview_or_null>,
        "file" : <file_metadata>
    }
}
```

`quote_preview` 始终存在，无引用时为 `null`；`file` 仅在原消息有 `file_hash` 时存在。该接口不返回 `mentioned_uids`。目标不存在或未被撤回时返回 `not_found`，非 root 返回 `forbidden`。

---

## WebSocket 实时通讯

消息实时通讯使用服务器的 `port_tcp` 端口（参见[主文档](main.md)和[通知文档](notification.md)的"TCP WebSocket 实时推送"部分）。

连接建立和 AES 密钥协商流程与通知推送一致，此处不再赘述。以下仅说明消息相关的 WebSocket 协议。

### 通用约定

- 所有 WebSocket 消息（除心跳外）均经过 AES 加密，封装格式同 [secret 类型 API](main.md#对于-secret-类型)。
- 每条消息可携带 `client_mid`（客户端生成的唯一标识），用于去重和发送确认。该字段可选；服务端仅在 `client_mid` 非 `null` 时发送成功或失败 ACK。
- 服务端对每个用户默认实施 **每秒 10 条**消息的限流。超出限制时返回 `message.ack` 带 `"rate_limited"` 错误。

---

### 心跳

客户端可发送 PING 以保持连接活跃：

请求（secret 加密后）：

```json
{
    "type" : "PING"
}
```

响应（secret 加密后）：

```json
{
    "type" : "PONG"
}
```

心跳消息不计入频率限制。

---

### 发送文本消息（WebSocket）

请求（secret 加密后）：

```json
{
    "type" : "message.plain",
    "client_mid" : <client_mid>,
    "content" : {
        "send_to" : <send_to>,
        "plain" : <plain>,
        "quote" : <quote_mid>
    }
}
```

其中：
- `<send_to>`（必填）是字符串。私聊为 `"U<uid>"`，群聊为 `"G<gid>"`。
- `<plain>`（必填）是消息文本。
- `<quote>`（必填，不引用时传 `-1`）引用的消息 `mid`。
- `<client_mid>`（可选）客户端去重标识。

---

### 发送文件消息（WebSocket）

请求（secret 加密后）：

```json
{
    "type" : "message.file",
    "client_mid" : <client_mid>,
    "content" : {
        "send_to" : <send_to>,
        "file_hashes" : <file_hashes>,
        "quote" : <quote_mid>
    }
}
```

其中 `<file_hashes>` 是文件取件码（参见[文件文档](file.md)）。

---

### 发送确认（ACK）

当发送请求中的 `client_mid` 非 `null` 时，服务端处理消息后返回 ACK 确认包。省略 `client_mid` 或传入 `null` 时仍可发送消息，但服务端不会发送成功或失败 ACK：

```json
{
    "type" : "message.ack",
    "client_mid" : <client_mid>,
    "mid" : <mid>,
    "status" : "sent"
}
```

若消息被拒绝，`status` 为 `"failed"`，同时包含 `error` 字段说明原因：

```json
{
    "type" : "message.ack",
    "client_mid" : <client_mid>,
    "status" : "failed",
    "error" : "not_friends" | "not_group_member" | "rate_limited" | "message_too_long" | "invalid_quote" | "invalid_target" | "invalid_file_hash" | "file_not_owned" | "group_not_found" | "banned"
}
```

错误类型：
- `not_friends` — 对方不是好友
- `not_group_member` — 发送者不在该群中
- `rate_limited` — 超过频率限制（10 条/秒）
- `message_too_long` — 文本超过 `max_message_length`
- `invalid_quote` — 引用消息 ID 非法
- `invalid_target` — 接收方格式非法
- `invalid_file_hash` — 文件哈希不是 64 位十六进制字符串
- `file_not_owned` — 文件不存在或发送者没有有效的文件所有权
- `client_mid_conflict` — 当前 `client_mid` 已用于另一条目标、正文、类型或回复关系不同的消息
- `group_not_found` — 群不存在
- `banned` — 账号已被封禁

`invalid_target` 适用于结构完整但前缀无效的目标。WebSocket 请求缺少 `send_to`、传入空字符串或无法转换的编号时，当前实现可能直接关闭连接而不返回 ACK；客户端应在发送前校验目标格式。

---

### 接收消息推送

消息发送成功后，服务端会向接收方（及发送方本人，私聊时）推送独立的 `MESSAGE.NEW` 事件（v2 起不再走 `NOTIFICATION.NEW`）：

```json
{
    "type" : "MESSAGE.NEW",
    "message" : {
        "event" : "message.plain",
        "title" : "<send_time>",
        "content" : <plain>,
        "sender" : "<sender_id>",
        "meta" : <quote_mid>,
        "mid" : <mid>,
        "client_mid" : <client_mid>,
        "room_id" : "<room_id>",
        "group_id" : <group_id>,
        "room_seq" : <room_seq>,
        "quote_preview" : <quote_preview_or_null>,
        "forwarded" : <forwarded_mid>,
        "forward_preview" : <forward_preview_or_null>,
        "mentioned_uids" : [<uid>, ...],
        "mentions_me" : <true_or_false>,
        "should_alert" : <true_or_false>
    }
}
```

**文件消息** `message` 中为：

```json
{
    "event" : "message.file",
    "title" : "<send_time>",
    "content" : <file_hashes>,
    "sender" : "<sender_id>",
    "meta" : <quote_mid>,
    "mid" : <mid>,
    "file_hash" : <file_hashes>,
    "client_mid" : <client_mid>,
    "room_id" : "<room_id>",
    "group_id" : <group_id>,
    "room_seq" : <room_seq>,
    "quote_preview" : <quote_preview_or_null>,
    "forwarded" : <forwarded_mid>,
    "forward_preview" : <forward_preview_or_null>,
    "file" : <file_metadata>,
    "mentioned_uids" : [],
    "mentions_me" : false,
    "should_alert" : <true_or_false>
}
```

字段说明：
- `<mid>`：服务端消息ID。
- `<client_mid>`：客户端去重标识（与发送时一致）。
- `<room_id>`：聊天室标识。私聊时，接收方看到的 `room_id` 为发送者 uid（`"U<sender_uid>"`），发送方看到的为接收者 uid（`"U<target_uid>"`）。群聊时为 `"G<gid>"`。
- `<group_id>`：群聊时存在，为群 gid。私聊时无此字段。
- `<sender_id>`：私聊为 `"U<uid>"`，群聊为 `"G<gid>U<uid>"`。
- `<meta>`：引用的消息 `mid`。
- `<room_seq>`：房间内单调递增序号。客户端应记录每房间最大序号，检测到序号跳跃时通过 `/message/sync` 的 `missing_sequences`/`missing_sequence_ranges` 补拉。
- `<quote_preview>`：回复目标摘要，结构与 REST 消息对象一致。
- `<forwarded>`、`<forward_preview>`：转发来源消息 ID 与摘要；非转发消息分别为 `-1` 和 `null`。
- `<file>`：文件消息的元数据。
- `<mentioned_uids>`：消息中完整的、经服务端解析出的 @提及用户 uid 列表；发送方收到的副本也保留此列表。文件消息固定为空数组。
- `<mentions_me>`：当前接收用户是否在被提及列表中。发送方收到的推送中固定为 `false`。
- `<should_alert>`：客户端是否应触发通知提醒。由用户的聊天室偏好（`notify_level`）和被提及状态共同决定。发送方收到的推送中固定为 `false`。

### 消息撤回推送

消息撤回后，会向会话内在线用户发送 `MESSAGE.RECALLED`：

```json
{
    "type" : "MESSAGE.RECALLED",
    "message" : {
        "event" : "message.recalled",
        "title" : "<deleted_at>",
        "content" : null,
        "sender" : <operator_uid>,
        "mid" : <mid>,
        "deleted" : true,
        "deleted_at" : <deleted_at>,
    "deleted_by" : <operator_uid>,
    "room_id" : <room_id>,
    "group_id" : <group_id_or_null>,
    "room_seq" : <递增后的房间序号>
}
```

该事件不包含被撤回消息的原始正文或文件哈希。客户端应将对应本地消息改为撤回占位，并清除引用该消息的缓存摘要。撤回同时递增房间 `room_seq`，离线客户端重连后通过 `/message/sync` 收到墓碑记录。

### 消息置顶推送

群内有消息被置顶或取消置顶时，会向群内所有在线成员发送 `NOTIFICATION.NEW`，其 `notification.info` 为：

**置顶消息**：

```json
{
    "event" : "messages.pinned",
    "title" : "消息已置顶",
    "content" : "消息已在群 <group_name> 中置顶。",
    "sender" : <operator_uid>,
    "meta" : {
        "gid" : <gid>,
        "mid" : <mid>,
        "pin_id" : <pin_id>
    },
    "group_id" : <gid_or_null>
}
```

**取消置顶**：

```json
{
    "event" : "messages.unpinned",
    "title" : "消息置顶已取消",
    "content" : "群中的一条消息置顶已被取消。",
    "sender" : <operator_uid>,
    "meta" : {
        "gid" : <gid>,
        "pin_id" : <pin_id>
    },
    "group_id" : <gid_or_null>
}
```

客户端收到这类事件后应刷新该群的置顶消息列表。

### 输入状态

客户端可发送输入状态（"正在输入..."），服务端会广播给聊天室内其他在线成员：

请求（secret 加密后）：

```json
{
    "type" : "typing.start",
    "room_id" : "<room_id>"
}
```

```json
{
    "type" : "typing.stop",
    "room_id" : "<room_id>"
}
```

广播（secret 加密后）：

```json
{
    "type" : "typing.start" | "typing.stop",
    "room_id" : "<room_id>",
    "uid" : <typer_uid>
}
```

群聊时不会广播给发送者本人。

---

## 服务端配置

消息相关配置项可通过 `^ POST /auth/server_settings/update` 设置：

- `max_message_length`：单条消息最大字符数，默认 `10000`，最小为 `1`。
- `min_group_name_length`：群名称最小字符数，默认 `1`，最小为 `1`。
- `max_group_name_length`：群名称最大字符数，默认 `50`，最小为 `1`。

## 注意事项

- 私聊消息仅好友之间可发送。非好友关系会收到 `not_friends` 错误。
- 群聊消息仅群成员可发送。非成员会收到 `not_group_member` 错误。
- 推荐客户端始终携带 `client_mid`，以便在网络重传时做到幂等去重。
