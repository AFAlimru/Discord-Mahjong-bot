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
"""比賽：報名／報到／抽選／預賽（每輪重新分桌）／決賽，整場自動開桌與計分。

流程：create → publish（開放報名，貼公告）→ 報名（Discord 按鈕或網站呼叫 register）→ 報到
→ draw（超額就抽選）→ 開賽時間到（或手動 start_round）→ 預賽每輪隨機分桌、開成真的對局
→ 對局結束時 on_game_end 記錄每個人的精算 pt → 前 advance_n 名進決賽 → 決賽排名＝最終名次。

給網站／後台用的函式都是「同步或 async 的純 Python 介面」，回傳值可直接轉 JSON：
- 會改資料的函式都要帶 by=<操作者的 Discord id>，內部檢查權限（主辦或 ADMIN_DISCORD_ID 裡的管理員；
  建立比賽另看 can_create）。by=None 代表系統自己呼叫（排程、測試），網站不要傳 None。
- 出錯一律丟 TournamentError（.code 是固定代碼，.args[0] 是說明），網站依 code 轉成訊息。
- 凡是會動到 Discord 的（publish／start_round／rerun_table／cancel…）都是 async，要在機器人的 event loop 裡 await。
  其他函式是同步的，需要更新公告時會自己丟背景任務（在 event loop 內呼叫才有效）。

資料表：tournaments／tournament_entrants／tournament_tables／tournament_results（見 db.py）。
對局端的接點只有三處：flow.match_loop_t 結束時 on_game_end、flow._cleanup 時 on_cleanup（對局被中途關掉＝這桌 void）、
flow.finalize_interrupted（中斷結算）也呼叫 on_game_end。"""
from __future__ import annotations

import asyncio
import copy
import json
import math
import random
import re
import secrets
import time
import uuid

import discord

from . import db
from . import i18n
from . import rooms
from . import rulesets
from . import settlement as st
from .config import ADMIN_IDS, TOURNAMENT_PLAYER_CREATE, WEB_BASE_URL
from .state import _games, _waiting, _room_owners, _room_configs

TICK            = 15.0     # 排程器檢查間隔（秒）
RETRY_AFTER     = 300      # 自動開賽失敗後多久再試（秒）
REFRESH_DELAY   = 2.0      # 公告更新的合併延遲（避免連續報名一直改訊息）
SEAT_TRIES      = 3000     # 第 2 輪起，為了少碰到同一批對手而嘗試的洗牌次數

STATUSES   = ("draft", "registration", "running", "finished", "cancelled")
EDITABLE   = ("draft", "registration")

DEFAULT_RULES = {
    "rules": "tenhou",          # 規則預設（rulesets.PRESETS 的鍵）
    "rule_over": {},            # 細項覆寫
    "length": "hanchan",        # 'tonpuu' 東風戰／'hanchan' 半莊戰
    "thinking_time": 25,
    "start_points": None,       # None＝照規則預設
    "wall": None,               # None＝天鳳規則用天鳳牌山，其餘用一番街
    "kuikae": False,
    "open_riichi": False,
    "spectate": True,
    "tobi": None,               # None＝照規則預設
    "abortive": None,           # None＝照規則預設
    "ai_level": "normal",
    "lang": None,               # None＝伺服器主要語言
}

DEFAULT_FORMAT = {
    "prelim_rounds": 2,
    "advance_n": None,          # None＝一桌的人數（四麻 4、三麻 3）
    "final_rounds": 1,
    "table_size": 4,            # 4＝四麻、3＝三麻
    "auto_advance": True,
    "round_gap_minutes": 5,
}


class TournamentError(Exception):
    """code：not_found／forbidden／bad_status／invalid／closed／invite_only／already／not_registered／
    checkin_early／checkin_late／not_required／full／no_players／no_channel／already_started／
    prev_incomplete／not_entrant／bad_game 等。"""
    def __init__(self, code: str, msg: str = ""):
        super().__init__(msg or code)
        self.code = code


def _clock() -> float:
    """現在時間（測試時可替換）。"""
    return time.time()


def _now() -> int:
    return int(_clock())


# ═══════════════════════════════════════════════════════════════
#  權限
# ═══════════════════════════════════════════════════════════════

def is_admin(user_id) -> bool:
    return user_id is not None and str(user_id) in ADMIN_IDS


def can_create(user_id) -> bool:
    """能不能建立比賽：管理員；TOURNAMENT_PLAYER_CREATE 開啟後一般玩家也行（之後的玩家自辦比賽）。"""
    return user_id is not None and (is_admin(user_id) or TOURNAMENT_PLAYER_CREATE)


def can_view(tid: int, user_id=None) -> bool:
    """能不能看這場比賽的頁面：公開且已發布的人人可看；草稿只有主辦／管理員；private 只有主辦／管理員／參賽者。
    get／standings 本身不檢查，網站要先用這個擋。"""
    t = db.tour_get(int(tid))
    if t is None:
        return False
    v = None if user_id is None else str(user_id)
    boss = v is not None and (v == t["organizer_id"] or is_admin(v))
    if t["status"] == "draft":
        return boss
    if t["visibility"] == "public":
        return True
    return boss or (v is not None and any(e["user_id"] == v and e["status"] != "withdrawn"
                                          for e in db.tour_ent_list(t["id"])))


def can_manage(tid: int, user_id) -> bool:
    """能不能管理這場比賽：主辦或管理員。"""
    if user_id is None:
        return False
    t = db.tour_get(int(tid))
    return t is not None and (str(user_id) == t["organizer_id"] or is_admin(user_id))


def _need(tid: int, by) -> dict:
    """取比賽並檢查操作者權限（by=None＝系統）。"""
    t = db.tour_get(int(tid))
    if t is None:
        raise TournamentError("not_found", "沒有這場比賽")
    if by is not None and not can_manage(tid, by):
        raise TournamentError("forbidden", "只有主辦或管理員可以這樣做")
    return t


def _get(tid: int) -> dict:
    t = db.tour_get(int(tid))
    if t is None:
        raise TournamentError("not_found", "沒有這場比賽")
    return t


# ═══════════════════════════════════════════════════════════════
#  設定檢查／規則
# ═══════════════════════════════════════════════════════════════

def _norm_rules(r: dict | None) -> dict:
    out = {**DEFAULT_RULES, **(r or {})}
    if out["rules"] not in rulesets.PRESETS:
        raise TournamentError("invalid", f"沒有這個規則預設：{out['rules']}")
    if out["length"] not in ("tonpuu", "hanchan"):
        raise TournamentError("invalid", "length 只能是 tonpuu 或 hanchan")
    try:
        out["thinking_time"] = max(5, min(180, int(out["thinking_time"])))
    except (TypeError, ValueError):
        raise TournamentError("invalid", "thinking_time 要是整數")
    if out["wall"] not in (None, "riichi_city", "tenhou"):
        raise TournamentError("invalid", "wall 只能是 riichi_city 或 tenhou")
    if not isinstance(out.get("rule_over"), dict):
        raise TournamentError("invalid", "rule_over 要是物件")
    return out


def _norm_format(f: dict | None) -> dict:
    out = {**DEFAULT_FORMAT, **(f or {})}
    try:
        out["table_size"] = int(out["table_size"])
        out["prelim_rounds"] = int(out["prelim_rounds"])
        out["final_rounds"] = int(out["final_rounds"])
        out["round_gap_minutes"] = max(0, int(out["round_gap_minutes"]))
    except (TypeError, ValueError):
        raise TournamentError("invalid", "賽制的數字欄位要是整數")
    if out["table_size"] not in (3, 4):
        raise TournamentError("invalid", "table_size 只能是 3 或 4")
    if out["prelim_rounds"] < 1 or out["final_rounds"] < 0:
        raise TournamentError("invalid", "prelim_rounds 至少 1、final_rounds 至少 0")
    adv = out["advance_n"]
    adv = out["table_size"] if adv is None else int(adv)
    if out["final_rounds"] and not (1 <= adv <= out["table_size"]):
        raise TournamentError("invalid", "advance_n 不能超過一桌的人數（決賽只有一桌）")
    out["advance_n"] = adv
    out["auto_advance"] = bool(out["auto_advance"])
    return out


