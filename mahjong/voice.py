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
"""0.7 語音配對：玩家加入「配對語音」→ 自動開一間 4 人語音房；滿人自動開局。

流程：
  1. 管理員 /setup create 建類別時，順便建一個固定的「配對語音」（hub）。
  2. 玩家進 hub → 在同類別下開「🔊雀月房-N」（限 4 人）並把玩家移過去。
  3. 語音房坐滿 4 個真人 → 自動開一場休閒對局（頻道照類別模式開）。
  4. 語音房沒人了 → 自動刪除。
"""
from __future__ import annotations
import uuid
import discord

from .state import _waiting, _room_owners, _room_configs, _user_game
from . import db
from . import i18n
from . import rooms

# voice_channel_id -> {"starting": bool}
_voice_rooms: dict[int, dict] = {}
_room_seq = 0


def _config_from_settings(sv, lang: str, sanma_override: bool | None = None) -> dict:
    """語音房的對局設定：讀語音文字區設定面板（RoomSettingsView）當下的值；
    沒有面板時用友人場預設。sanma_override＝「三人先開」按鈕強制三麻。"""
    if sv is None:
        sanma = bool(sanma_override)
        return {
            "is_sanma": sanma, "thinking_time": 25, "max_players": 3 if sanma else 4,
            "length": "tonpuu", "tobi": True, "ruleset": "mixed", "start_points": None,
            "kuikae": False, "open_riichi": False,
            "lang": lang, "ranked": False, "open_hand": False,
        }
    sanma = sanma_override if sanma_override is not None else sv.sanma
    return {
        "is_sanma": sanma, "thinking_time": sv.thinking_time,
        "max_players": 3 if sanma else 4,
        "length": "hanchan" if sv.hanchan else "tonpuu",
        "tobi": sv.tobi, "ruleset": sv.ruleset, "start_points": sv.start_points,
        "kuikae": sv.kuikae, "open_riichi": sv.open_riichi,
        "lang": lang, "ranked": False, "open_hand": sv.open_hand,
    }


async def _lobby_channel(guild: discord.Guild, setup: dict):
    """發開局公告用的文字頻道：優先開房頻道（指定遊玩頻道）→ 大廳頻道 → 類別內任一文字頻道。"""
    pc = db.get_play_channel(str(guild.id))
    if pc:
        ch = guild.get_channel(int(pc))
        if isinstance(ch, discord.TextChannel):
            return ch
    lid = setup.get("lobby_channel_id")
    if lid:
        ch = guild.get_channel(int(lid))
        if isinstance(ch, discord.TextChannel):
            return ch
    cid = setup.get("category_id")
    if cid:
        cat = guild.get_channel(int(cid))
        if isinstance(cat, discord.CategoryChannel):
            for ch in cat.text_channels:
                return ch
    return None


async def _clear_sanma_prompt(info: dict) -> None:
    """撤掉「三人可先開」提示（人數變動或已開局時）。"""
    msg = info.pop("sanma_msg", None)
    if msg is not None:
        try:
            await msg.delete()
        except Exception:
            pass


class SanmaStartButton(discord.ui.Button):
    """語音房 3 人時的「先開三麻」按鈕。"""
    def __init__(self, vc_id: int, lang: str):
        super().__init__(style=discord.ButtonStyle.success,
                         label=i18n.t("voice.sanma_btn", lang))
        self._vc_id = vc_id

    async def callback(self, interaction: discord.Interaction) -> None:
        lang = i18n.get_user_lang(str(interaction.user.id))
        vc   = interaction.guild.get_channel(self._vc_id)
        info = _voice_rooms.get(self._vc_id)
        if vc is None or info is None:
            await interaction.response.send_message(i18n.t("msg.no_open_room", lang),
                                                    ephemeral=True)
            return
        humans = [m for m in vc.members if not m.bot]
        free   = [m for m in humans if str(m.id) not in _user_game]
        if not any(m.id == interaction.user.id for m in free):
            await interaction.response.send_message(i18n.t("voice.not_in_room", lang),
                                                    ephemeral=True)
            return
        if len(free) != 3 or info.get("starting"):
            await interaction.response.send_message(i18n.t("voice.sanma_stale", lang),
                                                    ephemeral=True)
            return
        info["starting"] = True
        try:
            await interaction.response.defer()
        except Exception:
            pass
        await _clear_sanma_prompt(info)
        try:
            await vc.edit(user_limit=3, reason="Suzume Tsuk 三麻開局（限 3 人）")
        except Exception:
            pass
        await _start_voice_game(vc, free, sanma=True)


