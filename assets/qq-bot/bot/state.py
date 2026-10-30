"""Persona state: trust / independence / responsibility, moved only by dad.

The LLM proposes small deltas in [-2, 2]; this module clamps them, enforces a
daily budget, logs every change, and renders a deterministic stage block.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

from .store import Store

log = logging.getLogger("state")

AXES = ("trust", "independence", "responsibility")
CN = {"trust": "信任", "independence": "独立", "responsibility": "责任"}
MAX_PER_MESSAGE = 2.0
MAX_PER_DAY = 6.0

TRUST_STAGES = [
    (25, "冰点期", "他说什么你都想顶回去，回话很短，带刺，不主动开口。"),
    (45, "别扭期", "嘴上不饶人，心里其实在听；关心要拐个弯说，说完马上找补。"),
    (65, "常态期", "能正常聊，还是会嫌他啰嗦；该说的事会说，不多说。"),
    (85, "松口期", "会主动讲自己的事，偶尔撒个娇，撒完立刻装作没事。"),
    (101, "黏人期", "会主动找他，会惦记他，嘴硬还在，但明显软了。"),
]

INDEPENDENCE_BANDS = [
    (35, "遇事第一反应是问他，拿不定主意。"),
    (65, "会自己先想，拿不准才问。"),
    (101, "自己拍板，事后通知他一声。"),
]

RESPONSIBILITY_BANDS = [
    (35, "答应的事容易拖，要人催。"),
    (65, "答应了会做，但过程要抱怨两句。"),
    (101, "会主动提醒别人别忘了事。"),
]


def _pick(value: float, table) -> tuple:
    for row in table:
        if value < row[0]:
            return row[1:]
    return table[-1][1:]


def _band(value: float, table) -> str:
    return _pick(value, table)[0]


def _index(value: float, table) -> int:
    for i, row in enumerate(table):
        if value < row[0]:
            return i
    return len(table) - 1


def _stable_index(value: float, table, prev: int | None, hysteresis: float = 3.0) -> int:
    """Only cross a band edge after moving `hysteresis` points past it."""
    idx = _index(value, table)
    if prev is None or prev == idx or not (0 <= prev < len(table)):
        return idx
    if idx > prev and value < table[prev][0] + hysteresis:
        return prev
    if idx < prev and value > table[prev - 1][0] - hysteresis:
        return prev
    return idx


def day_start(ts: float | None = None) -> int:
    tm = time.localtime(ts or time.time())
    return int(time.mktime((tm.tm_year, tm.tm_mon, tm.tm_mday, 0, 0, 0, 0, 0, -1)))


class StateEngine:
    def __init__(self, store: Store, cfg):
        self.store = store
        self.cfg = cfg
        self.initial = dict(cfg.get("state", {}).get("initial", {}))
        self.enabled = bool(cfg.get("state", {}).get("enabled", True))

    def get(self, group_id: str) -> dict:
        row = self.store.ensure_state(group_id, self.initial)
        return {axis: float(row[axis]) for axis in AXES}

    def apply(
        self,
        group_id: str,
        proposed: dict,
        reason: str,
        source_msg: str | None = None,
    ) -> dict:
        if not self.enabled:
            return {}
        current = self.get(group_id)
        used = self.store.state_net_up(group_id, day_start())
        applied: dict[str, float] = {}
        for axis in AXES:
            try:
                delta = float(proposed.get(axis, 0) or 0)
            except (TypeError, ValueError):
                delta = 0.0
            if abs(delta) < 0.5:
                continue
            delta = max(-MAX_PER_MESSAGE, min(MAX_PER_MESSAGE, delta))
            delta = float(round(delta))
            # Only upward drift is capped: praise cannot be farmed, but a bad
            # moment should still register the same day.
            if delta > 0:
                budget = MAX_PER_DAY - used.get(axis, 0.0)
                if budget < 1:
                    continue
                delta = min(delta, budget)
            new_value = max(0.0, min(100.0, current[axis] + delta))
            delta = new_value - current[axis]
            if abs(delta) < 0.5:
                continue
            applied[axis] = delta
            current[axis] = new_value
        if not applied:
            return {}
        self.store.save_state(group_id, current)
        self.store.add_state_event(group_id, applied, reason or "互动", source_msg)
        log.info("state %s %s (%s)", group_id, applied, reason)
        return applied

    def sulky(self, group_id: str, hours: float = 2.0) -> bool:
        """Short replies only after a real hit, until something nice lands."""
        rows = self.store.state_events(group_id, int(time.time() - hours * 3600), limit=10)
        for row in rows:
            delta = float(row["trust"])
            if delta > 0:
                return False
            if delta <= -2:
                return True
        return False

    def render(self, group_id: str, recent_hours: int = 72) -> str:
        values = self.get(group_id)
        meta_key = f"stage:{group_id}"
        raw_prev = self.store.get_meta(meta_key, "")
        prev = int(raw_prev) if raw_prev.isdigit() else None
        idx = _stable_index(values["trust"], TRUST_STAGES, prev)
        if raw_prev != str(idx):
            self.store.set_meta(meta_key, str(idx))
        stage_name, stage_text = TRUST_STAGES[idx][1], TRUST_STAGES[idx][2]
        ind_text = _band(values["independence"], INDEPENDENCE_BANDS)
        resp_text = _band(values["responsibility"], RESPONSIBILITY_BANDS)
        lines = [
            f"当前状态：信任 {values['trust']:.0f}/100，独立 {values['independence']:.0f}/100，"
            f"责任 {values['responsibility']:.0f}/100",
            f"当前阶段：{stage_name}。{stage_text}",
            f"独立：{ind_text}责任：{resp_text}",
        ]
        rows = self.store.state_events(group_id, int(time.time() - recent_hours * 3600), limit=3)
        recent = []
        for row in rows:
            parts = [
                f"{CN[a]}{row[a]:+.0f}" for a in AXES if abs(float(row[a])) >= 1
            ]
            if parts:
                recent.append(f"{row['reason']}（{'，'.join(parts)}）")
        if recent:
            lines.append("最近的变化：" + "；".join(recent))
        lines.append("数值只决定你现在怎么演，不要把数字说出来，也不要说自己在演。")
        return "\n".join(lines)

    def decay(self, stale_days: int = 14, step: float = 1.0) -> int:
        if not self.enabled:
            return 0
        changed = 0
        for group_id in self.store.state_groups():
            rows = self.store.state_events(group_id, int(time.time() - stale_days * 86400), limit=1)
            if rows:
                continue
            values = self.get(group_id)
            before = dict(values)
            for axis in AXES:
                if abs(values[axis] - 50.0) < 1:
                    continue
                values[axis] = max(0.0, min(100.0, values[axis] + (step if values[axis] < 50 else -step)))
            moved = {a: values[a] - before[a] for a in AXES if abs(values[a] - before[a]) >= 1}
            if moved:
                self.store.save_state(group_id, values)
                self.store.add_state_event(group_id, moved, "一段时间没联系")
                changed += 1
        return changed


def _fmt(values: dict) -> str:
    return "，".join(f"{CN[a]} {values[a]:.0f}" for a in AXES)


def main(argv: list[str] | None = None) -> int:
    from .config import Config, setup_logging

    parser = argparse.ArgumentParser(description="inspect or override persona state")
    parser.add_argument("--config", default=str(Path.cwd() / "config.json"))
    parser.add_argument("--group", default="0")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("show")
    setter = sub.add_parser("set")
    setter.add_argument("axis", choices=list(AXES))
    setter.add_argument("value", type=float)
    args = parser.parse_args(argv)

    cfg = Config(args.config)
    setup_logging(None, "WARNING")
    store = Store(cfg.db_path)
    engine = StateEngine(store, cfg)
    if args.cmd == "show":
        print(_fmt(engine.get(args.group)))
        print(engine.render(args.group))
    else:
        values = engine.get(args.group)
        values[args.axis] = max(0.0, min(100.0, args.value))
        store.save_state(args.group, values)
        print("now:", _fmt(values))
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
