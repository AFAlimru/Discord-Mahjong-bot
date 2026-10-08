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
"""斜線指令：/mahjong 房間管理（start/join/end/status/stats/help）與大廳 LobbyView。"""
from __future__ import annotations
import asyncio
import uuid
import discord
from discord import app_commands

from .config import AI_NAMES
from .numfmt import compact, signed as signed_num
from . import db
from . import i18n
from . import rooms
from . import hubs
from .render import make_board_text
from . import tiles as T
from .ui import HelpButton, help_text
from .views import RoomSettingsView
from .flow import (launch_game, launch_ranked_game, launch_dm_game, _cleanup,
                   _delete_threads, _delete_announce, force_end, end_request_view)
from . import matchmaking
from .state import (
    _games, _channel_games, _waiting, _room_owners, _room_configs, _user_game,
    _game_tasks, _lobbies, _threads, _thread_game, _bg_tasks,
)


class LobbyView(discord.ui.View):
    def __init__(self, gid: str, channel: discord.TextChannel):
        # 不因等待而自動關房（timeout=None）；只有關掉機器人才會結束（重啟後大廳即失效）。
        super().__init__(timeout=None)
        self.gid           = gid
        self.channel       = channel
        self.lobby_message: Optional[discord.Message] = None
        self._ai_count     = 0
        self.lang          = _room_configs.get(gid, {}).get("lang", i18n.DEFAULT)
        # 翻譯按鈕：列出「房間語言以外」的語言（動態改 decorator 按鈕的標籤）
        for c in self.children:
            if getattr(c, "label", None) and str(c.label).startswith("🌐"):
                c.label = i18n.translate_label(self.lang)
        self.dm_btn.label = i18n.t("lobby.dm_btn", self.lang)
        self.leave_btn.label = i18n.t("lobby.leave_btn", self.lang)
        # 等待加入時也能看出牌說明
        self.add_item(HelpButton())

    def _content(self, lang: str = i18n.DEFAULT) -> str:
        cfg       = _room_configs.get(self.gid, {})
        mode      = i18n.t("mode.sanma" if cfg.get("is_sanma") else "mode.yonma", lang)
        tt        = cfg.get("thinking_time", 25)
        max_p     = cfg.get("max_players", 4)
        length    = i18n.t("len.hanchan" if cfg.get("length") == "hanchan" else "len.tonpuu", lang)
        tobi      = i18n.t("toggle.on" if cfg.get("tobi", True) else "toggle.off", lang)
        players   = _waiting.get(self.gid, [])
        dm_uids   = cfg.get("dm_uids", set())
        host      = _room_owners.get(self.gid)
        plist     = "\n".join(
            f"• {'🤖' if p.get('is_bot') else ''}{p['username']}"
            f"{' 👑' if p['user_id'] == host else ''}"
            f"{' 📩' if p['user_id'] in dm_uids else ''}"
            for p in players
        )
        return (
            f"**{i18n.t('lobby.title', lang)}**\n"
            f"{i18n.t('lobby.info', lang, mode=mode, length=length, tobi=tobi, tt=tt)}\n"
            f"{i18n.t('lobby.players', lang, cur=len(players), max=max_p)}\n{plist}\n\n"
            f"{i18n.t('lobby.join_hint', lang)}\n"
            f"-# {i18n.t('lobby.dm_hint', lang)}"
        )

    @discord.ui.button(label="加入遊戲", style=discord.ButtonStyle.success)
    async def join_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        gid     = self.gid
        uid     = str(interaction.user.id)
        cfg     = _room_configs.get(gid, {})
        max_p   = cfg.get("max_players", 4)
        waiting = _waiting.get(gid, [])

        if any(p["user_id"] == uid for p in waiting):
            await interaction.response.send_message(i18n.t("msg.already_in_room", i18n.get_user_lang(interaction.user.id, interaction.guild_id, interaction.channel)), ephemeral=True)
            return
        if len(waiting) >= max_p:
            await interaction.response.send_message(i18n.t("msg.room_full", i18n.get_user_lang(interaction.user.id, interaction.guild_id, interaction.channel)), ephemeral=True)
            return

        await _close_owned_waiting_rooms(uid, gid)   # 加入別人的房 → 關掉自己開的房
        _waiting[gid].append({"user_id": uid, "username": interaction.user.display_name, "is_bot": False})
        await self._update(interaction)

    @discord.ui.button(label="🚪 離開", style=discord.ButtonStyle.danger)
    async def leave_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        """離開等待中的房間。房主離開：還有其他真人就交棒給最早加入的那位，沒有就關房。"""
        gid     = self.gid
        uid     = str(interaction.user.id)
        ulang   = i18n.get_user_lang(uid, interaction.guild_id)
        waiting = _waiting.get(gid, [])
        if not any(p["user_id"] == uid for p in waiting):
            await interaction.response.send_message(i18n.t("msg.not_in_room", ulang), ephemeral=True)
            return
        _waiting[gid] = [p for p in waiting if p["user_id"] != uid]
        (_room_configs.get(gid, {}).get("dm_uids") or set()).discard(uid)
        if _room_owners.get(gid) == uid:
            humans = [p for p in _waiting[gid] if not p.get("is_bot")]
            if not humans:                              # 沒有其他真人 → 關房
                self.stop()
                _cleanup(gid, str(self.channel.id))
                await interaction.response.edit_message(
                    content=i18n.t("lobby.closed_host_left", self.lang), view=None)
                return
            _room_owners[gid] = humans[0]["user_id"]    # 交棒給最早加入的真人
            await interaction.response.edit_message(content=self._content(self.lang), view=self)
            try:
                await interaction.followup.send(i18n.t("lobby.host_transferred", self.lang,
                                                       mention=f"<@{humans[0]['user_id']}>"))
            except Exception:
                pass
            return
        await interaction.response.edit_message(content=self._content(self.lang), view=self)
        try:
            await interaction.followup.send(i18n.t("lobby.left", ulang), ephemeral=True)
        except Exception:
            pass

    @discord.ui.button(label="加入 AI", style=discord.ButtonStyle.secondary)
    async def ai_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        gid     = self.gid
        uid     = str(interaction.user.id)
        if _room_owners.get(gid) != uid:
            await interaction.response.send_message(i18n.t("msg.only_host_ai", i18n.get_user_lang(interaction.user.id, interaction.guild_id, interaction.channel)), ephemeral=True)
            return
        cfg   = _room_configs.get(gid, {})
        max_p = cfg.get("max_players", 4)
        waiting = _waiting.get(gid, [])
        if len(waiting) >= max_p:
            await interaction.response.send_message(i18n.t("msg.room_full", i18n.get_user_lang(interaction.user.id, interaction.guild_id, interaction.channel)), ephemeral=True)
            return

        ai_uid  = f"ai_{gid}_{self._ai_count}"
        ai_name = AI_NAMES[self._ai_count % len(AI_NAMES)]
        self._ai_count += 1
        _waiting[gid].append({"user_id": ai_uid, "username": ai_name, "is_bot": True})
        await self._update(interaction)

    @discord.ui.button(label="🌐", style=discord.ButtonStyle.secondary)   # 標籤於 __init__ 依房間語言設定
    async def translate_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        lang = i18n.get_user_lang(interaction.user.id, interaction.guild_id, interaction.channel)
        await interaction.response.send_message(self._content(lang), ephemeral=True)

    @discord.ui.button(label="📩 DM", style=discord.ButtonStyle.secondary)
    async def dm_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        """本場對局的手牌面板走私訊（每人各自切換；只影響這一場）。"""
        uid  = str(interaction.user.id)
        lang = i18n.get_user_lang(uid, interaction.guild_id)
        cfg  = _room_configs.get(self.gid)
        if cfg is None:
            await interaction.response.send_message(i18n.t("msg.no_open_room", lang), ephemeral=True)
            return
        dm_uids = cfg.setdefault("dm_uids", set())
        if uid in dm_uids:
            dm_uids.discard(uid)
            await interaction.response.send_message(i18n.t("lobby.dm_off", lang), ephemeral=True)
        else:
            dm_uids.add(uid)
            await interaction.response.send_message(i18n.t("lobby.dm_on", lang), ephemeral=True)
        if self.lobby_message is not None:           # 大廳名單即時標上 📩
            try:
                await self.lobby_message.edit(content=self._content(self.lang), view=self)
            except Exception:
                pass

    async def _update(self, interaction: discord.Interaction):
        gid   = self.gid
        cfg   = _room_configs.get(gid, {})
        max_p = cfg.get("max_players", 4)
        count = len(_waiting.get(gid, []))
        content = self._content(self.lang)

        if count >= max_p:
            self.stop()
            try:                       # 開局：大廳訊息直接刪除（不留殘影）
                await interaction.response.defer()
            except Exception:
                pass
            try:
                await interaction.message.delete()
            except Exception:
                pass
            await launch_game(gid, self.channel)
        else:
            await interaction.response.edit_message(content=content, view=self)

    async def push_update(self) -> None:
        """由指令（/join）觸發：直接編輯大廳訊息，必要時開局。"""
        gid   = self.gid
        cfg   = _room_configs.get(gid, {})
        max_p = cfg.get("max_players", 4)
        count = len(_waiting.get(gid, []))
        content = self._content(self.lang)
        if count >= max_p:
            self.stop()
            if self.lobby_message:     # 開局：大廳訊息直接刪除
                try:
                    await self.lobby_message.delete()
                except Exception:
                    pass
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


async def _close_owned_waiting_rooms(uid: str, except_gid: str) -> None:
    """玩家加入別的房間時，關掉自己開的、仍在等待中的房間（已開始的對局不動）。"""
    for other_gid, owner in list(_room_owners.items()):
        if owner != uid or other_gid == except_gid or other_gid in _games:
            continue
        chan_id = next((c for c, g in _channel_games.items() if g == other_gid), "")
        lobby = _lobbies.get(other_gid)
        _cleanup(other_gid, chan_id)
        if lobby is not None:
            lobby.stop()
            if getattr(lobby, "lobby_message", None):
                try:
                    await lobby.lobby_message.edit(
                        content="❌ 房主已加入其他房間，本房已關閉。", view=None)
                except Exception:
                    pass


mahjong = app_commands.Group(name="mahjong", description="日本麻將遊戲")


def _play_channels(guild_id) -> set[str]:
    """可以開房／加入的文字頻道：/setup channel 指定的＋每個大廳的開房頻道（都沒有＝不限制）。"""
    out = set(hubs.play_ids(guild_id))
    pc = db.get_play_channel(str(guild_id))
    if pc:
        out.add(str(pc))
    return out


