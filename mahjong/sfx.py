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
  **個人設定**：每位玩家在大廳「🎚️ 語音」自選（存 user_prefs）；對局中誰的動作就用誰的包，
  **沒選＝他的動作不出聲**。開局與 AI 的動作用房主（或其他有選的真人）的包。
  包內缺某檔時才退回根目錄的同名檔當後備。
- **名稱**：事件音（start/discard/riichi/double_riichi/open_riichi/pon/chi/kan/tsumo/ron/ryuukyoku；
  兩立直、開立直沒錄專屬檔時退回 riichi）＋和了役名（＝役名本身）
  ＋途中流局名（九種九牌/四風連打/…，缺檔退回 ryuukyoku）＋打點階級（mangan/haneman/…，滿貫以上才唸）。
  缺檔＝該項靜默略過。
- **播放**：每台伺服器一條佇列、依序播；`play()` 只排入即返回（不擋遊戲流程），背景消費者逐一播。
- **前置**：FFmpeg（需在 PATH）＋ PyNaCl（進語音）。缺 FFmpeg 時整個模組靜默停用，文字對局不受影響。
"""
from __future__ import annotations
import asyncio
import os
import random
import re
import shlex
import shutil
import discord

from .config import SOUNDS_DIR
from .state import _threads
from . import db

# FFmpeg 可解的常見音訊副檔名（解析檔案時依序嘗試）
AUDIO_EXTS = (".mp3", ".ogg", ".oga", ".opus", ".wav", ".m4a", ".aac", ".flac", ".webm")

# 事件音效名（動作當下播）
EVENT_SOUNDS = ("start", "discard", "riichi", "double_riichi", "open_riichi", "pon", "chi", "kan",
                "tsumo", "ron", "ryuukyoku")

# 途中流局語音（音效名＝流局名本身）；包裡沒有專屬檔時退回 ryuukyoku
ABORT_SOUNDS = ("九種九牌", "四風連打", "四槓散了", "四家立直", "三家和")

# 打點階級音效名（由 tier_sound 依 ScoreResult.name 對應；滿貫以下不唸）
TIER_SOUNDS = ("mangan", "haneman", "baiman", "sanbaiman", "yakuman", "kazoe")

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

# 一個「完整語音包」預期包含的所有音效名（供語音包選單顯示齊全度）
ALL_SOUND_NAMES: tuple = EVENT_SOUNDS + ABORT_SOUNDS + TIER_SOUNDS + tuple(sorted(YAKU_SOUNDS))

# 和了語音序列的時序（秒），可調。
WIN_INTRO_GAP = 1.5   # 和了 → 開始唸役名前的等待（對齊儀式標題／手牌揭曉）
SEQ_GAP       = 0.15  # 佇列中前後音效之間的基本空隙（實際長度由音檔決定）
CONSUMER_IDLE = 30.0  # 佇列閒置多久後消費者收工（下次播放再自動重啟）

DEBUG = False                                    # 診斷輸出（需要時改 True）
_ready = False                                   # FFmpeg 就緒
_queues: dict[int, asyncio.Queue] = {}           # guild_id -> 待播 (vc, paths) 佇列（多檔＝同時混音）
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
    import importlib.util
    missing = [m for m in ("nacl", "davey") if importlib.util.find_spec(m) is None]
    if missing:                             # 少了就進不了語音（discord.py 2.7 起語音需要 davey）
        print(f"[sfx] 缺少語音套件 {missing}，機器人進不了語音 → pip install -U \"discord.py[voice]\"")
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


def _user_pack(uid) -> str | None:
    """某玩家自己選的語音包（個人設定，存 DB）。沒選／資料夾已不存在＝None（＝他的動作不出聲）。"""
    if not uid:
        return None
    try:
        pack = db.get_user_voice(str(uid))
    except Exception:
        return None
    if pack and os.path.isdir(os.path.join(SOUNDS_DIR, pack)):
        return pack
    return None


def _humans(gid: str) -> list[str]:
    """本局真人的 user_id（對局中取自牌局，開局前取自等待名單）。"""
    from .state import _games, _waiting
    gs = _games.get(gid)
    if gs is not None:
        return [str(p.user_id) for p in gs.players if not p.is_bot]
    return [str(p["user_id"]) for p in (_waiting.get(gid) or []) if not p.get("is_bot")]


def _in_room(vc, uid) -> bool:
    """這位玩家還在語音房裡嗎（離開語音的人，他的動作就不出聲）。讀不到成員名單＝當作在。"""
    try:
        return any(str(m.id) == str(uid) for m in vc.members)
    except Exception:
        return True


def _voice_pack(vc, uid) -> str | None:
    """這個動作要用的語音包：做動作的真人有選、且人還在語音房。電腦／掛機（uid=None）一律不出聲。"""
    if not uid or not _in_room(vc, uid):
        return None
    return _user_pack(uid)


def _table_pack(gid: str, vc) -> str | None:
    """開局音用的語音包：房主的優先，否則任一位有選的真人（都要還在語音房）。"""
    from .state import _room_owners
    host = _room_owners.get(gid)
    for uid in ([host] if host else []) + _humans(gid):
        pack = _voice_pack(vc, uid)
        if pack:
            return pack
    return None


def _any_pack(gid: str, vc) -> bool:
    """語音房裡是否至少有一位本局真人選了語音包（都沒選＝機器人不必進語音）。"""
    return any(_voice_pack(vc, u) for u in _humans(gid))


def _variants(d: str, name: str) -> list[str]:
    """資料夾 d 裡屬於這個音效名的所有檔：`<name>.<ext>` 以及編號變體 `<name>1`、`<name>2`、`<name>_3`…"""
    try:
        files = os.listdir(d)
    except OSError:
        return []
    pat = re.compile(re.escape(name) + r"(?:[ _-]?\d+)?", re.IGNORECASE)
    out = []
    for f in files:
        stem, ext = os.path.splitext(f)
        if ext.lower() in AUDIO_EXTS and pat.fullmatch(stem):
            path = os.path.join(d, f)
            if os.path.isfile(path):
                out.append(path)
    return out


def _resolve(pack: str | None, name: str) -> str | None:
    """找出音效檔路徑：先找語音包資料夾，再退回根目錄（預設／後備）。
    同一個音效有好幾個檔（如 `七對子`、`七對子1`、`七對子2`）就每次隨機挑一個。找不到＝None。"""
    dirs = []
    if pack:
        dirs.append(os.path.join(SOUNDS_DIR, pack))
    dirs.append(SOUNDS_DIR)
    for d in dirs:
        found = _variants(d, name)
        if found:
            return random.choice(found)
    return None


def pack_coverage(pack: str | None) -> tuple[int, list[str]]:
    """某語音包（含根目錄後備）備齊了幾個預期音效、缺哪些。回傳 (齊全數, 缺少名稱列表)。"""
    missing = [n for n in ALL_SOUND_NAMES if _resolve(pack, n) is None]
    return len(ALL_SOUND_NAMES) - len(missing), missing


# ───────────────────────── 語音連線 ─────────────────────────
def _busy_elsewhere(vc) -> bool:
    """機器人正在同伺服器另一間「對局進行中」的語音房（一台伺服器只能進一個語音頻道，不搶過來）。"""
    from .state import _games
    cur = getattr(vc.guild, "voice_client", None)
    if not cur or not cur.channel or cur.channel.id == vc.id:
        return False
    return any((th.get("voice") is not None and th["voice"].id == cur.channel.id and gid in _games)
               for gid, th in _threads.items())


async def _ensure_client(vc) -> discord.VoiceClient | None:
    """確保機器人在該語音頻道，回傳 VoiceClient；失敗回 None。
    機器人正在別間對局中的語音房＝不搬過來（回 None）。不可自聾／自靜音（Discord error 50167）。"""
    try:
        cur = vc.guild.voice_client
        if cur and cur.channel and cur.channel.id == vc.id:
            return cur
        if _busy_elsewhere(vc):
            if DEBUG:
                print(f"[sfx] 略過：機器人正在 {cur.channel.name} 的對局裡")
            return None
        if cur:
            await cur.move_to(vc)
            return vc.guild.voice_client
        try:                       # 房間滿了：沒有「移動成員」權限的機器人進不去 → 多開一格
            p = vc.permissions_for(vc.guild.me)
            if (vc.user_limit and len(vc.members) >= vc.user_limit
                    and not (p.administrator or p.move_members)):
                await vc.edit(user_limit=vc.user_limit + 1, reason="Suzume Tsuk 留位給機器人播音效")
        except Exception:
            pass
        return await vc.connect(self_deaf=False, self_mute=False)
    except Exception as e:
        print(f"[sfx] 進語音失敗：{e!r}　← 需要語音套件（pip install -U \"discord.py[voice]\"，含 PyNaCl 與 davey）與連線權限")
        return None


async def join(vc, gid: str) -> bool:
    """對局開始時先讓機器人進語音房（不必等第一個音效）。未就緒／沒有任何真人選語音包＝略過。"""
    if not _ready:
        return False
    if not _any_pack(gid, vc):
        return False
    return (await _ensure_client(vc)) is not None


async def join_room(vc, uid) -> bool:
    """開局前：語音房裡有人選了語音包就先進房（讓大家知道機器人在）。
    沒選語音包、未就緒、或機器人正在別間對局的語音房＝不進。"""
    if not _ready or not _user_pack(uid) or not _in_room(vc, uid):
        return False
    return (await _ensure_client(vc)) is not None


async def leave(guild, vc=None) -> None:
    """對局結束：停止佇列、離開語音。vc＝這場的語音房：機器人不在這間（在別間對局）就不動它。"""
    cur = getattr(guild, "voice_client", None)
    if vc is not None and cur is not None and cur.channel is not None and cur.channel.id != vc.id:
        return
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
def _enqueue(vc, *paths: str) -> None:
    """排入一則；給多個檔＝這幾個同時播（混音，例如雙榮兩人一起喊「榮」）。"""
    gid = vc.guild.id
    q = _queues.get(gid)
    if q is None:
        q = _queues[gid] = asyncio.Queue()
    q.put_nowait((vc, paths))
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
                vc, paths = await asyncio.wait_for(q.get(), timeout=CONSUMER_IDLE)
            except asyncio.TimeoutError:
                return
            await _play_path(vc, paths)
            await asyncio.sleep(SEQ_GAP)
    except asyncio.CancelledError:
        pass
    finally:
        if _consumers.get(gid) is asyncio.current_task():
            _consumers.pop(gid, None)


def _source(paths: tuple) -> discord.FFmpegPCMAudio:
    """一個檔＝直接播；多個檔＝FFmpeg amix 混成一軌同時播（音量乘回人數免得變小聲，再用 alimiter 防爆音）。"""
    if len(paths) == 1:
        return discord.FFmpegPCMAudio(paths[0])
    n = len(paths)
    extra = " ".join(f"-i {shlex.quote(p)}" for p in paths[1:])
    return discord.FFmpegPCMAudio(
        paths[0], before_options=extra,
        options=(f"-filter_complex amix=inputs={n}:duration=longest:dropout_transition=0,"
                 f"volume={n},alimiter=limit=0.9"))


async def _play_path(vc, paths) -> None:
    """把音檔（多個＝混音同時播）播進語音頻道並等它播完（FFmpeg → PCM → Opus）。"""
    if isinstance(paths, str):
        paths = (paths,)
    path = " + ".join(os.path.basename(p) for p in paths)   # 記錄用
    client = await _ensure_client(vc)
    if client is None:
        print(f"[sfx] 播放略過：無法連上語音（PyNaCl／連線權限？）")
        return
    try:
        if client.is_playing():
            client.stop()
        source = _source(paths)
        done = asyncio.Event()
        loop = asyncio.get_running_loop()

        def _after(err):
            if err:
                print(f"[sfx] 播放錯誤：{err!r}")
            loop.call_soon_threadsafe(done.set)

        if DEBUG:
            print(f"[sfx] ▶ 播放 {path}（頻道 {getattr(client.channel,'name','?')}）")
        client.play(source, after=_after)
        await done.wait()
        if DEBUG:
            print(f"[sfx] ✔ 播完 {path}")
    except Exception as e:
        print(f"[sfx] 播放 {path} 失敗：{e!r}")


async def play(gid: str, name: str, uid=None, fallback: str | None = None) -> None:
    """把音效排入該對局語音房的播放佇列（不擋呼叫端）。
    uid＝做這個動作的真人 → 用他自己選的語音包；他沒選、已離開語音房、或 uid=None（電腦／掛機）＝不出聲。
    fallback＝包裡沒有 name 時改播的音效名（例：途中流局沒錄專屬檔 → ryuukyoku）。
    沒綁語音、功能未就緒、找不到檔＝靜默略過。"""
    if not _ready:
        return
    vc = (_threads.get(gid) or {}).get("voice")
    if vc is None:
        if DEBUG:
            print(f"[sfx] play({name}) 略過：本局未綁語音房")
        return
    pack = _table_pack(gid, vc) if uid is _TABLE else _voice_pack(vc, uid)
    if not pack:
        if DEBUG:
            print(f"[sfx] play({name}) 略過：電腦／掛機／未選語音包／不在語音房")
        return
    path = _resolve(pack, name) or (_resolve(pack, fallback) if fallback else None)
    if path is None:
        if DEBUG:
            print(f"[sfx] play({name}) 略過：找不到音效檔")
        return
    if DEBUG:
        print(f"[sfx] play({name}) → 排入 {os.path.basename(path)}")
    _enqueue(vc, path)


_TABLE = object()   # play() 內部用：不屬於任何玩家的聲音（開局、流局）


async def play_table(gid: str, name: str, fallback: str | None = None) -> None:
    """不屬於任何玩家的聲音（流局、四風連打…）：用房主（或其他還在語音房、有選語音包的真人）的語音包。"""
    await play(gid, name, _TABLE, fallback)


async def play_together(gid: str, name: str, uids) -> None:
    """幾位玩家「同時」發出同一個音效（雙榮一起喊「榮」）：各用自己的語音包混成一則播。
    電腦／掛機／沒選／不在語音房的人略過；同一個檔只播一次。"""
    if not _ready:
        return
    vc = (_threads.get(gid) or {}).get("voice")
    if vc is None:
        return
    paths = []
    for uid in uids:
        pack = _voice_pack(vc, uid)
        path = _resolve(pack, name) if pack else None
        if path and path not in paths:
            paths.append(path)
    if paths:
        _enqueue(vc, *paths)


async def play_start(gid: str) -> None:
    """開局音（見 `play_table`）。"""
    await play_table(gid, "start")


# ───────────────────────── 和了語音 ─────────────────────────
def tier_sound(name: str) -> str:
    """把計分等級名（`ScoreResult.name`）對應到音效名。
    滿貫以下（空字串）＝""（不唸）；含「役滿」＝役滿（累計役滿另計）；否則依滿貫階梯。"""
    if not name:
        return ""
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
    return ""


async def play_win(gid: str, result, uid=None) -> None:
    """單一和牌者的和牌語音（見 `play_wins`）。"""
    await play_wins(gid, [(result, uid)])


async def play_wins(gid: str, wins) -> None:
    """和牌語音：對齊和牌儀式，逐一「唸出每個役名」（音效名＝役名，如「立直」「平和」「寶牌」），
    滿貫以上最後再公布打點階級（滿貫／跳滿／…）。役名沒錄音效檔＝該役靜默略過。
    wins＝[(ScoreResult, 和牌者 uid), …]，依揭曉順序；雙榮就依序唸兩位，各用自己的語音包
    （電腦／掛機／沒選／不在語音房的那位略過）。
    設計為背景任務呼叫（含 sleep，不擋畫面儀式）；實際播放由佇列依序進行。"""
    if not _ready:
        return
    vc = (_threads.get(gid) or {}).get("voice")
    if vc is None:                                        # 沒綁語音就別空跑
        return
    wins = [(r, u) for r, u in wins if r is not None and _voice_pack(vc, u)]
    if not wins:
        return
    await asyncio.sleep(WIN_INTRO_GAP)        # 等標題＋手牌揭曉後才開始唸役
    for result, uid in wins:
        # 揭曉順序同 win_ceremony：役滿則只列役滿，否則列一般役（含寶牌等懸賞，有錄檔才會出聲）
        if getattr(result, "yakuman", None):
            names = [n for n, *_ in result.yakuman]
        else:
            names = [n for n, *_ in (result.yaku or [])]
        for n in names:
            await play(gid, n, uid)           # 逐一唸役名（排入佇列，依序播）
        tier = tier_sound(getattr(result, "name", "") or "")
        if tier:
            await play(gid, tier, uid)        # 打點階級
