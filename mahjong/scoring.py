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
"""
scoring.py — 日本麻將計分（役種判定 + 符數 + 點數）

取代舊的 yaku.py / japanese_mahjong.py。
依麻雀一番街 / 標準日麻規則實作：
  - 42 種役 + 七對子 + 國士無雙（含十三面、九蓮、四暗刻單騎等雙倍役滿）
  - 副露減一飜（喰い下がり）
  - 無役不可和（dora/赤dora 不算役）
  - 符數計算（基本 20、平和自摸 20、七對子 25、門前榮和 +10、副露不足補 30、進位至十位）
  - 點數計算（base = 符 × 2^(2+飜)，滿貫/跳滿/倍滿/三倍滿/役滿，支付進位至百位）

主要入口：score_hand(...) -> ScoreResult
"""

from __future__ import annotations
from dataclasses import dataclass, field
from collections import Counter
from typing import Optional

from .engine import Tile, Suit, Meld, MeldType


# ─── 牌性質判斷 ─────────────────────────────────────────────────────────────

NUMBER_SUITS = (Suit.MAN, Suit.SOU, Suit.PIN)


def is_honor(t: Tile) -> bool:
    return t.suit in (Suit.WIND, Suit.DRAGON)


def is_terminal(t: Tile) -> bool:
    """老頭牌：數字牌的 1 或 9。"""
    return t.suit in NUMBER_SUITS and t.value in (1, 9)


def is_terminal_or_honor(t: Tile) -> bool:
    return is_honor(t) or is_terminal(t)


def tk(t: Tile) -> tuple:
    """牌的等價鍵（忽略紅寶牌差異）。"""
    return (int(t.suit), t.value)


# ─── 面子（拆解後的群組）表示 ────────────────────────────────────────────────

@dataclass
class Group:
    kind: str                 # 'shuntsu' | 'kotsu' | 'kantsu' | 'pair'
    tiles: list[Tile]
    open: bool = False        # 是否來自副露（鳴牌）
    ankan: bool = False       # 是否暗槓

    @property
    def key(self) -> tuple:
        return tk(self.tiles[0])

    @property
    def is_triplet_like(self) -> bool:
        return self.kind in ("kotsu", "kantsu")

    def contains_terminal_or_honor(self) -> bool:
        return any(is_terminal_or_honor(t) for t in self.tiles)

    def all_terminal_or_honor(self) -> bool:
        return all(is_terminal_or_honor(t) for t in self.tiles)


# ─── 結果 ──────────────────────────────────────────────────────────────────

@dataclass
class ScoreResult:
    yaku: list[tuple[str, int]] = field(default_factory=list)  # [(名稱, 飜), ...]
    yakuman: list[tuple[str, int]] = field(default_factory=list)  # [(名稱, 倍數), ...]
    han: int = 0
    fu: int = 0
    base: int = 0
    points: int = 0           # 總得點（贏家收到的總和）
    name: str = ""            # 滿貫/跳滿/倍滿/三倍滿/役滿/雙倍役滿
    # 支付細節
    ron_payment: int = 0                      # 榮和：放銃者支付
    tsumo_dealer_payment: int = 0             # 自摸：莊家支付（閒家和牌時）
    tsumo_each_payment: int = 0               # 自摸：每位閒家支付
    valid: bool = False       # 是否有役（可和）

    def describe(self) -> str:
        parts = [f"{n} {h}飜" for n, h in self.yaku]
        parts += [f"{n}" for n, _ in self.yakuman]
        return " / ".join(parts)


# ─── 手牌拆解 ────────────────────────────────────────────────────────────────

