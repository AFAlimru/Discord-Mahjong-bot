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
"""電腦對手（純啟發式，不呼叫外部 API）。

- 打牌：向聽數最小 → 有效牌（還看得到的張數）最多 → 打掉價值最低的（保留寶牌、赤五、役牌對子）。
- 副露：只有「鳴了會變快」而且「鳴完還有役可做」（役牌、斷么、清／混一色、對對）才鳴；
  門清已接近聽牌就不亂鳴，留著立直。對手立直時不為了加速而鳴。
- 防守：有人立直時評估每張牌的危險度（現物、立直後他家通過的牌、筋、字牌已見張數、么九／中張），
  離聽牌遠就棄和打最安全的牌；已聽牌就推，但在維持聽牌的打法裡挑比較安全的。
- 立直：門清聽牌、待牌還有剩、點數夠、牌山還夠就立直；立直後摸切。
"""
from __future__ import annotations
from functools import lru_cache
from typing import Optional

from .engine import Tile, Suit, MeldType

# 34 種牌：萬 0-8、條 9-17、餅 18-26、風 27-30（東南西北）、三元 31-33（中發白）
_TERMINALS = (0, 8, 9, 17, 18, 26, 27, 28, 29, 30, 31, 32, 33)


def idx(t: Tile) -> int:
    s = int(t.suit)
    if s <= 2:
        return s * 9 + t.value - 1
    if s == 3:
        return 27 + t.value - 1
    return 31 + t.value - 1


def _is_num(k: int) -> bool:
    return k < 27


def _num(k: int) -> int:          # 數牌的數字（1-9）
    return k % 9 + 1


# ─────────────────────────── 向聽數 ───────────────────────────
def _pareto(items) -> frozenset:
    """同一個是否有雀頭(p)之下，(面子 m, 搭子 t) 互不支配的組合。"""
    out = []
    for x in items:
        if not any(y != x and y[2] == x[2] and y[0] >= x[0] and y[1] >= x[1] for y in items):
            out.append(x)
    return frozenset(out)


@lru_cache(maxsize=1 << 18)
def _opts(st: tuple, is_num: bool) -> frozenset:
    """一個花色（或字牌）的張數（已去掉開頭的 0）能拆出的 (面子, 搭子, 雀頭) 組合。"""
    if not st:
        return frozenset({(0, 0, 0)})
    c, n = list(st), len(st)
    found = set()

    def sub(nc, dm, dt, dp):
        j = 0
        while j < len(nc) and nc[j] == 0:
            j += 1
        for m, t, p in _opts(tuple(nc[j:]), is_num):
            if p + dp <= 1:
                found.add((m + dm, t + dt, p + dp))

    if c[0] >= 3:                                         # 刻子
        nc = c[:]; nc[0] -= 3; sub(nc, 1, 0, 0)
    if is_num and n >= 3 and c[1] and c[2]:               # 順子
        nc = c[:]; nc[0] -= 1; nc[1] -= 1; nc[2] -= 1; sub(nc, 1, 0, 0)
    if c[0] >= 2:                                         # 雀頭／對子搭子
        nc = c[:]; nc[0] -= 2; sub(nc, 0, 0, 1); sub(nc, 0, 1, 0)
    if is_num and n >= 2 and c[1]:                        # 兩面／邊張
        nc = c[:]; nc[0] -= 1; nc[1] -= 1; sub(nc, 0, 1, 0)
    if is_num and n >= 3 and c[2]:                        # 嵌張
        nc = c[:]; nc[0] -= 1; nc[2] -= 1; sub(nc, 0, 1, 0)
    nc = c[:]; nc[0] -= 1; sub(nc, 0, 0, 0)               # 孤張
    return _pareto(found)


def _strip(st: tuple) -> tuple:
    j = 0
    while j < len(st) and st[j] == 0:
        j += 1
    return st[j:]


