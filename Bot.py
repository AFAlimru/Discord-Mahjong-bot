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

import mahjong.db as db
from mahjong.engine import (
    GameState, PlayerState, Tile, Suit, Meld, MeldType,
    new_game, is_complete, is_tenpai, get_tenpai_waits,
)
from mahjong.scoring import score_hand, WinContext, is_chiitoitsu, is_kokushi
import mahjong.settlement as st
from views import RoomSettingsView

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# 共享記憶體狀態統一放在 mahjong.state（供各模組共用同一份）
from mahjong.state import (
    _games, _channel_games, _waiting, _room_owners, _room_configs, _user_game,
    _game_tasks, _lobbies, _threads, _input_queues, _input_thread, _thread_game,
    _action_logs, _bg_tasks,
)

WIND_LABELS = ["東", "南", "西", "北"]
AI_NAMES    = ["小春", "小夏", "小秋", "小冬"]


# ═══════════════════════════════════════════════════════════════
#  打字輸入（討論串）
# ═══════════════════════════════════════════════════════════════

@bot.event
async def on_message(message: discord.Message) -> None:
    """讀取玩家在自己私人討論串打的字；公開牌桌討論串則禁止打字。"""
    if message.author.bot:
        return
    uid = str(message.author.id)
    q = _input_queues.get(uid)
    if q is not None and message.channel.id == _input_thread.get(uid):
        await q.put((message.content.strip(), message))
        return
    # 只有「觀戰牌桌串」禁止打字（board_msg 存在）；真人對局的聊天串可自由聊天
    gid = _thread_game.get(message.channel.id)
    if gid:
        th = _threads.get(gid)
        pub = th.get("public") if th else None
        if pub is not None and message.channel.id == pub.id and th.get("board_msg") is not None:
            try:
                await message.delete()
            except Exception:
                pass


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


from mahjong.rules import (
    count_tiles, get_chi_options, get_ankan_options, has_kita,
    get_shouminkan_options, parse_tile, ai_choose_discard, ai_should_pon,
    evaluate_win, hand_waits, tenpai_advice, tenpai_note_text,
    is_furiten, ai_should_ron,
)
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
        f"寶牌：",
        f"# {ind_str}",
    ]
    for idx, p in enumerate(gs.players):
        if idx > 0:
            # 玩家之間：長分隔線 + 上下留白，明顯分開
            lines.append("")
            lines.append("━" * 35)
            lines.append("")
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
    """上家剛打出的牌（含打牌者）；牌單獨一行放大。"""
    if gs is not None and gs.pending_discard is not None and gs.pending_from_seat >= 0:
        who = gs.players[gs.pending_from_seat].username
        return f"🀫 上家：{WIND_LABELS[gs.pending_from_seat]} {who} 打出\n# {gs.pending_discard}"
    return ""


def _log_action(gid: str, text: str) -> None:
    """記錄一筆玩家動作（保留本局最近 40 筆，供「看動態」按鈕顯示完整記錄）。"""
    if not text:
        return
    log = _action_logs.setdefault(gid, [])
    log.append(text)
    del log[:-40]


def _action_feed(gid: str, gs: GameState) -> str:
    """私人面板上方的動態：上家剛打出的牌（放大）+ 最新一筆動作。
    完整動態請按「📜 看動態」按鈕。"""
    parts = []
    ld = _last_discard_info(gs)
    if ld:
        parts.append(ld)
    log = _action_logs.get(gid, [])
    if log:
        parts.append(f"📜 {log[-1]}")
    return "\n\n".join(parts)


def make_action_log_text(gid: str) -> str:
    """完整動態（給「看動態」按鈕的私密訊息用）。"""
    log = _action_logs.get(gid, [])
    if not log:
        return "📜 **本局動態**\n（尚無動作）"
    return "📜 **本局動態**\n" + "\n".join(f"> {t}" for t in log)


def _board_info(gs: GameState) -> str:
    """場況 + 寶牌（顯示在手牌上方）。"""
    total_ind = len(gs.dora_indicators)
    shown  = [str(t) for t in gs.dora_indicators[:gs.revealed_dora]]
    hidden = ["🀫"] * (total_ind - gs.revealed_dora)
    ind    = " ".join(shown + hidden)
    return (f"# {gs.round_label}　本場 {gs.honba}　牌山 {gs.tiles_left}\n"
            f"寶牌：\n# {ind}")


