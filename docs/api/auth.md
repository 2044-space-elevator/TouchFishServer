# TouchFish V5 Api 文档（账号相关部分）

**请确保在阅读本文档前阅读了[主文档](main.md)。**

- `^ POST /auth/login` 校验用户密码与账号是否对应（请注意：该 API 携带的验证信息一定为用户名与密码，而不能使用 JWT 替代）

**旧版，不推荐：直接使用 UID 和 密码 登录，见主文档。*

请求体：

```
{

}
```

返回：若身份核验成功，则返回值为时间戳加上 `True`。若身份核验失败，则返回值为时间戳加上 `False`。

**JWT 登录（推荐）**：请求体中加入 `"jwt": true` 时返回 JWT：

```
{
    "jwt": true
}
```

成功返回 `{"token": "<jwt>", "expires_in": 604800, "expires_at": <ts>}`；凭据错误返回 `{"error": "auth_failed"}`；达到单用户最大 token 数返回 `{"error": "token_limit_reached"}`。详见[主文档](main.md)的 JWT 认证章节。

- `^ POST /auth/validate` 会话探活（仅 JWT）

请求体：

```
{
    "token": "<jwt>"
}
```

返回：token 有效时返回 `{"valid": true, "uid": <uid>, "stat": <stat>}`；无认证信息返回 `{"error": "not_authenticated"}`；token 无效/过期/被吊销返回 `{"error": "token_expired"}`。

- `^ POST /auth/tokens/list` 列出当前用户的活跃 token（设备）列表（JWT 或旧版认证皆可）

请求体：空（携带 `token`，或旧版 `uid` + `password`）。

返回：`{"tokens": [{"jti": ..., "issued_at": <ts>, "expires_at": <ts>, "ip": ..., "ua": ..., "is_current": <bool>}], "max_per_user": <n>}`；`is_current` 标记当前请求所使用的 token。

管理员可在请求体携带 `"target_uid": <uid>` 列出指定用户的设备（权限与 `/auth/manage/*` 一致：admin 可查看 user/banned，root 可查看全部）；无权限返回 `{"error": "forbidden"}`。

- `^ POST /auth/tokens/revoke` 移除指定 token（踢出设备）

请求体：

```
{
    "jti": "<jti>"
}
```

返回：成功 `{"success": true}`；jti 无效 `{"error": "invalid_request"}`；试图移除当前请求所用 token 返回 `{"error": "current_token"}`。被移除的 token 立即失效，其 WebSocket 连接会被主动断开。

管理员可在请求体携带 `"target_uid": <uid>` 移除指定用户的设备（权限同上，不受 `current_token` 限制）。

- `^ POST /auth/logout` 登出：吊销当前请求所使用的 token

请求体：空（携带 `token`，或旧版 `uid` + `password`）。

返回：`{"success": true}`。当前 token 被吊销并立即失效，其 WebSocket 连接会被主动断开；旧版认证（无 token）直接返回成功。

- `^ POST  /auth/change_sign` 改变个性签名

请求体：

```
{
    "new_sign" : <sign>
}
```

其中 `<sign>` 是新的个性签名。

返回：若更改成功，返回时间戳加 `True`，否则返回时间戳加 `False`。

- `^ POST  /auth/change_introduction` 改变个人简介

请求体：

```
{
    "new_introduction" : <new_intro>
}
```

其中 `<new_intro>` 是新的个人简介

返回：若更改成功，返回时间戳加 `True`，否则返回时间戳加 `False`。

- `^ POST  /auth/change_pwd`  改变用户密码

请求体：

```
{
    "new_pwd" : <new_pwd>
}
```

其中 `<new_pwd>` 是新密码。

返回：若密码更改成功，返回时间戳加 `True`，否则返回时间戳加 `False`。

**注意：修改密码会使该用户已签发的全部 JWT 立即失效。**

- `^ POST /auth/change_auth`

请求体：

```
{
    "change_uid" : <uid>,
    "new_auth": <user_stat>
}
```

其中 `<uid>` 是被操作者的用户编号，`<new_auth>` 是用户的新状态（身份），有：`root, admin, banned, user`。

权限约束如下：

- `root` 可操作所有账号，也可把账号状态修改为 `root`
- `admin` 只能操作 `user` 与 `banned` 账号，且只能把它们改成 `user` 或 `banned`
- `root` 账号只有 `root` 能修改
- 服务端必须始终至少保留一个 `root`；最后一个 `root` 不能被降级，也不能被删除

返回体：若更改成功，返回时间戳加 `True`。若无权限或更改失败，返回时间戳加 `False`。

## 管理接口

以下接口要求操作者至少拥有 `admin` 权限。

统一约束：

- `root` 可操作所有账号
- `admin` 只能操作 `user` 与 `banned`
- `root` 账号仅允许 `root` 修改或删除
- 服务端必须至少保留一个 `root`