def room_config(t: dict) -> dict:
    """比賽規則 → 房間設定（跟 /mahjong start 的設定同樣的鍵）。"""
    r, f = t["rules"], t["format"]
    sanma = f["table_size"] == 3
    base = rulesets.resolve({"rules": r["rules"], "rule_over": r.get("rule_over") or {}, "is_sanma": sanma})
    tobi = base.tobi if r.get("tobi") is None else bool(r["tobi"])
    abortive = bool(base.abortive) if r.get("abortive") is None else bool(r["abortive"])
    wall = r.get("wall") or ("tenhou" if r["rules"] == "tenhou" else "riichi_city")
    return {
        "is_sanma": sanma, "thinking_time": r["thinking_time"], "max_players": f["table_size"],
        "length": r["length"], "tobi": tobi, "ruleset": "tenhou" if r["rules"] == "tenhou" else "mixed",
        "start_points": r.get("start_points"),
        "rules": r["rules"], "rule_over": dict(r.get("rule_over") or {}),
        "kuikae": bool(r.get("kuikae")), "open_riichi": bool(r.get("open_riichi")),
        "spectate": bool(r.get("spectate", True)), "abortive": abortive,
        "wall": wall, "ai_level": r.get("ai_level") or "normal",
        "lang": lang_of(t),
    }


def lang_of(t: dict) -> str:
    return (t["rules"].get("lang") or i18n.guild_lang(t["guild_id"]) or i18n.DEFAULT)


def ruleset_of(t: dict) -> "rulesets.RuleSet":
    return rulesets.resolve(room_config(t))


def compute_results(t: dict, gs) -> list[dict]:
    """用這場比賽的規則算一局的結果：只回真人，電腦不列。
    精算 ＝（最終點數 − 返點）÷ 1000 ＋ 順位馬（即 settlement.final_standings），同點由座位（起家起）決勝。"""
    cfg = room_config(t)
    g = copy.deepcopy(gs)               # final_standings 會把桌上的立直棒併進頭名，不要動到原本的對局
    rows = st.final_standings(g, start_points=cfg.get("start_points"), length=cfg["length"],
                              ruleset=cfg["ruleset"], rs=rulesets.resolve(cfg))
    by_seat = {p.seat: p for p in g.players}
    out = []
    for r in rows:
        p = by_seat[r.seat]
        if p.is_bot:
            continue
        out.append({"user_id": str(p.user_id), "username": p.username, "seat": int(r.seat),
                    "rank": int(r.rank), "final_points": int(r.score), "score": round(float(r.total_pt), 1)})
    return out


# ═══════════════════════════════════════════════════════════════
#  建立／修改／取消
# ═══════════════════════════════════════════════════════════════

def create(*, by, name: str, guild_id, channel_id, start_at: int, organizer_id=None,
           visibility: str = "public", join_mode: str = "open", max_entrants: int | None = None,
           require_checkin: bool = True, checkin_minutes: int = 30,
           rules: dict | None = None, format: dict | None = None) -> dict:
    """建立比賽（status＝draft，還不能報名；publish 後才開放）。回傳 get() 的內容。"""
    if by is not None and not can_create(by):
        raise TournamentError("forbidden", "沒有建立比賽的權限")
    org = str(organizer_id if organizer_id is not None else by or "")
    if not org:
        raise TournamentError("invalid", "要有主辦")
    name = (name or "").strip()
    if not name or len(name) > 80:
        raise TournamentError("invalid", "名稱要 1～80 字")
    if visibility not in ("public", "private") or join_mode not in ("open", "invite"):
        raise TournamentError("invalid", "visibility／join_mode 的值不對")
    if max_entrants is not None and int(max_entrants) < 1:
        raise TournamentError("invalid", "max_entrants 至少 1")
    tid = db.tour_create(
        name=name, guild_id=str(guild_id), channel_id=str(channel_id), organizer_id=org,
        start_at=int(start_at), status="draft", visibility=visibility, join_mode=join_mode,
        max_entrants=None if max_entrants is None else int(max_entrants),
        require_checkin=bool(require_checkin), checkin_minutes=max(0, int(checkin_minutes)),
        rules=_norm_rules(rules), format=_norm_format(format), state={})
    return get(tid)


_UPDATABLE = ("name", "channel_id", "start_at", "visibility", "join_mode", "max_entrants",
              "require_checkin", "checkin_minutes", "rules", "format", "organizer_id")


def update(tid: int, *, by, **fields) -> dict:
    """修改比賽設定（只有 draft／registration 能改）。rules／format 是合併進去（沒給的鍵不動）。
    organizer_id 只有管理員能改。"""
    t = _need(tid, by)
    if t["status"] not in EDITABLE:
        raise TournamentError("bad_status", "比賽開始後不能改設定")
    bad = set(fields) - set(_UPDATABLE)
    if bad:
        raise TournamentError("invalid", f"不能改的欄位：{', '.join(sorted(bad))}")
    if "organizer_id" in fields and by is not None and not is_admin(by):
        raise TournamentError("forbidden", "只有管理員能換主辦")
    new = {}
    for k, v in fields.items():
        if k == "rules":
            new[k] = _norm_rules({**t["rules"], **(v or {})})
        elif k == "format":
            new[k] = _norm_format({**t["format"], **(v or {})})
        elif k == "name":
            v = (v or "").strip()
            if not v or len(v) > 80:
                raise TournamentError("invalid", "名稱要 1～80 字")
            new[k] = v
        elif k == "visibility" and v not in ("public", "private"):
            raise TournamentError("invalid", "visibility 的值不對")
        elif k == "join_mode" and v not in ("open", "invite"):
            raise TournamentError("invalid", "join_mode 的值不對")
        elif k in ("start_at", "checkin_minutes"):
            new[k] = int(v)
        elif k == "max_entrants":
            new[k] = None if v is None else int(v)
        elif k in ("channel_id", "organizer_id"):
            new[k] = str(v)
        else:
            new[k] = v
        new.setdefault(k, v)
    db.tour_update(t["id"], **new)
    if t["status"] == "registration":
        _schedule_refresh(t["id"])
    return get(t["id"])


async def publish(tid: int, *, by) -> dict:
    """開放報名並在比賽頻道貼公告（draft → registration）。公告貼不出去不影響狀態（網站可以再呼叫 post_announce）。"""
    t = _need(tid, by)
    if t["status"] != "draft":
        raise TournamentError("bad_status", "只有草稿能發布")
    if t["start_at"] <= _now():
        raise TournamentError("invalid", "開賽時間已經過了")
    db.tour_update(t["id"], status="registration")
    await post_announce(t["id"])
    return get(t["id"])


async def cancel(tid: int, *, by) -> dict:
    """取消比賽：進行中的桌會被結束，公告改成已取消。"""
    t = _need(tid, by)
    if t["status"] in ("finished", "cancelled"):
        raise TournamentError("bad_status", "這場已經結束了")
    db.tour_update(t["id"], status="cancelled")
    for tb in db.tour_tbl_list(t["id"]):
        if tb["status"] == "running" and tb["gid"]:
            db.tour_tbl_set(t["id"], tb["stage"], tb["round_no"], tb["table_no"],
                            status="void", closed=True, finished_at=_now())
            await _end_game(tb["gid"])
        elif tb["status"] == "pending":
            db.tour_tbl_delete(t["id"], tb["stage"], tb["round_no"], tb["table_no"])
    await refresh_announce(t["id"])
    return get(t["id"])


# ═══════════════════════════════════════════════════════════════
#  報名／報到
# ═══════════════════════════════════════════════════════════════

def register(tid: int, user_id, username: str, source: str = "web") -> dict:
    """玩家自己報名（只有 registration 開放；邀請制 invite 不能自己報，要主辦用 add_entrant）。"""
    t = _get(tid)
    if t["status"] != "registration":
        raise TournamentError("closed", "現在不開放報名")
    if t["join_mode"] == "invite":
        raise TournamentError("invite_only", "這場是邀請制，只有主辦能加人")
    return _put_entrant(t, user_id, username, source if source in ("web", "discord") else "web")