def make_hand_panel(player: PlayerState, prompt: str = "", tenpai_note: str = "",
                    last_info: str = "", board_info: str = "") -> str:
    """私人討論串的個人面板：場況/寶牌、手牌｜剛摸到的牌、副露、提示。"""
    uni, names = player.hand_display_with_names()
    sep = "=" * 35
    lines = []
    if board_info:
        lines.append("\n".join("> " + ln for ln in board_info.split("\n")))
        lines.append(sep)
    if last_info:
        lines.append(last_info)
        lines.append(sep)
    lines.append("# 你的手牌" + ("　【已立直】" if player.riichi else ""))
    # 手牌 ｜ 剛摸到的牌（| 右邊為剛摸到）
    if player.drawn_tile:
        lines.append(f"# {uni} | {player.drawn_tile}" if uni else f"# | {player.drawn_tile}")
    else:
        lines.append(f"# {uni}" if uni else "（無）")
    if names:
        lines.append(names)
    # 副露（碰/吃/槓後）換行顯示
    if player.melds:
        lines.append("副露：" + "　".join(str(m) for m in player.melds))
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
    for idx, p in enumerate(gs.players):
        if idx > 0:
            lines.append("─" * 24)   # 玩家之間分隔線
        disc = " ".join(str(t) for t in p.discards) if p.discards else "（無）"
        lines.append(f"{WIND_LABELS[p.seat]} {p.username}（{len(p.discards)}）")
        lines.append(f"# {disc}")
    lines.append("")
    lines.append("＊這是當下快照；關閉後再按一次可看最新。")
    return "\n".join(lines)


def result_body(header: str, hand_str: str, result, log, gs: GameState,
                tenpai=None) -> str:
    """一局結果的靜態文字（送到聊天串與各私人討論串）。"""
    if result is None:
        tnames = "、".join(gs.players[s].username for s in (tenpai or [])) or "無人"
        return f"# 🀄 流局\n聽牌：{tnames}\n\n{log.describe(gs)}"
    top = f"# 🎉 {header}\n## {hand_str}"
    if result.yakuman:
        yaku = "\n".join(f"・**{n}**" for n, _ in result.yakuman)
        score_line = f"## ✨ {result.name}　{result.points} 點"
    else:
        yaku = "\n".join(f"・{n} {h}飜" for n, h in result.yaku)
        nm = f"　{result.name}" if result.name else ""
        score_line = f"## {result.han} 飜 {result.fu} 符{nm}　{result.points} 點"
    return top + "\n" + yaku + f"\n\n{score_line}\n\n" + log.describe(gs)


def make_score_text(gs: GameState) -> str:
    """各家點數（含風位）。"""
    lines = ["**📊 點數**"]
    for p in gs.players:
        bot = "🤖" if p.is_bot else ""
        lines.append(f"{WIND_LABELS[p.seat]}　{bot}{p.username}：**{p.score}** 點")
    return "\n".join(lines)


def make_meld_text(gs: GameState) -> str:
    """各家副露＋風位（給「看副露」按鈕用）。"""
    lines = ["**🀜 各家副露**"]
    any_meld = False
    for p in gs.players:
        if p.melds:
            any_meld = True
            melds = "　".join(str(m) for m in p.melds)
            lines.append(f"{WIND_LABELS[p.seat]}　{p.username}：{melds}")
    if not any_meld:
        lines.append("（目前無人副露）")
    return "\n".join(lines)


class HandHelpButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="❓ 說明", style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_message(HELP_TEXT, ephemeral=True)


class _GameInfoButton(discord.ui.Button):
    """共用：點擊顯示某種牌局資訊（私密）。"""
    def __init__(self, gid: str, label: str, builder):
        super().__init__(label=label, style=discord.ButtonStyle.secondary)
        self.gid = gid
        self._builder = builder

    async def callback(self, interaction: discord.Interaction):
        gs = _games.get(self.gid)
        if not gs:
            await interaction.response.send_message("❌ 牌局已結束。", ephemeral=True)
            return
        await interaction.response.send_message(self._builder(gs), ephemeral=True)