- `^ POST /auth/manage/create`

请求体：

```
{
    "uid" : <operator_uid>,
    "password" : <operator_password>,
    "username" : <username>,
    "target_password" : <target_password>,
    "email" : <email>,
    "new_auth" : <user_stat>,
    "sign" : <sign>,
    "introduction" : <introduction>
}
```

其中 `uid/password` 是操作者的凭据，`target_password` 是新建账号的密码。`new_auth` 可省略，默认值为 `user`。

权限约束与上文一致：`root` 可创建任意权限账号，`admin` 只能创建 `user` 或 `banned`。

返回体：若创建成功，返回时间戳加 `True`，否则返回时间戳加 `False`。

- `^ POST /auth/manage/update`

请求体：

```
{
    "uid" : <operator_uid>,
    "password" : <operator_password>,
    "change_uid" : <target_uid>,
    "username" : <username>,
    "target_password" : <target_password>,
    "email" : <email>,
    "new_auth" : <user_stat>,
    "sign" : <sign>,
    "introduction" : <introduction>
}
```

除 `uid/password/change_uid` 外，其余字段均为可选；仅会更新请求体里显式提供的字段。

权限约束与 `/auth/change_auth` 一致。若要清空邮箱，可将 `email` 设为 `null` 或空字符串。

返回体：若更新成功，返回时间戳加 `True`，否则返回时间戳加 `False`。

- `^ POST /auth/manage/ban`

请求体：

```
{
    "uid" : <operator_uid>,
    "password" : <operator_password>,
    "change_uid" : <target_uid>
}
```

该接口等价于把目标账号状态直接改为 `banned`。`root` 可封禁任意账号，`admin` 只能封禁 `user` 或 `banned` 账号。

返回体：若封禁成功，返回时间戳加 `True`，否则返回时间戳加 `False`。

- `^ POST /auth/manage/delete`

请求体：

```
{
    "uid" : <operator_uid>,
    "password" : <operator_password>,
    "change_uid" : <target_uid>
}
```

`root` 可删除任意账号，但最后一个 `root` 不可删除；`admin` 只能删除 `user` 或 `banned` 账号。

返回体：若删除成功，返回时间戳加 `True`，否则返回时间戳加 `False`。

- `^ POST /auth/manage/list`

请求体：

```
{
    "uid" : <operator_uid>,
    "password" : <operator_password>,
    "page" : <page>,
    "page_size" : <page_size>,
    "fetch_all" : <fetch_all>
}
```

其中 `page` 和 `page_size` 用于分页查询，默认分别为 `1` `50`；`page_size` 最大为 `500`。如果 `fetch_all=true` 或 `page_size=-1`，则一次性返回全部用户。

返回体：

```
{
    "users" : [
        {
            "uid" : <uid>,
            "username" : <username>,
            "email" : <email>,
            "stat" : <stat>,
            "create_time" : <create_time>,
            "personal_sign" : <personal_sign>,
            "introduction" : <introduction>
        }
    ],
    "pagination" : {
        "page" : <page>,
        "page_size" : <page_size>,
        "total" : <total>,
        "total_pages" : <total_pages>,
        "has_more" : <has_more>
    },
    "fetch_all" : <fetch_all>
}
```

该接口要求操作者至少拥有 `admin` 权限。

## 服务配置接口

以下接口要求操作者必须拥有 `root` 权限。

- `^ POST /auth/server_settings/query`

请求体：

```
{
    "uid" : <root_uid>,
    "password" : <root_password>
}
```

返回体：

```
{
    "server_name" : <server_name>,
    "port_api" : <port_api>,
    "port_tcp" : <port_tcp>,
    "captcha" : <captcha>,
    "file_last_time" : <file_last_time>,
    "groups_limit" : <groups_limit>,
    "single_group_max_people" : <single_group_max_people>,
    "max_file_size" : <max_file_size>,
    "email_activate" : <email_activate>,
    "verify_email" : <verify_email>,
    "rate_limits" : <rate_limits>,
    "default_asset_urls" : {
        "logo" : "/avatar/get_logo",
        "forum" : "/avatar/get_default/forum",
        "user" : "/avatar/get_default/user",
        "group" : "/avatar/get_default/group"
    }
}
```

其中 `verify_email` 仅在已启用邮箱验证时返回。

- `^ POST /auth/server_settings/update`

请求体：

```
{
    "uid" : <root_uid>,
    "password" : <root_password>,
    "server_name" : <server_name>,
    "captcha" : <captcha>,
    "file_last_time" : <file_last_time>,
    "groups_limit" : <groups_limit>,
    "single_group_max_people" : <single_group_max_people>,
    "max_file_size" : <max_file_size>
}
```

