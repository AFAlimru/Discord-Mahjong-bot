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
import re
import discord
from discord.ui import View, Button, button, select, Modal, TextInput

from . import i18n
from . import rulesets


# ═══════════════════════════════════════════════════════════════════════════════
#  Room Settings
# ═══════════════════════════════════════════════════════════════════════════════

class RoomSettingsModal(Modal):
    """房間設定對話框：思考秒數 + 起始點數"""

    def __init__(self, config: dict = None, lang: str = i18n.DEFAULT, panel: "RoomSettingsView | None" = None, **kwargs):
        super().__init__(title=i18n.t("settings.numbers_title", lang), **kwargs)
        self.config = config or {}
        self.lang = lang
        self.panel = panel      # 設定面板：提交後用它刷新面板上的規則摘要（起始點數會影響返點與延長門檻的顯示）
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
        saved = i18n.t("settings.saved", lang, tt=tt, pt=pt_txt)
        if self.panel is not None:
            try:                               # 有面板：先更新面板上的規則摘要，確認訊息改走 followup
                await interaction.response.edit_message(content=self.panel.panel_content(), view=self.panel)
                await interaction.followup.send(saved, ephemeral=True)
                return
            except Exception:
                if interaction.response.is_done():
                    return
        await interaction.response.send_message(saved, ephemeral=True)


def _hard_ai_label(level: str, lang: str) -> str | None:
    """私有進階電腦的設定按鈕文字；沒有私有模組（公開版）就回 None（面板不放這顆按鈕）。"""
    from .flow import hard_available            # 延後匯入，避免模組載入順序問題
    if not hard_available():
        return None
    from . import ai_hard
    return re.sub(r"^[^\w]+", "", ai_hard.setting_label(level, lang))   # 按鈕與文字都不放表情符號


