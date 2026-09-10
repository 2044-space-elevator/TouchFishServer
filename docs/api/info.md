# TouchFish V5 Api 文档（信息相关部分）

**请确保在阅读本文档前阅读了[主文档](main.md)。**

- `* GET /info` 查询服务器信息

请求体：无

返回体；

```
{
    "captcha" : <is_captcha>,
    "default_asset_urls" : {
        "logo" : "/avatar/get_logo",
        "forum" : "/avatar/get_default/forum",
        "user" : "/avatar/get_default/user",
        "group" : "/avatar/get_default/group"
    },
    "email_activate" : <is_email_activate>,
    "file_last_time" : <file_last_time>,
    "groups_limit" : <groups_limit>,
    "max_file_size" : <max_file_size>,
    "max_avatar_size" : <max_avatar_size>,
    "user_storage_quota" : <user_storage_quota>,
    "max_message_length" : <max_message_length>,
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
{"captcha":false,"default_asset_urls":{"logo":"/avatar/get_logo","forum":"/avatar/get_default/forum","user":"/avatar/get_default/user","group":"/avatar/get_default/group"},"email_activate":false,"file_last_time":72,"groups_limit":30,"max_file_size":-1,"max_avatar_size":-1,"user_storage_quota":-1,"max_message_length":10000,"min_group_name_length":1,"max_group_name_length":50,"port_api":7001,"port_tcp":1145,"ice_servers":[{"urls":["stun:stun.epygi.com","stun:stun.fitauto.ru"]}],"server_name":"TouchFish","single_group_max_people":200}
```

`ice_servers` is used by the client for WebRTC ICE negotiation. Set
`rtc.turn_enabled` to `true` and configure `rtc.ice_servers` in the instance
configuration to publish TURN servers.