def RiverButton(gid: str):
    return _GameInfoButton(gid, "🀫 看牌河", make_river_text)


def ScoreButton(gid: str):
    return _GameInfoButton(gid, "📊 看點數", make_score_text)


def MeldButton(gid: str):
    return _GameInfoButton(gid, "🀜 看副露", make_meld_text)


class _ActionLogButton(discord.ui.Button):
    """點擊顯示本局完整動態（私密）。"""
    def __init__(self, gid: str):
        super().__init__(label="📜 看動態", style=discord.ButtonStyle.secondary)
        self.gid = gid

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_message(make_action_log_text(self.gid), ephemeral=True)


def ActionLogButton(gid: str):
    return _ActionLogButton(gid)


def make_hand_view(gid: str) -> discord.ui.View:
    """手牌面板常駐按鈕：看牌河 / 看點數 / 看副露 / 看動態，說明放最右。"""
    v = discord.ui.View(timeout=None)
    v.add_item(RiverButton(gid))
    v.add_item(ScoreButton(gid))
    v.add_item(MeldButton(gid))
    v.add_item(ActionLogButton(gid))
    v.add_item(HandHelpButton())
    return v


async def setup_threads(gid: str, channel: discord.TextChannel, gs: GameState,
                        watch: bool = False) -> None:
    """建立公開討論串 + 每位真人玩家的私人討論串。
    watch=True（觀戰）：公開串當牌桌；否則當聊天/和牌資訊串（牌桌資訊改用按鈕看）。"""
    if watch:
        public = await channel.create_thread(
            name=f"🀄 麻將牌桌 {gs.round_label}",
            type=discord.ChannelType.public_thread,
        )
        board_msg = await public.send(make_thread_board(gs, "🀄 遊戲開始，輪到莊家。"))
    else:
        public = await channel.create_thread(
            name=f"💬 麻將聊天／結果 {gs.round_label}",
            type=discord.ChannelType.public_thread,
        )
        await public.send("💬 這裡會顯示每局和牌／流局結果，也可以自由聊天。")
        board_msg = None

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
            hm = await pt.send(make_hand_panel(p, "（等待開始）", board_info=_board_info(gs)),
                               view=make_hand_view(gid))
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
    _game_tasks.pop(gid, None)
    _action_logs.pop(gid, None)
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


async def _delete_threads_later(th: dict, delay: float = 60.0) -> None:
    """對局自然結束後，保留一段時間讓大家看結果，再刪除討論串。"""
    await asyncio.sleep(delay)
    for t in [th.get("public")] + list(th.get("private", {}).values()):
        if t is None:
            continue
        try:
            await t.delete()
        except Exception:
            pass