def shanten_counts(cnt: list, n_melds: int) -> int:
    """向聽數（-1＝和了、0＝聽牌）。cnt＝34 種牌的張數，n_melds＝副露數（含暗槓）。"""
    groups = ((tuple(cnt[0:9]), True), (tuple(cnt[9:18]), True),
              (tuple(cnt[18:27]), True), (tuple(cnt[27:34]), False))
    combos = frozenset({(0, 0, 0)})
    for st, isn in groups:
        new = set()
        for m, t, p in combos:
            for m2, t2, p2 in _opts(_strip(st), isn):
                if p + p2 <= 1:
                    new.add((m + m2, t + t2, p + p2))
        combos = _pareto(new)
    best = 8
    for m, t, p in combos:
        mm = min(m + n_melds, 4)
        best = min(best, 8 - 2 * mm - min(t, 4 - mm) - p)
    if n_melds == 0:                                      # 七對子、國士無雙（門清限定）
        pairs = sum(1 for x in cnt if x >= 2)
        kinds = sum(1 for x in cnt if x > 0)
        best = min(best, 6 - pairs + max(0, 7 - kinds))
        tk = sum(1 for k in _TERMINALS if cnt[k])
        tp = any(cnt[k] >= 2 for k in _TERMINALS)
        best = min(best, 13 - tk - (1 if tp else 0))
    return best


def _counts(tiles) -> list:
    cnt = [0] * 34
    for t in tiles:
        cnt[idx(t)] += 1
    return cnt


def shanten(tiles, n_melds: int) -> int:
    return shanten_counts(_counts(tiles), n_melds)


# ─────────────────────────── 場況 ───────────────────────────
def _visible(gs, me) -> list:
    """我看得到的每種牌張數：自己手牌、所有牌河、所有副露、已翻寶牌指示牌。"""
    v = _counts(me.full_hand)
    for p in gs.players:
        for t in p.discards:
            v[idx(t)] += 1
        for m in p.melds:
            for t in m.tiles:
                v[idx(t)] += 1
    for ind in gs.dora_indicators[:gs.revealed_dora]:
        v[idx(ind)] += 1
    return [min(x, 4) for x in v]


def _seat_wind(gs, p) -> int:
    return ((p.seat - gs.dealer_seat) % len(gs.players)) + 1


def _yakuhai_kinds(gs, p) -> set:
    return {31, 32, 33, 27 + gs.round_wind - 1, 27 + _seat_wind(gs, p) - 1}


def _dora_kinds(gs) -> list:
    return [idx(t) for t in gs.get_doras()]


def _ukeire(cnt: list, n_melds: int, base: int, vis: list) -> int:
    """這手 13 張牌：能讓向聽數前進的牌，還剩幾張（扣掉看得到的）。"""
    total = 0
    for k in range(34):
        left = 4 - vis[k]
        if left <= 0:
            continue
        cnt[k] += 1
        if shanten_counts(cnt, n_melds) < base:
            total += left
        cnt[k] -= 1
    return total


def _tile_value(gs, me, t: Tile, cnt: list, doras: list, yaku: set) -> int:
    """打掉這張的「損失」——越高越不想打。"""
    k, v = idx(t), 0
    if getattr(t, "red", False):
        v += 3
    v += 3 * doras.count(k)
    if not _is_num(k):
        if k in yaku:
            v += 2 + (3 if cnt[k] >= 2 else 0)
        elif cnt[k] >= 2:
            v += 1
        else:
            v -= 1                        # 孤立客風最先打
    else:
        n = _num(k)
        if n in (1, 9):
            v -= 1
        elif n in (4, 5, 6):
            v += 1
    return v


# ─────────────────────────── 防守 ───────────────────────────
def _all_discards(p) -> list:
    return getattr(p, "discards_all", None) or p.discards


def _threats(gs, me) -> list:
    return [p for p in gs.players if p is not me and p.riichi]


def _safe_kinds(gs, r) -> set:
    """對立直者 r 的現物：他自己的捨牌，加上他立直後其他人打出、他沒榮和的牌。"""
    safe = {idx(t) for t in _all_discards(r)}
    snap = getattr(r, "riichi_snap", None) or {}
    for p in gs.players:
        if p is r:
            continue
        ds = _all_discards(p)
        safe |= {idx(t) for t in ds[snap.get(p.seat, len(ds)):]}
    return safe


