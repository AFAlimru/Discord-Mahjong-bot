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
mahjong_engine.py - Japanese Mahjong Engine
支持三麻（3人、108牌）和四麻（4人、136牌）
"""

from __future__ import annotations
import hashlib
import json
import random
import secrets
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Optional
from collections import Counter


# ─── Tile Definition ──────────────────────────────────────────────────────

class Suit(IntEnum):
    MAN = 0      # 萬子
    SOU = 1      # 條子
    PIN = 2      # 餅子
    WIND = 3     # 風牌（東南西北）
    DRAGON = 4   # 龍牌（中發白）


WIND_NAMES = ["東", "南", "西", "北"]
DRAGON_NAMES = ["中", "發", "白"]
SUIT_CHARS = ["m", "s", "p"]


@dataclass(frozen=True, order=True)
class Tile:
    """麻将牌"""
    suit: Suit
    value: int      # 1-9 for numeral; 1-4 for wind; 1-3 for dragon
    red: bool = False

    @property
    def unicode(self) -> str:
        """返回 Unicode 字符"""
        if self.suit == Suit.MAN:
            return chr(0x1F007 + self.value - 1)
        elif self.suit == Suit.SOU:
            return chr(0x1F010 + self.value - 1)
        elif self.suit == Suit.PIN:
            return chr(0x1F019 + self.value - 1)
        elif self.suit == Suit.WIND:
            return chr(0x1F000 + self.value - 1)
        else:  # DRAGON
            return chr(0x1F004 + self.value - 1)

    @property
    def short(self) -> str:
        """短格式，如 5m、3p"""
        if self.suit in (Suit.MAN, Suit.SOU, Suit.PIN):
            r = "r" if self.red else ""
            return f"{r}{self.value}{SUIT_CHARS[self.suit]}"
        elif self.suit == Suit.WIND:
            return WIND_NAMES[self.value - 1]
        else:
            return DRAGON_NAMES[self.value - 1]

    @property
    def code(self) -> str:
        """牌山編碼（麻雀一番街公開規則）：
        萬子 1~9m（紅5萬=0m）、筒子 1~9p（紅5筒=0p）、條子 1~9s（紅5條=0s）、
        字牌 1~7z 分別對應 東南西北白發中。
        """
        if self.suit == Suit.MAN:
            return ("0" if self.red else str(self.value)) + "m"
        if self.suit == Suit.PIN:
            return ("0" if self.red else str(self.value)) + "p"
        if self.suit == Suit.SOU:
            return ("0" if self.red else str(self.value)) + "s"
        if self.suit == Suit.WIND:
            # 1=東 2=南 3=西 4=北 → 1~4z
            return f"{self.value}z"
        # DRAGON：本引擎 value 1=中 2=發 3=白；官方 白=5z 發=6z 中=7z
        dragon_to_z = {1: 7, 2: 6, 3: 5}
        return f"{dragon_to_z[self.value]}z"

    def __str__(self) -> str:
        prefix = "🔴" if self.red else ""
        return prefix + self.unicode

    def to_dict(self) -> dict:
        return {"suit": int(self.suit), "value": self.value, "red": self.red}

    @classmethod
    def from_dict(cls, d: dict) -> Tile:
        return cls(Suit(d["suit"]), d["value"], d.get("red", False))


# ─── Wall Generation ──────────────────────────────────────────────────────
#
# 採用麻雀一番街公開方案：
#   1) 以密碼學安全亂數 (secrets) 做 Fisher–Yates 洗牌（等同 Go 的 crypto/rand）；
#   2) 牌山編碼 = 由前到後每張牌的官方編碼字串相接，如 "1m2p3s7z..."；
#   3) SHA-256 直接對「牌山編碼字串」計算，玩家可在任意 SHA-256 網站自行校驗。

def _secure_shuffle(tiles: list[Tile]) -> None:
    """密碼學安全的 Fisher–Yates 洗牌（每次交換取一次 secrets 亂數）。"""
    for i in range(len(tiles) - 1, 0, -1):
        j = secrets.randbelow(i + 1)
        tiles[i], tiles[j] = tiles[j], tiles[i]


def wall_encode(wall: list[Tile]) -> str:
    """依牌山順序（前→後）產生官方牌山編碼字串。"""
    return "".join(t.code for t in wall)


def _finalize_wall(tiles: list[Tile]) -> tuple[list[Tile], str, str]:
    """洗牌 → 產生編碼字串 → 計算 SHA-256。回傳 (牌山, 編碼字串, sha256)。"""
    _secure_shuffle(tiles)
    encoding = wall_encode(tiles)
    sha256 = hashlib.sha256(encoding.encode()).hexdigest()
    return tiles, encoding, sha256


def generate_wall_sanma() -> tuple[list[Tile], str, str]:
    """生成三麻牌山 (108张，移除2-8萬)"""
    tiles: list[Tile] = []

    # 1萬、9萬 各4张
    tiles.extend([Tile(Suit.MAN, 1) for _ in range(4)])
    tiles.extend([Tile(Suit.MAN, 9) for _ in range(4)])

    # 條子、餅子 各36张 (1-9 各4张)
    for suit in (Suit.SOU, Suit.PIN):
        for val in range(1, 10):
            for copy in range(4):
                red = (val == 5 and copy == 3)
                tiles.append(Tile(suit, val, red))

    # 風牌、龍牌
    for val in range(1, 5):
        tiles.extend([Tile(Suit.WIND, val) for _ in range(4)])
    for val in range(1, 4):
        tiles.extend([Tile(Suit.DRAGON, val) for _ in range(4)])

    assert len(tiles) == 108
    return _finalize_wall(tiles)


def generate_wall_yonma() -> tuple[list[Tile], str, str]:
    """生成四麻牌山 (136张，完整)"""
    tiles: list[Tile] = []

    # 萬子、條子、餅子 各36张
    for suit in (Suit.MAN, Suit.SOU, Suit.PIN):
        for val in range(1, 10):
            for copy in range(4):
                red = (val == 5 and copy == 3)
                tiles.append(Tile(suit, val, red))

    # 風牌、龍牌
    for val in range(1, 5):
        tiles.extend([Tile(Suit.WIND, val) for _ in range(4)])
    for val in range(1, 4):
        tiles.extend([Tile(Suit.DRAGON, val) for _ in range(4)])

    assert len(tiles) == 136
    return _finalize_wall(tiles)


def wall_to_layout(wall: list[Tile]) -> dict:
    """分配牌山布局"""
    is_sanma = len(wall) == 108
    
    if is_sanma:
        # 三麻：70张摸牌 + 14张死牌
        live_count = 70
    else:
        # 四麻：122张摸牌 + 14张死牌
        live_count = 122
    
    return {
        "live_wall": wall[:live_count],
        "dora_indicators": [wall[live_count], wall[live_count + 2], wall[live_count + 4], wall[live_count + 6], wall[live_count + 8]],
        "ura_indicators": [wall[live_count + 1], wall[live_count + 3], wall[live_count + 5]],
        "rinshan_tiles": [wall[live_count + 6], wall[live_count + 7], wall[live_count + 8], wall[live_count + 9]],
    }


# ─── Game State ────────────────────────────────────────────────────────

class MeldType(IntEnum):
    """副露类型"""
    CHI = 1      # 吃
    PON = 2      # 碰
    KAN = 3      # 杠
    ANKAN = 4    # 暗杠


@dataclass
class Meld:
    """副露（吃、碰、杠、暗杠）"""
    meld_type: int  # MeldType
    tiles: list[Tile] = field(default_factory=list)
    from_seat: int = -1  # 谁打出的牌（-1表示暗杠）
    
    def __str__(self) -> str:
        type_names = {1: "吃", 2: "碰", 3: "杠", 4: "暗杠"}
        name = type_names.get(self.meld_type, "?")
        tiles_str = "".join(str(t) for t in self.tiles)
        return f"{name}:{tiles_str}"


@dataclass
class PlayerState:
    """玩家状态"""
    seat: int
    user_id: str
    username: str
    hand: list[Tile] = field(default_factory=list)
    drawn_tile: Optional[Tile] = None
    discards: list[Tile] = field(default_factory=list)
    melds: list = field(default_factory=list)  # 副露列表
    score: int = 25000
    riichi: bool = False
    is_dealer: bool = False
    is_bot: bool = False
    kita: int = 0   # 三麻拔北（北寶牌）累計張數

    @property
    def full_hand(self) -> list[Tile]:
        return self.hand + ([self.drawn_tile] if self.drawn_tile else [])

    def hand_display(self) -> str:
        """显示手牌（13张，Unicode 紧贴）"""
        if not self.hand:
            return ""
        
        sorted_hand = sorted(self.hand, key=lambda t: (t.suit, t.value))
        return "".join(str(t) for t in sorted_hand)
    
    def hand_display_with_names(self) -> tuple:
        """返回两种格式的手牌显示
        返回值：(unicode_display, name_display)
        例如: (🀉 🀌 🀍, 123m456p東南西北中白發)
        """
        if not self.hand:
            return ("", "")
        
        sorted_hand = sorted(self.hand, key=lambda t: (t.suit, t.value))

        # Unicode 显示（每个牌之间空格）
        unicode_display = " ".join(str(t) for t in sorted_hand)

        # 牌名显示：花色字母在前，例如 m123456 s123 p55 東東中
        suit_letter = {0: 'm', 1: 's', 2: 'p'}
        wind_names = ["東", "南", "西", "北"]
        dragon_names = ["中", "發", "白"]   # value 1=中 2=發 3=白

        groups: list[str] = []
        honors: list[str] = []
        cur_suit = -1
        cur_vals: list[str] = []

        def _flush():
            nonlocal cur_suit, cur_vals
            if cur_vals:
                groups.append(suit_letter[cur_suit] + "".join(cur_vals))
                cur_vals = []
                cur_suit = -1

        for t in sorted_hand:
            s = int(t.suit)
            if s == 3:       # 风牌
                _flush()
                honors.append(wind_names[t.value - 1])
            elif s == 4:     # 龙牌
                _flush()
                honors.append(dragon_names[t.value - 1])
            else:
                if s != cur_suit:
                    _flush()
                    cur_suit = s
                cur_vals.append(str(t.value))
        _flush()

        parts = groups[:]
        if honors:
            parts.append("".join(honors))
        name_display = " ".join(parts)

        return (unicode_display, name_display)

    def to_dict(self) -> dict:
        return {
            "seat": self.seat,
            "user_id": self.user_id,
            "username": self.username,
            "hand": [t.to_dict() for t in self.hand],
            "drawn_tile": self.drawn_tile.to_dict() if self.drawn_tile else None,
            "discards": [t.to_dict() for t in self.discards],
            "score": self.score,
            "riichi": self.riichi,
            "is_dealer": self.is_dealer,
            "is_bot": self.is_bot,
        }

    @classmethod
    def from_dict(cls, d: dict) -> PlayerState:
        p = cls(d["seat"], d["user_id"], d["username"])
        p.hand = [Tile.from_dict(t) for t in d["hand"]]
        p.drawn_tile = Tile.from_dict(d["drawn_tile"]) if d["drawn_tile"] else None
        p.discards = [Tile.from_dict(t) for t in d["discards"]]
        p.score = d["score"]
        p.riichi = d["riichi"]
        p.is_dealer = d["is_dealer"]
        p.is_bot = d.get("is_bot", False)
        return p


@dataclass
class GameState:
    """牌局状态"""
    game_id: str
    is_sanma: bool = False  # True=三麻, False=四麻
    round_wind: int = 1     # 1=東, 2=南
    round_num: int = 1
    honba: int = 0
    riichi_sticks: int = 0
    dealer_seat: int = 0
    current_seat: int = 0
    turn: int = 0
    phase: str = "discard"  # "discard" | "reaction" | "finished"

    players: list[PlayerState] = field(default_factory=list)
    live_wall: list[Tile] = field(default_factory=list)
    wall_idx: int = 0
    dora_indicators: list[Tile] = field(default_factory=list)
    ura_indicators: list[Tile] = field(default_factory=list)
    rinshan_tiles: list[Tile] = field(default_factory=list)
    rinshan_idx: int = 0
    revealed_dora: int = 1

    wall_seed: str = ""
    wall_sha256: str = ""

    pending_discard: Optional[Tile] = None
    pending_from_seat: int = -1

    @property
    def tiles_left(self) -> int:
        return len(self.live_wall) - self.wall_idx

    @property
    def round_label(self) -> str:
        winds = ["東", "南", "西", "北"]
        return f"{winds[self.round_wind - 1]}{self.round_num}局"

    def draw_tile(self) -> Optional[Tile]:
        if self.wall_idx >= len(self.live_wall):
            return None
        tile = self.live_wall[self.wall_idx]
        self.wall_idx += 1
        self.turn += 1
        return tile

    def open_dora(self) -> None:
        if self.revealed_dora < len(self.dora_indicators):
            self.revealed_dora += 1

    def get_doras(self) -> list[Tile]:
        """返回当前寶牌"""
        doras = []
        for ind in self.dora_indicators[:self.revealed_dora]:
            # 寶牌指示牌的下一张牌是寶牌
            if ind.suit == Suit.WIND:
                next_val = (ind.value % 4) + 1
                doras.append(Tile(Suit.WIND, next_val))
            elif ind.suit == Suit.DRAGON:
                next_val = (ind.value % 3) + 1
                doras.append(Tile(Suit.DRAGON, next_val))
            else:
                next_val = (ind.value % 9) + 1
                doras.append(Tile(ind.suit, next_val))
        return doras

    def to_dict(self) -> dict:
        return {
            "game_id": self.game_id,
            "is_sanma": self.is_sanma,
            "round_wind": self.round_wind,
            "round_num": self.round_num,
            "honba": self.honba,
            "riichi_sticks": self.riichi_sticks,
            "dealer_seat": self.dealer_seat,
            "current_seat": self.current_seat,
            "turn": self.turn,
            "phase": self.phase,
            "players": [p.to_dict() for p in self.players],
            "live_wall": [t.to_dict() for t in self.live_wall],
            "wall_idx": self.wall_idx,
            "dora_indicators": [t.to_dict() for t in self.dora_indicators],
            "ura_indicators": [t.to_dict() for t in self.ura_indicators],
            "rinshan_tiles": [t.to_dict() for t in self.rinshan_tiles],
            "rinshan_idx": self.rinshan_idx,
            "revealed_dora": self.revealed_dora,
            "wall_seed": self.wall_seed,
            "wall_sha256": self.wall_sha256,
            "pending_discard": self.pending_discard.to_dict() if self.pending_discard else None,
            "pending_from_seat": self.pending_from_seat,
        }

    @classmethod
    def from_dict(cls, d: dict) -> GameState:
        gs = cls(d["game_id"], d.get("is_sanma", False))
        gs.round_wind = d["round_wind"]
        gs.round_num = d["round_num"]
        gs.honba = d["honba"]
        gs.riichi_sticks = d["riichi_sticks"]
        gs.dealer_seat = d["dealer_seat"]
        gs.current_seat = d["current_seat"]
        gs.turn = d["turn"]
        gs.phase = d["phase"]
        gs.players = [PlayerState.from_dict(p) for p in d["players"]]
        gs.live_wall = [Tile.from_dict(t) for t in d["live_wall"]]
        gs.wall_idx = d["wall_idx"]
        gs.dora_indicators = [Tile.from_dict(t) for t in d["dora_indicators"]]
        gs.ura_indicators = [Tile.from_dict(t) for t in d["ura_indicators"]]
        gs.rinshan_tiles = [Tile.from_dict(t) for t in d["rinshan_tiles"]]
        gs.rinshan_idx = d["rinshan_idx"]
        gs.revealed_dora = d["revealed_dora"]
        gs.wall_seed = d["wall_seed"]
        gs.wall_sha256 = d["wall_sha256"]
        gs.pending_discard = Tile.from_dict(d["pending_discard"]) if d["pending_discard"] else None
        gs.pending_from_seat = d["pending_from_seat"]
        return gs


def new_game(game_id: str, players_info: list[dict], is_sanma: bool = False,
             start_points: int | None = None) -> GameState:
    """创建新牌局"""
    if is_sanma:
        wall, seed, sha = generate_wall_sanma()
        player_count = 3
    else:
        wall, seed, sha = generate_wall_yonma()
        player_count = 4

    layout = wall_to_layout(wall)
    
    gs = GameState(game_id, is_sanma)
    gs.wall_seed = seed
    gs.wall_sha256 = sha
    gs.live_wall = layout["live_wall"]
    gs.dora_indicators = layout["dora_indicators"]
    gs.ura_indicators = layout["ura_indicators"]
    gs.rinshan_tiles = layout["rinshan_tiles"]

    # 配牌
    tiles_per_player = 13 if is_sanma else 13
    idx = 0
    hands = [[] for _ in range(player_count)]
    
    # 配4轮各3张（或2张，具体看规则），最后庄家再摸1张
    for _ in range(3):
        for seat in range(player_count):
            hands[seat].extend(gs.live_wall[idx:idx + 4])
            idx += 4
    
    for seat in range(player_count):
        hands[seat].append(gs.live_wall[idx])
        idx += 1
    
    gs.wall_idx = idx

    for seat, info in enumerate(players_info[:player_count]):
        p = PlayerState(
            seat=seat,
            user_id=info["user_id"],
            username=info["username"],
            is_dealer=(seat == 0),
            is_bot=info.get("is_bot", False),
        )
        p.hand = hands[seat]
        # 預設起始點數：四麻 25000、三麻 35000（可由 start_points 覆寫）
        if start_points is not None:
            p.score = start_points
        else:
            p.score = 35000 if is_sanma else 25000
        gs.players.append(p)

    gs.current_seat = 0
    gs.phase = "discard"

    return gs


# ─── Hand Validation ──────────────────────────────────────────────────────

def is_complete(tiles: list[Tile]) -> bool:
    """检查手牌是否成胡（含七對子、國士無雙特殊型）"""
    if len(tiles) != 14:
        return False

    from collections import Counter
    counts = Counter((t.suit, t.value) for t in tiles)

    # 七對子：7 種各 2 張
    if len(counts) == 7 and all(v == 2 for v in counts.values()):
        return True

    # 國士無雙：13 種么九牌齊全，其一成對
    yaochuu = {
        (Suit.MAN, 1), (Suit.MAN, 9), (Suit.SOU, 1), (Suit.SOU, 9),
        (Suit.PIN, 1), (Suit.PIN, 9),
        (Suit.WIND, 1), (Suit.WIND, 2), (Suit.WIND, 3), (Suit.WIND, 4),
        (Suit.DRAGON, 1), (Suit.DRAGON, 2), (Suit.DRAGON, 3),
    }
    if set(counts.keys()) == yaochuu and any(v == 2 for v in counts.values()):
        return True
    
    def remove_mentsu(c: Counter) -> bool:
        if not c:
            return True
        key = min(c)
        suit, val = key
        
        # 顺子（仅限数字牌）
        if suit in (Suit.MAN, Suit.SOU, Suit.PIN) and val <= 7:
            seq_keys = [(suit, val), (suit, val + 1), (suit, val + 2)]
            if all(c[k] > 0 for k in seq_keys):
                c2 = c.copy()
                for k in seq_keys:
                    c2[k] -= 1
                    if c2[k] == 0:
                        del c2[k]
                if remove_mentsu(c2):
                    return True
        
        # 刻子
        if c[key] >= 3:
            c2 = c.copy()
            c2[key] -= 3
            if c2[key] == 0:
                del c2[key]
            if remove_mentsu(c2):
                return True
        
        return False
    
    # 尝试每个可能的对子
    for key in list(counts.keys()):
        if counts[key] >= 2:
            c2 = counts.copy()
            c2[key] -= 2
            if c2[key] == 0:
                del c2[key]
            if remove_mentsu(c2):
                return True
    
    return False


def is_tenpai(hand: list[Tile]) -> bool:
    """检查13张手是否聽牌"""
    all_types = set((t.suit, t.value) for t in hand)
    
    for suit in (Suit.MAN, Suit.SOU, Suit.PIN):
        for val in range(1, 10):
            if suit == Suit.MAN and val in range(2, 9):
                continue  # 三麻时跳过2-8萬
            test_tile = Tile(suit, val)
            if (test_tile.suit, test_tile.value) not in all_types:
                if is_complete(hand + [test_tile]):
                    return True
    
    for suit in (Suit.WIND, Suit.DRAGON):
        for val in range(1, (5 if suit == Suit.WIND else 4)):
            test_tile = Tile(suit, val)
            if (test_tile.suit, test_tile.value) not in all_types:
                if is_complete(hand + [test_tile]):
                    return True
    
    return False


def get_tenpai_waits(hand: list[Tile]) -> list[Tile]:
    """返回聽牌的待牌列表"""
    waits = []
    all_types = set((t.suit, t.value) for t in hand)
    
    for suit in (Suit.MAN, Suit.SOU, Suit.PIN):
        for val in range(1, 10):
            if suit == Suit.MAN and val in range(2, 9):
                continue
            test_tile = Tile(suit, val)
            if (test_tile.suit, test_tile.value) not in all_types:
                if is_complete(hand + [test_tile]):
                    waits.append(test_tile)
    
    for suit in (Suit.WIND, Suit.DRAGON):
        for val in range(1, (5 if suit == Suit.WIND else 4)):
            test_tile = Tile(suit, val)
            if (test_tile.suit, test_tile.value) not in all_types:
                if is_complete(hand + [test_tile]):
                    waits.append(test_tile)
    
    return waits


def count_dora(hand: list[Tile], dora_list: list[Tile]) -> int:
    """计算寶牌数"""
    count = 0
    for tile in hand:
        for dora in dora_list:
            if tile.suit == dora.suit and tile.value == dora.value:
                count += 1
    return count