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
"""rating.py — 段位階梯 + R 值（類天鳳）的純計算邏輯。

只有「段位賽」對局會套用。本檔不碰資料庫、不依賴 discord，方便單獨測試。

- 段位（門面）：新人 → 九級…一級 → 初段…十段 → 天鳳位。依順位加減段位點 (pt)，
  累積足夠即升段；初段（含）以上點數歸零會降段（級位不降）。
- R 值（實力）：類天鳳 Rate，依「順位 + 與對手平均 R 的差」加權，對局數越多變動越小。

所有常數集中在最上方，方便日後調整。
"""
from __future__ import annotations

# ─── 段位階梯（月／星主題）──────────────────────────────────────────────────
# (顯示名, 升段所需點數)；最後一階為頂點，無升段門檻。
DAN_LADDER: list[tuple[str, int | None]] = [
    ("新人",    30),
    ("新星",   100),
    ("星群",   200),
    ("星雀",   400),
    ("星月",   600),
    ("上弦月",  800),
    ("下弦月", 1000),
    ("月魂",   None),
]
DAN_NAMES = [n for n, _ in DAN_LADDER]
DAN_NEED  = [need for _, need in DAN_LADDER]
TOP_IDX   = len(DAN_LADDER) - 1
DEMOTABLE_FROM = 3         # idx>=3（星雀）起，點數歸零會降階；新人～星群不降

START_RATE = 1500.0        # R 初始值


def dan_name(idx: int) -> str:
    return DAN_NAMES[max(0, min(idx, TOP_IDX))]


# ─── 段位點數（依順位）─────────────────────────────────────────────────────────
def dan_place_pt(rank: int, dan_idx: int, is_sanma: bool) -> int:
    """一場段位賽依終局順位得到的段位點數。高段位墊底扣得更兇。"""
    penalty = 75 + max(0, dan_idx - (DEMOTABLE_FROM - 1)) * 15   # 初段起每段 +15
    if is_sanma:   # 三麻：1/2/3 位
        return {1: 90, 2: 0}.get(rank, -penalty)
    # 四麻：1/2/3/4 位
    if rank == 1:
        return 90
    if rank == 2:
        return 45
    if rank == 3:
        return 0 if dan_idx < 5 else -15       # 上弦月起三位也微扣
    return -penalty


def apply_dan(dan_idx: int, dan_pt: int, rank: int, is_sanma: bool) -> tuple[int, int]:
    """套用一場段位賽，回傳新的 (dan_idx, dan_pt)。"""
    dan_pt += dan_place_pt(rank, dan_idx, is_sanma)
    # 升段（可連升；點數溢出帶到下一階）
    while dan_idx < TOP_IDX and DAN_NEED[dan_idx] is not None and dan_pt >= DAN_NEED[dan_idx]:
        dan_pt -= DAN_NEED[dan_idx]
        dan_idx += 1
    # 降段（初段以上；級位點數最低 0）。一場最多降一階，落在配給（半條），不連環掉。
    if dan_pt < 0:
        if dan_idx >= DEMOTABLE_FROM:
            dan_idx -= 1
            dan_pt = (DAN_NEED[dan_idx] or 0) // 2       # 降段配給：半條
        else:
            dan_pt = 0
    if dan_idx >= TOP_IDX:        # 已達頂點，點數不再累積
        dan_idx, dan_pt = TOP_IDX, max(0, dan_pt)
    return dan_idx, dan_pt


# ─── R 值（依順位 + 對手平均）────────────────────────────────────────────────
def rate_place_pt(rank: int, is_sanma: bool) -> int:
    if is_sanma:
        return {1: 30, 2: 0, 3: -30}.get(rank, 0)
    return {1: 30, 2: 10, 3: -10, 4: -30}.get(rank, 0)


def apply_rate(my_rate: float, others_avg: float, rank: int,
               games_played: int, is_sanma: bool) -> float:
    """套用一場段位賽後的新 R。games_played 為「本場之前」已打的段位賽數。"""
    adjust = max(0.2, 1.0 - games_played * 0.002)
    delta  = adjust * (rate_place_pt(rank, is_sanma) + (others_avg - my_rate) / 40.0)
    return round(my_rate + delta, 1)


# ─── 一場段位賽：算出每位玩家的新數據 ──────────────────────────────────────────
def apply_game(rows: dict[str, dict], results: list[tuple[str, int]],
               is_sanma: bool) -> dict[str, dict]:
    """
    rows:    {user_id: {"dan_idx","dan_pt","rate","games"}}（缺者視為新手）
    results: [(user_id, rank), …]，rank 為終局順位（1 起算），含所有座位（bot 也要在）
    回傳：   {user_id: 新的 {"dan_idx","dan_pt","rate","games"}}（只含 rows 內的真人）
    """
    rates = {uid: rows.get(uid, {}).get("rate", START_RATE) for uid, _ in results}
    out: dict[str, dict] = {}
    for uid, rank in results:
        if uid not in rows:        # 只更新有紀錄的真人（bot 不存）
            continue
        cur = rows[uid]
        others = [r for u, r in rates.items() if u != uid]
        others_avg = sum(others) / len(others) if others else cur.get("rate", START_RATE)
        di, dp = apply_dan(cur.get("dan_idx", 0), cur.get("dan_pt", 0), rank, is_sanma)
        nr = apply_rate(cur.get("rate", START_RATE), others_avg, rank,
                        cur.get("games", 0), is_sanma)
        out[uid] = {"dan_idx": di, "dan_pt": dp, "rate": nr,
                    "games": cur.get("games", 0) + 1}
    return out
