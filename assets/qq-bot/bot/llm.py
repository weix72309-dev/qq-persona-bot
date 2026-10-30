"""OpenAI-compatible chat client (DeepSeek by default). Standard library only."""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request

log = logging.getLogger("llm")


class LLMError(RuntimeError):
    pass


class LLM:
    def __init__(self, cfg: dict):
        self.base_url = cfg["base_url"].rstrip("/")
        self.api_key = cfg["api_key"]
        self.model = cfg["model"]
        self.temperature = float(cfg.get("temperature", 0.75))
        self.max_tokens = int(cfg.get("max_tokens", 700))
        self.timeout = int(cfg.get("timeout", 90))

    def _post(self, payload: dict) -> dict:
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def chat(
        self,
        messages: list[dict],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_mode: bool = False,
    ) -> tuple[str, dict]:
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature if temperature is None else temperature,
            "max_tokens": self.max_tokens if max_tokens is None else max_tokens,
            "stream": False,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        last: Exception | None = None
        for attempt in range(3):
            try:
                data = self._post(payload)
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", "replace")[:400]
                last = LLMError(f"http {exc.code}: {body}")
                if 400 <= exc.code < 500 and exc.code not in (408, 409, 429):
                    raise last
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last = LLMError(f"network: {exc}")
            except json.JSONDecodeError as exc:
                last = LLMError(f"bad json: {exc}")
            else:
                choices = data.get("choices") or []
                if not choices:
                    raise LLMError(f"empty choices: {str(data)[:300]}")
                text = (choices[0].get("message") or {}).get("content") or ""
                return text.strip(), data.get("usage") or {}
            time.sleep(1.5 * (attempt + 1))
        raise last or LLMError("unknown failure")

    def ping(self) -> str:
        text, _ = self.chat(
            [{"role": "user", "content": "reply with the single word: ok"}],
            temperature=0,
            max_tokens=8,
        )
        return text


class FakeLLM:
    """Deterministic stub for selftest / offline prompt inspection."""

    model = "fake"

    def __init__(self, reply: str = "嗯，我在。"):
        self.reply = reply
        self.calls: list[list[dict]] = []

    def chat(self, messages, *, temperature=None, max_tokens=None, json_mode=False):
        self.calls.append(messages)
        if json_mode:
            return '{"memories":[]}', {}
        return self.reply, {"prompt_tokens": 1, "completion_tokens": 1}

    def ping(self) -> str:
        return "ok"
