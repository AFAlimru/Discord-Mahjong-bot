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
  2. 玩家進 hub → 在同類別下開「🔊 雀月房 K7Q2M」（房號代碼，開局沿用；限 4 人）並把玩家移過去。
  3. 語音房坐滿 4 個真人 → 自動開一場休閒對局（頻道照類別模式開）。
  4. 語音房沒人了 → 自動刪除。
"""
from __future__ import annotations
import uuid
import discord

from .state import _waiting, _room_owners, _room_configs, _user_game
from .config import AI_NAMES
from . import db
from . import i18n
from . import rooms
from . import hubs

# voice_channel_id -> {"starting": bool, "room_no", "hub_id", "settings", ...}
_voice_rooms: dict[int, dict] = {}


def _config_from_settings(sv, lang: str, sanma_override: bool | None = None) -> dict:
    """語音房的對局設定：讀語音文字區設定面板（RoomSettingsView）當下的值；
    沒有面板時用友人場預設。sanma_override＝「三人先開」按鈕強制三麻。"""
    if sv is None:
        sanma = bool(sanma_override)
        return {
            "is_sanma": sanma, "thinking_time": 25, "max_players": 3 if sanma else 4,
            "length": "tonpuu", "tobi": True, "ruleset": "mixed", "start_points": None,
            "kuikae": False, "open_riichi": False, "abortive": True, "wall": "riichi_city", "ai_level": "normal",
            "lang": lang, "ranked": False, "open_hand": False,
        }
    sanma = sanma_override if sanma_override is not None else sv.sanma
    return {
        "is_sanma": sanma, "thinking_time": sv.thinking_time,
        "max_players": 3 if sanma else 4,
        "length": "hanchan" if sv.hanchan else "tonpuu",
        "tobi": sv.tobi, "ruleset": sv.ruleset, "start_points": sv.start_points,
        "kuikae": sv.kuikae, "open_riichi": sv.open_riichi, "abortive": sv.abortive,
        "wall": sv.wall_method, "ai_level": sv.ai_level,
        "lang": lang, "ranked": False, "open_hand": sv.open_hand,
    }


async def _lobby_channel(guild: discord.Guild, hub: dict | None):
    """發開局公告用的文字頻道：這個大廳的開房頻道 → 大廳頻道 → 類別內任一文字頻道 → 伺服器指定遊玩頻道。"""
    hub = hub or {}
    for cid in (hub.get("play_channel_id"), hub.get("lobby_channel_id")):
        if cid:
            ch = guild.get_channel(int(cid))
            if isinstance(ch, discord.TextChannel):
                return ch
    cid = hub.get("category_id")
    if cid:
        cat = guild.get_channel(int(cid))
        if isinstance(cat, discord.CategoryChannel):
            for ch in cat.text_channels:
                return ch
    pc = db.get_play_channel(str(guild.id))
    if pc:
        ch = guild.get_channel(int(pc))
        if isinstance(ch, discord.TextChannel):
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
        lang = i18n.get_user_lang(str(interaction.user.id), interaction.guild_id, interaction.channel)
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
            await vc.edit(user_limit=4, reason="Suzume Tsuk 三麻開局（3 人＋機器人）")
        except Exception:
            pass
        await _start_voice_game(vc, free, sanma=True)


class CpuStartButton(discord.ui.Button):
    """語音房「與電腦開始」按鈕：用 AI 補滿剩下座位，立刻開一場休閒對局。"""
    def __init__(self, vc_id: int, lang: str):
        super().__init__(style=discord.ButtonStyle.secondary,
                         label=i18n.t("voice.cpu_btn", lang))
        self._vc_id = vc_id

    async def callback(self, interaction: discord.Interaction) -> None:
        lang = i18n.get_user_lang(str(interaction.user.id), interaction.guild_id, interaction.channel)
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
        if info.get("starting"):
            await interaction.response.send_message(i18n.t("voice.sanma_stale", lang),
                                                    ephemeral=True)
            return
        info["starting"] = True
        try:
            await interaction.response.defer()
        except Exception:
            pass
        await _clear_sanma_prompt(info)
        await _start_voice_game(vc, free, fill_ai=True)


class VoicePackSelect(discord.ui.Select):
    """切換**自己的**角色語音（個人設定；語音包＝assets/sounds 下的資料夾，用選單不用打字）。
    對局中誰的動作就用誰選的聲音；沒選＝他的動作不出聲。uid＝標出這人目前的選擇（共用面板不標）。"""
    NONE = "__none__"

    def __init__(self, lang: str, row: int = 4, uid: str | None = None, ai: bool = False):
        from . import sfx
        self.ai = ai                           # True＝設定「電腦語音」（自己開的房裡電腦用的聲音）
        get  = db.get_user_ai_voice if ai else db.get_user_voice
        cur  = get(str(uid)) if uid else None
        opts = [discord.SelectOption(label=i18n.t("voice.ai_off" if ai else "voice.pack_off", lang),
                                     value=self.NONE)]
        opts += [discord.SelectOption(label=p[:100], value=p[:100], default=(p == cur))
                 for p in sfx.list_packs()[:24]]
        super().__init__(placeholder=i18n.t("voice.ai_select" if ai else "voice.pack_select", lang),
                         min_values=1, max_values=1, options=opts, row=row)

    async def callback(self, interaction: discord.Interaction) -> None:
        ulang = i18n.get_user_lang(str(interaction.user.id), interaction.guild_id, interaction.channel)
        val   = self.values[0]
        uid   = str(interaction.user.id)
        if self.ai:
            db.set_user_ai_voice(uid, None if val == self.NONE else val)
            msg = (i18n.t("voice.ai_cleared", ulang) if val == self.NONE
                   else i18n.t("voice.ai_set", ulang, pack=f"**{val}**"))
            await interaction.response.send_message(msg, ephemeral=True)
            return
        if val == self.NONE:
            db.set_user_voice(uid, None)
            msg = i18n.t("voice.pack_cleared", ulang)
        else:
            db.set_user_voice(uid, val)
            msg = i18n.t("voice.pack_set", ulang, pack=f"**{val}**")
        await interaction.response.send_message(msg, ephemeral=True)
        if val != self.NONE:                   # 人在配對語音房裡選了語音包 → 機器人先進房
            room = getattr(getattr(interaction.user, "voice", None), "channel", None)
            if room is not None and room.id in _voice_rooms:
                try:
                    from . import sfx
                    await sfx.join_room(room, str(interaction.user.id))
                except Exception as e:
                    print(f"[voice] 機器人進語音房失敗：{e!r}")


async def _start_voice_game(vc: discord.VoiceChannel, members: list,
                            sanma: bool | None = None, fill_ai: bool = False) -> None:
    from .flow import launch_game
    guild = vc.guild
    hub   = (hubs.by_id(guild.id, _voice_rooms.get(vc.id, {}).get("hub_id"))
             or hubs.of_channel(guild.id, vc))         # 這間語音房是從哪個大廳開的
    lobby = await _lobby_channel(guild, hub)
    if lobby is None:
        try:
            await vc.send("❌ 找不到可以開局的文字頻道（大廳被刪了？請重新 /setup create）")
        except Exception:
            pass
        _voice_rooms[vc.id]["starting"] = False
        return
    # 語音局沒有「開房者」→ 房主固定給**東家**（起家＝members[0]＝seat 0＝莊家）
    host = members[0]
    lang = hubs.lang_for(guild.id, vc)     # 房間語言＝這個大廳的語言（只有一個大廳＝伺服器語言）
    gid  = str(uuid.uuid4())[:8]
    _waiting[gid] = [{"user_id": str(m.id), "username": m.display_name, "is_bot": False}
                     for m in members]
    _room_owners[gid]  = str(host.id)
    sv = _voice_rooms.get(vc.id, {}).get("settings")
    _room_configs[gid] = _config_from_settings(sv, lang, sanma_override=sanma)
    if fill_ai:                            # 用電腦補滿剩下的座位
        max_p = _room_configs[gid]["max_players"]
        for i in range(max_p - len(_waiting[gid])):
            _waiting[gid].append({"user_id": f"ai_{gid}_{i}",
                                  "username": AI_NAMES[i % len(AI_NAMES)],
                                  "is_bot": True})
    try:                                   # 開局時把設定面板整則刪掉（不留死面板）
        if sv is not None:
            sv.stop()
            msg = _voice_rooms.get(vc.id, {}).get("settings_msg")
            if msg is not None:
                await msg.delete()
                _voice_rooms.get(vc.id, {}).pop("settings_msg", None)
    except Exception:
        pass
    rooms.register(gid, guild.id, str(lobby.id), _voice_rooms.get(vc.id, {}).get("room_no"))
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
    # 開局成功：語音房掛上牌桌頻道連結＋房主資訊＋結束按鈕；綁定語音房供音效播放（sfx）
    try:
        from .state import _threads, _thread_game
        from .flow import EndGameButton
        from .flow import save_surfaces
        th = _threads.get(gid)
        if th is not None:
            th["voice"] = vc
            save_surfaces(gid)             # 記下語音房（重啟後中斷→結束時一併收掉）
        _thread_game[vc.id] = gid          # 讓 /end 等指令能在語音房文字區使用
        pub = (th or {}).get("public")
        if pub is not None:
            v = discord.ui.View(timeout=None)
            v.add_item(EndGameButton(gid, lang))
            await vc.send(
                i18n.t("voice.started", lang, channel=pub.mention) + "\n" +
                i18n.t("voice.host", lang, host=host.mention), view=v)
    except Exception as e:
        print(f"[voice] 開局訊息發送失敗：{e!r}")
    await fit_room_limit(vc, _room_configs.get(gid, {}).get("max_players", 4))
    try:
        from . import sfx
        await sfx.join(vc, gid)          # 開局即進語音（有人選了語音包才進）
        await sfx.play_start(gid)        # 開局音＝房主（或其他有選的真人）的語音包
    except Exception:
        pass


async def fit_room_limit(vc, max_players: int) -> None:
    """語音房人數上限＝這局人數＋1（三麻 4、四麻 5）：多的那一格留給機器人。
    機器人開局前就會進房播語音，也會佔一個名額；不留的話人還沒到齊房間就滿了。"""
    limit = max_players + 1
    if getattr(vc, "user_limit", None) != limit:
        try:
            await vc.edit(user_limit=limit, reason="Suzume Tsuk 依對局人數設定語音房上限")
        except Exception:
            pass


def _room_in_game(room_id: int) -> bool:
    """這間語音房綁著還在進行的對局嗎（對局結束時由 cleanup_room 收掉）。"""
    from .state import _threads, _games
    return any(th.get("voice") is not None and th["voice"].id == room_id and gid in _games
               for gid, th in _threads.items())


async def cleanup_room(vc) -> None:
    """對局結束時直接刪除語音配對房（不是本模組開的語音就不動）。"""
    if getattr(vc, "id", None) not in _voice_rooms:
        return
    _voice_rooms.pop(vc.id, None)
    try:
        await vc.delete(reason="Suzume Tsuk 語音房清理（對局結束）")
    except Exception:
        pass


async def _post_settings_panel(vc, lang: str) -> None:
    """在語音房的文字區發設定面板（開局前都能調；開局時鎖定），並記進 _voice_rooms。"""
    try:
        from .views import RoomSettingsView
        sv = RoomSettingsView(gid=f"vc{vc.id}", lang=lang, timeout=None,
                              show_confirm=False)   # 語音房不用確認鈕，開局時面板自動消失
        sv.add_item(CpuStartButton(vc.id, lang))
        sv.add_item(VoicePackSelect(lang))   # 每個人在面板上選自己的角色語音
        msg = await vc.send(i18n.t("voice.settings_hint", lang), view=sv)
        _voice_rooms[vc.id]["settings"] = sv
        _voice_rooms[vc.id]["settings_msg"] = msg
    except Exception as e:
        print(f"[voice] 設定面板發送失敗：{e!r}")


def _is_room_name(name: str) -> bool:
    """頻道名稱像不像本機器人開的配對語音房（任一語言的「房間名稱＋空格」開頭）。"""
    name = name or ""
    for lg in i18n.available():
        prefix = i18n.t("voice.room_name", lg)
        if prefix and name.startswith(prefix + " "):
            return True
    return False


def _interrupted_voice_ids() -> set[int]:
    """中斷／進行中對局記在 room_config["surfaces"]["voice"] 的語音房 ID。
    這些房歸「繼續／結束」流程處理，這裡絕不能動。"""
    import json
    out: set[int] = set()
    try:
        games = db.get_unfinished_games()
    except Exception as e:
        print(f"[voice] 讀取未完成對局失敗：{e!r}")
        raise                                # 查不到就不能確定哪些房是中斷局的 → 交給呼叫端放棄清理
    for g in games:
        cfg = g.get("room_config")
        if isinstance(cfg, str):
            try:
                cfg = json.loads(cfg)
            except Exception:
                cfg = None
        vid = ((cfg or {}).get("surfaces") or {}).get("voice") if isinstance(cfg, dict) else None
        if vid:
            try:
                out.add(int(vid))
            except (TypeError, ValueError):
                pass
    return out


def _is_orphan_room(vc, hub_list: list[dict], busy: set[int]) -> dict | None:
    """vc 是不是「殘留的配對語音房」：在某大廳的類別下、名稱符合、不是配對語音本身、
    沒被追蹤、不屬於中斷局、也沒綁著進行中的對局。是 → 回傳所屬大廳，否則 None。"""
    if vc.id in _voice_rooms or vc.id in busy or not _is_room_name(vc.name):
        return None
    cat_id = str(getattr(vc, "category_id", "") or "")
    for h in hub_list:
        if str(h.get("hub_voice_id") or "") == str(vc.id):
            return None
        if cat_id and str(h.get("category_id") or "") == cat_id:
            if _room_in_game(vc.id):
                return None
            return h
    return None


async def recover_rooms(bot) -> None:
    """重啟後清理／接管殘留的配對語音房（_voice_rooms 只在記憶體，重啟就丟）：
    沒真人 → 刪除；有真人 → 重新配房號、掛回追蹤並補發設定面板。每個行程只跑一次（由 on_ready 呼叫）。"""
    deleted = adopted = 0
    try:
        busy = _interrupted_voice_ids()
    except Exception:
        return
    for guild in list(bot.guilds):
        try:
            hub_list = hubs.all_hubs(guild.id)
            if not hub_list:
                continue
            for vc in list(guild.voice_channels):
                try:
                    hub = _is_orphan_room(vc, hub_list, busy)
                    if hub is None:
                        continue
                    humans = [m for m in vc.members if not m.bot]
                    if not humans:
                        await vc.delete(reason="Suzume Tsuk 語音房清理（重啟後殘留）")
                        deleted += 1
                        continue
                    # 有人在裡面 → 接管成等待房
                    rno  = rooms.next_room_no()
                    lang = hubs.lang_for(guild.id, vc)
                    try:
                        await vc.edit(name=f"{i18n.t('voice.room_name', lang)} {rooms.code(rno)}",
                                      reason="Suzume Tsuk 接管殘留語音房")
                    except Exception:
                        pass                 # 改名失敗（限流／權限）不影響接管
                    _voice_rooms[vc.id] = {"starting": False, "room_no": rno, "hub_id": hub["id"]}
                    await _post_settings_panel(vc, lang)
                    adopted += 1
                    # 已有 4 位以上沒在對局的真人 → 直接開局（與 handle_voice_update 步驟 2 相同）；
                    # 不到 4 人就等下一次語音事件（有人進出）或面板按鈕
                    free = [m for m in humans if str(m.id) not in _user_game]
                    if len(free) >= 4:
                        _voice_rooms[vc.id]["starting"] = True
                        await _start_voice_game(vc, free[:4], sanma=False)
                except Exception as e:
                    print(f"[voice] 重啟清理 {getattr(vc, 'id', '?')} 失敗：{e!r}")
        except Exception as e:
            print(f"[voice] 重啟清理伺服器 {getattr(guild, 'id', '?')} 失敗：{e!r}")
    print(f"[voice] 重啟清理：刪除 {deleted} 間、接管 {adopted} 間")


async def handle_voice_update(member: discord.Member, before, after) -> None:
    """on_voice_state_update 入口（run.py 掛上）。"""
    if member.bot:
        return
    guild = member.guild

    # 1) 加入配對語音（hub）→ 開一間語音房並移過去
    if after.channel is not None:
        hub = next((h for h in hubs.all_hubs(guild.id)
                    if h.get("hub_voice_id") == str(after.channel.id)), None)
        if hub is not None:                      # 進了某個大廳的配對語音
            cat = after.channel.category
            rno  = rooms.next_room_no()          # 先配房號：語音房名稱就用代碼，開局沿用同一個
            lang = hubs.lang_for(guild.id, after.channel)   # 這個大廳的語言
            try:
                vc = await guild.create_voice_channel(
                    f"{i18n.t('voice.room_name', lang)} {rooms.code(rno)}",
                    category=cat, user_limit=5,       # 4 位玩家＋機器人
                    reason="Suzume Tsuk 語音配對房")
            except Exception as e:
                print(f"[voice] 建語音房失敗：{e!r}")
                return
            _voice_rooms[vc.id] = {"starting": False, "room_no": rno, "hub_id": hub["id"]}
            try:
                await member.move_to(vc, reason="Suzume Tsuk 語音配對")
            except Exception:
                # 移不過去（權限不足）→ 房間留著讓玩家自己點進去
                pass
            await _post_settings_panel(vc, lang)   # 設定面板發在語音房的文字區（開局前都能調；開局時鎖定）
            return

    # 2) 加入受管理的語音房 → 滿員就開局；剛好 3 人時提示可先開三麻
    #    （設定面板選了三麻 → 3 人即滿員直接開三麻；4 人一律四麻）
    if after.channel is not None and after.channel.id in _voice_rooms:
        room = after.channel
        info = _voice_rooms[room.id]
        if before.channel is None or before.channel.id != room.id:   # 剛進房：有選語音包就讓機器人先進來
            try:
                from . import sfx
                await sfx.join_room(room, str(member.id))
            except Exception as e:
                print(f"[voice] 機器人進語音房失敗：{e!r}")
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
                await room.edit(user_limit=4, reason="Suzume Tsuk 三麻開局（3 人＋機器人）")
            except Exception:
                pass
            await _start_voice_game(room, free[:3], sanma=True)
        elif len(free) == 3 and not info["starting"] and info.get("sanma_msg") is None:
            lang = hubs.lang_for(member.guild.id, room)
            try:
                v = discord.ui.View(timeout=None)
                v.add_item(SanmaStartButton(room.id, lang))
                info["sanma_msg"] = await room.send(i18n.t("voice.sanma_hint", lang), view=v)
            except Exception as e:
                print(f"[voice] 三麻提示發送失敗：{e!r}")

    # 3) 離開受管理的語音房 → 空了就刪（對局進行中不刪，等對局結束才收）；掉到 3 人以下撤三麻提示
    if before.channel is not None and before.channel.id in _voice_rooms:
        room = before.channel
        info = _voice_rooms.get(room.id, {})
        humans = [m for m in room.members if not m.bot]
        if not humans and (info.get("starting") or _room_in_game(room.id)):
            return                               # 開局中／對局中：大家暫時離開也留著，回來還能接著玩
        if not humans:
            _voice_rooms.pop(room.id, None)
            try:
                await room.delete(reason="Suzume Tsuk 語音房清理（已無人）")
            except Exception:
                pass
        elif len(humans) < 3 and info.get("sanma_msg") is not None:
            await _clear_sanma_prompt(info)

    # 3b) 保險：離開的是「沒被追蹤的殘留房」（重啟後漏網）→ 沒人了就刪
    elif before.channel is not None and _is_room_name(before.channel.name):
        room = before.channel
        if [m for m in room.members if not m.bot]:
            return
        try:
            if _is_orphan_room(room, hubs.all_hubs(guild.id), _interrupted_voice_ids()) is None:
                return
            await room.delete(reason="Suzume Tsuk 語音房清理（殘留且已無人）")
        except Exception as e:
            print(f"[voice] 殘留語音房刪除失敗：{e!r}")
