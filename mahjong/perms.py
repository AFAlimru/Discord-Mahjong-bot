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
"""機器人權限需求（指令前檢查、後台顯示共用）。權限名一律用 Discord 英文原名。"""
from __future__ import annotations
import discord

# 任何指令所在頻道都要有，否則連回覆都做不到
CORE = [
    ("view_channel", "View Channel"),
    ("send_messages", "Send Messages"),
    ("embed_links", "Embed Links"),
]
# 文字頻道開局用：公開對局串＋每人的私人手牌串
THREADS = [
    ("create_public_threads", "Create Public Threads"),
    ("create_private_threads", "Create Private Threads"),
    ("send_messages_in_threads", "Send Messages in Threads"),
    ("manage_threads", "Manage Threads"),
]
# 語音頻道（配對房＋音效）
VOICE = [
    ("view_channel", "View Channel"),
    ("connect", "Connect"),
    ("speak", "Speak"),
]
# 進階功能：缺了只有該功能失效 → (屬性, 名稱, 用途)
FEATURES = [
    ("manage_channels", "Manage Channels", "建大廳、頻道制對局、語音配對房"),
    ("manage_roles", "Manage Roles", "大廳與對局頻道的權限鎖定"),
    ("manage_messages", "Manage Messages", "大廳打字自動刪除"),
    ("move_members", "Move Members", "語音配對自動把人拉進房"),
    ("connect", "Connect", "語音音效：進語音房"),
    ("speak", "Speak", "語音音效：播放"),
    ("use_external_emojis", "Use External Emojis", "麻將牌表情顯示"),
    ("create_instant_invite", "Create Invite", "後台的伺服器邀請連結"),
]


def _miss(perms: discord.Permissions, spec) -> list[str]:
    if getattr(perms, "administrator", False):
        return []
    return [s[1] for s in spec if not getattr(perms, s[0], True)]


def channel_missing(channel, me) -> list[str]:
    """指令執行前的檢查：機器人在該頻道缺哪些必要權限。"""
    try:
        perms = channel.permissions_for(me)
    except Exception:
        return []
    spec = list(CORE)
    if isinstance(channel, discord.TextChannel):   # 討論串內不需要「建立討論串」
        spec += THREADS
    return _miss(perms, spec)


def guild_report(guild, settings: dict | None = None) -> dict:
    """後台用：伺服器層級缺的權限，加上大廳／遊玩頻道／配對語音等關鍵頻道各自的問題。
    settings＝預先讀好的 guild_settings 列（批次列表用，免逐台查資料庫）；None＝自己查。"""
    me = guild.me
    if me is None:
        return {"admin": False, "core": [], "features": [], "channels": [], "ok": True}
    gp = me.guild_permissions
    core = _miss(gp, CORE + THREADS)
    feats = ([] if gp.administrator else
             [{"perm": n, "use": u} for a, n, u in FEATURES if not getattr(gp, a, True)])
    channels = []
    if settings is not None:
        setup, play = settings, settings.get("play_channel_id")
    else:
        from . import db
        try:
            setup = db.get_guild_setup(str(guild.id))
            play  = db.get_play_channel(str(guild.id))
        except Exception:
            setup, play = {}, None
    checks = (("大廳", setup.get("lobby_channel_id"), CORE + [("manage_messages", "Manage Messages")]),
              ("遊玩頻道", play, CORE + THREADS),
              ("配對語音", setup.get("hub_voice_id"), VOICE))
    for label, cid, spec in checks:
        if not cid:
            continue
        try:
            ch = guild.get_channel(int(cid))
        except (TypeError, ValueError):
            ch = None
        if ch is None:
            channels.append({"channel": label, "missing": ["頻道已不存在"]})
            continue
        m = _miss(ch.permissions_for(me), spec)
        if m:
            channels.append({"channel": f"{label}（#{ch.name}）", "missing": m})
    return {"admin": gp.administrator, "core": core, "features": feats, "channels": channels,
            "ok": not core and not channels}