def _schedule_delete_threads(th: dict, delay: float = 60.0) -> None:
    """排程延遲刪除討論串，並保留任務參考避免被 GC。"""
    task = asyncio.create_task(_delete_threads_later(th, delay))
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


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
    if s.startswith(("暗槓", "暗槓")) or low.startswith("ankan"):
        rest = s
        for kw in ("暗槓", "暗槓", "ankan", "ANKAN"):
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
            elif (not p.riichi) and ai_should_pon(p.hand, discard_tile):
                results.append((1, p.seat, "pon", None))
            continue
        ron_ok = (not is_furiten(p, furiten_perm, furiten_temp)) and \
                 evaluate_win(gs, p, discard_tile, is_tsumo=False) is not None
        actions, chi_opts = [], []
        if ron_ok:
            actions.append("ron")
        if not p.riichi:   # 立直後不能副露（碰／吃／槓），只能榮和
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

        CIRC = "①②③④⑤"
        # 吃的候選組合：牌面寫在訊息裡（放大），不塞進按鈕
        chi_combos = []
        for t1, t2 in chi_opts:
            srt = sorted([t1, t2, discard_tile], key=lambda t: (t.suit, t.value))
            chi_combos.append(" ".join(str(t) for t in srt))

        if "ron" in actions:
            add_btn("榮和", discord.ButtonStyle.danger, "ron", None)        # 紅
        if "pon" in actions:
            add_btn("碰", discord.ButtonStyle.primary, "pon", None)         # 藍
        if "kan" in actions:
            add_btn("槓", discord.ButtonStyle.primary, "kan", None)         # 藍
        if "chi" in actions:
            for idx, (t1, t2) in enumerate(chi_opts):
                lbl = "吃" if len(chi_opts) == 1 else f"吃{CIRC[idx]}"
                add_btn(lbl, discord.ButtonStyle.success, "chi", (t1, t2))  # 綠
        add_btn("跳過", discord.ButtonStyle.secondary, "skip", None)        # 灰（避免與紅色榮和混淆）

        def prompt_text(rem):
            lines = [f"## ⬇️ {from_name} 打出", f"# {discard_tile}"]
            if len(chi_combos) == 1:
                lines.append(f"# 吃 {chi_combos[0]}")
            else:
                for idx, cmb in enumerate(chi_combos):
                    lines.append(f"# 吃{CIRC[idx]} {cmb}")
            lines.append(f"請選擇（剩 {rem} 秒）")
            return "\n".join(lines)

        try:
            prompt_msg = await pt.send(prompt_text(int(thinking_time)), view=view)
        except Exception:
            return None

        async def cd():
            rem = int(thinking_time)
            while rem > 0:
                await asyncio.sleep(1)
                rem -= 1
                try:
                    await prompt_msg.edit(content=prompt_text(rem))
                except Exception:
                    pass

        cd_task = asyncio.create_task(cd())
        try:
            res = await asyncio.wait_for(fut, timeout=thinking_time)
        except asyncio.TimeoutError:
            res = ("skip", None)
        finally:
            cd_task.cancel()
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


