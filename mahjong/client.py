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
"""Discord 客戶端：建立 bot/tree、事件處理、啟動入口 run()。"""
from __future__ import annotations
import discord
from discord.ext import commands as _dpy

from .config import TOKEN
from . import db
from .state import _input_queues, _input_thread, _thread_game, _threads

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
    # 只有「觀戰牌桌串」禁止打字（board_msg 存在）；真人對局的聊天串可自由聊天
    gid = _thread_game.get(message.channel.id)
    if gid:
        th = _threads.get(gid)
        pub = th.get("public") if th else None
        if pub is not None and message.channel.id == pub.id and th.get("board_msg") is not None:
            try:
                await message.delete()
            except Exception:
                pass




@bot.event
async def on_ready() -> None:
    db.init_db()
    try:
        synced = await tree.sync()
        print(f"✅ 已同步 {len(synced)} 個命令")
    except Exception as e:
        print(f"⚠️ 命令同步錯誤: {e}")
    print(f"✅ Bot 登錄為 {bot.user}")
    print("=" * 50)


def run() -> None:
    """載入指令並啟動機器人。"""
    if not TOKEN:
        print("❌ DISCORD_TOKEN 未設置！請在 .env 設定後再啟動。")
        raise SystemExit(1)
    from . import commands as _commands  # noqa: F401  匯入即註冊 /mahjong 與 /help
    try:                                  # 選用擴充：存在時自動掛上
        from .web import server as _web
        _web.attach(bot)
    except ImportError:
        pass
    except Exception as e:
        print(f"ℹ️ 擴充未啟動（{e}）")
    try:
        bot.run(TOKEN)
    except KeyboardInterrupt:
        print("機器人已停止")
