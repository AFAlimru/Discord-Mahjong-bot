#!/usr/bin/env python3
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
from __future__ import annotations
import asyncio
import os
import uuid
from collections import Counter
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN", "")
if not TOKEN:
    print("❌ DISCORD_TOKEN 未設置！")
    exit(1)

import database as db
from mahjong_engine import (
    GameState, PlayerState, Tile, Suit, Meld, MeldType,
    new_game, is_complete, is_tenpai, get_tenpai_waits,
)
from scoring import score_hand, WinContext, is_chiitoitsu, is_kokushi
import settlement as st
from views import RoomSettingsView, DiscardInputModal, BoardRefreshView

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

_games:             dict[str, GameState]        = {}
_channel_games:     dict[str, str]              = {}
_waiting:           dict[str, list[dict]]       = {}
_room_owners:       dict[str, str]              = {}
_room_configs:      dict[str, dict]             = {}
_pending_reactions: dict[str, dict[str, dict]]  = {}
_user_game:         dict[str, str]              = {}
# user_id -> TurnView (for /riichi /tsumo commands during turn)
_active_turn:       dict[str, "TurnView"]       = {}
# game_id -> asyncio.Task (the running game loop, for /end cancellation)
_game_tasks:        dict[str, asyncio.Task]     = {}
# game_id -> LobbyView (for /join to update the lobby message)
_lobbies:           dict[str, "LobbyView"]      = {}

# ─── 0.2 討論串 UI ─────────────────────────────────────────────
# game_id -> {"public": Thread, "board_msg": Message,
#             "private": {uid: Thread}, "hand_msg": {uid: Message}}
_threads:           dict[str, dict]             = {}
# user_id -> asyncio.Queue（等待該玩家打字輸入時存在；on_message 投入 (raw, message)）
_input_queues:      dict[str, asyncio.Queue]    = {}
# user_id -> 該玩家應輸入的討論串 channel id（驗證打字來源）
_input_thread:      dict[str, int]              = {}
# thread channel id -> game_id（讓 /end 等指令能在討論串內使用）
_thread_game:       dict[int, str]              = {}

WIND_LABELS = ["東", "南", "西", "北"]
AI_NAMES    = ["小春", "小夏", "小秋", "小冬"]


# ═══════════════════════════════════════════════════════════════
#  打字輸入（討論串）
# ═══════════════════════════════════════════════════════════════

@bot.event
async def on_message(message: discord.Message) -> None:
    """讀取玩家在自己私人討論串打的字，投入對應的輸入佇列。"""
    if message.author.bot:
        return
    uid = str(message.author.id)
    q = _input_queues.get(uid)
    if q is not None and message.channel.id == _input_thread.get(uid):
        await q.put((message.content.strip(), message))


async def wait_typed(uid: str, thread: discord.abc.Messageable, thread_id: int,
                     timeout: float, validate) -> Optional[tuple]:
    """
    等待玩家在其私人討論串打字。validate(raw) 回傳 (ok, value, err)。
    收到一則訊息就刪除它；有效 → 回傳 ('value', value)；無效 → 提示後繼續等。
    逾時 → 回傳 None。
    """
    import time as _time
    q: asyncio.Queue = asyncio.Queue()
    _input_queues[uid] = q
    _input_thread[uid] = thread_id
    deadline = _time.monotonic() + timeout
    try:
        while True:
            remaining = deadline - _time.monotonic()
            if remaining <= 0:
                return None
            try:
                raw, msg = await asyncio.wait_for(q.get(), timeout=remaining)
            except asyncio.TimeoutError:
                return None
            try:
                await msg.delete()
            except Exception:
                pass
            ok, value, err = validate(raw)
            if ok:
                return value
            # 無效輸入 → 暫時提示（稍後自動刪）
            try:
                warn = await thread.send(f"❌ {err}")
                asyncio.create_task(_delete_later(warn, 4))
            except Exception:
                pass
    finally:
        _input_queues.pop(uid, None)
        _input_thread.pop(uid, None)


async def _delete_later(msg: discord.Message, delay: float) -> None:
    await asyncio.sleep(delay)
    try:
        await msg.delete()
    except Exception:
        pass


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
    aliases = {"e": "東", "s": "南", "w": "西", "n": "北",
               "r": "中", "wh": "白", "g": "發"}
    if text in aliases:
        text = aliases[text]
    for t in (list(hand) + ([drawn] if drawn else [])):
        if t.short.lower() == text:
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


# ═══════════════════════════════════════════════════════════════
#  Board embed
# ═══════════════════════════════════════════════════════════════

def make_board_text(gs: GameState, status: str = "", open_hand: bool = False) -> str:
    """牌局資訊（普通訊息版，用 # 標題放大牌面；牌與牌之間留空隔）
    open_hand=True 時公開每家手牌（觀戰模式）。"""
    mode = "三麻" if gs.is_sanma else "四麻"
    # 寶牌指示：已翻開的顯示牌面，未翻開的顯示牌背 🀫（需槓才翻）
    total_ind = len(gs.dora_indicators)
    shown     = [str(t) for t in gs.dora_indicators[:gs.revealed_dora]]
    hidden    = ["🀫"] * (total_ind - gs.revealed_dora)
    ind_str   = " ".join(shown + hidden)

    lines = [
        f"## 🀄 {gs.round_label}　{mode}",
        f"本場 {gs.honba} ・ 立直棒 {gs.riichi_sticks} ・ 牌山 {gs.tiles_left} 張",
        f"**寶牌指示**",
        f"# {ind_str}",
    ]
    for p in gs.players:
        wind     = WIND_LABELS[p.seat]
        cur      = "▶" if p.seat == gs.current_seat else "　"
        riichi   = "【立直】" if p.riichi else ""
        bot_mark = "🤖" if p.is_bot else ""
        kita     = f"　拔北×{p.kita}" if getattr(p, "kita", 0) else ""
        melds    = "　".join(str(m) for m in p.melds) if p.melds else "無"
        # 牌河：每張牌之間留空隔
        disc     = " ".join(str(t) for t in p.discards[-18:]) if p.discards else "—"
        lines.append(f"**{cur} {wind} {bot_mark}{p.username}{riichi}　（{p.score}點）**{kita}")
        lines.append(f"副露：{melds}")
        if open_hand:
            hand_tiles = sorted(p.hand, key=lambda t: (t.suit, t.value))
            drawn = f"　＋{p.drawn_tile}" if p.drawn_tile else ""
            hstr = " ".join(str(t) for t in hand_tiles) if hand_tiles else "—"
            lines.append(f"手牌（{len(p.hand)}）：{hstr}{drawn}")
        lines.append(f"🀫 牌河（{len(p.discards)}）")
        lines.append(f"## {disc}")
    if status:
        lines.append("")
        # 用 Discord 引言區塊（每行前綴 >）顯示狀態
        lines.append("\n".join(f"> {s}" for s in status.split("\n")))
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
#  0.2 討論串渲染
# ═══════════════════════════════════════════════════════════════

def make_thread_board(gs: GameState, status: str = "") -> str:
    """公開討論串的牌桌：玩家整行放大、副露只在有時顯示、牌河放大。"""
    mode      = "三麻" if gs.is_sanma else "四麻"
    total_ind = len(gs.dora_indicators)
    shown     = [str(t) for t in gs.dora_indicators[:gs.revealed_dora]]
    hidden    = ["🀫"] * (total_ind - gs.revealed_dora)
    ind_str   = " ".join(shown + hidden)

    lines = [
        f"## 🀄 {gs.round_label}　{mode}",
        f"本場 {gs.honba} ・ 立直棒 {gs.riichi_sticks} ・ 牌山 {gs.tiles_left} 張",
        f"寶牌：# {ind_str}",
    ]
    for idx, p in enumerate(gs.players):
        lines.append("---")   # 每位玩家之間加分隔線
        wind     = WIND_LABELS[p.seat]
        cur      = "▶" if p.seat == gs.current_seat else "　"
        riichi   = "【立直】" if p.riichi else ""
        bot_mark = "🤖" if p.is_bot else ""
        kita     = f"　拔北×{p.kita}" if getattr(p, "kita", 0) else ""
        # 玩家整行放大、加引言 >、名字用「」標註
        lines.append(f"> ## {cur} {wind} {bot_mark}「{p.username}」{riichi}（{p.score}點）{kita}")
        # 副露只在有時顯示
        if p.melds:
            lines.append("副露：" + "　".join(str(m) for m in p.melds))
        disc = " ".join(str(t) for t in p.discards[-18:]) if p.discards else "—"
        lines.append(f"牌河（{len(p.discards)}）：")
        lines.append(f"## {disc}")
    if status:
        lines.append("")
        lines.append("\n".join(f"> {s}" for s in status.split("\n")))
    return "\n".join(lines)


def _last_discard_info(gs: GameState) -> str:
    """上一張被打出的牌（含打牌者）；牌單獨一行放大。"""
    if gs is not None and gs.pending_discard is not None and gs.pending_from_seat >= 0:
        who = gs.players[gs.pending_from_seat].username
        return f"🀫 上一張：{WIND_LABELS[gs.pending_from_seat]} {who} 打出\n# {gs.pending_discard}"
    return ""


def make_hand_panel(player: PlayerState, prompt: str = "", tenpai_note: str = "",
                    last_info: str = "") -> str:
    """私人討論串的個人面板：上一張、自己的手牌放大 + 提示。"""
    uni, names = player.hand_display_with_names()
    drawn = f"\n\n🎲 **剛摸到：**\n# {player.drawn_tile}" if player.drawn_tile else ""
    lines = []
    if last_info:
        lines.append(last_info)
        lines.append("")
    lines += [
        "# 你的手牌",
        f"# {uni}" if uni else "（無）",
        names,
    ]
    if drawn:
        lines.append(drawn)
    if tenpai_note:
        lines.append("")
        lines.append(tenpai_note)
    if prompt:
        lines.append("")
        lines.append(prompt)
    return "\n".join(lines)


def make_river_text(gs: GameState) -> str:
    """各家牌河（給「看牌河」按鈕的私密訊息用）。"""
    lines = ["**🀫 各家牌河**"]
    for p in gs.players:
        disc = " ".join(str(t) for t in p.discards) if p.discards else "—"
        lines.append(f"{WIND_LABELS[p.seat]} {p.username}（{len(p.discards)}）")
        lines.append(f"# {disc}")
    lines.append("")
    lines.append("＊這是當下快照；關閉後再按一次「看牌河」可看最新。")
    return "\n".join(lines)


class HandHelpButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="❓ 說明", style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_message(HELP_TEXT, ephemeral=True)


class RiverButton(discord.ui.Button):
    def __init__(self, gid: str):
        super().__init__(label="🀫 看牌河", style=discord.ButtonStyle.secondary)
        self.gid = gid

    async def callback(self, interaction: discord.Interaction):
        gs = _games.get(self.gid)
        if not gs:
            await interaction.response.send_message("❌ 牌局已結束。", ephemeral=True)
            return
        await interaction.response.send_message(make_river_text(gs), ephemeral=True)


def make_hand_view(gid: str) -> discord.ui.View:
    """手牌面板常駐按鈕：說明 + 看牌河。"""
    v = discord.ui.View(timeout=None)
    v.add_item(HandHelpButton())
    v.add_item(RiverButton(gid))
    return v


async def setup_threads(gid: str, channel: discord.TextChannel, gs: GameState) -> None:
    """建立公開遊戲討論串 + 每位真人玩家的私人討論串。"""
    public = await channel.create_thread(
        name=f"🀄 麻將 {gs.round_label}",
        type=discord.ChannelType.public_thread,
    )
    board_msg = await public.send(make_thread_board(gs, "🀄 遊戲開始，輪到莊家。"))

    private: dict[str, discord.Thread] = {}
    hand_msg: dict[str, discord.Message] = {}
    for p in gs.players:
        if p.is_bot:
            continue
        try:
            pt = await channel.create_thread(
                name=f"🀫 {p.username} 的手牌",
                type=discord.ChannelType.private_thread,
                invitable=False,
            )
            try:
                await pt.add_user(discord.Object(id=int(p.user_id)))
            except Exception:
                pass
            private[p.user_id] = pt
            hm = await pt.send(make_hand_panel(p, "（等待開始）"), view=make_hand_view(gid))
            hand_msg[p.user_id] = hm
        except Exception as e:
            print(f"[threads] 建立 {p.username} 私人討論串失敗：{e}")

    _threads[gid] = {
        "public": public, "board_msg": board_msg,
        "private": private, "hand_msg": hand_msg,
        "result_msg": None,   # 上一局和牌/流局訊息（換局時刪除）
    }
    # 討論串 → gid（讓指令能在討論串內使用）
    _thread_game[public.id] = gid
    for pt in private.values():
        _thread_game[pt.id] = gid