def _danger_vs(k: int, safe: set, vis: list) -> int:
    if k in safe:
        return 0
    if not _is_num(k):
        return {4: 0, 3: 1, 2: 2}.get(vis[k], 4)          # 字牌：已見越多越安全
    n, base = _num(k), k - (_num(k) - 1)
    suji = lambda x: (base + x - 1) in safe
    if n in (1, 9):
        return 2 if suji(4 if n == 1 else 6) else 5
    if n in (2, 8):
        return 3 if suji(5) else 7
    if n in (3, 7):
        return 3 if suji(6 if n == 3 else 4) else 8
    lo, hi = suji(n - 3), suji(n + 3)                     # 4、5、6：兩邊筋才算筋
    return 4 if (lo and hi) else (7 if (lo or hi) else 10)


def _danger(k: int, safes: list, vis: list) -> int:
    return max((_danger_vs(k, s, vis) for s in safes), default=0)


# ─────────────────────────── 打牌（含立直判斷）───────────────────────────
def choose_discard(gs, me, banned: set = frozenset()) -> tuple[Optional[Tile], bool]:
    """回傳 (要打的牌, 是否立直)。me.hand 需已含摸到的牌（14 張－副露）。"""
    hand = list(me.hand)
    if not hand:
        return None, False
    n_melds = len(me.melds)
    vis     = _visible(gs, me)
    doras   = _dora_kinds(gs)
    yaku    = _yakuhai_kinds(gs, me)
    cnt     = _counts(hand)
    threats = _threats(gs, me)
    safes   = [_safe_kinds(gs, r) for r in threats]

    kinds = sorted({idx(t) for t in hand})
    allowed = [k for k in kinds if k not in banned] or kinds
    evals = []                                             # (k, 向聽, 有效牌, 危險度, 價值)
    for k in allowed:
        cnt[k] -= 1
        s = shanten_counts(cnt, n_melds)
        cnt[k] += 1
        evals.append([k, s, 0, _danger(k, safes, vis) if threats else 0, 0])
    best_s = min(e[1] for e in evals)
    for e in evals:
        if e[1] <= best_s + 1:                             # 只替有競爭力的打法算有效牌
            cnt[e[0]] -= 1
            e[2] = _ukeire(cnt, n_melds, e[1], vis)
            cnt[e[0]] += 1
        t = next(t for t in hand if idx(t) == e[0])
        e[4] = _tile_value(gs, me, t, cnt, doras, yaku)

    fold = bool(threats) and not me.riichi and _should_fold(gs, me, best_s, len(threats))
    if fold:
        pick = min(evals, key=lambda e: (e[3], e[1], -e[2], e[4]))
    elif threats:                                          # 推：先顧速度，同速度挑安全的
        pick = min(evals, key=lambda e: (e[1], e[3] if e[1] == 0 else 0,
                                         -e[2] + 2 * e[3], e[4]))
    else:
        pick = min(evals, key=lambda e: (e[1], -e[2], e[4]))

    k = pick[0]
    copies = [t for t in hand if idx(t) == k]
    tile = next((t for t in copies if not getattr(t, "red", False)), copies[0])
    riichi = (not fold) and _should_riichi(gs, me, pick[1], pick[2])
    return tile, riichi


def _should_fold(gs, me, best_shanten: int, n_threats: int) -> bool:
    if best_shanten <= 0:
        return False                                      # 聽牌就推
    if best_shanten >= 2:
        return True
    value = sum(_dora_kinds(gs).count(idx(t)) for t in me.full_hand) \
        + sum(1 for t in me.full_hand if getattr(t, "red", False))
    return n_threats >= 2 or value < 2                    # 一向聽：手很值錢才推


def _is_menzen(me) -> bool:
    return all(m.meld_type == MeldType.ANKAN for m in me.melds)


def _should_riichi(gs, me, shanten_after: int, ukeire: int) -> bool:
    return (shanten_after == 0 and not me.riichi and _is_menzen(me)
            and ukeire > 0 and me.score >= 1000 and gs.tiles_left >= 4)


