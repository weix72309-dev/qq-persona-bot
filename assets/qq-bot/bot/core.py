"""Message routing, replies, and incremental memory writing."""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor

from . import prompt as P
from .config import Config
from .llm import LLM, LLMError
from .onebot import OneBotAPI, Incoming
from .state import StateEngine
from .store import Store, terms

log = logging.getLogger("core")

COMMAND_RE = re.compile(
    r"^\s*(?:记住|记一下|记下|别忘了|remember)\s*[:：,，]?\s*(?P<mem>.+)$", re.I | re.S
)
FORGET_RE = re.compile(
    r"^\s*(?:忘掉|忘记|删掉记忆|forget)\s*[:：,，]?\s*(?P<mem>.+)$", re.I | re.S
)
LIST_RE = re.compile(r"^\s*(?:你记得什么|记得什么|记忆列表|memories)\s*[?？]?\s*$")
RULE_HINT = re.compile(r"(以后|每次|别再|不要|不准|记得|叫我|称呼|回复|说话)")
SELF_DISCLOSURE = re.compile(
    r"(我|俺|本人)\s*(是|在|住|来自|做|干|读|学|养|有|喜欢|讨厌|不吃|受不了|习惯|一般|每天|最近)"
)


def clean_reply(text: str, max_chars: int) -> str:
    text = (text or "").strip()
    text = re.sub(r"^```.*?\n(.*?)```$", r"\1", text, flags=re.S)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"(?m)^#{1,6}\s*", "", text)
    text = re.sub(r"(?m)^\s*[-*+]\s+", "· ", text)
    text = text.replace("```", "")
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) > max_chars:
        cut = text[:max_chars]
        for sep in ("。", "！", "？", "\n", "，", " "):
            idx = cut.rfind(sep)
            if idx > max_chars * 0.6:
                return cut[: idx + 1].strip()
        return cut.rstrip() + "…"
    return text


class RateLimiter:
    def __init__(self, group_per_min: int, user_per_min: int):
        self.group_limit = max(1, group_per_min)
        self.user_limit = max(1, user_per_min)
        self._group: dict[str, deque] = defaultdict(deque)
        self._user: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, group_id: str, user_id: str) -> bool:
        now = time.time()
        with self._lock:
            key = f"{group_id}:{user_id}"
            for bucket, limit in ((self._group[group_id], self.group_limit), (self._user[key], self.user_limit)):
                while bucket and now - bucket[0] > 60:
                    bucket.popleft()
                if len(bucket) >= limit:
                    return False
            self._group[group_id].append(now)
            self._user[key].append(now)
            return True


