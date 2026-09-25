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
"""0.7.2 音效（改為直接播檔 / FFmpeg，取代舊的 Soundboard 上限做法）：

音效檔放在 `SOUNDS_DIR/<語音包>/<名稱>.<副檔名>`；對局綁了語音房時，機器人進房用 FFmpeg
直接把檔案播進語音頻道。任何格式（mp3/ogg/wav/…）皆可，無 Soundboard 的數量上限。

- **語音包＝資料夾**：`SOUNDS_DIR` 底下每個子資料夾就是一個語音包，資料夾名＝包名。
  每台伺服器可用 `/setup voice`／面板選一個（存 DB）；**未選＝不出聲**（要有聲才選包），
  選了語音包後、包內缺某檔時才退回根目錄的同名檔當後備。
- **名稱**：事件音（start/riichi/ippatsu/pon/chi/kan/tsumo/ron/ryuukyoku）＋和了役名（＝役名本身）
  ＋打點階級（han/mangan/…）。缺檔＝該項靜默略過。
- **播放**：每台伺服器一條佇列、依序播；`play()` 只排入即返回（不擋遊戲流程），背景消費者逐一播。
- **前置**：FFmpeg（需在 PATH）＋ PyNaCl（進語音）。缺 FFmpeg 時整個模組靜默停用，文字對局不受影響。
"""
from __future__ import annotations
import asyncio
import os
import shutil
import discord

from .config import SOUNDS_DIR
from .state import _threads
from . import db

# FFmpeg 可解的常見音訊副檔名（解析檔案時依序嘗試）
AUDIO_EXTS = (".mp3", ".ogg", ".oga", ".opus", ".wav", ".m4a", ".aac", ".flac", ".webm")

# 事件音效名（動作當下播）
EVENT_SOUNDS = ("start", "discard", "riichi", "ippatsu", "pon", "chi", "kan",
                "tsumo", "ron", "ryuukyoku")

# 打點階級音效名（由 tier_sound 依 ScoreResult.name 對應）
TIER_SOUNDS = ("han", "mangan", "haneman", "baiman", "sanbaiman", "yakuman", "kazoe")

# 和了役名語音：音效名＝役名本身（結果畫面逐一揭曉的中文名）；play_win 逐一唸出，有檔才響。
# ⚠️ 此表需與 scoring.py 實際產生的役名一致——新增／改名役種時記得同步補這裡。
YAKU_SOUNDS: frozenset = frozenset({
    # 一般役（門清／副露）
    "立直", "兩立直", "開立直", "一發", "門前清自摸和", "平和", "斷么九", "七對子",
    "一盃口", "二盃口", "三色同順", "三色同刻", "一氣通貫", "對對和", "三暗刻", "三槓子",
    "小三元", "混全帶么九", "純全帶么九", "混老頭", "混一色", "清一色",
    "海底摸月", "河底撈魚", "嶺上開花", "槍槓",
    # 役牌（依風牌動態產生）
    "役牌白", "役牌發", "役牌中", "場風", "自風",
    # 懸賞（寶牌類，也會逐一唸出）
    "寶牌", "裏寶牌", "赤寶牌", "拔北",
    # 役滿
    "天和", "地和", "國士無雙", "國士無雙十三面", "四暗刻", "四暗刻單騎",
    "大三元", "大四喜", "小四喜", "字一色", "綠一色", "清老頭",
    "九蓮寶燈", "純正九蓮寶燈", "四槓子",
    # 途中和了
    "流局滿貫",
})

# 一個「完整語音包」預期包含的所有音效名（供 /setup voice 顯示齊全度）
ALL_SOUND_NAMES: tuple = EVENT_SOUNDS + TIER_SOUNDS + tuple(sorted(YAKU_SOUNDS))

# 和了語音序列的時序（秒），可調。
WIN_INTRO_GAP = 1.5   # 和了 → 開始唸役名前的等待（對齊儀式標題／手牌揭曉）
SEQ_GAP       = 0.15  # 佇列中前後音效之間的基本空隙（實際長度由音檔決定）
CONSUMER_IDLE = 30.0  # 佇列閒置多久後消費者收工（下次播放再自動重啟）

DEBUG = False                                    # 診斷輸出（需要時改 True）
_ready = False                                   # FFmpeg 就緒
_queues: dict[int, asyncio.Queue] = {}           # guild_id -> 待播 (vc, path) 佇列
_consumers: dict[int, asyncio.Task] = {}         # guild_id -> 消費者 task


# ───────────────────────── 就緒檢查 / 語音包 ─────────────────────────
def available() -> bool:
    """FFmpeg 是否可用（direct-play 前置）。"""
    return shutil.which("ffmpeg") is not None