# ─────────────────────────── 副露 ───────────────────────────
def _has_yaku_path(gs, me, hand: list, melds: list) -> bool:
    """鳴牌後還有沒有役可以做（役牌、斷么、清／混一色、對對）。"""
    yaku = _yakuhai_kinds(gs, me)
    meld_kinds = [[idx(t) for t in m.tiles] for m in melds]
    cnt = _counts(hand)
    # 役牌：已經碰了役牌，或手上還有役牌對子
    if any(len(set(ks)) == 1 and ks[0] in yaku for ks in meld_kinds):
        return True
    if any(cnt[k] >= 2 for k in yaku):
        return True
    all_k = [k for ks in meld_kinds for k in ks] + [idx(t) for t in hand]
    # 斷么：副露全中張，手上最多一張么九（之後打掉）
    simple = lambda k: _is_num(k) and 2 <= _num(k) <= 8
    if all(simple(k) for ks in meld_kinds for k in ks) and \
            sum(1 for t in hand if not simple(idx(t))) <= 1:
        return True
    # 清／混一色：副露同一花色（或字牌），手牌最多兩張別的花色
    suits = {k // 9 for ks in meld_kinds for k in ks if _is_num(k)}
    if len(suits) <= 1:
        s = next(iter(suits)) if suits else max(range(3), key=lambda x: sum(cnt[x * 9:(x + 1) * 9]))
        off = sum(1 for k in all_k if _is_num(k) and k // 9 != s)
        if off <= 2:
            return True
    # 對對和：副露都是刻子，手上對子／刻子夠多
    if all(len(set(ks)) == 1 for ks in meld_kinds):
        if len(melds) + sum(1 for x in cnt if x >= 2) >= 5:
            return True
    return False


def _best_after_call(hand: list, n_melds: int) -> int:
    """鳴牌後還要打一張：打完的最佳向聽數。"""
    cnt = _counts(hand)
    best = 8
    for k in {idx(t) for t in hand}:
        cnt[k] -= 1
        best = min(best, shanten_counts(cnt, n_melds))
        cnt[k] += 1
    return best


def call_choice(gs, me, tile: Tile, chi_options: list) -> Optional[tuple]:
    """要不要鳴這張：回傳 ("pon", None)／("chi", (t1, t2))／None。"""
    from .engine import Meld
    if me.riichi:
        return None
    hand = list(me.hand)
    before = shanten(hand, len(me.melds))
    k = idx(tile)
    yaku = _yakuhai_kinds(gs, me)
    if _threats(gs, me) and before >= 1:                  # 有人立直、自己還沒聽 → 不為加速而鳴
        return None
    # 門清已一向聽以內：除非碰役牌，否則不鳴（留著立直）
    keep_closed = _is_menzen(me) and before <= 1

    options = []
    if sum(1 for t in hand if idx(t) == k) >= 2 and not (keep_closed and k not in yaku):
        used, rest = 0, []
        for t in hand:
            if used < 2 and idx(t) == k:
                used += 1
            else:
                rest.append(t)
        melds = list(me.melds) + [Meld(MeldType.PON, [tile, tile, tile])]
        options.append(("pon", None, rest, melds))
    if not keep_closed:
        for t1, t2 in chi_options or []:
            rest = list(hand)
            rest.remove(t1); rest.remove(t2)
            melds = list(me.melds) + [Meld(MeldType.CHI, sorted([t1, t2, tile], key=idx))]
            options.append(("chi", (t1, t2), rest, melds))

    best = None
    for kind, extra, rest, melds in options:
        after = _best_after_call(rest, len(melds))
        # 碰役牌：不變慢就碰（直接確保有役）；其他鳴牌：一定要變快
        faster = after <= before if (kind == "pon" and k in yaku) else after < before
        if not faster or not _has_yaku_path(gs, me, rest, melds):
            continue
        if best is None or after < best[0]:
            best = (after, kind, extra)
    return (best[1], best[2]) if best else None
