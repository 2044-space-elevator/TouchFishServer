# TouchFish V5 Api 文档（好友相关部分）

**请确保在阅读本文档前阅读了[主文档](main.md)。**

TFV5 的好友系统基于申请-审核机制：一方发起好友申请后，关系进入 `pending` 状态，另一方处理（通过或拒绝）后结束流程。

申请规则：

- 只要双方不存在拉黑，**随时可以重复申请**（即使处于 `pending` 或曾经被拒绝）。
- 数据上**只保留最新一条 `pending`**：同一人重复申请就地更新留言与时间，不新增记录；
  对方反过来申请时申请者翻转（由原申请者处理）。
- 重复申请不会重复推送通知（首次申请、以及翻转后的新申请会推送）。
- 已有 `friend` 关系不可再申请。

## Secret API

> 因为这些接口属于 secret 类型，所以请求体仍然需要按照[主文档](main.md)中的 RSA + AES 方式加密。

- `^ POST /friend/add_friend` 向其他用户发起好友申请。

请求体：

```json
{
    "added" : <added_uid>,
    "req_word" : <request_message>
}
```

其中 `<added_uid>` 是被添加用户的 uid，`<req_word>`（可选）是申请留言，
长度上限为服务器配置 `max_request_message_length`（默认 200），超长返回 `request_message_too_long`。
留言会保存在关系记录中并随申请展示给接收方。

返回：申请结果仍按 bool 约定返回——成功返回时间戳加 `True`，被拒（如已成为好友）返回时间戳加 `False`。

失败特例（**仅当请求声明了 `detail_error: true`**，未声明时按[错误文档](errors.md)回 bool 接口的纯文本；
`detail_error` 模式下为规范化错误码）：
- 对方已拉黑你：`{"success": false, "error": "friend_blocked"}`（`FRIEND_BLOCKED`）；
- 鉴权失败：`{"success": false, "error": "auth_failed"}`；
- 目标用户不存在：`{"success": false, "error": "not_found"}`；
- 申请留言超长：`{"success": false, "error": "request_message_too_long"}`。
成功且为新申请（含翻转）时会向对方推送 `friend.request` 事件（参见[通知文档](notification.md)，meta 含 `message`）。

---

- `^ POST /friend/requests` 查看发给当前用户的待处理好友申请（不含自己发出的）。

请求体：

```json
{

}
```

返回体（按申请时间倒序）：

```json
[
    {
        "uid" : <applicant_uid>,
        "username" : <applicant_username>,
        "sign" : <applicant_personal_sign>,
        "message" : <request_message>,
        "request_at" : <timestamp>
    }
]
```

该列表是申请的持久视图（通知被处理后仍可查看，直到申请被处理）。

---

- `^ POST /friend/deal_ship` 处理好友申请（通过或拒绝）。

请求体：

```json
{
    "dealt" : <dealt_uid>,
    "stat" : <stat>
}
```

其中 `<dealt_uid>` 是发起好友申请的用户的 uid，`<stat>` 为 `"allow"` 或 `"reject"`。

- `"allow"`：通过申请，关系变为 `friend`。
- `"reject"`：拒绝申请，关系记录被删除（之后任一方可以重新申请）。

返回：若操作成功，返回时间戳加 `True`，否则返回时间戳加 `False`。

成功通过时会向对方推送 `friend.accepted` 事件（参见[通知文档](notification.md)）。

---

- `^ POST /friend/block` 拉黑用户。

请求体：

```json
{
    "target" : <target_uid>
}
```

**破坏性操作**：现有关系（含好友关系与进行中的申请）会被覆盖为 `blocked`；
被拉黑者无法再向拉黑方发起好友申请，也无法向拉黑方发送私聊消息（沿用好友校验）。解除拉黑不会自动恢复好友关系。

- `^ POST /friend/unblock` 解除拉黑（幂等）。

请求体同上。仅清除自己的拉黑标记；双方都解除后关系记录被删除，可重新申请。
未拉黑时调用为幂等空操作。

- `^ POST /friend/relationship` 查询自己与目标用户的当前关系（含拉黑方向）。

请求体：

```json
{
    "target" : <target_uid>
}
```

响应：

```json
{
    "success" : true,
    "relationship" : "none" | "pending" | "friend" | "blocked",
    "is_friend" : <true_or_false>,
    "blocked_by_me" : <true_or_false>,
    "blocked_by_peer" : <true_or_false>
}
```

拉黑是双向共用一个 `friendship` 行，申请失败时只回 `friend_blocked`，分不清是谁拉黑了谁；
资料页用 `blocked_by_me` 决定显示「拉黑」还是「解除拉黑」。目标不存在或 uid 非法返回
`{"success": false, "error": "invalid_request"}`。

- `^ POST /friend/blocked` 列出自己拉黑的用户（不含拉黑自己的）。

响应：

```json
{
    "success" : true,
    "blocked" : [ <uid>, ... ]
}
```

只包含 `blocked_by_userX` 指向自己的一方——拉黑自己的人才需要对方来解除，所以不出现在这里。

本端点按 uid 限流（默认每 10 秒 30 次），超出返回 `{"success": false, "error": "rate_limited"}`。

- `^ POST /friend/presence` 批量查询好友在线状态（不推送、不持久化）。需全服 `presence` 开关开启。

请求体：

```json
{
    "uids" : [<uid>, ...]
}
```

- `<uids>`（可选）要查询的 uid 列表；缺省表示全部好友。服务端取「请求集合 ∩ 好友」，**非好友的 uid 不会返回任何状态**（天然防止任意探测）。

本端点按 uid 限流（默认每 10 秒 30 次），超出返回 `{"success": false, "error": "rate_limited"}`（`detail_error` 模式下为 `RATE_LIMITED`）。

响应：

```json
{
    "success" : true,
    "presence" : {
        "<uid>" : "online" | "offline"
    }
}
```

已关闭「展示在线状态」的好友不会出现在 `presence` 中（调用方按未知处理即可）。

**实时推送**：好友上下线时，其在线好友会实时收到 WebSocket 事件：

```json
{
    "type" : "presence.update",
    "uid" : <上下线用户uid>,
    "status" : "online" | "offline" | "hidden",
    "ts" : <服务器毫秒时间戳>
}
```

- `online` / `offline`：好友上线/下线（下线有 5s 宽限以覆盖移动端重连抖动）。
- `hidden`：好友关闭了「展示在线状态」，收到后应清除其在线指示。
- 关闭在线状态的用户不会触发任何上下线事件。

## 注意事项

- 无法对自己发起好友申请。
- 收到申请的一方才能处理该申请（申请者不能自行通过）。
- 只有 `pending` 状态可被处理；翻转后由被申请方处理。
