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
支持出牌別名：E/S/W/N(風), R(中), Wh(白), G(發)
"""

from __future__ import annotations
from typing import Optional
import asyncio
import discord
from discord.ui import View, Button, button, Modal, TextInput

from . import i18n


# ═══════════════════════════════════════════════════════════════════════════════
#  Discard Input Modal (支持別名)
# ═══════════════════════════════════════════════════════════════════════════════

class DiscardInputModal(Modal):
    """出牌輸入框 - 支持別名，大小寫不敏感"""
    title = "🀫 選擇要丟的牌"
    
    tile_name = TextInput(
        label="輸入牌名 (5m, 東, e, wh, r；拔北輸入 !n)",
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
        """提交時的回調"""
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

        tile_input = raw.lower()  # 轉小寫

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
        
        # 檢查是否是別名
        if tile_input in aliases:
            tile_input = aliases[tile_input]
        
        # 嘗試在手牌中找到匹配的牌
        found = None
        
        # 直接匹配摸牌
        if self.drawn_tile:
            drawn_short = self.drawn_tile.short.lower()
            if drawn_short == tile_input:
                found = self.drawn_tile
        
        # 在手牌中尋找
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
    """出牌選擇 - 按按鈕彈出輸入框"""
    
    def __init__(self, player_id: str, hand: list, drawn = None, timeout: float = 30):
        super().__init__(timeout=timeout)
        self.player_id = player_id
        self.hand_tiles = hand
        self.drawn_tile = drawn
        self.selected_tile = None
        self.timed_out = False

    @button(label="選擇出牌", style=discord.ButtonStyle.primary)
    async def choose_discard(self, interaction: discord.Interaction, button: Button):
        """點擊後彈出輸入框"""
        if str(interaction.user.id) != self.player_id:
            await interaction.response.send_message("❌ 這不是你的回合!", ephemeral=True)
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
    """房間設定對話框：思考秒數 + 起始點數"""

    def __init__(self, config: dict = None, lang: str = i18n.DEFAULT, **kwargs):
        super().__init__(title=i18n.t("settings.numbers_title", lang), **kwargs)
        self.config = config or {}
        self.lang = lang
        self.success = False
        # 在 __init__ 直接用翻譯好的標籤建立欄位（避免事後設定 .label 觸發棄用警告）
        self.thinking_time = TextInput(
            label=i18n.t("settings.thinking_label", lang),
            placeholder="25", min_length=1, max_length=3,
        )
        self.start_points = TextInput(
            label=i18n.t("settings.points_label", lang),
            placeholder=i18n.t("settings.points_ph", lang),
            required=False, max_length=7,
        )
        self.add_item(self.thinking_time)
        self.add_item(self.start_points)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        """提交時的回調"""
        lang = self.lang
        try:
            tt = int(self.thinking_time.value)
        except ValueError:
            await interaction.response.send_message(i18n.t("err.seconds_num", lang), ephemeral=True)
            return
        if not (5 <= tt <= 300):
            await interaction.response.send_message(i18n.t("err.seconds_range", lang), ephemeral=True)
            return

        sp_raw = self.start_points.value.strip()
        sp = None
        if sp_raw:
            try:
                sp = int(sp_raw)
            except ValueError:
                await interaction.response.send_message(i18n.t("err.points_num", lang), ephemeral=True)
                return
            if not (0 <= sp <= 1000000):
                await interaction.response.send_message(i18n.t("err.points_range", lang), ephemeral=True)
                return

        if self.config is not None:
            self.config["thinking_time"] = tt
            self.config["start_points"] = sp   # None = 用預設

        self.success = True
        pt_txt = i18n.t("win.points", lang, n=sp) if sp is not None else i18n.t("settings.default", lang)
        await interaction.response.send_message(
            i18n.t("settings.saved", lang, tt=tt, pt=pt_txt), ephemeral=True)


class RoomSettingsView(View):
    """房間設定按鈕菜單"""
    
    def __init__(self, gid: str, lang: str = i18n.DEFAULT, timeout: float = 300):
        super().__init__(timeout=timeout)
        self.game_id = gid
        self.lang = lang
        self.thinking_time = 25
        self.start_points = None   # None=依模式預設
        self.open_hand = False
        self.sanma = False
        self.hanchan = False   # False=東風戰, True=半莊戰
        self.tobi = True       # 擊飛：點數為負即結束（預設開啟）
        self.ruleset = "mixed"  # "mixed"=混合式 / "tenhou"=完全天鳳式
        self.kuikae = False        # 食替：預設禁止（False=禁止）
        self.open_riichi = False   # 開立直：預設關閉
        # 依語言設定按鈕標籤（children 依宣告順序：數值/三麻/戰長/擊飛/規則/食替/開立直/確認）
        for child, key in zip(self.children, ("settings.numbers", "settings.sanma",
                                              "settings.length_tonpuu", "settings.tobi_on",
                                              "settings.rule_default",
                                              "settings.kuikae_off", "settings.openriichi_off",
                                              "settings.confirm")):
            child.label = i18n.t(key, lang)

    @button(label="⚙️ 數值設定", style=discord.ButtonStyle.primary)
    async def modify_time(self, interaction: discord.Interaction, button: Button):
        modal = RoomSettingsModal(self.__dict__, self.lang)
        await interaction.response.send_modal(modal)

    @button(label="👥 三麻模式", style=discord.ButtonStyle.secondary)
    async def toggle_sanma(self, interaction: discord.Interaction, button: Button):
        self.sanma = not self.sanma
        state = i18n.t("toggle.on" if self.sanma else "toggle.off", self.lang)
        button.label = i18n.t("settings.sanma_state", self.lang, state=state)
        await interaction.response.edit_message(view=self)

    @button(label="🀄 戰長：東風戰", style=discord.ButtonStyle.secondary)
    async def toggle_length(self, interaction: discord.Interaction, button: Button):
        self.hanchan = not self.hanchan
        button.label = i18n.t("settings.length_hanchan" if self.hanchan else "settings.length_tonpuu", self.lang)
        await interaction.response.edit_message(view=self)

    @button(label="💥 擊飛：開啟", style=discord.ButtonStyle.secondary)
    async def toggle_tobi(self, interaction: discord.Interaction, button: Button):
        self.tobi = not self.tobi
        button.label = i18n.t("settings.tobi_on" if self.tobi else "settings.tobi_off", self.lang)
        await interaction.response.edit_message(view=self)

    @button(label="📐 規則：預設", style=discord.ButtonStyle.secondary)
    async def toggle_ruleset(self, interaction: discord.Interaction, button: Button):
        self.ruleset = "tenhou" if self.ruleset == "mixed" else "mixed"
        button.label = i18n.t("settings.rule_tenhou" if self.ruleset == "tenhou" else "settings.rule_default", self.lang)
        await interaction.response.edit_message(view=self)

    @button(label="🚫 食替：禁止", style=discord.ButtonStyle.secondary)
    async def toggle_kuikae(self, interaction: discord.Interaction, button: Button):
        self.kuikae = not self.kuikae
        button.label = i18n.t("settings.kuikae_on" if self.kuikae else "settings.kuikae_off", self.lang)
        await interaction.response.edit_message(view=self)

    @button(label="👁 開立直：關閉", style=discord.ButtonStyle.secondary)
    async def toggle_open_riichi(self, interaction: discord.Interaction, button: Button):
        self.open_riichi = not self.open_riichi
        button.label = i18n.t("settings.openriichi_on" if self.open_riichi else "settings.openriichi_off", self.lang)
        await interaction.response.edit_message(view=self)

    @button(label="✅ 確認", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: Button):
        await interaction.response.defer()
        try:                                   # 確認後把設定面板整則刪掉（不留死按鈕）
            await interaction.message.delete()
        except Exception:
            try:                               # ephemeral 面板（/mahjong start）走這條刪
                await interaction.delete_original_response()
            except Exception:
                pass
        self.stop()


# ═══════════════════════════════════════════════════════════════════════════════
#  Reaction View (簡化)
# ═══════════════════════════════════════════════════════════════════════════════

class ReactionView(View):
    """副露反應窗口（已廢棄，保留供兼容）"""
    
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

    @button(label="槓", style=discord.ButtonStyle.primary)
    async def btn_kan(self, interaction: discord.Interaction, button: Button):
        self.reaction = "kan"
        self.stop()
        await interaction.response.defer()

    @button(label="跳過", style=discord.ButtonStyle.secondary)
    async def btn_pass(self, interaction: discord.Interaction, button: Button):
        self.reaction = "pass"
        self.stop()
        await interaction.response.defer()


# ═══════════════════════════════════════════════════════════════════════════════
#  Other Views (保留)
# ═══════════════════════════════════════════════════════════════════════════════

class DiscardActionView(View):
    """出牌時的行動按鈕"""
    
    def __init__(self, timeout: float = 30):
        super().__init__(timeout=timeout)
        self.action = None

    @button(label="立直", style=discord.ButtonStyle.success)
    async def btn_riichi(self, interaction: discord.Interaction, button: Button):
        self.action = "riichi"
        self.stop()
        await interaction.response.defer()

    @button(label="暗槓", style=discord.ButtonStyle.primary)
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
    """吃牌選擇"""
    
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
    """槓牌選擇"""
    
    def __init__(self, kan_options: list, timeout: float = 20):
        super().__init__(timeout=timeout)
        self.kan_options = kan_options
        self.selected = None
        
        for i, tile in enumerate(kan_options):
            btn = Button(label=f"槓{tile.short}", style=discord.ButtonStyle.primary)
            btn.callback = self._make_callback(tile)
            self.add_item(btn)

    def _make_callback(self, tile):
        async def callback(interaction: discord.Interaction):
            self.selected = tile
            self.stop()
            await interaction.response.defer()
        return callback


class BoardRefreshView(View):
    """牌局面板按鈕"""
    
    def __init__(self, game_state_getter=None, player_id: str = "", timeout: float = 600):
        super().__init__(timeout=timeout)
        self.game_state_getter = game_state_getter
        self.player_id = player_id

    @button(label="看我的牌", style=discord.ButtonStyle.primary)
    async def show_hand(self, interaction: discord.Interaction, button: Button):
        if str(interaction.user.id) != self.player_id:
            await interaction.response.send_message("❌ 這不是你的牌局!", ephemeral=True)
            return
        
        if not self.game_state_getter:
            await interaction.response.send_message("❌ 牌局已結束", ephemeral=True)
            return
        
        gs = self.game_state_getter()
        if not gs:
            await interaction.response.send_message("❌ 牌局已結束", ephemeral=True)
            return
        
        player = next((p for p in gs.players if p.user_id == self.player_id), None)
        if not player:
            await interaction.response.send_message("❌ 找不到你", ephemeral=True)
            return
        
        # 顯示兩種格式
        unicode_display, name_display = player.hand_display_with_names()
        
        # 單獨顯示摸牌（直接接在冒號後）
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

    @button(label="公平性驗證", style=discord.ButtonStyle.secondary)
    async def show_fairness(self, interaction: discord.Interaction, button: Button):
        if not self.game_state_getter:
            await interaction.response.send_message("❌ 牌局已結束", ephemeral=True)
            return
        
        gs = self.game_state_getter()
        if not gs:
            await interaction.response.send_message("❌ 牌局已結束", ephemeral=True)
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