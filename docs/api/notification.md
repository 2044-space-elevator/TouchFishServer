# TouchFish V5 Api 文档（通知相关部分）

**请确保在阅读本文档前阅读了[主文档](main.md)。**

TFV5 的通知系统由两部分组成：

1. Secret API：查询、清理持久化通知，以及服务端已读状态管理。
2. TCP WebSocket：推送实时通知。

需要注意的是，WebSocket 只负责推送新通知，不会在登录后自动补发历史通知。客户端应先通过查询 API 拉取历史通知，再保持 TCP 连接接收后续推送。

## 消息与通知分离

注意：TouchFish 已重构通知系统！旧版将不保证兼容性！

新协议中聊天消息不再写入通知系统：

- 消息实时推送将使用 `MESSAGE.NEW`（见[消息文档](message.md)），历史/断线补拉 `/message/sync`；
- 通知表（notifications）将只保存系统事件（好友、群聊、论坛、公告等），并新增服务端已读状态（`read_at`）与事件类别（`kind`，消息事件=1，系统事件=0）；
- 旧版本写入通知表的消息事件记录会保留在库中（不删除数据），但所有查询 API 均不再返回。


## 通知数据结构

无论是查询 API 还是实时推送，单条通知都使用下面的结构：

```json
{
    "id" : <id>,
    "time_stamp" : <time_stamp>,
    "read_at" : <read_at_or_null>,
    "info" : {
        "event" : <event>,
        "title" : <title>,
        "content" : <content>,
        "sender" : <sender_uid>,
        "meta" : <meta>
    }
}
```

其中：

- `<id>` 是通知唯一 ID，用于单条已读。
- `<time_stamp>` 是通知生成时间，为时间戳。
- `<read_at>` 是服务端已读时间，未读时为 `null`。
- `<event>` 是事件类型。
- `<title>` 是通知标题。
- `<content>` 是通知正文。
- `<sender_uid>` 是通知触发者 uid，没有触发者时也可能为空。
- `<meta>` 是附加信息，通常包含论坛、群聊、公告等上下文编号。

当前内置事件类型有：

- `friend.request`
- `friend.accepted`
- `auth.stat.changed`
- `forum.approved`
- `forum.rejected`
- `forum.review.submitted`
- `forum.review.pending`
- `forum.comment.created`
- `forum.comment.mentioned`
- `forum.post.mentioned`
- `forum.post.deleted`
- `forum.comment.deleted`
- `announcement.created`
- `announcement.edited`
- `group.admin.added`
- `group.member.removed`
- `group.admin.removed`
- `group.deleted`
- `group.owner.transferred`
- `group.join.request`
- `group.invited`
- `group.invited.pending`
- `group.join.approved`

消息事件（`message.plain` / `message.file` / `message.recalled`）已不再出现在通知系统中。

## Secret API

> 因为这些接口属于 secret 类型，所以请求体仍然需要按照[主文档](main.md)中的 RSA + AES 方式加密。

- `^ POST /notification/query_all` 查询当前用户的全部通知。

*注意：不应频繁调用通知接口实现通知收取，在有 WebSocket 连接时，推荐优先使用实时推送。*

请求体：

```json
{

}
```

返回体：

```json
[
    {
        "time_stamp" : <time_stamp>,
        "info" : {
            "event" : <event>,
            "title" : <title>,
            "content" : <content>,
            "sender" : <sender_uid>,
            "meta" : <meta>
        }
    }
]
```

- `^ POST /notification/query_after` 查询指定时间戳之后的通知。

*注意：不应频繁调用通知接口实现通知收取，在有 WebSocket 连接时，推荐优先使用实时推送。*

请求体：

```json
{
    "time_stamp" : <time_stamp>
}
```

返回体同上，只不过只返回比 `<time_stamp>` 更新的通知。

- `^ POST /notification/delete_before` 删除某个时间戳及之前的所有通知。

请求体：

```json
{
    "time_stamp" : <time_stamp>
}
```

