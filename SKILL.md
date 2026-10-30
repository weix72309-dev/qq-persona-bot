---
name: qq-group-persona-bot
description: 搭建一个接入 QQ 群的人格机器人（NapCat/OneBot v11 + DeepSeek），带长期记忆、可演化关系状态、无微调的自学习闭环。当用户要求"做一个群里的 AI 角色/女儿/朋友"、"QQ 群机器人接大模型"、"让人设越聊越像"时使用。
metadata:
  short-description: 群聊人格机器人，带记忆和关系状态
---

# QQ 群人格机器人

交付一个可直接跑的 OneBot v11 反向 WebSocket/HTTP 机器人：人设文件、SQLite 长期记忆、三维关系状态、夜间记忆整理。核心约束是**不微调**——所有"越用越顺手"都靠上下文工程和持久化状态实现。

## 先问清三件事

缺任何一项都不要猜，直接问：

1. 机器人 QQ 号和它爸（或主人）的 QQ 号。
2. 群号；不填则响应所有群。
3. 人设文本。用户给了就原样保留，不要重写性格；只补 `{{STATE}}` 段落和变量占位符。

## 交付步骤

1. 一条命令部署，自动填好 QQ 号和名字：

   ```powershell
   .\scripts\install.ps1 -Target C:\qq-bot -BotName 小满 -DadQQ 123456 -AdminQQ 123456 -Groups 987654321
   ```

   手动部署也可以：把 `assets/qq-bot/` 整个复制过去，再改 `config.json` 的 `dad_qq`、`admin_qq`、`bot_name`、`persona_vars`、`behavior.wake_words`、`behavior.groups`。
3. 把用户人设写进 `persona.md`，在性格段之后插入：

   ```
   # 状态
   {{STATE}}
   ```

   如果原文已有"状态"章节，直接删掉，由引擎渲染；否则模板里的数字会成为模型幻觉源头。

4. 让用户填 `DEEPSEEK_API_KEY`（`.env`）。没有 key 时 `Config` 不报错，但 `Bot` 构造会退出——这是有意的，避免半启动。
5. 跑 `python -m bot.selftest`。必须 57/57 通过再继续。这一步不需要 API key，不需要 QQ，包含一次真实的 HTTP 往返。
6. 跑 `python -m bot.main --check` 验证 DeepSeek 和 NapCat 连通。
   只想看一句回复、不连 QQ：`python -m bot.main --once "爸我今天不想上课"`，走完整管线但只打印不发送。
7. 指导用户在 NapCat 开两个端口：HTTP 服务端（默认 3000，供机器人发送）和 HTTP 客户端/反向上报（指向 `http://127.0.0.1:8088/onebot`）。详见 `references/napcat.md`。

## 关系状态怎么实现

三个 0–100 的轴，只由**爸爸**的发言驱动，群友的夸奖不加分（否则群里刷几句就能把角色刷成黏人模式）。

- 信任 trust：决定亲疏。分五档：冰点 / 别扭 / 常态 / 松口 / 黏人，各档带一段行为描述。
- 独立 independence：决定遇事自己扛还是一上来找他。
- 责任 responsibility：决定她会不会主动提醒别人。

写入路径：爸爸的每条消息 → 独立的 JSON 打分调用（temperature 0）→ 提议 `{trust, independence, responsibility, reason}` → `StateEngine.apply()` 钳制。打分是异步的，不阻塞回复。

三处硬约束，删掉任何一条都会跑偏：

- **单条上限 ±2，日内正向累计上限 6**。没有上限的话，一句"闺女真棒"能推 20 分，一周就走完一年的关系曲线。
- **档位有 3 分回滞**，在边界附近来回抖不会让角色反复切换人格。
- **沉默衰减**：14 天没互动向 50 回落，每次 1 分。不衰减则关系只涨不跌，久不联系也黏人。

否定方向不设日上限——凶一次就该当场变冷。生气状态由 `sulky()` 判定：2 小时内最后一条显著负向事件且之后没有正向事件，此时对爸爸的回复强制压到 40 字以内。

调数值用 `python -m bot.state --group <群号> show|set trust 40`，不用改代码。

## 记忆怎么实现

分层，不要合并：

- `messages`：全部群聊，60 天滚动删除。只做上下文和摘要原料，不做检索。
- `memories`：抽取后的长期条目，`scope` 分 global / group / user，按 `owner` 隔离。群友 A 的事实不会被检索进群友 B 的上下文。
- `pinned`：显式规则（"别叫他老板""回复别用感叹号"）。每轮必进 prompt，不参与检索竞争。

检索用 CJK bigram + 拉丁词做倒排，不用向量库。理由：群聊查询短、实体明确，词面匹配的召回足够，且省掉 embedding 成本和一次网络往返。真要上向量再换，接口已经收在 `Store.search()` 里。

学什么、不学什么写死在 `prompt.EXTRACT_SYSTEM`：只抽稳定事实、长期偏好、明确规则；一次性事件和情绪不进库。抽取失败（模型返回非 JSON）只记日志，不影响回复。

夜间 `python -m bot.consolidate`：按群生成纪要、合并相似度 ≥0.82 的重复条目、沉默衰减、过期清理。**不跑这个，记忆库三个月就烂掉**，prompt 会被二十条"他今天很累"塞满。Windows 上挂计划任务：

```powershell
schtasks /Create /SC DAILY /ST 04:00 /TN "qq-bot-consolidate" `
  /TR "cmd /c cd /d C:\path\to\qq-bot && python -m bot.consolidate" /F
```

## 会话行为

- 默认不响应无指向的群聊，只响应 @、回复机器人消息、以机器人名字开头、或记忆指令。这是群机器人和私聊最大的区别：宁可漏接，不可刷屏。
- 速率限制按群 6 条/分、按人 3 条/分。
- 回复过 `clean_reply()`：剥掉 markdown、标题、代码块，超长按句读截断。模型即使被要求"别用列表"，仍会偶发输出，这一层是兜底。
- `msg_id` 唯一索引去重，NapCat 重投不会让机器人回两遍。

## 怎么改人设和状态

- 改 `persona.md` 保存即生效，下一轮请求就换。进程内按 mtime 重载，不重启。
- 人设里 `{名字}`、`{爸爸的群昵称}` 由 `persona_vars` 替换。你给的人设若用别的占位符（如 `{x}`），照抄并在 `persona_vars` 里加同名键即可。
- 模板里已有的"# 状态"整段会被引擎替换；`{{STATE}}` 保留为注入点。两者选一，不要同时出现手工数值。
- 初始值 `state.initial` 只是新群的起点，改它不影响已有群的进度。

## 常见失败

- **角色开始报菜名式输出**：人设太长。压到 800 字以内，示范对话留 4 组就够。
- **记不住刚说的事**：`history_window` 太小，或抽取的 `key` 撞了。`key` 相同会覆盖，这是设计而非 bug。
- **群里冷场时它自己接话**：`wake_words` 写得太宽，或 `respond_to_reply` 把"回复任何机器人消息"当成了被叫。检查 `is_bot_message()`。
- **状态永远不动**：确认 `dad_qq` 填的是数字而不是字符串数组里的昵称；确认 `score_with_llm` 为 true；看日志里有没有 `state score failed`。

## 不做什么

- 不做微调，不引入 transformers/torch。用户明确说过嫌麻烦。
- 不加 Web 管理后台、不加多模型路由、不加语音。这些都不在当前需求里。
- 不把群友的私聊内容或他人隐私写进 `memories`；只记发言者自己透露的关于自己的信息。
