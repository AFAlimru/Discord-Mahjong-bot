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
from . import db
from . import i18n
from . import rooms
from .render import make_board_text
from . import tiles as T
from .ui import HelpButton, help_text
from .views import RoomSettingsView
from .flow import launch_game, launch_ranked_game, launch_dm_game, _cleanup, _delete_threads, _delete_announce
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
        plist     = "\n".join(
            f"• {'🤖' if p.get('is_bot') else ''}{p['username']}"
            for p in players
        )
        return (
            f"**{i18n.t('lobby.title', lang)}**\n"
            f"{i18n.t('lobby.info', lang, mode=mode, length=length, tobi=tobi, tt=tt)}\n"
            f"{i18n.t('lobby.players', lang, cur=len(players), max=max_p)}\n{plist}\n\n"
            f"{i18n.t('lobby.join_hint', lang)}"
        )

    @discord.ui.button(label="加入遊戲", style=discord.ButtonStyle.success)
    async def join_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        gid     = self.gid
        uid     = str(interaction.user.id)
        cfg     = _room_configs.get(gid, {})
        max_p   = cfg.get("max_players", 4)
        waiting = _waiting.get(gid, [])

        if any(p["user_id"] == uid for p in waiting):
            await interaction.response.send_message(i18n.t("msg.already_in_room", i18n.get_user_lang(interaction.user.id)), ephemeral=True)
            return
        if len(waiting) >= max_p:
            await interaction.response.send_message(i18n.t("msg.room_full", i18n.get_user_lang(interaction.user.id)), ephemeral=True)
            return

        await _close_owned_waiting_rooms(uid, gid)   # 加入別人的房 → 關掉自己開的房
        _waiting[gid].append({"user_id": uid, "username": interaction.user.display_name, "is_bot": False})
        await self._update(interaction)

    @discord.ui.button(label="加入 AI", style=discord.ButtonStyle.secondary)
    async def ai_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        gid     = self.gid
        uid     = str(interaction.user.id)
        if _room_owners.get(gid) != uid:
            await interaction.response.send_message(i18n.t("msg.only_host_ai", i18n.get_user_lang(interaction.user.id)), ephemeral=True)
            return
        cfg   = _room_configs.get(gid, {})
        max_p = cfg.get("max_players", 4)
        waiting = _waiting.get(gid, [])
        if len(waiting) >= max_p:
            await interaction.response.send_message(i18n.t("msg.room_full", i18n.get_user_lang(interaction.user.id)), ephemeral=True)
            return

        ai_uid  = f"ai_{gid}_{self._ai_count}"
        ai_name = AI_NAMES[self._ai_count % len(AI_NAMES)]
        self._ai_count += 1
        _waiting[gid].append({"user_id": ai_uid, "username": ai_name, "is_bot": True})
        await self._update(interaction)

    @discord.ui.button(label="🌐", style=discord.ButtonStyle.secondary)   # 標籤於 __init__ 依房間語言設定
    async def translate_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        lang = i18n.get_user_lang(interaction.user.id)
        await interaction.response.send_message(self._content(lang), ephemeral=True)

    async def _update(self, interaction: discord.Interaction):
        gid   = self.gid
        cfg   = _room_configs.get(gid, {})
        max_p = cfg.get("max_players", 4)
        count = len(_waiting.get(gid, []))
        content = self._content(self.lang)

        if count >= max_p:
            for child in self.children:
                child.disabled = True
            content += "\n\n" + i18n.t("lobby.full", self.lang)
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
        content = self._content(self.lang)
        if count >= max_p:
            for child in self.children:
                child.disabled = True
            content += "\n\n" + i18n.t("lobby.full", self.lang)
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
    elif channel_id in _channel_games:
        await interaction.response.send_message(i18n.t("msg.channel_has_game", i18n.get_user_lang(interaction.user.id)), ephemeral=True)
        return

    host_lang = i18n.get_user_lang(user_id)
    sv = RoomSettingsView(gid="setup", lang=host_lang)
    await interaction.response.send_message(i18n.t("settings.prompt", host_lang), view=sv, ephemeral=True)
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
        "kuikae": sv.kuikae, "open_riichi": sv.open_riichi,
        "lang": i18n.get_user_lang(user_id),   # 房間顯示語言＝房主語言（公開內容/翻譯按鈕用）
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
    gid        = _channel_games.get(channel_id)

    if not gid:
        await interaction.response.send_message(
            i18n.t("msg.no_open_room", i18n.get_user_lang(interaction.user.id)), ephemeral=True)
        return
    if gid in _games:
        await interaction.response.send_message(i18n.t("msg.game_started_nojoin", i18n.get_user_lang(interaction.user.id)), ephemeral=True)
        return
    if host is not None and _room_owners.get(gid) != str(host.id):
        await interaction.response.send_message(
            i18n.t("msg.not_host_named", i18n.get_user_lang(interaction.user.id),
                   host=host.display_name), ephemeral=True)
        return

    cfg     = _room_configs.get(gid, {})
    max_p   = cfg.get("max_players", 4)
    waiting = _waiting.get(gid, [])

    if any(p["user_id"] == uid for p in waiting):
        await interaction.response.send_message(i18n.t("msg.already_in_room", i18n.get_user_lang(interaction.user.id)), ephemeral=True)
        return
    if len(waiting) >= max_p:
        await interaction.response.send_message(i18n.t("msg.room_full", i18n.get_user_lang(interaction.user.id)), ephemeral=True)
        return

    await _close_owned_waiting_rooms(uid, gid)   # 加入別人的房 → 關掉自己開的房
    waiting.append({"user_id": uid, "username": interaction.user.display_name, "is_bot": False})
    await interaction.response.send_message(
        i18n.t("msg.joined", i18n.get_user_lang(interaction.user.id),
               cur=len(waiting), max=max_p), ephemeral=True)

    lobby = _lobbies.get(gid)
    if lobby:
        await lobby.push_update()


