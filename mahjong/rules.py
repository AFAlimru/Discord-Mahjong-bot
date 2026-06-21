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
"""純遊戲邏輯：牌面工具、AI、和牌評估、聽牌/振聽判定（不依賴 discord）。"""
from __future__ import annotations
from collections import Counter
from typing import Optional

from .engine import (
    Tile, Suit, Meld, MeldType, GameState, PlayerState, is_complete,
)
from .scoring import score_hand, WinContext
from .state import _room_configs
from . import i18n

# ═══════════════════════════════════════════════════════════════
#  Tile helpers
# ═══════════════════════════════════════════════════════════════

def count_tiles(hand: list[Tile], tile: Tile) -> int:
    return sum(1 for t in hand if t.suit == tile.suit and t.value == tile.value)


def get_chi_options(hand: list[Tile], tile: Tile) -> list[tuple[Tile, Tile]]:
    if tile.suit not in (Suit.MAN, Suit.SOU, Suit.PIN):
        return []
    counts = Counter((t.suit, t.value) for t in hand)
    v = tile.value
    options: list[tuple[Tile, Tile]] = []
    for a, b in [(v - 2, v - 1), (v - 1, v + 1), (v + 1, v + 2)]:
        if 1 <= a <= 9 and 1 <= b <= 9:
            if counts[(tile.suit, a)] >= 1 and counts[(tile.suit, b)] >= 1:
                t1 = next(t for t in hand if t.suit == tile.suit and t.value == a)
                t2 = next(t for t in hand if t.suit == tile.suit and t.value == b)
                options.append((t1, t2))
    return options


def get_ankan_options(hand: list[Tile]) -> list[Tile]:
    counts = Counter((t.suit, t.value) for t in hand)
    seen: set = set()
    result: list[Tile] = []
    for t in hand:
        key = (t.suit, t.value)
        if counts[key] >= 4 and key not in seen:
            seen.add(key)
            result.append(t)
    return result


def has_kita(player: PlayerState) -> bool:
    """三麻：手中（或剛摸）是否有北，可拔北。"""
    full = list(player.hand) + ([player.drawn_tile] if player.drawn_tile else [])
    return any(t.suit == Suit.WIND and t.value == 4 for t in full)


def get_shouminkan_options(player: PlayerState) -> list[Tile]:
    """加槓候選：已碰過的牌，且手中（或剛摸）持有第 4 張。"""
    pon_keys = {(int(m.tiles[0].suit), m.tiles[0].value)
                for m in player.melds if m.meld_type == MeldType.PON}
    full = list(player.hand) + ([player.drawn_tile] if player.drawn_tile else [])
    result: list[Tile] = []
    seen: set = set()
    for t in full:
        key = (int(t.suit), t.value)
        if key in pon_keys and key not in seen:
            seen.add(key)
            result.append(t)
    return result


def parse_tile(text: str, hand: list[Tile], drawn: Optional[Tile] = None) -> Optional[Tile]:
    text = text.strip().lower()
    pool = list(hand) + ([drawn] if drawn else [])
    aliases = {"e": "東", "s": "南", "w": "西", "n": "北",
               "r": "中", "wh": "白", "g": "發"}
    if text in aliases:
        text = aliases[text]

    # 紅寶牌：0m / 0p / 0s ＝ 該花色的紅五
    red_suit = {"0m": Suit.MAN, "0p": Suit.PIN, "0s": Suit.SOU}.get(text)
    if red_suit is not None:
        for t in pool:
            if t.suit == red_suit and t.value == 5 and getattr(t, "red", False):
                return t
        return None

    # 一般短碼比對
    for t in pool:
        if t.short.lower() == text:
            return t

    # 輸入 5m/5p/5s 但手上只有紅五 → 仍可選到紅五
    norm_suit = {"5m": Suit.MAN, "5p": Suit.PIN, "5s": Suit.SOU}.get(text)
    if norm_suit is not None:
        for t in pool:
            if t.suit == norm_suit and t.value == 5:
                return t
    return None


# ═══════════════════════════════════════════════════════════════
#  AI logic  (no external API, pure heuristic)
# ═══════════════════════════════════════════════════════════════

def _tile_connectivity(t: Tile, counts: Counter) -> int:
    """Score how well a tile connects with others (higher = keep)."""
    key = (t.suit, t.value)
    score = 0
    if counts[key] >= 2:
        score += 4           # pair / triplet bonus
    if counts[key] >= 3:
        score += 4
    if t.suit in (Suit.MAN, Suit.SOU, Suit.PIN):
        for d in (-2, -1, 1, 2):
            nv = t.value + d
            if 1 <= nv <= 9 and counts[(t.suit, nv)] > 0:
                score += 2 if abs(d) == 1 else 1
    return score