@mahjong.command(name="start", description="開啟新麻將房間")
async def cmd_start(interaction: discord.Interaction) -> None:
    channel_id = str(interaction.channel_id)
    user_id    = str(interaction.user.id)

    if interaction.guild_id is None:
        # DM：可以開「跟電腦打」的休閒局（不列段位）；一人同時只能一場
        other = _user_game.get(user_id)
        if other and other in _games:
            await interaction.response.send_message(
                i18n.t("msg.channel_has_game", i18n.get_user_lang(user_id)), ephemeral=True)
            return
    else:
        allowed = _play_channels(interaction.guild_id)
        if allowed and channel_id not in allowed:   # 伺服器有指定遊玩頻道／大廳的開房頻道
            await interaction.response.send_message(
                i18n.t("msg.play_channel_only", i18n.get_user_lang(user_id),
                       channel="、".join(f"<#{c}>" for c in sorted(allowed))),
                ephemeral=True)
            return
        if channel_id in _channel_games:
            await interaction.response.send_message(i18n.t("msg.channel_has_game", i18n.get_user_lang(interaction.user.id, interaction.guild_id, interaction.channel)), ephemeral=True)
            return

    # 房間語言（設定面板、公開內容、翻譯按鈕都用它）：在大廳的頻道開＝該大廳的語言（只有一個大廳＝伺服器語言），
    # 其他頻道＝房主語言
    room_lang = (hubs.lang_for(interaction.guild_id, interaction.channel)
                 if interaction.guild_id and hubs.of_channel(interaction.guild_id, interaction.channel)
                 else i18n.get_user_lang(user_id))
    sv = RoomSettingsView(gid="setup", lang=room_lang)
    await interaction.response.send_message(i18n.t("settings.prompt", room_lang), view=sv, ephemeral=True)
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
    _room_configs[gid]       = {
        "is_sanma": is_sanma, "thinking_time": thinking_time, "max_players": max_players,
        "length": length, "tobi": tobi, "ruleset": ruleset, "start_points": start_points,
        "kuikae": sv.kuikae, "open_riichi": sv.open_riichi, "abortive": sv.abortive,
        "lang": room_lang,
    }

    if interaction.guild_id is None:
        # DM：AI 補滿直接開打（跟電腦打，不列段位、不開討論串）
        _waiting[gid].extend(
            {"user_id": f"ai_{gid}_{i}", "username": AI_NAMES[i % len(AI_NAMES)], "is_bot": True}
            for i in range(max_players - 1))
        await launch_dm_game(gid, interaction.user)
        return

    _channel_games[channel_id] = gid
    rooms.register(gid, interaction.guild_id, channel_id)

    lobby = LobbyView(gid, interaction.channel)
    msg   = await interaction.followup.send(lobby._content(lobby.lang), view=lobby)
    lobby.lobby_message = msg
    _lobbies[gid] = lobby


@mahjong.command(name="join", description="加入房間（頻道內＝此房；私訊＝跨伺服器隨機匹配）")
@app_commands.describe(host="（選填）指定房主，確認是要加入誰開的房")
async def cmd_join(interaction: discord.Interaction, host: discord.Member = None) -> None:
    channel_id = str(interaction.channel_id)
    uid        = str(interaction.user.id)
    # 私訊裡沒有頻道房間 → 直接跨伺服器隨機匹配（四人）
    if interaction.guild_id is None:
        await _do_casual_match(interaction, sanma=False)
        return
    allowed = _play_channels(interaction.guild_id)
    if allowed and channel_id not in allowed:   # 伺服器有指定遊玩頻道／大廳的開房頻道
        await interaction.response.send_message(
            i18n.t("msg.play_channel_only", i18n.get_user_lang(uid),
                   channel="、".join(f"<#{c}>" for c in sorted(allowed))),
            ephemeral=True)
        return
    gid        = _channel_games.get(channel_id)

    if not gid:
        await interaction.response.send_message(
            i18n.t("msg.no_open_room", i18n.get_user_lang(interaction.user.id, interaction.guild_id, interaction.channel)), ephemeral=True)
        return
    if gid in _games:
        await interaction.response.send_message(i18n.t("msg.game_started_nojoin", i18n.get_user_lang(interaction.user.id, interaction.guild_id, interaction.channel)), ephemeral=True)
        return
    if host is not None and _room_owners.get(gid) != str(host.id):
        await interaction.response.send_message(
            i18n.t("msg.not_host_named", i18n.get_user_lang(interaction.user.id, interaction.guild_id, interaction.channel),
                   host=host.display_name), ephemeral=True)
        return

    cfg     = _room_configs.get(gid, {})
    max_p   = cfg.get("max_players", 4)
    waiting = _waiting.get(gid, [])

    if any(p["user_id"] == uid for p in waiting):
        await interaction.response.send_message(i18n.t("msg.already_in_room", i18n.get_user_lang(interaction.user.id, interaction.guild_id, interaction.channel)), ephemeral=True)
        return
    if len(waiting) >= max_p:
        await interaction.response.send_message(i18n.t("msg.room_full", i18n.get_user_lang(interaction.user.id, interaction.guild_id, interaction.channel)), ephemeral=True)
        return

    await _close_owned_waiting_rooms(uid, gid)   # 加入別人的房 → 關掉自己開的房
    waiting.append({"user_id": uid, "username": interaction.user.display_name, "is_bot": False})
    await interaction.response.send_message(
        i18n.t("msg.joined", i18n.get_user_lang(interaction.user.id, interaction.guild_id, interaction.channel),
               cur=len(waiting), max=max_p), ephemeral=True)

    lobby = _lobbies.get(gid)
    if lobby:
        await lobby.push_update()


@mahjong.command(name="fairness", description="查看目前對局的牌山承諾值（SHA-256），結束後可用來驗證牌山沒被動過")
async def cmd_fairness(interaction: discord.Interaction) -> None:
    from .ui import send_fairness
    uid  = str(interaction.user.id)
    cid  = interaction.channel_id
    gid  = (_user_game.get(uid) or _thread_game.get(cid) or _thread_game.get(str(cid))
            or _channel_games.get(str(cid)))
    if not gid or gid not in _games:
        lang = i18n.get_user_lang(interaction.user.id, interaction.guild_id, interaction.channel)
        await interaction.response.send_message(i18n.t("msg.fairness_no_game", lang), ephemeral=True)
        return
    await send_fairness(interaction, gid)


@mahjong.command(name="end", description="強制結束牌局（房主直接結束；其他玩家需房主同意）")
async def cmd_end(interaction: discord.Interaction) -> None:
    channel_id = str(interaction.channel_id)
    user_id    = str(interaction.user.id)
    # 可在主頻道或任一遊戲討論串內使用；DM 對局（跟電腦打）用玩家對照找
    gid = (_channel_games.get(channel_id) or _thread_game.get(int(channel_id))
           or (_user_game.get(user_id) if interaction.guild_id is None else None))
    if not gid:
        await interaction.response.send_message(i18n.t("msg.no_game", i18n.get_user_lang(interaction.user.id, interaction.guild_id, interaction.channel)), ephemeral=True)
        return
    lang = i18n.get_user_lang(interaction.user.id, interaction.guild_id, interaction.channel)
    host = _room_owners.get(gid)
    if host != user_id:
        # 0.7：非房主也能發起，但要房主按「同意結束」；非本局玩家不行
        gs = _games.get(gid)
        if gs is not None:
            participants = {p.user_id for p in gs.players if not p.is_bot}
        else:
            participants = {w["user_id"] for w in _waiting.get(gid, []) if not w.get("is_bot")}
        if user_id not in participants:
            await interaction.response.send_message(i18n.t("msg.only_host_end", lang), ephemeral=True)
            return
        rlang = _room_configs.get(gid, {}).get("lang", i18n.DEFAULT)
        await interaction.response.send_message(
            i18n.t("end.request", rlang,
                   name=interaction.user.display_name, host=f"<@{host}>"),
            view=end_request_view(gid, rlang))
        return

    # 房主：先回覆（要在 3 秒內回應 interaction），再走共用的強制結束
    await interaction.response.send_message(i18n.t("msg.room_closed", lang), ephemeral=True)
    failed = await force_end(gid)
    if failed:
        try:
            await interaction.followup.send(i18n.t("msg.thread_delete_fail", lang), ephemeral=True)
        except Exception:
            pass


@mahjong.command(name="status", description="查看當前牌局狀態")
async def cmd_status(interaction: discord.Interaction) -> None:
    lang       = i18n.get_user_lang(interaction.user.id, interaction.guild_id, interaction.channel)
    channel_id = str(interaction.channel_id)
    gid        = _channel_games.get(channel_id)
    if not gid:
        await interaction.response.send_message(i18n.t("status.no_game", lang), ephemeral=True)
        return
    gs = _games.get(gid)
    if not gs:
        waiting = _waiting.get(gid, [])
        cfg     = _room_configs.get(gid, {})
        max_p   = cfg.get("max_players", 4)
        plist   = "\n".join(f"• {p['username']}" for p in waiting)
        head    = i18n.t("status.waiting", lang, cur=len(waiting), max=max_p)
        await interaction.response.send_message(f"{head}\n{plist}", ephemeral=True)
        return
    open_hand = _room_configs.get(gid, {}).get("open_hand", False)
    await interaction.response.send_message(
        content=make_board_text(gs, "", open_hand, lang), ephemeral=True)


def _web_stats_button(lang: str, row: int | None = None) -> discord.ui.Button | None:
    """「🌐 網站看更詳細的數據」連結（WEB_BASE_URL/me，登入 Discord 後看自己的完整數據）；沒設定網址＝None。"""
    from .config import WEB_BASE_URL
    if not WEB_BASE_URL:
        return None
    return discord.ui.Button(label=i18n.t("web.more_stats", lang), style=discord.ButtonStyle.link,
                             url=WEB_BASE_URL.rstrip("/") + "/me", row=row)


def _web_stats_view(lang: str):
    """只放網站連結的 View（沒設定網址＝MISSING，等於不附按鈕）。"""
    b = _web_stats_button(lang)
    if b is None:
        return discord.utils.MISSING
    v = discord.ui.View(timeout=None)
    v.add_item(b)
    return v


