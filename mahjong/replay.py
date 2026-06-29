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
"""replay.py — 由牌譜（game_log）重建逐步畫格，供「像對局中」的重播。

牌譜記錄公開動作（摸打／吃碰槓／立直／和牌）與每局結算，沒有每位玩家每巡的完整手牌；
因此重播還原的是逐步演進的牌桌：各家牌河、副露、當下動作，依原順序一格一格播放。

build_frames(events) → (frames, seats, n)
  frame: {"hand", "turn"(-1=結算), "rivers":{seat:[牌]}, "melds":{seat:[標記]}, "action"}
render_frame(frames, idx, seats, lang) → 該格的牌桌文字（重播時逐格 edit 同一則訊息）。
"""
from __future__ import annotations
from datetime import datetime

from . import i18n

DISCARD_KEYS = {"feed.discard", "feed.riichi_tsumogiri"}
CALL_TAKE    = {"feed.pon", "feed.chi", "feed.kan"}                 # 從捨牌者牌河取走
CALL_TAG     = {"feed.pon": "碰", "feed.chi": "吃", "feed.kan": "槓",
                "feed.ankan": "暗槓", "feed.kakan": "加槓"}


def build_frames(events: list[dict], lang: str = i18n.DEFAULT):
    from .render import feed_text

    name2seat, seats, uid2name, n = {}, {}, {}, 4
    for e in events:
        if e.get("t") == "gamestart":
            pl = e.get("players", {}) or {}
            for uid, info in pl.items():
                seats[info["seat"]] = info["name"]
                name2seat[info["name"]] = info["seat"]
                uid2name[uid] = info["name"]
            n = len(pl) or 4
            break
    if not seats:
        return [], {}, 4

    frames = []
    hand, dcount, turn = 0, 0, 0
    rivers = {s: [] for s in seats}
    melds  = {s: [] for s in seats}
    prev_ts = [None]

    def _delay(cur_ts, lo, hi, default):
        if not prev_ts[0] or not cur_ts:
            d = default
        else:
            try:
                d = (datetime.fromisoformat(cur_ts) - datetime.fromisoformat(prev_ts[0])).total_seconds()
            except Exception:
                d = default
        prev_ts[0] = cur_ts or prev_ts[0]
        return round(max(lo, min(hi, d)), 2)

    def snap(action, delay):
        frames.append({"hand": hand, "turn": turn, "action": action, "delay": delay,
                       "rivers": {s: list(v) for s, v in rivers.items()},
                       "melds":  {s: list(v) for s, v in melds.items()}})

    for e in events:
        t = e.get("t")
        if t == "move":
            key = e.get("key", "")
            kw  = e.get("kw", {}) or {}
            if key in DISCARD_KEYS:
                s = name2seat.get(kw.get("name"))
                if s is not None:
                    rivers.setdefault(s, []).append(kw.get("tile", "🀫"))
                    dcount += 1
                    turn = (dcount - 1) // max(1, n)
            elif key in CALL_TAKE:
                s = name2seat.get(kw.get("loser"))
                if s is not None and rivers.get(s):
                    rivers[s].pop()
            if key in CALL_TAG:
                cs = name2seat.get(kw.get("name"))
                if cs is not None:
                    melds.setdefault(cs, []).append(f"{CALL_TAG[key]}{kw.get('tile', '')}")
            try:
                action = feed_text(key, lang, **kw)
            except Exception:
                action = key
            snap(action, _delay(e.get("_ts"), 0.4, 8.0, 1.1))   # 依實際出牌間隔
        elif t == "settle":
            win = e.get("win", "")
            winners = "、".join(uid2name.get(u, u) for u in (e.get("winners") or []))
            if win in ("ron", "dblron"):
                res = "🀄 " + i18n.t("result.ron", lang, name=winners or "?",
                                     loser=uid2name.get(e.get("loser"), "?"))
            elif win in ("tsumo", "nagashi"):
                res = "🀄 " + i18n.t("result.tsumo", lang, name=winners or "?")
            else:
                res = i18n.t("result.draw_title", lang)
            wh = e.get("wh", {}) or {}                     # 和了牌型（贏家）
            for u, hd in wh.items():
                if hd:
                    res += f"\n　{uid2name.get(u, u)}：{hd}"
            turn = -1
            snap(f"**{res}**", _delay(e.get("_ts"), 1.8, 8.0, 2.4))
            hand += 1
            dcount, turn = 0, 0
            rivers = {s: [] for s in seats}
            melds  = {s: [] for s in seats}

    return frames, seats, n


def render_frame(frames: list[dict], idx: int, seats: dict, lang: str = i18n.DEFAULT) -> str:
    f = frames[idx]
    head = i18n.t("replay.hand", lang, n=f["hand"] + 1)
    if f["turn"] >= 0:
        head += "　" + i18n.t("replay.turn", lang, n=f["turn"] + 1)
    lines = [f"## 🎞️ {head}", ""]
    for s in sorted(seats):
        meld = ("　" + "　".join(f["melds"].get(s, []))) if f["melds"].get(s) else ""
        lines.append(f"**{i18n.t('wind.%d' % s, lang)} {seats[s]}**{meld}")
        lines.append(" ".join(f["rivers"].get(s, [])) or "—")
    lines.append("")
    lines.append(f"> {f['action']}")
    lines.append(f"`{i18n.t('replay.step', lang, i=idx + 1, total=len(frames))}`")
    return "\n".join(lines)
