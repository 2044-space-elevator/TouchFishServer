# TouchFish V5 Api 文档（搜索）

**请确保在阅读本文档前阅读了[主文档](main.md)。**

搜索接口均为 secret 类型，请求体需按[主文档](main.md)中的 RSA + AES 方式加密，并要求登录。

所有搜索接口都有**按 uid 的内存限流**（默认每 10 秒最多 20 次），超出返回 `{"success": false, "error": "rate_limited"}`（detail_error 模式下为 `RATE_LIMITED`）。
关键词长度不得小于服务器配置 `min_search_length`（默认 2），过短返回 `search_keyword_too_short`（`VALIDATION_SEARCH_KEYWORD_TOO_SHORT`）。

---

- `^ POST /user/search` 按用户名搜索用户。

请求体：

```json
{
    "keyword" : <keyword>,
    "limit" : <limit>
}
```

`<limit>`（可选，默认 20）为返回上限，最大 20。

返回体（前缀匹配优先，其次按用户名升序）：

```json
[
    {
        "uid" : <uid>,
        "username" : <username>,
        "sign" : <personal_sign>
    }
]
```

- 结果中**不包含 email**。
- 排除封禁用户（`stat == "banned"`）与当前用户自己。
- 匹配为大小写不敏感的模糊匹配，`%` 与 `_` 会被转义为字面量。

---

- `^ POST /group/search` 按群名搜索群组（分页）。

请求体：

```json
{
    "keyword" : <keyword>,
    "limit" : <limit>,
    "offset" : <offset>
}
```

`<offset>`（可选，默认 0）用于分页。

返回体：

```json
{
    "success" : true,
    "groups" : [
        {
            "gid" : <gid>,
            "groupname" : <groupname>,
            "introduction" : <introduction>,
            "member_count" : <member_count>,
            "allow_direct_join" : <bool>,
            "require_review" : <bool>,
            "public_messages" : <bool>
        }
    ],
    "has_more" : <bool>
}
```

- **不返回成员/管理员 UID 列表**。
- `has_more` 表示在本次 `offset + limit` 之后是否还有更多结果。

## 兼容说明

`* GET /group/groupname_search/<groupname>` 为旧版明文接口，**已废弃**，仅为兼容老客户端保留。
新客户端请使用 `POST /group/search`。