@mahjong.command(name="stats", description="查看個人統計")
async def cmd_stats(interaction: discord.Interaction) -> None:
    uid  = str(interaction.user.id)
    lang = i18n.get_user_lang(uid)
    y = db.get_mode_summary(uid, "yonma")
    s = db.get_mode_summary(uid, "sanma")
    if not y and not s:
        await interaction.response.send_message(i18n.t("msg.no_stats", lang), ephemeral=True)
        return

    L      = lambda k: i18n.t(k, lang)
    pct    = lambda n, d: f"{(100 * n / d):.0f}%" if d else "—"
    signed = lambda n: (f"+{n}" if (n or 0) >= 0 else str(n))
    medals = ["🥇", "🥈", "🥉", "4️⃣"]

    embed = discord.Embed(
        title=i18n.t("stats.title", lang, name=interaction.user.display_name), color=0xE0AF68)
    try:
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
    except Exception:
        pass

    for mode, summ in (("yonma", y), ("sanma", s)):
        if not summ:
            continue
        is4 = (mode == "yonma")
        g   = summ["games"] or 0
        r   = [summ["r1"] or 0, summ["r2"] or 0, summ["r3"] or 0, summ["r4"] or 0]
        last = r[3] if is4 else r[2]
        dist = "　".join(f"{medals[i]} {r[i]}" for i in range(4 if is4 else 3))
        lines = [
            f"`{L('stats.games')}` **{g}**　`{L('stats.avg_rank')}` **{(summ['avg_rank'] or 0):.2f}**",
            dist,
            (f"`{L('stats.top_rate')}` {pct(r[0], g)}　`{L('stats.rentai')}` {pct(r[0] + r[1], g)}"
             f"　`{L('stats.last_rate')}` {pct(last, g)}"),
        ]
        dan = db.get_rating(uid, mode)
        if dan and dan["games"]:
            from . import rating as _rt
            lines.insert(0, f"🏅 **{_rt.dan_name(dan['dan_idx'])}**（{_rt.pt_text(dan['dan_idx'], dan['dan_pt'])}pt）"
                            f"　`R` {dan['rate']:.0f}　`{L('stats.games')}` {dan['games']}")
        rt = db.get_hand_rates(uid, mode)
        if rt["hands"]:
            h, ag = rt["hands"], rt["agari"]
            lines.append(
                f"`{L('stats.agari_rate')}` {pct(ag, h)}　`{L('stats.houju_rate')}` {pct(rt['houju'], h)}"
                f"　`{L('stats.riichi_rate')}` {pct(rt['riichi'], h)}　`{L('stats.furo_rate')}` {pct(rt['furo'], h)}")
            avg_h = round((summ["houju_points"] or 0) / summ["houju"]) if summ["houju"] else 0
            if ag:
                lines.append(f"`{L('stats.avg_agari')}` {round(rt['agari_pts'] / ag)}"
                             f"　`{L('stats.avg_houju')}` {avg_h}")
            lines.append(f"_{i18n.t('stats.rates_note', lang, n=h)}_")
        lines.append(
            f"`{L('stats.tsumo')}` {summ['tsumo'] or 0}　`{L('stats.ron')}` {summ['ron'] or 0}"
            f"　`{L('stats.houju')}` {summ['houju'] or 0}　`{L('stats.riichi')}` {summ['riichi'] or 0}")
        lines.append(f"`{L('stats.gain')}` {signed_num(summ['gain_points'])}　`{L('stats.lost')}` {compact(summ['houju_points'])}")
        embed.add_field(name=i18n.t("mode.yonma" if is4 else "mode.sanma", lang),
                        value="\n".join(lines), inline=False)
    await interaction.response.send_message(embed=embed, view=_web_stats_view(lang))   # 公開顯示（非 ephemeral）


class RankQueueView(discord.ui.View):
    """段位賽排隊中的「離開」按鈕。"""
    def __init__(self, lang: str):
        super().__init__(timeout=900)
        self.lang = lang

    @discord.ui.button(label="🚪 離開排隊", style=discord.ButtonStyle.danger)
    async def leave_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        lang = i18n.get_user_lang(interaction.user.id, interaction.guild_id, interaction.channel)
        matchmaking.leave(str(interaction.user.id))
        for c in self.children:
            c.disabled = True
        await interaction.response.edit_message(content=i18n.t("rank.left", lang), view=self)
        self.stop()


@mahjong.command(name="rank", description="段位賽：加入全域匹配排隊（對局在你的私訊進行）")
@app_commands.describe(mode="選擇人數模式（不選＝四人）")
@app_commands.choices(mode=[
    app_commands.Choice(name="四人麻將", value="yonma"),
    app_commands.Choice(name="三人麻將", value="sanma"),
])
async def cmd_rank(interaction: discord.Interaction,
                   mode: app_commands.Choice[str] = None) -> None:
    await _do_rank_match(interaction, mode is not None and mode.value == "sanma")


_notify_last: dict = {}      # uid → 上次通知時間（冷卻，避免洗頻）
_NOTIFY_CD = 600             # 每人至多每 10 分鐘收到一次排隊通知


async def _notify_queue_subscribers(client, qkind: str, sanma: bool,
                                    cur: int, mx: int, joiner_uid: str) -> None:
    """有人加入排隊 → 私訊通知訂閱者（排除本人／對局中／已在排隊者，帶冷卻）。"""
    import time as _t
    now = _t.time()
    mode_key = "mode.sanma" if sanma else "mode.yonma"
    key = "notify.queue_rank" if qkind == "rank" else "notify.queue_casual"
    for uid in db.get_queue_subscribers():
        if uid == joiner_uid or uid in _user_game or matchmaking.in_queue(uid):
            continue
        if now - _notify_last.get(uid, 0) < _NOTIFY_CD:
            continue
        lang = i18n.get_user_lang(uid)
        try:
            user = client.get_user(int(uid)) or await client.fetch_user(int(uid))
            await user.send(i18n.t(key, lang, mode=i18n.t(mode_key, lang), cur=cur, max=mx))
            _notify_last[uid] = now
        except Exception:
            pass


def _fire_queue_notify(interaction, qkind: str, sanma: bool, cur: int, mx: int) -> None:
    task = asyncio.create_task(_notify_queue_subscribers(
        interaction.client, qkind, sanma, cur, mx, str(interaction.user.id)))
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


async def _toggle_notify(interaction: discord.Interaction) -> None:
    """切換「有人排隊就私訊我」訂閱。"""
    uid  = str(interaction.user.id)
    on   = not db.get_notify_queue(uid)
    db.set_notify_queue(uid, on)
    lang = i18n.get_user_lang(uid)
    await interaction.response.send_message(
        i18n.t("notify.on" if on else "notify.off", lang), ephemeral=True)


@mahjong.command(name="notify", description="開／關「有人排隊配對就私訊我」通知")
async def cmd_notify(interaction: discord.Interaction) -> None:
    await _toggle_notify(interaction)


async def _do_rank_match(interaction: discord.Interaction, sanma: bool) -> None:
    """段位賽排隊（指令與大廳按鈕共用）。"""
    lang  = i18n.get_user_lang(interaction.user.id, interaction.guild_id, interaction.channel)
    res   = matchmaking.join(interaction.user, sanma)
    kind = res[0]
    if kind == "in_game":
        await interaction.response.send_message(i18n.t("rank.in_game", lang), ephemeral=True)
    elif kind in ("queued", "already"):
        _, cur, mx = res
        key = "rank.queued" if kind == "queued" else "rank.already"
        await interaction.response.send_message(
            i18n.t(key, lang, cur=cur, max=mx), ephemeral=True, view=RankQueueView(lang))
        if kind == "queued":
            _fire_queue_notify(interaction, "rank", sanma, cur, mx)
    elif kind == "matched":
        _, players, mode, _k = res
        await interaction.response.send_message(i18n.t("rank.matched_you", lang), ephemeral=True)
        await launch_ranked_game(players, mode)


async def _do_casual_match(interaction: discord.Interaction, sanma: bool) -> None:
    """休閒隨機匹配（跨伺服器、對局走 DM、不計段位）。"""
    from .flow import launch_match_game
    lang = i18n.get_user_lang(interaction.user.id, interaction.guild_id, interaction.channel)
    res  = matchmaking.join(interaction.user, sanma, kind="casual")
    kind = res[0]
    if kind == "in_game":
        await interaction.response.send_message(i18n.t("rank.in_game", lang), ephemeral=True)
    elif kind in ("queued", "already"):
        _, cur, mx = res
        key = "match.queued" if kind == "queued" else "match.already"
        await interaction.response.send_message(
            i18n.t(key, lang, cur=cur, max=mx), ephemeral=True, view=RankQueueView(lang))
        if kind == "queued":
            _fire_queue_notify(interaction, "casual", sanma, cur, mx)
    elif kind == "matched":
        _, players, mode, _k = res
        await interaction.response.send_message(i18n.t("match.matched_you", lang), ephemeral=True)
        await launch_match_game(players, mode, ranked=False)


@mahjong.command(name="match", description="隨機匹配：跨伺服器湊人開局（對局在你的私訊進行，不計段位）")
@app_commands.describe(mode="選擇人數模式（不選＝四人）")
@app_commands.choices(mode=[
    app_commands.Choice(name="四人麻將", value="yonma"),
    app_commands.Choice(name="三人麻將", value="sanma"),
])
async def cmd_match(interaction: discord.Interaction,
                    mode: app_commands.Choice[str] = None) -> None:
    await _do_casual_match(interaction, mode is not None and mode.value == "sanma")


@mahjong.command(name="rankinfo", description="段位／R 與段位賽說明")
async def cmd_rankinfo(interaction: discord.Interaction) -> None:
    from . import rating
    lang   = i18n.get_user_lang(interaction.user.id, interaction.guild_id, interaction.channel)
    ladder = rating.ladder_text()
    table  = i18n.t("rank.pt_table", lang,
                    m4=i18n.t("mode.yonma", lang), t4=rating.place_table(False),
                    m3=i18n.t("mode.sanma", lang), t3=rating.place_table(True))
    await interaction.response.send_message(
        i18n.t("rank.info", lang, ladder=ladder) + "\n\n" + table, ephemeral=True)


# 役種一覽（與 scoring.py 實作一致）；※＝副露減 1 飜
_YAKU_CHART = [
    (1, ["立直", "一發", "門前清自摸和", "平和", "斷么九", "一盃口", "役牌",
         "嶺上開花", "槍槓", "海底摸月", "河底撈魚"]),
    (2, ["兩立直", "開立直", "七對子", "對對和", "三暗刻", "三槓子", "三色同刻",
         "小三元", "混老頭", "三色同順", "一氣通貫", "混全帶么九"]),
    (3, ["二盃口", "純全帶么九", "混一色"]),
    (6, ["清一色"]),
]
_YAKU_FURO_MINUS = {"三色同順", "一氣通貫", "混全帶么九", "純全帶么九", "混一色", "清一色"}
_YAKUMAN  = ["天和", "地和", "國士無雙", "四暗刻", "大三元", "字一色", "綠一色",
             "清老頭", "九蓮寶燈", "四槓子", "小四喜"]
_YAKUMAN2 = ["國士無雙十三面", "四暗刻單騎", "純正九蓮寶燈", "大四喜"]
_BONUS    = ["寶牌", "裏寶牌", "赤寶牌", "拔北"]


def _yaku_embed(lang: str) -> discord.Embed:
    Y = lambda n: i18n.yaku(n, lang) + ("※" if n in _YAKU_FURO_MINUS else "")
    embed = discord.Embed(title=i18n.t("yakulist.title", lang), color=0xE0AF68)
    for han, names in _YAKU_CHART:
        embed.add_field(name=i18n.t("yakulist.han", lang, n=han),
                        value="、".join(Y(n) for n in names), inline=False)
    embed.add_field(name=i18n.t("yakulist.yakuman", lang),
                    value="、".join(i18n.yaku(n, lang) for n in _YAKUMAN), inline=False)
    embed.add_field(name=i18n.t("yakulist.yakuman2", lang),
                    value="、".join(i18n.yaku(n, lang) for n in _YAKUMAN2), inline=False)
    embed.add_field(name=i18n.t("yakulist.bonus", lang),
                    value="、".join(i18n.yaku(n, lang) for n in _BONUS), inline=False)
    embed.set_footer(text=i18n.t("yakulist.notes", lang))
    return embed


