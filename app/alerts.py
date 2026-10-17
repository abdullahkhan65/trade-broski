"""Telegram alerts + a few chat commands (/status /pause /resume /closeall /help).

Uses the plain Bot HTTP API with httpx - no extra library. Only messages from
TELEGRAM_CHAT_ID are obeyed. If Telegram isn't configured, alerts go to the log.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Awaitable, Callable

import httpx

from .config import ENV

log = logging.getLogger("alerts")
API = "https://api.telegram.org/bot{token}/{method}"


class Telegram:
    def __init__(self) -> None:
        self.enabled = ENV.has_telegram
        self.commands: dict[str, Callable[[], Awaitable[str]]] = {}
        self._offset = 0

    def _url(self, method: str) -> str:
        return API.format(token=ENV.telegram_bot_token, method=method)

    async def send(self, text: str) -> None:
        log.info("ALERT: %s", text.replace("\n", " | "))
        if not self.enabled:
            return
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                await c.post(self._url("sendMessage"), json={
                    "chat_id": ENV.telegram_chat_id, "text": text[:4000],
                    "parse_mode": "HTML", "disable_web_page_preview": True})
        except Exception as e:  # noqa: BLE001 - alerts must never crash the bot
            log.warning("Telegram send failed: %s", e)

    async def send_file(self, path: Path, caption: str = "") -> bool:
        if not self.enabled:
            return False
        try:
            async with httpx.AsyncClient(timeout=120) as c:
                with path.open("rb") as fh:
                    r = await c.post(self._url("sendDocument"),
                                     data={"chat_id": ENV.telegram_chat_id, "caption": caption},
                                     files={"document": (path.name, fh)})
                return r.status_code == 200
        except Exception as e:  # noqa: BLE001
            log.warning("Telegram file send failed: %s", e)
            return False

    def command(self, name: str):
        def deco(fn: Callable[[], Awaitable[str]]):
            self.commands[name] = fn
            return fn
        return deco

    async def poll_commands(self) -> None:
        if not self.enabled:
            return
        while True:
            try:
                async with httpx.AsyncClient(timeout=40) as c:
                    r = await c.get(self._url("getUpdates"),
                                    params={"offset": self._offset, "timeout": 30})
                    for upd in r.json().get("result", []):
                        self._offset = upd["update_id"] + 1
                        msg = upd.get("message") or {}
                        if str(msg.get("chat", {}).get("id")) != str(ENV.telegram_chat_id):
                            continue
                        cmd = (msg.get("text") or "").strip().split()[0].lstrip("/").split("@")[0].lower()
                        handler = self.commands.get(cmd) or self.commands.get("help")
                        if handler:
                            await self.send(await handler())
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.debug("Telegram poll error: %s", e)
                await asyncio.sleep(10)


telegram = Telegram()
