#!/usr/bin/env python3
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
"""啟動入口：建立 bot/tree、事件處理，並以 `python run.py` 啟動。"""
from __future__ import annotations
import logging
import discord
from discord.ext import commands as _dpy

from mahjong.config import TOKEN, DEV_GUILD_ID
from mahjong import db
from mahjong.state import (_input_queues, _input_thread, _thread_game, _threads,
                           _lobby_channels)


class _QuietBadRequests(logging.Filter):
    """丟掉網站被畸形／掃描請求（非 HTTP 位元組）打到時 aiohttp 印的 400 traceback。
    這類請求本就被拒絕、無害，只是洗版 log；真正的伺服器錯誤照常印出。"""
    def filter(self, record: logging.LogRecord) -> bool:
        exc = record.exc_info
        if exc and exc[0] is not None and getattr(exc[0], "__module__", "") == "aiohttp.http_exceptions":
            return False
        return True


logging.getLogger("aiohttp.server").addFilter(_QuietBadRequests())

intents = discord.Intents.default()
intents.message_content = True
bot = _dpy.Bot(command_prefix="!", intents=intents)
tree = bot.tree


async def _global_perm_check(interaction: discord.Interaction) -> bool:
    """任何指令執行前檢查機器人在該頻道的權限；缺權限就明確告知缺哪些並中止。"""
    g = interaction.guild
    if g is None or g.me is None:
        return True
    from mahjong import perms as _perms
    missing = _perms.channel_missing(interaction.channel, g.me)
    if not missing:
        return True
    from mahjong import i18n as _i
    lang = _i.get_user_lang(interaction.user.id, g.id)
    txt  = _i.t("perm.bot_missing", lang, perms="、".join(missing))
    try:
        if interaction.response.is_done():
            await interaction.followup.send(txt, ephemeral=True)
        else:
            await interaction.response.send_message(txt, ephemeral=True)
    except Exception:
        pass
    return False


tree.interaction_check = _global_perm_check


@bot.event
async def on_message(message: discord.Message) -> None:
    """讀取玩家在自己私人討論串打的字；公開牌桌討論串則禁止打字。
    0.7：大廳頻道（面板）任何人打字都直接刪（含有權限繞過唯讀的管理員）。"""
    if message.author.bot:
        return
    if message.guild is not None:
        gid_s = str(message.guild.id)
        lid = _lobby_channels.get(gid_s)
        if lid is None:                       # 尚未查過 → 查一次 DB 進快取
            lid = db.get_guild_setup(gid_s).get("lobby_channel_id") or "?"
            _lobby_channels[gid_s] = lid
        if lid == str(message.channel.id):
            try:
                await message.delete()
            except Exception:
                pass
            return
    uid = str(message.author.id)
    q = _input_queues.get(uid)
    if q is not None and message.channel.id == _input_thread.get(uid):
        await q.put((message.content.strip(), message))
        return
    gid = _thread_game.get(message.channel.id)
    if not gid:
        return
    th = _threads.get(gid)
    if not th:
        return
    # 觀戰公開牌桌串：禁止打字
    pub = th.get("public")
    if pub is not None and message.channel.id == pub.id and th.get("board_msg") is not None:
        try:
            await message.delete()
        except Exception:
            pass
        return
    # 私人手牌串：非自己回合打字也直接刪掉（不給任何提示）
    if message.channel.id in {t.id for t in th.get("private", {}).values()}:
        try:
            await message.delete()
        except Exception:
            pass


@bot.event
async def on_ready() -> None:
    db.init_db()
    try:                                      # 指令描述在地化（依使用者 Discord 介面語言）
        from mahjong.commands import CommandTranslator
        if tree.translator is None:
            await tree.set_translator(CommandTranslator())
    except Exception as e:
        print(f"⚠️ 指令翻譯器掛載失敗: {e}")
    try:
        if DEV_GUILD_ID:
            guild = discord.Object(id=int(DEV_GUILD_ID))
            tree.copy_global_to(guild=guild)
            synced = await tree.sync(guild=guild)
            print(f"✅ 已同步 {len(synced)} 個命令到測試伺服器 {DEV_GUILD_ID}（即時生效）")
        else:
            synced = await tree.sync()
            print(f"✅ 已同步 {len(synced)} 個全域命令")
    except Exception as e:
        print(f"⚠️ 命令同步錯誤: {e}")
    print(f"✅ Bot 登錄為 {bot.user}")
    print("=" * 50)
    try:                                      # 重啟後回復中斷的對局（讓玩家選繼續／結束）
        from mahjong.flow import recover_interrupted_games
        await recover_interrupted_games(bot)
    except Exception as e:
        print(f"⚠️ 對局回復檢查失敗: {e}")
    try:                                      # 段位賽排隊逾時清掃器
        from mahjong import matchmaking
        matchmaking.start_sweeper()
    except Exception as e:
        print(f"⚠️ 排隊清掃器啟動失敗: {e}")
    try:                                      # 等待房逾時清掃器（開了沒人加入的殘留房）
        from mahjong import flow as _flow
        _flow.start_room_sweeper()
    except Exception as e:
        print(f"⚠️ 房間清掃器啟動失敗: {e}")
    try:                                      # 音效：檢查 FFmpeg/opus、列出語音包
        from mahjong import sfx
        await sfx.load(bot)
    except Exception as e:
        print(f"⚠️ 音效載入失敗: {e}")
    try:                                      # 大廳常駐面板＋指南（persistent view，重啟後按鈕仍有效）
        if not getattr(bot, "_hub_view_added", False):
            from mahjong.commands import LobbyPanel, GuideView
            bot.add_view(LobbyPanel())
            bot.add_view(GuideView())
            bot._hub_view_added = True
    except Exception as e:
        print(f"⚠️ 大廳面板掛載失敗: {e}")
    try:                                      # 啟動檢測：設定各伺服器主要語言＋補建缺少的指南頻道
        for g in bot.guilds:                  #（已有大廳類別的就跳過指南）
            await ensure_guild_setup(g)
    except Exception as e:
        print(f"⚠️ 伺服器初始化檢測失敗: {e}")
    try:                                      # 上次沒收掉的回放頻道（重啟後按鈕已失效）
        from mahjong.commands import cleanup_replay_channels
        for g in bot.guilds:
            await cleanup_replay_channels(g)
    except Exception as e:
        print(f"⚠️ 回放頻道清理失敗: {e}")


