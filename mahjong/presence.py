# Suzume Tsuk — Discord 日本麻將機器人
# Copyright (C) 2026  AFAlimru
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option)
# any later version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE.  See the GNU General Public License for more
# details.  You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""機器人的 Discord 狀態（「正在玩 …」）：目前在對局中的人數＋網站網址，定時更新。"""
from __future__ import annotations

import asyncio

import discord

from .config import WEB_BASE_URL
from .state import _games
from . import i18n

_started = False
_last: str | None = None


def playing_count() -> int:
    """進行中對局裡的真人人數（同一人只算一次；電腦不算）。"""
    return len({p.user_id for gs in list(_games.values()) for p in gs.players if not p.is_bot})


def status_text(n: int | None = None) -> str:
    """狀態文字，例如「12 人對局中｜yubingserver.top」；沒設定網站就只有人數。"""
    n = playing_count() if n is None else n
    text = i18n.t("presence.playing", i18n.DEFAULT, n=n)
    site = WEB_BASE_URL.split("://", 1)[-1].rstrip("/")
    if site.startswith(("127.", "localhost")):      # 本機測試的網址對外沒意義，不顯示
        site = ""
    return f"{text}｜{site}" if site else text


async def update(bot) -> None:
    """文字有變才更新（避免無謂地呼叫 Discord）。"""
    global _last
    text = status_text()
    if text == _last:
        return
    await bot.change_presence(activity=discord.Game(name=text))
    _last = text


def start(bot, interval: float = 60.0) -> None:
    """啟動狀態更新器（重複呼叫只會啟動一次）。由 on_ready 呼叫。"""
    global _started
    if _started:
        return
    _started = True

    async def _loop():
        while True:
            try:
                await update(bot)
            except Exception as e:
                print(f"[presence] 更新狀態失敗：{e!r}")
            await asyncio.sleep(interval)

    asyncio.create_task(_loop())
