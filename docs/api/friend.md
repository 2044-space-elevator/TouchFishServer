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

返回：若操作成功，返回时间戳加 `True`，否则返回时间戳加 `False`。

失败特例：对方已拉黑你时返回 `{"success": false, "error": "friend_blocked"}`（detail_error 模式下为 `FRIEND_BLOCKED`）。
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

## 注意事项

- 无法对自己发起好友申请。
- 收到申请的一方才能处理该申请（申请者不能自行通过）。
- 只有 `pending` 状态可被处理；翻转后由被申请方处理。
