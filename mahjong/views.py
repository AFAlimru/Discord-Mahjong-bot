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
"""views.py — 房間設定面板（RoomSettingsView）與數值設定對話框。"""

from __future__ import annotations
import discord
from discord.ui import View, Button, button, Modal, TextInput

from . import i18n


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
    
    def __init__(self, gid: str, lang: str = i18n.DEFAULT, timeout: float = 300,
                 show_confirm: bool = True):
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
        self.abortive = True       # 途中流局（九種九牌／四風連打／四槓散了／四家立直／三家和）：預設開啟
        # 依語言設定按鈕標籤（children 依宣告順序：數值/三麻/戰長/擊飛/規則/食替/開立直/途中流局/確認）
        for child, key in zip(self.children, ("settings.numbers", "settings.sanma",
                                              "settings.length_tonpuu", "settings.tobi_on",
                                              "settings.rule_default",
                                              "settings.kuikae_off", "settings.openriichi_off",
                                              "settings.abort_on", "settings.confirm")):
            child.label = i18n.t(key, lang)
        if not show_confirm:               # 語音房面板不放確認鈕（開局時面板自動消失）
            try:
                self.remove_item(self.confirm)
            except Exception:
                pass

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
        if str(self.game_id).startswith("vc") and interaction.guild is not None:
            try:                               # 語音房：上限跟著改（三麻 3 人、四麻 4 人）
                vc = interaction.guild.get_channel(int(str(self.game_id)[2:]))
                if vc is not None:
                    from .voice import fit_room_limit
                    await fit_room_limit(vc, 3 if self.sanma else 4, reserve_bot=False)
            except Exception:
                pass

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

    @button(label="🀫 途中流局：開啟", style=discord.ButtonStyle.secondary)
    async def toggle_abortive(self, interaction: discord.Interaction, button: Button):
        # 關閉：九種九牌不能宣告、四風連打／四槓散了／四家立直照打、三家和改為三家都和（流局滿貫不受影響）
        self.abortive = not self.abortive
        button.label = i18n.t("settings.abort_on" if self.abortive else "settings.abort_off", self.lang)
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
