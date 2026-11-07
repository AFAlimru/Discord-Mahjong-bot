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
"""觀戰：每場伺服器對局（房間選項「觀戰」開啟時）都記錄牌桌快照，有人要看才建 Discord 頻道。

- 即時頻道（看不到手牌）：跟公開牌桌同步，結果訊息也貼在這裡。
- 明牌頻道（看得到手牌）：延遲 SPECTATE_DELAY 秒，顯示「那個時間點」的快照。
- 網站用的公開介面：watchable_games / live_state / delayed_state / is_player（回傳都可直接轉 JSON）。

每局的紀錄放在這個模組自己的 _recs（對局結束、_cleanup 之後還留著，直到延遲播完再過一段時間才清掉）。
對局結束後：即時畫面馬上顯示「已結束」與最終結果；明牌畫面把最後 SPECTATE_DELAY 秒的緩衝播完，
播到結束點才顯示最終結果（播完前顯示「對局已結束，延遲畫面播放中」）。紀錄保留到 CLOSE_AFTER 後才清掉，
這段時間內網站與 Discord 都還能進來看。
沒有人看的時候只做字串與字典的組裝，不呼叫 Discord。"""
from __future__ import annotations

import asyncio
import copy
import math
import re
import time

import discord

from . import i18n
from . import rooms
from . import tiles as T
from .render import board_parts, board_status_text, _round, _wind
from .state import _games, _threads, _bg_tasks

SPECTATE_DELAY = 180       # 明牌的延遲秒數
RING_KEEP      = 300       # 快照保留秒數（至少要大於延遲）
CLOSE_AFTER    = 600       # 延遲播完後，再保留多久才刪觀戰頻道
TICK           = 3.0       # 明牌頻道的更新間隔

_recs: dict = {}           # gid -> _Rec


def _clock() -> float:
    """現在時間（測試時可替換）。"""
    return time.time()


class _Snap:
    __slots__ = ("ts", "parts", "state")

    def __init__(self, ts: float, parts: list, state: dict):
        self.ts, self.parts, self.state = ts, parts, state


class _Ev:
    __slots__ = ("ts", "hand", "text")

    def __init__(self, ts: float, hand: int, text: str):
        self.ts, self.hand, self.text = ts, hand, text


class _Rec:
    def __init__(self, gid: str, guild_id, channel, gs, config: dict, enabled: bool):
        self.gid = gid
        self.guild_id = str(guild_id)
        self.channel = channel                       # 開房的頻道（建觀戰頻道時找類別用）
        self.enabled = enabled
        self.lang = config.get("lang", i18n.DEFAULT)
        self.mode = "sanma" if gs.is_sanma else "yonma"
        self.started_at = _clock()
        self.room_no = rooms.room_no(gid)
        self.tag = rooms.tag(gid)                    # 頻道名用的房號代碼（對局結束後房號登記會被清掉，所以先記下）
        self.players = [{"seat": p.seat, "user_id": p.user_id, "name": p.username, "is_bot": p.is_bot}
                        for p in gs.players]
        self.names = {p["user_id"]: p["name"] for p in self.players}     # 狀態文字裡的 <@id> 換成名字用
        self.snaps: list[_Snap] = []
        self.events: list[_Ev] = []
        self.hand_no = 0
        self.status = ""
        self.finished_at: float | None = None
        # Discord 端
        self.lock = asyncio.Lock()
        self.live_ch = None
        self.del_ch = None
        self.live_grp = None
        self.live_parts: list[str] | None = None
        self.live_dirty = False
        self.live_events: list[str] = []
        self.live_final: list[str] | None = None     # 結束時的即時版牌桌（不含手牌），結束後才建的即時頻道用
        self.live_end_sent = False
        self.live_task: asyncio.Task | None = None
        self.del_grp = None
        self.del_notice = None
        self.del_shown: _Snap | None = None
        self.del_ev_idx = 0
        self.del_pending = None                      # 「對局已結束，延遲畫面播放中」那則訊息
        self.del_end_sent = False
        self.pub_task: asyncio.Task | None = None
        self.close_task: asyncio.Task | None = None

    @property
    def latest(self) -> _Snap | None:
        return self.snaps[-1] if self.snaps else None