def _decompose_standard(counts: Counter, need_mentsu: int) -> list[tuple[list, tuple]]:
    """
    把 concealed 牌（counts: key->數量）拆成 need_mentsu 個面子 + 1 雀頭。
    回傳所有拆法：[(mentsu_list, pair_key), ...]
    mentsu_list 元素：('shuntsu'|'kotsu', key)
    """
    results: list[tuple[list, tuple]] = []
    keys = sorted(counts.keys())

    def rec(cnt: Counter, mentsu: list) -> None:
        if len(mentsu) == need_mentsu:
            # 剩下必須剛好是一個雀頭
            remain = [k for k, v in cnt.items() if v > 0]
            if len(remain) == 1 and cnt[remain[0]] == 2:
                results.append((list(mentsu), remain[0]))
            return
        # 找最小的還有的牌
        k = None
        for key in keys:
            if cnt[key] > 0:
                k = key
                break
        if k is None:
            return
        suit, val = k
        # 刻子
        if cnt[k] >= 3:
            cnt[k] -= 3
            mentsu.append(("kotsu", k))
            rec(cnt, mentsu)
            mentsu.pop()
            cnt[k] += 3
        # 順子（數字牌且 val<=7）
        if suit in (int(Suit.MAN), int(Suit.SOU), int(Suit.PIN)) and val <= 7:
            k2, k3 = (suit, val + 1), (suit, val + 2)
            if cnt[k2] > 0 and cnt[k3] > 0:
                cnt[k] -= 1; cnt[k2] -= 1; cnt[k3] -= 1
                mentsu.append(("shuntsu", k))
                rec(cnt, mentsu)
                mentsu.pop()
                cnt[k] += 1; cnt[k2] += 1; cnt[k3] += 1

    # 先抽出雀頭再拆 → 改成在遞迴尾端判斷，但需要先嘗試所有可能雀頭：
    # 上面 rec 的尾端判斷已涵蓋「剩兩張同牌＝雀頭」。但若雀頭在中途才出現會漏。
    # 改用：枚舉雀頭，剩餘全拆面子。
    results.clear()

    def rec2(cnt: Counter, mentsu: list) -> bool:
        if len(mentsu) == need_mentsu:
            return all(v == 0 for v in cnt.values())
        k = None
        for key in keys:
            if cnt[key] > 0:
                k = key
                break
        if k is None:
            return False
        suit, val = k
        ok_any = False
        if cnt[k] >= 3:
            cnt[k] -= 3
            mentsu.append(("kotsu", k))
            if rec2(cnt, mentsu):
                ok_any = True
            mentsu.pop()
            cnt[k] += 3
        if suit in (int(Suit.MAN), int(Suit.SOU), int(Suit.PIN)) and val <= 7:
            k2, k3 = (suit, val + 1), (suit, val + 2)
            if cnt[k2] > 0 and cnt[k3] > 0:
                cnt[k] -= 1; cnt[k2] -= 1; cnt[k3] -= 1
                mentsu.append(("shuntsu", k))
                if rec2(cnt, mentsu):
                    ok_any = True
                mentsu.pop()
                cnt[k] += 1; cnt[k2] += 1; cnt[k3] += 1
        return ok_any

    for pair_key in list(counts.keys()):
        if counts[pair_key] >= 2:
            c2 = counts.copy()
            c2[pair_key] -= 2
            if c2[pair_key] == 0:
                del c2[pair_key]
            found: list[list] = []

            def rec3(cnt: Counter, mentsu: list) -> None:
                if len(mentsu) == need_mentsu:
                    if all(v == 0 for v in cnt.values()):
                        found.append(list(mentsu))
                    return
                kk = None
                for key in sorted(cnt.keys()):
                    if cnt[key] > 0:
                        kk = key
                        break
                if kk is None:
                    return
                suit, val = kk
                if cnt[kk] >= 3:
                    cnt[kk] -= 3
                    mentsu.append(("kotsu", kk))
                    rec3(cnt, mentsu)
                    mentsu.pop()
                    cnt[kk] += 3
                if suit in (int(Suit.MAN), int(Suit.SOU), int(Suit.PIN)) and val <= 7:
                    k2, k3 = (suit, val + 1), (suit, val + 2)
                    if cnt.get(k2, 0) > 0 and cnt.get(k3, 0) > 0:
                        cnt[kk] -= 1; cnt[k2] -= 1; cnt[k3] -= 1
                        mentsu.append(("shuntsu", kk))
                        rec3(cnt, mentsu)
                        mentsu.pop()
                        cnt[kk] += 1; cnt[k2] += 1; cnt[k3] += 1

            rec3(c2.copy(), [])
            for m in found:
                results.append((m, pair_key))

    # 去除重複
    uniq = []
    seen = set()
    for m, p in results:
        sig = (tuple(sorted(m)), p)
        if sig not in seen:
            seen.add(sig)
            uniq.append((m, p))
    return uniq


def _key_to_tile(key: tuple) -> Tile:
    return Tile(Suit(key[0]), key[1])


# ─── 特殊型判斷 ─────────────────────────────────────────────────────────────

KOKUSHI_KEYS = {
    (int(Suit.MAN), 1), (int(Suit.MAN), 9),
    (int(Suit.SOU), 1), (int(Suit.SOU), 9),
    (int(Suit.PIN), 1), (int(Suit.PIN), 9),
    (int(Suit.WIND), 1), (int(Suit.WIND), 2), (int(Suit.WIND), 3), (int(Suit.WIND), 4),
    (int(Suit.DRAGON), 1), (int(Suit.DRAGON), 2), (int(Suit.DRAGON), 3),
}


