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
"""房間編號與註冊：分配房號、追蹤進行中的房間（供後台讀取）。
房號內部是遞增整數（資料庫存這個），對外一律顯示成 5 碼代碼（如 K7Q2M，見 `code`／`parse`）。"""
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

# 房號代碼：整數以乘法打散成一對一的 5 碼（相鄰房號的代碼看不出連號），31^5 ≈ 2,860 萬間才會繞回。
_ALPHA = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"    # 去掉容易看錯的 0/O、1/I/L
_LEN   = 5
_MOD   = len(_ALPHA) ** _LEN
_MUL   = 7_368_787                           # 與 _MOD 互質（不是 31 的倍數）→ 可逆
_ADD   = 9_041_533
_INV   = pow(_MUL, -1, _MOD)


def code(n) -> str:
    """房號整數 → 顯示用代碼（如 K7Q2M）。"""
    x = (int(n) * _MUL + _ADD) % _MOD
    out = []
    for _ in range(_LEN):
        x, r = divmod(x, len(_ALPHA))
        out.append(_ALPHA[r])
    return "".join(reversed(out))


def parse(text) -> int | None:
    """玩家輸入的房號代碼 → 整數（不分大小寫、可帶 #）；不是有效代碼＝None。"""
    t = str(text or "").strip().upper().lstrip("#").replace(" ", "")
    if len(t) != _LEN or any(c not in _ALPHA for c in t):
        return None
    x = 0
    for c in t:
        x = x * len(_ALPHA) + _ALPHA.index(c)
    return ((x - _ADD) * _INV) % _MOD


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
    """房間顯示名，如「房間 K7Q2M」。未註冊則回傳「房間」。"""
    rm = rooms.get(gid)
    return f"房間 {code(rm.room_no)}" if rm and rm.room_no else "房間"


def tag(gid: str) -> str:
    """頻道／討論串名稱用的房號標記（代碼；未註冊用 gid 前 4 碼）。"""
    rm = rooms.get(gid)
    return code(rm.room_no) if rm and rm.room_no else gid[:4]


def room_no(gid: str):
    rm = rooms.get(gid)
    return rm.room_no if rm else None


def set_status(gid: str, status: str) -> None:
    rm = rooms.get(gid)
    if rm:
        rm.status = status


def unregister(gid: str) -> None:
    rooms.pop(gid, None)


def all_meta() -> dict:
    """目前註冊的所有房間（gid -> RoomMeta）淺拷貝，供後台列出／清理。"""
    return dict(rooms)
