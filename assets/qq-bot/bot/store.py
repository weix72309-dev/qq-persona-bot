"""SQLite storage: messages, long-term memories, lexical index, usage."""

from __future__ import annotations

import re
import sqlite3
import threading
import time
from pathlib import Path

CJK_RE = re.compile(r"[\u4e00-\u9fff]+")
WORD_RE = re.compile(r"[a-z0-9][a-z0-9_\-]{1,}")
STRIP_RE = re.compile(r"[\s,.!?;:'\"，。！？；：、“”‘’（）()\[\]【】]+")

KINDS = ("fact", "pref", "rule", "note", "digest")

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  group_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  nickname TEXT,
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  msg_id TEXT,
  reply_to TEXT,
  ts INTEGER NOT NULL,
  distilled INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_messages_msgid ON messages(msg_id) WHERE msg_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_messages_group_ts ON messages(group_id, ts);

CREATE TABLE IF NOT EXISTS memories(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scope TEXT NOT NULL,
  owner TEXT NOT NULL,
  kind TEXT NOT NULL,
  key TEXT NOT NULL,
  value TEXT NOT NULL,
  pinned INTEGER NOT NULL DEFAULT 0,
  weight REAL NOT NULL DEFAULT 1.0,
  source TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  deleted INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_memories ON memories(scope, owner, kind, key);

CREATE TABLE IF NOT EXISTS memory_terms(
  memory_id INTEGER NOT NULL,
  term TEXT NOT NULL,
  PRIMARY KEY(memory_id, term)
);
CREATE INDEX IF NOT EXISTS ix_memory_terms ON memory_terms(term);

CREATE TABLE IF NOT EXISTS usage(
  day TEXT NOT NULL,
  model TEXT NOT NULL,
  prompt_tokens INTEGER NOT NULL DEFAULT 0,
  completion_tokens INTEGER NOT NULL DEFAULT 0,
  calls INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(day, model)
);

CREATE TABLE IF NOT EXISTS meta(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS persona_state(
  group_id TEXT PRIMARY KEY,
  trust REAL NOT NULL,
  independence REAL NOT NULL,
  responsibility REAL NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS state_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  group_id TEXT NOT NULL,
  trust REAL NOT NULL DEFAULT 0,
  independence REAL NOT NULL DEFAULT 0,
  responsibility REAL NOT NULL DEFAULT 0,
  reason TEXT NOT NULL,
  source_msg TEXT,
  ts INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_state_events ON state_events(group_id, ts);
"""


def terms(text: str) -> set[str]:
    """Latin words plus CJK bigrams; no external tokenizer needed."""
    lowered = (text or "").lower()
    out: set[str] = set(WORD_RE.findall(lowered))
    for run in CJK_RE.findall(lowered):
        if len(run) == 1:
            out.add(run)
            continue
        out.update(run[i : i + 2] for i in range(len(run) - 1))
    return out


def norm_key(text: str) -> str:
    return STRIP_RE.sub("", (text or "").lower())[:64]


class Store:
    def __init__(self, path: Path | str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(path), check_same_thread=False, timeout=15)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.executescript(SCHEMA)
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            if getattr(self, "_closed", False):
                return
            self._closed = True
            self._db.close()

    # ---------- messages ----------

    def add_message(
        self,
        group_id: str,
        user_id: str,
        nickname: str,
        role: str,
        content: str,
        msg_id: str | None = None,
        reply_to: str | None = None,
        ts: int | None = None,
    ) -> bool:
        with self._lock:
            try:
                self._db.execute(
                    "INSERT INTO messages(group_id,user_id,nickname,role,content,msg_id,reply_to,ts)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (str(group_id), str(user_id), nickname or "", role, content, msg_id, reply_to, ts or int(time.time())),
                )
                self._db.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def history(self, group_id: str, limit: int) -> list[sqlite3.Row]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM messages WHERE group_id=? ORDER BY id DESC LIMIT ?",
                (str(group_id), limit),
            ).fetchall()
        return list(reversed(rows))

    def is_bot_message(self, msg_id: str) -> bool:
        if not msg_id:
            return False
        with self._lock:
            row = self._db.execute(
                "SELECT 1 FROM messages WHERE msg_id=? AND role='assistant' LIMIT 1", (str(msg_id),)
            ).fetchone()
        return row is not None

    def undistilled(self, older_than: int, limit: int = 300) -> list[sqlite3.Row]:
        cutoff = int(time.time()) - older_than
        with self._lock:
            return self._db.execute(
                "SELECT * FROM messages WHERE distilled=0 AND role='user' AND ts<? ORDER BY id LIMIT ?",
                (cutoff, limit),
            ).fetchall()

    def mark_distilled(self, ids: list[int]) -> None:
        if not ids:
            return
        with self._lock:
            self._db.executemany("UPDATE messages SET distilled=1 WHERE id=?", [(i,) for i in ids])
            self._db.commit()

    def recent_groups(self, hours: int = 48) -> list[str]:
        cutoff = int(time.time()) - hours * 3600
        with self._lock:
            rows = self._db.execute(
                "SELECT DISTINCT group_id FROM messages WHERE ts>=?", (cutoff,)
            ).fetchall()
        return [r["group_id"] for r in rows]

    def recent_transcript(self, group_id: str, since_ts: int, limit: int = 400) -> str:
        with self._lock:
            rows = self._db.execute(
                "SELECT nickname,role,content FROM messages WHERE group_id=? AND ts>=? ORDER BY id LIMIT ?",
                (str(group_id), since_ts, limit),
            ).fetchall()
        lines = []
        for row in rows:
            who = "bot" if row["role"] == "assistant" else (row["nickname"] or "?")
            lines.append(f"{who}: {row['content']}")
        return "\n".join(lines)

    def prune_messages(self, retention_days: int) -> int:
        cutoff = int(time.time()) - retention_days * 86400
        with self._lock:
            cur = self._db.execute("DELETE FROM messages WHERE ts<?", (cutoff,))
            self._db.commit()
        return cur.rowcount

    # ---------- memories ----------

    def upsert_memory(
        self,
        scope: str,
        owner: str,
        kind: str,
        key: str,
        value: str,
        source: str = "",
        pinned: bool = False,
        weight: float = 1.0,
    ) -> int:
        if kind not in KINDS:
            raise ValueError(f"bad kind: {kind}")
        key = norm_key(key) or norm_key(value)
        now = int(time.time())
        with self._lock:
            row = self._db.execute(
                "SELECT id FROM memories WHERE scope=? AND owner=? AND kind=? AND key=?",
                (scope, str(owner), kind, key),
            ).fetchone()
            if row:
                mid = int(row["id"])
                self._db.execute(
                    "UPDATE memories SET value=?, pinned=MAX(pinned,?), weight=MAX(weight,?),"
                    " source=?, updated_at=?, deleted=0 WHERE id=?",
                    (value, int(pinned), weight, source, now, mid),
                )
            else:
                cur = self._db.execute(
                    "INSERT INTO memories(scope,owner,kind,key,value,pinned,weight,source,created_at,updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (scope, str(owner), kind, key, value, int(pinned), weight, source, now, now),
                )
                mid = int(cur.lastrowid)
            self._reindex(mid, f"{key} {value}")
            self._db.commit()
        return mid

    def _reindex(self, memory_id: int, text: str) -> None:
        self._db.execute("DELETE FROM memory_terms WHERE memory_id=?", (memory_id,))
        self._db.executemany(
            "INSERT OR IGNORE INTO memory_terms(memory_id,term) VALUES(?,?)",
            [(memory_id, t) for t in terms(text)],
        )

    def delete_memory(self, memory_id: int) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT value FROM memories WHERE id=? AND deleted=0", (memory_id,)).fetchone()
            if not row:
                return None
            self._db.execute("UPDATE memories SET deleted=1 WHERE id=?", (memory_id,))
            self._db.commit()
        return row["value"]

    def delete_matching(self, group_id: str, user_id: str, needle: str) -> list[str]:
        needle = (needle or "").strip()
        if not needle:
            return []
        with self._lock:
            rows = self._db.execute(
                "SELECT id,value FROM memories WHERE deleted=0 AND (scope='global' OR (scope='group' AND owner=?)"
                " OR (scope='user' AND owner=?)) AND value LIKE ?",
                (str(group_id), str(user_id), f"%{needle}%"),
            ).fetchall()
            for row in rows:
                self._db.execute("UPDATE memories SET deleted=1 WHERE id=?", (row["id"],))
            self._db.commit()
        return [r["value"] for r in rows]

    def always_memories(self, group_id: str, user_id: str, limit: int = 30) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(
                "SELECT * FROM memories WHERE deleted=0 AND pinned=1"
                " AND (scope='global' OR (scope='group' AND owner=?) OR (scope='user' AND owner=?))"
                " ORDER BY weight DESC, updated_at DESC LIMIT ?",
                (str(group_id), str(user_id), limit),
            ).fetchall()

    def search(
        self,
        query: str,
        group_id: str,
        user_id: str,
        limit: int = 8,
        exclude: set[int] | None = None,
    ) -> list[sqlite3.Row]:
        q = terms(query)
        if not q:
            return []
        exclude = exclude or set()
        marks = ",".join("?" for _ in q)
        with self._lock:
            hit_rows = self._db.execute(
                f"SELECT memory_id, COUNT(*) AS hits FROM memory_terms WHERE term IN ({marks})"
                " GROUP BY memory_id ORDER BY hits DESC LIMIT 200",
                list(q),
            ).fetchall()
            if not hit_rows:
                return []
            scores = {int(r["memory_id"]): int(r["hits"]) for r in hit_rows}
            ids = [i for i in scores if i not in exclude]
            if not ids:
                return []
            id_marks = ",".join("?" for _ in ids)
            rows = self._db.execute(
                f"SELECT * FROM memories WHERE id IN ({id_marks}) AND deleted=0"
                " AND (scope='global' OR (scope='group' AND owner=?) OR (scope='user' AND owner=?))",
                [*ids, str(group_id), str(user_id)],
            ).fetchall()
        norm = max(1, len(q)) ** 0.5
        now = time.time()
        ranked = []
        for row in rows:
            base = scores.get(int(row["id"]), 0) / norm
            age = max(0.0, (now - row["updated_at"]) / 86400)
            freshness = 1.0 + 0.3 * max(0.0, 1 - age / 90)
            ranked.append((base * float(row["weight"]) * freshness, row))
        ranked.sort(key=lambda item: item[0], reverse=True)
        return [row for _, row in ranked[:limit]]

    def remember_list(self, group_id: str, user_id: str, limit: int = 20) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(
                "SELECT * FROM memories WHERE deleted=0"
                " AND (scope='global' OR (scope='group' AND owner=?) OR (scope='user' AND owner=?))"
                " ORDER BY pinned DESC, updated_at DESC LIMIT ?",
                (str(group_id), str(user_id), limit),
            ).fetchall()

    def all_active(self) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute("SELECT * FROM memories WHERE deleted=0 ORDER BY id").fetchall()

    def expire(self, max_age_days: int) -> int:
        cutoff = int(time.time()) - max_age_days * 86400
        with self._lock:
            cur = self._db.execute(
                "UPDATE memories SET deleted=1 WHERE deleted=0 AND pinned=0"
                " AND kind IN ('fact','digest') AND updated_at<?",
                (cutoff,),
            )
            self._db.commit()
        return cur.rowcount

    # ---------- meta / usage ----------

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            self._db.commit()

    def get_meta(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self._db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def add_usage(self, model: str, prompt_tokens: int, completion_tokens: int) -> None:
        day = time.strftime("%Y-%m-%d")
        with self._lock:
            self._db.execute(
                "INSERT INTO usage(day,model,prompt_tokens,completion_tokens,calls) VALUES(?,?,?,?,1)"
                " ON CONFLICT(day,model) DO UPDATE SET prompt_tokens=prompt_tokens+excluded.prompt_tokens,"
                " completion_tokens=completion_tokens+excluded.completion_tokens, calls=calls+1",
                (day, model, prompt_tokens, completion_tokens),
            )
            self._db.commit()

    def usage_today(self) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute("SELECT * FROM usage WHERE day=?", (time.strftime("%Y-%m-%d"),)).fetchall()

    def stats(self) -> dict:
        with self._lock:
            mem = self._db.execute("SELECT COUNT(*) AS c FROM memories WHERE deleted=0").fetchone()["c"]
            msg = self._db.execute("SELECT COUNT(*) AS c FROM messages").fetchone()["c"]
        return {"memories": mem, "messages": msg}

    # ---------- persona state ----------

    def ensure_state(self, group_id: str, initial: dict) -> sqlite3.Row:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM persona_state WHERE group_id=?", (str(group_id),)
            ).fetchone()
            if row is None:
                self._db.execute(
                    "INSERT INTO persona_state(group_id,trust,independence,responsibility,updated_at)"
                    " VALUES(?,?,?,?,?)",
                    (
                        str(group_id),
                        float(initial.get("trust", 50)),
                        float(initial.get("independence", 50)),
                        float(initial.get("responsibility", 50)),
                        int(time.time()),
                    ),
                )
                self._db.commit()
                row = self._db.execute(
                    "SELECT * FROM persona_state WHERE group_id=?", (str(group_id),)
                ).fetchone()
        return row

    def save_state(self, group_id: str, values: dict) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE persona_state SET trust=?, independence=?, responsibility=?, updated_at=?"
                " WHERE group_id=?",
                (
                    float(values["trust"]),
                    float(values["independence"]),
                    float(values["responsibility"]),
                    int(time.time()),
                    str(group_id),
                ),
            )
            self._db.commit()

    def add_state_event(
        self, group_id: str, deltas: dict, reason: str, source_msg: str | None, ts: int | None = None
    ) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO state_events(group_id,trust,independence,responsibility,reason,source_msg,ts)"
                " VALUES(?,?,?,?,?,?,?)",
                (
                    str(group_id),
                    float(deltas.get("trust", 0)),
                    float(deltas.get("independence", 0)),
                    float(deltas.get("responsibility", 0)),
                    reason[:60],
                    source_msg,
                    ts or int(time.time()),
                ),
            )
            self._db.commit()

    def state_events(self, group_id: str, since_ts: int, limit: int = 20) -> list[sqlite3.Row]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM state_events WHERE group_id=? AND ts>=? ORDER BY id DESC LIMIT ?",
                (str(group_id), since_ts, limit),
            ).fetchall()
        return list(rows)

    def state_net_up(self, group_id: str, since_ts: int) -> dict:
        with self._lock:
            row = self._db.execute(
                "SELECT SUM(MAX(trust,0)) t, SUM(MAX(independence,0)) i, SUM(MAX(responsibility,0)) r"
                " FROM state_events WHERE group_id=? AND ts>=?",
                (str(group_id), since_ts),
            ).fetchone()
        return {
            "trust": float(row["t"] or 0),
            "independence": float(row["i"] or 0),
            "responsibility": float(row["r"] or 0),
        }

    def state_groups(self) -> list[str]:
        with self._lock:
            rows = self._db.execute("SELECT group_id FROM persona_state").fetchall()
        return [r["group_id"] for r in rows]
