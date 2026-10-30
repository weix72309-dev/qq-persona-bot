"""Nightly maintenance: digest chats, merge duplicates, expire stale memories.

Run: python -m bot.consolidate [--config config.json] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from collections import defaultdict
from pathlib import Path

from . import prompt as P
from .config import Config, setup_logging
from .llm import LLM, LLMError
from .state import StateEngine
from .store import Store, norm_key, terms

log = logging.getLogger("consolidate")


def digest_groups(store: Store, llm: LLM, cfg: Config, hours: int, dry_run: bool) -> int:
    since = int(time.time()) - hours * 3600
    made = 0
    for group_id in store.recent_groups(hours=hours):
        text = store.recent_transcript(group_id, since)
        if len(text) < 200:
            continue
        if dry_run:
            log.info("[dry-run] would digest group %s (%d chars)", group_id, len(text))
            continue
        try:
            out, usage = llm.chat(
                P.build_digest(text), temperature=0.2, max_tokens=500, json_mode=True
            )
        except LLMError as exc:
            log.warning("digest failed for %s: %s", group_id, exc)
            continue
        store.add_usage(
            llm.model, int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0))
        )
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            log.warning("digest non-json for %s: %s", group_id, out[:200])
            continue
        summary = str(data.get("digest", "")).strip()
        if len(summary) >= 10:
            store.upsert_memory(
                scope="group",
                owner=group_id,
                kind="digest",
                key=time.strftime("%Y-%m-%d-%H"),
                value=summary,
                source="digest",
                weight=0.9,
            )
            made += 1
        for fact in (data.get("facts") or [])[:4]:
            value = str(fact.get("value", "")).strip()
            key = str(fact.get("key", "")).strip() or value[:12]
            if len(value) < 6:
                continue
            store.upsert_memory(
                scope="group",
                owner=group_id,
                kind="note",
                key=key,
                value=value,
                source="digest",
                weight=0.8,
            )
    return made


def merge_duplicates(store: Store, dry_run: bool) -> int:
    """Collapse near-identical memories that slipped past key-based upsert."""
    rows = store.all_active()
    by_slot: dict[tuple, list] = defaultdict(list)
    for row in rows:
        if row["kind"] == "digest":
            continue
        by_slot[(row["scope"], row["owner"], row["kind"])].append(row)
    merged = 0
    for slot, items in by_slot.items():
        kept: list = []
        for row in sorted(items, key=lambda r: (r["pinned"], r["updated_at"], r["id"]), reverse=True):
            toks = terms(row["value"])
            dup = None
            for other in kept:
                other_toks = terms(other["value"])
                if not toks or not other_toks:
                    continue
                jaccard = len(toks & other_toks) / len(toks | other_toks)
                if jaccard >= 0.82:
                    dup = other
                    break
            if dup is None:
                kept.append(row)
                continue
            merged += 1
            if dry_run:
                log.info("[dry-run] merge %r into %r", row["value"], dup["value"])
                continue
            if len(row["value"]) > len(dup["value"]):
                store.upsert_memory(
                    scope=dup["scope"],
                    owner=dup["owner"],
                    kind=dup["kind"],
                    key=norm_key(row["value"]) or row["key"],
                    value=row["value"],
                    pinned=bool(dup["pinned"] or row["pinned"]),
                    weight=max(float(dup["weight"]), float(row["weight"])),
                )
                store.delete_memory(int(dup["id"]))
            else:
                store.delete_memory(int(row["id"]))
    return merged


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="nightly memory consolidation")
    parser.add_argument("--config", default=str(Path.cwd() / "config.json"))
    parser.add_argument("--hours", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    cfg = Config(args.config)
    cfg.require_api_key()
    setup_logging(cfg.log_path, "INFO")
    store = Store(cfg.db_path)
    llm = LLM(cfg["llm"])
    hours = args.hours or int(cfg["memory"].get("compact_hours", 6))

    digests = digest_groups(store, llm, cfg, hours, args.dry_run)
    merged = merge_duplicates(store, args.dry_run)
    decayed = 0 if args.dry_run else StateEngine(store, cfg).decay(
        int(cfg["state"].get("stale_decay_days", 14))
    )
    expired = 0 if args.dry_run else store.expire(int(cfg["memory"]["retention_days"]))
    pruned = 0 if args.dry_run else store.prune_messages(int(cfg["memory"]["retention_days"]))
    log.info(
        "consolidated: digests=%d merged=%d decayed_groups=%d expired=%d pruned_messages=%d stats=%s",
        digests,
        merged,
        decayed,
        expired,
        pruned,
        store.stats(),
    )
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
