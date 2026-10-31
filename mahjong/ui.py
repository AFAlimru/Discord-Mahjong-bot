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
import io

import discord

from .render import (
    make_river_text, make_score_text, make_meld_text, make_action_log_text,
    make_thread_board,
)
from .state import _games, _auto_modes
from . import db
from . import i18n

class HandHelpButton(discord.ui.Button):
    def __init__(self, lang: str = i18n.DEFAULT):
        super().__init__(label=i18n.t("btn.help", lang), style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            help_text(i18n.get_user_lang(interaction.user.id, interaction.guild_id, interaction.channel)), ephemeral=True)


class _GameInfoButton(discord.ui.Button):
    """共用：點擊顯示某種牌局資訊（私密，依點擊者語言）。"""
    def __init__(self, gid: str, label: str, builder):
        super().__init__(label=label, style=discord.ButtonStyle.secondary)
        self.gid = gid
        self._builder = builder

    async def callback(self, interaction: discord.Interaction):
        gs = _games.get(self.gid)
        if not gs:
            await interaction.response.send_message(
                i18n.t("msg.game_ended", i18n.get_user_lang(interaction.user.id, interaction.guild_id, interaction.channel)), ephemeral=True)
            return
        lang = i18n.get_user_lang(interaction.user.id, interaction.guild_id, interaction.channel)
        await interaction.response.send_message(self._builder(gs, lang), ephemeral=True)


def RiverButton(gid: str, lang: str = i18n.DEFAULT):
    return _GameInfoButton(gid, i18n.t("btn.river", lang), make_river_text)


def ScoreButton(gid: str, lang: str = i18n.DEFAULT):
    return _GameInfoButton(gid, i18n.t("btn.score", lang), make_score_text)


def MeldButton(gid: str, lang: str = i18n.DEFAULT):
    return _GameInfoButton(gid, i18n.t("btn.meld", lang), make_meld_text)


class _ActionLogButton(discord.ui.Button):
    """點擊顯示本局完整動態（私密）。"""
    def __init__(self, gid: str, lang: str = i18n.DEFAULT):
        super().__init__(label=i18n.t("btn.log", lang), style=discord.ButtonStyle.secondary)
        self.gid = gid

    async def callback(self, interaction: discord.Interaction):
        lang = i18n.get_user_lang(interaction.user.id, interaction.guild_id, interaction.channel)
        await interaction.response.send_message(make_action_log_text(self.gid, lang), ephemeral=True)


def ActionLogButton(gid: str, lang: str = i18n.DEFAULT):
    return _ActionLogButton(gid, lang)


class TranslateBoardButton(discord.ui.Button):
    """🌐 翻譯：用點擊者的語言、僅自己可見地顯示目前牌桌。
    標籤列出「牌桌顯示語言以外」的語言。"""
    def __init__(self, gid: str, default_lang: str = i18n.DEFAULT):
        super().__init__(label=i18n.translate_label(default_lang), style=discord.ButtonStyle.secondary)
        self.gid = gid

    async def callback(self, interaction: discord.Interaction):
        gs = _games.get(self.gid)
        if not gs:
            await interaction.response.send_message(
                i18n.t("msg.game_ended", i18n.get_user_lang(interaction.user.id, interaction.guild_id, interaction.channel)), ephemeral=True)
            return
        lang = i18n.get_user_lang(interaction.user.id, interaction.guild_id, interaction.channel)
        await interaction.response.send_message(make_thread_board(gs, "", lang), ephemeral=True)


def make_board_view(gid: str, default_lang: str = i18n.DEFAULT) -> discord.ui.View:
    """公開牌桌（觀戰）按鈕：🌐 翻譯。（牌山承諾值改用 /mahjong fairness 查）"""
    v = discord.ui.View(timeout=None)
    v.add_item(TranslateBoardButton(gid, default_lang))
    return v