def _cleanup(gid: str, channel_id: str) -> None:
    gs = _games.pop(gid, None)
    if gs:
        for p in gs.players:
            _user_game.pop(p.user_id, None)
            _active_turn.pop(p.user_id, None)
            _input_queues.pop(p.user_id, None)
            _input_thread.pop(p.user_id, None)
    th = _threads.get(gid)
    if th:
        pub = th.get("public")
        if pub:
            _thread_game.pop(pub.id, None)
        for pt in th.get("private", {}).values():
            _thread_game.pop(pt.id, None)
    _waiting.pop(gid, None)
    _room_owners.pop(gid, None)
    _room_configs.pop(gid, None)
    _pending_reactions.pop(gid, None)
    _game_tasks.pop(gid, None)
    _lobbies.pop(gid, None)
    _threads.pop(gid, None)
    if _channel_games.get(channel_id) == gid:
        del _channel_games[channel_id]


async def _delete_announce(th: dict) -> None:
    """刪除主頻道的開局公告訊息。"""
    ann = th.get("announce")
    if ann is not None:
        try:
            await ann.delete()
        except Exception:
            pass


async def _archive_threads(th: dict) -> None:
    """對局結束後封存討論串，並刪除開局公告。"""
    await _delete_announce(th)
    threads = [th.get("public")] + list(th.get("private", {}).values())
    for t in threads:
        if t is None:
            continue
        try:
            await t.edit(archived=True, locked=True)
        except Exception:
            pass


async def _delete_threads(th: dict) -> None:
    """刪除對局的所有討論串與開局公告（用於 /end 強制結束）。"""
    await _delete_announce(th)
    threads = [th.get("public")] + list(th.get("private", {}).values())
    for t in threads:
        if t is None:
            continue
        try:
            await t.delete()
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════
#  HandButton  (任何玩家點擊 → 私密顯示自己的手牌)
# ═══════════════════════════════════════════════════════════════

class HandButton(discord.ui.Button):
    def __init__(self, gid: str):
        super().__init__(label="看我的牌", style=discord.ButtonStyle.secondary)
        self.gid = gid

    async def callback(self, interaction: discord.Interaction):
        gs = _games.get(self.gid)
        if not gs:
            await interaction.response.send_message("❌ 牌局已結束。", ephemeral=True)
            return
        p = next((x for x in gs.players if x.user_id == str(interaction.user.id)), None)
        if not p:
            await interaction.response.send_message("❌ 你不在這局牌中。", ephemeral=True)
            return
        uni, names = p.hand_display_with_names()  # uni 已含空隔
        # 剛摸到的牌單獨放大（文字小、牌大）
        drawn = f"\n\n🎲 **剛摸到：**\n# {p.drawn_tile}" if p.drawn_tile else ""
        msg = (
            f"**你的手牌**\n"
            f"# {uni}\n"
            f"{names}{drawn}\n\n"
            f"丟完牌時，請按【刪除這則訊息】\n"
            f"下次摸牌時才會顯示正確的"
        )
        await interaction.response.send_message(msg, ephemeral=True)