@mahjong.command(name="yaku", description="役種一覽（飜數表）")
async def cmd_yaku(interaction: discord.Interaction) -> None:
    lang = i18n.get_user_lang(interaction.user.id, interaction.guild_id, interaction.channel)
    await interaction.response.send_message(embed=_yaku_embed(lang), ephemeral=True)


@mahjong.command(name="skin", description="切換牌面風格（黑色牌風＝達到「日全食」段位解鎖）")
@app_commands.describe(style="要使用的牌風")
@app_commands.choices(style=[
    app_commands.Choice(name="預設（白）", value="default"),
    app_commands.Choice(name="黑色（日全食解鎖）", value="black"),
])
async def cmd_skin(interaction: discord.Interaction, style: app_commands.Choice[str]) -> None:
    await _set_skin_choice(interaction, style.value, style.name)


def _skin_unlocked(uid: str) -> bool:
    """黑色牌風是否解鎖（任一模式段位達日全食）。"""
    from . import rating as _rt
    return any((db.get_rating(uid, m) or {}).get("dan_idx", 0) >= _rt.BLACK_SKIN_IDX
               for m in ("yonma", "sanma"))


async def _set_skin_choice(interaction: discord.Interaction, val: str, label: str) -> None:
    from . import rating as _rt
    from . import tiles as _tiles
    from .flow import _skin_cache
    uid  = str(interaction.user.id)
    lang = i18n.get_user_lang(uid)
    if val != "default":
        if val not in _tiles.available_skins():
            await interaction.response.send_message(i18n.t("skin.na", lang), ephemeral=True)
            return
        # 解鎖條件：任一模式段位達「日全食」；機器人擁有者直接可用（測試）
        unlocked = _skin_unlocked(uid)
        if not unlocked:
            try:
                unlocked = await interaction.client.is_owner(interaction.user)
            except Exception:
                unlocked = False
        if not unlocked:
            await interaction.response.send_message(
                i18n.t("skin.locked", lang, dan=_rt.dan_name(_rt.BLACK_SKIN_IDX)), ephemeral=True)
            return
    db.set_user_skin(uid, None if val == "default" else val)
    _skin_cache.pop(uid, None)   # 對局中的快取立即失效，下一次渲染就換
    await interaction.response.send_message(i18n.t("skin.set", lang, name=label), ephemeral=True)


@mahjong.command(name="daily", description="每日簽到，獲得活躍度")
async def cmd_daily(interaction: discord.Interaction) -> None:
    lang = i18n.get_user_lang(interaction.user.id, interaction.guild_id, interaction.channel)
    r = db.checkin(str(interaction.user.id), interaction.user.display_name)
    key = "daily.already" if r["already"] else "daily.done"
    await interaction.response.send_message(
        i18n.t(key, lang, reward=compact(r["reward"]), streak=r["streak"], activity=compact(r["activity"])))


@mahjong.command(name="tasks", description="查看每日任務與活躍度")
async def cmd_tasks(interaction: discord.Interaction) -> None:
    lang = i18n.get_user_lang(interaction.user.id, interaction.guild_id, interaction.channel)
    s    = db.task_status(str(interaction.user.id))
    mk   = lambda b: i18n.t("task.done" if b else "task.todo", lang)
    embed = discord.Embed(title=i18n.t("task.title", lang), color=0x9ECE6A)
    embed.description = (
        f"{mk(s['checkin'])} {i18n.t('task.checkin', lang)}\n"
        f"{mk(s['played'])} {i18n.t('task.play', lang)}\n\n"
        f"{i18n.t('task.activity', lang)} **{compact(s['activity'])}**　"
        f"{i18n.t('task.streak', lang)} **{s['streak']}**"
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


def _profile_embed(u) -> discord.Embed:
    from . import rating as _rt
    uid  = str(u.id)
    lang = i18n.get_user_lang(uid)
    L    = lambda k: i18n.t(k, lang)
    embed = discord.Embed(title=i18n.t("profile.title", lang, name=u.display_name), color=0x7AA2F7)
    try:
        embed.set_thumbnail(url=u.display_avatar.url)
    except Exception:
        pass
    ts = int(u.created_at.timestamp())
    lines = [f"{L('profile.id')}：`{uid}`",
             f"{L('profile.created')}：<t:{ts}:D>（<t:{ts}:R>）"]

    dans = []
    for mode in ("yonma", "sanma"):
        r = db.get_rating(uid, mode)
        if r and r["games"]:
            dans.append(f"{i18n.t('mode.' + mode, lang)} {_rt.dan_name(r['dan_idx'])}（R {r['rate']:.0f}）")
    if dans:
        lines.append(f"{L('profile.dan')}：" + "　/　".join(dans))

    act = db.get_activity(uid) or {}
    lines.append(f"{L('profile.activity')}：**{compact(act.get('activity', 0))}**"
                 f"　{L('profile.streak')}：{act.get('streak', 0)}")

    y = db.get_mode_summary(uid, "yonma")
    s = db.get_mode_summary(uid, "sanma")
    total = (y["games"] if y else 0) + (s["games"] if s else 0)
    lines.append(f"{L('profile.games')}：{total}")

    best = act.get("best_win", 0) or 0
    if best:
        nm   = act.get("best_win_name") or ""
        hand = act.get("best_win_hand") or ""
        lines.append(f"{L('profile.best_win')}：**{(nm + ' ') if nm else ''}{best} {L('profile.points')}**")
        if hand:
            lines.append(T.emojify_hand(hand))
    else:
        lines.append(f"{L('profile.best_win')}：{L('profile.none')}")

    embed.description = "\n".join(lines)
    return embed


@mahjong.command(name="profile", description="查看個人資訊卡")
async def cmd_profile(interaction: discord.Interaction) -> None:
    lang = i18n.get_user_lang(str(interaction.user.id))
    await interaction.response.send_message(embed=_profile_embed(interaction.user),
                                            view=_web_stats_view(lang))


_MEDALS = ["🥇", "🥈", "🥉", "4️⃣"]


def _signed(n: int) -> str:
    return f"{'+' if n >= 0 else ''}{n:,}"


def _discord_time(iso: str | None) -> str:
    """資料庫的 UTC 時間字串 → Discord 時間戳（每個人看到自己的時區）；解析不了就原樣顯示日期。"""
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(iso).replace(tzinfo=timezone.utc)
        return f"<t:{int(dt.timestamp())}:f>"
    except Exception:
        return (iso or "")[:10]


def _history_page(uid: str, mode: str, idx: int, lang: str):
    """牌譜檢視：一頁一場（第 idx 場，0＝最近）。回傳 (embed, idx, 總場數, 房號整數或 None)。"""
    total = db.count_records(uid, mode)
    embed = discord.Embed(title=i18n.t("history.title", lang, mode=i18n.t("mode." + mode, lang)),
                          color=0x7AA2F7)
    if total <= 0:
        embed.description = i18n.t("history.empty", lang)
        return embed, 0, 0, None
    idx  = max(0, min(idx, total - 1))
    rows = db.get_recent_records(uid, mode, 1, offset=idx)
    if not rows:
        embed.description = i18n.t("history.empty", lang)
        return embed, 0, 0, None
    r    = rows[0]
    rn   = r.get("room_no")
    day  = _discord_time(r["created_at"])
    sd   = r["score_delta"] or 0
    lines = [
        i18n.t("history.game_head", lang, room=rooms.code(rn) if rn else "—", day=day),
        "",
        i18n.t("history.my_result", lang, medal=_MEDALS[r["rank"] - 1], rank=r["rank"],
               score=f"{r['score']:,}", delta=_signed(sd)),
        i18n.t("history.my_stats", lang, win=(r["tsumo"] or 0) + (r["ron"] or 0),
               tsumo=r["tsumo"] or 0, ron=r["ron"] or 0, houju=r["houju"] or 0,
               riichi=r["riichi"] or 0),
    ]
    try:                                            # 全桌最終順位（含電腦）取自對局存檔
        g = db.get_game(r["game_id"]) if r.get("game_id") else None
        players = ((g or {}).get("game_data") or {}).get("players") or []
    except Exception:
        players = []
    if players:
        lines += ["", f"**{i18n.t('history.standings', lang)}**"]
        ordered = sorted(enumerate(players), key=lambda t: (-(t[1].get("score") or 0), t[0]))
        for i, (_, pl) in enumerate(ordered):
            bot = "🤖 " if pl.get("is_bot") else ""
            me  = "　◀" if str(pl.get("user_id")) == uid else ""
            lines.append(f"{_MEDALS[min(i, 3)]} {bot}{pl.get('username', '?')}　{(pl.get('score') or 0):,}{me}")
    embed.description = "\n".join(lines)
    embed.set_footer(text=i18n.t("history.page", lang, page=idx + 1, total=total))
    return embed, idx, total, rn


class HistoryView(discord.ui.View):
    """牌譜：◀ ▶ 一場一場翻，「🎞 回放」直接看這場，「🔁」切換四麻／三麻。"""
    def __init__(self, uid: str, mode: str, lang: str):
        super().__init__(timeout=600)
        self.uid, self.mode, self.lang = uid, mode, lang
        self.idx, self.total, self.room = 0, 0, None
        self.replay_btn.label = i18n.t("history.btn_replay", lang)
        web = _web_stats_button(lang, row=1)
        if web is not None:
            self.add_item(web)

    def build(self) -> discord.Embed:
        embed, self.idx, self.total, self.room = _history_page(self.uid, self.mode, self.idx, self.lang)
        other = "sanma" if self.mode == "yonma" else "yonma"
        self.prev.disabled = self.idx <= 0
        self.nxt.disabled = self.idx >= self.total - 1
        self.pos.label = f"{self.idx + 1} / {self.total}" if self.total else "0 / 0"
        self.replay_btn.disabled = not self.room
        self.mode_btn.label = "🔁 " + i18n.t("mode." + other, self.lang)
        return embed

    async def _show(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(embed=self.build(), view=self)

    @discord.ui.button(label="◀", style=discord.ButtonStyle.primary, row=0)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.idx -= 1
        await self._show(interaction)

    @discord.ui.button(label="0 / 0", style=discord.ButtonStyle.secondary, disabled=True, row=0)
    async def pos(self, interaction: discord.Interaction, button: discord.ui.Button):
        pass

    @discord.ui.button(label="▶", style=discord.ButtonStyle.primary, row=0)
    async def nxt(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.idx += 1
        await self._show(interaction)

    @discord.ui.button(label="🎞 回放", style=discord.ButtonStyle.success, row=1)
    async def replay_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.room:
            await _start_replay(interaction, self.room)

    @discord.ui.button(label="🔁", style=discord.ButtonStyle.secondary, row=1)
    async def mode_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.mode = "sanma" if self.mode == "yonma" else "yonma"
        self.idx = 0
        await self._show(interaction)


async def _send_history(interaction: discord.Interaction, mode: str | None) -> None:
    """打開牌譜檢視。沒指定模式＝四麻；四麻沒對局但三麻有就直接開三麻。"""
    uid  = str(interaction.user.id)
    lang = i18n.get_user_lang(uid, interaction.guild_id)
    if mode is None:
        mode = "sanma" if (db.count_records(uid, "yonma") == 0
                           and db.count_records(uid, "sanma") > 0) else "yonma"
    v = HistoryView(uid, mode, lang)
    await interaction.response.send_message(embed=v.build(), view=v, ephemeral=True)


@mahjong.command(name="history", description="查看自己的牌譜（左右切換、可直接回放）")
@app_commands.describe(mode="選擇人數模式（不選＝四人）")
@app_commands.choices(mode=[
    app_commands.Choice(name="四人麻將", value="yonma"),
    app_commands.Choice(name="三人麻將", value="sanma"),
])
async def cmd_history(interaction: discord.Interaction,
                      mode: app_commands.Choice[str] = None) -> None:
    await _send_history(interaction, mode.value if mode is not None else None)


class ReplayControl(discord.ui.View):
    """回放控制：自動播放＋步／巡／局導覽＋暫停／繼續。"""
    def __init__(self, frames, seats, lang):
        super().__init__(timeout=3600)
        self.frames, self.seats, self.lang = frames, seats, lang
        self.idx = 0
        self.order = sorted(seats)
        self.focus = self.order[0] if self.order else 0
        self.play = asyncio.Event()
        self.play.set()
        self.msg = None
        self.own = None          # 為這次回放建立的頻道（或建不了頻道時的討論串）：退出／逾時刪掉
        self.toggle.label = i18n.t("replay.pause", lang)
        self.pov.label = f"👁 {seats.get(self.focus, '')}"
        self.quit.label = "✖ " + i18n.t("replay.quit", lang)

    def render(self):
        from . import replay
        return replay.render_frame(self.frames, self.idx, self.seats, self.lang, self.focus)

    async def _redraw(self, interaction):
        self.toggle.label = i18n.t("replay.pause" if self.play.is_set() else "replay.resume", self.lang)
        self.toggle.style = (discord.ButtonStyle.secondary if self.play.is_set()
                             else discord.ButtonStyle.success)
        self.pov.label = f"👁 {self.seats.get(self.focus, '')}"
        await interaction.response.edit_message(content=self.render(), view=self)

    async def _jump(self, interaction, what, direction):
        from . import replay
        self.play.clear()                                # 手動操作 → 暫停自動播放
        self.idx = replay.nav(self.frames, self.idx, what, direction)
        await self._redraw(interaction)

    @discord.ui.button(label="◀局", style=discord.ButtonStyle.secondary, row=0)
    async def ph(self, i, b): await self._jump(i, "hand", -1)
    @discord.ui.button(label="◀巡", style=discord.ButtonStyle.secondary, row=0)
    async def pt(self, i, b): await self._jump(i, "turn", -1)
    @discord.ui.button(label="◀步", style=discord.ButtonStyle.secondary, row=0)
    async def ps(self, i, b): await self._jump(i, "step", -1)
    @discord.ui.button(label="步▶", style=discord.ButtonStyle.secondary, row=0)
    async def ns(self, i, b): await self._jump(i, "step", +1)
    @discord.ui.button(label="巡▶", style=discord.ButtonStyle.secondary, row=0)
    async def nt(self, i, b): await self._jump(i, "turn", +1)
    @discord.ui.button(label="局▶", style=discord.ButtonStyle.secondary, row=1)
    async def nh(self, i, b): await self._jump(i, "hand", +1)

    @discord.ui.button(label="⏸", style=discord.ButtonStyle.primary, row=1)
    async def toggle(self, interaction, button):
        if self.play.is_set():
            self.play.clear()
        else:
            self.play.set()
        await self._redraw(interaction)

    @discord.ui.button(label="👁", style=discord.ButtonStyle.secondary, row=1)
    async def pov(self, interaction, button):      # 切換視角（換看哪家手牌）
        if self.order:
            self.focus = self.order[(self.order.index(self.focus) + 1) % len(self.order)]
        await self._redraw(interaction)

    async def _drop(self) -> None:
        self.play.clear()
        if self.own is not None:
            try:
                await self.own.delete(reason="Suzume Tsuk 回放結束")
            except Exception:
                pass
            self.own = None

    @discord.ui.button(label="✖", style=discord.ButtonStyle.danger, row=1)
    async def quit(self, interaction, button):     # 退出 → 刪掉回放頻道
        try:
            await interaction.response.defer()
        except Exception:
            pass
        await self._drop()
        self.stop()

    async def on_timeout(self) -> None:            # 一小時沒人操作 → 收掉回放頻道
        await self._drop()


@mahjong.command(name="replay", description="輸入房號回放該場對局")
@app_commands.describe(room="房號（如 K7Q2M）；清單裡是你最近的對局")
async def cmd_replay(interaction: discord.Interaction, room: str) -> None:
    n = rooms.parse(room)
    if n is None:
        lang = i18n.get_user_lang(interaction.user.id, interaction.guild_id, interaction.channel)
        await interaction.response.send_message(
            i18n.t("replay.not_found", lang, room=room.strip()[:20]), ephemeral=True)
        return
    await _start_replay(interaction, n)


@cmd_replay.autocomplete("room")
async def _replay_room_ac(interaction: discord.Interaction, current: str):
    cur = (current or "").strip().upper().lstrip("#")
    out = []
    try:
        rows = db.get_recent_games_any(str(interaction.user.id), 25)
    except Exception:
        rows = []
    for r in rows:
        c = rooms.code(r["room_no"])
        if cur and cur not in c:
            continue
        day = (r["created_at"] or "")[:10]
        out.append(app_commands.Choice(
            name=f"{c} · {day} · {_MEDALS[r['rank'] - 1]} {r['score']:,}", value=c))
    return out[:25]


REPLAY_TOPIC = "suzume-replay"   # 回放頻道的主題標記：重啟時只清有這個標記的頻道


async def _replay_channel(interaction: discord.Interaction, lang: str, room: str):
    """為這次回放開一個只有自己看得到的文字頻道（開在雀月類別；沒有就開在目前頻道的類別）。"""
    guild = interaction.guild
    if guild is None:
        return None
    cat = None
    try:
        h   = hubs.of_channel(guild.id, interaction.channel) or (hubs.all_hubs(guild.id) or [None])[0]
        cid = (h or {}).get("category_id")
        cat = guild.get_channel(int(cid)) if cid else None
    except Exception:
        cat = None
    if not isinstance(cat, discord.CategoryChannel):
        cat = getattr(interaction.channel, "category", None)
    over = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=False,
                                                      read_message_history=True),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True,
                                              read_message_history=True, manage_channels=True,
                                              manage_messages=True),
    }
    return await guild.create_text_channel(
        i18n.t("replay.channel_name", lang, room=room), category=cat, overwrites=over,
        topic=REPLAY_TOPIC, reason="Suzume Tsuk 回放")


