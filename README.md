# qq-persona-bot

QQ 群人格机器人：OneBot v11（NapCat）+ DeepSeek，带长期记忆和可演化的关系状态。不训练模型，不装 torch，纯标准库 Python。

> 如果你是 AI agent，直接读 [AGENTS.md](AGENTS.md)，里面有完整部署流程。下面给人看。

## 结构

| 路径 | 作用 |
|---|---|
| `AGENTS.md` | 给 agent 的执行说明，读完就能部署 |
| `SKILL.md` | Codex skill 版本，装进 `~/.codex/skills` 即可用 `$qq-group-persona-bot` 调用 |
| `assets/qq-bot/` | 可运行的程序本体 |
| `references/napcat.md` | NapCat 接入细节 |
| `scripts/install.ps1` | 一键部署 |

## 前提

Python 3.10+、一个 NapCat 登录的 QQ 小号、DeepSeek API key。除此之外不需要装任何 Python 包。

## 部署

让 agent 读 `AGENTS.md` 照着做，或者自己跑：

```powershell
.\scripts\install.ps1 -Target C:\qq-bot -BotName 小满 -DadQQ 你的QQ号 -Groups 群号
```

装完填 `C:\qq-bot\.env` 里的 `DEEPSEEK_API_KEY`，NapCat 开两个端口（发 3000，收回填 `http://127.0.0.1:8088/onebot`），再 `python -m bot.main --check`。

## 适合谁

个人小群的角色扮演。非官方协议，封号风险自负。群友的记录只进本地 SQLite，不导出、不上传；训练数据不碰，也没有训练。

## 已知边界

- 只做群聊，私聊按设计忽略。
- 不接语音、不接图片理解（图片只记成 `[图片]`）。
- 记忆检索是字面匹配（CJK bigram），不是向量。群聊查询短、实体明确，够用且省一次网络往返。
