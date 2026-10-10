# TouchFish V5 Api 文档（信息相关部分）

**请确保在阅读本文档前阅读了[主文档](main.md)。**

- `* GET /info` 查询服务器信息

请求体：无

返回体；

```
{
    "captcha" : <is_captcha>,
    "captcha_provider" : <captcha_provider>,
    "captcha_site_key" : <captcha_site_key>,
    "default_asset_urls" : {
        "logo" : "/avatar/get_logo",
        "forum" : "/avatar/get_default/forum",
        "user" : "/avatar/get_default/user",
        "group" : "/avatar/get_default/group"
    },
    "email_activate" : <is_email_activate>,
    "external_login_issuers" : [<iss>, ...],
    "file_last_time" : <file_last_time>,
    "groups_limit" : <groups_limit>,
    "max_file_size" : <max_file_size>,
    "max_avatar_size" : <max_avatar_size>,
    "user_storage_quota" : <user_storage_quota>,
    "max_message_length" : <max_message_length>,
    "max_request_message_length" : <max_request_message_length>,
    "min_search_length" : <min_search_length>,
    "features" : {
        "chat" : {
            "private_chat" : <true_or_false>,
            "group_chat" : <true_or_false>,
            "group_create" : <true_or_false>,
            "friend_request" : <true_or_false>
        },
        "forum" : <true_or_false>,
        "sticker" : <true_or_false>,
        "announcement" : <true_or_false>
    },
    "min_group_name_length" : <min_group_name_length>,
    "max_group_name_length" : <max_group_name_length>,
    "port_api" : <port_api>,
     "port_tcp" : <port_tcp>,
     "ice_servers" : [
         {"urls": ["stun:stun.epygi.com", "stun:stun.fitauto.ru"]}
     ],
     "server_name" : <servername>,
    "single_group_max_people" : <single_group_max_people>
}
```

Example:

```
{"captcha":false,"captcha_provider":"image","captcha_site_key":"","default_asset_urls":{"logo":"/avatar/get_logo","forum":"/avatar/get_default/forum","user":"/avatar/get_default/user","group":"/avatar/get_default/group"},"email_activate":false,"file_last_time":72,"groups_limit":30,"max_file_size":-1,"max_avatar_size":-1,"user_storage_quota":-1,"max_message_length":10000,"min_group_name_length":1,"max_group_name_length":50,"port_api":7001,"port_tcp":1145,"ice_servers":[{"urls":["stun:stun.epygi.com","stun:stun.fitauto.ru"]}],"server_name":"TouchFish","single_group_max_people":200}
```

`captcha_provider` 为验证码类型：`image` 表示内置图片验证码，其余（`turnstile` / `hcaptcha` / `recaptcha`）为第三方验证码，此时 `captcha_site_key` 供客户端渲染 `GET /auth/captcha/page` 页面使用；服务端私钥 `captcha_secret` 不会下发。详见[账号文档](auth.md)。

`ice_servers` is used by the client for WebRTC ICE negotiation. Set
`rtc.turn_enabled` to `true` and configure `rtc.ice_servers` in the instance
configuration to publish TURN servers.

`external_login_issuers` 为已配置的外部登录签发方 `iss` 列表（仅标识，不含任何密钥）；为空数组表示服务器未启用外部平台登录。详见[账号文档](auth.md)的「第三方签发（外部平台登录）」。

`features` 为服务器功能开关的当前值（在实例 config.json 的 `features` 中配置，缺省全部开启）。
按设计，细分开关关闭后对应写操作会被服务端拒绝（错误码 `FEATURE_DISABLED_*`），客户端应据此隐藏入口；
各端点的检查点随版本逐步接入。
当前可开关项：`chat.private_chat`（私聊消息）、`chat.group_chat`（群消息）、`chat.group_create`（建群）、
`chat.friend_request`（好友申请）、`forum`（论坛写操作）、`sticker`（贴纸写操作）、`announcement`（公告写操作）。

`max_request_message_length` 为好友/入群申请留言的最大长度（默认 200）。
`min_search_length` 为搜索关键词的最小长度（默认 2）。

