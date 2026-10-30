"""Reverse-HTTP webhook server for OneBot v11 (NapCat / LLOneBot)."""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .config import Config
from .core import Bot

log = logging.getLogger("server")


class Handler(BaseHTTPRequestHandler):
    server_version = "qq-persona-bot/1.0"
    bot: Bot
    route: str = "/onebot"
    token: str

    def log_message(self, fmt: str, *args) -> None:
        log.debug("%s %s", self.address_string(), fmt % args)

    def _authorized(self) -> bool:
        if not self.token:
            return True
        header = self.headers.get("Authorization", "")
        if header == f"Bearer {self.token}" or header == f"Token {self.token}":
            return True
        query = parse_qs(urlparse(self.path).query)
        return self.token in query.get("access_token", [])

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if urlparse(self.path).path in ("/health", "/health/"):
            stats = self.bot.store.stats()
            self._json(200, {"ok": True, **stats})
        else:
            self._json(404, {"ok": False})

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_POST(self) -> None:
        # Compare against the configured route, not the raw path: NapCat may
        # append ?access_token=..., which made the old check 404 every event.
        if urlparse(self.path).path.rstrip("/") != self.route.rstrip("/"):
            self._json(404, {"ok": False})
            return
        if not self._authorized():
            self._json(401, {"ok": False, "error": "bad token"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            event = json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError) as exc:
            self._json(400, {"ok": False, "error": str(exc)})
            return
        # Answer fast: NapCat treats slow acknowledgements as failures.
        self._json(200, {"ok": True})
        try:
            self.bot.submit(event, event.get("self_id", ""))
        except Exception:
            log.exception("dispatch failed")


def serve(cfg: Config, bot: Bot) -> ThreadingHTTPServer:
    listen = cfg["listen"]
    handler = type(
        "BoundHandler",
        (Handler,),
        {
            "bot": bot,
            "token": str(listen.get("token") or ""),
            "route": str(listen.get("path", "/onebot")),
        },
    )
    httpd = ThreadingHTTPServer((listen["host"], int(listen["port"])), handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, name="http", daemon=True)
    thread.start()
    log.info(
        "listening on http://%s:%s%s  (reverse webhook)",
        listen["host"],
        listen["port"],
        listen.get("path", "/onebot"),
    )
    return httpd
