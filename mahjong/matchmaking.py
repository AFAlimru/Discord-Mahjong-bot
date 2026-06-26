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
"""matchmaking.py — 段位賽全域匹配佇列（跨伺服器，對局走 DM）。

純佇列邏輯，不依賴 discord 物件以外的狀態；湊滿即回傳配對名單交給
flow.launch_ranked_game 開局。佇列為記憶體狀態，重啟即清空。
"""
from __future__ import annotations
import asyncio
import time

from .state import _rank_queue, _user_game
from . import i18n

TIMEOUT = 600     # 排隊逾時（秒）：等待逾 10 分鐘自動離開


def need(is_sanma: bool) -> int:
    return 3 if is_sanma else 4


def _mode(is_sanma: bool) -> str:
    return "sanma" if is_sanma else "yonma"


def count(is_sanma: bool) -> int:
    return len(_rank_queue[_mode(is_sanma)])


def leave(uid: str) -> bool:
    """把某人從所有佇列移除；有移除回 True。"""
    removed = False
    for m in ("yonma", "sanma"):
        q = _rank_queue[m]
        keep = [e for e in q if e["uid"] != uid]
        if len(keep) != len(q):
            q[:] = keep
            removed = True
    return removed


def in_queue(uid: str) -> str | None:
    for m in ("yonma", "sanma"):
        if any(e["uid"] == uid for e in _rank_queue[m]):
            return m
    return None


def join(user, is_sanma: bool):
    """加入段位賽排隊。回傳其一：
    ("in_game",)             ── 已在對局中，不能排隊
    ("already", count, need) ── 已在此模式佇列
    ("queued",  count, need) ── 已加入，等待中
    ("matched", players, mode) ── 此次加入湊滿，players 為配對名單（含 discord.User）
    """
    uid = str(user.id)
    if uid in _user_game:
        return ("in_game",)
    m = _mode(is_sanma)
    n = need(is_sanma)
    if any(e["uid"] == uid for e in _rank_queue[m]):
        return ("already", len(_rank_queue[m]), n)
    leave(uid)                                  # 不能同時在兩個佇列
    _rank_queue[m].append({
        "uid": uid, "name": getattr(user, "display_name", str(uid)),
        "user": user, "lang": i18n.get_user_lang(uid), "since": time.time(),
    })
    q = _rank_queue[m]
    if len(q) >= n:
        players = q[:n]
        del q[:n]
        return ("matched", players, m)
    return ("queued", len(q), n)


def expire(now: float = None) -> list[dict]:
    """移除等待逾 TIMEOUT 的排隊者，回傳被移除的項目（供通知）。"""
    now = now or time.time()
    expired = []
    for m in ("yonma", "sanma"):
        q = _rank_queue[m]
        keep = []
        for e in q:
            (expired if now - e["since"] >= TIMEOUT else keep).append(e)
        q[:] = keep
    return expired


_sweeper_started = False


def start_sweeper(interval: float = 30.0) -> None:
    """啟動背景清掃器（重複呼叫只會啟動一次）。由 on_ready 呼叫。"""
    global _sweeper_started
    if _sweeper_started:
        return
    _sweeper_started = True

    async def _loop():
        while True:
            await asyncio.sleep(interval)
            for e in expire():
                try:
                    await e["user"].send(i18n.t("rank.timeout", e.get("lang", i18n.DEFAULT)))
                except Exception:
                    pass

    asyncio.create_task(_loop())