def _ensure_opus() -> bool:
    """確保 libopus 已載入（discord.py 送語音要用它把 PCM 編成 Opus）。已載入＝直接回 True。"""
    if discord.opus.is_loaded():
        return True
    try:                                    # Windows：載入 discord.py 內建的 libopus
        discord.opus._load_default()
    except Exception:
        pass
    if not discord.opus.is_loaded():        # 其他平台：試常見的系統函式庫名
        for lib in ("libopus.so.0", "libopus.so", "libopus.0.dylib", "opus"):
            try:
                discord.opus.load_opus(lib)
                break
            except Exception:
                continue
    return discord.opus.is_loaded()


async def load(bot=None) -> None:
    """啟動檢查（on_ready 呼叫）：確認 FFmpeg 與 libopus 可用並列出語音包。"""
    global _ready
    if not available():
        print("[sfx] 找不到 FFmpeg，音效停用（安裝後重啟；Linux: apt install ffmpeg / Win: 放進 PATH）")
        _ready = False
        return
    if not _ensure_opus():
        print("[sfx] libopus 未載入，語音送音會失敗（Linux: apt install libopus0；Win 通常內建）")
    _ready = True
    packs = list_packs()
    print(f"[sfx] 音效就緒（FFmpeg 直接播檔；opus={'OK' if discord.opus.is_loaded() else '未載入'}）。"
          f"語音包：{'、'.join(packs) or '（無子資料夾，僅用根目錄預設）'}")


def list_packs() -> list[str]:
    """`SOUNDS_DIR` 底下的子資料夾＝語音包名稱（排序）。"""
    try:
        return sorted(d for d in os.listdir(SOUNDS_DIR)
                      if os.path.isdir(os.path.join(SOUNDS_DIR, d)))
    except Exception:
        return []


VOICE_OFF = "__off__"   # db.voice_pack 存這個＝該伺服器「關閉語音」（完全不出聲）


def _guild_pack(guild_id: str) -> str | None:
    """該伺服器選定的語音包：回傳包名／`VOICE_OFF`（關閉）／None（未選＝不出聲）。"""
    try:
        pack = db.get_voice_pack(guild_id)
    except Exception:
        return None
    if pack == VOICE_OFF:
        return VOICE_OFF
    if pack and os.path.isdir(os.path.join(SOUNDS_DIR, pack)):
        return pack
    return None


def _guild_muted(guild_id: str) -> bool:
    """該伺服器是否不出聲：沒選語音包（預設）或選了「關閉語音」都算——有選有效語音包才會播。"""
    p = _guild_pack(guild_id)
    return (not p) or p == VOICE_OFF


def _resolve(pack: str | None, name: str) -> str | None:
    """找出音效檔路徑：先找 `<pack>/<name>.<ext>`，再退回根目錄 `<name>.<ext>`（預設／後備）。
    找不到＝None。"""
    dirs = []
    if pack:
        dirs.append(os.path.join(SOUNDS_DIR, pack))
    dirs.append(SOUNDS_DIR)
    for d in dirs:
        for ext in AUDIO_EXTS:
            p = os.path.join(d, name + ext)
            if os.path.isfile(p):
                return p
    return None


def pack_coverage(pack: str | None) -> tuple[int, list[str]]:
    """某語音包（含根目錄後備）備齊了幾個預期音效、缺哪些。回傳 (齊全數, 缺少名稱列表)。"""
    missing = [n for n in ALL_SOUND_NAMES if _resolve(pack, n) is None]
    return len(ALL_SOUND_NAMES) - len(missing), missing


# ───────────────────────── 語音連線 ─────────────────────────
async def _ensure_client(vc) -> discord.VoiceClient | None:
    """確保機器人在該語音頻道，回傳 VoiceClient；失敗回 None。
    不可自聾／自靜音（Discord error 50167）。"""
    try:
        cur = vc.guild.voice_client
        if cur and cur.channel and cur.channel.id == vc.id:
            return cur
        if cur:
            await cur.move_to(vc)
            return vc.guild.voice_client
        return await vc.connect(self_deaf=False, self_mute=False)
    except Exception as e:
        print(f"[sfx] 進語音失敗：{e!r}　← 需要 PyNaCl（pip install PyNaCl）與連線權限")
        return None


async def join(vc) -> bool:
    """對局開始時先讓機器人進語音房（不必等第一個音效）。未就緒／已關閉語音＝略過。"""
    if not _ready:
        return False
    if _guild_muted(str(vc.guild.id)):
        return False
    return (await _ensure_client(vc)) is not None


async def leave(guild) -> None:
    """對局結束：停止佇列、離開語音。"""
    gid = getattr(guild, "id", None)
    if gid is not None:
        t = _consumers.pop(gid, None)
        if t is not None:
            t.cancel()
        _queues.pop(gid, None)
    try:
        if guild.voice_client:
            guild.voice_client.stop()
            await guild.voice_client.disconnect(force=True)
    except Exception:
        pass


# ───────────────────────── 播放佇列 ─────────────────────────
def _enqueue(vc, path: str) -> None:
    gid = vc.guild.id
    q = _queues.get(gid)
    if q is None:
        q = _queues[gid] = asyncio.Queue()
    q.put_nowait((vc, path))
    t = _consumers.get(gid)
    if t is None or t.done():
        _consumers[gid] = asyncio.create_task(_consume(gid))


