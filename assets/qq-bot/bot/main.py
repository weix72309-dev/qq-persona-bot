"""Entry point: python -m bot.main [--config config.json] [--check]"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from pathlib import Path

from .config import Config, setup_logging
from .core import Bot
from .server import serve

log = logging.getLogger("main")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="QQ group persona bot (OneBot v11 + DeepSeek)")
    parser.add_argument("--config", default=str(Path.cwd() / "config.json"))
    parser.add_argument("--check", action="store_true", help="validate config, persona, LLM and OneBot, then exit")
    parser.add_argument("--once", metavar="TEXT", help="run one reply through the pipeline without QQ")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)

    cfg = Config(args.config)
    setup_logging(cfg.log_path, "DEBUG" if args.debug else "INFO")
    log.info("config loaded from %s", cfg.path)
    log.info("persona %s (%d chars)", cfg.persona_path.name, len(cfg.persona()))

    bot = Bot(cfg)
    if args.check:
        problems = 0
        try:
            bot_id = bot.api.self_id()
            log.info("onebot ok, bot QQ = %s", bot_id or "(unknown)")
        except RuntimeError as exc:
            problems += 1
            log.error("onebot check failed: %s", exc)
        try:
            log.info("llm ok, reply = %r", bot.llm.ping())
        except Exception as exc:
            problems += 1
            log.error("llm check failed: %s", exc)
        log.info("store ok: %s", bot.store.stats())
        bot.close()
        return 1 if problems else 0

    if args.once:
        from .onebot import Incoming

        cfg.data["behavior"]["dry_run"] = True
        msg = Incoming(
            group_id=str((cfg["behavior"].get("groups") or ["0"])[0]),
            user_id=str((cfg.get("dad_qq") or cfg.get("admin_qq") or ["10000"])[0]),
            nickname="测试用户",
            text=args.once,
            msg_id=str(int(time.time() * 1000)),
            self_id="",
        )
        # --once exists to eyeball the prompt/answer, so bypass the addressing gate.
        bot.ingest(msg, msg.self_id, force=True)
        time.sleep(1)
        bot.close()
        return 0

    try:
        bot_id = bot.api.self_id()
    except RuntimeError as exc:
        log.warning("could not read bot QQ at startup (will read from events): %s", exc)
        bot_id = ""
    httpd = serve(cfg, bot)
    stop = {"flag": False}

    def _stop(*_):
        stop["flag"] = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _stop)
        except (ValueError, OSError):
            pass
    log.info("bot ready; press Ctrl+C to stop")
    try:
        while not stop["flag"]:
            time.sleep(0.5)
    finally:
        log.info("shutting down")
        httpd.shutdown()
        bot.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