def _put_entrant(t: dict, user_id, username: str, source: str) -> dict:
    ex = db.tour_ent_get(t["id"], str(user_id))
    if ex is not None and ex["status"] != "withdrawn":
        raise TournamentError("already", "已經報名了")
    db.tour_ent_put(t["id"], str(user_id), username or str(user_id), "registered", source, _now())
    _schedule_refresh(t["id"])
    return db.tour_ent_get(t["id"], str(user_id))


def unregister(tid: int, user_id) -> dict:
    """玩家取消報名（開賽前）。紀錄保留為 withdrawn。"""
    t = _get(tid)
    if t["status"] != "registration":
        raise TournamentError("closed", "現在不能取消報名")
    ex = db.tour_ent_get(t["id"], str(user_id))
    if ex is None or ex["status"] == "withdrawn":
        raise TournamentError("not_registered", "你還沒報名")
    db.tour_ent_set(t["id"], str(user_id), status="withdrawn")
    _schedule_refresh(t["id"])
    return db.tour_ent_get(t["id"], str(user_id))


def add_entrant(tid: int, user_id, username: str, *, by) -> dict:
    """主辦／管理員直接加人（不受 invite 限制；draft 也可以）。"""
    t = _need(tid, by)
    if t["status"] not in EDITABLE:
        raise TournamentError("closed", "比賽開始後不能加人")
    return _put_entrant(t, user_id, username, "manual")


def remove_entrant(tid: int, user_id, *, by) -> dict:
    t = _need(tid, by)
    if t["status"] not in EDITABLE:
        raise TournamentError("closed", "比賽開始後不能移除")
    ex = db.tour_ent_get(t["id"], str(user_id))
    if ex is None or ex["status"] == "withdrawn":
        raise TournamentError("not_registered", "這個人沒有報名")
    db.tour_ent_set(t["id"], str(user_id), status="withdrawn")
    _schedule_refresh(t["id"])
    return db.tour_ent_get(t["id"], str(user_id))


def checkin_window(t: dict) -> tuple[int, int]:
    """報到時段（開始, 結束）＝ 開賽前 checkin_minutes 分鐘 ～ 開賽時間。"""
    return t["start_at"] - t["checkin_minutes"] * 60, t["start_at"]


def checkin(tid: int, user_id) -> dict:
    t = _get(tid)
    if t["status"] != "registration":
        raise TournamentError("closed", "現在不能報到")
    if not t["require_checkin"]:
        raise TournamentError("not_required", "這場不用報到")
    ex = db.tour_ent_get(t["id"], str(user_id))
    if ex is None or ex["status"] == "withdrawn":
        raise TournamentError("not_registered", "你還沒報名")
    lo, hi = checkin_window(t)
    now = _now()
    if now < lo:
        raise TournamentError("checkin_early", "還沒開放報到")
    if now > hi:
        raise TournamentError("checkin_late", "報到已經截止")
    if ex["checked_in_at"] is None:
        db.tour_ent_set(t["id"], str(user_id), checked_in_at=now)
        _schedule_refresh(t["id"])
    return db.tour_ent_get(t["id"], str(user_id))


# ═══════════════════════════════════════════════════════════════
#  抽選
# ═══════════════════════════════════════════════════════════════

def _rng(seed):
    return random.Random(seed) if seed is not None else secrets.SystemRandom()


def draw(tid: int, *, by, seed=None, trim: bool = False, only_checked_in: bool = False) -> dict:
    """抽選：報名人數超過 max_entrants 就隨機選出 max_entrants 人（selected），其餘候補（waitlist，照抽出順序補位）；
    沒超過就全部 selected。trim＝再把人數削到一桌人數的整數倍（至少留一桌）。
    only_checked_in＝只從已報到的人裡選，沒報到的排在候補最後。
    開賽前可以重複抽（每次重新抽；同一個 seed 結果相同）。回傳 {"selected": [...], "waitlist": [...]}。"""
    t = _need(tid, by)
    if t["status"] not in EDITABLE:
        raise TournamentError("bad_status", "比賽開始後不能抽選")
    return _draw(t, seed, trim, only_checked_in)