class RoomSettingsView(View):
    """房間設定按鈕菜單"""
    
    def __init__(self, gid: str, lang: str = i18n.DEFAULT, timeout: float = 300,
                 show_confirm: bool = True, base_content: str | None = None):
        super().__init__(timeout=timeout)
        self.game_id = gid
        self.lang = lang
        self.thinking_time = 25
        self.start_points = None   # None=依模式預設
        self.open_hand = False
        self.sanma = False
        self.hanchan = False   # False=東風戰, True=半莊戰
        self.tobi = True       # 擊飛：點數為負即結束（預設開啟）
        self.rules = "default"  # 規則預設（rulesets.PRESETS 的鍵）
        self.rule_over = {}     # 「細項」改過的欄位 {欄位: 值}（只存跟預設不同的）
        self.show_confirm = show_confirm
        self.base_content = base_content   # 面板訊息的開頭文字（後面接規則摘要）；None＝用 settings.prompt
        self.origin = None      # ephemeral 面板的原始互動（細項改完用它更新面板訊息）
        self.panel_message = None   # 公開面板（語音房）的訊息，按「細項」時記下
        self.kuikae = False        # 食替：預設禁止（False=禁止）
        self.open_riichi = False   # 開立直：預設關閉
        self.spectate = True       # 觀戰：預設開放
        self.abortive = True       # 途中流局（九種九牌／四風連打／四槓散了／四家立直／三家和）：預設開啟
        self.wall_method = "riichi_city"   # 牌山生成："riichi_city"=一番街 / "tenhou"=天鳳
        self.ai_level = "normal"           # 電腦種類（私有模組才有其他選項）
        # 私有選用功能：沒有對應模組（公開版）就不放這顆按鈕，ai_level 恆為 normal
        self._has_ai_toggle = _hard_ai_label("normal", lang) is not None
        if not self._has_ai_toggle:
            self.remove_item(self.toggle_ai)
        # 版面（Discord：最多 5 列、每列 5 顆按鈕，下拉選單獨占一列），依意義分組：
        #   第 0 列 規則預設下拉
        #   第 1 列 對局：人數/戰長/數值/牌山/[電腦]
        #   第 2 列 規則：擊飛/途中流局/細項（食替、開立直在細項視窗裡）
        #   第 3 列 確認
        #   語音房另外 add_item 的「與電腦開始」（自動找有空位的列＝第 3 列）與語音包下拉（第 4 列）
        # 開關鈕：開（或不是預設值）＝藍色 primary、關＝灰色 secondary；標籤與顏色都由 _refresh_buttons 依目前狀態設
        self.modify_time.label = i18n.t("settings.numbers", lang)
        self.show_details.label = i18n.t("rules.details_btn", lang)
        self.confirm.label = i18n.t("settings.confirm", lang)
        self._refresh_buttons()
        self.rules_select.placeholder = i18n.t("rules.select_ph", lang)
        self._sync_rules_select()
        if not show_confirm:               # 語音房面板不放確認鈕（開局時面板自動消失）
            try:
                self.remove_item(self.confirm)
            except Exception:
                pass

    # ── 規則預設／細項 ──
    @property
    def ruleset(self) -> str:
        """舊設定相容用（"mixed"／"tenhou"），新程式請讀 rules／rule_over。"""
        return "tenhou" if self.rules == "tenhou" else "mixed"

    def rules_config(self) -> dict:
        """交給 rulesets.resolve 的設定（跟房間設定同樣的鍵）。"""
        return {"rules": self.rules, "rule_over": dict(self.rule_over), "is_sanma": self.sanma,
                "tobi": self.tobi, "abortive": self.abortive}

    def effective_rules(self) -> "rulesets.RuleSet":
        return rulesets.resolve(self.rules_config())

    def base_rules(self) -> "rulesets.RuleSet":
        """目前選的預設（三麻選了沒有三麻規則的預設時＝退回的「預設」）；細項只存跟它不同的值。"""
        rs = rulesets.PRESETS[self.rules]
        if self.sanma and not rs.sanma_ok:
            rs = rulesets.PRESETS["default"]
        return rs

    def panel_content(self) -> str:
        """面板訊息文字：標題／人數・戰長／思考・牌山・電腦／規則名稱（後接跟預設不同的項目）／起始與返點／馬點。"""
        lang = self.lang
        t    = lambda key, **kw: i18n.t(key, lang, **kw)
        base = self.base_content if self.base_content is not None else t("settings.prompt")
        game = t("settings.sum.game", mode=t("mode.sanma" if self.sanma else "mode.yonma"),
                 length=t("len.hanchan" if self.hanchan else "len.tonpuu"))
        opts = t("settings.sum.opts2", tt=self.thinking_time, wall=t(f"settings.wallname.{self.wall_method}"))
        if self._has_ai_toggle:                       # 私有選用功能：沒有對應模組就不顯示
            opts += "・" + _hard_ai_label(self.ai_level, lang)
        rules = rulesets.compact(self.effective_rules(), lang, self.sanma, selected=self.rules,
                                 start_points=self.start_points, hanchan=self.hanchan,
                                 kuikae=self.kuikae, open_riichi=self.open_riichi,
                                 spectate=self.spectate)
        return "\n".join((base, game, opts, *rules))

    def _refresh_buttons(self) -> None:
        """依目前狀態重設所有開關鈕的標籤與顏色（開／非預設＝primary 藍，關＝secondary 灰）。"""
        lang = self.lang
        t    = lambda key, **kw: i18n.t(key, lang, **kw)
        def mark(btn, label: str, on: bool) -> None:
            btn.label = label
            btn.style = discord.ButtonStyle.primary if on else discord.ButtonStyle.secondary
        mark(self.toggle_sanma, t("mode.sanma" if self.sanma else "mode.yonma"), self.sanma)
        mark(self.toggle_length, t("len.hanchan" if self.hanchan else "len.tonpuu"), self.hanchan)
        mark(self.toggle_wall, t("settings.wall_btn", wall=t(f"settings.wallname.{self.wall_method}")),
             self.wall_method != "riichi_city")
        mark(self.toggle_tobi, t("settings.tobi_on" if self.tobi else "settings.tobi_off"), self.tobi)
        mark(self.toggle_abortive, t("settings.abort_on" if self.abortive else "settings.abort_off"), self.abortive)
        if self._has_ai_toggle:
            mark(self.toggle_ai, _hard_ai_label(self.ai_level, lang), self.ai_level != "normal")

    async def _edit(self, interaction: discord.Interaction) -> None:
        """開關按完：刷新按鈕（標籤＋顏色）和面板文字。"""
        self._refresh_buttons()
        await interaction.response.edit_message(content=self.panel_content(), view=self)

    async def refresh_panel(self) -> None:
        """細項（ephemeral 視窗）改完後，更新共用面板訊息上的規則摘要。失敗就算了（下次按面板按鈕會刷新）。"""
        content = self.panel_content()
        for target in (self.origin, self.panel_message):
            if target is None:
                continue
            try:
                if target is self.origin:
                    await target.edit_original_response(content=content)
                else:
                    await target.edit(content=content)
                return
            except Exception:
                continue

    def _sync_toggles_from_rules(self) -> None:
        """擊飛／途中流局按鈕跟著目前生效的預設（之後玩家還能自己切）。"""
        rs = self.base_rules()
        self.tobi = rs.tobi
        self.abortive = bool(rs.abortive)
        self._refresh_buttons()

    def _sync_rules_select(self) -> None:
        """依目前選擇重建下拉選項（標出已選的那個）。"""
        opts = []
        for key, rs in rulesets.PRESETS.items():
            desc = None if rs.sanma_ok else i18n.t("rules.desc_4only", self.lang)
            opts.append(discord.SelectOption(label=rulesets.label(key, self.lang), value=key,
                                             description=desc, default=(key == self.rules)))
        self.rules_select.options = opts

    @select(placeholder="📐 規則", row=0, min_values=1, max_values=1,
            options=[discord.SelectOption(label="預設", value="default")])
    async def rules_select(self, interaction: discord.Interaction, sel: discord.ui.Select):
        key = sel.values[0]
        if key not in rulesets.PRESETS:
            key = "default"
        self.rules = key
        self.rule_over = {}                # 換預設就清掉細項
        self._sync_toggles_from_rules()
        self._sync_rules_select()
        await self._edit(interaction)

    # ── 第 1 列：對局 ──
    @button(label="四麻", style=discord.ButtonStyle.secondary, row=1)
    async def toggle_sanma(self, interaction: discord.Interaction, button: Button):
        self.sanma = not self.sanma
        if not rulesets.PRESETS[self.rules].sanma_ok:     # M聯盟／WRC 三麻退回預設：擊飛／途中流局跟著換
            self._sync_toggles_from_rules()
        await self._edit(interaction)
        if str(self.game_id).startswith("vc") and interaction.guild is not None:
            try:                               # 語音房：上限跟著改（三麻 3 人、四麻 4 人）
                vc = interaction.guild.get_channel(int(str(self.game_id)[2:]))
                if vc is not None:
                    from .voice import fit_room_limit
                    await fit_room_limit(vc, 3 if self.sanma else 4)
            except Exception:
                pass

    @button(label="東風戰", style=discord.ButtonStyle.secondary, row=1)
    async def toggle_length(self, interaction: discord.Interaction, button: Button):
        self.hanchan = not self.hanchan
        await self._edit(interaction)

    @button(label="數值", style=discord.ButtonStyle.secondary, row=1)
    async def modify_time(self, interaction: discord.Interaction, button: Button):
        modal = RoomSettingsModal(self.__dict__, self.lang, panel=self)
        await interaction.response.send_modal(modal)

    @button(label="一番街", style=discord.ButtonStyle.secondary, row=1)
    async def toggle_wall(self, interaction: discord.Interaction, button: Button):
        self.wall_method = "tenhou" if self.wall_method == "riichi_city" else "riichi_city"
        await self._edit(interaction)

    @button(label="普通", style=discord.ButtonStyle.secondary, row=1)
    async def toggle_ai(self, interaction: discord.Interaction, button: Button):
        self.ai_level = "hard" if self.ai_level == "normal" else "normal"
        await self._edit(interaction)

    # ── 第 2 列：規則 ──
    @button(label="擊飛 ✓", style=discord.ButtonStyle.primary, row=2)
    async def toggle_tobi(self, interaction: discord.Interaction, button: Button):
        self.tobi = not self.tobi
        await self._edit(interaction)

    @button(label="途中流局 ✓", style=discord.ButtonStyle.primary, row=2)
    async def toggle_abortive(self, interaction: discord.Interaction, button: Button):
        # 關閉：九種九牌不能宣告、四風連打／四槓散了／四家立直照打、三家和改為三家都和（流局滿貫不受影響）
        self.abortive = not self.abortive
        await self._edit(interaction)

    @button(label="🔧 細項", style=discord.ButtonStyle.secondary, row=2)
    async def show_details(self, interaction: discord.Interaction, button: Button):
        self.panel_message = interaction.message
        dv = RuleDetailsView(self)
        await interaction.response.send_message(dv.content(), view=dv, ephemeral=True)

    @button(label="確認", style=discord.ButtonStyle.success, row=3)
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