以上字段均为可选，接口只会更新请求体中显式传入的字段。

约束：

- `server_name` 必须是非空字符串
- `file_last_time` 必须是大于等于 `0` 的整数
- `groups_limit`、`single_group_max_people`、`max_file_size` 支持传入 `-1` 表示不限制

返回体与 `/auth/server_settings/query` 相同。

- `^ POST /avatar/upload_default_avatar`

请求体：

```
{
    "uid" : <operator_uid>,
    "password" : <operator_password>,
    "type" : <asset_type>,
    "pic" : <base64_png>
}
```

其中 `<asset_type>` 允许为 `logo`、`forum`、`user`、`group`。
`<base64_png>` 需要是 PNG 图片的 Base64 编码内容。

该接口要求操作者至少拥有 `admin` 权限。

- `^ POST /auth/change_captcha` 改变是否要开启图片验证码注册

请求体：

```
{
    "change_to" : <new_stat>
}
```

`<new_stat>` 为布尔对象，如果为 `true` 表示启用图片验证码注册，如果为 `false` 表示不启用图片验证码注册。

需注意只有 `root` 用户有修改这个的权限。

返回体：修改成功为时间戳加 `True`，修改失败为时间戳加 `False`。

- `^ POST /auth/change_email_verify` 改变是否要开启邮箱验证

请求体：

```
{
    "change_to" : <new_stat>,
    "verify_email" : <verify_email>,
    "email_password" : <email_pwd>
}
```

`<new_stat>` 含义同上，只不过修改的是是否启用邮箱验证。如果它的值为 `true`，请附上后面两项。

`verify_email` 是发送验证邮件的邮箱，`email_password` 是发送验证邮件的的邮箱的 SMTP 授权码（**不是密码！**）。

返回体：修改成功为时间戳加 `True`，否则为时间戳加 `False`。

- `^ POST /auth/change_email` 改变用户的邮箱地址

请求体：

```
{
    "new_email" : <email>
}
```

其中 `<email>` 是新邮箱地址，必须满足邮箱地址格式。

返回体：操作成功返回时间戳加 `True`，否则返回时间戳加 `False`。

- `* GET /auth/captcha` 获取验证码图片

请求体：无

该请求用处是为注册做准备。

需要注意的是：这只有部分服务器会要求，具体是否要求请询问服主或者查询服务器信息。

返回体：若未启用验证码，返回体为空，若启用了验证码，返回体为：

```
{
    "pic" : <pic>,
    "stamp" : <stamp>
}
```

`<pic>` 是 `png` 格式图片的 Base64 编码用 `utf-8` 解码后的格式，`<stamp>` 是验证码标识码（时间戳，为整数）。


- `* GET /auth/uid/<uid>` 查询用户（以 `uid` 为键）相关信息

`<uid>` 是用户编号。

返回体：

```
{
    "uid" : <uid>,
    "username" : <username>,
    "email" : <email>,
    "stat" : <stat>,
    "create_time" : <create_time>,
    "personal_sign" : <personal_sign>,
    "introduction" : <introduction>
}
```

`<create_time>` 是用户创建时间，**为字符串形式的时间戳**。`<personal_sign>` 是个性签名，`<introduction>` 是自我介绍（约束中使用 Markdown 格式）。`<stat>` 是用户权限。

- `* GET /auth/username/<username>` 查询用户，以 `username` 查询。

返回体和返回体含义同上。

- `^ POST /auth/mention_candidates` 获取可被 @提及的用户列表。

请求体：

```
{

}
```

返回体：

```
[
    {
        "uid" : <uid>,
        "username" : <username>
    }
]
```

过滤掉当前用户以及被封禁的账号。返回结果按 `username` 升序排列。用于客户端在输入 `@` 时提供候选用户列表。

- `POST /auth/activate` 激活账号（只有要求邮箱验证的服务器会要求）

请求体：
```
{
    "uid" : <uid>,
    "activate_code" : <code>
}
```

`<code>` 是整数类型。

返回体：如果激活成功返回时间戳加 `True`，否则返回时间戳加 `False`。

- `POST /auth/register`

请求体：

```
{
    "username" : <username>,
    "password" : <password>,
    "captcha_stamp" : <captcha_stamp>,
    "captcha_code" : <captcha_code>,
    "email" : <email>
}
```

如果服务器不启用图形验证码， `<captcha_stamp>` 和 `<captcha_code>` 是可以省略的。

`captcha_stamp` 请填写 `/auth/captcha` 返回体中的 `stamp`。该值实际返回为整数时间戳；服务端校验时会将其转为整数处理。

如果服务器不启用邮箱激活，`<email>` 是可以省略的。

注册成功返回时间戳加 True，否则返回时间戳加 False。需注意如果要邮箱验证，用户初始状态为 `banned`。