@mahjong.command(name="end", description="強制結束牌局（房主）")
async def cmd_end(interaction: discord.Interaction) -> None:
    channel_id = str(interaction.channel_id)
    user_id    = str(interaction.user.id)
    # 可在主頻道或任一遊戲討論串內使用；DM 對局（跟電腦打）用玩家對照找
    gid = (_channel_games.get(channel_id) or _thread_game.get(int(channel_id))
           or (_user_game.get(user_id) if interaction.guild_id is None else None))
    if not gid:
        await interaction.response.send_message(i18n.t("msg.no_game", i18n.get_user_lang(interaction.user.id)), ephemeral=True)
        return
    lang = i18n.get_user_lang(interaction.user.id)
    if _room_owners.get(gid) != user_id:
        await interaction.response.send_message(i18n.t("msg.only_host_end", lang), ephemeral=True)
        return
    # 找出主頻道 id（給 _cleanup 用）
    parent_cid = next((c for c, g in _channel_games.items() if g == gid), channel_id)
    gs    = _games.get(gid)
    task  = _game_tasks.get(gid)
    th    = _threads.get(gid)
    lobby = _lobbies.get(gid)
    closed = i18n.t("msg.room_closed", lang)

    # 先回覆房主（要在 3 秒內回應 interaction，避免後續 await 拖過時限）
    await interaction.response.send_message(closed, ephemeral=True)

    # 1) DB 標記為結束，避免機器人重啟時被當成「中斷的對局」要求回復
    if gs is not None:
        try:
            db.finish_game(gid, gs.to_dict())
        except Exception as e:
            print(f"[end] finish_game 失敗：{e}")

    # 2) 等人中的大廳訊息 → 關閉
    if lobby is not None:
        try:
            lobby.stop()
        except Exception:
            pass
        lm = getattr(lobby, "lobby_message", None)
        if lm:
            try:
                await lm.edit(content=closed, view=None)
            except Exception:
                pass

    # 3) 主頻道開局公告 → 刪除
    if th:
        await _delete_announce(th)

    # 4) 停掉對局迴圈
    if task and not task.done():
        task.cancel()

    # 5) 每個討論串貼「房間已關閉」，稍候再刪除
    #    DM 對局沒有討論串（private 是 DM 頻道，不能刪也不用刪），跳過刪除與權限警告
    if th:
        for t in [th.get("public")] + list(th.get("private", {}).values()):
            if t is None:
                continue
            try:
                await t.send(closed)
            except Exception:
                pass
        if not th.get("is_dm"):
            await asyncio.sleep(3)
            failed = await _delete_threads(th)
            if failed:
                try:
                    await interaction.followup.send(i18n.t("msg.thread_delete_fail", lang), ephemeral=True)
                except Exception:
                    pass

    _cleanup(gid, parent_cid)