async def _start_voice_game(vc: discord.VoiceChannel, members: list,
                            sanma: bool | None = None) -> None:
    from .flow import launch_game
    guild = vc.guild
    setup = db.get_guild_setup(str(guild.id))
    lobby = await _lobby_channel(guild, setup)
    if lobby is None:
        try:
            await vc.send("❌ 找不到可以開局的文字頻道（大廳被刪了？請重新 /setup create）")
        except Exception:
            pass
        _voice_rooms[vc.id]["starting"] = False
        return
    host = members[0]
    lang = i18n.get_user_lang(str(host.id))
    gid  = str(uuid.uuid4())[:8]
    _waiting[gid] = [{"user_id": str(m.id), "username": m.display_name, "is_bot": False}
                     for m in members]
    _room_owners[gid]  = str(host.id)
    sv = _voice_rooms.get(vc.id, {}).get("settings")
    _room_configs[gid] = _config_from_settings(sv, lang, sanma_override=sanma)
    try:                                   # 設定面板鎖起來（開局後不能再改）
        if sv is not None:
            sv.stop()
            msg = _voice_rooms.get(vc.id, {}).get("settings_msg")
            if msg is not None:
                await msg.edit(view=None)
    except Exception:
        pass
    rooms.register(gid, guild.id, str(lobby.id))
    try:
        await vc.send(i18n.t("voice.starting", lang))
    except Exception:
        pass
    try:
        await launch_game(gid, lobby)
    except Exception as e:
        print(f"[voice] 開局失敗：{e!r}")
        for k in (_waiting, _room_owners, _room_configs):
            k.pop(gid, None)
        _voice_rooms.get(vc.id, {})["starting"] = False
        return
    # 開局成功：語音房掛上牌桌頻道連結；綁定語音房供音效播放（sfx）
    try:
        from .state import _threads
        th = _threads.get(gid)
        if th is not None:
            th["voice"] = vc
        pub = (th or {}).get("public")
        if pub is not None:
            await vc.send(i18n.t("voice.started", lang, channel=pub.mention))
    except Exception:
        pass
    try:
        from . import sfx
        await sfx.play(gid, "start")
    except Exception:
        pass


async def cleanup_room(vc) -> None:
    """對局結束時直接刪除語音配對房（不是本模組開的語音就不動）。"""
    if getattr(vc, "id", None) not in _voice_rooms:
        return
    _voice_rooms.pop(vc.id, None)
    try:
        await vc.delete(reason="Suzume Tsuk 語音房清理（對局結束）")
    except Exception:
        pass


async def handle_voice_update(member: discord.Member, before, after) -> None:
    """on_voice_state_update 入口（run.py 掛上）。"""
    global _room_seq
    if member.bot:
        return
    guild = member.guild

    # 1) 加入配對語音（hub）→ 開一間語音房並移過去
    if after.channel is not None:
        setup = db.get_guild_setup(str(guild.id))
        hub = setup.get("hub_voice_id")
        if hub and str(after.channel.id) == hub:
            cat = after.channel.category
            _room_seq += 1
            lang = i18n.get_user_lang(str(member.id))
            try:
                vc = await guild.create_voice_channel(
                    f"{i18n.t('voice.room_name', lang)}-{_room_seq}",
                    category=cat, user_limit=4,
                    reason="Suzume Tsuk 語音配對房")
            except Exception as e:
                print(f"[voice] 建語音房失敗：{e!r}")
                return
            _voice_rooms[vc.id] = {"starting": False}
            try:
                await member.move_to(vc, reason="Suzume Tsuk 語音配對")
            except Exception:
                # 移不過去（權限不足）→ 房間留著讓玩家自己點進去
                pass
            # 設定面板發在語音房的文字區（開局前都能調；開局時鎖定）
            try:
                from .views import RoomSettingsView
                sv = RoomSettingsView(gid=f"vc{vc.id}", lang=lang, timeout=None)
                msg = await vc.send(i18n.t("voice.settings_hint", lang), view=sv)
                _voice_rooms[vc.id]["settings"] = sv
                _voice_rooms[vc.id]["settings_msg"] = msg
            except Exception as e:
                print(f"[voice] 設定面板發送失敗：{e!r}")
            return

    # 2) 加入受管理的語音房 → 滿員就開局；剛好 3 人時提示可先開三麻
    #    （設定面板選了三麻 → 3 人即滿員直接開三麻；4 人一律四麻）
    if after.channel is not None and after.channel.id in _voice_rooms:
        room = after.channel
        info = _voice_rooms[room.id]
        sv   = info.get("settings")
        humans = [m for m in room.members if not m.bot]
        free   = [m for m in humans if str(m.id) not in _user_game]
        if len(free) >= 4 and not info["starting"]:
            info["starting"] = True
            await _clear_sanma_prompt(info)
            await _start_voice_game(room, free[:4], sanma=False)
        elif len(free) == 3 and not info["starting"] and sv is not None and sv.sanma:
            info["starting"] = True
            await _clear_sanma_prompt(info)
            try:
                await room.edit(user_limit=3, reason="Suzume Tsuk 三麻開局（限 3 人）")
            except Exception:
                pass
            await _start_voice_game(room, free[:3], sanma=True)
        elif len(free) == 3 and not info["starting"] and info.get("sanma_msg") is None:
            lang = i18n.get_user_lang(str(member.id))
            try:
                v = discord.ui.View(timeout=None)
                v.add_item(SanmaStartButton(room.id, lang))
                info["sanma_msg"] = await room.send(i18n.t("voice.sanma_hint", lang), view=v)
            except Exception as e:
                print(f"[voice] 三麻提示發送失敗：{e!r}")

    # 3) 離開受管理的語音房 → 空了就刪；掉到 3 人以下撤三麻提示
    if before.channel is not None and before.channel.id in _voice_rooms:
        room = before.channel
        info = _voice_rooms.get(room.id, {})
        humans = [m for m in room.members if not m.bot]
        if not humans:
            _voice_rooms.pop(room.id, None)
            try:
                await room.delete(reason="Suzume Tsuk 語音房清理（已無人）")
            except Exception:
                pass
        elif len(humans) < 3 and info.get("sanma_msg") is not None:
            await _clear_sanma_prompt(info)