# ═══════════════════════════════════════════════════════════════
#  設定／登記
# ═══════════════════════════════════════════════════════════════

def enabled(config: dict) -> bool:
    """這桌的設定是否開放觀戰（預設開；段位／匹配局不開）。"""
    return bool(config.get("spectate", True)) and not config.get("ranked")


def rec_of(gid: str):
    return _recs.get(gid)


def register(gid: str, channel, gs, config: dict):
    """對局開始時登記。回傳這局的紀錄（不開放觀戰＝輕量紀錄，只供 is_player／watchable_games 用）。"""
    guild = getattr(channel, "guild", None)
    if guild is None:
        return None
    rec = _Rec(gid, guild.id, channel, gs, config, enabled(config))
    _recs[gid] = rec
    if rec.enabled:
        record(gid, gs, "")
    return rec


def watch_view(gid: str, lang: str):
    """開局公告上的「觀戰」按鈕；這局不開放觀戰就回 None。"""
    rec = _recs.get(gid)
    if rec is None or not rec.enabled:
        return None
    v = discord.ui.View(timeout=None)
    v.add_item(WatchButton(gid, lang))
    return v


def channel_ids(gid: str) -> list:
    """這局建過的觀戰頻道 id（存進房間設定，重啟後清理用）。"""
    rec = _recs.get(gid)
    if rec is None:
        return []
    return [c.id for c in (rec.live_ch, rec.del_ch) if c is not None]


def is_player(gid: str, user_id) -> bool:
    """這個人是不是該局的玩家（網站用來拒絕玩家看明牌）。"""
    uid = str(user_id)
    rec = _recs.get(gid)
    if rec is not None and any(p["user_id"] == uid for p in rec.players):
        return True
    gs = _games.get(gid)
    return gs is not None and any(p.user_id == uid for p in gs.players)


# ═══════════════════════════════════════════════════════════════
#  結構化狀態（給網站）
# ═══════════════════════════════════════════════════════════════

_EMO_RE  = re.compile(r"<a?:\w+:\d+>")
_rev_cache: dict = {}


def _emoji_codes() -> dict:
    """表情字串 → 牌代碼（含橫牌 _s）。"""
    if not _rev_cache:
        for sk in T._MAPS.values():
            for code, emo in sk.get("code", {}).items():
                _rev_cache.setdefault(emo, code)
    return _rev_cache


_MENTION_RE = re.compile(r"<@[!&]?(\d+)>")
_CHANNEL_RE = re.compile(r"<#\d+>")


def _demention(text: str, names: dict | None) -> str:
    """<@使用者id> 換成遊戲裡的名字（找不到就寫「玩家」），不要把 id 露給觀戰的人，也不會真的提及人。"""
    names = names or {}
    s = _MENTION_RE.sub(lambda m: names.get(m.group(1)) or "玩家", text)
    return _CHANNEL_RE.sub("", s)


def plain(text: str, names: dict | None = None) -> str:
    """Discord 文字 → 純文字：拿掉 markdown 標記，牌表情換成牌代碼（如 1m），橫放標記去掉；
    提及（<@id>）換成名字。"""
    rev = _emoji_codes()
    text = _demention(text, names)
    s = _EMO_RE.sub(lambda m: " " + rev.get(m.group(0), "?").replace("_s", "") + " ", text)
    s = s.replace(T.SIDE_MARK, "")
    out = []
    for ln in s.split("\n"):
        ln = re.sub(r"^(?:>\s*)?(?:#{1,3}\s+)?", "", ln.strip())
        out.append(re.sub(r"[ ]{2,}", " ", ln.replace("**", "")).strip())
    return "\n".join(out).strip()


def _tile_obj(tile, side: bool = False) -> dict:
    return {"tile": tile.code, "side": bool(side)}


