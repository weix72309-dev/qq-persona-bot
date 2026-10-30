"""Offline selftest: no network, no QQ, no API key. Validates the whole pipeline."""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

from . import prompt as P
from .core import Bot, clean_reply
from .onebot import Incoming, normalize, parse_segments
from .store import Store

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(ok), detail))


def test_parsing() -> None:
    text, ats, reply = parse_segments(
        [{"type": "at", "data": {"qq": "123"}}, {"type": "text", "data": {"text": " 在吗"}}]
    )
    check("array at+text", text == "在吗" and ats == ["123"] and reply is None)
    text, ats, reply = parse_segments("[CQ:reply,id=88][CQ:at,qq=999]你好")
    check("cq string parse", text == "你好" and ats == ["999"] and reply == "88")
    ev = {
        "post_type": "message",
        "message_type": "group",
        "group_id": 42,
        "user_id": 7,
        "message_id": 100,
        "self_id": 999,
        "sender": {"card": "小明"},
        "message": [{"type": "text", "data": {"text": "hi"}}],
    }
    msg = normalize(ev)
    check("normalize group event", msg is not None and msg.group_id == "42" and msg.nickname == "小明")
    check("ignore private", normalize({**ev, "message_type": "private"}) is None)
    from .core import SELF_DISCLOSURE

    check("detects self-disclosure", bool(SELF_DISCLOSURE.search("我在杭州上班")))
    check("ignores chit-chat", not SELF_DISCLOSURE.search("今天天气不错啊"))


def test_store(tmp: Path) -> Store:
    store = Store(tmp / "t.db")
    ok = store.add_message("1", "7", "小明", "user", "我在杭州做后端", msg_id="m1")
    check("insert message", ok)
    check("dedupe msg_id", not store.add_message("1", "7", "小明", "user", "dup", msg_id="m1"))
    store.upsert_memory("user", "7", "fact", "所在地", "小明在杭州做后端")
    store.upsert_memory("user", "7", "rule", "称呼", "别叫他老板", pinned=True)
    store.upsert_memory("user", "8", "fact", "所在地", "另一个人在成都")
    hits = store.search("杭州 后端", "1", "7")
    check("search recalls own fact", any("杭州" in r["value"] for r in hits), f"{len(hits)} hits")
    check("search isolates users", all(r["owner"] == "7" or r["scope"] != "user" for r in hits))
    pinned = store.always_memories("1", "7")
    check("pinned rule always loaded", any("老板" in r["value"] for r in pinned))
    store.upsert_memory("user", "7", "fact", "所在地", "小明在杭州做后端")
    count = len(store.all_active())
    check("upsert does not duplicate", count == 3, f"{count} rows")
    removed = store.delete_matching("1", "7", "成都")
    check("no cross-user delete", removed == [])
    check("delete own", len(store.delete_matching("1", "7", "老板")) == 1)
    return store


def test_prompt(store: Store) -> None:
    rows = store.history("1", 10)
    raw = (Path(__file__).resolve().parent.parent / "persona.md").read_text(encoding="utf-8")
    persona = P.render_persona(
        raw, "当前状态：信任 62/100", {"名字": "小满", "爸爸的群昵称": "老王"}
    )
    check("persona fills name", "你叫小满" in persona)
    check("persona fills dad alias", "老王是你爸" in persona)
    check("persona injects state", "信任 62/100" in persona)
    check("persona drops template placeholder", "{{STATE}}" not in persona)
    legacy = "# 人设\n嘴硬。\n\n# 状态（框架会提供这些数值时才用）\n当前状态：信任{x}/100\n\n# 记忆\n记住事情。"
    fixed = P.render_persona(legacy, "当前状态：信任 70/100")
    check("legacy status section replaced", "信任{x}" not in fixed and "信任 70/100" in fixed)
    check("section after status survives", "# 记忆" in fixed)
    check("single status block", fixed.count("当前状态") == 1, str(fixed.count("当前状态")))
    msgs = P.build(
        persona=persona,
        group_id="1",
        speaker_id="7",
        speaker_name="小明",
        rules=store.always_memories("1", "7"),
        memories=store.search("杭州", "1", "7"),
        history=rows,
        user_text="你在哪",
        max_reply_chars=200,
    )
    joined = "\n".join(m["content"] for m in msgs)
    check("prompt has persona", "嘴硬心软" in joined)
    check("prompt has speaker name", "小明" in joined)
    check("prompt has history", "杭州" in joined)
    check("prompt has style guard", "QQ群里打字" in joined)
    check("prompt ends with user turn", msgs[-1]["role"] == "user")