async def cleanup_replay_channels(guild) -> None:
    """啟動時清掉上次沒收掉的回放頻道（重啟後按鈕已失效）。只動有 REPLAY_TOPIC 標記的頻道。"""
    for ch in list(getattr(guild, "text_channels", [])):
        if getattr(ch, "topic", None) == REPLAY_TOPIC:
            try:
                await ch.delete(reason="Suzume Tsuk 清除遺留的回放頻道")
            except Exception:
                pass


async def _start_replay(interaction: discord.Interaction, room: int) -> None:
    from . import replay
    lang = i18n.get_user_lang(interaction.user.id, interaction.guild_id, interaction.channel)
    code = rooms.code(room)
    g = db.get_game_by_room_no(room)
    if not g:
        await interaction.response.send_message(i18n.t("replay.not_found", lang, room=code), ephemeral=True)
        return
    frames, seats, n = replay.build_frames(db.get_game_logs(g["game_id"]), lang)
    if not frames or not seats:
        await interaction.response.send_message(i18n.t("replay.no_log", lang, room=code), ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    own = None
    try:                                         # 開一個回放頻道（只有自己看得到）
        own = await _replay_channel(interaction, lang, code)
    except Exception as e:
        print(f"[replay] 建回放頻道失敗（{e!r}），改用討論串")
    if own is None and interaction.guild is not None:
        try:                                     # 缺「管理頻道」權限 → 退回討論串
            own = await interaction.channel.create_thread(
                name=i18n.t("replay.title", lang, room=code),
                type=discord.ChannelType.public_thread)
        except Exception:
            own = None
    target = own or interaction.channel          # 私訊／都建不了 → 就在這裡播
    ctrl = ReplayControl(frames, seats, lang)
    ctrl.own = own
    ctrl.msg = await target.send(ctrl.render(), view=ctrl)
    where = target.mention if hasattr(target, "mention") else "此頻道"
    await interaction.followup.send(i18n.t("replay.opened", lang, thread=where), ephemeral=True)

    async def _autoplay():
        while True:
            await ctrl.play.wait()                       # 暫停時卡住
            nxt = ctrl.idx + 1
            if nxt >= len(ctrl.frames):                  # 已到最後一格
                break
            await asyncio.sleep(ctrl.frames[nxt].get("delay", 1.1))
            # 期間可能被暫停或手動跳轉 → 重新檢查
            if not ctrl.play.is_set() or ctrl.idx + 1 >= len(ctrl.frames):
                continue
            ctrl.idx += 1
            try:
                await ctrl.msg.edit(content=ctrl.render(), view=ctrl)
            except Exception:
                break
    task = asyncio.create_task(_autoplay())
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


# ═══════════════════════════════════════════════════════════════
#  Top-level commands（由 register(tree) 在入口掛上）
# ═══════════════════════════════════════════════════════════════

async def cmd_help_top(interaction: discord.Interaction) -> None:
    lang = i18n.get_user_lang(interaction.user.id, interaction.guild_id, interaction.channel)
    await interaction.response.send_message(i18n.t("help.guide", lang), ephemeral=True)


# ── 語言設定（per-user）──────────────────────────────────────────
class LanguageButton(discord.ui.Button):
    def __init__(self, code: str):
        super().__init__(label=i18n.lang_name(code), style=discord.ButtonStyle.primary)
        self.code = code

    async def callback(self, interaction: discord.Interaction) -> None:
        i18n.set_user_lang(interaction.user.id, self.code)
        await interaction.response.send_message(
            i18n.t("language.set", self.code, name=i18n.lang_name(self.code)), ephemeral=True)


class LanguageView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)
        for code in i18n.available():
            self.add_item(LanguageButton(code))


async def cmd_language(interaction: discord.Interaction) -> None:
    cur = i18n.get_user_lang(interaction.user.id, interaction.guild_id, interaction.channel)
    await interaction.response.send_message(
        i18n.t("language.choose", cur, cur=i18n.lang_name(cur)),
        view=LanguageView(), ephemeral=True)


# ═══════════════════════════════════════════════════════════════
#  遊戲大廳頻道（/mahjong lobby）：唯讀頻道＋一則常駐按鈕面板
# ═══════════════════════════════════════════════════════════════

