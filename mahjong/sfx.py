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
"""0.7 全域音效（Soundboard）：音效包只上傳到「家」伺服器（SOUND_GUILD_ID），
對局綁了語音房時由機器人進房代發——任何伺服器都聽得到，不用各伺服器自己加。

- 對局音效（riichi/ron/tsumo…）＝ scope "global"：所有伺服器可播。
- 劇情語音（story_*）＝ scope "home"：只在家伺服器播。
- 前置：discord.py ≥ 2.5（send_sound / soundboard API）＋ PyNaCl（進語音）。
  版本不足或未設 SOUND_GUILD_ID 時整個模組靜默停用，文字對局完全不受影響。
"""
from __future__ import annotations
import os
import discord

from .config import SOUND_GUILD_ID, SOUNDS_DIR
from .state import _threads

# 音效登記表：名稱＝家伺服器音效板上的名稱（＝assets/sounds/ 檔名去副檔名）
# scope: "global"＝任何伺服器；"home"＝只在家伺服器（劇情語音一律 story_ 前綴，自動視為 home）
SOUNDS: dict[str, str] = {
    "riichi":    "global",   # 立直！
    "ippatsu":   "global",   # 一發！
    "ron":       "global",   # 榮和！
    "tsumo":     "global",   # 自摸！
    "kan":       "global",   # 槓！
    "ryuukyoku": "global",   # 流局～
    "start":     "global",   # 開局
}

_cache: dict[str, object] = {}   # 名稱 -> SoundboardSound
_ready = False


def _scope(name: str) -> str | None:
    if name in SOUNDS:
        return SOUNDS[name]
    if name.startswith("story_"):
        return "home"
    return None


def available() -> bool:
    """soundboard API 可用（discord.py ≥ 2.5）且設定了家伺服器。"""
    return bool(SOUND_GUILD_ID) and hasattr(discord.Guild, "fetch_soundboard_sounds")


async def load(bot) -> None:
    """啟動時載入家伺服器的音效板到快取（on_ready 呼叫）。"""
    global _ready
    if not SOUND_GUILD_ID:
        return
    if not available():
        print("[sfx] discord.py < 2.5，音效板功能停用（pip install -U discord.py 後重啟）")
        return
    g = bot.get_guild(int(SOUND_GUILD_ID))
    if g is None:
        print(f"[sfx] 找不到家伺服器 {SOUND_GUILD_ID}（機器人不在裡面？）")
        return
    try:
        _cache.clear()
        for s in await g.fetch_soundboard_sounds():
            _cache[s.name] = s
        _ready = bool(_cache)
        print(f"[sfx] 已載入 {len(_cache)} 個音效：{'、'.join(sorted(_cache))}")
    except Exception as e:
        print(f"[sfx] 載入音效失敗：{e!r}")


async def _ensure_voice(vc) -> bool:
    """讓機器人待在該語音頻道（觸發音效的 API 前置）。"""
    try:
        cur = vc.guild.voice_client
        if cur and cur.channel and cur.channel.id == vc.id:
            return True
        if cur:
            await cur.move_to(vc)
        else:
            await vc.connect(self_deaf=True)
        return True
    except Exception as e:
        print(f"[sfx] 進語音失敗：{e!r}　← 需要 PyNaCl（pip install PyNaCl）與連線權限")
        return False


async def leave(guild) -> None:
    """對局結束離開語音。"""
    try:
        if guild.voice_client:
            await guild.voice_client.disconnect(force=True)
    except Exception:
        pass


async def play(gid: str, name: str) -> None:
    """對該對局綁定的語音房播音效；沒綁語音、功能未就緒、找不到音效＝靜默略過。"""
    if not _ready:
        return
    vc = (_threads.get(gid) or {}).get("voice")
    if vc is None:
        return
    sc = _scope(name)
    if sc is None:
        return
    if sc == "home" and str(vc.guild.id) != SOUND_GUILD_ID:
        return
    snd = _cache.get(name)
    if snd is None or not hasattr(vc, "send_sound"):
        return
    try:
        if await _ensure_voice(vc):
            await vc.send_sound(snd)
    except Exception as e:
        print(f"[sfx] 播放 {name} 失敗：{e!r}")


async def upload_pack(guild) -> tuple[int, list[str]]:
    """把 SOUNDS_DIR 裡的音效檔批次上傳到該伺服器音效板（家伺服器用；擁有者指令呼叫）。
    檔名（去副檔名）＝音效名稱；已存在同名者跳過。回傳 (成功數, 錯誤訊息列表)。"""
    if not hasattr(guild, "create_soundboard_sound"):
        return 0, ["discord.py < 2.5，不支援音效板上傳"]
    try:
        existing = {s.name for s in await guild.fetch_soundboard_sounds()}
    except Exception as e:
        return 0, [f"讀取現有音效失敗：{e!r}"]
    ok, errs = 0, []
    if not os.path.isdir(SOUNDS_DIR):
        return 0, [f"找不到音效資料夾 {SOUNDS_DIR}"]
    for fn in sorted(os.listdir(SOUNDS_DIR)):
        stem, ext = os.path.splitext(fn)
        if ext.lower() not in (".mp3", ".ogg", ".wav"):
            continue
        if stem in existing:
            continue
        path = os.path.join(SOUNDS_DIR, fn)
        if os.path.getsize(path) > 512 * 1024:
            errs.append(f"{fn}：超過 512KB，跳過")
            continue
        try:
            with open(path, "rb") as f:
                await guild.create_soundboard_sound(
                    name=stem, sound=f.read(), reason="Suzume Tsuk 音效包")
            ok += 1
        except Exception as e:
            errs.append(f"{fn}：{e!r}")
    return ok, errs