def is_chiitoitsu(all14: list[Tile]) -> bool:
    if len(all14) != 14:
        return False
    c = Counter(tk(t) for t in all14)
    return len(c) == 7 and all(v == 2 for v in c.values())


def is_kokushi(all14: list[Tile]) -> bool:
    if len(all14) != 14:
        return False
    c = Counter(tk(t) for t in all14)
    if set(c.keys()) != KOKUSHI_KEYS:
        return False
    return all(v >= 1 for v in c.values()) and any(v == 2 for v in c.values())


# ─── 主計分 ─────────────────────────────────────────────────────────────────

@dataclass
class WinContext:
    is_tsumo: bool = False
    is_riichi: bool = False
    is_double_riichi: bool = False
    is_ippatsu: bool = False
    is_rinshan: bool = False       # 嶺上開花
    is_chankan: bool = False       # 槍槓
    is_haitei: bool = False        # 海底摸月（自摸）
    is_houtei: bool = False        # 河底撈魚（榮和）
    is_tenhou: bool = False        # 天和
    is_chiihou: bool = False       # 地和
    round_wind: int = 1            # 1=東 2=南 3=西 4=北
    seat_wind: int = 1             # 1=東 2=南 3=西 4=北
    dora_tiles: list = field(default_factory=list)   # 表寶牌（實際寶牌，非指示牌）
    ura_tiles: list = field(default_factory=list)    # 裏寶牌
    is_dealer: bool = False
    allow_double_yakuman: bool = True   # True=混合(有雙倍役滿) / False=天鳳(全部單倍)
    kita: int = 0                        # 三麻拔北（北寶牌）張數，每張 +1 飜


