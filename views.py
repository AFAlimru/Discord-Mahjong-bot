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
views.py — Discord UI Components (Clean Version)
支持出牌别名：E/S/W/N(风), R(中), Wh(白), G(发)
"""

from __future__ import annotations
from typing import Optional
import asyncio
import discord
from discord.ui import View, Button, button, Modal, TextInput


# ═══════════════════════════════════════════════════════════════════════════════
#  Discard Input Modal (支持别名)
# ═══════════════════════════════════════════════════════════════════════════════

class DiscardInputModal(Modal):
    """出牌输入框 - 支持别名，大小写不敏感"""
    title = "🀫 选择要丢的牌"
    
    tile_name = TextInput(
        label="输入牌名 (5m, 東, e, wh, r；拔北輸入 !n)",
        placeholder="5m",
        min_length=1,
        max_length=3,
    )

    def __init__(self, hand_tiles: list, drawn_tile=None, kita_ok: bool = False, **kwargs):
        super().__init__(**kwargs)
        self.hand_tiles = hand_tiles
        self.drawn_tile = drawn_tile
        self.kita_ok = kita_ok
        self.selected_tile = None
        self.kita_requested = False
        self.submit_event = asyncio.Event()

    async def on_submit(self, interaction: discord.Interaction) -> None:
        """提交时的回调"""
        raw = self.tile_name.value.strip()

        # 拔北（三麻北寶牌）：輸入「拔北」或「!n」
        if raw.lower() in ("!n", "n!", "拔北"):
            if self.kita_ok:
                self.kita_requested = True
                await interaction.response.defer()
                self.submit_event.set()
            else:
                await interaction.response.send_message("❌ 現在無法拔北。", ephemeral=True)
            return

        tile_input = raw.lower()  # 转小写

        # 別名轉換
        aliases = {
            'e': '東',      # East
            's': '南',      # South
            'w': '西',      # West
            'n': '北',      # North
            'r': '中',      # Red/中
            'wh': '白',     # White/白
            'g': '發',      # Green/發
        }
        
        # 检查是否是別名
        if tile_input in aliases:
            tile_input = aliases[tile_input]
        
        # 尝试在手牌中找到匹配的牌
        found = None
        
        # 直接匹配摸牌
        if self.drawn_tile:
            drawn_short = self.drawn_tile.short.lower()
            if drawn_short == tile_input:
                found = self.drawn_tile
        
        # 在手牌中寻找
        if not found:
            for tile in self.hand_tiles:
                tile_short = tile.short.lower()
                if tile_short == tile_input:
                    found = tile
                    break
        
        if found:
            self.selected_tile = found
            await interaction.response.defer()
            self.submit_event.set()
        else:
            await interaction.response.send_message(f"❌ 找不到牌：{tile_input}", ephemeral=True)


# ═══════════════════════════════════════════════════════════════════════════════
#  Discard View
# ═══════════════════════════════════════════════════════════════════════════════

class DiscardView(View):
    """出牌选择 - 按按钮弹出输入框"""
    
    def __init__(self, player_id: str, hand: list, drawn = None, timeout: float = 30):
        super().__init__(timeout=timeout)
        self.player_id = player_id
        self.hand_tiles = hand
        self.drawn_tile = drawn
        self.selected_tile = None
        self.timed_out = False

    @button(label="选择出牌", style=discord.ButtonStyle.primary)
    async def choose_discard(self, interaction: discord.Interaction, button: Button):
        """点击后弹出输入框"""
        if str(interaction.user.id) != self.player_id:
            await interaction.response.send_message("❌ 这不是你的回合!", ephemeral=True)
            return
        
        modal = DiscardInputModal(self.hand_tiles, self.drawn_tile)
        await interaction.response.send_modal(modal)
        
        try:
            await asyncio.wait_for(modal.submit_event.wait(), timeout=120)
            if modal.selected_tile:
                self.selected_tile = modal.selected_tile
        except asyncio.TimeoutError:
            self.timed_out = True
        
        self.stop()

    async def on_timeout(self):
        self.timed_out = True
        self.stop()


# ═══════════════════════════════════════════════════════════════════════════════
#  Room Settings
# ═══════════════════════════════════════════════════════════════════════════════

class RoomSettingsModal(Modal):
    """房间设定对话框：思考秒數 + 起始點數"""
    title = "⚙️ 數值設定"

    thinking_time = TextInput(
        label="思考秒数 (5-300)",
        placeholder="25",
        min_length=1,
        max_length=3,
    )
    start_points = TextInput(
        label="起始點數 (1~1000000，留空=預設)",
        placeholder="四麻預設 25000、三麻預設 35000",
        required=False,
        max_length=7,
    )

    def __init__(self, config: dict = None, **kwargs):
        super().__init__(**kwargs)
        self.config = config or {}
        self.success = False

    async def on_submit(self, interaction: discord.Interaction) -> None:
        """提交时的回调"""
        try:
            tt = int(self.thinking_time.value)
        except ValueError:
            await interaction.response.send_message("❌ 秒数应为数字", ephemeral=True)
            return
        if not (5 <= tt <= 300):
            await interaction.response.send_message("❌ 秒数应在 5-300 之间", ephemeral=True)
            return

        sp_raw = self.start_points.value.strip()
        sp = None
        if sp_raw:
            try:
                sp = int(sp_raw)
            except ValueError:
                await interaction.response.send_message("❌ 起始點數应为数字", ephemeral=True)
                return
            if not (0 <= sp <= 1000000):
                await interaction.response.send_message("❌ 起始點數需介於 0 ~ 1000000", ephemeral=True)
                return

        if self.config is not None:
            self.config["thinking_time"] = tt
            self.config["start_points"] = sp   # None = 用預設

        self.success = True
        pt_txt = f"{sp} 點" if sp is not None else "預設"
        await interaction.response.send_message(f"✅ 已設定：思考 {tt} 秒、起始 {pt_txt}", ephemeral=True)


class RoomSettingsView(View):
    """房间设定按钮菜单"""
    
    def __init__(self, gid: str, timeout: float = 300):
        super().__init__(timeout=timeout)
        self.game_id = gid
        self.thinking_time = 25
        self.start_points = None   # None=依模式預設
        self.open_hand = False
        self.sanma = False
        self.hanchan = False   # False=東風戰, True=半莊戰
        self.tobi = True       # 擊飛：點數為負即結束（預設開啟）
        self.ruleset = "mixed"  # "mixed"=混合式 / "tenhou"=完全天鳳式

    @button(label="⚙️ 數值設定", style=discord.ButtonStyle.primary)
    async def modify_time(self, interaction: discord.Interaction, button: Button):
        modal = RoomSettingsModal(self.__dict__)
        await interaction.response.send_modal(modal)

    @button(label="👥 三麻模式", style=discord.ButtonStyle.secondary)
    async def toggle_sanma(self, interaction: discord.Interaction, button: Button):
        self.sanma = not self.sanma
        state = "开启" if self.sanma else "关闭"
        button.label = f"👥 三麻模式({state})"
        await interaction.response.edit_message(view=self)

    @button(label="🀄 戰長：東風戰", style=discord.ButtonStyle.secondary)
    async def toggle_length(self, interaction: discord.Interaction, button: Button):
        self.hanchan = not self.hanchan
        button.label = "🀄 戰長：半莊戰" if self.hanchan else "🀄 戰長：東風戰"
        await interaction.response.edit_message(view=self)

    @button(label="💥 擊飛：開啟", style=discord.ButtonStyle.secondary)
    async def toggle_tobi(self, interaction: discord.Interaction, button: Button):
        self.tobi = not self.tobi
        button.label = "💥 擊飛：開啟" if self.tobi else "💥 擊飛：關閉"
        await interaction.response.edit_message(view=self)

    @button(label="📐 規則：預設", style=discord.ButtonStyle.secondary)
    async def toggle_ruleset(self, interaction: discord.Interaction, button: Button):
        self.ruleset = "tenhou" if self.ruleset == "mixed" else "mixed"
        button.label = "📐 規則：完全天鳳式" if self.ruleset == "tenhou" else "📐 規則：預設"
        await interaction.response.edit_message(view=self)

    @button(label="✅ 确认", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: Button):
        await interaction.response.defer()
        self.stop()


# ═══════════════════════════════════════════════════════════════════════════════
#  Reaction View (简化)
# ═══════════════════════════════════════════════════════════════════════════════

class ReactionView(View):
    """副露反应窗口（已废弃，保留供兼容）"""
    
    def __init__(self, timeout: float = 5):
        super().__init__(timeout=timeout)
        self.reaction = None

    @button(label="榮", style=discord.ButtonStyle.success)
    async def btn_ron(self, interaction: discord.Interaction, button: Button):
        self.reaction = "ron"
        self.stop()
        await interaction.response.defer()

    @button(label="碰", style=discord.ButtonStyle.primary)
    async def btn_pon(self, interaction: discord.Interaction, button: Button):
        self.reaction = "pon"
        self.stop()
        await interaction.response.defer()

    @button(label="吃", style=discord.ButtonStyle.success)
    async def btn_chi(self, interaction: discord.Interaction, button: Button):
        self.reaction = "chi"
        self.stop()
        await interaction.response.defer()

    @button(label="杠", style=discord.ButtonStyle.primary)
    async def btn_kan(self, interaction: discord.Interaction, button: Button):
        self.reaction = "kan"
        self.stop()
        await interaction.response.defer()

    @button(label="跳过", style=discord.ButtonStyle.secondary)
    async def btn_pass(self, interaction: discord.Interaction, button: Button):
        self.reaction = "pass"
        self.stop()
        await interaction.response.defer()


# ═══════════════════════════════════════════════════════════════════════════════
#  Other Views (保留)
# ═══════════════════════════════════════════════════════════════════════════════

class DiscardActionView(View):
    """出牌时的行动按钮"""
    
    def __init__(self, timeout: float = 30):
        super().__init__(timeout=timeout)
        self.action = None

    @button(label="立直", style=discord.ButtonStyle.success)
    async def btn_riichi(self, interaction: discord.Interaction, button: Button):
        self.action = "riichi"
        self.stop()
        await interaction.response.defer()

    @button(label="暗杠", style=discord.ButtonStyle.primary)
    async def btn_ankan(self, interaction: discord.Interaction, button: Button):
        self.action = "ankan"
        self.stop()
        await interaction.response.defer()

    @button(label="自摸", style=discord.ButtonStyle.success)
    async def btn_tsumo(self, interaction: discord.Interaction, button: Button):
        self.action = "tsumo"
        self.stop()
        await interaction.response.defer()

    @button(label="出牌", style=discord.ButtonStyle.primary)
    async def btn_discard(self, interaction: discord.Interaction, button: Button):
        self.action = "discard"
        self.stop()
        await interaction.response.defer()


class ChiSelectView(View):
    """吃牌选择"""
    
    def __init__(self, options: list[tuple], timeout: float = 20):
        super().__init__(timeout=timeout)
        self.chi_options = options
        self.selected: Optional[tuple] = None
        
        for i, (t1, t2) in enumerate(options):
            btn = Button(label=f"{t1.short}{t2.short}", style=discord.ButtonStyle.success)
            btn.callback = self._make_callback(i)
            self.add_item(btn)

    def _make_callback(self, idx):
        async def callback(interaction: discord.Interaction):
            self.selected = self.chi_options[idx]
            self.stop()
            await interaction.response.defer()
        return callback


class KanSelectView(View):
    """杠牌选择"""
    
    def __init__(self, kan_options: list, timeout: float = 20):
        super().__init__(timeout=timeout)
        self.kan_options = kan_options
        self.selected = None
        
        for i, tile in enumerate(kan_options):
            btn = Button(label=f"杠{tile.short}", style=discord.ButtonStyle.primary)
            btn.callback = self._make_callback(tile)
            self.add_item(btn)

    def _make_callback(self, tile):
        async def callback(interaction: discord.Interaction):
            self.selected = tile
            self.stop()
            await interaction.response.defer()
        return callback


class BoardRefreshView(View):
    """牌局面板按钮"""
    
    def __init__(self, game_state_getter=None, player_id: str = "", timeout: float = 600):
        super().__init__(timeout=timeout)
        self.game_state_getter = game_state_getter
        self.player_id = player_id

    @button(label="看我的牌", style=discord.ButtonStyle.primary)
    async def show_hand(self, interaction: discord.Interaction, button: Button):
        if str(interaction.user.id) != self.player_id:
            await interaction.response.send_message("❌ 这不是你的牌局!", ephemeral=True)
            return
        
        if not self.game_state_getter:
            await interaction.response.send_message("❌ 牌局已结束", ephemeral=True)
            return
        
        gs = self.game_state_getter()
        if not gs:
            await interaction.response.send_message("❌ 牌局已结束", ephemeral=True)
            return
        
        player = next((p for p in gs.players if p.user_id == self.player_id), None)
        if not player:
            await interaction.response.send_message("❌ 找不到你", ephemeral=True)
            return
        
        # 显示两种格式
        unicode_display, name_display = player.hand_display_with_names()
        
        # 单独显示摸牌（直接接在冒号后）
        drawn_info = ""
        if player.drawn_tile:
            drawn_info = f"\n\n🎲 **剛摸到：{player.drawn_tile}**"

        msg = (
            f"**你的手牌**\n"
            f"# {unicode_display}\n"
            f"{name_display}{drawn_info}\n\n"
            f"丟完牌時，請按【刪除這則訊息】\n"
            f"下次摸牌時才會顯示正確的"
        )

        await interaction.response.send_message(msg, ephemeral=True)

    @button(label="公平性验证", style=discord.ButtonStyle.secondary)
    async def show_fairness(self, interaction: discord.Interaction, button: Button):
        if not self.game_state_getter:
            await interaction.response.send_message("❌ 牌局已结束", ephemeral=True)
            return
        
        gs = self.game_state_getter()
        if not gs:
            await interaction.response.send_message("❌ 牌局已结束", ephemeral=True)
            return
        
        embed = discord.Embed(
            title="🔐 牌山公平性驗證",
            color=0x00AA88,
        )
        embed.add_field(
            name="本局 SHA-256（承諾值）",
            value=f"```\n{gs.wall_sha256}\n```",
            inline=False,
        )
        embed.add_field(
            name="說明",
            value=(
                "對局進行中僅公開 SHA-256，對局結束後會公開完整牌山編碼，"
                "可貼到任意 SHA-256 網站校驗是否一致。\n"
                "編碼規則：1~9m/p/s（紅5=0），字牌 1~7z＝東南西北白發中。"
            ),
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)