def test_clean() -> None:
    check("strip markdown bold", clean_reply("**你好**", 100) == "你好")
    check("strip heading", clean_reply("## 标题\n正文", 100) == "标题\n正文")
    long = "啊" * 500
    out = clean_reply(long, 50)
    check("truncates long reply", len(out) <= 51, str(len(out)))


def test_state(tmp: Path) -> None:
    from .config import Config
    from .state import StateEngine

    cfg_path = tmp / "state-config.json"
    cfg_path.write_text(json.dumps({"persona_vars": {"name": "小满", "dad": "爸爸"}}), encoding="utf-8")
    cfg = Config(cfg_path)
    store = Store(tmp / "state.db")
    engine = StateEngine(store, cfg)

    start = engine.get("1")["trust"]
    applied = engine.apply("1", {"trust": 9, "independence": 0.2, "responsibility": 0}, "测试")
    check("clamps single-step delta", applied.get("trust") == 2, str(applied))
    check("ignores sub-point noise", "independence" not in applied, str(applied))
    check("state persisted", engine.get("1")["trust"] == start + 2)

    for _ in range(10):
        engine.apply("1", {"trust": 2}, "连刷")
    moved = engine.get("1")["trust"] - start
    check("daily budget caps drift", moved == 6, f"moved {moved}")

    engine.apply("1", {"trust": -2}, "降温")
    check("can go down too", engine.get("1")["trust"] == start + 4)
    check("event log written", len(store.state_events("1", 0)) >= 3)

    block = engine.render("1")
    check("state block has numbers", "信任" in block and "/100" in block)
    check("state block names stage", "阶段" in block)
    check("state block hides mechanics", "不要把数字说出来" in block)

    engine.apply("1", {"trust": -2}, "被冷落")
    check("sulky flag after drops", engine.sulky("1"))
    store.close()


class Recorder:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send_group(self, group_id, text, reply_to=None, at=None):
        self.sent.append(text)
        return f"bot-{len(self.sent)}"

    def self_id(self):
        return "999"