async def collect_chankan_t(gs, gid, kan_tile, kan_seat, thinking_time,
                            furiten_perm, furiten_temp):
    """討論串版搶槓（槍槓）窗口：可榮和此加槓牌者按「搶槓」。回傳搶槓者 user_id 或 None。"""
    th       = _threads.get(gid, {})
    private  = th.get("private", {})
    kan_name = gs.players[kan_seat].username
    results  = []        # seat
    candidates = []
    for p in gs.players:
        if p.seat == kan_seat:
            continue
        if is_furiten(p, furiten_perm, furiten_temp):
            continue
        if evaluate_win(gs, p, kan_tile, is_tsumo=False, is_chankan=True) is None:
            continue
        if p.is_bot:
            results.append(p.seat)       # AI 一律搶槓
        else:
            candidates.append(p)

    async def ask(p):
        pt = private.get(p.user_id)
        if not pt:
            return None
        uid = p.user_id
        fut = asyncio.get_event_loop().create_future()
        view = discord.ui.View(timeout=thinking_time + 5)

        def add_btn(label, style, choice):
            b = discord.ui.Button(label=label, style=style)
            async def cb(inter):
                if str(inter.user.id) != uid:
                    await inter.response.send_message("❌ 不是你的回應", ephemeral=True)
                    return
                await inter.response.defer()
                if not fut.done():
                    fut.set_result(choice)
            b.callback = cb
            view.add_item(b)

        add_btn("搶槓榮和", discord.ButtonStyle.danger, "ron")
        add_btn("跳過", discord.ButtonStyle.secondary, "skip")

        def prompt_text(rem):
            return (f"## 🀄 {kan_name} 宣告加槓\n# {kan_tile}\n"
                    f"可**搶槓榮和**！請選擇（剩 {rem} 秒）")

        try:
            prompt_msg = await pt.send(prompt_text(int(thinking_time)), view=view)
        except Exception:
            return None

        async def cd():
            rem = int(thinking_time)
            while rem > 0:
                await asyncio.sleep(1)
                rem -= 1
                try:
                    await prompt_msg.edit(content=prompt_text(rem))
                except Exception:
                    pass

        cd_task = asyncio.create_task(cd())
        try:
            res = await asyncio.wait_for(fut, timeout=thinking_time)
        except asyncio.TimeoutError:
            res = "skip"
        finally:
            cd_task.cancel()
        try:
            await prompt_msg.delete()
        except Exception:
            pass
        return p.seat if res == "ron" else None

    if candidates:
        human = await asyncio.gather(*[ask(p) for p in candidates])
        results.extend(s for s in human if s is not None)

    if not results:
        return None
    results.sort()
    return gs.players[results[0]].user_id


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
                           prompt_base, tenpai_note, last_info="", board_info="",
                           riichi_locked=False, kakan_opts=None):
    """輪到玩家：自摸/立直/暗槓/拔北用按鈕，出牌用打字。回傳 (action, arg) 或 None（逾時）。
    riichi_locked=True（已立直）：鎖手，打字無效，只能按自摸或逾時自動摸切。"""
    uid   = player.user_id
    fut   = asyncio.get_event_loop().create_future()
    state = {"riichi": False, "rem": int(thinking_time)}
    view  = discord.ui.View(timeout=float(thinking_time) + 10)
    # 資訊按鈕：看牌河 / 看點數 / 看副露（輪到自己時也能看）；說明最後加（最右）
    view.add_item(RiverButton(gid))
    view.add_item(ScoreButton(gid))
    view.add_item(MeldButton(gid))
    view.add_item(ActionLogButton(gid))

    def panel(rem):
        if riichi_locked:
            extra = f"🀄 **已立直**：將自動摸切，可按「自摸」　（剩 {rem} 秒）"
        elif state["riichi"]:
            extra = "🀄 已宣告立直，請**打字**打出宣言牌（再按一次「立直」可取消）"
        else:
            extra = f"{prompt_base}　（剩 {rem} 秒）"
        return make_hand_panel(player, extra, tenpai_note, last_info, board_info)

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
                state["riichi"] = not state["riichi"]   # 再按一次取消立直
                await refresh(state["rem"])
            elif not fut.done():
                fut.set_result(kind)
        b.callback = cb
        view.add_item(b)

    if can_tsumo:
        add_btn("自摸", discord.ButtonStyle.danger, ("tsumo", None))       # 紅
    if can_riichi:
        add_btn("立直", discord.ButtonStyle.secondary, "riichi")
    if ankan_opts:
        add_btn("暗槓", discord.ButtonStyle.secondary, ("ankan", ankan_opts[0]))
    if kakan_opts:
        add_btn("加槓", discord.ButtonStyle.secondary, ("kakan", kakan_opts[0]))
    if kita_ok:
        add_btn("拔北", discord.ButtonStyle.secondary, ("kita", None))

    view.add_item(HandHelpButton())   # 說明放最右

    await refresh(int(thinking_time))

    q = asyncio.Queue()
    _input_queues[uid] = q
    _input_thread[uid] = pt.id

    async def countdown():
        rem = int(thinking_time)
        while rem > 0:
            await asyncio.sleep(1)
            rem -= 1
            state["rem"] = rem
            if not state["riichi"]:
                await refresh(rem)

    async def reader():
        while not fut.done():
            raw, msg = await q.get()
            try:
                await msg.delete()
            except Exception:
                pass
            if riichi_locked:
                await _warn(pt, "立直中只能摸切（可按「自摸」），無法換牌或副露")
                continue
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
    _action_logs[gid] = []   # 每局開始清空動作記錄

    async def render_board(status="", log=True):
        if log:
            _log_action(gid, status)   # 真人對局沒有牌桌，動作改記在私人面板上方
        if board_msg is None:   # 真人對局沒有公開牌桌（資訊改用按鈕看）
            return
        try:
            await board_msg.edit(content=make_thread_board(gs, status))
        except Exception:
            pass

    async def render_hand(p, prompt="", tenpai_note=""):
        hm = hand_msg.get(p.user_id)
        if hm:
            try:
                await hm.edit(content=make_hand_panel(p, prompt, tenpai_note,
                                                      _action_feed(gid, gs), _board_info(gs)),
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
            already_riichi = player.riichi
            can_tsumo  = bool(player.drawn_tile) and evaluate_win(
                gs, player, player.drawn_tile, is_tsumo=True, is_rinshan=is_rinshan_draw) is not None

            if already_riichi:
                # 立直後：鎖手，只能摸切或自摸，不可立直／暗槓／加槓／拔北
                adv = []
                can_riichi  = False
                ankan_opts  = []
                kakan_opts  = []
                kita_ok     = False
                tenpai_note = ""
                prompt_base = "🀄 **已立直**：自動摸切（可自摸）"
                turn_time   = thinking_time if can_tsumo else 3
            else:
                adv = tenpai_advice(player)   # 14 張時：打哪張可進聽
                can_riichi = (len(player.melds) == 0) and bool(adv)
                ankan_opts = get_ankan_options(player.hand + ([player.drawn_tile] if player.drawn_tile else []))
                kakan_opts = get_shouminkan_options(player)   # 加槓：已碰且持有第 4 張
                kita_ok    = gs.is_sanma and has_kita(player)
                # 進聽提示：打哪張可聽、聽哪些；並標註（無役）／（振聽）
                tenpai_note = tenpai_note_text(gs, player, adv) if adv else ""
                prompt_base = "✍️ **打字**丟牌；其他行動請看「說明」"
                turn_time   = thinking_time

            await render_board(f"輪到 <@{player.user_id}>（{WIND_LABELS[player.seat]}）出牌", log=False)

            pt = private.get(player.user_id)
            hm = hand_msg.get(player.user_id)
            ping_msg = None
            if pt:
                try:
                    ping_msg = await pt.send(f"<@{player.user_id}> 輪到你了！")
                except Exception:
                    pass
            result = None
            if pt and hm:
                result = await wait_turn_action(
                    gid, player, pt, hm, turn_time,
                    can_tsumo, can_riichi, kita_ok, ankan_opts,
                    prompt_base, tenpai_note, _action_feed(gid, gs), _board_info(gs),
                    riichi_locked=already_riichi, kakan_opts=kakan_opts,
                )
            else:
                await render_hand(player, prompt_base, tenpai_note)
            if ping_msg:   # 出完牌（行動結束）才刪掉提醒
                try:
                    await ping_msg.delete()
                except Exception:
                    pass

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

            # ── 加槓（小明槓）+ 搶槓 ──
            if action == "kakan" and arg is not None:
                kan_tile = arg
                await render_board(f"{player.username} 宣告加槓 {kan_tile}…（搶槓確認中）")
                robber = await collect_chankan_t(
                    gs, gid, kan_tile, player.seat, min(thinking_time, 12),
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
                        await render_board(f"🎉 {rp.username} 搶槓！榮和 {player.username} 加槓的 {kan_tile}")
                        return ("ron", rp.seat, player.seat, res, hs)
                # 放過搶槓 → 同巡振聽（立直者永久）
                _wk = (int(kan_tile.suit), kan_tile.value)
                for _p in gs.players:
                    if _p.seat != player.seat and _wk in hand_waits(_p):
                        temp_furiten[_p.seat] = True
                        if _p.riichi:
                            furiten_perm[_p.seat] = True
                # 完成加槓：移除第 4 張、碰子升級為槓
                if player.drawn_tile and player.drawn_tile.suit == kan_tile.suit and player.drawn_tile.value == kan_tile.value:
                    player.drawn_tile = None
                else:
                    for i, t in enumerate(player.hand):
                        if t.suit == kan_tile.suit and t.value == kan_tile.value:
                            player.hand.pop(i); break
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
                await render_hand(player)
                await render_board(f"{player.username} 加槓 {kan_tile}！摸嶺上牌。")
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
            if already_riichi:
                await render_board(f"{player.username} 立直摸切，輪到 {nxt}")
            else:
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
            tenpai = None
            header = ""
            hand_str = ""

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

            # 一局結束：先把各家手牌面板刪掉，讓結果獨佔畫面（下一局再重建）
            for uid, hm in list(th.get("hand_msg", {}).items()):
                if hm:
                    try:
                        await hm.delete()
                    except Exception:
                        pass
                th["hand_msg"][uid] = None

            # 和牌：公開串與各私人串都做「逐一揭曉」的和牌儀式；流局則送結果文字
            if result is not None:
                channels = [public] + list(th.get("private", {}).values())
                cer = await asyncio.gather(
                    *[win_ceremony(ch, gs, header, hand_str, result, log) for ch in channels],
                    return_exceptions=True,
                )
                all_msgs, result_body_text = [], ""
                for c in cer:
                    if isinstance(c, Exception):
                        continue
                    m, b = c
                    all_msgs.append(m)
                    result_body_text = b
                result_msg = all_msgs[0] if all_msgs else None
                priv_msgs  = all_msgs[1:]
            else:
                result_body_text = result_body("", "", None, log, gs, tenpai)
                result_msg = await public.send(result_body_text)
                priv_msgs = []
                for pt in th.get("private", {}).values():
                    try:
                        priv_msgs.append(await pt.send(result_body_text))
                    except Exception:
                        pass

            # 全部顯示完 → 保留完整結果，倒數 5 秒自動進入下一局
            await _result_countdown([result_msg, *priv_msgs], result_body_text, 5)

            if st.is_game_over(gs, length, tobi):
                break

            # 換下一局：刪掉上一局的結果訊息（公開＋各私人）
            for m in [result_msg, *priv_msgs]:
                if m:
                    try:
                        await m.delete()
                    except Exception:
                        pass

            new_gs = deal_next_hand(gid, players_info, gs)
            _games[gid] = new_gs
            if th.get("board_msg"):
                try:
                    await th["board_msg"].edit(
                        content=make_thread_board(new_gs, f"🀄 {new_gs.round_label} 開始，輪到莊家。"))
                except Exception:
                    pass
            for p in new_gs.players:
                if p.is_bot:
                    continue
                pt = th["private"].get(p.user_id)
                if not pt:
                    continue
                try:
                    th["hand_msg"][p.user_id] = await pt.send(
                        make_hand_panel(p, board_info=_board_info(new_gs)),
                        view=make_hand_view(gid))
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
        if th.get("board_msg") is not None:
            # 觀戰：保留約一分鐘讓大家看結果，再刪除討論串
            await _delete_announce(th)
            _schedule_delete_threads(th, 60)
        else:
            # 真人對局：封存（保留供回顧）
            await _archive_threads(th)
    finally:
        _cleanup(gid, channel_id)


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


def format_winning_hand(player: PlayerState, win_tile: Tile) -> str:
    """和牌手牌顯示：門前手牌（含空隔）＋副露，並標出和牌張。"""
    hand_sorted = sorted(player.hand, key=lambda t: (t.suit, t.value))
    parts = [" ".join(str(t) for t in hand_sorted)]
    if player.melds:
        parts.append("　".join(str(m) for m in player.melds))
    return "　".join(parts) + f"　🟦[{win_tile}]"


async def win_ceremony(channel: discord.TextChannel, gs: GameState,
                       header: str, hand_str: str, result, log: "st.SettleLog") -> None:
    """和牌儀式：先放標題，再放手牌，接著逐一揭曉役種，最後公布等級與點數。"""
    head = f"# 🎉 {header}"
    msg = await channel.send(head)          # ① 先只放榮和／自摸標題
    await asyncio.sleep(1.0)
    top = f"{head}\n## {hand_str}"
    try:
        await msg.edit(content=top)         # ② 再放手牌
    except Exception:
        pass
    await asyncio.sleep(1.0)

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
    return msg, body


async def _result_countdown(msgs: list, base: str, secs: int = 5) -> None:
    """一局結果全部顯示完後，保留完整結果並於尾端倒數，時間到自動進入下一局。"""
    for n in range(secs, 0, -1):
        for m in msgs:
            if not m:
                continue
            try:
                await m.edit(content=f"{base}\n\n⏳ {n} 秒後進入下一局…")
            except Exception:
                pass
        await asyncio.sleep(1)


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

    # 0.2：建立公開討論串 + 每位真人的私人討論串（觀戰模式才用牌桌）
    try:
        await setup_threads(gid, channel, gs, watch=config.get("open_hand", False))
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




# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    try:
        bot.run(TOKEN)
    except KeyboardInterrupt:
        print("\n機器人已停止")
    except Exception as e:
        import traceback
        traceback.print_exc()
