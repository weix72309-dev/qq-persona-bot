# 给 AI agent 的执行说明

你正在帮用户部署一个 QQ 群人格机器人。读完这份文件就能完成，不需要问用户要额外文档。

## 这是什么

`assets/qq-bot/` 是一个完整可运行的程序：OneBot v11（NapCat）反向 HTTP + DeepSeek，SQLite 长期记忆，三维关系状态。纯 Python 标准库，不需要 pip 安装任何东西，不需要 torch，不做微调。

`SKILL.md` 是同一个流程的 Codex skill 版本，`references/napcat.md` 是接入细节。优先按本文件执行，遇到细节再查那两个。

## 开始前必须问到

缺任何一项就问，不要猜、不要填示例值：

1. 机器人要用的 QQ 号。
2. 它爸（或主人）的 QQ 号——只有这个人的发言会推动关系状态。
3. 群号。不填则响应所有群。
4. 角色名字。
5. 人设文本。用户给了就原样保留，只补 `{{STATE}}` 注入点，不要重写性格。
6. DeepSeek API key。让用户自己写进 `.env`，不要让他贴在聊天里，你不要把 key 写进任何提交。

## 部署

Windows：

```powershell
.\scripts\install.ps1 -Target C:\qq-bot -BotName <名字> -DadQQ <QQ号> -AdminQQ <QQ号> -Groups <群号>
```

脚本会拷贝程序、写 `config.json`、生成 `.env`，最后跑一遍自检，必须看到 `57/57 passed` 才算成功。

其他系统：把 `assets/qq-bot/` 整个拷到目标目录，改 `config.json` 的 `dad_qq`、`admin_qq`、`bot_name`、`persona_vars`、`behavior.wake_words`、`behavior.groups`，再复制 `.env.example` 为 `.env`。

## 验证

```bash
python -m bot.selftest        # 57 项，不需要 key 和 QQ
python -m bot.main --check    # 需要 key 和 NapCat，应打印 onebot ok 和 llm ok
python -m bot.main            # 启动
```

`--check` 里任何一项失败都先解决再继续，不要带着半残状态交付。

只想看一句回复、不连 QQ：`python -m bot.main --once "随便说句话"`。这是 dry run，不发送。

## NapCat 配置（必须让用户自己在客户端里点）

在 NapCat WebUI → 网络配置里开两个，收发分开：

| 用途 | 类型 | 值 |
|---|---|---|
| 发消息 | HTTP 服务器 | `http://127.0.0.1:3000`，对应 `onebot.api_base` |
| 收消息 | HTTP 客户端 / 反向上报 | `http://127.0.0.1:8088/onebot`，对应 `listen` |

只实现了 HTTP 上报，没有 WebSocket。让用户选"HTTP 客户端"而不是 WS。

两处 token 要和 `config.json` 一致，或都留空。留空可以跑，但不要监听 `0.0.0.0`。

## 改人设

编辑 `persona.md`，保存即生效，进程会按 mtime 重载，不用重启。

- `{名字}`、`{爸爸的群昵称}` 由 `config.json` 的 `persona_vars` 替换。
- `{{STATE}}` 是关系状态注入点，整段 `# 状态` 会被引擎覆盖。不要在人设里手写数值。
- 人设控制在 800 字以内。太长会导致语气崩、开始输出列表。

## 关系状态

信任 / 独立 / 责任三个 0–100 的轴，只由 `dad_qq` 的发言驱动，群友的夸奖不加分。每条消息单独打分（temperature 0），异步执行。

硬约束，改代码时不要删：

- 单条 ±2，日内正向累计 6。
- 档位切换留 3 分回滞。
- 14 天没互动向 50 回落。
- 否定方向不设日上限，凶一次当场变冷。

调数值用 `python -m bot.state --group <群号> show|set trust 40`。

## 记忆

- `messages`：全部群聊，60 天滚动删除，只做上下文和摘要原料。
- `memories`：抽取后的长期条目，按 `scope` + `owner` 隔离，群友之间不串。
- `pinned`：显式规则，每轮必进 prompt。

检索是 CJK bigram 字面匹配，不是向量。不要为了"更先进"换成 embedding，除非用户明确要求，那会多一次网络往返和一笔费用。

夜间整理：`python -m bot.consolidate`。建议挂每日计划任务，否则记忆库会慢慢变脏。

## 不要做

- 不要微调，不要引入 transformers/torch。用户明确嫌麻烦。
- 不要把 `.env`、`data/`、`*.pyc` 提交到仓库。
- 不要加 Web 后台、多模型路由、语音——不在需求里。
- 不要把群友的隐私写进记忆，只记发言者自己透露的关于自己的信息。
- 不要在没有用户确认的情况下把仓库改成 public，或往群里发测试消息。