@mahjong.command(name="status", description="查看當前牌局狀態")
async def cmd_status(interaction: discord.Interaction) -> None:
    lang       = i18n.get_user_lang(interaction.user.id)
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
            lines.insert(0, f"🏅 **{_rt.dan_name(dan['dan_idx'])}**（{dan['dan_pt']}pt）"
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
        lines.append(f"`{L('stats.gain')}` {signed(summ['gain_points'])}　`{L('stats.lost')}` {summ['houju_points'] or 0}")
        embed.add_field(name=i18n.t("mode.yonma" if is4 else "mode.sanma", lang),
                        value="\n".join(lines), inline=False)
    await interaction.response.send_message(embed=embed)   # 公開顯示（非 ephemeral）


class RankQueueView(discord.ui.View):
    """段位賽排隊中的「離開」按鈕。"""
    def __init__(self, lang: str):
        super().__init__(timeout=900)
        self.lang = lang

    @discord.ui.button(label="🚪 離開排隊", style=discord.ButtonStyle.danger)
    async def leave_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        lang = i18n.get_user_lang(interaction.user.id)
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


async def _do_rank_match(interaction: discord.Interaction, sanma: bool) -> None:
    """段位賽排隊（指令與大廳按鈕共用）。"""
    lang  = i18n.get_user_lang(interaction.user.id)
    res   = matchmaking.join(interaction.user, sanma)
    kind = res[0]
    if kind == "in_game":
        await interaction.response.send_message(i18n.t("rank.in_game", lang), ephemeral=True)
    elif kind in ("queued", "already"):
        _, cur, mx = res
        key = "rank.queued" if kind == "queued" else "rank.already"
        await interaction.response.send_message(
            i18n.t(key, lang, cur=cur, max=mx), ephemeral=True, view=RankQueueView(lang))
    elif kind == "matched":
        _, players, mode, _k = res
        await interaction.response.send_message(i18n.t("rank.matched_you", lang), ephemeral=True)
        await launch_ranked_game(players, mode)


async def _do_casual_match(interaction: discord.Interaction, sanma: bool) -> None:
    """休閒隨機匹配（跨伺服器、對局走 DM、不計段位）。"""
    from .flow import launch_match_game
    lang = i18n.get_user_lang(interaction.user.id)
    res  = matchmaking.join(interaction.user, sanma, kind="casual")
    kind = res[0]
    if kind == "in_game":
        await interaction.response.send_message(i18n.t("rank.in_game", lang), ephemeral=True)
    elif kind in ("queued", "already"):
        _, cur, mx = res
        key = "match.queued" if kind == "queued" else "match.already"
        await interaction.response.send_message(
            i18n.t(key, lang, cur=cur, max=mx), ephemeral=True, view=RankQueueView(lang))
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
    lang   = i18n.get_user_lang(interaction.user.id)
    ladder = "　→　".join(rating.DAN_NAMES)
    await interaction.response.send_message(i18n.t("rank.info", lang, ladder=ladder), ephemeral=True)


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
    lang = i18n.get_user_lang(interaction.user.id)
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
                i18n.t("skin.locked", lang, dan=_rt.DAN_NAMES[_rt.BLACK_SKIN_IDX]), ephemeral=True)
            return
    db.set_user_skin(uid, None if val == "default" else val)
    _skin_cache.pop(uid, None)   # 對局中的快取立即失效，下一次渲染就換
    await interaction.response.send_message(i18n.t("skin.set", lang, name=label), ephemeral=True)


@mahjong.command(name="daily", description="每日簽到，獲得活躍度")
async def cmd_daily(interaction: discord.Interaction) -> None:
    lang = i18n.get_user_lang(interaction.user.id)
    r = db.checkin(str(interaction.user.id), interaction.user.display_name)
    key = "daily.already" if r["already"] else "daily.done"
    await interaction.response.send_message(
        i18n.t(key, lang, reward=r["reward"], streak=r["streak"], activity=r["activity"]))