def _meld_state(m, owner_seat: int) -> dict:
    kind = {1: "chi", 2: "pon", 3: "kan", 4: "ankan"}.get(int(m.meld_type), "?")
    if m.meld_type == 4 and len(m.tiles) == 4:
        tiles = [{"tile": "back", "side": False}, _tile_obj(m.tiles[1]),
                 _tile_obj(m.tiles[2]), {"tile": "back", "side": False}]
    else:
        order = T._meld_order(m, owner_seat)
        if order is None:
            tiles = [_tile_obj(x) for x in m.tiles]
        else:
            tiles = [_tile_obj(x, sd) for x, sd in order]
    return {"type": kind, "tiles": tiles, "from_seat": m.from_seat}


def _build_state(gs, rec: _Rec, status: str, finished: bool) -> dict:
    """一張牌桌的結構化狀態（含手牌；即時版由 _strip_hands 去掉）。"""
    lang = rec.lang
    total = len(gs.dora_indicators)
    dora = [x.code for x in gs.dora_indicators[:gs.revealed_dora]] + ["back"] * (total - gs.revealed_dora)
    players = []
    for p in gs.players:
        ri = getattr(p, "riichi_idx", -1)
        hand = sorted(p.hand, key=lambda x: (x.suit, x.value))
        players.append({
            "seat": p.seat, "wind": _wind(p.seat, lang, gs),
            "wind_idx": (p.seat - gs.dealer_seat) % len(gs.players),
            "name": p.username, "is_bot": bool(p.is_bot), "is_dealer": bool(p.is_dealer),
            "score": int(p.score), "riichi": bool(p.riichi),
            "open_riichi": bool(p.riichi and getattr(p, "open_riichi", False)),
            "riichi_idx": int(ri),
            "river": [_tile_obj(x, i == ri) for i, x in enumerate(p.discards)],
            "melds": [_meld_state(m, p.seat) for m in p.melds],
            "kita": int(getattr(p, "kita", 0) or 0),
            "hand": {"tiles": [x.code for x in hand],
                     "drawn": p.drawn_tile.code if p.drawn_tile else None},
        })
    return {
        "mode": rec.mode, "lang": lang,
        "round_wind": gs.round_wind, "round_num": gs.round_num, "round_text": _round(gs, lang),
        "honba": gs.honba, "riichi_sticks": gs.riichi_sticks, "wall": gs.tiles_left,
        "dora": dora, "dealer_seat": gs.dealer_seat, "turn_seat": gs.current_seat,
        "players": players, "status": plain(status, rec.names), "finished": bool(finished),
    }


def _strip_hands(state: dict) -> dict:
    """即時版：去掉手牌（開立直的人手牌本來就公開，保留牌、不給剛摸的那張）。"""
    st = copy.deepcopy(state)
    for p in st["players"]:
        if p.get("open_riichi"):
            p["hand"]["drawn"] = None
        else:
            p.pop("hand", None)
    return st


def _results(rec: _Rec, upto: float | None) -> list:
    """目前看得到的結果文字（最新一局的和牌／流局結果與最終順位，純文字）。"""
    evs = [e for e in rec.events if upto is None or e.ts <= upto]
    if not evs:
        return []
    h = evs[-1].hand
    return [plain(e.text, rec.names) for e in evs if e.hand == h]


def _base(rec: _Rec) -> dict:
    return {"gid": rec.gid, "room_no": rec.room_no,
            "room": rooms.code(rec.room_no) if rec.room_no else None,
            "guild_id": rec.guild_id, "started_at": rec.started_at}


def _ended_info(rec: _Rec, now: float | None = None) -> dict:
    """結束相關欄位：ended＝對局已打完；ended_at；end_in＝明牌畫面還要幾秒才播到結束點（沒結束＝None）；
    closes_in＝還剩幾秒觀戰紀錄會被清掉（沒結束＝None）。"""
    if rec.finished_at is None:
        return {"ended": False, "ended_at": None, "end_in": None, "closes_in": None}
    now = _clock() if now is None else now
    return {"ended": True, "ended_at": rec.finished_at,
            "end_in": max(0, int(math.ceil(rec.finished_at + SPECTATE_DELAY - now))),
            "closes_in": max(0, int(math.ceil(rec.finished_at + SPECTATE_DELAY + CLOSE_AFTER - now)))}


