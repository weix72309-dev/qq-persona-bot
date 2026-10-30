"""Config and .env loading. Standard library only."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

DEFAULTS = {
    "listen": {"host": "127.0.0.1", "port": 8088, "path": "/onebot", "token": ""},
    "onebot": {"api_base": "http://127.0.0.1:3000", "token": ""},
    "llm": {
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
        "api_key_env": "DEEPSEEK_API_KEY",
        "temperature": 0.75,
        "max_tokens": 700,
        "timeout": 90,
    },
    "behavior": {
        "wake_words": ["小满"],
        "groups": [],
        "reply_with_at": True,
        "respond_to_reply": True,
        "max_reply_chars": 300,
        "history_window": 16,
        "max_context_chars": 6000,
        "memory_budget_chars": 1400,
        "max_per_group_per_min": 6,
        "max_per_user_per_min": 3,
        "workers": 4,
        "learn_from": "addressed",
        "dry_run": False,
    },
    "memory": {
        "min_confidence": 0.6,
        "retention_days": 60,
        "max_items": 5,
        "compact_hours": 6,
        "digest_after_minutes": 30,
        "learn_cooldown_sec": 25,
        "learn_from_group": True,
    },
    "admin_qq": [],
    "dad_qq": [],
    "persona_vars": {"name": "小满", "dad": "爸爸"},
    "state": {
        "enabled": True,
        "initial": {"trust": 62, "independence": 45, "responsibility": 40},
        "score_with_llm": True,
        "score_cooldown_min": 8,
        "stale_decay_days": 14,
    },
    "bot_name": "小满",
    "persona": "persona.md",
    "db": "data/bot.db",
    "log": "data/bot.log",
}


def load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


class Config:
    def __init__(self, path: Path | str):
        self.path = Path(path).resolve()
        self.root = self.path.parent
        load_dotenv(self.root / ".env")
        if not self.path.is_file():
            raise SystemExit(f"config not found: {self.path}")
        self.data = deep_merge(DEFAULTS, json.loads(self.path.read_text(encoding="utf-8")))
        llm = self.data["llm"]
        llm["api_key"] = os.environ.get(llm["api_key_env"], "") or llm.get("api_key", "")
        self.data["onebot"]["token"] = os.environ.get("ONEBOT_TOKEN", "") or self.data["onebot"].get("token", "")
        self.persona_path = (self.root / self.data["persona"]).resolve()
        self.db_path = (self.root / self.data["db"]).resolve()
        self.log_path = (self.root / self.data["log"]).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def __getitem__(self, key: str):
        return self.data[key]

    def get(self, key: str, default=None):
        return self.data.get(key, default)

    def persona(self) -> str:
        if not self.persona_path.is_file():
            raise SystemExit(f"persona not found: {self.persona_path}")
        return self.persona_path.read_text(encoding="utf-8").strip()

    def is_admin(self, user_id: str) -> bool:
        return str(user_id) in {str(x) for x in self.data.get("admin_qq", [])}

    def is_dad(self, user_id: str) -> bool:
        return str(user_id) in {str(x) for x in self.data.get("dad_qq", [])}

    def groups_allowed(self) -> list[str]:
        return [str(x) for x in self.data["behavior"].get("groups", [])]

    def require_api_key(self) -> str:
        key = self.data["llm"].get("api_key", "")
        if not key:
            raise SystemExit(
                f"missing API key: put {self.data['llm']['api_key_env']}=... in {self.root / '.env'}"
            )
        return key


def setup_logging(path: Path | None, level: str = "INFO") -> None:
    import logging
    from logging.handlers import RotatingFileHandler

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)
    if path:
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(path, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)