@mahjong.command(name="tasks", description="查看每日任務與活躍度")
async def cmd_tasks(interaction: discord.Interaction) -> None:
    lang = i18n.get_user_lang(interaction.user.id)
    s    = db.task_status(str(interaction.user.id))
    mk   = lambda b: i18n.t("task.done" if b else "task.todo", lang)
    embed = discord.Embed(title=i18n.t("task.title", lang), color=0x9ECE6A)
    embed.description = (
        f"{mk(s['checkin'])} {i18n.t('task.checkin', lang)}\n"
        f"{mk(s['played'])} {i18n.t('task.play', lang)}\n\n"
        f"{i18n.t('task.activity', lang)} **{s['activity']}**　"
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
    lines.append(f"{L('profile.activity')}：**{act.get('activity', 0)}**"
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
            lines.append(T.emojify(hand))
    else:
        lines.append(f"{L('profile.best_win')}：{L('profile.none')}")

    embed.description = "\n".join(lines)
    return embed


@mahjong.command(name="profile", description="查看個人資訊卡")
async def cmd_profile(interaction: discord.Interaction) -> None:
    await interaction.response.send_message(embed=_profile_embed(interaction.user))


_HIST_PAGE = 10
_MEDALS = ["🥇", "🥈", "🥉", "4️⃣"]


def _history_embed(uid: str, mode: str, page: int, lang: str):
    total = db.count_records(uid, mode)
    pages = max(1, (total + _HIST_PAGE - 1) // _HIST_PAGE)
    page  = max(0, min(page, pages - 1))
    rows  = db.get_recent_records(uid, mode, _HIST_PAGE, offset=page * _HIST_PAGE)
    lines = []
    for r in rows:
        room = f"`#{r['room_no']:04d}`" if r.get("room_no") else "`  —  `"
        day  = (r["created_at"] or "")[:10]
        sd   = r["score_delta"] or 0
        lines.append(f"{room}　{_MEDALS[r['rank'] - 1]}　{r['score']}（{'+' if sd >= 0 else ''}{sd}）　{day}")
    embed = discord.Embed(
        title=i18n.t("history.title", lang, mode=i18n.t("mode." + mode, lang)), color=0x7AA2F7)
    embed.description = "\n".join(lines) or i18n.t("history.empty", lang)
    embed.set_footer(text=i18n.t("history.page", lang, page=page + 1, total=pages))
    return embed, page, pages


class HistoryView(discord.ui.View):
    def __init__(self, uid: str, mode: str, page: int, pages: int, lang: str):
        super().__init__(timeout=300)
        self.uid, self.mode, self.page, self.pages, self.lang = uid, mode, page, pages, lang
        self._sync()

    def _sync(self):
        self.prev.disabled = self.page <= 0
        self.nxt.disabled  = self.page >= self.pages - 1

    async def _edit(self, interaction, delta):
        self.page += delta
        embed, self.page, self.pages = _history_embed(self.uid, self.mode, self.page, self.lang)
        self._sync()
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._edit(interaction, -1)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def nxt(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._edit(interaction, +1)


@mahjong.command(name="history", description="查看自己的牌譜（分頁）")
@app_commands.describe(mode="選擇人數模式（不選＝四人）")
@app_commands.choices(mode=[
    app_commands.Choice(name="四人麻將", value="yonma"),
    app_commands.Choice(name="三人麻將", value="sanma"),
])
async def cmd_history(interaction: discord.Interaction,
                      mode: app_commands.Choice[str] = None) -> None:
    uid  = str(interaction.user.id)
    lang = i18n.get_user_lang(uid)
    m    = mode.value if mode is not None else "yonma"
    embed, page, pages = _history_embed(uid, m, 0, lang)
    if pages > 1:
        await interaction.response.send_message(
            embed=embed, view=HistoryView(uid, m, page, pages, lang), ephemeral=True)
    else:
        await interaction.response.send_message(embed=embed, ephemeral=True)


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
        self.thread = None
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

    @discord.ui.button(label="✖", style=discord.ButtonStyle.danger, row=1)
    async def quit(self, interaction, button):     # 退出 → 刪掉回放討論串
        self.play.clear()
        ch = self.thread or (self.msg.channel if self.msg else interaction.channel)
        try:
            await interaction.response.defer()
        except Exception:
            pass
        try:
            if isinstance(ch, discord.Thread):
                await ch.delete()
        except Exception:
            pass
        self.stop()


@mahjong.command(name="replay", description="輸入房號回放該場對局")
@app_commands.describe(room="房號（例：1 代表 房間#0001）")
async def cmd_replay(interaction: discord.Interaction, room: int) -> None:
    await _start_replay(interaction, room)


async def _start_replay(interaction: discord.Interaction, room: int) -> None:
    from . import replay
    lang = i18n.get_user_lang(interaction.user.id)
    g = db.get_game_by_room_no(room)
    if not g:
        await interaction.response.send_message(i18n.t("replay.not_found", lang, room=room), ephemeral=True)
        return
    frames, seats, n = replay.build_frames(db.get_game_logs(g["game_id"]), lang)
    if not frames or not seats:
        await interaction.response.send_message(i18n.t("replay.no_log", lang, room=room), ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        target = await interaction.channel.create_thread(
            name=i18n.t("replay.title", lang, room=room),
            type=discord.ChannelType.public_thread)
    except Exception:
        target = interaction.channel
    ctrl = ReplayControl(frames, seats, lang)
    ctrl.thread = target if isinstance(target, discord.Thread) else None
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


@mahjong.command(name="repair", description="掃描並修復戰績資料（限管理員）")
async def cmd_repair(interaction: discord.Interaction) -> None:
    perms = getattr(interaction.user, "guild_permissions", None)
    if not (perms and perms.administrator):
        await interaction.response.send_message("此指令僅限伺服器管理員使用。", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        rep = db.repair_game_records()
    except Exception as e:
        await interaction.followup.send(f"❌ 修復失敗：{e}", ephemeral=True)
        return
    await interaction.followup.send(
        "🛠️ **戰績修復完成**\n"
        f"・掃描紀錄：{rep['scanned']} 筆（{rep['games']} 場）\n"
        f"・去除重複：{rep['dups']} 筆\n"
        f"・依結算牌譜重算次數：{rep['counter_fixed']} 筆\n"
        f"・負值歸零：{rep['clamp_fixed']} 筆\n"
        f"・無牌譜可重算的舊對局：{rep['no_log_games']} 場（立直等逐手資料無法回填）",
        ephemeral=True)


# ═══════════════════════════════════════════════════════════════
#  Top-level commands（由 register(tree) 在入口掛上）
# ═══════════════════════════════════════════════════════════════

async def cmd_help_top(interaction: discord.Interaction) -> None:
    lang = i18n.get_user_lang(interaction.user.id)
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
    cur = i18n.get_user_lang(interaction.user.id)
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
    room = discord.ui.TextInput(label="房號（數字）", placeholder="例：12", min_length=1, max_length=8)

    def __init__(self, lang: str):
        super().__init__(title="🎞 回放")

    async def on_submit(self, interaction: discord.Interaction):
        raw = str(self.room.value).strip().lstrip("#")
        if not raw.isdigit():
            await interaction.response.send_message("❌ 房號要是數字。", ephemeral=True)
            return
        await _start_replay(interaction, int(raw))


class LobbyPanel(discord.ui.View):
    """大廳常駐面板（persistent view：custom_id 固定、重啟後由 run.py 重新掛上）。
    lang：面板按鈕標籤語言（建立大廳時依建立者語言；分發只看 custom_id，與標籤無關）。"""
    def __init__(self, lang: str = None):
        super().__init__(timeout=None)
        lang = lang or i18n.DEFAULT
        labels = {"rank": "hub.btn.rank", "match": "hub.btn.match", "daily": "hub.btn.daily",
                  "profile": "hub.btn.profile", "skin": "hub.btn.skin", "history": "hub.btn.history",
                  "replay": "hub.btn.replay", "yaku": "hub.btn.yaku", "lang": "hub.btn.lang",
                  "news": "hub.btn.news"}
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
        lang = i18n.get_user_lang(interaction.user.id)
        await interaction.response.send_message(
            i18n.t("hub.pick_mode", lang), view=_ModePick(True, lang), ephemeral=True)

    @discord.ui.button(label="🎲 休閒場", style=discord.ButtonStyle.success,
                       custom_id="hub:match", row=0)
    async def match(self, interaction: discord.Interaction, button: discord.ui.Button):
        lang = i18n.get_user_lang(interaction.user.id)
        await interaction.response.send_message(
            i18n.t("hub.pick_mode", lang), view=_ModePick(False, lang), ephemeral=True)

    @discord.ui.button(label="✅ 每日簽到", style=discord.ButtonStyle.secondary,
                       custom_id="hub:daily", row=0)
    async def daily(self, interaction: discord.Interaction, button: discord.ui.Button):
        lang = i18n.get_user_lang(interaction.user.id)
        r = db.checkin(str(interaction.user.id), interaction.user.display_name)
        key = "daily.already" if r["already"] else "daily.done"
        await interaction.response.send_message(
            i18n.t(key, lang, reward=r["reward"], streak=r["streak"], activity=r["activity"]),
            ephemeral=True)

    @discord.ui.button(label="👤 個人資訊", style=discord.ButtonStyle.secondary,
                       custom_id="hub:profile", row=1)
    async def profile(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(embed=_profile_embed(interaction.user), ephemeral=True)

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
        uid  = str(interaction.user.id)
        lang = i18n.get_user_lang(uid)
        embed, page, pages = _history_embed(uid, "yonma", 0, lang)
        if pages > 1:
            await interaction.response.send_message(
                embed=embed, view=HistoryView(uid, "yonma", page, pages, lang), ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="🎞 回放", style=discord.ButtonStyle.secondary,
                       custom_id="hub:replay", row=1)
    async def replay(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(_ReplayModal(i18n.get_user_lang(interaction.user.id)))

    @discord.ui.button(label="📖 役種", style=discord.ButtonStyle.secondary,
                       custom_id="hub:yaku", row=2)
    async def yaku(self, interaction: discord.Interaction, button: discord.ui.Button):
        lang = i18n.get_user_lang(interaction.user.id)
        await interaction.response.send_message(embed=_yaku_embed(lang), ephemeral=True)

    @discord.ui.button(label="🗣 語言", style=discord.ButtonStyle.secondary,
                       custom_id="hub:lang", row=2)
    async def lang_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        cur = i18n.get_user_lang(interaction.user.id)
        await interaction.response.send_message(
            i18n.t("language.choose", cur, cur=i18n.lang_name(cur)),
            view=LanguageView(), ephemeral=True)

    @discord.ui.button(label="📢 公告", style=discord.ButtonStyle.secondary,
                       custom_id="hub:news", row=2)
    async def news(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(_latest_changelog(), ephemeral=True)


setup_group = app_commands.Group(name="setup", description="伺服器設定（遊戲大廳等）")


@setup_group.command(name="create", description="建立遊戲大廳頻道（唯讀＋常駐按鈕面板；需管理頻道權限）")
async def cmd_setup_create(interaction: discord.Interaction) -> None:
    lang = i18n.get_user_lang(interaction.user.id)
    if interaction.guild_id is None:
        await interaction.response.send_message(i18n.t("msg.guild_only", lang), ephemeral=True)
        return
    perms = getattr(interaction.user, "guild_permissions", None)
    if not (perms and (perms.manage_channels or perms.administrator)):
        await interaction.response.send_message(i18n.t("hub.need_perm", lang), ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
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
            create_public_threads=True, send_messages_in_threads=True),
    }
    ch = None
    note = ""
    try:
        ch = await guild.create_text_channel(name, overwrites=overwrites,
                                             reason="Suzume Tsuk 遊戲大廳")
    except discord.Forbidden:
        # 降級 1：不帶 overwrites 建立
        try:
            ch = await guild.create_text_channel(name, reason="Suzume Tsuk 遊戲大廳")
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
    try:
        await ch.send(i18n.t("hub.panel", lang), view=LobbyPanel(lang))
    except Exception as e:
        await interaction.followup.send(
            i18n.t("hub.create_fail", lang) + f"\n`{e}`", ephemeral=True)
        return
    await interaction.followup.send(
        i18n.t("hub.created", lang, channel=ch.mention) + note, ephemeral=True)


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