def live_state(gid: str) -> dict | None:
    """即時狀態（沒有手牌）。這局不存在／不開放觀戰＝None。"""
    rec = _recs.get(gid)
    if rec is None or not rec.enabled or rec.latest is None:
        return None
    snap = rec.latest
    out = _base(rec)
    out.update(_strip_hands(snap.state))
    out.update(ready=True, updated_at=snap.ts, results=_results(rec, None))
    out.update(_ended_info(rec))
    return out


def _visible(rec: _Rec, cutoff: float, upto_ts: float | None = None) -> _Snap | None:
    best = None
    lim = cutoff if upto_ts is None else min(cutoff, upto_ts)
    for s in rec.snaps:
        if s.ts <= lim:
            best = s
        else:
            break
    return best


def delayed_state(gid: str, now: float | None = None) -> dict | None:
    """明牌狀態（含手牌）：SPECTATE_DELAY 秒前的快照。還沒到時間＝ready False 與 available_in 秒數。"""
    rec = _recs.get(gid)
    if rec is None or not rec.enabled:
        return None
    now = _clock() if now is None else now
    cutoff = now - SPECTATE_DELAY
    snap = _visible(rec, cutoff)
    out = _base(rec)
    out["delay"] = SPECTATE_DELAY
    if snap is None:
        first = rec.snaps[0].ts if rec.snaps else now
        out.update(ready=False, available_in=max(0, int(math.ceil(first + SPECTATE_DELAY - now))))
        out.update(_ended_info(rec, now))
        return out
    out.update(copy.deepcopy(snap.state))
    out.update(ready=True, updated_at=snap.ts, results=_results(rec, cutoff))
    out.update(_ended_info(rec, now))
    return out


def _round_of(rec: _Rec) -> str:
    return rec.latest.state["round_text"] if rec.latest else ""


def watchable_games(guild_id: str | None = None, include_closed: bool = False) -> list[dict]:
    """可以觀戰的對局：進行中的，加上剛結束、紀錄還沒清掉的（finished 欄位 True）。
    include_closed＝連「不開放觀戰」的也列，spectate 欄位會是 False。"""
    out = []
    for rec in list(_recs.values()):
        if guild_id is not None and rec.guild_id != str(guild_id):
            continue
        if not (rec.enabled or include_closed):
            continue
        d = _base(rec)
        d.update(mode=rec.mode, round=_round_of(rec), spectate=rec.enabled,
                 finished=rec.finished_at is not None, ended_at=rec.finished_at,
                 players=[p["name"] for p in rec.players],
                 humans=[p["name"] for p in rec.players if not p["is_bot"]])
        out.append(d)
    out.sort(key=lambda d: (d["finished"], d["room_no"] is None, d["room_no"] or 0))
    return out


# ═══════════════════════════════════════════════════════════════
#  記錄（由 flow 呼叫）
# ═══════════════════════════════════════════════════════════════

def record(gid: str, gs, status: str = "", finished: bool = False) -> None:
    """每次牌桌更新記一張快照。沒登記／不開放觀戰＝什麼都不做。有即時頻道就順便排程同步。"""
    rec = _recs.get(gid)
    if rec is None or not rec.enabled or gs is None:
        return
    if rec.finished_at is not None and not finished:
        return                                # 結束後不再有新畫面（最後一張快照已經是 finished）
    try:
        T.set_skin(None)                      # 公開畫面一律預設牌風
        now = _clock()
        status = _demention(status, rec.names)    # 提及換成名字（Discord 觀戰頻道也一樣，不會提及到人）
        parts = board_parts(gs, rec.lang, True) + [board_status_text(status)]
        prev = rec.latest
        rec.status = status
        if prev is None or prev.parts != parts or prev.state["finished"] != finished:
            snap = _Snap(now, parts, _build_state(gs, rec, status, finished))
            rec.snaps.append(snap)
            # 修剪：舊於 RING_KEEP、且已被更新的「延遲可見快照」取代的才丟
            while (len(rec.snaps) >= 2 and rec.snaps[1].ts <= now - SPECTATE_DELAY
                   and rec.snaps[0].ts < now - RING_KEEP):
                rec.snaps.pop(0)
        if finished or rec.live_grp is not None:
            live = board_parts(gs, rec.lang, False) + [board_status_text(status)]
            if finished:
                rec.live_final = live
            if rec.live_grp is not None:
                rec.live_parts = live
                _kick_live(rec)
    except Exception as e:
        print(f"[spectate] 記錄快照失敗：{e!r}")


