"""Prompt assembly: persona, rules, recalled memories, recent group log."""

from __future__ import annotations

STYLE_RULES = """输出规则：
- 你在QQ群里打字，不是在写文档。不用标题、列表、加粗、表格、代码块。
- 默认1到3句，不超过{max_chars}字；对方问复杂问题时才展开。
- 不说"作为AI/助手"、不解释人设、不解释记忆机制、不说"根据我的记忆"。
- 不复述对方的话，不每句都反问，不用"哈哈"开头凑气氛。
- 只输出要发进群的内容，不写旁白，不用引号把整段话包起来。"""


def render_persona(text: str, state_block: str, variables: dict | None = None) -> str:
    """Fill name slots and swap any hand-written status section for the live one."""
    out = text
    for key, value in (variables or {}).items():
        out = out.replace("{" + str(key) + "}", str(value))
    if "{{STATE}}" in out:
        return out.replace("{{STATE}}", state_block).strip()

    kept: list[str] = []
    skipping = False
    replaced = False
    for line in out.splitlines():
        if line.startswith("#"):
            skipping = "状态" in line.lstrip("#").strip()
            if skipping:
                if not replaced:
                    kept.extend(["# 状态", state_block, ""])
                    replaced = True
                continue
        if skipping:
            continue
        kept.append(line)
    if not replaced:
        kept.extend(["", "# 状态", state_block, ""])
    return "\n".join(kept).strip()


def _rules_block(rows) -> str:
    rules = [row["value"] for row in rows if row["kind"] in ("rule", "pref")]
    if not rules:
        return "无额外规则，按人设自然说话。"
    return "\n".join(f"- {r}" for r in rules)


def _memory_block(rows, budget: int) -> str:
    if not rows:
        return "（暂无）"
    lines: list[str] = []
    used = 0
    for row in rows:
        line = f"- {row['value']}"
        if used + len(line) > budget:
            break
        lines.append(line)
        used += len(line)
    return "\n".join(lines) if lines else "（暂无）"


def build(
    *,
    persona: str,
    group_id: str,
    speaker_id: str,
    speaker_name: str,
    rules,
    memories,
    history,
    user_text: str,
    max_reply_chars: int,
    memory_budget: int = 1400,
) -> list[dict]:
    system = (
        f"{persona}\n\n"
        f"当前场景：QQ群 {group_id}，正在对你说话的是 {speaker_name}（QQ {speaker_id}）。"
        "群里还有其他人，历史消息带昵称前缀，不是每句都在跟你说。\n\n"
        f"必须遵守的规则：\n{_rules_block(rules)}\n\n"
        f"你记得的相关信息：\n{_memory_block(memories, memory_budget)}\n"
        "这些信息只在自然相关时使用，无关就忽略，不要逐条复述。\n\n"
        f"{STYLE_RULES.format(max_chars=max_reply_chars)}"
    )
    messages: list[dict] = [{"role": "system", "content": system}]
    for row in history:
        content = row["content"]
        if row["role"] == "assistant":
            messages.append({"role": "assistant", "content": content})
        else:
            name = row["nickname"] or row["user_id"]
            messages.append({"role": "user", "content": f"{name}: {content}"})
    messages.append({"role": "user", "content": f"{speaker_name}: {user_text}"})
    return messages


EXTRACT_SYSTEM = """你从QQ群聊天中抽取值得长期记住的信息，只输出JSON。

规则：
1. 只抽说话人自己透露、以后还有用的信息：身份、称呼、所在地、职业、作息、长期偏好、明确要求bot遵守的说法。
2. 不抽：一次性事件、当下情绪、别人的隐私、玩笑、寒暄、提问本身。
3. kind 只能取：
   - "fact" 稳定事实（如"在杭州做后端"）
   - "pref" 偏好或习惯（如"讨厌被叫老板"）
   - "rule" 明确要求以后怎么说话（如"回复别用感叹号"）
4. key 是2到6字的归类词；value 是一句脱离上下文也能看懂的陈述，主语用"{speaker}"。
5. 没有可记的就输出 {"memories":[]}。

输出格式：
{"memories":[{"kind":"fact","key":"所在地","value":"{speaker}在杭州工作","confidence":0.9}]}"""


def build_extract(speaker: str, speaker_id: str, text: str, existing: list[str]) -> list[dict]:
    known = "\n".join(f"- {e}" for e in existing) if existing else "（无）"
    return [
        {"role": "system", "content": EXTRACT_SYSTEM.replace("{speaker}", speaker)},
        {
            "role": "user",
            "content": f"说话人：{speaker}（QQ {speaker_id}）\n已知信息：\n{known}\n\n本轮发言：\n{text}",
        },
    ]


DIGEST_SYSTEM = """你把一段QQ群聊天压缩成当天纪要，供以后回忆上下文，只输出JSON。

输出格式：
{"digest":"2到4句话，写清聊了什么、有什么结论或约定","facts":[{"key":"话题名","value":"一句可独立理解的结论"}]}

digest只写有信息量的内容；facts最多4条，没有就给空数组。事实主语写清楚是谁。"""


def build_digest(transcript: str) -> list[dict]:
    return [
        {"role": "system", "content": DIGEST_SYSTEM},
        {"role": "user", "content": transcript},
    ]


SCORE_SYSTEM = """你是关系状态评估器。判断爸爸这条发言会在多大程度上影响女儿对你的三个数值。只输出JSON。

数值语义（0-100）：
- trust 信任：她对你敞开到什么程度。你认真关心、守约、耐心、记得她的事 → 升；忽视、食言、敷衍、说教、说难听话 → 降。
- independence 独立：她遇事先自己扛还是一上来找你。你鼓励她自己拿主意、给她空间 → 升；你替她做决定、过度干涉、时时查岗 → 降。
- responsibility 责任：她对承诺和提醒的自觉度。你让她承担后果、托付具体的事 → 升；你替她收拾一切、有求必应 → 降。

规则：
1. 单条消息的改动很小：普通互动最多 ±1，明确有意义的事件才 ±2。多数消息应该是 0。
2. 没有实质情绪或关系信息的消息（问天气、发链接、寒暄）全部给 0。
3. 不要因为消息里有亲昵词就一律加分，要看是否真的被当回事。
4. reason 用8个字以内说明原因，例如"关心加班"、"又食言了"。

输出格式：
{"trust":0,"independence":0,"responsibility":0,"reason":"日常闲聊"}"""


def build_score(text: str, current: dict) -> list[dict]:
    now = "，".join(f"{k}={current[k]:.0f}" for k in ("trust", "independence", "responsibility"))
    return [
        {"role": "system", "content": SCORE_SYSTEM},
        {"role": "user", "content": f"当前数值：{now}\n\n爸爸刚说的话：\n{text}"},
    ]