返回体：删除成功返回时间戳加 `True`，否则返回时间戳加 `False`。

- `^ POST /notification/delete_all` 删除当前用户的全部通知。

请求体：

```json
{

}
```

返回体：删除成功返回时间戳加 `True`，否则返回时间戳加 `False`。

- `^ POST /notification/unread_count` 查询未读通知数（仅系统事件）。

请求体：

```json
{

}
```

返回体：

```json
{
    "count" : <未读数>
}
```

- `^ POST /notification/mark_read` 将通知标记为已读（服务端写入 `read_at`）。

请求体（二选一）：

```json
{
    "time_stamp" : <时间戳>
}
```

将 `<time_stamp>` 及之前的系统通知全部标为已读；或：

```json
{
    "ids" : [<通知 id>, ...]
}
```

按 id 单条标记。返回体：

```json
{
    "success" : true,
    "changed" : <影响行数>
}
```

- `^ POST /notification/mark_all_read` 将全部系统通知标记为已读。

请求体：

```json
{

}
```

返回体同 `mark_read`。

- `^ POST /notification/list` 分页查询系统通知（按时间倒序）。

请求体：

```json
{
    "offset" : <偏移>,
    "take" : <每页条数，默认 50，最大 100>
}
```

返回体：

```json
{
    "items" : [<通知结构>, ...],
    "total" : <总条数>,
    "has_more" : <是否还有下一页>
}
```

## 数据迁移说明

升级到 v2 时，服务端自动完成以下无损迁移：

- `messages` 表新增 `room_key`（房间规范化 key）与 `room_seq`（房间内单调递增序号），旧数据按 `mid` 升序回填，不删除任何消息；
- `notifications` 表新增 `read_at`（已读时间）与 `kind`（0=系统事件，1=消息事件）列；旧记录按事件类型回填，旧消息事件**保留在库中但不再展示**；
- 旧通知一律视为已读（`read_at = time_stamp`），升级后未读数从 0 开始。

## TCP WebSocket 实时推送

通知实时推送使用服务器的 `port_tcp` 端口，端口值可通过 `GET /info` 获取。

连接地址格式如下：

```text
ws://<server_host>:<port_tcp>
```

### 建立连接

建立连接后，请按如下顺序发送数据：

1. 明文发送 AES 密钥更新包：

```json
{
    "type" : "REQ.UPDATE_AES_KEY",
    "aes_key" : <rsa_encrypted_aes_key_base64>
}
```

2. 再发送一个 secret 类型的加密请求，请求体解密后应为：

```json
{
    "type" : "AUTH.LOGIN",
    "uid" : <uid>,
    "password" : <password>
}
```

如果登录成功，服务器会返回一个 secret 类型的加密响应，解密后内容如下：

```json
{
    "type" : "AUTH.LOGIN_SUCCEEDED"
}
```

### 接收实时通知

登录成功后，服务器会主动推送新的通知。推送包也是 secret 类型的加密响应，解密后格式如下：

```json
{
    "type" : "NOTIFICATION.NEW",
    "notification" : {
        "time_stamp" : <time_stamp>,
        "info" : {
            "event" : <event>,
            "title" : <title>,
            "content" : <content>,
            "sender" : <sender_uid>,
            "meta" : <meta>
        }
    }
}
```

客户端收到 `NOTIFICATION.NEW` 后，可以直接展示，也可以用其中的 `time_stamp` 配合 `query_after` 或 `delete_before` 做本地同步与清理。

### 聊天消息的实时推送（v2）

自 v2 起，聊天消息不再通过 `NOTIFICATION.NEW` 推送，改为独立的 `MESSAGE.NEW` 事件：