# ── ⚙️ 自動操作：自動和牌／自動拔北／不吃碰槓／自動摸切（每位玩家各自的設定，整場牌局有效）──
# 顯示順序＝選單與面板標記的順序；(key, 選單名稱鍵, 說明鍵, 面板短名鍵, 是否只有三麻)
AUTO_OPTIONS = (
    ("giri",   "auto.giri",   "auto.giri_desc",   "auto.s_giri",   False),
    ("win",    "auto.win",    "auto.win_desc",    "auto.s_win",    False),
    ("kita",   "auto.kita",   "auto.kita_desc",   "auto.s_kita",   True),
    ("nocall", "auto.nocall", "auto.nocall_desc", "auto.s_nocall", False),
)


def auto_flags(gid: str, uid) -> dict:
    """某玩家在這場牌局的自動操作設定（沒設過＝空 dict；只讀，不會建立紀錄）。"""
    return _auto_modes.get((gid, str(uid))) or {}


def auto_marker(gid: str, uid, lang: str = i18n.DEFAULT) -> str:
    """手牌面板上的小標記，例如「🤖 自動：摸切／和牌」；沒開任何自動就回空字串。"""
    fl = auto_flags(gid, uid)
    names = [i18n.t(short, lang) for key, _n, _d, short, _s in AUTO_OPTIONS if fl.get(key)]
    return i18n.t("auto.marker", lang, modes="／".join(names)) if names else ""


class _AutoToggle(discord.ui.Button):
    """自動選單裡的一顆開關鈕：按一下切換並就地更新選單。"""
    def __init__(self, menu: "AutoMenuView", key: str, name_key: str):
        on = bool(auto_flags(menu.gid, menu.uid).get(key))
        super().__init__(label=f"{'✅' if on else '⬜'} {i18n.t(name_key, menu.lang)}",
                         style=discord.ButtonStyle.success if on else discord.ButtonStyle.secondary)
        self.menu, self.key, self.name_key = menu, key, name_key

    async def callback(self, interaction: discord.Interaction):
        m = self.menu
        cur = _auto_modes.setdefault((m.gid, m.uid), {})
        cur[self.key] = not cur.get(self.key, False)
        note = i18n.t("auto.on_msg" if cur[self.key] else "auto.off_msg", m.lang,
                      name=i18n.t(self.name_key, m.lang))
        await interaction.response.edit_message(content=m.body(note), view=m.rebuilt())


class AutoMenuView(discord.ui.View):
    """⚙️ 自動 的私密選單（四顆開關；四麻不顯示「自動拔北」）。"""
    def __init__(self, gid: str, uid: str, lang: str, sanma: bool):
        super().__init__(timeout=600)
        self.gid, self.uid, self.lang, self.sanma = gid, uid, lang, sanma
        for key, name_key, _d, _s, sanma_only in AUTO_OPTIONS:
            if sanma_only and not sanma:
                continue
            self.add_item(_AutoToggle(self, key, name_key))

    def rebuilt(self) -> "AutoMenuView":
        return AutoMenuView(self.gid, self.uid, self.lang, self.sanma)

    def body(self, note: str = "") -> str:
        fl = auto_flags(self.gid, self.uid)
        lines = [note] if note else []
        lines.append(i18n.t("auto.title", self.lang))
        for key, name_key, desc_key, _s, sanma_only in AUTO_OPTIONS:
            if sanma_only and not self.sanma:
                continue
            lines.append(f"{'✅' if fl.get(key) else '⬜'} **{i18n.t(name_key, self.lang)}**：{i18n.t(desc_key, self.lang)}")
        return "\n".join(lines)


class AutoButton(discord.ui.Button):
    """⚙️ 自動：打開自己的自動操作設定（私密選單，只有這場牌局的玩家能用）。"""
    def __init__(self, gid: str, lang: str = i18n.DEFAULT):
        super().__init__(label=i18n.t("btn.auto", lang), style=discord.ButtonStyle.secondary)
        self.gid = gid

    async def callback(self, interaction: discord.Interaction):
        lang = i18n.get_user_lang(interaction.user.id, interaction.guild_id, interaction.channel)
        gs = _games.get(self.gid)
        if not gs:
            await interaction.response.send_message(i18n.t("msg.game_ended", lang), ephemeral=True)
            return
        uid = str(interaction.user.id)
        if not any(p.user_id == uid and not p.is_bot for p in gs.players):
            await interaction.response.send_message(i18n.t("auto.players_only", lang), ephemeral=True)
            return
        menu = AutoMenuView(self.gid, uid, lang, gs.is_sanma)
        await interaction.response.send_message(menu.body(), view=menu, ephemeral=True)


