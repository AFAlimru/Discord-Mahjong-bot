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
import discord
from discord.ext import commands as _dpy

from mahjong.config import TOKEN, DEV_GUILD_ID
from mahjong import db
from mahjong.state import _input_queues, _input_thread, _thread_game, _threads

intents = discord.Intents.default()
intents.message_content = True
bot = _dpy.Bot(command_prefix="!", intents=intents)
tree = bot.tree


@bot.event
async def on_message(message: discord.Message) -> None:
    """讀取玩家在自己私人討論串打的字；公開牌桌討論串則禁止打字。"""
    if message.author.bot:
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
    try:                                      # 大廳常駐面板（persistent view，重啟後按鈕仍有效）
        if not getattr(bot, "_hub_view_added", False):
            from mahjong.commands import LobbyPanel
            bot.add_view(LobbyPanel())
            bot._hub_view_added = True
    except Exception as e:
        print(f"⚠️ 大廳面板掛載失敗: {e}")


def run() -> None:
    """掛上指令並啟動機器人。"""
    if not TOKEN:
        print("❌ DISCORD_TOKEN 未設置！請在 .env 設定後再啟動。")
        raise SystemExit(1)
    from mahjong import commands as _commands
    _commands.register(tree)              # 把 /mahjong、/help、/language 掛到 tree
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