def new_hand(gid: str) -> None:
    rec = _recs.get(gid)
    if rec is not None:
        rec.hand_no += 1


def event(gid: str, text: str) -> None:
    """一局的結果文字（和牌儀式最後一版、流局結果、最終順位）。即時頻道馬上貼，明牌頻道延遲貼。"""
    rec = _recs.get(gid)
    if rec is None or not rec.enabled or not text:
        return
    rec.events.append(_Ev(_clock(), rec.hand_no, text))
    if len(rec.events) > 60:
        drop = len(rec.events) - 60
        del rec.events[:drop]
        rec.del_ev_idx = max(0, rec.del_ev_idx - drop)
    if rec.live_grp is not None:
        rec.live_events.append(text)
        _kick_live(rec)


def finish(gid: str, gs=None, status: str = "") -> None:
    """對局結束（可重複呼叫）：記最後一張快照，排程延遲播完＋保留一段時間後清掉頻道與紀錄。"""
    rec = _recs.get(gid)
    if rec is None or rec.finished_at is not None:
        return
    rec.finished_at = _clock()
    if not rec.enabled:
        _recs.pop(gid, None)
        return
    if gs is None:
        gs = _games.get(gid)                  # _cleanup 呼叫時對局還在 _games 裡，用它補一張結束快照
    if gs is not None:
        record(gid, gs, status, finished=True)
    elif rec.latest is not None and not rec.latest.state["finished"]:
        st = copy.deepcopy(rec.latest.state)
        st["finished"] = True
        rec.snaps.append(_Snap(rec.finished_at, rec.latest.parts, st))
    if rec.live_grp is not None:                  # 即時頻道已經開著：貼「已結束」
        rec.live_events.append(i18n.t("spectate.ended_live", rec.lang))
        rec.live_end_sent = True
        try:
            _kick_live(rec)
        except RuntimeError:
            pass
    try:
        rec.close_task = asyncio.get_running_loop().create_task(_closer(rec))
        _bg_tasks.add(rec.close_task)
        rec.close_task.add_done_callback(_bg_tasks.discard)
    except RuntimeError:
        pass


async def _closer(rec: _Rec) -> None:
    deadline = rec.finished_at + SPECTATE_DELAY + CLOSE_AFTER
    while _clock() < deadline:
        await asyncio.sleep(min(5.0, max(0.0, deadline - _clock())))
    await close(rec.gid, force=True)


async def close(gid: str, force: bool = False) -> None:
    """立刻收掉：停掉背景任務、刪觀戰頻道、丟掉紀錄（/end 強制結束走這）。
    已經正常打完的局不會被外面的房間清理提早收掉（force=False），要等 _closer 到期才清；
    進行中的局被強制結束＝直接收。"""
    rec = _recs.get(gid)
    if rec is None:
        return
    if rec.finished_at is not None and rec.enabled and not force:
        return
    _recs.pop(gid, None)
    for tk in (rec.live_task, rec.pub_task):
        if tk is not None and not tk.done() and tk is not asyncio.current_task():
            tk.cancel()
    if rec.close_task is not None and not rec.close_task.done() and rec.close_task is not asyncio.current_task():
        rec.close_task.cancel()
    for ch in (rec.live_ch, rec.del_ch):
        if ch is not None:
            try:
                await ch.delete(reason="Suzume Tsuk 觀戰頻道")
            except Exception as e:
                print(f"[spectate] 刪除觀戰頻道失敗：{e!r}")
    rec.live_ch = rec.del_ch = None


# ═══════════════════════════════════════════════════════════════
#  Discord：即時頻道
# ═══════════════════════════════════════════════════════════════

def _kick_live(rec: _Rec) -> None:
    rec.live_dirty = True
    if rec.live_task is None or rec.live_task.done():
        rec.live_task = asyncio.get_running_loop().create_task(_live_loop(rec))
        _bg_tasks.add(rec.live_task)
        rec.live_task.add_done_callback(_bg_tasks.discard)


