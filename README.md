# qq-persona-bot

QQ 群人格机器人：OneBot v11（NapCat）+ DeepSeek，带长期记忆和可演化的关系状态。不训练模型，不装 torch，纯标准库 Python。

## 先搞清楚一件事

这不是一个"给 DeepSeek 就能跑"的东西。它由两部分组成：

- `assets/qq-bot/`：能直接运行的机器人程序，只需要 Python 3.10+ 和 DeepSeek API key。
- `SKILL.md`：给 Codex（或任何能读 SKILL.md 的 agent）看的安装说明书。

只想让机器人跑起来，**不需要** agent，也不需要 Codex。想让 agent 帮你改人设、加功能、排查问题，才需要把整个仓库作为 skill 装进去。

## 三步跑起来

```powershell
git clone <repo-url> qq-bot-src
cd qq-bot-src
.\scripts\install.ps1 -Target C:\qq-bot -BotName 小满 -DadQQ 你的QQ号 -Groups 群号
```

装完去 `C:\qq-bot\.env` 填 `DEEPSEEK_API_KEY`，然后：

```powershell
cd C:\qq-bot
python -m bot.main --check     # 验证 DeepSeek + NapCat 都通
python -m bot.main             # 启动
```

只想先看一句回复、不连 QQ：

```powershell
python -m bot.main --once "爸我今天不想上课"
```

## NapCat 要开两个端口

| 用途 | 类型 | 端口 | 对应配置 |
|---|---|---|---|
| 机器人发消息 | HTTP 服务器 | 3000 | `onebot.api_base` |
| 机器人收消息 | HTTP 客户端（反向上报） | 指向 `http://127.0.0.1:8088/onebot` | `listen` |

混成一个端口会收得到发不出。两处 token 留空也能跑，但别把端口暴露到公网。

## 日常操作

```powershell
python -m bot.state --group 群号 show          # 看当前关系状态
python -m bot.state --group 群号 set trust 40  # 手动改
python -m bot.consolidate --dry-run            # 预览记忆整理
python -m bot.consolidate                      # 跑一次整理（建议每天凌晨）
python -m bot.selftest                         # 57 项自检，不需要 key 和 QQ
```

群里的指令（@机器人 或叫名字）：`记一下：...`、`忘掉...`、`你记得什么`。

## 改人设

编辑 `persona.md`，保存即生效，不用重启。`{名字}`、`{爸爸的群昵称}` 由 `config.json` 的 `persona_vars` 替换；`{{STATE}}` 是关系状态注入点，整段 `# 状态` 会被引擎覆盖，不要手写数字。

## 关系状态怎么算

信任 / 独立 / 责任三个 0–100 的轴，只由 `dad_qq` 的发言驱动。每条消息单独打一次分（temp 0），单条最多 ±2，日内正向累计最多 6，14 天没互动向 50 回落。数值只渲染成阶段描述注入 prompt，模型看不到原始数字。

不设这些上限，群里夸两句就能把角色刷成黏人模式。

## 适合谁

个人小群的角色扮演。非官方协议，封号风险自负。群友的记录只进本地 SQLite，不导出、不上传；训练数据不碰，也没有训练。

## 已知边界

- 只做群聊，私聊按设计忽略。
- 不接语音、不接图片理解（图片只记成 `[图片]`）。
- 记忆检索是字面匹配（CJK bigram），不是向量。群聊查询短、实体明确，够用且省一次网络往返。