```json
{
    "type" : "MESSAGE.NEW",
    "message" : {
        "event" : "message.plain",
        "title" : "<send_time>",
        "content" : <content>,
        "sender" : "<sender_id>",
        "mid" : <mid>,
        "client_mid" : <client_mid>,
        "room_id" : "<room_id>",
        "group_id" : <group_id>,
        "room_seq" : <房间序号>,
        "quote_preview" : <quote_preview_or_null>,
        "forwarded" : <forwarded_mid>,
        "forward_preview" : <forward_preview_or_null>,
        "mentioned_uids" : [<uid>, ...],
        "mentions_me" : <true_or_false>,
        "should_alert" : <true_or_false>
    }
}
```

`room_seq` 是该房间内单调递增的消息序号，客户端据此检测缺口并通过 `/message/sync` 补拉（详见[消息文档](message.md#增量同步)）。文件消息为 `"event": "message.file"`，字段与旧协议一致。

#### 消息撤回推送（MESSAGE.RECALLED）

撤回成功后会推送：

```json
{
    "type" : "MESSAGE.RECALLED",
    "message" : {
        "event" : "message.recalled",
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
}
```

撤回还会使 `messages` 表中该消息所在房间的 `room_seq` 递增，离线客户端重连后通过 `/message/sync` 获取记录（不再残留原文）。

### 发送消息与确认

TFV5 在 WebSocket 中支持发送文本和文件消息，可带引用。

每条消息可携带 `client_mid`（客户端生成的唯一标识），该字段可选。仅当 `client_mid` 非 `null` 时，服务端才会返回 `message.ack` 确认包（成功时含服务端 `mid`），并利用 `client_mid` 对重传做去重；省略或传入 `null` 时仍可发送消息，但不会收到成功或失败 ACK。

服务端对每个用户实施每秒 10 条消息的限流。详情参见[消息文档](message.md)。

**文本消息**格式如下（封装格式参考 secret 类型）：

```json
{
    "type" : "message.plain",
    "client_mid" : "<client_mid>",
    "content" : {
        "send_to" : "<id>",
        "plain" : "<content>",
        "quote" : <mid>
    }
}
```

其中 `<id>` 是**字符串**：
- 发给用户：`"U<Uid>"`，例如 `"U0"`
- 发到群聊：`"G<Gid>"`

不引用时 `<mid>` 为 `-1`。

**文件消息**格式如下：

```json
{
    "type" : "message.file",
    "client_mid" : "<client_mid>",
    "content" : {
        "send_to" : "<id>",
        "file_hashes" : "<hashes>",
        "quote" : <mid>
    }
}
```

`<hashes>` 是文件取件码，由上传文件 API 返回。

### 发送确认（message.ack）

仅当发送请求中的 `client_mid` 非 `null` 时，服务端处理消息后返回 ACK：

```json
{
    "type" : "message.ack",
    "client_mid" : "<client_mid>",
    "mid" : <mid>,
    "status" : "sent"
}
```

若消息被拒绝：

```json
{
    "type" : "message.ack",
    "client_mid" : "<client_mid>",
    "status" : "failed",
    "error" : "<error_code>"
}
```

错误码包括：`not_friends`、`not_group_member`、`rate_limited`、`message_too_long`、`invalid_quote`、`invalid_target`、`invalid_file_hash`、`file_not_owned`、`client_mid_conflict`、`group_not_found`、`banned`。详见[消息文档](message.md)。

客户端必须保证 `send_to` 是非空的 `"U<uid>"` 或 `"G<gid>"`。缺少字段、空字符串或不可转换的编号属于畸形协议包，当前实现可能关闭连接而不返回 ACK。

### 输入状态（typing.start / typing.stop）

客户端发送输入状态，服务端广播给聊天室内其他在线成员（群聊时不广播给自己）：

请求：
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

广播：
```json
{
    "type" : "typing.start",
    "room_id" : "<room_id>",
    "uid" : <typer_uid>
}
```

### 心跳（PING / PONG）

保持连接活跃：

请求：
```json
{
    "type" : "PING"
}
```

响应：
```json
{
    "type" : "PONG"
}
```

心跳不计入消息频率限制，但心跳仍然有频率限制。