def ai_choose_discard(hand: list[Tile], drawn: Optional[Tile] = None) -> Optional[Tile]:
    """Discard the tile with the lowest connectivity score."""
    full = list(hand) + ([drawn] if drawn else [])
    if not full:
        return None
    counts = Counter((t.suit, t.value) for t in full)
    return min(full, key=lambda t: _tile_connectivity(t, counts))


def ai_should_pon(hand: list[Tile], tile: Tile) -> bool:
    return count_tiles(hand, tile) >= 2


# ═══════════════════════════════════════════════════════════════
#  和牌評估（接 scoring）
# ═══════════════════════════════════════════════════════════════

def _indicator_to_dora(ind: Tile) -> Tile:
    """寶牌指示牌 → 寶牌（下一張）。"""
    if ind.suit == Suit.WIND:
        return Tile(Suit.WIND, (ind.value % 4) + 1)
    if ind.suit == Suit.DRAGON:
        return Tile(Suit.DRAGON, (ind.value % 3) + 1)
    return Tile(ind.suit, (ind.value % 9) + 1)


def get_ura_tiles(gs: GameState) -> list[Tile]:
    return [_indicator_to_dora(i) for i in gs.ura_indicators[:gs.revealed_dora]]


def build_ctx(gs: GameState, player: PlayerState, win_tile: Tile, is_tsumo: bool,
              is_rinshan: bool = False, is_chankan: bool = False,
              is_ippatsu: bool = False, is_double_riichi: bool = False,
              is_tenhou: bool = False, is_chiihou: bool = False) -> WinContext:
    n = len(gs.players)
    seat_wind = ((player.seat - gs.dealer_seat) % n) + 1
    last_tile = (gs.tiles_left == 0)
    ruleset = _room_configs.get(gs.game_id, {}).get("ruleset", "mixed")
    return WinContext(
        is_tsumo=is_tsumo,
        is_riichi=player.riichi,
        is_double_riichi=is_double_riichi,
        is_ippatsu=is_ippatsu,
        round_wind=gs.round_wind,
        seat_wind=seat_wind,
        dora_tiles=gs.get_doras(),
        ura_tiles=get_ura_tiles(gs),
        is_dealer=player.is_dealer,
        is_haitei=(is_tsumo and last_tile),
        is_houtei=((not is_tsumo) and last_tile),
        is_rinshan=is_rinshan,
        is_chankan=is_chankan,
        is_tenhou=is_tenhou,
        is_chiihou=is_chiihou,
        allow_double_yakuman=(ruleset != "tenhou"),
        kita=getattr(player, "kita", 0),
    )


def evaluate_win(gs: GameState, player: PlayerState, win_tile: Tile, is_tsumo: bool,
                 **flags):
    """回傳有效的 ScoreResult，無役/未成胡回傳 None。"""
    ctx = build_ctx(gs, player, win_tile, is_tsumo, **flags)
    res = score_hand(list(player.hand), list(player.melds), win_tile, ctx)
    return res if res.valid else None


def hand_waits(player: PlayerState) -> set:
    """玩家目前的待牌（牌型完成，含副露，忽略役）。回傳 {(suit,value), ...}。"""
    base = list(player.hand) + [t for m in player.melds for t in m.tiles[:3]]
    if len(base) % 3 != 1:
        return set()
    waits = set()
    for suit in (Suit.MAN, Suit.SOU, Suit.PIN, Suit.WIND, Suit.DRAGON):
        rng = 9 if suit in (Suit.MAN, Suit.SOU, Suit.PIN) else (4 if suit == Suit.WIND else 3)
        for v in range(1, rng + 1):
            if is_complete(base + [Tile(suit, v)]):
                waits.add((int(suit), v))
    return waits


def tenpai_advice(player: PlayerState) -> list:
    """輪到時（14 張）分析：回傳 [(要打的牌, [待牌...]), ...]，即打哪張可進聽、聽哪些。"""
    full = list(player.hand) + ([player.drawn_tile] if player.drawn_tile else [])
    meld_tiles = [t for m in player.melds for t in m.tiles[:3]]
    if (len(full) + len(meld_tiles)) % 3 != 2:
        return []
    results = []
    seen = set()
    for d in full:
        key = (int(d.suit), d.value)
        if key in seen:
            continue
        seen.add(key)
        remaining = list(full)
        remaining.remove(d)
        base = remaining + meld_tiles
        waits = []
        for suit in (Suit.MAN, Suit.SOU, Suit.PIN, Suit.WIND, Suit.DRAGON):
            rng = 9 if suit in (Suit.MAN, Suit.SOU, Suit.PIN) else (4 if suit == Suit.WIND else 3)
            for v in range(1, rng + 1):
                if is_complete(base + [Tile(suit, v)]):
                    waits.append(Tile(suit, v))
        if waits:
            results.append((d, waits))
    return results


