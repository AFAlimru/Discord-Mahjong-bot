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
"""斜線指令：/mahjong 房間管理（start/join/end/status/watch/stats/help）與大廳 LobbyView。"""
from __future__ import annotations
import uuid
import discord
from discord import app_commands

from .config import AI_NAMES
from . import db
from .render import make_board_text
from .ui import HelpButton, HELP_TEXT
from .views import RoomSettingsView
from .flow import launch_game, _cleanup, _delete_threads
from .state import (
    _games, _channel_games, _waiting, _room_owners, _room_configs, _user_game,
    _game_tasks, _lobbies, _threads, _thread_game,
)
from .client import bot, tree


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