async def _live_loop(rec: _Rec) -> None:
    """合併連續的更新：一次只有一個任務在送，送完看有沒有新的。"""
    while rec.live_dirty or rec.live_events:
        rec.live_dirty = False
        evs, rec.live_events = rec.live_events, []
        if rec.live_ch is None or rec.live_grp is None:
            return
        for tx in evs:
            try:
                await rec.live_ch.send(tx)
            except Exception:
                pass
        if rec.live_parts is not None:
            await rec.live_grp.sync(rec.live_parts)


async def flush(gid: str) -> None:
    """等即時頻道的同步任務做完（測試用）。"""
    rec = _recs.get(gid)
    if rec is not None and rec.live_task is not None:
        await rec.live_task


# ═══════════════════════════════════════════════════════════════
#  Discord：明牌頻道（延遲）
# ═══════════════════════════════════════════════════════════════

async def _show(rec: _Rec, snap: _Snap | None) -> None:
    from .flow import MsgGroup
    if rec.del_ch is None:
        return
    if snap is None:
        if rec.del_notice is None:
            try:
                rec.del_notice = await rec.del_ch.send(
                    i18n.t("spectate.wait", rec.lang, min=SPECTATE_DELAY // 60))
            except Exception:
                pass
        return
    if snap is rec.del_shown:
        return
    if rec.del_notice is not None:
        try:
            await rec.del_notice.delete()
        except Exception:
            pass
        rec.del_notice = None
    if rec.del_grp is None:
        rec.del_grp = await MsgGroup.send(rec.del_ch, snap.parts)
    else:
        await rec.del_grp.sync(snap.parts)
    rec.del_shown = snap


async def _after_show(rec: _Rec) -> None:
    """對局結束後的提示：延遲畫面還沒播到結束點＝貼一則「播放中」，播到了＝刪掉它、貼「已結束」。"""
    if rec.finished_at is None or rec.del_ch is None or rec.del_end_sent:
        return
    caught = rec.del_shown is not None and rec.del_shown.state["finished"]
    if caught:
        if rec.del_pending is not None:
            try:
                await rec.del_pending.delete()
            except Exception:
                pass
            rec.del_pending = None
        try:
            await rec.del_ch.send(i18n.t("spectate.ended_hands", rec.lang))
        except Exception:
            pass
        rec.del_end_sent = True
    elif rec.del_pending is None:
        try:
            rec.del_pending = await rec.del_ch.send(i18n.t("spectate.ended_pending", rec.lang))
        except Exception:
            pass


async def _publish_once(rec: _Rec, now: float | None = None) -> None:
    """把「現在減延遲」以前的東西依時間順序顯示：快照以最新的為準，結果文字在它的時間點貼出。"""
    now = _clock() if now is None else now
    cutoff = now - SPECTATE_DELAY
    while rec.del_ev_idx < len(rec.events) and rec.events[rec.del_ev_idx].ts <= cutoff:
        ev = rec.events[rec.del_ev_idx]
        rec.del_ev_idx += 1
        await _show(rec, _visible(rec, cutoff, ev.ts))
        try:
            await rec.del_ch.send(ev.text)
        except Exception:
            pass
    await _show(rec, _visible(rec, cutoff))
    await _after_show(rec)


def _publish_done(rec: _Rec) -> bool:
    return (rec.finished_at is not None and rec.del_ev_idx >= len(rec.events)
            and rec.latest is not None and rec.del_shown is rec.latest and rec.del_end_sent)


async def _publisher(rec: _Rec) -> None:
    try:
        while rec.del_ch is not None:
            try:
                await _publish_once(rec)
            except Exception as e:
                print(f"[spectate] 明牌頻道更新失敗：{e!r}")
            if _publish_done(rec):
                break
            await asyncio.sleep(TICK)
    except asyncio.CancelledError:
        raise


# ═══════════════════════════════════════════════════════════════
#  Discord：建頻道、請求觀戰
# ═══════════════════════════════════════════════════════════════

def _category(rec: _Rec):
    """觀戰頻道開在哪：對局頻道所在的類別，沒有就用開房頻道的類別。"""
    th = _threads.get(rec.gid) or {}
    pub = th.get("public")
    for ch in (pub, getattr(pub, "parent", None), rec.channel, getattr(rec.channel, "parent", None)):
        cat = getattr(ch, "category", None)
        if isinstance(cat, discord.CategoryChannel):
            return cat
    return None


async def _deny_members(guild, rec: _Rec) -> dict:
    from .flow import _player_member
    out = {}
    for p in rec.players:
        if p["is_bot"]:
            continue
        try:
            m = await _player_member(guild, p["user_id"])
        except Exception:
            m = discord.Object(id=int(p["user_id"]), type=discord.Member)
        out[m] = discord.PermissionOverwrite(view_channel=False)
    return out


async def ensure_channels(guild, gid: str):
    """確保兩個觀戰頻道存在（第一次有人要看才建，之後沿用）。回傳 (即時頻道, 明牌頻道)。"""
    from .flow import MsgGroup, save_surfaces
    rec = _recs.get(gid)
    if rec is None or not rec.enabled:
        raise RuntimeError("not watchable")
    async with rec.lock:
        if rec.live_ch is not None and rec.del_ch is not None:
            return rec.live_ch, rec.del_ch
        cat = _category(rec)
        over = {
            guild.default_role: discord.PermissionOverwrite(
                view_channel=True, read_message_history=True, send_messages=False,
                add_reactions=False, create_public_threads=False, create_private_threads=False,
                send_messages_in_threads=False),
            guild.me: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, manage_channels=True,
                manage_messages=True, embed_links=True),
        }
        over.update(await _deny_members(guild, rec))
        tag = rec.tag
        if rec.live_ch is None:
            ch = await guild.create_text_channel(
                i18n.t("spectate.ch_live", rec.lang, tag=tag), category=cat,
                overwrites=over, reason="Suzume Tsuk 觀戰頻道")
            rec.live_ch = ch
            save_surfaces(gid)
            gs = _games.get(gid)
            T.set_skin(None)
            rec.live_parts = (board_parts(gs, rec.lang, False) + [board_status_text(rec.status)]
                              if gs is not None else
                              (rec.live_final or [board_status_text(rec.status)]))   # 已結束的局用結束時存的即時版（沒有手牌）
            rec.live_grp = await MsgGroup.send(ch, rec.live_parts)
            if rec.finished_at is not None:       # 結束後才建頻道：補貼最後的結果與「已結束」
                h = rec.events[-1].hand if rec.events else None
                for e in rec.events:
                    if e.hand == h:
                        try:
                            await ch.send(e.text)
                        except Exception:
                            pass
                try:
                    await ch.send(i18n.t("spectate.ended_live", rec.lang))
                except Exception:
                    pass
                rec.live_end_sent = True
        if rec.del_ch is None:
            ch = await guild.create_text_channel(
                i18n.t("spectate.ch_hands", rec.lang, tag=tag), category=cat,
                overwrites=over, reason="Suzume Tsuk 觀戰頻道")
            rec.del_ch = ch
            save_surfaces(gid)
            cutoff = _clock() - SPECTATE_DELAY
            rec.del_ev_idx = sum(1 for e in rec.events if e.ts <= cutoff)   # 更早的結果不補貼
            await _show(rec, _visible(rec, cutoff))
            rec.pub_task = asyncio.get_running_loop().create_task(_publisher(rec))
            _bg_tasks.add(rec.pub_task)
            rec.pub_task.add_done_callback(_bg_tasks.discard)
        return rec.live_ch, rec.del_ch


def _lang_of(interaction) -> str:
    return i18n.get_user_lang(interaction.user.id, interaction.guild_id, interaction.channel)


async def request_watch(interaction: discord.Interaction, gid: str) -> None:
    """按下「觀戰」／選了房間後：檢查資格、（第一次才）建頻道、私下回覆兩個頻道的連結。"""
    lang = _lang_of(interaction)
    rec = _recs.get(gid)

    async def refuse(key: str) -> None:
        msg = i18n.t(key, lang)
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)

    guild = interaction.guild
    if rec is None or guild is None or str(guild.id) != rec.guild_id:
        return await refuse("spectate.not_found")
    if not rec.enabled:
        return await refuse("spectate.closed")
    if is_player(gid, interaction.user.id):
        return await refuse("spectate.is_player")
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True)
    try:
        live, hands = await ensure_channels(guild, gid)
    except Exception as e:
        print(f"[spectate] 建立觀戰頻道失敗：{e!r}")
        await interaction.followup.send(i18n.t("spectate.fail", lang), ephemeral=True)
        return
    await interaction.followup.send(
        i18n.t("spectate.links", lang, live=live.mention, hands=hands.mention,
               min=SPECTATE_DELAY // 60), ephemeral=True)


def find_by_code(guild_id, text: str):
    """房號代碼 → 這個伺服器正在進行（或剛結束、還能看）的那局的 gid（找不到＝None）。"""
    n = rooms.parse(text)
    if n is None:
        return None
    ended = None
    for rec in _recs.values():
        if rec.guild_id == str(guild_id) and rec.enabled and rec.room_no == n:
            if rec.finished_at is None:
                return rec.gid               # 房號可能已經被新的一局用掉，進行中的優先
            ended = ended or rec.gid
    return ended


class WatchButton(discord.ui.Button):
    """開局公告上的「觀戰」按鈕。"""
    def __init__(self, gid: str, lang: str):
        super().__init__(style=discord.ButtonStyle.secondary, label=i18n.t("spectate.btn", lang))
        self.gid = gid

    async def callback(self, interaction: discord.Interaction) -> None:
        await request_watch(interaction, self.gid)


class _WatchSelect(discord.ui.Select):
    def __init__(self, games: list, lang: str):
        opts = []
        for g in games[:25]:
            names = "・".join(g["humans"] or g["players"])
            tag = f"　{i18n.t('spectate.tag_ended', lang)}" if g.get("finished") else ""
            opts.append(discord.SelectOption(
                label=f"{g['room'] or ''}　{g['round']}{tag}"[:100], value=g["gid"],
                description=names[:100] or None))
        super().__init__(placeholder=i18n.t("spectate.pick", lang), options=opts)

    async def callback(self, interaction: discord.Interaction) -> None:
        await request_watch(interaction, self.values[0])


async def send_picker(interaction: discord.Interaction) -> None:
    """列出這個伺服器可以觀戰的對局讓人選（指令不帶房號、大廳面板按鈕都用它）。"""
    lang = _lang_of(interaction)
    if interaction.guild_id is None:
        await interaction.response.send_message(i18n.t("msg.guild_only", lang), ephemeral=True)
        return
    games = watchable_games(interaction.guild_id)
    if not games:
        await interaction.response.send_message(i18n.t("spectate.none", lang), ephemeral=True)
        return
    v = discord.ui.View(timeout=120)
    v.add_item(_WatchSelect(games, lang))
    await interaction.response.send_message(i18n.t("spectate.pick", lang), view=v, ephemeral=True)


async def command_watch(interaction: discord.Interaction, room: str | None = None) -> None:
    """/mahjong watch [房號]"""
    if not room:
        return await send_picker(interaction)
    lang = _lang_of(interaction)
    if interaction.guild_id is None:
        await interaction.response.send_message(i18n.t("msg.guild_only", lang), ephemeral=True)
        return
    if rooms.parse(room) is None:
        await interaction.response.send_message(i18n.t("spectate.bad_code", lang), ephemeral=True)
        return
    gid = find_by_code(interaction.guild_id, room)
    if gid is None:
        await interaction.response.send_message(i18n.t("spectate.not_found", lang), ephemeral=True)
        return
    await request_watch(interaction, gid)


def room_choices(guild_id, current: str = "", lang: str = i18n.DEFAULT) -> list[tuple[str, str]]:
    """房號自動完成用：[(顯示, 房號代碼)]。"""
    cur = (current or "").strip().upper().lstrip("#")
    out = []
    for g in watchable_games(guild_id):
        if g["room"] and g["room"].startswith(cur):
            tag = f"　{i18n.t('spectate.tag_ended', lang)}" if g.get("finished") else ""
            out.append((f"{g['room']}　{g['round']}{tag}　{'・'.join(g['humans'] or g['players'])}"[:100], g["room"]))
    return out[:25]