def is_menzen(player: PlayerState) -> bool:
    """門清：沒有鳴牌副露。暗槓不算鳴牌，仍視為門清（可立直）。"""
    return all(m.meld_type == MeldType.ANKAN for m in player.melds)


def can_declare_riichi(player: PlayerState) -> bool:
    """可立直：門清（暗槓不算副露）、未立直，且 14 張中至少有一打能保持聽牌。"""
    return (not player.riichi) and is_menzen(player) and bool(tenpai_advice(player))


def _wait_flags(gs: GameState, player: PlayerState, discard: Tile, waits: list) -> tuple:
    """某個進聽打法的提醒標記，回傳 (無役, 振聽, 榮和無役)。
    振聽：待牌曾在自己牌河（含正要打出的這張）。
    無役：對所有待牌，榮和與自摸皆無役 → 完全不可和（多半是副露手）。
    榮和無役：對所有待牌榮和皆無役（但自摸有役，如門清自摸）→ 未立直不能榮和。"""
    river = {(int(t.suit), t.value) for t in player.discards}
    river.add((int(discard.suit), discard.value))
    furiten = any((int(w.suit), w.value) in river for w in waits)

    full = list(player.hand) + ([player.drawn_tile] if player.drawn_tile else [])
    post = list(full)
    if discard in post:
        post.remove(discard)
    saved = player.hand
    player.hand = post
    try:
        ron_no_yaku = all(evaluate_win(gs, player, w, is_tsumo=False) is None for w in waits)
        tsumo_no_yaku = all(evaluate_win(gs, player, w, is_tsumo=True) is None for w in waits)
    finally:
        player.hand = saved
    no_yaku = ron_no_yaku and tsumo_no_yaku
    return no_yaku, furiten, ron_no_yaku


def tenpai_note_text(gs: GameState, player: PlayerState, adv: list,
                     lang: str = i18n.DEFAULT) -> str:
    """組出「💡 可進聽」提示（依語言），並對每個打法標註（無役）／（振聽）。"""
    # 門清且未立直時，才需要提醒「未立直不能榮和」（立直即可解）
    menzen_no_riichi = is_menzen(player) and not player.riichi
    lines = []
    any_no_yaku = any_furiten = any_ron_only = False
    for d, waits in adv:
        no_yaku, furiten, ron_no_yaku = _wait_flags(gs, player, d, waits)
        ron_only = menzen_no_riichi and ron_no_yaku and not no_yaku  # 自摸有役、榮和無役
        any_no_yaku |= no_yaku
        any_furiten |= furiten
        any_ron_only |= ron_only
        tags = []
        if no_yaku:
            tags.append(i18n.t("term.no_yaku", lang))
        if ron_only:
            tags.append(i18n.t("term.ron_no_yaku", lang))
        if furiten:
            tags.append(i18n.t("term.furiten", lang))
        suffix = f"　（{'・'.join(tags)}）" if tags else ""
        lines.append(i18n.t("tenpai.line", lang, d=d,
                            waits=' '.join(str(w) for w in waits)) + suffix)
    note = i18n.t("tenpai.advice_title", lang) + "\n" + "\n".join(lines)
    extra = []
    if any_no_yaku:
        extra.append(i18n.t("tenpai.expl_no_yaku", lang))
    if any_ron_only:
        extra.append(i18n.t("tenpai.expl_ron_only", lang))
    if any_furiten:
        extra.append(i18n.t("tenpai.expl_furiten", lang))
    if extra:
        note += "\n（" + "；".join(extra) + "）"
    return note


def is_furiten(player: PlayerState, perm: dict, temp: dict) -> bool:
    """振聽判定：永久(立直)振聽 / 同巡振聽 / 捨牌振聽（待牌曾在自己牌河）。"""
    if perm.get(player.seat) or temp.get(player.seat):
        return True
    waits = hand_waits(player)
    if not waits:
        return False
    discarded = {(int(t.suit), t.value) for t in player.discards}
    return bool(waits & discarded)


def ai_should_ron(gs: GameState, player: PlayerState, tile: Tile,
                  perm: dict = None, temp: dict = None) -> bool:
    """AI 是否榮和：成胡有役且非振聽。"""
    if perm is not None and is_furiten(player, perm, temp or {}):
        return False
    return evaluate_win(gs, player, tile, is_tsumo=False) is not None
