"""OneBot v11 helpers: event normalization and HTTP API calls."""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field

log = logging.getLogger("onebot")

CQ_RE = re.compile(r"\[CQ:([a-zA-Z_]+)((?:,[^\]]*)?)\]")


def _cq_unescape(text: str) -> str:
    return (
        text.replace("&#91;", "[")
        .replace("&#93;", "]")
        .replace("&#44;", ",")
        .replace("&amp;", "&")
    )


def parse_segments(raw) -> tuple[str, list[str], str | None]:
    """Return (plain_text, at_targets, reply_msg_id) from array or CQ string."""
    if raw is None:
        return "", [], None
    if isinstance(raw, str):
        ats: list[str] = []
        reply: str | None = None

        def repl(match: re.Match) -> str:
            nonlocal reply
            kind, params = match.group(1), match.group(2)
            data = {}
            for part in params.lstrip(",").split(","):
                if "=" in part:
                    k, _, v = part.partition("=")
                    data[k] = _cq_unescape(v)
            if kind == "at":
                ats.append(data.get("qq", ""))
                return ""
            if kind == "reply":
                reply = data.get("id")
                return ""
            if kind == "face":
                return ""
            if kind == "image":
                return "[图片]"
            return data.get("text", "")

        text = _cq_unescape(CQ_RE.sub(repl, raw))
        return text.strip(), ats, reply

    parts: list[str] = []
    ats = []
    reply = None
    if isinstance(raw, dict):
        raw = [raw]
    for seg in raw or []:
        if not isinstance(seg, dict):
            continue
        kind = seg.get("type")
        data = seg.get("data") or {}
        if kind == "text":
            parts.append(str(data.get("text", "")))
        elif kind == "at":
            ats.append(str(data.get("qq", "")))
        elif kind == "reply":
            reply = str(data.get("id", "")) or None
        elif kind == "image":
            parts.append("[图片]")
        elif kind == "face":
            continue
        elif kind == "mface":
            parts.append("[表情]")
    return "".join(parts).strip(), ats, reply


@dataclass
class Incoming:
    group_id: str
    user_id: str
    nickname: str
    text: str
    msg_id: str
    at_targets: list[str] = field(default_factory=list)
    reply_to: str | None = None
    self_id: str = ""
    raw: dict = field(default_factory=dict)


def normalize(event: dict) -> Incoming | None:
    if event.get("post_type") != "message":
        return None
    if event.get("message_type") != "group":
        return None
    sender = event.get("sender") or {}
    text, ats, reply = parse_segments(event.get("message", event.get("raw_message", "")))
    return Incoming(
        group_id=str(event.get("group_id", "")),
        user_id=str(event.get("user_id", "")),
        nickname=str(sender.get("card") or sender.get("nickname") or event.get("user_id", "")),
        text=text,
        msg_id=str(event.get("message_id", "")),
        at_targets=ats,
        reply_to=reply,
        self_id=str(event.get("self_id", "")),
        raw=event,
    )


class OneBotAPI:
    def __init__(self, base_url: str, token: str = ""):
        self.base_url = base_url.rstrip("/")
        self.token = token

    def _call(self, action: str, payload: dict, timeout: int = 20) -> dict:
        req = urllib.request.Request(
            f"{self.base_url}/{action}",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                **({"Authorization": f"Bearer {self.token}"} if self.token else {}),
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"{action} http {exc.code}: {exc.read()[:300]!r}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RuntimeError(f"{action} unreachable: {exc}") from exc
        if data.get("status") == "failed" or data.get("retcode") not in (0, None):
            raise RuntimeError(f"{action} failed: {str(data)[:300]}")
        return data

    def send_group(self, group_id: str, text: str, reply_to: str | None = None, at: str | None = None) -> str:
        segments: list[dict] = []
        if reply_to:
            segments.append({"type": "reply", "data": {"id": str(reply_to)}})
        if at:
            segments.append({"type": "at", "data": {"qq": str(at)}})
            segments.append({"type": "text", "data": {"text": " "}})
        segments.append({"type": "text", "data": {"text": text}})
        data = self._call("send_group_msg", {"group_id": int(group_id), "message": segments})
        return str((data.get("data") or {}).get("message_id", ""))

    def self_id(self) -> str:
        data = self._call("get_login_info", {}, timeout=10)
        return str((data.get("data") or {}).get("user_id", ""))