def _draw(t: dict, seed=None, trim: bool = False, only_checked_in: bool = False) -> dict:
    ents = [e for e in db.tour_ent_list(t["id"]) if e["status"] != "withdrawn"]
    cands = [e for e in ents if e["checked_in_at"] is not None] if only_checked_in else list(ents)
    late = [e for e in ents if e not in cands]
    n, ts = len(cands), t["format"]["table_size"]
    cap = t["max_entrants"] or n
    if trim:
        cap = min(cap, max(ts, n // ts * ts))
    cap = min(cap, n)
    rng = _rng(seed)
    if cap >= n:
        chosen = list(cands)
        rng.shuffle(chosen)
        rest = []
    else:
        order = list(cands)
        rng.shuffle(order)
        chosen, rest = order[:cap], order[cap:]
    k = 0
    for e in chosen:
        db.tour_ent_set(t["id"], e["user_id"], status="selected", draw_order=k)
        k += 1
    for e in rest + late:
        db.tour_ent_set(t["id"], e["user_id"], status="waitlist", draw_order=k)
        k += 1
    _schedule_refresh(t["id"])
    return _lists(t["id"])


def _lists(tid: int) -> dict:
    ents = db.tour_ent_list(tid)
    key = lambda e: (e["draw_order"] if e["draw_order"] is not None else 1 << 30)
    return {"selected": sorted((e for e in ents if e["status"] == "selected"), key=key),
            "waitlist": sorted((e for e in ents if e["status"] == "waitlist"), key=key)}


def set_selection(tid: int, selected_ids: list, *, by) -> dict:
    """主辦手動指定參賽者（取代抽選）：名單內的人 selected，其餘（沒退出的）候補。"""
    t = _need(tid, by)
    if t["status"] not in EDITABLE:
        raise TournamentError("bad_status", "比賽開始後不能改名單")
    ids = [str(x) for x in selected_ids]
    ents = {e["user_id"]: e for e in db.tour_ent_list(t["id"]) if e["status"] != "withdrawn"}
    miss = [u for u in ids if u not in ents]
    if miss:
        raise TournamentError("not_entrant", f"不在報名名單裡：{', '.join(miss)}")
    if t["max_entrants"] and len(set(ids)) > t["max_entrants"]:
        raise TournamentError("full", "超過名額")
    k = 0
    for u in dict.fromkeys(ids):
        db.tour_ent_set(t["id"], u, status="selected", draw_order=k)
        k += 1
    for u in ents:
        if u not in ids:
            db.tour_ent_set(t["id"], u, status="waitlist", draw_order=k)
            k += 1
    _schedule_refresh(t["id"])
    return _lists(t["id"])


def _round1_pool(t: dict, apply: bool) -> list[str]:
    """第 1 輪實際會上場的真人：已選的人（要報到時只算有報到的），缺的由候補（有報到的優先照抽選順序）補上。
    apply＝把補位寫進名單（補上的 → selected，缺席被換掉的 → waitlist）；False＝只算不寫。"""
    ents = db.tour_ent_list(t["id"])
    need_ci = t["require_checkin"]
    if apply and any(e["status"] == "registered" for e in ents):    # 還沒抽過：現在抽
        _draw(t, only_checked_in=need_ci)
        ents = db.tour_ent_list(t["id"])
    key = lambda e: (e["draw_order"] if e["draw_order"] is not None else 1 << 30)
    sel = sorted((e for e in ents if e["status"] == "selected"), key=key)
    present = [e for e in sel if not need_ci or e["checked_in_at"] is not None]
    absent = [e for e in sel if e not in present]
    subs = sorted((e for e in ents if e["status"] == "waitlist"
                   and (not need_ci or e["checked_in_at"] is not None)), key=key)
    want = len(absent)
    if t["max_entrants"]:
        want += max(0, t["max_entrants"] - len(sel))
    used = subs[:want]
    if apply:
        last = max([key(e) for e in ents if e["draw_order"] is not None] + [0])
        for e in used:
            db.tour_ent_set(t["id"], e["user_id"], status="selected")
        for e in absent[:len(used)]:
            last += 1
            db.tour_ent_set(t["id"], e["user_id"], status="waitlist", draw_order=last)
    return [e["user_id"] for e in present + used]


# ═══════════════════════════════════════════════════════════════
#  分桌
# ═══════════════════════════════════════════════════════════════

def _sizes(n: int, ts: int) -> list[int]:
    k = math.ceil(n / ts)
    base, extra = divmod(n, k)
    return [base + 1] * extra + [base] * (k - extra)


def _pair_counts(tables: list[dict]) -> dict:
    cnt: dict = {}
    for tb in tables:
        hs = [u for u in tb["seats"] if u != "cpu"]
        for i in range(len(hs)):
            for j in range(i + 1, len(hs)):
                k = frozenset((hs[i], hs[j]))
                cnt[k] = cnt.get(k, 0) + 1
    return cnt


def _layout(humans: list[str], ts: int, rng, prev: dict | None = None, tries: int = 1) -> list[list[str]]:
    """把真人隨機分成幾桌（人數盡量平均，缺的座位補 "cpu"）；給了 prev（以前同桌次數）就多試幾次挑重複最少的。"""
    sizes = _sizes(len(humans), ts)
    best, best_cost = None, None
    for _ in range(max(1, tries)):
        order = list(humans)
        rng.shuffle(order)
        tables, i = [], 0
        for s in sizes:
            tables.append(order[i:i + s])
            i += s
        cost = 0
        if prev:
            for tb in tables:
                for a in range(len(tb)):
                    for b in range(a + 1, len(tb)):
                        cost += prev.get(frozenset((tb[a], tb[b])), 0)
        if best is None or cost < best_cost:
            best, best_cost = tables, cost
            if cost == 0:
                break
    out = []
    for tb in best:
        seats = tb + ["cpu"] * (ts - len(tb))
        rng.shuffle(seats)
        out.append(seats)
    return out


def seat_round(tid: int, stage: str, round_no: int, *, by, seed=None) -> list[dict]:
    """排這一輪的桌次（只存成 pending，還沒開打；開賽時 start_round 會用它，必要時重排）。
    預賽第 1 輪：已選的人（有要求報到＝有報到的）；第 2 輪起：第 1 輪上過場的人，且盡量少碰到上一輪以前的對手；
    決賽：預賽前 advance_n 名。回傳這一輪的桌（含 seats）。"""
    t = _need(tid, by)
    if t["status"] not in ("registration", "running"):
        raise TournamentError("bad_status", "現在不能排桌")
    return _seat(t, stage, round_no, seed, apply_pool=False)


def _round_tables(tid: int, stage: str, round_no: int) -> list[dict]:
    return [x for x in db.tour_tbl_list(tid) if x["stage"] == stage and x["round_no"] == round_no]


def _valid_round(t: dict, stage: str, round_no: int) -> None:
    f = t["format"]
    lim = f["prelim_rounds"] if stage == "prelim" else f["final_rounds"] if stage == "final" else 0
    if stage not in ("prelim", "final") or not (1 <= round_no <= lim):
        raise TournamentError("invalid", "沒有這一輪")


def _pool(t: dict, stage: str, round_no: int, apply_pool: bool) -> list[str]:
    if stage == "prelim" and round_no == 1:
        return _round1_pool(t, apply_pool)
    if stage == "prelim":
        first = _round_tables(t["id"], "prelim", 1)
        pool = [u for tb in first for u in tb["seats"] if u != "cpu"]
        if not pool:
            raise TournamentError("prev_incomplete", "第 1 輪還沒開始")
        return pool
    return [r["user_id"] for r in prelim_ranking(t["id"]) if r["games"]][:t["format"]["advance_n"]]


def _seat(t: dict, stage: str, round_no: int, seed, apply_pool: bool, pool: list | None = None) -> list[dict]:
    _valid_round(t, stage, round_no)
    tid = t["id"]
    exist = _round_tables(tid, stage, round_no)
    if any(x["status"] in ("running", "finished") for x in exist):
        raise TournamentError("already_started", "這一輪已經開始了")
    if pool is None:
        pool = _pool(t, stage, round_no, apply_pool)
    if not pool:
        raise TournamentError("no_players", "沒有人可以上場")
    rng = _rng(seed)
    prev = None
    tries = 1
    if stage == "prelim" and round_no >= 2:
        prev = _pair_counts([x for x in db.tour_tbl_list(tid) if x["stage"] == "prelim" and x["round_no"] < round_no])
        tries = SEAT_TRIES
    tables = _layout(pool, t["format"]["table_size"], rng, prev, tries)
    db.tour_tbl_delete(tid, stage, round_no)
    for i, seats in enumerate(tables, 1):
        db.tour_tbl_put(tid, stage, round_no, i, seats)
    return _round_tables(tid, stage, round_no)


# ═══════════════════════════════════════════════════════════════
#  開賽／開桌／排程
# ═══════════════════════════════════════════════════════════════

_locks: dict = {}


def _lock(tid: int) -> asyncio.Lock:
    return _locks.setdefault(tid, asyncio.Lock())


def _stage_order(t: dict) -> list[tuple]:
    f = t["format"]
    return ([("prelim", r) for r in range(1, f["prelim_rounds"] + 1)]
            + [("final", r) for r in range(1, f["final_rounds"] + 1)])


def _started(tables: list[dict]) -> list[tuple]:
    return sorted({(x["stage"], x["round_no"]) for x in tables if x["status"] != "pending"},
                  key=lambda k: (k[0] == "final", k[1]))


def _is_done(tb: dict) -> bool:
    return tb["status"] == "finished" or (tb["status"] == "void" and tb["closed"])


def _round_done(tables: list[dict], key: tuple) -> bool:
    rt = [x for x in tables if (x["stage"], x["round_no"]) == key]
    return bool(rt) and all(_is_done(x) for x in rt)


def _next_round(t: dict, tables: list[dict]):
    """下一個該開的 (stage, round_no)；全開完了＝None。"""
    done = _started(tables)
    for k in _stage_order(t):
        if k not in done:
            return k
    return None


async def start_round(tid: int, stage: str, round_no: int, *, by) -> dict:
    """手動開始一輪（把這一輪的桌開成真的對局）。上一輪沒打完不能開。"""
    t = _need(tid, by)
    return await _start_round(t, stage, round_no)


async def _start_round(t: dict, stage: str, round_no: int) -> dict:
    tid = t["id"]
    async with _lock(tid):
        t = _get(tid)
        if t["status"] not in ("registration", "running"):
            raise TournamentError("bad_status", "這場比賽不在可以開桌的狀態")
        _valid_round(t, stage, round_no)
        tables = db.tour_tbl_list(tid)
        order = _stage_order(t)
        idx = order.index((stage, round_no))
        if idx > 0 and not _round_done(tables, order[idx - 1]):
            raise TournamentError("prev_incomplete", "上一輪還沒打完")
        if any(x["status"] in ("running", "finished") for x in _round_tables(tid, stage, round_no)):
            raise TournamentError("already_started", "這一輪已經開始了")
        if (stage, round_no) == ("prelim", 1) and t["status"] == "registration":
            pool = _round1_pool(t, True)                       # 補位寫進名單
            cur = _round_tables(tid, stage, round_no)
            if not cur or {u for x in cur for u in x["seats"] if u != "cpu"} != set(pool):
                _seat(t, stage, round_no, None, apply_pool=True, pool=pool)
            db.tour_update(tid, status="running")
            t = _get(tid)
        elif not _round_tables(tid, stage, round_no):
            _seat(t, stage, round_no, None, apply_pool=True)
        tbls = [x for x in _round_tables(tid, stage, round_no) if x["status"] == "pending"]
        if not tbls:
            raise TournamentError("no_players", "沒有可以開的桌")
        await _announce_round_start(t, stage, round_no, len(tbls))
        launched = []
        for tb in tbls:
            if await _launch_table(t, tb):
                launched.append(tb["table_no"])
        await _announce_tables(t, stage, round_no)
        _schedule_refresh(tid)
    return get(tid)


async def _launch_table(t: dict, tb: dict) -> bool:
    """把一桌開成對局。成功回 True；失敗把這桌標成 void（主辦用 rerun_table 重開）。"""
    from . import flow
    tid, stage, rn, tn = t["id"], tb["stage"], tb["round_no"], tb["table_no"]
    ch = await _channel(t)
    cfg = room_config(t)
    gid = uuid.uuid4().hex[:8]
    names = {e["user_id"]: e["username"] for e in db.tour_ent_list(tid)}
    info = []
    for i, u in enumerate(tb["seats"]):
        if u == "cpu":
            info.append({"user_id": f"ai_{gid}_{i}", "username": flow.bot_name(cfg["ai_level"], cfg["lang"]),
                         "is_bot": True})
        else:
            info.append({"user_id": u, "username": names.get(u, u), "is_bot": False})
    cfg["tournament"] = {"tid": tid, "stage": stage, "round_no": rn, "table_no": tn}
    db.tour_tbl_set(tid, stage, rn, tn, gid=gid, status="running", started_at=_now(), closed=False)
    if ch is None:
        db.tour_tbl_set(tid, stage, rn, tn, status="void", finished_at=_now())
        return False
    _waiting[gid] = info
    _room_owners[gid] = t["organizer_id"]
    _room_configs[gid] = cfg
    try:
        rooms.register(gid, ch.guild.id, ch.id)
        await flow.launch_game(gid, ch)
    except Exception as e:
        print(f"[tournament] {tid} {stage}{rn}-{tn} 開桌失敗：{e!r}")
        if gid in _waiting or gid in _room_configs:
            try:
                flow._cleanup(gid, str(ch.id))
            except Exception:
                pass
        cur = db.tour_tbl_by_gid(gid)
        if cur and cur["status"] == "running":
            db.tour_tbl_set(tid, stage, rn, tn, status="void", finished_at=_now())
        return False
    if gid not in _games:           # launch_game 內部失敗（討論串建不起來）已經 _cleanup 並標 void
        return False
    db.tour_tbl_set(tid, stage, rn, tn,
                    seats=["cpu" if p.get("is_bot") else p["user_id"] for p in info])   # 洗牌後的實際座位（起家在前）
    return True


async def _end_game(gid: str) -> None:
    from . import flow
    if gid in _games or gid in _waiting:
        try:
            await flow.force_end(gid)
        except Exception as e:
            print(f"[tournament] 結束對局 {gid} 失敗：{e!r}")


async def void_table(tid: int, stage: str, round_no: int, table_no: int, *, by) -> dict:
    """作廢一桌且不重打：結果清掉、進行中的對局結束，這桌算已處理（這輪才能算打完）。"""
    t = _need(tid, by)
    tb = _find_table(t["id"], stage, round_no, table_no)
    db.tour_res_replace(t["id"], stage, round_no, table_no, [])
    db.tour_tbl_set(t["id"], stage, round_no, table_no, status="void", closed=True, finished_at=_now())
    if tb["status"] == "running" and tb["gid"]:
        await _end_game(tb["gid"])
    _schedule_refresh(t["id"])
    return get(t["id"])


async def rerun_table(tid: int, stage: str, round_no: int, table_no: int, *, by) -> dict:
    """這桌重打：舊結果清掉、進行中的對局結束，用同一批人馬上開新的一局。"""
    t = _need(tid, by)
    if t["status"] != "running":
        raise TournamentError("bad_status", "比賽沒有在進行")
    tb = _find_table(t["id"], stage, round_no, table_no)
    async with _lock(t["id"]):
        db.tour_res_replace(t["id"], stage, round_no, table_no, [])
        db.tour_tbl_set(t["id"], stage, round_no, table_no, status="void", closed=False)
        if tb["status"] == "running" and tb["gid"]:
            await _end_game(tb["gid"])
        db.tour_tbl_set(t["id"], stage, round_no, table_no, status="pending", gid=None,
                        closed=False, started_at=None, finished_at=None)
        tb = _find_table(t["id"], stage, round_no, table_no)
        await _launch_table(t, tb)
    _schedule_refresh(t["id"])
    return get(t["id"])


def _find_table(tid: int, stage: str, round_no: int, table_no: int) -> dict:
    for x in _round_tables(tid, stage, round_no):
        if x["table_no"] == table_no:
            return x
    raise TournamentError("not_found", "沒有這一桌")


# ─── 結果 ───

def on_game_end(gid: str, gs) -> bool:
    """對局打完時由 flow 呼叫：這局是比賽的桌就記錄結果。回傳是否記錄了。"""
    tb = db.tour_tbl_by_gid(gid)
    if tb is None or tb["status"] != "running":
        return False
    t = db.tour_get(tb["tid"])
    if t is None:
        return False
    _record(t, tb, gs)
    return True


def _record(t: dict, tb: dict, gs) -> None:
    rows = compute_results(t, gs)
    db.tour_res_replace(t["id"], tb["stage"], tb["round_no"], tb["table_no"], rows)
    db.tour_tbl_set(t["id"], tb["stage"], tb["round_no"], tb["table_no"],
                    status="finished", finished_at=_now(),
                    seats=["cpu" if p.is_bot else str(p.user_id) for p in sorted(gs.players, key=lambda p: p.seat)])
    _schedule_tick(t["id"])
    _schedule_refresh(t["id"])


def on_cleanup(gid: str) -> None:
    """對局被關掉時由 flow._cleanup 呼叫：這桌還在 running ＝中途被結束 → void（主辦決定 rerun 或 void_table）。"""
    tb = db.tour_tbl_by_gid(gid)
    if tb is not None and tb["status"] == "running":
        db.tour_tbl_set(tb["tid"], tb["stage"], tb["round_no"], tb["table_no"],
                        status="void", closed=False, finished_at=_now())
        _schedule_refresh(tb["tid"])


async def bind_game(tid: int, stage: str, round_no: int, table_no: int, gid: str, *, by) -> dict:
    """把玩家自己開的對局綁到比賽的一桌。
    - 對局裡的真人都要是參賽者、人數跟這場的一桌相同；同一輪每人只能有一桌結果。
    - 對局已經打完（資料庫裡有正常結算的戰績）＝馬上匯入結果；還在打＝這桌改成進行中，打完時自動記錄。
    - 用這場比賽的規則重算精算 pt（不是對局原本設的規則）。回傳 get() ＋ warnings（規則跟比賽不同時列出）。"""
    t = _need(tid, by)
    if t["status"] != "running":
        raise TournamentError("bad_status", "比賽沒有在進行")
    tb = _find_table(t["id"], stage, round_no, table_no)
    if tb["status"] in ("running", "finished"):
        raise TournamentError("bad_status", "這一桌已經在打或打完了；要換請先 rerun_table 或 void_table")
    other = db.tour_tbl_by_gid(gid)
    if other is not None and other["status"] in ("running", "finished"):
        raise TournamentError("bad_game", "這局已經綁在別桌了")
    live = _games.get(gid)
    row = db.get_game(gid)
    warnings = []
    if live is not None:
        gs, finished = live, False
        cfg = _room_configs.get(gid, {})
    elif row is not None:
        if row["state"] != "finished":
            raise TournamentError("bad_game", "這局不在進行中，也沒有打完")
        if not db.game_record_count(gid):
            raise TournamentError("bad_game", "這局不是正常打完的（被中途結束），沒有結算")
        from .engine import GameState
        gs, finished = GameState.from_dict(row["game_data"]), True
        try:
            cfg = json.loads(row.get("room_config") or "{}")
        except Exception:
            cfg = {}
    else:
        raise TournamentError("bad_game", "找不到這局")
    ts = t["format"]["table_size"]
    if len(gs.players) != ts:
        raise TournamentError("bad_game", f"這場比賽是 {ts} 人一桌")
    ents = {e["user_id"] for e in db.tour_ent_list(t["id"]) if e["status"] != "withdrawn"}
    humans = [str(p.user_id) for p in gs.players if not p.is_bot]
    if not humans:
        raise TournamentError("bad_game", "這局沒有真人")
    notin = [u for u in humans if u not in ents]
    if notin:
        raise TournamentError("not_entrant", f"不是參賽者：{', '.join(notin)}")
    played = {r["user_id"] for r in db.tour_res_list(t["id"])
              if r["stage"] == stage and r["round_no"] == round_no}
    dup = [u for u in humans if u in played]
    if dup:
        raise TournamentError("bad_game", f"這輪已經有結果的人：{', '.join(dup)}")
    mine = room_config(t)
    for k in ("rules", "length", "is_sanma"):
        if cfg and cfg.get(k) is not None and cfg.get(k) != mine[k]:
            warnings.append(f"{k}: {cfg.get(k)} (比賽規定 {mine[k]})")
    seats = ["cpu" if p.is_bot else str(p.user_id) for p in sorted(gs.players, key=lambda p: p.seat)]
    db.tour_tbl_set(t["id"], stage, round_no, table_no, gid=gid, seats=seats, status="running",
                    started_at=_now(), closed=False, finished_at=None)
    if finished:
        _record(t, _find_table(t["id"], stage, round_no, table_no), gs)
    _schedule_refresh(t["id"])
    out = get(t["id"])
    out["warnings"] = warnings
    return out


# ═══════════════════════════════════════════════════════════════
#  排名
# ═══════════════════════════════════════════════════════════════

def _rank_rows(tid: int, stage: str, players: list[str], names: dict, reg: dict) -> list[dict]:
    """某階段的累計排名。同分：單場最高 pt 較高 → 那一場的座位較前（起家起算）→ 報名較早。"""
    res = [r for r in db.tour_res_list(tid) if r["stage"] == stage]
    per: dict = {u: [] for u in players}
    for r in res:
        per.setdefault(r["user_id"], []).append(r)
    rows = []
    for u, games in per.items():
        games.sort(key=lambda r: (r["round_no"], r["table_no"]))
        total = round(sum(g["score"] for g in games), 1)
        best = max((g["score"] for g in games), default=None)
        best_seat = min((g["seat"] for g in games if g["score"] == best), default=99)
        rows.append({
            "user_id": u, "username": names.get(u, u), "total": total, "games": len(games),
            "best": best, "best_seat": best_seat if games else None,
            "scores": [{"round": g["round_no"], "table": g["table_no"], "score": g["score"],
                        "rank": g["rank"], "seat": g["seat"], "points": g["final_points"]} for g in games],
            "_k": (-total, -(best if best is not None else -1e9), best_seat, reg.get(u, 1 << 30)),
        })
    rows.sort(key=lambda r: r["_k"])
    for i, r in enumerate(rows, 1):
        r["rank"] = i
        r.pop("_k")
    return rows


def _participants(t: dict, ents: list[dict]) -> list[str]:
    """預賽的參賽者：排過桌的真人；還沒排桌＝已選的人（沒抽過＝全部報名者）。"""
    tabs = [x for x in db.tour_tbl_list(t["id"]) if x["stage"] == "prelim"]
    seen = list(dict.fromkeys(u for x in tabs for u in x["seats"] if u != "cpu"))
    if seen:
        return seen
    live = [e for e in ents if e["status"] != "withdrawn"]
    sel = [e for e in live if e["status"] == "selected"]
    return [e["user_id"] for e in (sel or live)]


def _ctx(tid: int):
    t = _get(tid)
    ents = db.tour_ent_list(tid)
    names = {e["user_id"]: e["username"] for e in ents}
    reg = {e["user_id"]: i for i, e in enumerate(ents)}
    return t, ents, names, reg


def prelim_ranking(tid: int) -> list[dict]:
    t, ents, names, reg = _ctx(tid)
    return _rank_rows(tid, "prelim", _participants(t, ents), names, reg)


def standings(tid: int) -> dict:
    """{"prelim": [...], "final": [...], "placings": [...]}。
    每列：rank／user_id／username／total（累計 pt）／games／scores（每場）／best。
    placings（比賽 finished 後才有）：決賽的人照決賽排名，其餘接在後面照預賽排名。"""
    t, ents, names, reg = _ctx(tid)
    pre = _rank_rows(tid, "prelim", _participants(t, ents), names, reg)
    ftab = [x for x in db.tour_tbl_list(tid) if x["stage"] == "final"]
    fplayers = list(dict.fromkeys(u for x in ftab for u in x["seats"] if u != "cpu"))
    fin = _rank_rows(tid, "final", fplayers, names, reg) if fplayers else []
    placings = []
    if t["status"] == "finished":
        order = [r["user_id"] for r in fin] + [r["user_id"] for r in pre if r["user_id"] not in {x["user_id"] for x in fin}]
        placings = [{"place": i, "user_id": u, "username": names.get(u, u)} for i, u in enumerate(order, 1)]
    return {"prelim": pre, "final": fin, "placings": placings}


# ═══════════════════════════════════════════════════════════════
#  查詢（給網站）
# ═══════════════════════════════════════════════════════════════

def _summary(t: dict, ents: list[dict]) -> dict:
    live = [e for e in ents if e["status"] != "withdrawn"]
    return {
        "id": t["id"], "name": t["name"], "guild_id": t["guild_id"], "channel_id": t["channel_id"],
        "organizer_id": t["organizer_id"], "start_at": t["start_at"], "status": t["status"],
        "visibility": t["visibility"], "join_mode": t["join_mode"], "max_entrants": t["max_entrants"],
        "require_checkin": t["require_checkin"], "checkin_minutes": t["checkin_minutes"],
        "rules": t["rules"], "format": t["format"], "created_at": t["created_at"],
        "checkin_opens_at": checkin_window(t)[0],
        "counts": {"entrants": len(live), "selected": sum(e["status"] == "selected" for e in live),
                   "waitlist": sum(e["status"] == "waitlist" for e in live),
                   "checked_in": sum(e["checked_in_at"] is not None for e in live)},
    }


def get(tid: int) -> dict:
    """完整資料：比賽設定＋報名名單（不含 withdrawn 以外的過濾，狀態都帶著）＋每桌＋排名。找不到丟 not_found。"""
    t, ents, names, _ = _ctx(int(tid))
    out = _summary(t, ents)
    out["entrants"] = [{k: e[k] for k in ("user_id", "username", "status", "checked_in_at", "source",
                                          "draw_order", "created_at")} for e in ents]
    res = db.tour_res_list(t["id"])
    tables = []
    for tb in db.tour_tbl_list(t["id"]):
        tables.append({
            "stage": tb["stage"], "round_no": tb["round_no"], "table_no": tb["table_no"], "gid": tb["gid"],
            "status": tb["status"], "closed": tb["closed"], "started_at": tb["started_at"],
            "finished_at": tb["finished_at"],
            "seats": [{"user_id": u, "username": ("電腦" if u == "cpu" else names.get(u, u)), "cpu": u == "cpu"}
                      for u in tb["seats"]],
            "results": [r for r in res if (r["stage"], r["round_no"], r["table_no"])
                        == (tb["stage"], tb["round_no"], tb["table_no"])],
        })
    out["tables"] = tables
    out["standings"] = standings(t["id"])
    return out


def list_all(status=None, guild_id=None, include_private: bool = False, viewer_id=None) -> list[dict]:
    """比賽列表（摘要，開賽時間新的在前）。草稿只有主辦／管理員看得到；private 的只有 include_private＝True、
    或 viewer_id 是主辦／管理員／參賽者才列（include_private＝True 等於不過濾，給後台用）。
    status 可以是單一狀態或狀態清單。"""
    sts = [status] if isinstance(status, str) else (list(status) if status else None)
    out = []
    v = None if viewer_id is None else str(viewer_id)
    for t in db.tour_list(sts, guild_id):
        ents = db.tour_ent_list(t["id"])
        if not include_private:
            boss = v is not None and (v == t["organizer_id"] or is_admin(v))
            if t["status"] == "draft" and not boss:
                continue
            if t["visibility"] == "private" and not (
                    boss or (v is not None and any(e["user_id"] == v and e["status"] != "withdrawn" for e in ents))):
                continue
        out.append(_summary(t, ents))
    return out


# ═══════════════════════════════════════════════════════════════
#  排程
# ═══════════════════════════════════════════════════════════════

_ticking: set = set()


async def tick(now: int | None = None) -> None:
    """排程器每次的工作：到時間的開第 1 輪、打完的輪次公告排名並（自動接續時）開下一輪、全部打完就結束比賽。"""
    now = _now() if now is None else int(now)
    for t in db.tour_list(["registration", "running"]):
        if t["id"] in _ticking:                  # 同一場已經有人在處理（排程器與「桌打完就檢查」同時到）
            continue
        _ticking.add(t["id"])
        try:
            await _tick_one(t["id"], now)
        except TournamentError as e:
            if e.code not in ("already_started", "prev_incomplete"):
                _note_error(t["id"], now, str(e))
        except Exception as e:
            print(f"[tournament] {t['id']} 排程錯誤：{e!r}")
            _note_error(t["id"], now, repr(e))
        finally:
            _ticking.discard(t["id"])


def _note_error(tid: int, now: int, msg: str) -> None:
    t = db.tour_get(tid)
    if t is not None:
        s = dict(t["state"])
        s["last_error"], s["retry_at"] = msg, now + RETRY_AFTER
        db.tour_update(tid, state=s)


async def _tick_one(tid: int, now: int) -> None:
    t = _get(tid)
    s = t["state"]
    if s.get("retry_at", 0) > now:
        return
    if t["status"] == "registration":
        if now >= t["start_at"]:
            await _start_round(t, "prelim", 1)
        return
    tables = db.tour_tbl_list(tid)
    await _reconcile(t, tables)
    tables = db.tour_tbl_list(tid)
    started = _started(tables)
    if not started:
        return
    last = started[-1]
    if not _round_done(tables, last):
        return
    key = f"{last[0]}:{last[1]}"
    nxt = _next_round(t, tables)
    done_at = max((x["finished_at"] or 0) for x in tables if (x["stage"], x["round_no"]) == last)
    gap = t["format"]["round_gap_minutes"] * 60
    if key not in s.get("announced", []):
        s = dict(s)
        s["announced"] = list(s.get("announced", [])) + [key]
        db.tour_update(tid, state=s)
        await _announce_round_done(t, last, nxt, done_at + gap)
    if nxt is None:
        await _finish(t)
        return
    if t["format"]["auto_advance"] and now >= done_at + gap:
        await _start_round(t, nxt[0], nxt[1])


async def _reconcile(t: dict, tables: list[dict]) -> None:
    """重啟前漏記的：進行中的桌，如果對局其實已經正常打完（資料庫有結算），補記結果。"""
    for tb in tables:
        if tb["status"] != "running" or not tb["gid"] or tb["gid"] in _games:
            continue
        row = db.get_game(tb["gid"])
        if row and row["state"] == "finished" and db.game_record_count(tb["gid"]):
            from .engine import GameState
            _record(t, tb, GameState.from_dict(row["game_data"]))


async def _finish(t: dict) -> None:
    db.tour_update(t["id"], status="finished")
    st_ = standings(t["id"])
    lang = lang_of(t)
    lines = [f"{p['place']}. {p['username']}" for p in st_["placings"][:t["format"]["advance_n"] or 4]]
    await _send(t, i18n.t("tour.finished", lang, name=t["name"], placings="\n".join(lines)))
    await refresh_announce(t["id"])


def _schedule_tick(tid: int) -> None:
    """有桌打完就盡快檢查一次（不用等下一次排程）。"""
    try:
        asyncio.get_running_loop().create_task(_tick_soon())
    except RuntimeError:
        pass


async def _tick_soon() -> None:
    await asyncio.sleep(1)
    await tick()


_started_loop = False


def start(bot) -> None:
    """機器人啟動時呼叫（run.on_ready）：掛上報名按鈕、啟動排程器、更新進行中的公告。只會啟動一次。"""
    global _started_loop
    if not getattr(bot, "_tour_items", False):
        try:
            bot.add_dynamic_items(TourButton)       # 常駐按鈕：重啟後仍然有效（custom_id 帶比賽 id）
            bot._tour_items = True
        except Exception as e:
            print(f"[tournament] 掛載報名按鈕失敗：{e!r}")
    if _started_loop:
        return
    _started_loop = True

    async def _loop():
        try:
            for t in db.tour_list(["registration", "running"]):
                await refresh_announce(t["id"])
        except Exception as e:
            print(f"[tournament] 啟動時更新公告失敗：{e!r}")
        while True:
            try:
                await tick()
            except Exception as e:
                print(f"[tournament] 排程失敗：{e!r}")
            await asyncio.sleep(TICK)

    asyncio.create_task(_loop())


# ═══════════════════════════════════════════════════════════════
#  Discord：公告／按鈕／訊息
# ═══════════════════════════════════════════════════════════════

def _bot():
    from . import flow
    return getattr(flow, "BOT", None)


async def _channel(t: dict):
    bot = _bot()
    if bot is None:
        return None
    cid = int(t["channel_id"])
    try:
        return bot.get_channel(cid) or await bot.fetch_channel(cid)
    except Exception:
        return None


async def _send(t: dict, content: str, view=None):
    ch = await _channel(t)
    if ch is None:
        return None
    try:
        kw = {"view": view} if view is not None else {}
        return await ch.send(content[:2000], **kw)
    except Exception as e:
        print(f"[tournament] {t['id']} 發訊息失敗：{e!r}")
        return None


def _tt(t: dict, key: str, **kw) -> str:
    return i18n.t(key, lang_of(t), **kw)


def web_url(tid: int) -> str:
    return f"{WEB_BASE_URL.rstrip('/')}/tournaments/{tid}" if WEB_BASE_URL else ""


def announce_text(t: dict) -> str:
    lang = lang_of(t)
    f, r = t["format"], t["rules"]
    sanma = f["table_size"] == 3
    cfg = room_config(t)
    rs = rulesets.resolve(cfg)
    ents = [e for e in db.tour_ent_list(t["id"]) if e["status"] != "withdrawn"]
    lines = [i18n.t("tour.title", lang, name=t["name"]),
             i18n.t("tour.line.time", lang, ts=t["start_at"]),
             i18n.t("tour.line.status", lang, status=i18n.t(f"tour.status.{t['status']}", lang))]
    comp = rulesets.compact(rs, lang, sanma=sanma, selected=r["rules"], start_points=r.get("start_points"),
                            hanchan=r["length"] == "hanchan", kuikae=cfg["kuikae"],
                            open_riichi=cfg["open_riichi"], spectate=cfg["spectate"])
    mode = i18n.t("mode.sanma" if sanma else "mode.yonma", lang)
    length = i18n.t(f"len.{r['length']}", lang)
    lines.append(i18n.t("tour.line.game", lang, game=f"{mode}・{length}", c1=comp[0]))
    lines.append(i18n.t("tour.line.pts", lang, c2=comp[1], c3=comp[2]))
    if f["final_rounds"]:
        fmt = i18n.t("tour.fmt", lang, pre=f["prelim_rounds"], adv=f["advance_n"], fin=f["final_rounds"], ts=f["table_size"])
    else:
        fmt = i18n.t("tour.fmt_nofinal", lang, pre=f["prelim_rounds"], ts=f["table_size"])
    lines.append(i18n.t("tour.line.format", lang, fmt=fmt))
    if t["max_entrants"]:
        lines.append(i18n.t("tour.line.entrants_max", lang, n=len(ents), max=t["max_entrants"]))
    else:
        lines.append(i18n.t("tour.line.entrants", lang, n=len(ents)))
    if t["join_mode"] == "invite":
        lines.append(i18n.t("tour.line.invite", lang))
    if t["status"] == "registration":
        if t["require_checkin"]:
            n_ci = sum(e["checked_in_at"] is not None for e in ents)
            lines.append(i18n.t("tour.line.checkin", lang, min=t["checkin_minutes"], n=n_ci))
        else:
            lines.append(i18n.t("tour.line.nocheckin", lang))
    return "\n".join(lines)


class TourButton(discord.ui.DynamicItem[discord.ui.Button],
                 template=r"tour:(?P<act>join|leave|checkin):(?P<tid>[0-9]+)"):
    """公告上的按鈕（報名／取消報名／報到）。custom_id 帶比賽 id，重啟後由 bot.add_dynamic_items 接手，不用重新掛 view。"""
    _STYLE = {"join": discord.ButtonStyle.success, "leave": discord.ButtonStyle.secondary,
              "checkin": discord.ButtonStyle.primary}

    def __init__(self, act: str, tid: int, lang: str = i18n.DEFAULT):
        super().__init__(discord.ui.Button(label=i18n.t(f"tour.btn.{act}", lang), style=self._STYLE[act],
                                           custom_id=f"tour:{act}:{int(tid)}"))
        self.act, self.tid = act, int(tid)

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["act"], int(match["tid"]))

    async def callback(self, interaction: discord.Interaction):
        await handle_button(interaction, self.act, self.tid)


def announce_view(t: dict):
    """公告的按鈕列：報名中＝報名／取消報名／（要報到才有）報到；其他狀態只剩網站連結。沒有按鈕＝None。"""
    lang = lang_of(t)
    v = discord.ui.View(timeout=None)
    if t["status"] == "registration":
        v.add_item(TourButton("join", t["id"], lang))
        v.add_item(TourButton("leave", t["id"], lang))
        if t["require_checkin"]:
            v.add_item(TourButton("checkin", t["id"], lang))
    url = web_url(t["id"]) if t["visibility"] == "public" else ""
    if url:
        v.add_item(discord.ui.Button(label=i18n.t("tour.btn.web", lang), style=discord.ButtonStyle.link, url=url))
    return v if v.children else None


async def post_announce(tid: int):
    """貼（或重貼）公告並記下訊息 id。"""
    t = _get(tid)
    msg = await _send(t, announce_text(t), announce_view(t))
    if msg is not None:
        db.tour_update(tid, announce_message_id=str(msg.id))
    return msg


async def refresh_announce(tid: int) -> None:
    """把公告改成最新內容（人數、狀態、按鈕）；訊息被刪了就重貼。"""
    t = db.tour_get(tid)
    if t is None or t["status"] == "draft":
        return
    if not t["announce_message_id"]:
        await post_announce(tid)
        return
    ch = await _channel(t)
    if ch is None:
        return
    try:
        msg = await ch.fetch_message(int(t["announce_message_id"]))
    except discord.NotFound:
        await post_announce(tid)
        return
    except Exception:
        return
    v = announce_view(t)
    try:
        await msg.edit(content=announce_text(t), view=v if v is not None else discord.utils.MISSING)
    except Exception as e:
        print(f"[tournament] {tid} 更新公告失敗：{e!r}")


_refresh_tasks: dict = {}


def _schedule_refresh(tid: int) -> None:
    """合併短時間內的多次更新（連續報名只改一次訊息）。沒有 event loop（純測試）就略過。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    old = _refresh_tasks.get(tid)
    if old is not None and not old.done():
        return

    async def _go():
        await asyncio.sleep(REFRESH_DELAY)
        await refresh_announce(tid)

    _refresh_tasks[tid] = loop.create_task(_go())


_ERR_KEYS = ("closed", "invite_only", "already", "not_registered", "checkin_early", "checkin_late",
             "not_required", "not_found")


async def handle_button(interaction: discord.Interaction, act: str, tid: int) -> None:
    uid = str(interaction.user.id)
    lang = i18n.get_user_lang(interaction.user.id, interaction.guild_id, interaction.channel)
    try:
        t = _get(tid)
        name = t["name"]
        if act == "join":
            register(tid, uid, interaction.user.display_name, "discord")
            msg = i18n.t("tour.ok.join", lang, name=name)
            if t["require_checkin"]:
                msg += "\n" + i18n.t("tour.ok.join_checkin", lang, min=t["checkin_minutes"], ts=t["start_at"])
        elif act == "leave":
            unregister(tid, uid)
            msg = i18n.t("tour.ok.leave", lang, name=name)
        else:
            checkin(tid, uid)
            msg = i18n.t("tour.ok.checkin", lang, name=name)
    except TournamentError as e:
        key = f"tour.err.{e.code}" if e.code in _ERR_KEYS else "tour.err.other"
        t = db.tour_get(tid)
        kw = {}
        if e.code == "checkin_early" and t:
            kw["ts"] = checkin_window(t)[0]
        msg = i18n.t(key, lang, **kw)
    try:
        await interaction.response.send_message(msg, ephemeral=True)
    except Exception as e:
        print(f"[tournament] 回覆按鈕失敗：{e!r}")


async def command_list(interaction: discord.Interaction) -> None:
    """/mahjong tournament：列出這個伺服器的比賽（報名中、進行中，以及最近結束的幾場）。"""
    lang = i18n.get_user_lang(interaction.user.id, interaction.guild_id, interaction.channel)
    if interaction.guild_id is None:
        await interaction.response.send_message(i18n.t("tour.cmd.guild_only", lang), ephemeral=True)
        return
    rows = list_all(["registration", "running"], guild_id=interaction.guild_id, viewer_id=interaction.user.id)
    rows += list_all("finished", guild_id=interaction.guild_id, viewer_id=interaction.user.id)[:3]
    if not rows:
        await interaction.response.send_message(i18n.t("tour.cmd.empty", lang), ephemeral=True)
        return
    lines = [i18n.t("tour.cmd.head", lang)]
    for r in rows:
        line = i18n.t("tour.cmd.row", lang, name=r["name"], status=i18n.t(f"tour.status.{r['status']}", lang),
                      ts=r["start_at"], n=r["counts"]["entrants"], ch=f"<#{r['channel_id']}>")
        url = web_url(r["id"]) if r["visibility"] == "public" else ""
        lines.append(line + (f"　{url}" if url else ""))
    await interaction.response.send_message("\n".join(lines)[:2000], ephemeral=True)


# ─── 輪次公告 ───

def _stage_name(t: dict, stage: str) -> str:
    return _tt(t, f"tour.stage.{stage}")


async def _announce_round_start(t: dict, stage: str, round_no: int, n_tables: int) -> None:
    await _send(t, _tt(t, "tour.round.start", name=t["name"], stage=_stage_name(t, stage), r=round_no, n=n_tables))


async def _announce_tables(t: dict, stage: str, round_no: int) -> None:
    """各桌的名單與牌桌連結（一則訊息放不下就分幾則）。"""
    from .state import _threads
    names = {e["user_id"]: e["username"] for e in db.tour_ent_list(t["id"])}
    cpu = _tt(t, "tour.cpu")
    chunks, cur = [], ""
    for tb in _round_tables(t["id"], stage, round_no):
        who = " ".join(cpu if u == "cpu" else f"<@{u}>" for u in tb["seats"])
        th = _threads.get(tb["gid"] or "", {}).get("public")
        link = th.mention if th is not None else ""
        line = _tt(t, "tour.table.line", n=tb["table_no"], players=who, link=link).rstrip()
        if len(cur) + len(line) + 1 > 1800:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur:
        chunks.append(cur)
    for c in chunks:
        await _send(t, c)


def _standing_lines(rows: list[dict], limit: int = 16) -> str:
    return "\n".join(f"{r['rank']}. {r['username']}　{r['total']:+.1f}" for r in rows[:limit])


async def _announce_round_done(t: dict, key: tuple, nxt, next_at: int) -> None:
    stage, rn = key
    rows = prelim_ranking(t["id"]) if stage == "prelim" else standings(t["id"])["final"]
    txt = _tt(t, "tour.round.done", stage=_stage_name(t, stage), r=rn, standings=_standing_lines(rows))
    if nxt is not None and t["format"]["auto_advance"]:
        txt += "\n" + _tt(t, "tour.round.next", stage=_stage_name(t, nxt[0]), r=nxt[1], ts=next_at)
    await _send(t, txt)