class Bot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.store = Store(cfg.db_path)
        cfg.data["llm"]["api_key"] = cfg.require_api_key()
        self.llm = LLM(cfg["llm"])
        self.api = OneBotAPI(cfg["onebot"]["api_base"], cfg["onebot"]["token"])
        self.rate = RateLimiter(
            cfg["behavior"]["max_per_group_per_min"],
            cfg["behavior"]["max_per_user_per_min"],
        )
        self.pool = ThreadPoolExecutor(max_workers=int(cfg["behavior"]["workers"]), thread_name_prefix="bot")
        self.state = StateEngine(self.store, cfg)
        self._last_score: dict[str, float] = {}
        self._last_learn: dict[str, float] = {}
        self._persona = cfg.persona()
        self._persona_mtime = cfg.persona_path.stat().st_mtime
        self._lock = threading.Lock()

    # ---------- persona ----------

    def persona_raw(self) -> str:
        try:
            mtime = self.cfg.persona_path.stat().st_mtime
        except OSError:
            return self._persona
        if mtime != self._persona_mtime:
            with self._lock:
                self._persona = self.cfg.persona()
                self._persona_mtime = mtime
                log.info("persona reloaded (%d chars)", len(self._persona))
        return self._persona

    def persona(self, group_id: str) -> str:
        variables = dict(self.cfg.get("persona_vars", {}))
        variables.setdefault("名字", variables.get("name", ""))
        variables.setdefault("爸爸的群昵称", variables.get("dad", "爸爸"))
        variables.setdefault("name", variables["名字"])
        variables.setdefault("dad", variables["爸爸的群昵称"])
        return P.render_persona(
            self.persona_raw(),
            self.state.render(group_id),
            variables,
        )

    # ---------- filtering ----------

    def should_reply(self, msg: Incoming, bot_id: str) -> bool:
        behavior = self.cfg["behavior"]
        allowed = self.cfg.groups_allowed()
        if allowed and msg.group_id not in allowed:
            return False
        if bot_id and msg.user_id == bot_id:
            return False
        if bot_id and bot_id in msg.at_targets:
            return True
        if msg.reply_to and self.store.is_bot_message(msg.reply_to):
            return True if behavior.get("respond_to_reply", True) else False
        if COMMAND_RE.match(msg.text) or FORGET_RE.match(msg.text) or LIST_RE.match(msg.text):
            return True
        for word in behavior.get("wake_words") or []:
            if word and msg.text.startswith(word):
                return True
            if word and word in msg.text[:24]:
                return True
        name = self.cfg.get("bot_name", "")
        if name and name in msg.text[:24]:
            return True
        return False

    def strip_wake(self, text: str) -> str:
        out = text
        for word in self.cfg["behavior"].get("wake_words") or []:
            if word and out.startswith(word):
                out = out[len(word) :].lstrip(" :：,，")
                break
        return out.strip()

    # ---------- memory ----------

    def _rules_and_memories(self, msg: Incoming, query: str):
        pinned = self.store.always_memories(msg.group_id, msg.user_id)
        pinned_ids = {int(r["id"]) for r in pinned}
        found = self.store.search(
            f"{query} {msg.nickname}", msg.group_id, msg.user_id, limit=8, exclude=pinned_ids
        )
        merged = list(pinned) + [r for r in found if int(r["id"]) not in pinned_ids]
        return pinned, merged

    def _save_extracted(self, msg: Incoming, payload: str) -> int:
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            log.warning("extract: non-json output: %s", payload[:200])
            return 0
        items = data.get("memories") or []
        if not isinstance(items, list):
            return 0
        min_conf = float(self.cfg["memory"]["min_confidence"])
        saved = 0
        for item in items[: int(self.cfg["memory"]["max_items"])]:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind", "fact"))
            value = str(item.get("value", "")).strip()
            key = str(item.get("key", "")).strip()
            try:
                conf = float(item.get("confidence", 0.8))
            except (TypeError, ValueError):
                conf = 0.8
            if kind not in ("fact", "pref", "rule") or len(value) < 4 or conf < min_conf:
                continue
            self.store.upsert_memory(
                scope="user",
                owner=msg.user_id,
                kind=kind,
                key=key or value[:12],
                value=value,
                source=f"msg:{msg.msg_id}",
                pinned=(kind == "rule"),
                weight=min(1.6, 0.8 + conf * 0.8),
            )
            saved += 1
        if saved:
            log.info("learned %d memories from %s", saved, msg.user_id)
        return saved

    # ---------- state ----------

    def score_state_async(self, msg: Incoming) -> None:
        if not self.state.enabled or not self.cfg["state"].get("score_with_llm", True):
            return
        if not self.cfg.is_dad(msg.user_id):
            return
        cooldown = float(self.cfg["state"].get("score_cooldown_min", 8)) * 60
        now = time.time()
        if now - self._last_score.get(msg.group_id, 0.0) < cooldown:
            return
        self._last_score[msg.group_id] = now
        current = self.state.get(msg.group_id)
        messages = P.build_score(msg.text, current)

        def run() -> None:
            try:
                out, _ = self.llm.chat(messages, temperature=0, max_tokens=200, json_mode=True)
                data = json.loads(out)
                self.state.apply(
                    msg.group_id,
                    {
                        "trust": data.get("trust", 0),
                        "independence": data.get("independence", 0),
                        "responsibility": data.get("responsibility", 0),
                    },
                    str(data.get("reason", ""))[:40],
                    msg.msg_id,
                )
            except (LLMError, json.JSONDecodeError) as exc:
                log.warning("state score failed: %s", exc)
            except Exception:
                log.exception("state score crashed")

        self.pool.submit(run)

    def learn_async(self, msg: Incoming) -> None:
        if not self.cfg["memory"].get("extract_online", True):
            return
        text = msg.text
        if len(text) < 6 or len(text) > 800:
            return
        cooldown = float(self.cfg["memory"].get("learn_cooldown_sec", 25))
        if time.time() - self._last_learn.get(msg.user_id, 0.0) < cooldown:
            return
        self._last_learn[msg.user_id] = time.time()
        known = [r["value"] for r in self.store.remember_list(msg.group_id, msg.user_id, limit=30)]
        messages = P.build_extract(msg.nickname, msg.user_id, text, known)

        def run() -> None:
            try:
                out, usage = self.llm.chat(messages, temperature=0.1, max_tokens=400, json_mode=True)
                self.store.add_usage(
                    self.llm.model,
                    int(usage.get("prompt_tokens", 0)),
                    int(usage.get("completion_tokens", 0)),
                )
                self._save_extracted(msg, out)
            except (LLMError, RuntimeError) as exc:
                log.warning("extract failed: %s", exc)
            except Exception:
                log.exception("extract crashed")

        self.pool.submit(run)

    def worth_learning(self, msg: Incoming, addressed: bool) -> bool:
        """Gate extra LLM calls: dad always, others only on self-disclosure."""
        if addressed or self.cfg.is_dad(msg.user_id):
            return True
        if not self.cfg["memory"].get("learn_from_group", True):
            return False
        return bool(SELF_DISCLOSURE.search(msg.text))

    # ---------- commands ----------

    def handle_command(self, msg: Incoming) -> str | None:
        text = self.strip_wake(msg.text)
        if LIST_RE.match(text):
            rows = self.store.remember_list(msg.group_id, msg.user_id, limit=25)
            if not rows:
                return "现在还没记住什么。"
            head = "我记得这些：\n"
            body = "\n".join(f"{i}. {r['value']}" for i, r in enumerate(rows, 1))
            return clean_reply(head + body, 1200)

        m = COMMAND_RE.match(text)
        if m:
            value = m.group("mem").strip()
            if len(value) < 2:
                return "记什么，说完整点。"
            kind = "rule" if RULE_HINT.search(value) else "fact"
            self.store.upsert_memory(
                scope="user",
                owner=msg.user_id,
                kind=kind,
                key=value[:14],
                value=f"{msg.nickname}：{value}" if not value.startswith(msg.nickname) else value,
                source=f"manual:{msg.msg_id}",
                pinned=True,
                weight=1.6,
            )
            return "记下了。"

        m = FORGET_RE.match(text)
        if m:
            removed = self.store.delete_matching(msg.group_id, msg.user_id, m.group("mem").strip())
            if not removed:
                return "没找到对应的记忆。"
            return f"删掉了{len(removed)}条。"

        if re.match(r"^\s*你记得我什么", text):
            rows = self.store.remember_list(msg.group_id, msg.user_id, limit=25)
            return clean_reply("\n".join(f"{i}. {r['value']}" for i, r in enumerate(rows, 1)) or "还没记住什么。", 1200)

        return None

    # ---------- reply ----------

    def ingest(self, msg: Incoming, bot_id: str, force: bool = False) -> None:
        """Record every group message, then reply only when addressed."""
        fresh = self.store.add_message(
            msg.group_id,
            msg.user_id,
            msg.nickname,
            "user",
            msg.text,
            msg_id=msg.msg_id or None,
            reply_to=msg.reply_to,
        )
        if not fresh:
            log.debug("duplicate message %s skipped", msg.msg_id)
            return
        addressed = force or self.should_reply(msg, bot_id)
        if addressed:
            self.reply(msg, bot_id)
        elif self.worth_learning(msg, addressed=False):
            # Dad's remarks and others' self-disclosures are the only passive
            # sources; idle chatter never triggers an extraction call.
            self.learn_async(msg)
        if self.cfg.is_dad(msg.user_id) and not addressed:
            self.score_state_async(msg)

    def reply(self, msg: Incoming, bot_id: str) -> None:
        if not self.rate.allow(msg.group_id, msg.user_id):
            log.info("rate limited in group %s", msg.group_id)
            return

        command_out = self.handle_command(msg)
        if command_out is not None:
            self._send(msg, command_out, bot_id)
            return

        query = self.strip_wake(msg.text) or msg.text
        rules, memories = self._rules_and_memories(msg, query)
        history = self.store.history(
            msg.group_id, int(self.cfg["behavior"]["history_window"]) + 1
        )
        history = [r for r in history if r["msg_id"] != msg.msg_id]
        history = history[-int(self.cfg["behavior"]["history_window"]) :]
        budget = int(self.cfg["behavior"].get("max_context_chars", 6000))
        total = 0
        kept: list = []
        for row in reversed(history):
            total += len(row["content"]) + len(row["nickname"] or "")
            if total > budget and kept:
                break
            kept.append(row)
        history = list(reversed(kept))
        max_chars = int(self.cfg["behavior"]["max_reply_chars"])
        if self.cfg.is_dad(msg.user_id) and self.state.sulky(msg.group_id):
            max_chars = min(max_chars, 40)
        messages = P.build(
            persona=self.persona(msg.group_id),
            group_id=msg.group_id,
            speaker_id=msg.user_id,
            speaker_name=msg.nickname,
            rules=rules,
            memories=memories,
            history=history,
            user_text=query,
            max_reply_chars=max_chars,
            memory_budget=int(self.cfg["behavior"]["memory_budget_chars"]),
        )
        try:
            text, usage = self.llm.chat(messages)
        except (LLMError, RuntimeError) as exc:
            log.error("llm failed: %s", exc)
            return
        self.store.add_usage(
            self.llm.model,
            int(usage.get("prompt_tokens", 0)),
            int(usage.get("completion_tokens", 0)),
        )
        text = clean_reply(text, max_chars)
        if not text:
            return
        self._send(msg, text, bot_id)
        if self.cfg["behavior"].get("learn_from", "addressed") != "off":
            self.learn_async(msg)
        if self.cfg.is_dad(msg.user_id):
            self.score_state_async(msg)

    def _send(self, msg: Incoming, text: str, bot_id: str) -> None:
        if self.cfg["behavior"].get("dry_run"):
            log.info("[dry_run] would send to %s: %s", msg.group_id, text)
            return
        try:
            mid = self.api.send_group(
                msg.group_id,
                text,
                reply_to=msg.msg_id if self.cfg["behavior"].get("reply_with_at", True) else None,
                at=msg.user_id if self.cfg["behavior"].get("reply_with_at", True) else None,
            )
            self.store.add_message(
                msg.group_id,
                bot_id or "bot",
                self.cfg["bot_name"],
                "assistant",
                text,
                msg_id=mid or None,
            )
        except RuntimeError as exc:
            log.error("send failed: %s", exc)

    def submit(self, event: dict, bot_id: str) -> None:
        from .onebot import normalize

        msg = normalize(event)
        if not msg:
            return
        self.pool.submit(self._guarded_ingest, msg, bot_id)

    def _guarded_ingest(self, msg: Incoming, bot_id: str) -> None:
        try:
            self.ingest(msg, bot_id)
        except Exception:
            log.exception("ingest crashed")

    def close(self) -> None:
        self.pool.shutdown(wait=True, cancel_futures=True)
        self.store.close()