def test_bot(tmp: Path) -> None:
    cfg_path = tmp / "config.json"
    cfg_path.write_text(
        json.dumps(
            {
                "persona_vars": {"name": "小满", "dad": "爸爸"},
                "dad_qq": ["7"],
                "behavior": {
                    "groups": [],
                    "dry_run": False,
                    "max_per_group_per_min": 50,
                    "max_per_user_per_min": 50,
                },
                "memory": {"extract_online": False},
            }
        ),
        encoding="utf-8",
    )
    from .config import Config

    import os

    os.environ["DEEPSEEK_API_KEY"] = "test-key"
    cfg = Config(cfg_path)
    (tmp / "persona.md").write_text(
        (Path(__file__).resolve().parent.parent / "persona.md").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    bot = Bot(cfg)
    recorder = Recorder()
    bot.api = recorder
    seen: dict[str, list] = {}

    def fake_chat(messages, **kw):
        if kw.get("json_mode"):
            return '{"trust":1,"independence":0,"responsibility":0,"reason":"关心"}', {}
        seen["last"] = messages
        return "我在。", {"prompt_tokens": 10, "completion_tokens": 2}

    bot.llm.chat = fake_chat
    bot.llm.model = "fake"

    msg = Incoming(group_id="1", user_id="7", nickname="爸爸", text="小满 在吗", msg_id="e1", at_targets=[])
    bot.ingest(msg, "999")
    check("bot replies to wake word", recorder.sent == ["我在。"], str(recorder.sent))
    check("bot message persisted", any(r["role"] == "assistant" for r in bot.store.history("1", 10)))
    system_prompt = seen["last"][0]["content"]
    check("live state in prompt", "当前状态：信任" in system_prompt)
    check("persona name rendered", "你叫小满" in system_prompt)

    bot.ingest(
        Incoming(group_id="1", user_id="7", nickname="爸爸", text="记一下：我在杭州", msg_id="e2"),
        "999",
    )
    check("manual remember", recorder.sent[-1] == "记下了。", str(recorder.sent[-1:]))
    check("memory written", any("杭州" in r["value"] for r in bot.store.remember_list("1", "7")))

    bot.ingest(
        Incoming(group_id="1", user_id="7", nickname="爸爸", text="小满 你记得什么", msg_id="e3"),
        "999",
    )
    check("memory listing", "杭州" in recorder.sent[-1], recorder.sent[-1][:60])

    before = len(recorder.sent)
    bot.ingest(Incoming(group_id="1", user_id="7", nickname="爸爸", text="随便聊聊", msg_id="e4"), "999")
    check("ignores unrelated chatter", len(recorder.sent) == before)
    check("still records chatter", bot.store.history("1", 20)[-1]["content"] == "随便聊聊")

    bot.ingest(
        Incoming(group_id="1", user_id="7", nickname="爸爸", text="重复", msg_id="e1"),
        "999",
    )
    check("dedupes replayed event", len(recorder.sent) == before)

    bot.ingest(
        Incoming(group_id="9", user_id="7", nickname="爸爸", text="小满 在吗", msg_id="e5"),
        "999",
    )
    check("memory scoped per group", len(bot.store.history("9", 10)) == 2)
    bot.close()


def test_webhook(tmp: Path) -> None:
    """Full HTTP round-trip: token, route, dispatch, replay dedupe."""
    import time
    import urllib.error
    import urllib.request

    from .config import Config
    from .server import serve

    cfg_path = tmp / "web.json"
    cfg_path.write_text(
        json.dumps(
            {
                "listen": {"host": "127.0.0.1", "port": 18099, "path": "/onebot", "token": "t0k"},
                "persona_vars": {"name": "小满", "dad": "爸爸"},
                "dad_qq": ["7"],
                "behavior": {"max_per_group_per_min": 50, "max_per_user_per_min": 50},
                "memory": {"extract_online": False},
                "state": {"score_with_llm": False},
            }
        ),
        encoding="utf-8",
    )
    (tmp / "persona.md").write_text("你叫{名字}。\n\n# 状态\n{{STATE}}\n", encoding="utf-8")
    cfg = Config(cfg_path)
    bot = Bot(cfg)
    sent: list[str] = []
    bot.api.send_group = lambda g, t, reply_to=None, at=None: sent.append(t) or "mid-1"
    bot.llm.chat = lambda messages, **kw: ("嗯。", {})
    httpd = serve(cfg, bot)
    time.sleep(0.3)

    def post(payload: dict, path: str = "/onebot", token: str = "t0k") -> int:
        req = urllib.request.Request(
            f"http://127.0.0.1:18099{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status
        except urllib.error.HTTPError as exc:
            return exc.code

    event = {
        "post_type": "message",
        "message_type": "group",
        "group_id": 1,
        "user_id": 7,
        "message_id": "h1",
        "self_id": 999,
        "sender": {"card": "爸爸"},
        "message": [{"type": "text", "data": {"text": "小满 在吗"}}],
    }
    check("webhook rejects wrong route", post(event, path="/nope") == 404)
    check("webhook rejects bad token", post(event, token="wrong") == 401)
    check("webhook accepts query token", post(event, path="/onebot?access_token=t0k") == 200)
    for _ in range(40):
        if sent:
            break
        time.sleep(0.05)
    check("webhook replied", sent == ["嗯。"], str(sent))
    check("webhook dedupes replay", post(event) == 200)
    time.sleep(0.3)
    check("no double reply", sent == ["嗯。"], str(sent))
    check(
        "health endpoint",
        urllib.request.urlopen("http://127.0.0.1:18099/health", timeout=5).status == 200,
    )
    httpd.shutdown()
    bot.close()


def main() -> int:
    import os

    os.environ.setdefault("DEEPSEEK_API_KEY", "test-key")
    tmp = Path(tempfile.mkdtemp(prefix="qqbot-selftest-"))
    try:
        test_parsing()
        store = test_store(tmp)
        test_prompt(store)
        store.close()
        test_clean()
        test_state(tmp)
        test_bot(tmp)
        test_webhook(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    failed = [c for c in CHECKS if not c[1]]
    for name, ok, detail in CHECKS:
        print(("PASS  " if ok else "FAIL  ") + name + (f"  [{detail}]" if detail and not ok else ""))
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