def make_hand_view(gid: str, lang: str = i18n.DEFAULT) -> discord.ui.View:
    """手牌面板常駐按鈕：看點數 / 看副露 / 看動態，說明、⚙️ 自動放最右（牌河已獨立成訊息）。"""
    v = discord.ui.View(timeout=None)
    v.add_item(ScoreButton(gid, lang))
    v.add_item(MeldButton(gid, lang))
    v.add_item(ActionLogButton(gid, lang))
    v.add_item(HandHelpButton(lang))
    v.add_item(AutoButton(gid, lang))
    return v



_WALL_NAMES = {"riichi_city": "麻雀一番街方式（secrets＋Fisher–Yates）",
               "tenhou": "天鳳方式（MT19937＋SHA-512，<https://tenhou.net/stat/rand/>）"}


async def send_fairness(interaction: discord.Interaction, gid: str) -> None:
    """回覆某局的牌山承諾值（進行中只給 SHA-256；已結束附完整牌山編碼，天鳳方式另附種子檔）。按鈕與指令共用。"""
    gs = _games.get(gid)
    if gs:
        # 對局進行中：只給 SHA-256，避免提前洩漏後面的牌
        embed = discord.Embed(title="🔐 牌山公平性驗證", color=0x00AA88)
        embed.add_field(name="牌山生成方式", value=_WALL_NAMES.get(gs.wall_method, gs.wall_method),
                        inline=False)
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
        game = db.get_game(gid)
        if not game or not game.get("game_data"):
            await interaction.response.send_message("❌ 找不到該局牌譜。", ephemeral=True)
            return
        data     = game["game_data"]
        encoding = data.get("wall_seed", "")
        sha      = data.get("wall_sha256", "")
        method   = data.get("wall_method", "riichi_city")
        rng_seed = data.get("wall_rng_seed", "")
        embed = discord.Embed(title="🔐 牌山公平性驗證（對局已結束）", color=0x00AA88)
        embed.add_field(name="牌山生成方式", value=_WALL_NAMES.get(method, method), inline=False)
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
        if method == "tenhou" and rng_seed:
            # 天鳳方式：624 個種子太長放不進 embed，改附文字檔；用天鳳範例程式就能算出同一個牌山
            embed.add_field(
                name="天鳳種子（附檔）",
                value=("附檔是這一局的 624 個 32 位元種子（十六進位）。把它當 `init_by_array` 的參數，"
                       "照 <https://tenhou.net/stat/rand/> 的 sample_code.txt（nKyoku=0）"
                       "算出的牌山，換成上面的編碼會完全一致。"),
                inline=False,
            )
            text = ("# Suzume Tsuk - Tenhou wall seed (mt19937ar init_by_array, 624 x uint32, hex)\n"
                    f"# SHA-256 of wall encoding: {sha}\n"
                    f"# wall encoding: {encoding}\n"
                    f"{rng_seed}\n")
            file = discord.File(io.BytesIO(text.encode()), filename=f"tenhou_seed_{gid}.txt")
            await interaction.response.send_message(embed=embed, file=file, ephemeral=True)
            return
        await interaction.response.send_message(embed=embed, ephemeral=True)


def help_text(lang: str = i18n.DEFAULT) -> str:
    """出牌輸入說明（依語言）。"""
    return i18n.t("help.text", lang)


class HelpButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="❓ 說明", style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            help_text(i18n.get_user_lang(interaction.user.id, interaction.guild_id, interaction.channel)), ephemeral=True)


# ═══════════════════════════════════════════════════════════════