def _round_up(n: int, step: int) -> int:
    return ((n + step - 1) // step) * step


def score_hand(
    concealed: list[Tile],
    melds: list[Meld],
    win_tile: Tile,
    ctx: WinContext,
) -> ScoreResult:
    """
    concealed：門前手牌（不含副露、不含 win_tile）
    melds：副露列表
    win_tile：和牌的那張
    回傳最高分的 ScoreResult；若無役則 valid=False。
    """
    all14 = list(concealed) + [win_tile] + [t for m in melds for t in m.tiles[:3]]
    # 註：槓視為 3 張參與牌型計算（第四張只影響符與槓子役）
    is_closed = all(m.meld_type == MeldType.ANKAN for m in melds) or len(melds) == 0
    # 門前 = 沒有非暗槓的副露
    menzen = all(m.meld_type == MeldType.ANKAN for m in melds)

    candidates: list[ScoreResult] = []

    # ── 役滿/特殊型：國士無雙 ──
    full_concealed = list(concealed) + [win_tile]
    if len(melds) == 0 and is_kokushi(full_concealed):
        r = ScoreResult()
        c = Counter(tk(t) for t in full_concealed)
        thirteen = (c[tk(win_tile)] == 1)  # 和牌張原本就有一張 → 單騎13面
        if thirteen:
            r.yakuman.append(("國士無雙十三面", 2))
        else:
            r.yakuman.append(("國士無雙", 1))
        _finalize_yakuman(r, ctx)
        candidates.append(r)

    # ── 七對子 ──
    if len(melds) == 0 and is_chiitoitsu(full_concealed):
        r = _score_chiitoitsu(full_concealed, win_tile, ctx, menzen)
        if r:
            candidates.append(r)

    # ── 標準型（4 面子 + 雀頭）──
    need = 4 - len(melds)
    if need >= 0:
        counts = Counter(tk(t) for t in full_concealed)
        decomps = _decompose_standard(counts, need)
        for mentsu_list, pair_key in decomps:
            r = _score_standard(
                concealed, melds, win_tile, ctx,
                mentsu_list, pair_key, menzen,
            )
            if r:
                candidates.append(r)

    if not candidates:
        return ScoreResult(valid=False)

    # 取最高分（先比役滿倍數，再比點數）
    def rank(r: ScoreResult):
        ym = sum(x[1] for x in r.yakuman)
        return (ym, r.points, r.han, r.fu)

    best = max(candidates, key=rank)
    return best


# ─── 七對子 ─────────────────────────────────────────────────────────────────

def _score_chiitoitsu(all14, win_tile, ctx: WinContext, menzen: bool) -> Optional[ScoreResult]:
    r = ScoreResult()
    han = 0
    yaku = []

    if ctx.is_double_riichi:
        yaku.append(("兩立直", 2)); han += 2
    elif ctx.is_riichi:
        yaku.append(("立直", 1)); han += 1
    if ctx.is_ippatsu:
        yaku.append(("一發", 1)); han += 1
    if ctx.is_tsumo and menzen:
        yaku.append(("門前清自摸和", 1)); han += 1
    yaku.append(("七對子", 2)); han += 2

    # 斷么九
    if all(not is_terminal_or_honor(t) for t in all14):
        yaku.append(("斷么九", 1)); han += 1
    # 混一色 / 清一色
    flush = _flush_han(all14, menzen=True)
    if flush:
        yaku.append(flush); han += flush[1]
    # 混老頭（七對子全么九 → 字一色或混老頭）
    if all(is_terminal_or_honor(t) for t in all14):
        if all(is_honor(t) for t in all14):
            r.yakuman.append(("字一色", 1))
            _finalize_yakuman(r, ctx)
            return r
        yaku.append(("混老頭", 2)); han += 2
    if ctx.is_haitei:
        yaku.append(("海底摸月", 1)); han += 1
    if ctx.is_houtei:
        yaku.append(("河底撈魚", 1)); han += 1

    if han == 0:
        return None

    # 寶牌
    han += _dora_han(all14, ctx, yaku)

    r.yaku = yaku
    r.han = han
    r.fu = 25
    _finalize_points(r, ctx)
    r.valid = True
    return r


# ─── 標準型計分 ─────────────────────────────────────────────────────────────

def _build_groups(concealed, melds, win_tile, mentsu_list, pair_key, ctx) -> tuple[list[Group], Group, Optional[Group]]:
    """組出 group 列表（含副露），回傳 (mentsu_groups, pair_group, win_group)。
    win_group：和牌張所屬的群組（用於判斷暗刻/聽牌型）。"""
    groups: list[Group] = []

    # 副露
    for m in melds:
        if m.meld_type == MeldType.CHI:
            groups.append(Group("shuntsu", list(m.tiles[:3]), open=True))
        elif m.meld_type == MeldType.PON:
            groups.append(Group("kotsu", list(m.tiles[:3]), open=True))
        elif m.meld_type == MeldType.KAN:
            groups.append(Group("kantsu", list(m.tiles[:4]), open=True))
        elif m.meld_type == MeldType.ANKAN:
            groups.append(Group("kantsu", list(m.tiles[:4]), open=False, ankan=True))

    # concealed mentsu
    for kind, key in mentsu_list:
        if kind == "shuntsu":
            tiles = [_key_to_tile((key[0], key[1])),
                     _key_to_tile((key[0], key[1] + 1)),
                     _key_to_tile((key[0], key[1] + 2))]
            groups.append(Group("shuntsu", tiles, open=False))
        else:
            tiles = [_key_to_tile(key)] * 3
            groups.append(Group("kotsu", tiles, open=False))

    pair = Group("pair", [_key_to_tile(pair_key)] * 2, open=False)

    # 找出 win_tile 所屬群組（concealed 完成的群組）
    win_group = _locate_win_group(groups, pair, win_tile, mentsu_list, pair_key)
    return groups, pair, win_group


def _locate_win_group(groups, pair, win_tile, mentsu_list, pair_key) -> Optional[Group]:
    wkey = tk(win_tile)
    # 雀頭單騎
    if pair_key == wkey:
        return pair
    # 找含 win_tile 的 concealed 群組
    for g in groups:
        if g.open or g.ankan:
            continue
        if g.kind == "kotsu" and g.key == wkey:
            return g
        if g.kind == "shuntsu":
            vals = [t.value for t in g.tiles]
            if win_tile.suit == g.tiles[0].suit and win_tile.value in vals:
                return g
    return None


def _score_standard(concealed, melds, win_tile, ctx: WinContext,
                    mentsu_list, pair_key, menzen) -> Optional[ScoreResult]:
    groups, pair, win_group = _build_groups(concealed, melds, win_tile, mentsu_list, pair_key, ctx)
    all_groups = groups + [pair]
    all14 = [t for g in groups for t in g.tiles[:3]] + pair.tiles

    # 榮和完成的刻子視為明刻（影響暗刻數與符）
    ron_completes_kotsu = (not ctx.is_tsumo and win_group is not None
                           and win_group.kind == "kotsu" and not win_group.open and not win_group.ankan)

    yaku: list[tuple[str, int]] = []
    yakuman: list[tuple[str, int]] = []
    han = 0

    shuntsu = [g for g in groups if g.kind == "shuntsu"]
    kotsu_kan = [g for g in groups if g.is_triplet_like]
    kans = [g for g in groups if g.kind == "kantsu"]

    # 標記和牌張是否落在雀頭（單騎）
    pair._is_win = (win_group is pair)

    # 暗刻數（含暗槓；榮和完成者不算暗刻）
    ankou = 0
    for g in kotsu_kan:
        if g.kind == "kantsu":
            if g.ankan:
                ankou += 1
        else:  # kotsu
            if not g.open:
                if g is win_group and ron_completes_kotsu:
                    pass  # 榮和→明刻
                else:
                    ankou += 1

    # ── 役滿系 ──
    _check_yakuman_standard(all_groups, groups, pair, ctx, ankou, kans, yakuman)
    if yakuman:
        r = ScoreResult(yakuman=yakuman)
        _finalize_yakuman(r, ctx)
        return r

    # ── 一般役 ──
    if ctx.is_double_riichi:
        yaku.append(("兩立直", 2)); han += 2
    elif ctx.is_riichi:
        yaku.append(("立直", 1)); han += 1
    if ctx.is_ippatsu:
        yaku.append(("一發", 1)); han += 1

    # 門前清自摸和
    if ctx.is_tsumo and menzen:
        yaku.append(("門前清自摸和", 1)); han += 1

    # 平和
    pinfu = _is_pinfu(groups, pair, win_group, win_tile, ctx, menzen)
    if pinfu:
        yaku.append(("平和", 1)); han += 1

    # 斷么九
    if all(not is_terminal_or_honor(t) for t in all14):
        yaku.append(("斷么九", 1)); han += 1

    # 役牌（場風/自風/三元）
    yakuhai_han, yakuhai_names = _yakuhai(kotsu_kan, ctx)
    for nm in yakuhai_names:
        yaku.append((nm, 1))
    han += yakuhai_han

    # 一盃口 / 二盃口（門前限定）
    if menzen:
        iipeikou = _count_iipeikou(shuntsu)
        if iipeikou == 2:
            yaku.append(("二盃口", 3)); han += 3
        elif iipeikou == 1:
            yaku.append(("一盃口", 1)); han += 1

    # 三色同順
    if _has_sanshoku_shuntsu(shuntsu):
        h = 2 if menzen else 1
        yaku.append(("三色同順", h)); han += h
    # 三色同刻
    if _has_sanshoku_kotsu(kotsu_kan):
        yaku.append(("三色同刻", 2)); han += 2
    # 一氣通貫
    if _has_ittsu(shuntsu):
        h = 2 if menzen else 1
        yaku.append(("一氣通貫", h)); han += h

    # 對對和
    if len(shuntsu) == 0 and len([g for g in kotsu_kan]) == 4:
        yaku.append(("對對和", 2)); han += 2
    # 三暗刻 / 四暗刻已在役滿處理；此處三暗刻
    if ankou == 3:
        yaku.append(("三暗刻", 2)); han += 2
    # 三槓子
    if len(kans) == 3:
        yaku.append(("三槓子", 2)); han += 2
    # 小三元
    if _is_shousangen(kotsu_kan, pair):
        yaku.append(("小三元", 2)); han += 2

    # 帶么九系
    chanta = _chanta_type(groups, pair)
    if chanta == "junchan":
        h = 3 if menzen else 2
        yaku.append(("純全帶么九", h)); han += h
    elif chanta == "chanta":
        h = 2 if menzen else 1
        yaku.append(("混全帶么九", h)); han += h
    # 混老頭（全么九且含字牌、非順子）
    if len(shuntsu) == 0 and all(g.all_terminal_or_honor() for g in all_groups) and any(is_honor(g.tiles[0]) for g in all_groups) and any(is_terminal(g.tiles[0]) for g in all_groups):
        yaku.append(("混老頭", 2)); han += 2

    # 清一色 / 混一色
    flush = _flush_han(all14, menzen)
    if flush:
        yaku.append(flush); han += flush[1]

    # 情境役（海底/河底/嶺上/槍槓，各 1 飜）
    if ctx.is_haitei:
        yaku.append(("海底摸月", 1)); han += 1
    if ctx.is_houtei:
        yaku.append(("河底撈魚", 1)); han += 1
    if ctx.is_rinshan:
        yaku.append(("嶺上開花", 1)); han += 1
    if ctx.is_chankan:
        yaku.append(("槍槓", 1)); han += 1

    if han == 0:
        return None  # 無役不可和（dora 不算役）

    # 寶牌（在確定有役後才加）
    han += _dora_han(all14, ctx, yaku, melds_full=melds)

    # 符計算
    fu = _calc_fu(groups, pair, win_group, win_tile, ctx, menzen, pinfu, ron_completes_kotsu)

    r = ScoreResult(yaku=yaku, han=han, fu=fu)
    _finalize_points(r, ctx)
    r.valid = True
    return r


# ─── 役滿判定（標準型）──────────────────────────────────────────────────────

def _check_yakuman_standard(all_groups, groups, pair, ctx, ankou, kans, yakuman):
    # 天和/地和
    if ctx.is_tenhou:
        yakuman.append(("天和", 1))
    if ctx.is_chiihou:
        yakuman.append(("地和", 1))

    kotsu_kan = [g for g in groups if g.is_triplet_like]

    # 大三元
    dragons = [g for g in kotsu_kan if g.tiles[0].suit == Suit.DRAGON]
    if len(dragons) == 3:
        yakuman.append(("大三元", 1))
    # 大四喜 / 小四喜
    winds = [g for g in kotsu_kan if g.tiles[0].suit == Suit.WIND]
    if len(winds) == 4:
        yakuman.append(("大四喜", 2))
    elif len(winds) == 3 and pair.tiles[0].suit == Suit.WIND:
        yakuman.append(("小四喜", 1))
    # 四暗刻
    if ankou == 4:
        if pair is not None and _is_tanki_win(pair, ctx):
            yakuman.append(("四暗刻單騎", 2))
        else:
            yakuman.append(("四暗刻", 1))
    # 字一色
    if all(is_honor(g.tiles[0]) for g in all_groups):
        yakuman.append(("字一色", 1))
    # 清老頭（全為老頭牌、皆為刻子/槓）
    if all(g.kind in ("kotsu", "kantsu", "pair") for g in all_groups) and \
       all(is_terminal(t) for g in all_groups for t in g.tiles[:3]):
        yakuman.append(("清老頭", 1))
    # 綠一色
    if _is_ryuuiisou(all_groups):
        yakuman.append(("綠一色", 1))
    # 四槓子
    if len(kans) == 4:
        yakuman.append(("四槓子", 1))
    # 九蓮寶燈
    chuuren = _chuuren(groups, pair, ctx)
    if chuuren == "junsei":
        yakuman.append(("純正九蓮寶燈", 2))
    elif chuuren == "normal":
        yakuman.append(("九蓮寶燈", 1))


def _is_tanki_win(pair: Group, ctx: WinContext) -> bool:
    # 由 win_group 判斷較準，這裡簡化：交給呼叫端在 fu/型別時已知
    return getattr(pair, "_is_win", False)


def _is_ryuuiisou(all_groups) -> bool:
    green_man = {(int(Suit.SOU), v) for v in (2, 3, 4, 6, 8)} | {(int(Suit.DRAGON), 2)}  # 發=2
    for g in all_groups:
        for t in g.tiles[:3]:
            if tk(t) not in green_man:
                return False
    return True


def _chuuren(groups, pair, ctx) -> Optional[str]:
    # 需門前清、同一數字花色、形如 1112345678999 + 1
    all_groups = groups + [pair]
    if any(g.open or g.ankan for g in groups):
        return None
    suits = set()
    for g in all_groups:
        for t in g.tiles[:3]:
            if t.suit not in NUMBER_SUITS:
                return None
            suits.add(int(t.suit))
    if len(suits) != 1:
        return None
    cnt = Counter()
    for g in all_groups:
        for t in g.tiles[:3]:
            cnt[t.value] += 1
    base = {1: 3, 2: 1, 3: 1, 4: 1, 5: 1, 6: 1, 7: 1, 8: 1, 9: 3}
    diff = {v: cnt.get(v, 0) - base.get(v, 0) for v in range(1, 10)}
    extra = [v for v, d in diff.items() if d > 0]
    if sum(1 for d in diff.values() if d < 0) > 0:
        return None
    if len(extra) == 1 and sum(d for d in diff.values() if d > 0) == 1:
        # 純正：和牌張使 9 面聽 → 簡化判斷：和牌前為標準九蓮型
        return "junsei" if getattr(ctx, "_chuuren_junsei", False) else "normal"
    return None


# ─── 平和 / 役牌 / 一盃口 / 三色 / 一通 / 帶么九 / 一色 ────────────────────────

def _is_pinfu(groups, pair, win_group, win_tile, ctx, menzen) -> bool:
    if not menzen:
        return False
    if any(g.kind != "shuntsu" for g in groups):
        return False
    # 雀頭不可為役牌
    if _is_yakuhai_tile(pair.tiles[0], ctx):
        return False
    # 聽牌必須為兩面（win_group 是順子且非坎張/邊張）
    if win_group is None or win_group.kind != "shuntsu":
        return False
    vals = sorted(t.value for t in win_group.tiles)
    wv = win_tile.value
    # 坎張：贏中間
    if wv == vals[1]:
        return False
    # 邊張：123 等 3 或 789 等 7
    if vals == [1, 2, 3] and wv == 3:
        return False
    if vals == [7, 8, 9] and wv == 7:
        return False
    return True


def _is_yakuhai_tile(t: Tile, ctx: WinContext) -> bool:
    if t.suit == Suit.DRAGON:
        return True
    if t.suit == Suit.WIND and t.value in (ctx.round_wind, ctx.seat_wind):
        return True
    return False


def _yakuhai(kotsu_kan, ctx) -> tuple[int, list[str]]:
    han = 0
    names = []
    for g in kotsu_kan:
        t = g.tiles[0]
        if t.suit == Suit.DRAGON:
            nm = {1: "中", 2: "發", 3: "白"}[t.value]
            names.append(f"役牌{nm}"); han += 1
        elif t.suit == Suit.WIND:
            if t.value == ctx.round_wind:
                names.append("場風"); han += 1
            if t.value == ctx.seat_wind:
                names.append("自風"); han += 1
    return han, names


def _count_iipeikou(shuntsu) -> int:
    keys = [(int(g.tiles[0].suit), g.tiles[0].value) for g in shuntsu]
    c = Counter(keys)
    pairs = sum(v // 2 for v in c.values())
    return min(pairs, 2)


def _has_sanshoku_shuntsu(shuntsu) -> bool:
    by_val = {}
    for g in shuntsu:
        v = g.tiles[0].value
        by_val.setdefault(v, set()).add(int(g.tiles[0].suit))
    return any(len(s) >= 3 for s in by_val.values())


def _has_sanshoku_kotsu(kotsu_kan) -> bool:
    by_val = {}
    for g in kotsu_kan:
        t = g.tiles[0]
        if t.suit in NUMBER_SUITS:
            by_val.setdefault(t.value, set()).add(int(t.suit))
    return any(len(s) >= 3 for s in by_val.values())


def _has_ittsu(shuntsu) -> bool:
    by_suit = {}
    for g in shuntsu:
        by_suit.setdefault(int(g.tiles[0].suit), set()).add(g.tiles[0].value)
    for s, starts in by_suit.items():
        if {1, 4, 7} <= starts:
            return True
    return False


def _is_shousangen(kotsu_kan, pair) -> bool:
    dragons = sum(1 for g in kotsu_kan if g.tiles[0].suit == Suit.DRAGON)
    return dragons == 2 and pair.tiles[0].suit == Suit.DRAGON


def _chanta_type(groups, pair) -> Optional[str]:
    all_groups = groups + [pair]
    # 每個群組都含么九
    if not all(g.contains_terminal_or_honor() for g in all_groups):
        return None
    has_shuntsu = any(g.kind == "shuntsu" for g in groups)
    if not has_shuntsu:
        return None  # 全刻子 → 屬混老頭/清老頭，不算帶么九
    has_honor = any(is_honor(t) for g in all_groups for t in g.tiles[:3])
    return "chanta" if has_honor else "junchan"


def _flush_han(all_tiles, menzen) -> Optional[tuple[str, int]]:
    suits = set()
    has_honor = False
    for t in all_tiles:
        if t.suit in NUMBER_SUITS:
            suits.add(int(t.suit))
        else:
            has_honor = True
    if len(suits) == 1:
        if not has_honor:
            return ("清一色", 6 if menzen else 5)
        return ("混一色", 3 if menzen else 2)
    return None


# ─── 寶牌 ───────────────────────────────────────────────────────────────────

def _dora_han(all_tiles, ctx: WinContext, yaku: list, melds_full=None) -> int:
    extra = 0
    # 表寶牌
    if ctx.dora_tiles:
        n = sum(1 for t in all_tiles for d in ctx.dora_tiles if tk(t) == tk(d))
        # 加上槓子第四張
        if melds_full:
            for m in melds_full:
                if m.meld_type in (MeldType.KAN, MeldType.ANKAN):
                    n += sum(1 for d in ctx.dora_tiles if tk(m.tiles[0]) == tk(d))
        if n:
            yaku.append(("寶牌", n)); extra += n
    # 裏寶牌（立直才有）
    if ctx.ura_tiles and (ctx.is_riichi or ctx.is_double_riichi):
        n = sum(1 for t in all_tiles for d in ctx.ura_tiles if tk(t) == tk(d))
        if melds_full:
            for m in melds_full:
                if m.meld_type in (MeldType.KAN, MeldType.ANKAN):
                    n += sum(1 for d in ctx.ura_tiles if tk(m.tiles[0]) == tk(d))
        # 立直一律列出裡寶牌（即使 0 飜也標示，方便確認結果）
        yaku.append(("裡寶牌", n)); extra += n
    # 赤寶牌（all_tiles 已含副露前三張的紅五；不重複計）
    aka = sum(1 for t in all_tiles if getattr(t, "red", False))
    if aka:
        yaku.append(("赤寶牌", aka)); extra += aka
    # 拔北（三麻北寶牌），每張 +1 飜
    if getattr(ctx, "kita", 0):
        yaku.append(("拔北", ctx.kita)); extra += ctx.kita
    return extra


# ─── 符計算 ─────────────────────────────────────────────────────────────────

def _calc_fu(groups, pair, win_group, win_tile, ctx, menzen, pinfu, ron_completes_kotsu) -> int:
    # 平和
    if pinfu:
        return 20 if ctx.is_tsumo else 30

    fu = 20
    # 門前榮和 +10
    if menzen and not ctx.is_tsumo:
        fu += 10
    # 自摸 +2（非平和）
    if ctx.is_tsumo:
        fu += 2

    # 面子符
    for g in groups:
        if g.kind == "shuntsu":
            continue
        yaochuu = is_terminal_or_honor(g.tiles[0])
        if g.kind == "kantsu":
            if g.ankan:
                fu += 32 if yaochuu else 16
            else:
                fu += 16 if yaochuu else 8
        else:  # kotsu
            is_open = g.open or (g is win_group and ron_completes_kotsu)
            if is_open:
                fu += 4 if yaochuu else 2
            else:
                fu += 8 if yaochuu else 4

    # 雀頭符（役牌）
    pt = pair.tiles[0]
    if pt.suit == Suit.DRAGON:
        fu += 2
    elif pt.suit == Suit.WIND:
        if pt.value == ctx.round_wind:
            fu += 2
        if pt.value == ctx.seat_wind:
            fu += 2  # 連風 +2/+2

    # 聽牌型符（坎張/邊張/單騎 +2）
    fu += _wait_fu(win_group, pair, win_tile)

    # 副露平和型不足 20 → 補 30（即無任何加符的副露榮和）
    if not menzen and fu == 20:
        fu = 30

    return _round_up(fu, 10)


def _wait_fu(win_group, pair, win_tile) -> int:
    if win_group is None:
        return 0
    if win_group.kind == "pair":
        return 2  # 單騎
    if win_group.kind == "shuntsu":
        vals = sorted(t.value for t in win_group.tiles)
        wv = win_tile.value
        if wv == vals[1]:
            return 2  # 坎張
        if vals == [1, 2, 3] and wv == 3:
            return 2  # 邊張
        if vals == [7, 8, 9] and wv == 7:
            return 2  # 邊張
    return 0


# ─── 點數結算 ───────────────────────────────────────────────────────────────

def _finalize_yakuman(r: ScoreResult, ctx: WinContext):
    if ctx.allow_double_yakuman:
        multiplier = sum(m[1] for m in r.yakuman)         # 混合：四暗單騎等算雙倍
    else:
        multiplier = len(r.yakuman)                       # 天鳳：每種役滿各算單倍
    r.han = 0
    r.fu = 0
    base = 8000 * multiplier
    r.base = base
    r.name = "役滿" if multiplier == 1 else f"{multiplier}倍役滿"
    _pay_from_base(r, ctx, base)
    r.valid = True


def _finalize_points(r: ScoreResult, ctx: WinContext):
    han, fu = r.han, r.fu
    if han >= 13:
        base = 8000; r.name = "役滿(累計)"
    elif han >= 11:
        base = 6000; r.name = "三倍滿"
    elif han >= 8:
        base = 4000; r.name = "倍滿"
    elif han >= 6:
        base = 3000; r.name = "跳滿"
    elif han >= 5:
        base = 2000; r.name = "滿貫"
    else:
        base = fu * (2 ** (2 + han))
        if base >= 2000:
            base = 2000; r.name = "滿貫"
        else:
            r.name = ""
    r.base = base
    _pay_from_base(r, ctx, base)


def _pay_from_base(r: ScoreResult, ctx: WinContext, base: int):
    if ctx.is_dealer:
        if ctx.is_tsumo:
            each = _round_up(base * 2, 100)
            r.tsumo_each_payment = each
            r.points = each * 3
        else:
            pay = _round_up(base * 6, 100)
            r.ron_payment = pay
            r.points = pay
    else:
        if ctx.is_tsumo:
            dealer = _round_up(base * 2, 100)
            other = _round_up(base * 1, 100)
            r.tsumo_dealer_payment = dealer
            r.tsumo_each_payment = other
            r.points = dealer + other * 2
        else:
            pay = _round_up(base * 4, 100)
            r.ron_payment = pay
            r.points = pay
