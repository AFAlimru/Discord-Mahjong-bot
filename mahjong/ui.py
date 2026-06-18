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
"""Discord UI 元件：資訊按鈕（牌河/點數/副露/動態/說明）、公平性驗證、操作說明文字。"""
from __future__ import annotations
import discord

from .render import (
    make_river_text, make_score_text, make_meld_text, make_action_log_text,
    make_thread_board,
)
from .state import _games
from . import db
from . import i18n

class HandHelpButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="❓ 說明", style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_message(HELP_TEXT, ephemeral=True)


class _GameInfoButton(discord.ui.Button):
    """共用：點擊顯示某種牌局資訊（私密，依點擊者語言）。"""
    def __init__(self, gid: str, label: str, builder):
        super().__init__(label=label, style=discord.ButtonStyle.secondary)
        self.gid = gid
        self._builder = builder

    async def callback(self, interaction: discord.Interaction):
        gs = _games.get(self.gid)
        if not gs:
            await interaction.response.send_message("❌ 牌局已結束。", ephemeral=True)
            return
        lang = i18n.get_user_lang(interaction.user.id)
        await interaction.response.send_message(self._builder(gs, lang), ephemeral=True)


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
        lang = i18n.get_user_lang(interaction.user.id)
        await interaction.response.send_message(make_action_log_text(self.gid, lang), ephemeral=True)


def ActionLogButton(gid: str):
    return _ActionLogButton(gid)


class TranslateBoardButton(discord.ui.Button):
    """🌐 翻譯：用點擊者的語言、僅自己可見地顯示目前牌桌。"""
    def __init__(self, gid: str):
        super().__init__(label="🌐 翻譯 / 翻訳", style=discord.ButtonStyle.secondary)
        self.gid = gid

    async def callback(self, interaction: discord.Interaction):
        gs = _games.get(self.gid)
        if not gs:
            await interaction.response.send_message("❌ 牌局已結束。", ephemeral=True)
            return
        lang = i18n.get_user_lang(interaction.user.id)
        await interaction.response.send_message(make_thread_board(gs, "", lang), ephemeral=True)


def make_board_view(gid: str) -> discord.ui.View:
    """公開牌桌（觀戰）按鈕：🌐 翻譯 + 公平性驗證。"""
    v = discord.ui.View(timeout=None)
    v.add_item(TranslateBoardButton(gid))
    v.add_item(FairnessButton(gid))
    return v


def make_hand_view(gid: str) -> discord.ui.View:
    """手牌面板常駐按鈕：看牌河 / 看點數 / 看副露 / 看動態，說明放最右。"""
    v = discord.ui.View(timeout=None)
    v.add_item(RiverButton(gid))
    v.add_item(ScoreButton(gid))
    v.add_item(MeldButton(gid))
    v.add_item(ActionLogButton(gid))
    v.add_item(HandHelpButton())
    return v



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