@bot.event
async def on_voice_state_update(member, before, after) -> None:
    """0.7 語音配對：進「配對語音」自動開房、滿人開局、空房自刪。"""
    try:
        from mahjong.voice import handle_voice_update
        await handle_voice_update(member, before, after)
    except Exception as e:
        print(f"⚠️ 語音配對處理失敗: {e!r}")


async def ensure_guild_setup(guild, create_guide: bool = True) -> None:
    """伺服器初始化：依 Discord 地區設定主要語言（未設定時）＋（需要時）建立管理員指南頻道。
    已建大廳類別、或已有指南頻道的伺服器就不再建指南。"""
    from mahjong import i18n as _i, db as _db
    from mahjong.commands import GuideView
    gid = str(guild.id)
    if _i.guild_lang(gid) is None:            # 主要語言：未設定→依伺服器地區自動偵測
        _i.set_guild_lang(gid, _i.detect_locale(getattr(guild, "preferred_locale", "")))
    lang = _i.guild_lang(gid) or _i.DEFAULT
    if not create_guide:
        return
    try:                                      # 已建大廳類別＝已設定好，不需指南
        if _db.get_guild_setup(gid).get("category_id"):
            return
    except Exception:
        pass
    names  = {_i.t("guide.channel_name", L) for L in _i.available()}
    guides = [c for c in getattr(guild, "text_channels", []) if c.name in names]
    me     = guild.me
    text   = _i.t("guide.text", lang)

    def _usable(c) -> bool:
        try:
            p = c.permissions_for(me)
            return p.view_channel and p.send_messages
        except Exception:
            return False

    for c in guides:                          # 已有機器人用得了的指南頻道
        if _usable(c):
            if c.last_message_id is None:     # 之前發送失敗留下的空頻道 → 補發
                try:
                    await c.send(text, view=GuideView(lang))
                    print(f"[setup] 已補發「{getattr(guild, 'name', '?')}」的設定指南")
                except Exception as e:
                    print(f"⚠️ 補發指南失敗（{getattr(guild, 'name', '?')}）: {e}")
            return
    for c in guides:                          # 只剩機器人看不到的舊指南頻道（舊版建錯的）→ 試著刪掉
        try:
            await c.delete(reason="Suzume Tsuk 重建設定指南")
        except Exception:
            pass
    ch = None
    try:
        # 對 @everyone 隱藏，但一定要給機器人自己看得到、發得了言（否則沒管理員權限時會變空頻道）
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            me: discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                            embed_links=True, read_message_history=True),
        }
        ch = await guild.create_text_channel(_i.t("guide.channel_name", lang),
                                             overwrites=overwrites,
                                             reason="Suzume Tsuk 設定指南（僅管理員可見）")
        await ch.send(text, view=GuideView(lang))
        print(f"[setup] 已為「{getattr(guild, 'name', '?')}」建立設定指南頻道")
    except Exception as e:
        print(f"⚠️ 指南頻道建立失敗（{getattr(guild, 'name', '?')}）: {e}")
        if ch is not None:                    # 建了卻發不出去 → 刪掉空頻道，下次啟動再試
            try:
                await ch.delete(reason="Suzume Tsuk 指南發送失敗")
            except Exception:
                pass


@bot.event
async def on_guild_join(guild) -> None:
    """加入新伺服器：設定主要語言＋建立「僅管理員可見」的設定指南頻道。"""
    await ensure_guild_setup(guild)


def run() -> None:
    """掛上指令並啟動機器人。"""
    if not TOKEN:
        print("❌ DISCORD_TOKEN 未設置！請在 .env 設定後再啟動。")
        raise SystemExit(1)
    from mahjong import commands as _commands
    _commands.register(tree)              # 把 /mahjong、/help、/language 掛到 tree
    from mahjong import flow as _flow
    _flow.BOT = bot                       # 0.7：DM 面板／語音配對需要 bot 參考
    try:                                  # 選用擴充：存在時自動掛上
        from mahjong.web import server as _web
        _web.attach(bot)
    except ImportError:
        pass
    except Exception as e:
        print(f"ℹ️ 擴充未啟動（{e}）")
    try:
        bot.run(TOKEN)
    except KeyboardInterrupt:
        print("機器人已停止")


if __name__ == "__main__":
    run()
