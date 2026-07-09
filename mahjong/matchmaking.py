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

from .state import _rank_queue, _casual_queue, _user_game
from . import i18n

TIMEOUT = 600     # 排隊逾時（秒）：等待逾 10 分鐘自動離開

_QUEUES = {"rank": _rank_queue, "casual": _casual_queue}


def need(is_sanma: bool) -> int:
    return 3 if is_sanma else 4


def _mode(is_sanma: bool) -> str:
    return "sanma" if is_sanma else "yonma"


def count(is_sanma: bool, kind: str = "rank") -> int:
    return len(_QUEUES[kind][_mode(is_sanma)])


def leave(uid: str) -> bool:
    """把某人從所有佇列（段位＋休閒）移除；有移除回 True。"""
    removed = False
    for q_kind in _QUEUES.values():
        for m in ("yonma", "sanma"):
            q = q_kind[m]
            keep = [e for e in q if e["uid"] != uid]
            if len(keep) != len(q):
                q[:] = keep
                removed = True
    return removed


def in_queue(uid: str):
    """回傳 (kind, mode) 或 None。"""
    for kind, q_kind in _QUEUES.items():
        for m in ("yonma", "sanma"):
            if any(e["uid"] == uid for e in q_kind[m]):
                return (kind, m)
    return None


def join(user, is_sanma: bool, kind: str = "rank"):
    """加入排隊（kind＝rank 段位／casual 休閒）。回傳其一：
    ("in_game",)                     ── 已在對局中，不能排隊
    ("already", count, need)         ── 已在此模式佇列
    ("queued",  count, need)         ── 已加入，等待中
    ("matched", players, mode, kind) ── 此次加入湊滿，players 為配對名單（含 discord.User）
    """
    uid = str(user.id)
    if uid in _user_game:
        return ("in_game",)
    m = _mode(is_sanma)
    n = need(is_sanma)
    q = _QUEUES[kind][m]
    if any(e["uid"] == uid for e in q):
        return ("already", len(q), n)
    leave(uid)                                  # 不能同時在多個佇列
    q.append({
        "uid": uid, "name": getattr(user, "display_name", str(uid)),
        "user": user, "lang": i18n.get_user_lang(uid), "since": time.time(),
    })
    if len(q) >= n:
        players = q[:n]
        del q[:n]
        return ("matched", players, m, kind)
    return ("queued", len(q), n)


def expire(now: float = None) -> list[dict]:
    """移除等待逾 TIMEOUT 的排隊者，回傳被移除的項目（供通知）。"""
    now = now or time.time()
    expired = []
    for q_kind in _QUEUES.values():
        for m in ("yonma", "sanma"):
            q = q_kind[m]
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
