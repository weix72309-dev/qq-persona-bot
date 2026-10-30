# NapCat 接入

非官方协议，封号风险由用户承担。个人号和大量群发都属于高风险用法，先跟用户讲明再动手。

## 需要开的两个东西

NapCat WebUI → 网络配置 → 新建：

1. **HTTP 服务器**（正向，默认 3000）。机器人通过它发消息，对应 `config.json` 的 `onebot.api_base`。
2. **HTTP 客户端**（反向上报）。URL 填 `http://127.0.0.1:8088/onebot`，对应 `behavior` 之外监听端口。不同版本叫"HTTP 客户端""反向 HTTP""Webhook"，认准"上报地址 / report URL"这一项。

两处的 Token 必须和配置一致：HTTP 服务器的 token 填 `onebot.token`，反向上报的 token 填 `listen.token`。留空也能跑，但端口暴露到局域网就等于把机器人送人。

## 验证顺序

```powershell
# 1. NapCat 是否活着
curl.exe -s http://127.0.0.1:3000/get_login_info

# 2. 机器人自身
python -m bot.main --check

# 3. 群里发一条 @机器人 的消息，看日志
Get-Content data\bot.log -Tail 20 -Wait
```

`--check` 会分别打印 `onebot ok, bot QQ = ...` 和 `llm ok, reply = ...`。两行都在再进群。

## 发不出去但能收到

常见原因是 `api_base` 指向了反向端口。反向只负责收事件，不能调 `send_group_msg`。收发必须是两个不同端口。

## 群限制

群里必须 @ 才响应是刻意设计。NapCat 能拿到全量群消息，但机器人不该对每句话都接——那不是人格，那是噪音源。