class FairnessButton(discord.ui.Button):
    """公平性驗證（麻雀一番街公開方案）。
    對局中：只公開該局牌山的 SHA-256（承諾值）。
    對局結束：公開完整牌山編碼，玩家可自行用任意 SHA-256 工具校驗。
    """
    def __init__(self, gid: str):
        super().__init__(label="🔐 公平性驗證", style=discord.ButtonStyle.secondary)
        self.gid = gid

    async def callback(self, interaction: discord.Interaction):
        gs = _games.get(self.gid)
        if gs:
            # 對局進行中：只給 SHA-256，避免提前洩漏後面的牌
            embed = discord.Embed(title="🔐 牌山公平性驗證", color=0x00AA88)
            embed.add_field(
                name="本局 SHA-256（承諾值）",
                value=f"```\n{gs.wall_sha256}\n```",
                inline=False,
            )
            embed.add_field(
                name="說明",
                value=(
                    "・每局配牌的牌山都以密碼學安全亂數洗牌，並產生對應的 SHA-256 碼。\n"
                    "・對局**進行中**僅公開此 SHA-256，以免提前看到後面的牌。\n"
                    "・對局**結束後**會公開完整牌山編碼，你可把編碼貼到任意 SHA-256 "
                    "網站計算，與此碼比對，相同即代表牌山全程未被竄改。"
                ),
                inline=False,
            )
            embed.add_field(
                name="牌山編碼規則",
                value=(
                    "萬子 1~9m（紅5萬=0m）、筒子 1~9p（紅5筒=0p）、條子 1~9s（紅5條=0s）、"
                    "字牌 1~7z＝東南西北白發中。\n"
                    "編碼依牌山順序由前到後排列，例如 `1m2p3s7z`。"
                ),
                inline=False,
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
        else:
            # 牌局已從記憶體清除（已結束）：嘗試從資料庫取出牌譜的牌山編碼
            game = db.get_game(self.gid)
            if not game or not game.get("game_data"):
                await interaction.response.send_message("❌ 找不到該局牌譜。", ephemeral=True)
                return
            data     = game["game_data"]
            encoding = data.get("wall_seed", "")
            sha      = data.get("wall_sha256", "")
            embed = discord.Embed(title="🔐 牌山公平性驗證（對局已結束）", color=0x00AA88)
            embed.add_field(name="SHA-256", value=f"```\n{sha}\n```", inline=False)
            embed.add_field(
                name="完整牌山編碼（可自行校驗）",
                value=f"```\n{encoding}\n```",
                inline=False,
            )
            embed.add_field(
                name="如何校驗",
                value="把上面的牌山編碼整串貼到任意 SHA-256 計算網站，得到的結果與 SHA-256 相同即代表未竄改。",
                inline=False,
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)


HELP_TEXT = (
    "**🀄 操作說明（直接在你的私人討論串打字）**\n"
    "輪到你時，直接在自己的私人討論串輸入下列指令（打完 bot 會自動刪字）：\n\n"
    "**出牌**\n"
    "・數字牌：`1m`~`9m`（萬）、`1p`~`9p`（筒）、`1s`~`9s`（索），大小寫皆可\n"
    "・紅寶牌：`0m` / `0p` / `0s`\n"
    "・字牌：`東/南/西/北`（或 `e/s/w/n`）、`中`(=`r`)、`白`(=`wh`)、`發`(=`g`)\n\n"
    "**特殊行動**\n"
    "・自摸：`tsumo`\n"
    "・立直：`立直 5m`（指定要打出的牌）\n"
    "・暗槓：`暗槓 5m`\n"
    "・拔北（三麻）：`!n`\n\n"
    "**別人打牌時可回應**\n"
    "・`ron` 榮和、`pon` 碰、`chi 1` 吃（選編號）、`kan` 槓、`skip` 跳過"
)


class HelpButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="❓ 說明", style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_message(HELP_TEXT, ephemeral=True)


def make_base_view(gid: str) -> discord.ui.View:
    """非自己回合時掛在主訊息上的視圖：看我的牌 + 說明 + 公平性驗證。"""
    v = discord.ui.View(timeout=None)
    v.add_item(HandButton(gid))
    v.add_item(HelpButton())
    v.add_item(FairnessButton(gid))
    return v


# ═══════════════════════════════════════════════════════════════
#  TurnView
# ═══════════════════════════════════════════════════════════════

class TurnView(discord.ui.View):
    def __init__(
        self,
        gid:        str,
        player_id:  str,
        hand:       list[Tile],
        drawn_tile: Optional[Tile],
        can_tsumo:  bool,
        can_riichi: bool,
        ankan_opts: list[Tile],
        timeout:    float,
        kakan_opts: list[Tile] = None,
        kita_ok:    bool = False,
    ):
        super().__init__(timeout=timeout)
        self.player_id   = player_id
        self.hand        = hand
        self.drawn_tile  = drawn_tile
        self.action:     Optional[str]  = None
        self.discard_tile: Optional[Tile] = None
        self.ankan_tile:   Optional[Tile] = None
        self.kakan_tile:   Optional[Tile] = None
        self._kita_ok    = kita_ok
        self.timed_out   = False
        self.inputting   = False   # 正在輸入框打字時為 True，倒數暫停
        self._done       = asyncio.Event()

        # 任何人都能看自己的牌 / 說明 / 驗證公平性
        self.add_item(HandButton(gid))
        self.add_item(HelpButton())
        self.add_item(FairnessButton(gid))

        btn = discord.ui.Button(label="出牌", style=discord.ButtonStyle.primary)
        btn.callback = self._cb_discard
        self.add_item(btn)

        # 立直／自摸 不放公開按鈕（會洩漏聽牌）→ 改用 /riichi、/tsumo 指令，私訊告知

        if ankan_opts:
            ab = discord.ui.Button(label="暗杠", style=discord.ButtonStyle.secondary)
            ab.callback = self._cb_ankan
            self.add_item(ab)

        if kakan_opts:
            self._kakan_opts = kakan_opts
            kb = discord.ui.Button(label="加杠", style=discord.ButtonStyle.secondary)
            kb.callback = self._cb_kakan
            self.add_item(kb)

        # 拔北不放公開按鈕 → 改用 /kita 指令，私訊告知

    async def _check(self, interaction: discord.Interaction) -> bool:
        if str(interaction.user.id) != self.player_id:
            await interaction.response.send_message("❌ 不是你的回合！", ephemeral=True)
            return False
        return True

    async def _cb_discard(self, interaction: discord.Interaction):
        if not await self._check(interaction):
            return
        modal = DiscardInputModal(self.hand, self.drawn_tile, kita_ok=self._kita_ok)
        await interaction.response.send_modal(modal)
        self.inputting = True   # 暫停倒數
        try:
            await asyncio.wait_for(modal.submit_event.wait(), timeout=120)
        except asyncio.TimeoutError:
            pass
        if getattr(modal, "kita_requested", False):
            self.action = "kita"
            self._done.set(); self.stop()
        elif modal.selected_tile:
            self.action = "discard"
            self.discard_tile = modal.selected_tile
            self._done.set(); self.stop()
        else:
            self.inputting = False   # 沒送出（取消/逾時）→ 恢復倒數

    async def _cb_riichi(self, interaction: discord.Interaction):
        if not await self._check(interaction):
            return
        modal = DiscardInputModal(self.hand, self.drawn_tile)
        await interaction.response.send_modal(modal)
        self.inputting = True   # 暫停倒數
        try:
            await asyncio.wait_for(modal.submit_event.wait(), timeout=120)
        except asyncio.TimeoutError:
            pass
        if modal.selected_tile:
            self._set_riichi(modal.selected_tile)
            self._done.set(); self.stop()
        else:
            self.inputting = False   # 沒送出 → 恢復倒數

    def _set_riichi(self, tile: Tile):
        # Validate tenpai after discard
        test = list(self.hand)
        if tile == self.drawn_tile:
            pass
        elif tile in test:
            test.remove(tile)
        remaining = test + ([self.drawn_tile] if self.drawn_tile and tile != self.drawn_tile else [])
        if is_tenpai(remaining):
            self.action = "riichi"
        else:
            self.action = "discard"
        self.discard_tile = tile

    async def _cb_tsumo(self, interaction: discord.Interaction):
        if not await self._check(interaction):
            return
        self.action = "tsumo"
        await interaction.response.defer()
        self._done.set(); self.stop()

    async def _cb_ankan(self, interaction: discord.Interaction):
        if not await self._check(interaction):
            return
        self.action = "ankan"
        await interaction.response.defer()
        self._done.set(); self.stop()

    async def _cb_kakan(self, interaction: discord.Interaction):
        if not await self._check(interaction):
            return
        self.action = "kakan"
        self.kakan_tile = self._kakan_opts[0]
        await interaction.response.defer()
        self._done.set(); self.stop()

    async def _cb_kita(self, interaction: discord.Interaction):
        if not await self._check(interaction):
            return
        self.action = "kita"
        await interaction.response.defer()
        self._done.set(); self.stop()

    async def on_timeout(self):
        self.timed_out = True
        self._done.set()


# ═══════════════════════════════════════════════════════════════
#  Reaction collection
# ═══════════════════════════════════════════════════════════════

async def collect_reactions(
    gs:           GameState,
    discard_tile: Tile,
    from_seat:    int,
    thinking_time: int,
    furiten_perm: dict = None,
    furiten_temp: dict = None,
) -> Optional[tuple[str, str, any]]:
    gid       = gs.game_id
    next_seat = (from_seat + 1) % len(gs.players)
    bucket:   dict[str, dict] = {}
    furiten_perm = furiten_perm or {}
    furiten_temp = furiten_temp or {}

    for p in gs.players:
        if p.seat == from_seat:
            continue
        actions:   list[str]              = []
        chi_opts:  list[tuple[Tile, Tile]] = []
        ron_ok = not is_furiten(p, furiten_perm, furiten_temp)

        # AI reactions (instant decision)
        if p.is_bot:
            if ron_ok and ai_should_ron(gs, p, discard_tile):
                # AI will ron
                bucket[p.user_id] = {
                    "actions": ["ron"], "choice": "ron", "extra": None,
                    "chi_options": [], "event": asyncio.Event(), "seat": p.seat,
                }
                bucket[p.user_id]["event"].set()
            elif ai_should_pon(p.hand, discard_tile):
                bucket[p.user_id] = {
                    "actions": ["pon"], "choice": "pon", "extra": None,
                    "chi_options": [], "event": asyncio.Event(), "seat": p.seat,
                }
                bucket[p.user_id]["event"].set()
            # AI doesn't chi or kan for simplicity
            continue

        # Human reactions（榮和必須有役且非振聽）
        if ron_ok and evaluate_win(gs, p, discard_tile, is_tsumo=False) is not None:
            actions.append("ron")
        if count_tiles(p.hand, discard_tile) >= 2:
            actions.append("pon")
        if count_tiles(p.hand, discard_tile) >= 3:
            actions.append("kan")
        if p.seat == next_seat and not gs.is_sanma:   # 三麻不能吃
            chi_opts = get_chi_options(p.hand, discard_tile)
            if chi_opts:
                actions.append("chi")

        if not actions:
            continue

        bucket[p.user_id] = {
            "actions":     actions,
            "choice":      None,
            "extra":       None,
            "chi_options": chi_opts,
            "event":       asyncio.Event(),
            "seat":        p.seat,
        }

    if not bucket:
        return None

    _pending_reactions[gid] = bucket

    # DM human players
    from_name = gs.players[from_seat].username
    for uid, info in bucket.items():
        p = next((x for x in gs.players if x.user_id == uid), None)
        if not p or p.is_bot:
            continue
        user = bot.get_user(int(uid))
        if not user:
            try:
                user = await bot.fetch_user(int(uid))
            except Exception:
                pass
        if not user:
            info["event"].set()
            continue

        actions_str = " / ".join(f"`/{a}`" for a in info["actions"])
        chi_text = ""
        for i, (t1, t2) in enumerate(info["chi_options"], 1):
            srt = sorted([t1, t2, discard_tile], key=lambda t: (t.suit, t.value))
            tiles_uni = " ".join(str(t) for t in srt)
            chi_text += f"\n選項{i}（`/chi {i}`）：\n# {tiles_uni}"

        try:
            await user.send(
                f"🀄 **{from_name}** 打出了 **{discard_tile}**！\n"
                f"你可以：{actions_str}\n"
                f"或使用 `/skip` 跳過{chi_text}\n"
                f"（{thinking_time} 秒後自動跳過）"
            )
        except discord.Forbidden:
            info["event"].set()

    # Wait
    events = [info["event"] for info in bucket.values()]
    try:
        await asyncio.wait_for(asyncio.gather(*[e.wait() for e in events]), timeout=float(thinking_time))
    except asyncio.TimeoutError:
        pass

    _pending_reactions.pop(gid, None)

    priority = {"ron": 0, "pon": 1, "kan": 1, "chi": 2}
    best: Optional[tuple[int, str, str, any]] = None
    for uid, info in bucket.items():
        if info["choice"]:
            pr = priority.get(info["choice"], 99)
            if best is None or pr < best[0]:
                best = (pr, uid, info["choice"], info["extra"])

    return (best[2], best[1], best[3]) if best else None


async def collect_chankan(
    gs: GameState, kan_tile: Tile, kan_seat: int, thinking_time: int,
    furiten_perm: dict, temp_furiten: dict,
) -> Optional[str]:
    """搶槓（槍槓）窗口：其他玩家若可榮和此加槓牌則可搶。回傳搶槓者 user_id 或 None。"""
    gid = gs.game_id
    bucket: dict[str, dict] = {}
    for p in gs.players:
        if p.seat == kan_seat:
            continue
        if is_furiten(p, furiten_perm, temp_furiten):
            continue
        if evaluate_win(gs, p, kan_tile, is_tsumo=False, is_chankan=True) is None:
            continue
        info = {"actions": ["ron"], "choice": None, "extra": None,
                "chi_options": [], "event": asyncio.Event(), "seat": p.seat}
        if p.is_bot:
            info["choice"] = "ron"
            info["event"].set()
        bucket[p.user_id] = info

    if not bucket:
        return None

    _pending_reactions[gid] = bucket
    kan_name = gs.players[kan_seat].username
    for uid, info in bucket.items():
        p = next((x for x in gs.players if x.user_id == uid), None)
        if not p or p.is_bot:
            continue
        user = bot.get_user(int(uid))
        if not user:
            try:
                user = await bot.fetch_user(int(uid))
            except Exception:
                pass
        if not user:
            info["event"].set()
            continue
        try:
            await user.send(
                f"🀄 **{kan_name}** 加槓 **{kan_tile}**！\n"
                f"你可以 `/ron` **搶槓**，或 `/skip` 跳過（{thinking_time} 秒）"
            )
        except discord.Forbidden:
            info["event"].set()

    events = [info["event"] for info in bucket.values()]
    try:
        await asyncio.wait_for(asyncio.gather(*[e.wait() for e in events]), timeout=float(thinking_time))
    except asyncio.TimeoutError:
        pass
    _pending_reactions.pop(gid, None)

    for uid, info in bucket.items():
        if info["choice"] == "ron":
            return uid
    return None


# ═══════════════════════════════════════════════════════════════
#  0.2 討論串：打字輸入解析 + 反應收集 + 對局迴圈
# ═══════════════════════════════════════════════════════════════

def _parse_turn_input(raw, player, can_tsumo, can_riichi, kita_ok, ankan_opts):
    """解析玩家輪到時打的字 → (ok, (action, arg), err)。"""
    s = raw.strip()
    low = s.lower()
    if low in ("tsumo", "自摸", "zimo"):
        return (True, ("tsumo", None), "") if can_tsumo else (False, None, "現在無法自摸")
    if low in ("!n", "n!", "拔北", "kita"):
        return (True, ("kita", None), "") if kita_ok else (False, None, "現在無法拔北")
    if low.startswith(("riichi", "reach")) or s.startswith("立直"):
        rest = s
        for kw in ("riichi", "reach", "立直", "RIICHI", "Reach"):
            rest = rest.replace(kw, "")
        rest = rest.strip()
        if not can_riichi:
            return (False, None, "現在無法立直")
        t = parse_tile(rest, player.hand, player.drawn_tile)
        if not t:
            return (False, None, "立直需指定要打的牌，例如：立直 5m")
        test = list(player.hand)
        dt = player.drawn_tile
        if t == dt:
            dt = None
        elif t in test:
            test.remove(t)
        if not is_tenpai(test + ([dt] if dt else [])):
            return (False, None, "打出該牌後未聽牌，不能立直")
        return (True, ("riichi", t), "")
    if s.startswith(("暗槓", "暗杠")) or low.startswith("ankan"):
        rest = s
        for kw in ("暗槓", "暗杠", "ankan", "ANKAN"):
            rest = rest.replace(kw, "")
        rest = rest.strip()
        t = parse_tile(rest, player.hand, player.drawn_tile) if rest else (ankan_opts[0] if ankan_opts else None)
        if t and any(o.suit == t.suit and o.value == t.value for o in ankan_opts):
            return (True, ("ankan", t), "")
        return (False, None, "無法暗槓該牌")
    t = parse_tile(s, player.hand, player.drawn_tile)
    if t:
        return (True, ("discard", t), "")
    return (False, None, f"看不懂「{raw}」，請打出要丟的牌（如 5m、東、中）")


async def collect_reactions_t(gs, gid, discard_tile, from_seat, thinking_time,
                              furiten_perm, furiten_temp):
    """討論串版反應收集：在各玩家私人討論串貼提示、等打字。回傳 (choice, uid, extra) 或 None。"""
    th        = _threads.get(gid, {})
    private   = th.get("private", {})
    next_seat = (from_seat + 1) % len(gs.players)
    from_name = gs.players[from_seat].username

    results = []   # (priority, seat, choice, extra)
    candidates = []
    for p in gs.players:
        if p.seat == from_seat:
            continue
        if p.is_bot:
            if ai_should_ron(gs, p, discard_tile, furiten_perm, furiten_temp):
                results.append((0, p.seat, "ron", None))
            elif ai_should_pon(p.hand, discard_tile):
                results.append((1, p.seat, "pon", None))
            continue
        ron_ok = (not is_furiten(p, furiten_perm, furiten_temp)) and \
                 evaluate_win(gs, p, discard_tile, is_tsumo=False) is not None
        actions, chi_opts = [], []
        if ron_ok:
            actions.append("ron")
        if count_tiles(p.hand, discard_tile) >= 2:
            actions.append("pon")
        if count_tiles(p.hand, discard_tile) >= 3:
            actions.append("kan")
        if p.seat == next_seat and not gs.is_sanma:
            chi_opts = get_chi_options(p.hand, discard_tile)
            if chi_opts:
                actions.append("chi")
        if actions:
            candidates.append((p, actions, chi_opts))

    async def ask(p, actions, chi_opts):
        pt = private.get(p.user_id)
        if not pt:
            return None
        uid = p.user_id
        fut = asyncio.get_event_loop().create_future()
        view = discord.ui.View(timeout=thinking_time + 5)

        def add_btn(label, style, choice, extra):
            b = discord.ui.Button(label=label, style=style)
            async def cb(inter):
                if str(inter.user.id) != uid:
                    await inter.response.send_message("❌ 不是你的回應", ephemeral=True)
                    return
                await inter.response.defer()
                if not fut.done():
                    fut.set_result((choice, extra))
            b.callback = cb
            view.add_item(b)

        if "ron" in actions:
            add_btn("🀄 榮和", discord.ButtonStyle.success, "ron", None)
        if "pon" in actions:
            add_btn("碰", discord.ButtonStyle.primary, "pon", None)
        if "kan" in actions:
            add_btn("槓", discord.ButtonStyle.primary, "kan", None)
        if "chi" in actions:
            for t1, t2 in chi_opts:
                srt = sorted([t1, t2, discard_tile], key=lambda t: (t.suit, t.value))
                add_btn("吃 " + "".join(str(t) for t in srt), discord.ButtonStyle.secondary, "chi", (t1, t2))
        add_btn("跳過", discord.ButtonStyle.danger, "skip", None)

        try:
            prompt_msg = await pt.send(
                f"🀄 **{from_name}** 打出 **{discard_tile}**！請選擇（{thinking_time} 秒）",
                view=view,
            )
        except Exception:
            return None
        try:
            res = await asyncio.wait_for(fut, timeout=thinking_time)
        except asyncio.TimeoutError:
            res = ("skip", None)
        try:
            await prompt_msg.delete()
        except Exception:
            pass
        choice, extra = res
        if choice == "skip":
            return None
        pr = {"ron": 0, "pon": 1, "kan": 1, "chi": 2}.get(choice, 9)
        return (pr, p.seat, choice, extra)

    if candidates:
        human = await asyncio.gather(*[ask(p, a, c) for p, a, c in candidates])
        results.extend(r for r in human if r is not None)

    if not results:
        return None
    results.sort(key=lambda x: x[0])
    _, seat, choice, extra = results[0]
    return (choice, gs.players[seat].user_id, extra)


async def _warn(thread, text: str) -> None:
    try:
        w = await thread.send(f"❌ {text}")
        asyncio.create_task(_delete_later(w, 4))
    except Exception:
        pass


async def _ping_turn(thread, uid: str) -> None:
    """在玩家私人討論串 @ 一下提醒輪到他，隨即刪除（通知已送出）。"""
    try:
        m = await thread.send(f"<@{uid}> 輪到你了！")
        await asyncio.sleep(0.6)
        await m.delete()
    except Exception:
        pass


async def wait_turn_action(gid, player, pt, hand_msg, thinking_time,
                           can_tsumo, can_riichi, kita_ok, ankan_opts,
                           prompt_base, tenpai_note, last_info=""):
    """輪到玩家：自摸/立直/暗槓/拔北用按鈕，出牌用打字。回傳 (action, arg) 或 None（逾時）。"""
    uid   = player.user_id
    fut   = asyncio.get_event_loop().create_future()
    state = {"riichi": False}
    view  = discord.ui.View(timeout=float(thinking_time) + 10)
    # 常駐按鈕：說明 + 看牌河
    view.add_item(HandHelpButton())
    view.add_item(RiverButton(gid))

    def panel(rem):
        extra = "🀄 已宣告立直，請**打字**打出宣言牌" if state["riichi"] else f"{prompt_base}　（剩 {rem} 秒）"
        return make_hand_panel(player, extra, tenpai_note, last_info)

    async def refresh(rem):
        try:
            await hand_msg.edit(content=panel(rem), view=view)
        except Exception:
            pass

    def add_btn(label, style, kind):
        b = discord.ui.Button(label=label, style=style)
        async def cb(inter):
            if str(inter.user.id) != uid:
                await inter.response.send_message("❌ 不是你的回合", ephemeral=True)
                return
            await inter.response.defer()
            if kind == "riichi":
                state["riichi"] = True
                await refresh(0)
            elif not fut.done():
                fut.set_result(kind)
        b.callback = cb
        view.add_item(b)

    if can_tsumo:
        add_btn("自摸", discord.ButtonStyle.success, ("tsumo", None))
    if can_riichi:
        add_btn("立直", discord.ButtonStyle.success, "riichi")
    if ankan_opts:
        add_btn("暗槓", discord.ButtonStyle.secondary, ("ankan", ankan_opts[0]))
    if kita_ok:
        add_btn("拔北", discord.ButtonStyle.secondary, ("kita", None))

    await refresh(int(thinking_time))

    q = asyncio.Queue()
    _input_queues[uid] = q
    _input_thread[uid] = pt.id

    async def countdown():
        rem = int(thinking_time)
        while rem > 0:
            await asyncio.sleep(1)
            rem -= 1
            if not state["riichi"]:
                await refresh(rem)

    async def reader():
        while not fut.done():
            raw, msg = await q.get()
            try:
                await msg.delete()
            except Exception:
                pass
            if state["riichi"]:
                t = parse_tile(raw, player.hand, player.drawn_tile)
                if not t:
                    await _warn(pt, f"看不懂「{raw}」")
                    continue
                test = list(player.hand)
                dt = player.drawn_tile
                if t == dt:
                    dt = None
                elif t in test:
                    test.remove(t)
                if is_tenpai(test + ([dt] if dt else [])):
                    if not fut.done():
                        fut.set_result(("riichi", t))
                    return
                await _warn(pt, "打出該牌後未聽牌，不能立直")
            else:
                ok, val, err = _parse_turn_input(raw, player, can_tsumo, can_riichi, kita_ok, ankan_opts)
                if ok:
                    if not fut.done():
                        fut.set_result(val)
                    return
                await _warn(pt, err)

    cd = asyncio.create_task(countdown())
    rd = asyncio.create_task(reader())
    try:
        result = await asyncio.wait_for(fut, timeout=float(thinking_time))
    except asyncio.TimeoutError:
        result = None
    finally:
        cd.cancel()
        rd.cancel()
        _input_queues.pop(uid, None)
        _input_thread.pop(uid, None)
        try:
            await hand_msg.edit(view=make_hand_view(gid))   # 回合結束保留說明/看牌河
        except Exception:
            pass
    return result


async def play_hand_t(gid: str, channel: discord.TextChannel):
    """0.2：以討論串 + 打字輸入進行「一局」。回傳同 play_hand。"""
    gs            = _games[gid]
    config        = _room_configs.get(gid, {})
    thinking_time = config.get("thinking_time", 25)
    th            = _threads[gid]
    board_msg     = th["board_msg"]
    private       = th["private"]
    hand_msg      = th["hand_msg"]

    async def render_board(status=""):
        try:
            await board_msg.edit(content=make_thread_board(gs, status))
        except Exception:
            pass

    async def render_hand(p, prompt="", tenpai_note=""):
        hm = hand_msg.get(p.user_id)
        if hm:
            try:
                await hm.edit(content=make_hand_panel(p, prompt, tenpai_note, _last_discard_info(gs)),
                              view=make_hand_view(gid))
            except Exception:
                pass

    furiten_perm = {p.seat: False for p in gs.players}
    temp_furiten = {p.seat: False for p in gs.players}
    ippatsu      = {p.seat: False for p in gs.players}
    double_rii   = {p.seat: False for p in gs.players}
    any_call     = False
    rinshan_next = False
    no_draw      = False

    while True:
        if _games.get(gid) is not gs:
            return None
        player = gs.players[gs.current_seat]
        is_rinshan_draw = False
        temp_furiten[player.seat] = False

        if no_draw:
            no_draw = False
            player.drawn_tile = None
        else:
            tile = gs.draw_tile()
            if tile is None:
                return ("draw", [p.seat for p in gs.players if is_tenpai(p.hand)])
            player.drawn_tile = tile
            is_rinshan_draw = rinshan_next
            rinshan_next = False
        ipp_start = ippatsu[player.seat]

        # ── AI ──────────────────────────────────────────────
        if player.is_bot:
            res = evaluate_win(
                gs, player, player.drawn_tile, is_tsumo=True, is_rinshan=is_rinshan_draw,
                is_tenhou=(player.is_dealer and not player.discards and not any_call),
                is_chiihou=((not player.is_dealer) and not player.discards and not any_call),
            ) if player.drawn_tile else None
            if res:
                hs = format_winning_hand(player, player.drawn_tile)
                await render_board(f"🎉 🤖 {player.username} 自摸！")
                return ("tsumo", player.seat, res, hs)
            if gs.is_sanma and has_kita(player):
                if player.drawn_tile is not None:
                    player.hand.append(player.drawn_tile); player.drawn_tile = None
                for i, t in enumerate(player.hand):
                    if t.suit == Suit.WIND and t.value == 4:
                        player.hand.pop(i); break
                player.kita += 1
                await render_board(f"🤖 {player.username} 拔北 ×{player.kita}")
                await asyncio.sleep(0.6)
                continue
            drawn = player.drawn_tile
            if player.drawn_tile is not None:
                player.hand.append(player.drawn_tile); player.drawn_tile = None
            discard_tile = ai_choose_discard(player.hand)
            if discard_tile is None:
                return ("draw", [p.seat for p in gs.players if is_tenpai(p.hand)])
            player.hand.remove(discard_tile)
            player.discards.append(discard_tile)
            giri = "摸切" if (drawn is not None and discard_tile == drawn) else "手切"
            nxt = gs.players[(player.seat + 1) % len(gs.players)].username
            await render_board(f"🤖 {player.username} 出牌了（{giri}），輪到 {nxt}")
            await asyncio.sleep(1.0)

        # ── Human（打字）────────────────────────────────────
        else:
            can_tsumo  = bool(player.drawn_tile) and evaluate_win(
                gs, player, player.drawn_tile, is_tsumo=True, is_rinshan=is_rinshan_draw) is not None
            can_riichi = (not player.riichi) and len(player.melds) == 0 and is_tenpai(player.hand)
            ankan_opts = get_ankan_options(player.hand + ([player.drawn_tile] if player.drawn_tile else []))
            kita_ok    = gs.is_sanma and has_kita(player)

            tenpai_note = ""
            if is_tenpai(player.hand):
                waits = get_tenpai_waits(player.hand)
                tenpai_note = f"🀄 **聽牌！** 待牌：{' '.join(str(t) for t in waits)}"

            prompt_base = "✍️ **打字**丟牌（如 `5m` `東`）；自摸／立直／暗槓／拔北用下方按鈕"

            await render_board(f"輪到 <@{player.user_id}>（{WIND_LABELS[player.seat]}）出牌")

            pt = private.get(player.user_id)
            hm = hand_msg.get(player.user_id)
            if pt:
                asyncio.create_task(_ping_turn(pt, player.user_id))   # @ 提醒（隨即刪）
            result = None
            if pt and hm:
                result = await wait_turn_action(
                    gid, player, pt, hm, thinking_time,
                    can_tsumo, can_riichi, kita_ok, ankan_opts,
                    prompt_base, tenpai_note, _last_discard_info(gs),
                )
            else:
                await render_hand(player, prompt_base, tenpai_note)

            timed = result is None
            action, arg = ("discard", None) if timed else result

            # ── Tsumo ──
            if action == "tsumo":
                res = evaluate_win(
                    gs, player, player.drawn_tile, is_tsumo=True, is_rinshan=is_rinshan_draw,
                    is_ippatsu=ippatsu[player.seat], is_double_riichi=double_rii[player.seat],
                    is_tenhou=(player.is_dealer and not player.discards and not any_call),
                    is_chiihou=((not player.is_dealer) and not player.discards and not any_call),
                )
                if res:
                    hs = format_winning_hand(player, player.drawn_tile)
                    await render_board(f"🎉 {player.username} 自摸！")
                    return ("tsumo", player.seat, res, hs)
                action = "discard"  # 萬一無效退回出牌

            # ── 拔北 ──
            if action == "kita":
                if player.drawn_tile is not None:
                    player.hand.append(player.drawn_tile); player.drawn_tile = None
                for i, t in enumerate(player.hand):
                    if t.suit == Suit.WIND and t.value == 4:
                        player.hand.pop(i); break
                player.kita += 1
                await render_hand(player)
                await render_board(f"{player.username} 拔北 ×{player.kita}")
                continue

            # ── 暗槓 ──
            if action == "ankan" and ankan_opts:
                kan_tile = arg
                player.hand = [t for t in player.hand
                               if not (t.suit == kan_tile.suit and t.value == kan_tile.value)]
                if player.drawn_tile and player.drawn_tile.suit == kan_tile.suit and player.drawn_tile.value == kan_tile.value:
                    player.drawn_tile = None
                player.melds.append(Meld(MeldType.ANKAN, [Tile(kan_tile.suit, kan_tile.value)] * 4, -1))
                gs.open_dora()
                any_call = True
                for s in ippatsu:
                    ippatsu[s] = False
                rinshan_next = True
                await render_hand(player)
                await render_board(f"{player.username} 暗槓 {kan_tile}")
                continue

            # ── 出牌 / 立直 ──
            if timed or arg is None:
                discard_tile = player.drawn_tile if player.drawn_tile else player.hand[-1]
            else:
                discard_tile = arg
            drawn = player.drawn_tile
            giri = "摸切" if (drawn is not None and discard_tile == drawn) else "手切"

            if action == "riichi" and not player.riichi:
                player.riichi = True
                gs.riichi_sticks += 1
                player.score -= 1000
                double_rii[player.seat] = (not any_call) and (len(player.discards) == 0)
                ippatsu[player.seat] = True
                word = "宣告立直！"
            elif timed:
                word = "超時自動打出"
            else:
                word = "出牌了"

            if player.drawn_tile is not None:
                player.hand.append(player.drawn_tile); player.drawn_tile = None
            if discard_tile in player.hand:
                player.hand.remove(discard_tile)
            player.discards.append(discard_tile)
            if ipp_start:
                ippatsu[player.seat] = False

            post_note = ""
            if is_tenpai(player.hand) and not player.riichi:
                waits = get_tenpai_waits(player.hand)
                post_note = f"🀄 **聽牌！** 待牌：{' '.join(str(t) for t in waits)}"
            await render_hand(player, "", post_note)

            nxt = gs.players[(player.seat + 1) % len(gs.players)].username
            await render_board(f"{player.username} {word}（{giri}），輪到 {nxt}")

        gs.pending_discard   = discard_tile
        gs.pending_from_seat = player.seat

        # ── 反應（打字）──
        reaction = await collect_reactions_t(gs, gid, discard_tile, player.seat, thinking_time,
                                             furiten_perm, temp_furiten)
        wkey = (int(discard_tile.suit), discard_tile.value)
        for p in gs.players:
            if p.seat != player.seat and wkey in hand_waits(p):
                temp_furiten[p.seat] = True
                if p.riichi:
                    furiten_perm[p.seat] = True

        if reaction:
            rtype, r_uid, extra = reaction
            rp = next((p for p in gs.players if p.user_id == r_uid), None)
            from_name = player.username
            if rtype == "ron" and rp:
                res = evaluate_win(gs, rp, discard_tile, is_tsumo=False,
                                   is_ippatsu=ippatsu[rp.seat], is_double_riichi=double_rii[rp.seat])
                if res:
                    hs = format_winning_hand(rp, discard_tile)
                    await render_board(f"🎉 {rp.username} 榮和 {from_name} 的 {discard_tile}！")
                    return ("ron", rp.seat, player.seat, res, hs)
                await render_board(f"❌ {rp.username} 榮和無效")
            elif rtype == "pon" and rp:
                removed, new_hand = 0, []
                for t in rp.hand:
                    if t.suit == discard_tile.suit and t.value == discard_tile.value and removed < 2:
                        removed += 1
                    else:
                        new_hand.append(t)
                rp.hand = new_hand
                rp.melds.append(Meld(MeldType.PON, [Tile(discard_tile.suit, discard_tile.value)] * 3, player.seat))
                if player.discards and player.discards[-1] == discard_tile:
                    player.discards.pop()   # 被碰走 → 從牌河移除
                gs.current_seat = rp.seat
                no_draw = True
                any_call = True
                for s in ippatsu:
                    ippatsu[s] = False
                await render_hand(rp)
                await render_board(f"{rp.username} 碰了 {from_name} 的 {discard_tile}！請出牌。")
                continue
            elif rtype == "chi" and rp and extra:
                t1, t2 = extra
                rp.hand.remove(t1); rp.hand.remove(t2)
                meld_tiles = sorted([t1, t2, discard_tile], key=lambda t: (t.suit, t.value))
                rp.melds.append(Meld(MeldType.CHI, meld_tiles, player.seat))
                if player.discards and player.discards[-1] == discard_tile:
                    player.discards.pop()   # 被吃走 → 從牌河移除
                gs.current_seat = rp.seat
                no_draw = True
                any_call = True
                for s in ippatsu:
                    ippatsu[s] = False
                await render_hand(rp)
                await render_board(f"{rp.username} 吃了 {from_name} 的 {discard_tile}！請出牌。")
                continue
            elif rtype == "kan" and rp:
                removed, new_hand = 0, []
                for t in rp.hand:
                    if t.suit == discard_tile.suit and t.value == discard_tile.value and removed < 3:
                        removed += 1
                    else:
                        new_hand.append(t)
                rp.hand = new_hand
                rp.melds.append(Meld(MeldType.KAN, [Tile(discard_tile.suit, discard_tile.value)] * 4, player.seat))
                if player.discards and player.discards[-1] == discard_tile:
                    player.discards.pop()   # 被槓走 → 從牌河移除
                gs.open_dora()
                gs.current_seat = rp.seat
                any_call = True
                for s in ippatsu:
                    ippatsu[s] = False
                rinshan_next = True
                await render_hand(rp)
                await render_board(f"{rp.username} 槓了 {from_name} 的 {discard_tile}！摸嶺上牌。")
                continue

        gs.current_seat = (gs.current_seat + 1) % len(gs.players)


async def match_loop_t(gid: str, channel: discord.TextChannel) -> None:
    """0.2：討論串版多局對戰。"""
    config       = _room_configs.get(gid, {})
    length       = config.get("length", "tonpuu")
    tobi         = config.get("tobi", True)
    players_info = _waiting.get(gid, [])
    channel_id   = str(channel.id)
    th           = _threads[gid]
    public       = th["public"]

    try:
        while True:
            outcome = await play_hand_t(gid, channel)
            if outcome is None:
                return
            gs = _games[gid]

            if outcome[0] == "tsumo":
                _, wseat, result, hand_str = outcome
                log = st.apply_tsumo(gs, wseat, result)
                header = f"{gs.players[wseat].username}　自摸！"
                st.advance_after_win(gs, wseat)
            elif outcome[0] == "ron":
                _, wseat, lseat, result, hand_str = outcome
                log = st.apply_ron(gs, wseat, lseat, result)
                header = f"{gs.players[wseat].username}　榮和！（放銃：{gs.players[lseat].username}）"
                st.advance_after_win(gs, wseat)
            else:
                _, tenpai = outcome
                log = st.apply_ryuukyoku(gs, tenpai)
                result = None
                st.advance_after_draw(gs, tenpai)

            db.update_game_state(gid, "playing", gs.to_dict())

            if result is not None:
                result_msg = await win_ceremony(public, gs, header, hand_str, result, log)
            else:
                tnames = "、".join(gs.players[s].username for s in tenpai) or "無人"
                result_msg = await public.send(f"# 🀄 流局\n聽牌：{tnames}\n\n{log.describe(gs)}")
            await asyncio.sleep(6)

            if st.is_game_over(gs, length, tobi):
                break

            # 換下一局：刪掉上一局的和牌／流局訊息
            if result_msg:
                try:
                    await result_msg.delete()
                except Exception:
                    pass

            new_gs = deal_next_hand(gid, players_info, gs)
            _games[gid] = new_gs
            try:
                await th["board_msg"].edit(
                    content=make_thread_board(new_gs, f"🀄 {new_gs.round_label} 開始，輪到莊家。"))
            except Exception:
                pass
            for p in new_gs.players:
                if not p.is_bot:
                    hm = th["hand_msg"].get(p.user_id)
                    if hm:
                        try:
                            await hm.edit(content=make_hand_panel(p), view=make_hand_view(gid))
                        except Exception:
                            pass
            await asyncio.sleep(1)

        gs = _games[gid]
        rows = st.final_standings(gs, start_points=config.get("start_points"))
        medals = ["🥇", "🥈", "🥉", "4️⃣"]
        lines = ["# 🏁 最終順位", ""]
        for r in rows:
            sign = "＋" if r.total_pt >= 0 else "－"
            lines.append(f"{medals[r.rank - 1]} **第 {r.rank} 位**　{r.username}"
                         f"　{r.score} 點　｜　精算 {sign}{abs(r.total_pt):.1f}")
        db.finish_game(gid, gs.to_dict())
        end_view = discord.ui.View(timeout=None)
        end_view.add_item(FairnessButton(gid))
        await public.send("\n".join(lines), view=end_view)
        await _archive_threads(th)
    finally:
        _cleanup(gid, channel_id)


# ═══════════════════════════════════════════════════════════════
#  Game loop（0.1 舊版，保留）
# ═══════════════════════════════════════════════════════════════

async def play_hand(gid: str, channel: discord.TextChannel,
                    main_msg: discord.Message, base_view: discord.ui.View):
    """
    進行「一局」。回傳結果：
      ("tsumo", winner_seat, ScoreResult)
      ("ron", winner_seat, loser_seat, ScoreResult)
      ("draw", [tenpai_seats])
      None  → 牌局已被強制結束
    """
    gs            = _games[gid]
    config        = _room_configs.get(gid, {})
    thinking_time = config.get("thinking_time", 25)
    open_hand     = config.get("open_hand", False)
    state         = {"status": f"🀄 {gs.round_label} 開始，輪到莊家。"}

    async def render(status: Optional[str] = None, view: Optional[discord.ui.View] = None):
        if status is not None:
            state["status"] = status
        try:
            await main_msg.edit(
                content=make_board_text(gs, state["status"], open_hand),
                view=view if view is not None else base_view,
            )
        except Exception:
            pass

    no_draw = False   # 碰/吃之後輪到該玩家，直接出牌不摸牌

    # 一局內的狀態追蹤（振聽 / 一發 / 兩立直 / 天和地和 / 嶺上）
    furiten_perm = {p.seat: False for p in gs.players}   # 立直振聽（永久）
    temp_furiten = {p.seat: False for p in gs.players}   # 同巡振聽（下次摸牌解除）
    ippatsu      = {p.seat: False for p in gs.players}   # 一發資格
    double_rii   = {p.seat: False for p in gs.players}   # 兩立直
    any_call     = False                                 # 是否發生過鳴牌（破一發/天地和）
    rinshan_next = False                                 # 下一張摸牌是否為嶺上牌

    while True:
        # 若牌局已被 /end 強制結束（從 _games 移除），立即停止
        if _games.get(gid) is not gs:
            return None

        player = gs.players[gs.current_seat]
        is_rinshan_draw = False

        # ── Draw ────────────────────────────────────────────────
        temp_furiten[player.seat] = False   # 輪到自己 → 解除同巡振聽
        if no_draw:
            no_draw = False
            player.drawn_tile = None   # 碰/吃後不摸牌
        else:
            tile = gs.draw_tile()
            if tile is None:
                tenpai = [p.seat for p in gs.players if is_tenpai(p.hand)]
                return ("draw", tenpai)
            player.drawn_tile = tile
            is_rinshan_draw = rinshan_next
            rinshan_next = False

        ipp_start = ippatsu[player.seat]   # 本回合開始時的一發資格（用於回合末過期）

        # ── AI turn ─────────────────────────────────────────────
        if player.is_bot:
            res = evaluate_win(
                gs, player, player.drawn_tile, is_tsumo=True,
                is_rinshan=is_rinshan_draw,
                is_tenhou=(player.is_dealer and not player.discards and not any_call),
                is_chiihou=((not player.is_dealer) and not player.discards and not any_call),
            ) if player.drawn_tile else None
            if res:
                hs = format_winning_hand(player, player.drawn_tile)
                await render(f"🎉 🤖 {player.username} 自摸！")
                return ("tsumo", player.seat, res, hs)
            # 三麻：自動拔北（把北設為北寶牌後補摸一張）
            if gs.is_sanma and has_kita(player):
                if player.drawn_tile is not None:
                    player.hand.append(player.drawn_tile)
                    player.drawn_tile = None
                for i, t in enumerate(player.hand):
                    if t.suit == Suit.WIND and t.value == 4:
                        player.hand.pop(i)
                        break
                player.kita += 1
                await render(f"🤖 {player.username} 拔北 ×{player.kita}")
                await asyncio.sleep(0.6)
                continue   # 補摸後重新行動
            # 摸到的牌先併回手牌，再從手牌選一張打出（避免遺失摸牌）
            drawn = player.drawn_tile
            if player.drawn_tile is not None:
                player.hand.append(player.drawn_tile)
                player.drawn_tile = None
            discard_tile = ai_choose_discard(player.hand)
            if discard_tile is None:
                tenpai = [p.seat for p in gs.players if is_tenpai(p.hand)]
                return ("draw", tenpai)
            player.hand.remove(discard_tile)
            player.discards.append(discard_tile)
            giri = "摸切" if (drawn is not None and discard_tile == drawn) else "手切"
            nxt = gs.players[(player.seat + 1) % len(gs.players)].username
            await render(f"🤖 {player.username} 出牌了（{giri}），輪到 {nxt}")
            await asyncio.sleep(1.0)

        # ── Human turn ──────────────────────────────────────────
        else:
            can_tsumo  = bool(player.drawn_tile) and evaluate_win(
                gs, player, player.drawn_tile, is_tsumo=True, is_rinshan=is_rinshan_draw
            ) is not None
            can_riichi = (not player.riichi) and len(player.melds) == 0 and is_tenpai(player.hand)
            full_for_kan = player.hand + ([player.drawn_tile] if player.drawn_tile else [])
            ankan_opts = get_ankan_options(full_for_kan)
            kakan_opts = get_shouminkan_options(player)
            kita_ok    = gs.is_sanma and has_kita(player)

            # 私訊提示（不公開，避免洩漏聽牌）：待牌 + 可立直 + 可自摸
            dm_lines: list[str] = []
            if is_tenpai(player.hand):
                waits = get_tenpai_waits(player.hand)
                dm_lines.append(f"🀄 你已聽牌！待牌：{' '.join(str(t) for t in waits)}")
            if can_riichi:
                dm_lines.append("✅ 可立直：輸入 `/riichi 牌名`")
            if can_tsumo:
                dm_lines.append("🎉 可自摸：輸入 `/tsumo`")
            if kita_ok:
                dm_lines.append("🀃 可拔北：指令 `/n`（或 `/kita`），或在出牌框輸入 `!n`")
            if dm_lines:
                u = bot.get_user(int(player.user_id))
                if u:
                    try:
                        await u.send("\n".join(dm_lines))
                    except discord.Forbidden:
                        pass

            turn_view = TurnView(
                gid, player.user_id, player.hand, player.drawn_tile,
                can_tsumo, can_riichi, ankan_opts,
                timeout=600,   # 視圖本身用長逾時（避免顯示中過期使按鈕失效）；實際回合時間由倒數控制
                kakan_opts=kakan_opts,
                kita_ok=kita_ok,
            )
            _active_turn[player.user_id] = turn_view

            hint = ""   # 公開狀態不顯示可立直/可自摸（避免洩漏聽牌）

            # 即時倒數：每秒編輯主訊息更新剩餘秒數，直到玩家行動或時間到
            # 玩家開啟輸入框打字時暫停倒數，並顯示「輸入中」
            done_task = asyncio.create_task(turn_view._done.wait())
            remaining = int(thinking_time)
            while remaining > 0:
                if turn_view.inputting:
                    await render(
                        f"<@{player.user_id}> ✏️ **輸入中…**（倒數暫停）{hint}",
                        view=turn_view,
                    )
                else:
                    await render(
                        f"<@{player.user_id}> 輪到你了，請選擇行動（剩 **{remaining}** 秒）{hint}",
                        view=turn_view,
                    )
                done, _ = await asyncio.wait({done_task}, timeout=1)
                if done:
                    break
                # 只有「非輸入中」才扣秒，輸入中則暫停倒數
                if not turn_view.inputting:
                    remaining -= 1
            if not done_task.done():
                turn_view.timed_out = True
                done_task.cancel()

            turn_view.stop()   # 回合結束關閉視圖，釋放資源
            _active_turn.pop(player.user_id, None)

            # ── Tsumo ──────────────────────────────────────────
            if turn_view.action == "tsumo":
                res = evaluate_win(
                    gs, player, player.drawn_tile, is_tsumo=True,
                    is_rinshan=is_rinshan_draw,
                    is_ippatsu=ippatsu[player.seat],
                    is_double_riichi=double_rii[player.seat],
                    is_tenhou=(player.is_dealer and not player.discards and not any_call),
                    is_chiihou=((not player.is_dealer) and not player.discards and not any_call),
                )
                if res:
                    hs = format_winning_hand(player, player.drawn_tile)
                    await render(f"🎉 {player.username} 自摸！")
                    return ("tsumo", player.seat, res, hs)
                await render(f"❌ {player.username} 無役不可自摸")

            # ── 拔北（三麻北寶牌）──────────────────────────────
            if turn_view.action == "kita":
                if player.drawn_tile is not None:
                    player.hand.append(player.drawn_tile)
                    player.drawn_tile = None
                for i, t in enumerate(player.hand):
                    if t.suit == Suit.WIND and t.value == 4:
                        player.hand.pop(i)
                        break
                player.kita += 1
                await render(f"{player.username} 拔北（北寶牌 ×{player.kita}）")
                continue   # 補摸後重新行動

            # ── Ankan ──────────────────────────────────────────
            if turn_view.action == "ankan" and ankan_opts:
                kan_tile = ankan_opts[0]
                player.hand = [
                    t for t in player.hand
                    if not (t.suit == kan_tile.suit and t.value == kan_tile.value)
                ]
                if player.drawn_tile and player.drawn_tile.suit == kan_tile.suit and player.drawn_tile.value == kan_tile.value:
                    player.drawn_tile = None
                player.melds.append(Meld(MeldType.ANKAN, [Tile(kan_tile.suit, kan_tile.value)] * 4, -1))
                gs.open_dora()
                any_call = True
                for s in ippatsu:           # 任何槓打斷一發
                    ippatsu[s] = False
                rinshan_next = True          # 下一張為嶺上牌
                await render(f"{player.username} 暗杠 {kan_tile}")
                continue  # same seat, redraw

            # ── 加槓（小明槓）+ 搶槓 ─────────────────────────────
            if turn_view.action == "kakan" and turn_view.kakan_tile:
                kan_tile = turn_view.kakan_tile
                await render(f"{player.username} 宣告加槓 {kan_tile}…（搶槓確認中）")
                robber = await collect_chankan(
                    gs, kan_tile, player.seat, min(thinking_time, 12),
                    furiten_perm, temp_furiten,
                )
                if robber:
                    rp = next(p for p in gs.players if p.user_id == robber)
                    res = evaluate_win(
                        gs, rp, kan_tile, is_tsumo=False, is_chankan=True,
                        is_ippatsu=ippatsu[rp.seat], is_double_riichi=double_rii[rp.seat],
                    )
                    if res:
                        hs = format_winning_hand(rp, kan_tile)
                        await render(f"🎉 {rp.username} 搶槓！榮和 {player.username} 加槓的 {kan_tile}")
                        return ("ron", rp.seat, player.seat, res, hs)
                # 放過搶槓者 → 同巡振聽（立直者永久）
                _wk = (int(kan_tile.suit), kan_tile.value)
                for _p in gs.players:
                    if _p.seat != player.seat and _wk in hand_waits(_p):
                        temp_furiten[_p.seat] = True
                        if _p.riichi:
                            furiten_perm[_p.seat] = True
                # 無人搶槓 → 完成加槓：碰子升級為槓
                if player.drawn_tile and player.drawn_tile.suit == kan_tile.suit and player.drawn_tile.value == kan_tile.value:
                    player.drawn_tile = None
                else:
                    for i, t in enumerate(player.hand):
                        if t.suit == kan_tile.suit and t.value == kan_tile.value:
                            player.hand.pop(i)
                            break
                for m in player.melds:
                    if m.meld_type == MeldType.PON and m.tiles[0].suit == kan_tile.suit and m.tiles[0].value == kan_tile.value:
                        m.meld_type = MeldType.KAN
                        m.tiles = [Tile(kan_tile.suit, kan_tile.value)] * 4
                        break
                gs.open_dora()
                any_call = True
                for s in ippatsu:
                    ippatsu[s] = False
                rinshan_next = True
                await render(f"{player.username} 加槓 {kan_tile}！摸嶺上牌。")
                continue  # same seat, redraw rinshan

            # ── Discard ─────────────────────────────────────────
            auto = turn_view.timed_out or not turn_view.discard_tile
            if auto:
                discard_tile = player.drawn_tile if player.drawn_tile else player.hand[-1]
            else:
                discard_tile = turn_view.discard_tile

            nxt = gs.players[(player.seat + 1) % len(gs.players)].username
            # 摸切＝打出剛摸到的牌；手切＝打出原本手裡的牌
            giri = "摸切" if (player.drawn_tile is not None and discard_tile == player.drawn_tile) else "手切"
            if turn_view.action == "riichi" and not player.riichi:
                player.riichi     = True
                gs.riichi_sticks += 1
                player.score     -= 1000
                # 第一巡且無人鳴牌 → 兩立直
                double_rii[player.seat] = (not any_call) and (len(player.discards) == 0)
                ippatsu[player.seat]    = True   # 取得一發資格（本回合不會過期）
                status_text = f"🀄 {player.username} 宣告立直！（{giri}）輪到 {nxt}"
            elif auto:
                status_text = f"⏰ {player.username} 超時自動打出（{giri}），輪到 {nxt}"
            else:
                status_text = f"{player.username} 出牌了（{giri}），輪到 {nxt}"

            # 摸到的牌先併回手牌，再移除要打的那張（避免遺失摸牌）
            if player.drawn_tile is not None:
                player.hand.append(player.drawn_tile)
                player.drawn_tile = None
            if discard_tile in player.hand:
                player.hand.remove(discard_tile)
            player.discards.append(discard_tile)

            # 一發過期：本回合開始時已有資格（代表立直後的下一巡），出牌後失效
            if ipp_start:
                ippatsu[player.seat] = False

            await render(status_text)

            # Post-discard tenpai DM
            if is_tenpai(player.hand) and not player.riichi:
                waits    = get_tenpai_waits(player.hand)
                wait_str = " ".join(str(t) for t in waits)
                u = bot.get_user(int(player.user_id))
                if u:
                    try:
                        await u.send(f"🀄 打出後聽牌！待牌：{wait_str}")
                    except discord.Forbidden:
                        pass

        gs.pending_discard   = discard_tile
        gs.pending_from_seat = player.seat

        # ── Reactions ───────────────────────────────────────────
        reaction = await collect_reactions(
            gs, discard_tile, player.seat, thinking_time, furiten_perm, temp_furiten
        )

        # 振聽更新：凡是「此牌為其待牌卻未榮和」的玩家 → 同巡振聽；立直者 → 永久振聽
        wkey = (int(discard_tile.suit), discard_tile.value)
        for p in gs.players:
            if p.seat == player.seat:
                continue
            if wkey in hand_waits(p):
                temp_furiten[p.seat] = True
                if p.riichi:
                    furiten_perm[p.seat] = True

        if reaction:
            rtype, r_uid, extra = reaction
            rp = next((p for p in gs.players if p.user_id == r_uid), None)
            from_name = player.username  # 被反應（被吃碰槓榮）的那位打牌者

            if rtype == "ron" and rp:
                res = evaluate_win(
                    gs, rp, discard_tile, is_tsumo=False,
                    is_ippatsu=ippatsu[rp.seat],
                    is_double_riichi=double_rii[rp.seat],
                )
                if res:
                    hs = format_winning_hand(rp, discard_tile)
                    await render(f"🎉 {rp.username} 榮和 {from_name} 打出的 {discard_tile}！")
                    return ("ron", rp.seat, player.seat, res, hs)
                else:
                    await render(f"❌ {rp.username} 榮和無效（無役）")

            elif rtype == "pon" and rp:
                removed, new_hand = 0, []
                for t in rp.hand:
                    if t.suit == discard_tile.suit and t.value == discard_tile.value and removed < 2:
                        removed += 1
                    else:
                        new_hand.append(t)
                rp.hand = new_hand
                rp.melds.append(Meld(MeldType.PON, [Tile(discard_tile.suit, discard_tile.value)] * 3, player.seat))
                gs.current_seat = rp.seat
                no_draw = True   # 碰後直接出牌不摸
                any_call = True
                for s in ippatsu:
                    ippatsu[s] = False
                await render(f"{rp.username} 碰了 {from_name} 的 {discard_tile}！請打出一張牌。")
                continue

            elif rtype == "chi" and rp and extra:
                t1, t2 = extra
                rp.hand.remove(t1)
                rp.hand.remove(t2)
                meld_tiles = sorted([t1, t2, discard_tile], key=lambda t: (t.suit, t.value))
                rp.melds.append(Meld(MeldType.CHI, meld_tiles, player.seat))
                gs.current_seat = rp.seat
                no_draw = True   # 吃後直接出牌不摸
                any_call = True
                for s in ippatsu:
                    ippatsu[s] = False
                await render(f"{rp.username} 吃了 {from_name} 的 {discard_tile}！請打出一張牌。")
                continue

            elif rtype == "kan" and rp:
                removed, new_hand = 0, []
                for t in rp.hand:
                    if t.suit == discard_tile.suit and t.value == discard_tile.value and removed < 3:
                        removed += 1
                    else:
                        new_hand.append(t)
                rp.hand = new_hand
                rp.melds.append(Meld(MeldType.KAN, [Tile(discard_tile.suit, discard_tile.value)] * 4, player.seat))
                gs.open_dora()
                gs.current_seat = rp.seat
                any_call = True
                for s in ippatsu:
                    ippatsu[s] = False
                rinshan_next = True   # 大明槓後摸嶺上牌
                await render(f"{rp.username} 槓了 {from_name} 的 {discard_tile}！摸嶺上牌。")
                continue

        # ── Next seat ───────────────────────────────────────────
        gs.current_seat = (gs.current_seat + 1) % len(gs.players)


# ═══════════════════════════════════════════════════════════════
#  Match loop（多局：東風戰／半莊）
# ═══════════════════════════════════════════════════════════════

def deal_next_hand(gid: str, players_info: list[dict], prev: GameState) -> GameState:
    """開新一局：重洗牌山、重新配牌，但保留分數、莊家、場風局數、本場、立直棒。"""
    gs = new_game(gid, players_info, prev.is_sanma)
    gs.round_wind    = prev.round_wind
    gs.round_num     = prev.round_num
    gs.honba         = prev.honba
    gs.riichi_sticks = prev.riichi_sticks
    gs.dealer_seat   = prev.dealer_seat
    for p, pp in zip(gs.players, prev.players):
        p.score     = pp.score
        p.is_dealer = (p.seat == prev.dealer_seat)
    gs.current_seat = prev.dealer_seat   # 莊家先摸
    return gs


def _format_settle(gs: GameState, title: str, result, log: "st.SettleLog") -> str:
    lines = [title]
    if result is not None:
        if result.describe():
            lines.append(f"役：{result.describe()}")
        tail = []
        if result.name:
            tail.append(result.name)
        if result.han:
            tail.append(f"{result.han}飜{result.fu}符")
        if tail:
            lines.append("　".join(tail))
        lines.append(f"得點：**{result.points}**")
    lines.append("")
    lines.append(log.describe(gs))
    return "\n".join(lines)


def format_winning_hand(player: PlayerState, win_tile: Tile) -> str:
    """和牌手牌顯示：門前手牌（含空隔）＋副露，並標出和牌張。"""
    hand_sorted = sorted(player.hand, key=lambda t: (t.suit, t.value))
    parts = [" ".join(str(t) for t in hand_sorted)]
    if player.melds:
        parts.append("　".join(str(m) for m in player.melds))
    return "　".join(parts) + f"　🟦[{win_tile}]"


def fairness_footer(gs: GameState) -> str:
    """本局牌山編碼揭曉（可貼到任意 SHA-256 網站驗證對局未竄改）。"""
    return (
        f"🔐 本局完整牌山編碼：\n```\n{gs.wall_seed}\n```\n"
        f"SHA-256：`{gs.wall_sha256}`"
    )


async def win_ceremony(channel: discord.TextChannel, gs: GameState,
                       header: str, hand_str: str, result, log: "st.SettleLog") -> None:
    """和牌儀式：先秀牌，再逐一揭曉役種，最後公布等級與點數。"""
    top = f"# 🎉 {header}\n## {hand_str}"
    msg = await channel.send(top)
    await asyncio.sleep(1.2)

    shown: list[str] = []
    if result.yakuman:
        items = [(n, None) for n, _ in result.yakuman]
    else:
        items = [(n, h) for n, h in result.yaku]

    for name, han in items:
        shown.append(f"・**{name}**" if han is None else f"・{name}　{han}飜")
        try:
            await msg.edit(content=top + "\n" + "\n".join(shown))
        except Exception:
            pass
        await asyncio.sleep(0.9)

    await asyncio.sleep(0.4)
    if result.yakuman:
        score_line = f"## ✨ {result.name}　{result.points} 點"
    else:
        nm = f"　{result.name}" if result.name else ""
        score_line = f"## {result.han} 飜 {result.fu} 符{nm}　{result.points} 點"
    body = top + "\n" + "\n".join(shown) + f"\n\n{score_line}\n\n" + log.describe(gs)
    try:
        await msg.edit(content=body)
    except Exception:
        pass
    return msg


async def match_loop(gid: str, channel: discord.TextChannel,
                     main_msg: discord.Message) -> None:
    config       = _room_configs.get(gid, {})
    length       = config.get("length", "tonpuu")   # tonpuu=東風戰 / hanchan=半莊
    tobi         = config.get("tobi", True)          # 擊飛：負分即結束
    open_hand    = config.get("open_hand", False)    # 公開手牌（觀戰模式）
    players_info = _waiting.get(gid, [])
    channel_id   = str(channel.id)
    base_view    = make_base_view(gid)

    try:
        while True:
            outcome = await play_hand(gid, channel, main_msg, base_view)
            if outcome is None:
                return  # 被 /end 強制結束

            gs = _games[gid]

            # ── 結算 ──
            if outcome[0] == "tsumo":
                _, wseat, result, hand_str = outcome
                log = st.apply_tsumo(gs, wseat, result)
                header = f"{gs.players[wseat].username}　自摸！"
                st.advance_after_win(gs, wseat)
            elif outcome[0] == "ron":
                _, wseat, lseat, result, hand_str = outcome
                log = st.apply_ron(gs, wseat, lseat, result)
                header = f"{gs.players[wseat].username}　榮和！（放銃：{gs.players[lseat].username}）"
                st.advance_after_win(gs, wseat)
            else:  # draw
                _, tenpai = outcome
                log = st.apply_ryuukyoku(gs, tenpai)
                result = None
                st.advance_after_draw(gs, tenpai)

            db.update_game_state(gid, "playing", gs.to_dict())

            # ── 刪掉牌桌，播放和牌儀式 / 流局結果 ──
            try:
                await main_msg.delete()
            except Exception:
                pass

            if result is not None:
                await win_ceremony(channel, gs, header, hand_str, result, log)
            else:
                tnames = "、".join(gs.players[s].username for s in tenpai) or "無人"
                await channel.send(
                    f"# 🀄 流局\n聽牌：{tnames}\n\n{log.describe(gs)}"
                )
            await asyncio.sleep(6)

            # ── 是否結束整場 ──
            if st.is_game_over(gs, length, tobi):
                break

            # ── 開新局：建立新的牌桌主訊息（每局用全新的視圖物件，避免跨訊息重用導致按鈕失效）──
            new_gs = deal_next_hand(gid, players_info, gs)
            _games[gid] = new_gs
            base_view = make_base_view(gid)
            main_msg = await channel.send(
                content=make_board_text(new_gs, f"🀄 {new_gs.round_label} 開始，輪到莊家。", open_hand),
                view=base_view,
            )
            await asyncio.sleep(1)

        # ── 全場結束：最終順位（精算）＋公開牌山編碼 ──
        gs = _games[gid]
        rows = st.final_standings(gs, start_points=config.get("start_points"))
        medals = ["🥇", "🥈", "🥉", "4️⃣"]
        lines = ["# 🏁 最終順位", ""]
        for r in rows:
            sign = "＋" if r.total_pt >= 0 else "－"
            lines.append(
                f"{medals[r.rank - 1]} **第 {r.rank} 位**　{r.username}"
                f"　{r.score} 點　｜　精算 {sign}{abs(r.total_pt):.1f}"
            )
        lines.append("")
        lines.append("🔐 牌山驗證請按下方按鈕")
        db.finish_game(gid, gs.to_dict())   # 先存檔，按鈕才查得到牌譜
        end_view = discord.ui.View(timeout=None)
        end_view.add_item(FairnessButton(gid))
        await channel.send("\n".join(lines), view=end_view)
    finally:
        _cleanup(gid, channel_id)


# ═══════════════════════════════════════════════════════════════
#  Launch game
# ═══════════════════════════════════════════════════════════════

async def launch_game(gid: str, channel: discord.TextChannel) -> None:
    config       = _room_configs[gid]
    is_sanma     = config["is_sanma"]
    players_info = _waiting[gid]

    gs = new_game(gid, players_info, is_sanma, start_points=config.get("start_points"))
    _games[gid] = gs
    for p in gs.players:
        _user_game[p.user_id] = gid

    db.create_game(gid, str(channel.guild.id), str(channel.id), gs.wall_seed)
    db.update_game_state(gid, "playing", gs.to_dict())

    # 0.2：建立公開遊戲討論串 + 每位真人的私人討論串
    try:
        await setup_threads(gid, channel, gs)
    except Exception as e:
        await channel.send(f"❌ 建立討論串失敗：{e}\n（需確認 bot 有「建立公開／私人討論串、管理討論串」權限）")
        _cleanup(gid, str(channel.id))
        return

    announce = await channel.send(
        f"🀄 遊戲開始！牌桌在討論串 {_threads[gid]['public'].mention}，"
        f"真人玩家請到自己的私人討論串出牌（直接打字）。"
    )
    _threads[gid]["announce"] = announce
    _game_tasks[gid] = asyncio.create_task(match_loop_t(gid, channel))


# ═══════════════════════════════════════════════════════════════
#  Lobby View  (with AI button)
# ═══════════════════════════════════════════════════════════════

class LobbyView(discord.ui.View):
    def __init__(self, gid: str, channel: discord.TextChannel):
        super().__init__(timeout=300)
        self.gid           = gid
        self.channel       = channel
        self.lobby_message: Optional[discord.Message] = None
        self._ai_count     = 0
        # 等待加入時也能看出牌說明
        self.add_item(HelpButton())

    def _content(self) -> str:
        cfg       = _room_configs.get(self.gid, {})
        mode      = "三麻" if cfg.get("is_sanma") else "四麻"
        tt        = cfg.get("thinking_time", 25)
        max_p     = cfg.get("max_players", 4)
        len_label = "半莊戰" if cfg.get("length") == "hanchan" else "東風戰"
        tobi_label = "開啟" if cfg.get("tobi", True) else "關閉"
        players   = _waiting.get(self.gid, [])
        plist     = "\n".join(
            f"• {'🤖' if p.get('is_bot') else ''}{p['username']}"
            for p in players
        )
        return (
            f"**麻將房間開啟！**\n"
            f"模式：{mode}　戰長：{len_label}　擊飛：{tobi_label}　思考：{tt} 秒\n"
            f"玩家（{len(players)}/{max_p}）：\n{plist}\n\n"
            f"點擊按鈕加入，或由房主加入 AI 填滿空位！"
        )

    @discord.ui.button(label="加入遊戲", style=discord.ButtonStyle.success)
    async def join_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        gid     = self.gid
        uid     = str(interaction.user.id)
        cfg     = _room_configs.get(gid, {})
        max_p   = cfg.get("max_players", 4)
        waiting = _waiting.get(gid, [])

        if any(p["user_id"] == uid for p in waiting):
            await interaction.response.send_message("❌ 你已在房間中！", ephemeral=True)
            return
        if len(waiting) >= max_p:
            await interaction.response.send_message("❌ 房間已滿！", ephemeral=True)
            return

        _waiting[gid].append({"user_id": uid, "username": interaction.user.display_name, "is_bot": False})
        await self._update(interaction)

    @discord.ui.button(label="加入 AI", style=discord.ButtonStyle.secondary)
    async def ai_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        gid     = self.gid
        uid     = str(interaction.user.id)
        if _room_owners.get(gid) != uid:
            await interaction.response.send_message("❌ 只有房主可以加入 AI。", ephemeral=True)
            return
        cfg   = _room_configs.get(gid, {})
        max_p = cfg.get("max_players", 4)
        waiting = _waiting.get(gid, [])
        if len(waiting) >= max_p:
            await interaction.response.send_message("❌ 房間已滿！", ephemeral=True)
            return

        ai_uid  = f"ai_{gid}_{self._ai_count}"
        ai_name = AI_NAMES[self._ai_count % len(AI_NAMES)]
        self._ai_count += 1
        _waiting[gid].append({"user_id": ai_uid, "username": ai_name, "is_bot": True})
        await self._update(interaction)

    async def _update(self, interaction: discord.Interaction):
        gid   = self.gid
        cfg   = _room_configs.get(gid, {})
        max_p = cfg.get("max_players", 4)
        count = len(_waiting.get(gid, []))
        content = self._content()

        if count >= max_p:
            for child in self.children:
                child.disabled = True
            content += "\n\n✅ **人數已滿，遊戲即將開始！**"
            await interaction.response.edit_message(content=content, view=self)
            self.stop()
            await launch_game(gid, self.channel)
        else:
            await interaction.response.edit_message(content=content, view=self)

    async def push_update(self) -> None:
        """由指令（/join）觸發：直接編輯大廳訊息，必要時開局。"""
        gid   = self.gid
        cfg   = _room_configs.get(gid, {})
        max_p = cfg.get("max_players", 4)
        count = len(_waiting.get(gid, []))
        content = self._content()
        if count >= max_p:
            for child in self.children:
                child.disabled = True
            content += "\n\n✅ **人數已滿，遊戲即將開始！**"
            if self.lobby_message:
                try:
                    await self.lobby_message.edit(content=content, view=self)
                except Exception:
                    pass
            self.stop()
            await launch_game(gid, self.channel)
        elif self.lobby_message:
            try:
                await self.lobby_message.edit(content=content, view=self)
            except Exception:
                pass

    async def on_timeout(self):
        cfg     = _room_configs.get(self.gid, {})
        waiting = _waiting.get(self.gid, [])
        if len(waiting) < cfg.get("max_players", 4):
            _cleanup(self.gid, str(self.channel.id))
            if self.lobby_message:
                try:
                    await self.lobby_message.edit(content="❌ 等待逾時，房間已關閉。", view=None)
                except Exception:
                    pass


# ═══════════════════════════════════════════════════════════════
#  Bot events
# ═══════════════════════════════════════════════════════════════

@bot.event
async def on_ready() -> None:
    db.init_db()
    try:
        synced = await tree.sync()
        print(f"✅ 已同步 {len(synced)} 個命令")
    except Exception as e:
        print(f"⚠️ 命令同步錯誤: {e}")
    print(f"✅ Bot 登錄為 {bot.user}")
    print("=" * 50)


# ═══════════════════════════════════════════════════════════════
#  /mahjong group
# ═══════════════════════════════════════════════════════════════

mahjong = app_commands.Group(name="mahjong", description="日本麻將遊戲")


@mahjong.command(name="start", description="開啟新麻將房間")
async def cmd_start(interaction: discord.Interaction) -> None:
    channel_id = str(interaction.channel_id)
    user_id    = str(interaction.user.id)

    if channel_id in _channel_games:
        await interaction.response.send_message("❌ 此頻道已有牌局！", ephemeral=True)
        return

    sv = RoomSettingsView(gid="setup")
    await interaction.response.send_message("⚙️ 請設定遊戲選項，完成後按 ✅ 確認：", view=sv, ephemeral=True)
    await sv.wait()

    is_sanma      = sv.sanma
    thinking_time = sv.thinking_time
    max_players   = 3 if is_sanma else 4
    length        = "hanchan" if sv.hanchan else "tonpuu"
    tobi          = sv.tobi
    ruleset       = sv.ruleset
    # 起始點數：未指定則依模式預設（四麻 25000、三麻 35000）
    start_points  = sv.start_points if sv.start_points is not None else (35000 if is_sanma else 25000)

    gid = str(uuid.uuid4())[:8]
    _waiting[gid]            = [{"user_id": user_id, "username": interaction.user.display_name, "is_bot": False}]
    _room_owners[gid]        = user_id
    _channel_games[channel_id] = gid
    _room_configs[gid]       = {
        "is_sanma": is_sanma, "thinking_time": thinking_time, "max_players": max_players,
        "length": length, "tobi": tobi, "ruleset": ruleset, "start_points": start_points,
    }

    mode      = "三麻" if is_sanma else "四麻"
    len_label = "半莊戰" if length == "hanchan" else "東風戰"
    tobi_label = "開啟" if tobi else "關閉"
    rule_label = "天鳳式" if ruleset == "tenhou" else "預設"
    lobby = LobbyView(gid, interaction.channel)
    msg   = await interaction.followup.send(
        f"**麻將房間開啟！**\n"
        f"模式：{mode}　戰長：{len_label}　擊飛：{tobi_label}　規則：{rule_label}\n"
        f"起始：{start_points} 點　思考：{thinking_time} 秒\n"
        f"玩家（1/{max_players}）：\n• {interaction.user.display_name}\n\n"
        f"點擊按鈕加入（或用 `/mahjong join`），或由房主加入 AI 填滿空位！",
        view=lobby,
    )
    lobby.lobby_message = msg
    _lobbies[gid] = lobby


@mahjong.command(name="join", description="加入此頻道的麻將房間")
@app_commands.describe(host="（選填）指定房主，確認是要加入誰開的房")
async def cmd_join(interaction: discord.Interaction, host: discord.Member = None) -> None:
    channel_id = str(interaction.channel_id)
    uid        = str(interaction.user.id)
    gid        = _channel_games.get(channel_id)

    if not gid:
        await interaction.response.send_message(
            "❌ 此頻道沒有開放中的房間，請先 `/mahjong start` 開房。", ephemeral=True)
        return
    if gid in _games:
        await interaction.response.send_message("❌ 該房間已開始遊戲，無法加入。", ephemeral=True)
        return
    if host is not None and _room_owners.get(gid) != str(host.id):
        await interaction.response.send_message(
            f"❌ {host.display_name} 不是此頻道的房主。", ephemeral=True)
        return

    cfg     = _room_configs.get(gid, {})
    max_p   = cfg.get("max_players", 4)
    waiting = _waiting.get(gid, [])

    if any(p["user_id"] == uid for p in waiting):
        await interaction.response.send_message("❌ 你已在房間中！", ephemeral=True)
        return
    if len(waiting) >= max_p:
        await interaction.response.send_message("❌ 房間已滿！", ephemeral=True)
        return

    waiting.append({"user_id": uid, "username": interaction.user.display_name, "is_bot": False})
    await interaction.response.send_message(
        f"✅ 已加入房間！（{len(waiting)}/{max_p}）", ephemeral=True)

    lobby = _lobbies.get(gid)
    if lobby:
        await lobby.push_update()


@mahjong.command(name="end", description="強制結束牌局（房主）")
async def cmd_end(interaction: discord.Interaction) -> None:
    channel_id = str(interaction.channel_id)
    user_id    = str(interaction.user.id)
    # 可在主頻道或任一遊戲討論串內使用
    gid = _channel_games.get(channel_id) or _thread_game.get(int(channel_id))
    if not gid:
        await interaction.response.send_message("❌ 沒有進行中的牌局。", ephemeral=True)
        return
    if _room_owners.get(gid) != user_id:
        await interaction.response.send_message("❌ 只有房主可以結束牌局。", ephemeral=True)
        return
    # 找出主頻道 id（給 _cleanup 用）
    parent_cid = next((c for c, g in _channel_games.items() if g == gid), channel_id)
    task = _game_tasks.get(gid)
    th   = _threads.get(gid)
    _cleanup(gid, parent_cid)
    if task and not task.done():
        task.cancel()
    await interaction.response.send_message("✅ 牌局已強制結束，討論串將關閉。", ephemeral=True)
    if th:
        await _delete_threads(th)


@mahjong.command(name="status", description="查看當前牌局狀態")
async def cmd_status(interaction: discord.Interaction) -> None:
    channel_id = str(interaction.channel_id)
    gid        = _channel_games.get(channel_id)
    if not gid:
        await interaction.response.send_message("❌ 此頻道沒有進行中的牌局。", ephemeral=True)
        return
    gs = _games.get(gid)
    if not gs:
        waiting = _waiting.get(gid, [])
        cfg     = _room_configs.get(gid, {})
        max_p   = cfg.get("max_players", 4)
        plist   = "\n".join(f"• {p['username']}" for p in waiting)
        await interaction.response.send_message(
            f"🕐 等待玩家中...（{len(waiting)}/{max_p}）\n{plist}", ephemeral=True
        )
        return
    open_hand = _room_configs.get(gid, {}).get("open_hand", False)
    await interaction.response.send_message(content=make_board_text(gs, "", open_hand), ephemeral=True)


@mahjong.command(name="watch", description="觀戰模式：全部 AI 自動對局（公開手牌）")
@app_commands.describe(half="是否打半莊（預設東風戰）", sanma="是否三麻（預設四麻）")
async def cmd_watch(interaction: discord.Interaction, half: bool = False, sanma: bool = False) -> None:
    channel_id = str(interaction.channel_id)
    if channel_id in _channel_games:
        await interaction.response.send_message("❌ 此頻道已有牌局！", ephemeral=True)
        return

    n_players = 3 if sanma else 4
    gid = str(uuid.uuid4())[:8]
    players_info = [
        {"user_id": f"ai_{gid}_{i}", "username": AI_NAMES[i % len(AI_NAMES)], "is_bot": True}
        for i in range(n_players)
    ]
    _waiting[gid]              = players_info
    _room_owners[gid]         = str(interaction.user.id)
    _channel_games[channel_id] = gid
    _room_configs[gid]        = {
        "is_sanma": sanma, "thinking_time": 25, "max_players": n_players,
        "length": "hanchan" if half else "tonpuu", "tobi": True,
        "open_hand": True,   # 觀戰模式預設公開手牌
        "ruleset": "mixed",
        "start_points": 35000 if sanma else 25000,
    }

    mode_label = "三麻" if sanma else "四麻"
    len_label  = "半莊戰" if half else "東風戰"
    await interaction.response.send_message(
        f"🀄 **觀戰模式**啟動！全部 AI 自動對局（{mode_label}・{len_label}・公開手牌）"
    )
    await launch_game(gid, interaction.channel)


@mahjong.command(name="stats", description="查看個人統計")
async def cmd_stats(interaction: discord.Interaction) -> None:
    uid   = str(interaction.user.id)
    stats = db.get_stats(uid)
    if not stats:
        await interaction.response.send_message("❌ 你還沒有遊戲記錄。", ephemeral=True)
        return
    embed = discord.Embed(title=f"📊 {interaction.user.display_name} 的統計", color=0x3498DB)
    embed.add_field(name="遊玩場次", value=str(stats["games_played"]))
    embed.add_field(name="勝場",     value=str(stats["wins"]))
    embed.add_field(name="自摸",     value=str(stats["tsumo_wins"]))
    embed.add_field(name="榮和",     value=str(stats["ron_wins"]))
    embed.add_field(name="立直次數", value=str(stats["riichi_count"]))
    embed.add_field(name="累積得分", value=str(stats["total_score"]))
    await interaction.response.send_message(embed=embed, ephemeral=True)


@mahjong.command(name="help", description="查看出牌輸入說明")
async def cmd_help(interaction: discord.Interaction) -> None:
    await interaction.response.send_message(HELP_TEXT, ephemeral=True)


tree.add_command(mahjong)


# ═══════════════════════════════════════════════════════════════
#  Top-level reaction / action commands
# ═══════════════════════════════════════════════════════════════

@tree.command(name="help", description="查看出牌輸入說明")
async def cmd_help_top(interaction: discord.Interaction) -> None:
    await interaction.response.send_message(HELP_TEXT, ephemeral=True)


def _get_pending(user_id: str) -> Optional[tuple[str, dict]]:
    gid  = _user_game.get(user_id)
    if not gid:
        return None
    info = _pending_reactions.get(gid, {}).get(user_id)
    if not info:
        return None
    return (gid, info)


@tree.command(name="ron", description="宣告榮和")
async def cmd_ron(interaction: discord.Interaction) -> None:
    uid    = str(interaction.user.id)
    result = _get_pending(uid)
    if not result or "ron" not in result[1]["actions"]:
        await interaction.response.send_message("❌ 現在無法宣告榮和。", ephemeral=True)
        return
    _, info = result
    info["choice"] = "ron"
    info["event"].set()
    await interaction.response.send_message("✅ 已宣告榮和！", ephemeral=True)


@tree.command(name="pon", description="宣告碰")
async def cmd_pon(interaction: discord.Interaction) -> None:
    uid    = str(interaction.user.id)
    result = _get_pending(uid)
    if not result or "pon" not in result[1]["actions"]:
        await interaction.response.send_message("❌ 現在無法宣告碰。", ephemeral=True)
        return
    _, info = result
    info["choice"] = "pon"
    info["event"].set()
    await interaction.response.send_message("✅ 已宣告碰！", ephemeral=True)


@tree.command(name="chi", description="宣告吃（指定選項編號）")
@app_commands.describe(option="選項編號 1 或 2（預設 1）")
async def cmd_chi(interaction: discord.Interaction, option: int = 1) -> None:
    uid    = str(interaction.user.id)
    result = _get_pending(uid)
    if not result or "chi" not in result[1]["actions"]:
        await interaction.response.send_message("❌ 現在無法宣告吃。", ephemeral=True)
        return
    _, info = result
    chi_opts = info.get("chi_options", [])
    idx = option - 1
    if idx < 0 or idx >= len(chi_opts):
        lines = "\n".join(
            f"選項{i+1}：{t1.short}＋{t2.short}" for i, (t1, t2) in enumerate(chi_opts)
        )
        await interaction.response.send_message(f"❌ 無效選項。\n{lines}", ephemeral=True)
        return
    info["choice"] = "chi"
    info["extra"]  = chi_opts[idx]
    info["event"].set()
    t1, t2 = chi_opts[idx]
    await interaction.response.send_message(f"✅ 吃：{t1.short}＋{t2.short}", ephemeral=True)


@tree.command(name="kan", description="宣告槓")
async def cmd_kan(interaction: discord.Interaction) -> None:
    uid    = str(interaction.user.id)
    result = _get_pending(uid)
    if not result or "kan" not in result[1]["actions"]:
        await interaction.response.send_message("❌ 現在無法宣告槓。", ephemeral=True)
        return
    _, info = result
    info["choice"] = "kan"
    info["event"].set()
    await interaction.response.send_message("✅ 已宣告槓！", ephemeral=True)


@tree.command(name="tsumo", description="宣告自摸（輪到你時）")
async def cmd_tsumo(interaction: discord.Interaction) -> None:
    uid  = str(interaction.user.id)
    view = _active_turn.get(uid)
    if not view:
        await interaction.response.send_message("❌ 現在不是你的摸牌回合。", ephemeral=True)
        return
    # 用 scoring 驗證：必須成胡且有役
    gid = _user_game.get(uid)
    gs  = _games.get(gid) if gid else None
    player = next((p for p in gs.players if p.user_id == uid), None) if gs else None
    if not player or not player.drawn_tile:
        await interaction.response.send_message("❌ 現在無法自摸。", ephemeral=True)
        return
    if evaluate_win(gs, player, player.drawn_tile, is_tsumo=True) is None:
        await interaction.response.send_message("❌ 無役不可自摸（手牌未成胡或無役）。", ephemeral=True)
        return
    view.action = "tsumo"
    view._done.set()
    view.stop()
    await interaction.response.send_message("✅ 已宣告自摸！", ephemeral=True)


async def _do_kita(interaction: discord.Interaction) -> None:
    uid  = str(interaction.user.id)
    view = _active_turn.get(uid)
    if not view:
        await interaction.response.send_message("❌ 現在不是你的摸牌回合。", ephemeral=True)
        return
    gid    = _user_game.get(uid)
    gs     = _games.get(gid) if gid else None
    player = next((p for p in gs.players if p.user_id == uid), None) if gs else None
    if not gs or not gs.is_sanma or not player or not has_kita(player):
        await interaction.response.send_message("❌ 現在無法拔北。", ephemeral=True)
        return
    view.action = "kita"
    view._done.set()
    view.stop()
    await interaction.response.send_message("✅ 已拔北！", ephemeral=True)


@tree.command(name="kita", description="拔北（三麻北寶牌）")
async def cmd_kita(interaction: discord.Interaction) -> None:
    await _do_kita(interaction)


@tree.command(name="n", description="拔北（三麻北寶牌，/kita 的簡寫）")
async def cmd_n(interaction: discord.Interaction) -> None:
    await _do_kita(interaction)


@tree.command(name="riichi", description="宣告立直並打出指定牌")
@app_commands.describe(tile="要打出的牌名，例如：5m、東、中")
async def cmd_riichi(interaction: discord.Interaction, tile: str) -> None:
    uid  = str(interaction.user.id)
    view = _active_turn.get(uid)
    if not view:
        await interaction.response.send_message("❌ 現在不是你的摸牌回合。", ephemeral=True)
        return
    if view.player_id != uid:
        await interaction.response.send_message("❌ 不是你的回合。", ephemeral=True)
        return
    t = parse_tile(tile, view.hand, view.drawn_tile)
    if not t:
        await interaction.response.send_message(f"❌ 找不到牌：{tile}", ephemeral=True)
        return
    view._set_riichi(t)
    view._done.set()
    view.stop()
    label = "立直" if view.action == "riichi" else "出牌（手牌打出後未聽牌，普通出牌）"
    await interaction.response.send_message(f"✅ {label}：{t}", ephemeral=True)


@tree.command(name="skip", description="跳過本次反應機會")
async def cmd_skip(interaction: discord.Interaction) -> None:
    uid    = str(interaction.user.id)
    result = _get_pending(uid)
    if not result:
        await interaction.response.send_message("❌ 目前沒有等待你的反應。", ephemeral=True)
        return
    _, info = result
    info["choice"] = None
    info["event"].set()
    await interaction.response.send_message("✅ 已跳過。", ephemeral=True)


# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    try:
        bot.run(TOKEN)
    except KeyboardInterrupt:
        print("\n機器人已停止")
    except Exception as e:
        import traceback
        traceback.print_exc()