class _DetailButton(Button):
    """細項視窗上的一顆切換鈕（對應 rulesets.OVERRIDABLE 的一個欄位）。"""
    def __init__(self, field: str, row: int):
        super().__init__(style=discord.ButtonStyle.secondary, row=row)
        self.field = field

    async def callback(self, interaction: discord.Interaction) -> None:
        view: RuleDetailsView = self.view
        view.toggle(self.field)
        view.sync()
        await interaction.response.edit_message(content=view.content(), view=view)
        await view.panel.refresh_panel()


class RuleDetailsView(View):
    """規則細項：只有按「細項」的人看得到（ephemeral），但改的是共用面板的設定（panel.rule_over）。
    權限跟面板上其他按鈕一樣（沒有額外限制）。rule_over 只存跟預設不同的值。"""
    _ROWS = {"ippatsu": 0, "ura": 0, "red_fives": 0, "kuitan": 0, "open_riichi": 0,
             "kiriage": 1, "multi_ron": 1, "leftover_sticks": 1, "kuikae": 1, "spectate": 2}
    _PANEL_FIELDS = ("kuikae", "open_riichi", "spectate")        # 不在 RuleSet 裡、直接存在面板上的開關
    _KEYS = {"ippatsu": "rules.det.ippatsu", "ura": "rules.det.ura", "red_fives": "rules.det.red",
             "kuitan": "rules.det.kuitan", "kiriage": "rules.det.kiriage",
             "multi_ron": "rules.det.multi", "leftover_sticks": "rules.det.left",
             "kuikae": "rules.det.kuikae", "open_riichi": "rules.det.openriichi",
             "spectate": "rules.det.spectate"}
    _MULTI_CYCLE = ("double", "triple", "atamahane")

    def __init__(self, panel: "RoomSettingsView"):
        super().__init__(timeout=180)
        self.panel = panel
        self.lang = panel.lang
        self.btns = {}
        for f in (*rulesets.OVERRIDABLE, *self._PANEL_FIELDS):
            b = _DetailButton(f, self._ROWS[f])
            self.btns[f] = b
            self.add_item(b)
        done = Button(style=discord.ButtonStyle.success, row=1, label=i18n.t("rules.det.done", self.lang))
        done.callback = self._done
        self.add_item(done)
        self.sync()

    def _value(self, rs, f: str):
        """欄位目前的有效值（赤寶牌看當下人數的張數）。"""
        if f in self._PANEL_FIELDS:
            return getattr(self.panel, f)
        if f == "red_fives":
            return rs.for_mode(self.panel.sanma)["red"] > 0
        return getattr(rs, f)

    def _value_text(self, rs, f: str) -> str:
        v = self._value(rs, f)
        if f == "multi_ron":
            return i18n.t(f"rules.multi.{v}", self.lang)
        if f == "leftover_sticks":
            return i18n.t(f"rules.left.{v}", self.lang)
        if f == "kuikae":
            return i18n.t("rules.v.allow" if v else "rules.v.deny", self.lang)
        if f == "spectate":
            return i18n.t("rules.v.open" if v else "rules.v.closed", self.lang)
        return i18n.t("rules.v.on" if v else "rules.v.off", self.lang)

    def sync(self) -> None:
        rs = self.panel.effective_rules()
        for f, b in self.btns.items():
            b.label = i18n.t(self._KEYS[f], self.lang, v=self._value_text(rs, f))
            if f in ("multi_ron", "leftover_sticks"):
                b.style = discord.ButtonStyle.primary
            else:
                b.style = discord.ButtonStyle.success if self._value(rs, f) else discord.ButtonStyle.secondary

    def content(self) -> str:
        p = self.panel
        name = rulesets.label(p.rules, self.lang)
        return (i18n.t("rules.det.title", self.lang, name=name) + "\n"
                + rulesets.summary(p.effective_rules(), self.lang, p.sanma, selected=p.rules,
                                   start_points=p.start_points, hanchan=p.hanchan))

    def toggle(self, f: str) -> None:
        """切換一個欄位；跟預設相同就從 rule_over 拿掉，不同才存。"""
        p = self.panel
        if f in self._PANEL_FIELDS:                 # 食替／開立直：直接改面板上的值（開局時照舊由面板取）
            setattr(p, f, not getattr(p, f))
            return
        rs = p.effective_rules()
        cur = self._value(rs, f)
        if f == "multi_ron":
            new = self._MULTI_CYCLE[(self._MULTI_CYCLE.index(rs.multi_ron) + 1) % len(self._MULTI_CYCLE)]
        elif f == "leftover_sticks":
            new = "none" if rs.leftover_sticks == "top" else "top"
        elif f == "red_fives":
            new = 0 if cur else 3
        else:
            new = not cur
        if new == getattr(p.base_rules(), f):
            p.rule_over.pop(f, None)
        else:
            p.rule_over[f] = new

    async def _done(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        try:
            await interaction.delete_original_response()
        except Exception:
            pass
        self.stop()
