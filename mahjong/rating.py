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

- 段位（門面）：新人 → 新月 → … → 日全食 → 血月；除了最高的血月，每階分 1～3 級（同雀魂「雀聖 1／2／3」）。
  依順位加減段位點 (pt)，達到該級所需 pt 就升一級，升級後從新級所需 pt 的**一半**開始（同天鳳、雀魂）。
  每一級的所需 pt 都比前一級多：同一階 1→2→3 級各多 25%，下一階的 1 級是上一階 1 級的兩倍。
  第一階（新人）例外：所需 pt 另訂、從 0 開始、不會降級。
  上弦月起 pt 低於 0 會降一級，同樣從該級所需 pt 的一半開始。
- R 值（實力）：類天鳳 Rate，依「順位 + 與對手平均 R 的差」加權，對局數越多變動越小。

資料庫存的 dan_idx＝攤平後的「級」索引（LEVELS），所有常數集中在最上方，方便日後調整。
"""
from __future__ import annotations
from .numfmt import compact

# ─── 段位階梯（月蝕主題）──────────────────────────────────────────────────
# (階名, [1 級, 2 級, 3 級 各自升級所需 pt])；None＝頂點（不分級、無升級門檻）。
# 規則：每一級都比前一級多——同一階裡 ×1／×1.25／×1.5，下一階 1 級＝上一階 1 級 ×2（新人例外）。
TIERS: list[tuple[str, list[int] | None]] = [
    ("新人",   [20, 30, 40]),          # 第一階：另訂、從 0 開始、不降級
    ("新月",   [100, 125, 150]),
    ("殘月",   [200, 250, 300]),
    ("上弦月", [400, 500, 600]),
    ("下弦月", [800, 1000, 1200]),
    ("月偏食", [1600, 2000, 2400]),
    ("日全食", [3200, 4000, 4800]),
    ("血月",   None),
]
DEMOTABLE_TIER = 3         # 上弦月（含）起 pt 低於 0 會降級；新人～殘月不降
TOP_START_PT   = 10_000    # 升上血月時的起始 pt（血月不分級、pt 往上累積，掉到 0 以下回日全食 3）

# 攤平成「級」：[(階索引, 級 1～3；頂點為 0), …]，dan_idx 就是這個列表的索引
LEVELS: list[tuple[int, int]] = [
    (t, sub) for t, (_, needs) in enumerate(TIERS)
    for sub in (range(1, len(needs) + 1) if needs else (0,))
]
TOP_IDX  = len(LEVELS) - 1
START_RATE = 1500.0        # R 初始值


def _clamp(idx: int) -> int:
    return max(0, min(int(idx or 0), TOP_IDX))


def tier_of(idx: int) -> int:
    """級 → 所屬階索引（配對用的段位跨度以階計）。"""
    return LEVELS[_clamp(idx)][0]


def level_index(tier: int, sub: int = 1) -> int:
    """(階, 級) → dan_idx；頂點忽略級。"""
    for i, (t, s) in enumerate(LEVELS):
        if t == tier and (s == sub or s == 0):
            return i
    return TOP_IDX


def need_pt(idx: int) -> int | None:
    """這一級升級所需 pt（頂點＝None）。"""
    t, sub = LEVELS[_clamp(idx)]
    needs = TIERS[t][1]
    return needs[sub - 1] if needs else None


def start_pt(idx: int) -> int:
    """升／降到這一級時的起始 pt：新人＝0，血月＝TOP_START_PT，其他＝所需 pt 的一半。"""
    idx = _clamp(idx)
    if tier_of(idx) == 0:
        return 0
    if idx == TOP_IDX:
        return TOP_START_PT
    return (need_pt(idx) or 0) // 2


def dan_name(idx: int) -> str:
    """顯示名，如「上弦月 2」；頂點只有階名。"""
    t, sub = LEVELS[_clamp(idx)]
    return f"{TIERS[t][0]} {sub}" if sub else TIERS[t][0]


def pt_text(idx: int, pt: int) -> str:
    """pt 進度，如「250/400」「2.4k/4.8k」；頂點只顯示累積 pt（如「12.3k」）。"""
    need = need_pt(idx)
    return f"{compact(pt)}/{compact(need)}" if need is not None else compact(pt)


def ladder_text() -> str:
    """段位說明用：每階一行「新人　20／30／40」，頂點只有階名。"""
    return "\n".join(f"{n}　" + "／".join(compact(x) for x in needs) if needs
                     else f"{n}　（{compact(TOP_START_PT)} 起）" for n, needs in TIERS)


def place_table(is_sanma: bool) -> str:
    """段位說明用：各階依順位的 pt 增減（相同的相鄰階合併成「新人～殘月」）。"""
    places = (1, 2, 3) if is_sanma else (1, 2, 3, 4)
    fmt = lambda v: f"+{v}" if v > 0 else (str(v) if v < 0 else "0")
    rows: list[list] = []                       # [[起始階名, 結束階名, 數值列], …]
    for t, (name, _) in enumerate(TIERS):
        vals = tuple(dan_place_pt(r, level_index(t, 1), is_sanma) for r in places)
        if rows and rows[-1][2] == vals:
            rows[-1][1] = name
        else:
            rows.append([name, name, vals])
    return "\n".join((a if a == b else f"{a}～{b}") + "　" + "／".join(fmt(v) for v in vals)
                     for a, b, vals in rows)


BLACK_SKIN_IDX = level_index([n for n, _ in TIERS].index("日全食"), 1)   # 達「日全食 1」解鎖黑色牌風


# ─── 段位點數（依順位）─────────────────────────────────────────────────────────
# 各階依順位的 pt（與 TIERS 同順序）。所需 pt 每階 ×2，加分也跟著放大（上弦月起約每階 ×1.4）——
# 越高階越難爬，但不會慢到爬不動（估算中上水準打完日全食約 90 場）；墊底扣分同步加重，
# 讓高階的「整桌加總」偏小（平均水準的人會停在自己的階附近，強的人才往上）。
PLACE_PT_4: list[tuple[int, int, int, int]] = [     # 四麻：一位／二位／三位／四位
    (40,  20,  10,    0),    # 新人（不扣分）
    (60,  30,   0,  -20),    # 新月
    (80,  40,   0,  -50),    # 殘月
    (120, 60,   0, -110),    # 上弦月
    (180, 90, -15, -190),    # 下弦月
    (260, 130, -30, -290),   # 月偏食
    (380, 190, -50, -420),   # 日全食
    (480, 240, -70, -540),   # 血月
]
PLACE_PT_3: list[tuple[int, int, int]] = [          # 三麻：一位／二位／三位
    (40,  10,    0),         # 新人（不扣分）
    (60,   0,  -20),         # 新月
    (80,   0,  -40),         # 殘月
    (120,  0,  -90),         # 上弦月
    (180,  0, -150),         # 下弦月
    (260,  0, -235),         # 月偏食
    (380,  0, -350),         # 日全食
    (480,  0, -465),         # 血月
]


def dan_place_pt(rank: int, dan_idx: int, is_sanma: bool) -> int:
    """一場段位賽依終局順位得到的段位點數（依所在的階查 PLACE_PT_4／PLACE_PT_3）。"""
    row = (PLACE_PT_3 if is_sanma else PLACE_PT_4)[tier_of(dan_idx)]
    return row[max(1, min(rank, len(row))) - 1]


def apply_dan(dan_idx: int, dan_pt: int, rank: int, is_sanma: bool) -> tuple[int, int]:
    """套用一場段位賽，回傳新的 (dan_idx, dan_pt)。一場最多升／降一級，溢出的 pt 不帶走。"""
    dan_idx = _clamp(dan_idx)
    dan_pt += dan_place_pt(rank, dan_idx, is_sanma)
    need = need_pt(dan_idx)
    if need is not None and dan_pt >= need:            # 升一級，從新級所需 pt 的一半開始
        dan_idx += 1
        dan_pt = start_pt(dan_idx)
    elif dan_pt < 0:
        if tier_of(dan_idx) >= DEMOTABLE_TIER:         # 降一級，同樣從一半開始
            dan_idx -= 1
            dan_pt = start_pt(dan_idx)
        else:
            dan_pt = 0
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
