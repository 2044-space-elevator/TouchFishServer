# TURN（非必需，独立设置）

TURN 独立于 TF 部署。服务器不会管理 TURN，也不会处理这些。
只通过 `/info` 将管理员配置的 ICE 服务器传给客户端。

## 配置

在 `res/<port_api>/config.json` 中加入：

```json
{
  "rtc": {
    "turn_enabled": true,
    "ice_servers": [
      {
        "urls": [
          "stun:stun.epygi.com",
          "stun:stun.fitauto.ru"
        ]
      },
      {
        "urls": ["turn:turn.example.com:3478"],
        "username": "touchfish",
        "credential": "replace-with-a-strong-password"
      }
    ]
  }
}
```

`turn_enabled` 为 `false` 或缺失时，服务端只返回内置 STUN 列表。启用
TURN 后，服务端会返回配置中合法的 `ice_servers` 条目。

## 部署

可以使用 coturn、云 TURN 服务或其他兼容 WebRTC ICE 的服务。TURN 服务需要
开放 UDP/TCP 3478。

若使用 TLS TURN，还需配置 5349 和证书。TURN 的用户名和
密码会下发给客户端。

## 验证

访问 `http://<server>:<port_api>/info`，确认 `ice_servers` 中出现 TURN URL。
随后在严格 NAT 或受限网络中进行一次通话，检查双方 ICE 是否进入 connected。