async def _consume(gid: int) -> None:
    """某伺服器的播放消費者：依序播佇列裡的檔，閒置一段時間後收工。"""
    q = _queues.get(gid)
    if q is None:
        return
    try:
        while True:
            try:
                vc, path = await asyncio.wait_for(q.get(), timeout=CONSUMER_IDLE)
            except asyncio.TimeoutError:
                return
            await _play_path(vc, path)
            await asyncio.sleep(SEQ_GAP)
    except asyncio.CancelledError:
        pass
    finally:
        if _consumers.get(gid) is asyncio.current_task():
            _consumers.pop(gid, None)


async def _play_path(vc, path: str) -> None:
    """把單一音檔播進語音頻道並等它播完（FFmpeg → PCM → Opus）。"""
    client = await _ensure_client(vc)
    if client is None:
        print(f"[sfx] 播放略過：無法連上語音（PyNaCl／連線權限？）")
        return
    try:
        if client.is_playing():
            client.stop()
        source = discord.FFmpegPCMAudio(path)
        done = asyncio.Event()
        loop = asyncio.get_running_loop()

        def _after(err):
            if err:
                print(f"[sfx] 播放錯誤：{err!r}")
            loop.call_soon_threadsafe(done.set)

        if DEBUG:
            print(f"[sfx] ▶ 播放 {os.path.basename(path)}（頻道 {getattr(client.channel,'name','?')}）")
        client.play(source, after=_after)
        await done.wait()
        if DEBUG:
            print(f"[sfx] ✔ 播完 {os.path.basename(path)}")
    except Exception as e:
        print(f"[sfx] 播放 {os.path.basename(path)} 失敗：{e!r}")


async def play(gid: str, name: str) -> None:
    """把音效排入該對局語音房的播放佇列（不擋呼叫端）。
    沒綁語音、功能未就緒、找不到檔＝靜默略過。"""
    if not _ready:
        return
    vc = (_threads.get(gid) or {}).get("voice")
    if vc is None:
        if DEBUG:
            print(f"[sfx] play({name}) 略過：本局未綁語音房")
        return
    pack = _guild_pack(str(vc.guild.id))
    if not pack or pack == VOICE_OFF:        # 沒選語音包／關閉語音＝不出聲
        if DEBUG:
            print(f"[sfx] play({name}) 略過：未選語音包或已關閉")
        return
    path = _resolve(pack, name)
    if path is None:
        if DEBUG:
            print(f"[sfx] play({name}) 略過：找不到音效檔")
        return
    if DEBUG:
        print(f"[sfx] play({name}) → 排入 {os.path.basename(path)}")
    _enqueue(vc, path)


# ───────────────────────── 和了語音 ─────────────────────────
def tier_sound(name: str) -> str:
    """把計分等級名（`ScoreResult.name`）對應到音效名。
    空字串／一般手＝翻；含「役滿」＝役滿（累計役滿另計）；否則依滿貫階梯。"""
    if not name:
        return "han"
    if "役滿" in name:                 # 役滿／N倍役滿／累計役滿
        return "kazoe" if name == "累計役滿" else "yakuman"
    if "三倍滿" in name:               # 需先於「倍滿」判（三倍滿含「倍滿」子字串）
        return "sanbaiman"
    if "倍滿" in name:
        return "baiman"
    if "跳滿" in name:
        return "haneman"
    if "滿貫" in name:                 # 含流局滿貫
        return "mangan"
    return "han"


async def play_win(gid: str, result) -> None:
    """和牌語音：對齊和牌儀式，逐一「唸出每個役名」（音效名＝役名，如「立直」「平和」「寶牌」），
    最後公布打點階級（翻／滿貫／…）。役名沒錄音效檔＝該役靜默略過。
    設計為背景任務呼叫（含 sleep，不擋畫面儀式）；實際播放由佇列依序進行。"""
    if not _ready or result is None:
        return
    vc = (_threads.get(gid) or {}).get("voice")
    if vc is None:                                        # 沒綁語音就別空跑
        return
    if _guild_muted(str(vc.guild.id)):                    # 未選語音包／關閉語音
        return
    # 揭曉順序同 win_ceremony：役滿則只列役滿，否則列一般役（含寶牌等懸賞，有錄檔才會出聲）
    if getattr(result, "yakuman", None):
        names = [n for n, *_ in result.yakuman]
    else:
        names = [n for n, *_ in (result.yaku or [])]
    await asyncio.sleep(WIN_INTRO_GAP)        # 等標題＋手牌揭曉後才開始唸役
    for n in names:
        await play(gid, n)                    # 逐一唸役名（排入佇列，依序播）
    tier = tier_sound(getattr(result, "name", "") or "")
    if tier:
        await play(gid, tier)                 # 打點階級
