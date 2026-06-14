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
"""房間編號與註冊：分配「房間#0001」流水號、追蹤進行中的房間（供後台讀取）。"""
from __future__ import annotations
import time
from dataclasses import dataclass, field

from . import db
from .state import rooms


@dataclass
class RoomMeta:
    room_no: int
    gid: str
    guild_id: str
    channel_id: str
    status: str = "waiting"          # waiting / playing / finished
    created_at: float = field(default_factory=time.time)


_next = {"value": None}


def _ensure_counter() -> None:
    if _next["value"] is None:
        try:
            _next["value"] = db.max_room_no() + 1
        except Exception:
            _next["value"] = 1


def next_room_no() -> int:
    """配發下一個房間編號（啟動後從資料庫最大值續號）。"""
    _ensure_counter()
    n = _next["value"]
    _next["value"] = n + 1
    return n


def register(gid: str, guild_id, channel_id) -> RoomMeta:
    rm = RoomMeta(next_room_no(), gid, str(guild_id), str(channel_id))
    rooms[gid] = rm
    return rm


def register_existing(gid: str, guild_id, channel_id, room_no,
                      status: str = "interrupted") -> RoomMeta:
    """重啟後以「已知房號」重新註冊（回復中斷對局用），不另配新號。"""
    rm = RoomMeta(int(room_no or 0), gid, str(guild_id), str(channel_id), status=status)
    rooms[gid] = rm
    _ensure_counter()                       # 確保續號計數器不會與既有房號相撞
    if room_no and _next["value"] is not None and room_no >= _next["value"]:
        _next["value"] = int(room_no) + 1
    return rm


def label(gid: str) -> str:
    """房間顯示名，如「房間#0001」。未註冊則回傳「房間」。"""
    rm = rooms.get(gid)
    return f"房間#{rm.room_no:04d}" if rm else "房間"


def room_no(gid: str):
    rm = rooms.get(gid)
    return rm.room_no if rm else None


def set_status(gid: str, status: str) -> None:
    rm = rooms.get(gid)
    if rm:
        rm.status = status


def unregister(gid: str) -> None:
    rooms.pop(gid, None)