def _latest_changelog() -> str:
    """CHANGELOG.md 最新一版的內容（給「📢 公告」按鈕）。"""
    import os as _os
    path = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
                         "CHANGELOG.md")
    try:
        with open(path, encoding="utf-8") as f:
            md = f.read()
    except Exception:
        return "（暫時讀不到更新日誌）"
    i = md.find("## [")
    if i < 0:
        return md[:1800]
    j = md.find("## [", i + 4)
    body = md[i:j].strip() if j > 0 else md[i:].strip()
    if len(body) > 1800:
        body = body[:1800] + "…"
    return body


class _ModePick(discord.ui.View):
    """大廳：選人數（四人／三人）後進排隊。"""
    def __init__(self, ranked: bool, lang: str):
        super().__init__(timeout=120)
        self.ranked = ranked
        self.y.label = i18n.t("hub.yonma", lang)
        self.s.label = i18n.t("hub.sanma", lang)

    async def _go(self, interaction, sanma):
        if self.ranked:
            await _do_rank_match(interaction, sanma)
        else:
            await _do_casual_match(interaction, sanma)

    @discord.ui.button(label="四人", style=discord.ButtonStyle.primary)
    async def y(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._go(interaction, False)

    @discord.ui.button(label="三人", style=discord.ButtonStyle.secondary)
    async def s(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._go(interaction, True)


class _SkinPick(discord.ui.View):
    """大廳：倉庫（牌風切換）。"""
    def __init__(self, lang: str):
        super().__init__(timeout=120)

    @discord.ui.button(label="⚪ 預設（白）", style=discord.ButtonStyle.secondary)
    async def d(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _set_skin_choice(interaction, "default", "預設（白）")

    @discord.ui.button(label="⚫ 黑色", style=discord.ButtonStyle.secondary)
    async def b(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _set_skin_choice(interaction, "black", "黑色")


class _ReplayModal(discord.ui.Modal):
    """大廳：輸入房號回放。"""
    room = discord.ui.TextInput(label="房號", placeholder="例：K7Q2M", min_length=1, max_length=8)

    def __init__(self, lang: str):
        super().__init__(title="🎞 回放")

    async def on_submit(self, interaction: discord.Interaction):
        n = rooms.parse(self.room.value)
        if n is None:
            lang = i18n.get_user_lang(interaction.user.id, interaction.guild_id, interaction.channel)
            await interaction.response.send_message(
                i18n.t("replay.not_found", lang, room=str(self.room.value).strip()[:20]), ephemeral=True)
            return
        await _start_replay(interaction, n)


class LobbyPanel(discord.ui.View):
    """大廳常駐面板（persistent view：custom_id 固定、重啟後由 run.py 重新掛上）。
    lang：面板按鈕標籤語言（建立大廳時依建立者語言；分發只看 custom_id，與標籤無關）。"""
    def __init__(self, lang: str = None):
        super().__init__(timeout=None)
        lang = lang or i18n.DEFAULT
        labels = {"rank": "hub.btn.rank", "match": "hub.btn.match", "daily": "hub.btn.daily",
                  "profile": "hub.btn.profile", "skin": "hub.btn.skin", "history": "hub.btn.history",
                  "replay": "hub.btn.replay", "yaku": "hub.btn.yaku", "lang": "hub.btn.lang",
                  "voice": "hub.btn.voice", "news": "hub.btn.news", "refresh": "hub.btn.refresh"}
        for c in self.children:
            cid = (getattr(c, "custom_id", "") or "").replace("hub:", "")
            if cid in labels:
                c.label = i18n.t(labels[cid], lang)
        from .config import WEB_BASE_URL
        if WEB_BASE_URL:
            self.add_item(discord.ui.Button(label=i18n.t("hub.btn.web", lang),
                                            style=discord.ButtonStyle.link,
                                            url=WEB_BASE_URL, row=2))

    @discord.ui.button(label="🏅 段位賽", style=discord.ButtonStyle.primary,
                       custom_id="hub:rank", row=0)
    async def rank(self, interaction: discord.Interaction, button: discord.ui.Button):
        lang = i18n.get_user_lang(interaction.user.id, interaction.guild_id, interaction.channel)
        await interaction.response.send_message(
            i18n.t("hub.pick_mode", lang), view=_ModePick(True, lang), ephemeral=True)

    @discord.ui.button(label="🎲 休閒場", style=discord.ButtonStyle.success,
                       custom_id="hub:match", row=0)
    async def match(self, interaction: discord.Interaction, button: discord.ui.Button):
        lang = i18n.get_user_lang(interaction.user.id, interaction.guild_id, interaction.channel)
        await interaction.response.send_message(
            i18n.t("hub.pick_mode", lang), view=_ModePick(False, lang), ephemeral=True)

    @discord.ui.button(label="✅ 每日簽到", style=discord.ButtonStyle.secondary,
                       custom_id="hub:daily", row=0)
    async def daily(self, interaction: discord.Interaction, button: discord.ui.Button):
        lang = i18n.get_user_lang(interaction.user.id, interaction.guild_id, interaction.channel)
        r = db.checkin(str(interaction.user.id), interaction.user.display_name)
        key = "daily.already" if r["already"] else "daily.done"
        await interaction.response.send_message(
            i18n.t(key, lang, reward=compact(r["reward"]), streak=r["streak"],
                   activity=compact(r["activity"])),
            ephemeral=True)

    @discord.ui.button(label="🔔 通知", style=discord.ButtonStyle.secondary,
                       custom_id="hub:notify", row=0)
    async def notify(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _toggle_notify(interaction)

    @discord.ui.button(label="👤 個人資訊", style=discord.ButtonStyle.secondary,
                       custom_id="hub:profile", row=1)
    async def profile(self, interaction: discord.Interaction, button: discord.ui.Button):
        lang = i18n.get_user_lang(str(interaction.user.id))
        await interaction.response.send_message(embed=_profile_embed(interaction.user),
                                                view=_web_stats_view(lang), ephemeral=True)

    @discord.ui.button(label="🎒 倉庫", style=discord.ButtonStyle.secondary,
                       custom_id="hub:skin", row=1)
    async def skin(self, interaction: discord.Interaction, button: discord.ui.Button):
        uid  = str(interaction.user.id)
        lang = i18n.get_user_lang(uid)
        cur  = db.get_user_skin(uid) or "default"
        black = "✅" if _skin_unlocked(uid) else "🔒（日全食解鎖）"
        body = i18n.t("hub.skin_body", lang, cur=("黑色" if cur == "black" else "預設（白）"), black=black)
        await interaction.response.send_message(body, view=_SkinPick(lang), ephemeral=True)

    @discord.ui.button(label="📜 牌譜", style=discord.ButtonStyle.secondary,
                       custom_id="hub:history", row=1)
    async def history(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _send_history(interaction, None)

    @discord.ui.button(label="🎞 回放", style=discord.ButtonStyle.secondary,
                       custom_id="hub:replay", row=1)
    async def replay(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(_ReplayModal(i18n.get_user_lang(interaction.user.id, interaction.guild_id, interaction.channel)))

    @discord.ui.button(label="📖 役種", style=discord.ButtonStyle.secondary,
                       custom_id="hub:yaku", row=2)
    async def yaku(self, interaction: discord.Interaction, button: discord.ui.Button):
        lang = i18n.get_user_lang(interaction.user.id, interaction.guild_id, interaction.channel)
        await interaction.response.send_message(embed=_yaku_embed(lang), ephemeral=True)

    @discord.ui.button(label="🗣 語言", style=discord.ButtonStyle.secondary,
                       custom_id="hub:lang", row=2)
    async def lang_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        cur = i18n.get_user_lang(interaction.user.id, interaction.guild_id, interaction.channel)
        await interaction.response.send_message(
            i18n.t("language.choose", cur, cur=i18n.lang_name(cur)),
            view=LanguageView(), ephemeral=True)

    @discord.ui.button(label="📢 公告", style=discord.ButtonStyle.secondary,
                       custom_id="hub:news", row=2)
    async def news(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(_latest_changelog(), ephemeral=True)

    @discord.ui.button(label="🎚️ 語音", style=discord.ButtonStyle.secondary,
                       custom_id="hub:voice", row=1)
    async def voice_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        lang = i18n.get_user_lang(interaction.user.id, interaction.guild_id, interaction.channel)
        if interaction.guild_id is None:
            await interaction.response.send_message(i18n.t("msg.guild_only", lang), ephemeral=True)
            return
        from .voice import VoicePackSelect
        v = discord.ui.View(timeout=120)
        v.add_item(VoicePackSelect(lang, row=0, uid=str(interaction.user.id)))   # 個人設定
        await interaction.response.send_message(i18n.t("voice.pack_prompt", lang),
                                                view=v, ephemeral=True)

    @discord.ui.button(label="🔁 更新面板", style=discord.ButtonStyle.secondary,
                       custom_id="hub:refresh", row=2)
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        # 更新面板到最新版（機器人改版後新按鈕才會出現）；管理員限定、順便切成點按者語言
        perms = getattr(interaction.user, "guild_permissions", None)
        lang = i18n.get_user_lang(interaction.user.id, interaction.guild_id, interaction.channel)
        if not (perms and (perms.manage_channels or perms.administrator)):
            await interaction.response.send_message(i18n.t("hub.need_perm", lang), ephemeral=True)
            return
        await interaction.response.edit_message(content=i18n.t("hub.panel", lang),
                                                view=LobbyPanel(lang))


setup_group = app_commands.Group(
    name="setup", description="伺服器設定（遊戲大廳等）", guild_only=True,
    # 沒有「管理伺服器」權限的人看不到也用不了 /setup（伺服器設定 → 整合 可另外調整）
    default_permissions=discord.Permissions(manage_guild=True))


@setup_group.command(name="create",
                     description="建立雀月類別（大廳頻道＋配對語音；對局頻道也會開在裡面；需管理頻道權限）")
async def cmd_setup_create(interaction: discord.Interaction) -> None:
    await _do_setup_create(interaction)


def _can_manage(interaction: discord.Interaction) -> bool:
    perms = getattr(interaction.user, "guild_permissions", None)
    return bool(perms and (perms.manage_channels or perms.administrator))


def _hub_lobby(guild, hub: dict | None):
    """大廳設定裡的大廳頻道（頻道已被刪＝None）。"""
    try:
        return guild.get_channel(int((hub or {}).get("lobby_channel_id") or 0))
    except (TypeError, ValueError):
        return None


async def _do_setup_create(interaction: discord.Interaction, lang: str | None = None) -> None:
    """建立一套大廳（類別＋按鈕大廳＋開房頻道＋配對語音）；/setup create 與指南的「建立大廳」按鈕共用。
    lang＝大廳語言（指南按鈕傳指南目前顯示的語言；沒給＝下指令的人的語言）。
    一台伺服器可以有多個大廳、每種語言一個：同語言已經有了就告知，不同語言就另外建一套（舊的保留）。"""
    ulang = i18n.get_user_lang(interaction.user.id, interaction.guild_id, interaction.channel)
    lang  = lang or ulang
    if interaction.guild_id is None:
        await interaction.response.send_message(i18n.t("msg.guild_only", ulang), ephemeral=True)
        return
    if not _can_manage(interaction):
        await interaction.response.send_message(i18n.t("hub.need_perm", ulang), ephemeral=True)
        return
    same = hubs.for_lang(interaction.guild_id, lang)
    old  = _hub_lobby(interaction.guild, same)
    if old is not None:
        await interaction.response.send_message(
            i18n.t("hub.already", ulang, channel=old.mention, language=i18n.lang_name(lang)),
            ephemeral=True)
        return
    if same is not None:                       # 同語言的大廳頻道被手動刪掉了 → 舊紀錄作廢、重建
        db.delete_hub(same["id"])
    await interaction.response.defer(ephemeral=True)
    await _build_hub(interaction, lang)


async def _build_hub(interaction: discord.Interaction, lang: str) -> None:
    """實際建立大廳一套（呼叫前 interaction 已回應過；結果用 followup 回）。"""
    guild = interaction.guild
    # 0.7：建「類別」→ 裡面放大廳文字頻道＋配對語音；之後對局頻道全開在此類別下
    cat = None
    try:
        cat = await guild.create_category(i18n.t("hub.category_name", lang),
                                          reason="Suzume Tsuk 雀月類別")
    except Exception as e:
        await interaction.followup.send(
            i18n.t("hub.create_fail", lang) + f"\n`{e}`", ephemeral=True)
        return
    name = i18n.t("hub.channel_name", lang)
    # 帶 overwrites 建頻道時，機器人必須「擁有」所設定的每項權限＋管理權限（Manage Roles）；
    # 缺任何一項都會 Forbidden。所以先試完整版，不行就降級（先建、再鎖 @everyone 發言）。
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(
            send_messages=False, add_reactions=False,
            create_public_threads=False, create_private_threads=False,
            send_messages_in_threads=False),
        guild.me: discord.PermissionOverwrite(
            send_messages=True, manage_channels=True, manage_threads=True,
            manage_messages=True,
            create_public_threads=True, send_messages_in_threads=True),
    }
    ch = None
    note = ""
    try:
        ch = await guild.create_text_channel(name, category=cat, overwrites=overwrites,
                                             reason="Suzume Tsuk 遊戲大廳")
    except discord.Forbidden:
        # 降級 1：不帶 overwrites 建立
        try:
            ch = await guild.create_text_channel(name, category=cat,
                                                 reason="Suzume Tsuk 遊戲大廳")
        except Exception as e:
            await interaction.followup.send(
                i18n.t("hub.create_fail", lang) + f"\n`{e}`", ephemeral=True)
            return
        # 降級 2：至少鎖 @everyone 發言（需要該頻道的管理權限；不行就提示手動）
        try:
            await ch.set_permissions(guild.default_role, send_messages=False,
                                     add_reactions=False, create_public_threads=False,
                                     create_private_threads=False,
                                     send_messages_in_threads=False)
        except Exception:
            note = "\n" + i18n.t("hub.lock_manual", lang)
    except Exception as e:
        await interaction.followup.send(
            i18n.t("hub.create_fail", lang) + f"\n`{e}`", ephemeral=True)
        return
    # 開房頻道：給玩家打 /mahjong start・/join 用（可發言），並預設設為指定遊玩頻道
    start_ch = None
    try:
        start_ch = await guild.create_text_channel(
            i18n.t("hub.start_channel_name", lang), category=cat,
            reason="Suzume Tsuk 開房頻道")
    except Exception as e:
        note += "\n" + i18n.t("hub.start_fail", lang) + f" `{e}`"
    # 配對語音：玩家加入 → 自動開 4 人語音房，滿人自動開局（建不了就略過，不擋大廳）
    vc = None
    try:
        vc = await guild.create_voice_channel(i18n.t("hub.voice_name", lang),
                                              category=cat,
                                              reason="Suzume Tsuk 配對語音")
    except Exception as e:
        note += "\n" + i18n.t("hub.voice_fail", lang) + f" `{e}`"
    db.add_hub(guild.id, lang, cat.id, ch.id, vc.id if vc else None,
               start_ch.id if start_ch is not None else None)
    from .state import _lobby_channels
    _lobby_channels.pop(str(guild.id), None)      # 「大廳打字即刪」快取：下次重讀所有大廳
    if start_ch is not None:
        note += "\n" + i18n.t("hub.start_created", lang, channel=start_ch.mention)
    try:
        await ch.send(i18n.t("hub.panel", lang), view=LobbyPanel(lang))
    except Exception as e:
        await interaction.followup.send(
            i18n.t("hub.create_fail", lang) + f"\n`{e}`", ephemeral=True)
        return
    await interaction.followup.send(
        i18n.t("hub.created", lang, channel=ch.mention) + note, ephemeral=True)


def _fmt_age(s) -> str:
    if s is None:
        return "?"
    s = int(s); h, m = s // 3600, (s % 3600) // 60
    return f"{h}時{m}分" if h else f"{m}分"


def _room_line(r) -> str:
    st  = "🎮進行中" if r["status"] == "playing" else "🕓等待中"
    ch  = f"<#{r['channel_id']}>" if r["channel_id"] else "?"
    who = "、".join(r["humans"]) or "（無真人）"
    ai  = f" +{r['ai']}電腦" if r["ai"] else ""
    rno = rooms.code(r["room_no"]) if r["room_no"] else "?"
    return f"`{rno}` {st}｜{ch}｜{who}{ai}｜{_fmt_age(r['age_s'])}"


def _clear_room_text(rms) -> str:
    if not rms:
        return "✅ 目前沒有任何對局或等待房。"
    return ("🗂️ **本伺服器對局／等待房**\n" + "\n".join(_room_line(r) for r in rms[:40])
            + "\n\n用下方選單勾選要清的房間，或直接「只清等待房」／「全部清除」。")


class _RoomPick(discord.ui.Select):
    def __init__(self, rms):
        opts = []
        for r in rms[:25]:
            st  = "🎮" if r["status"] == "playing" else "🕓"
            who = "、".join(r["humans"]) or "無真人"
            opts.append(discord.SelectOption(
                label=f"{rooms.code(r['room_no']) if r['room_no'] else '?'} {st} {who}"[:100], value=r["gid"],
                description=_fmt_age(r["age_s"])[:100]))
        super().__init__(placeholder="勾選要清除的房間…", min_values=1,
                         max_values=len(opts), options=opts, row=0)

    async def callback(self, interaction: discord.Interaction) -> None:
        self.view.picked = set(self.values)
        await interaction.response.defer()


class ClearRoomView(discord.ui.View):
    """/setup clear_room：列出房間，勾選後清除，或一鍵清等待房／全部（含討論串／頻道）。"""
    def __init__(self, guild_id: str, rms: list):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.picked: set = set()
        self.add_item(_RoomPick(rms))

    async def _clear(self, interaction: discord.Interaction, pick) -> None:
        from . import flow
        rms = flow.list_rooms(self.guild_id)          # 以當下狀態為準（列出後可能已有房結束）
        targets = [r for r in rms if pick(r)]
        if not targets:
            await interaction.response.send_message("❌ 沒有符合的房間。", ephemeral=True)
            return
        await interaction.response.defer()
        ok = fail = 0
        for r in targets:
            try:
                await flow.force_end(r["gid"]); ok += 1
            except Exception as e:
                fail += 1; print(f"[rooms] force_end {r['gid']} 失敗：{e!r}")
        msg = f"🧹 已清除 {ok} 個房間（含討論串／頻道）。"
        if fail:
            msg += f"\n⚠️ {fail} 個刪除失敗（多半是機器人缺管理權限）。"
        self.stop()
        try:
            await interaction.edit_original_response(content=msg, view=None)
        except Exception:
            pass

    @discord.ui.button(label="🧹 清除勾選的", style=discord.ButtonStyle.danger, row=1)
    async def clear_picked(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.picked:
            await interaction.response.send_message("請先在選單勾選房間。", ephemeral=True)
            return
        await self._clear(interaction, lambda r: r["gid"] in self.picked)

    @discord.ui.button(label="🕓 只清等待房", style=discord.ButtonStyle.secondary, row=1)
    async def clear_waiting(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._clear(interaction, lambda r: r["status"] == "waiting")

    @discord.ui.button(label="💥 全部清除", style=discord.ButtonStyle.danger, row=1)
    async def clear_all(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._clear(interaction, lambda r: True)


@setup_group.command(name="clear_room",
                     description="清除本伺服器殘留的對局／等待房（含討論串／頻道）")
async def cmd_setup_clear_room(interaction: discord.Interaction) -> None:
    lang = i18n.get_user_lang(interaction.user.id, interaction.guild_id, interaction.channel)
    if interaction.guild_id is None:
        await interaction.response.send_message(i18n.t("msg.guild_only", lang), ephemeral=True)
        return
    perms = getattr(interaction.user, "guild_permissions", None)
    if not (perms and (perms.manage_guild or perms.administrator)):
        await interaction.response.send_message(i18n.t("hub.need_perm", lang), ephemeral=True)
        return
    from . import flow
    rms = flow.list_rooms(str(interaction.guild_id))
    if not rms:
        await interaction.response.send_message(_clear_room_text(rms), ephemeral=True)
        return
    await interaction.response.send_message(_clear_room_text(rms),
                                            view=ClearRoomView(str(interaction.guild_id), rms),
                                            ephemeral=True)


@setup_group.command(name="lang",
                     description="伺服器主要語言：未自訂語言的成員預設用它；不選＝顯示目前設定")
@app_commands.describe(language="要設定的語言；「依伺服器地區」＝依 Discord 伺服器地區自動偵測")
@app_commands.choices(language=[app_commands.Choice(name=i18n.lang_name(c), value=c)
                                for c in i18n.available()]
                      + [app_commands.Choice(name="依伺服器地區（auto）", value="auto")])
async def cmd_setup_lang(interaction: discord.Interaction,
                         language: app_commands.Choice[str] = None) -> None:
    lang = i18n.get_user_lang(interaction.user.id, interaction.guild_id, interaction.channel)
    if interaction.guild_id is None:
        await interaction.response.send_message(i18n.t("msg.guild_only", lang), ephemeral=True)
        return
    perms = getattr(interaction.user, "guild_permissions", None)
    if not (perms and (perms.manage_guild or perms.administrator)):
        await interaction.response.send_message(i18n.t("hub.need_perm", lang), ephemeral=True)
        return
    cur      = i18n.guild_lang(interaction.guild_id)
    detected = i18n.detect_locale(getattr(interaction.guild, "preferred_locale", ""))
    avail    = "、".join(i18n.available())
    if not language:
        c = cur or f"未設定（用母本 {i18n.DEFAULT}）"
        await interaction.response.send_message(
            f"🌐 伺服器主要語言：**{c}**\nDiscord 伺服器地區偵測：**{detected}**\n"
            f"可設定：{avail}（或「依伺服器地區」自動）。", ephemeral=True)
        return
    code = detected if language.value == "auto" else language.value
    if code not in i18n.available():
        await interaction.response.send_message(
            f"❌ 不支援的語言碼「{code}」。可用：{avail}", ephemeral=True)
        return
    i18n.set_guild_lang(interaction.guild_id, code)
    await interaction.response.send_message(
        f"✅ 伺服器主要語言已設為 **{code}**（未自訂語言的成員會預設用它）。", ephemeral=True)


@setup_group.command(name="channel", description="指定遊玩頻道（start/join 只能在該頻道用），或解除限制")
@app_commands.describe(channel="要指定的頻道（不選＝目前頻道）", action="指定頻道／解除限制（不選＝指定）")
@app_commands.choices(action=[app_commands.Choice(name="指定為遊玩頻道", value="set"),
                              app_commands.Choice(name="解除限制", value="clear")])
async def cmd_setup_channel(interaction: discord.Interaction,
                            channel: discord.TextChannel = None,
                            action: app_commands.Choice[str] = None) -> None:
    lang = i18n.get_user_lang(interaction.user.id, interaction.guild_id, interaction.channel)
    if interaction.guild_id is None:
        await interaction.response.send_message(i18n.t("msg.guild_only", lang), ephemeral=True)
        return
    perms = getattr(interaction.user, "guild_permissions", None)
    if not (perms and (perms.manage_channels or perms.administrator)):
        await interaction.response.send_message(i18n.t("hub.need_perm", lang), ephemeral=True)
        return
    gid = str(interaction.guild_id)
    if action is not None and action.value == "clear":
        db.set_play_channel(gid, None)
        await interaction.response.send_message(i18n.t("setup.channel_cleared", lang), ephemeral=True)
        return
    ch = channel or interaction.channel
    db.set_play_channel(gid, str(ch.id))
    await interaction.response.send_message(
        i18n.t("setup.channel_set", lang, channel=ch.mention), ephemeral=True)


async def teardown_hub(guild) -> dict:
    """機器人退出前清掉自己建立的東西：語音配對房、大廳／開房／配對語音頻道、指南頻道；
    類別只有在裡面沒有別人的頻道時才刪。最後移除該伺服器的資料庫設定。
    回傳 {"deleted": [...], "kept": [...], "failed": [...]}（頻道名稱）。"""
    from .state import _lobby_channels
    from . import voice as _voice
    gid    = str(guild.id)
    lang   = i18n.guild_lang(gid) or i18n.detect_locale(getattr(guild, "preferred_locale", ""))
    hub_list = hubs.all_hubs(gid)
    deleted, kept, failed, done = [], [], [], set()
    deleted_ids, failed_chs = set(), []

    def _get(cid):
        try:
            return guild.get_channel(int(cid)) if cid else None
        except (TypeError, ValueError):
            return None

    async def _del(ch):
        if ch is None or ch.id in done:
            return
        done.add(ch.id)
        try:
            await ch.delete(reason="Suzume Tsuk 退出伺服器前清理")
            deleted.append(ch.name)
            deleted_ids.add(ch.id)
        except Exception:
            failed.append(ch.name)
            failed_chs.append(ch)

    for vcid in list(_voice._voice_rooms):            # 還沒開局的語音配對房
        ch = guild.get_channel(vcid)
        if ch is not None:
            _voice._voice_rooms.pop(vcid, None)
            await _del(ch)
    for h in hub_list:                                 # 每個大廳：大廳、配對語音、建在類別裡的開房頻道
        await _del(_get(h.get("lobby_channel_id")))
        await _del(_get(h.get("hub_voice_id")))
        play = _get(h.get("play_channel_id"))
        if play is not None and str(getattr(play, "category_id", "")) == str(h.get("category_id")):
            await _del(play)
    guide_names = {i18n.t("guide.channel_name", L) for L in i18n.available()}
    for ch in list(guild.text_channels):
        if ch.name in guide_names:
            await _del(ch)

    try:                                               # 向 API 重抓，避免快取裡還留著剛刪掉的頻道
        fresh = await guild.fetch_channels()
    except Exception:
        fresh = None
    for h in hub_list:                                 # 類別只有在裡面沒有別人的頻道時才刪
        cat = _get(h.get("category_id"))
        if cat is None:
            continue
        rest = ([c for c in fresh if getattr(c, "category_id", None) == cat.id and c.id not in done]
                if fresh is not None else [c for c in cat.channels if c.id not in done])
        if rest:
            kept.append(f"{cat.name}（裡面還有 {len(rest)} 個其他頻道）")
        else:
            await _del(cat)

    # 有刪不掉的（多半是權限不足）→ 在文字頻道留言請管理員手動刪；全部刪乾淨就不打擾
    notified = None
    if failed_chs:
        items = "\n".join(f"・**{c.name}**" if isinstance(c, discord.CategoryChannel) else f"・<#{c.id}>"
                          for c in failed_chs)
        me = guild.me
        cands = ([guild.system_channel] if guild.system_channel else []) + list(guild.text_channels)
        for ch in cands:
            if ch is None or ch.id in deleted_ids:
                continue
            try:
                p = ch.permissions_for(me)
                if not (p.view_channel and p.send_messages):
                    continue
                await ch.send(i18n.t("leave.notice", lang, channels=items))
                notified = ch.name
                break
            except Exception:
                continue

    db.delete_guild_settings(gid)
    _lobby_channels.pop(gid, None)
    i18n._guild_cache.pop(gid, None)
    return {"deleted": deleted, "kept": kept, "failed": failed, "notified": notified}


def _all_langs(key: str) -> str:
    """把某個鍵在所有語言的文字串起來（語言選單要讓每個人都看得懂）。"""
    return "🌐 " + " / ".join(dict.fromkeys(i18n.t(key, L) for L in i18n.available()))


def _guide_admin_ok(interaction) -> bool:
    perms = getattr(interaction.user, "guild_permissions", None)
    return bool(perms and (perms.manage_guild or perms.administrator))


class GuideDisplaySelect(discord.ui.Select):
    """指南顯示語言：只重繪這則指南方便閱讀，不動伺服器設定。"""
    def __init__(self, lang: str = None):
        lang = lang or i18n.DEFAULT
        opts = [discord.SelectOption(label=i18n.lang_name(c), value=c) for c in i18n.available()]
        super().__init__(placeholder=_all_langs("guide.disp_select")[:150],
                         min_values=1, max_values=1, options=opts, custom_id="guide:disp", row=1)

    async def callback(self, interaction: discord.Interaction) -> None:
        if not _guide_admin_ok(interaction):
            await interaction.response.send_message(
                i18n.t("hub.need_perm", i18n.get_user_lang(interaction.user.id, interaction.guild_id, interaction.channel)),
                ephemeral=True)
            return
        code = self.values[0]
        await interaction.response.edit_message(content=i18n.t("guide.text", code), view=GuideView(code))


class GuideServerLangSelect(discord.ui.Select):
    """設定伺服器主要語言：沒自訂語言的成員預設用它（同時把指南重繪成該語言）。"""
    def __init__(self, lang: str = None):
        lang = lang or i18n.DEFAULT
        opts = [discord.SelectOption(label=i18n.lang_name(c), value=c) for c in i18n.available()]
        super().__init__(placeholder=_all_langs("guide.srv_select")[:150],
                         min_values=1, max_values=1, options=opts, custom_id="guide:srvlang", row=2)

    async def callback(self, interaction: discord.Interaction) -> None:
        ulang = i18n.get_user_lang(interaction.user.id, interaction.guild_id, interaction.channel)
        if not _guide_admin_ok(interaction):
            await interaction.response.send_message(i18n.t("hub.need_perm", ulang), ephemeral=True)
            return
        code = self.values[0]
        if interaction.guild_id is not None:
            i18n.set_guild_lang(interaction.guild_id, code)
        await interaction.response.edit_message(content=i18n.t("guide.text", code), view=GuideView(code))
        try:
            await interaction.followup.send(
                i18n.t("guide.srv_set", ulang, lang=i18n.lang_name(code)), ephemeral=True)
        except Exception:
            pass


def _guide_lang(message) -> str | None:
    """從指南訊息內容認出它目前顯示的語言（比對標題行）；認不出＝None。"""
    head = ((getattr(message, "content", None) or "").split("\n") or [""])[0].strip()
    for code in i18n.available():
        if head and head == i18n.t("guide.text", code).split("\n")[0].strip():
            return code
    return None


class GuideView(discord.ui.View):
    """加入伺服器時的管理員指南（persistent）：指南顯示語言＋伺服器主要語言＋刪除頻道＋支援連結。"""
    def __init__(self, lang: str = None):
        super().__init__(timeout=None)
        lang = lang or i18n.DEFAULT
        self.create_btn.label = i18n.t("guide.create_btn", lang)
        self.delete_btn.label = i18n.t("guide.delete_btn", lang)
        self.add_item(GuideDisplaySelect(lang))
        self.add_item(GuideServerLangSelect(lang))
        from .config import SUPPORT_URL
        if SUPPORT_URL:
            self.add_item(discord.ui.Button(label="💬 支援伺服器",
                                            style=discord.ButtonStyle.link, url=SUPPORT_URL))

    @discord.ui.button(label="🏗️ 建立大廳", style=discord.ButtonStyle.success,
                       custom_id="guide:create", row=0)
    async def create_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        # 等同 /setup create（權限檢查在裡面），大廳用「指南目前顯示的語言」
        await _do_setup_create(interaction, _guide_lang(interaction.message))

    @discord.ui.button(label="🗑 刪除指南頻道", style=discord.ButtonStyle.danger,
                       custom_id="guide:delete", row=0)
    async def delete_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        perms = getattr(interaction.user, "guild_permissions", None)
        if not (perms and (perms.manage_channels or perms.administrator)):
            await interaction.response.send_message(
                i18n.t("hub.need_perm", i18n.get_user_lang(interaction.user.id, interaction.guild_id, interaction.channel)), ephemeral=True)
            return
        try:
            await interaction.channel.delete(reason="管理員刪除指南頻道")
        except Exception as e:
            await interaction.response.send_message(f"x {e}", ephemeral=True)


class CommandTranslator(app_commands.Translator):
    """斜線指令在地化：依使用者的 Discord 介面語言翻譯指令描述／參數／選項。
    discord.py 預設把所有描述包成 locale_str（原文＝繁中母本），這裡以
    「cmdtr.<原文>」為鍵查 ja/en 語言檔；查無＝保持原文。指令名稱不翻譯。"""
    _MAP = {
        discord.Locale.taiwan_chinese: "zh_tw",
        discord.Locale.chinese: "zh_tw",       # 簡中暫用繁中
        discord.Locale.japanese: "ja",
        discord.Locale.american_english: "en",
        discord.Locale.british_english: "en",
    }

    async def translate(self, string, locale, context):
        lang = self._MAP.get(locale)
        if not lang or lang == i18n.DEFAULT:
            return None
        return i18n.raw("cmdtr." + str(string), lang)


def register(tree) -> None:
    """把指令掛到 tree（由入口 run.py 在 bot/tree 建好後呼叫）。"""
    tree.add_command(mahjong)
    tree.add_command(setup_group)
    tree.command(name="help", description="使用說明（指令一覽・如何開始遊戲）")(cmd_help_top)
    tree.command(name="language", description="設定顯示語言 / 表示言語を設定する")(cmd_language)




# ═══════════════════════════════════════════════════════════════
