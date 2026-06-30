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


def _ceremony(snap, win, e, wd, wh, uid2name, lang, d0):
    """和牌儀式：逐項揭曉役種，最後公布等級與點數（每位贏家一輪）。"""
    dora  = e.get("dora", []) or []
    ura   = e.get("ura", []) or []
    loser = uid2name.get(e.get("loser"), "?")
    for u, det in wd.items():
        name = uid2name.get(u, u)
        title = (i18n.t("result.ron", lang, name=name, loser=loser)
                 if win in ("ron", "dblron") else i18n.t("result.tsumo", lang, name=name))
        head = [f"🎉 {title}"]
        dline = "[" + i18n.t("board.dora", lang) + "：" + (" ".join(dora) or "—")
        if ura:
            dline += "　" + i18n.t("board.ura_dora", lang) + "：" + " ".join(ura)
        head.append(dline + "]")
        if wh.get(u):
            head.append(wh[u])
        snap("\n".join(head), d0)                                  # ① 標題＋寶牌＋手牌
        ym, yk = det.get("yakuman", []), det.get("yaku", [])
        items = [(n, None) for n in ym] if ym else [(n, h) for n, h in yk]
        shown = []
        for n, h in items:
            disp = i18n.yaku(n, lang)
            shown.append(f"・{disp}" if h is None else f"・{disp}　{h}飜")
            snap("\n".join(head + shown), 0.9)                    # ② 逐項揭曉
        pts = i18n.t("win.points", lang, n=det.get("points", 0))
        if ym:
            score = f"✨ {i18n.yaku(det.get('name', ''), lang)}　{pts}"
        else:
            nm = f"　{i18n.yaku(det.get('name', ''), lang)}" if det.get("name") else ""
            score = f"{i18n.t('win.han_fu', lang, han=det.get('han', 0), fu=det.get('fu', 0))}{nm}　{pts}"
        snap("\n".join(head + shown + ["", score]), 1.4)          # ③ 等級與點數


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
    hands  = {s: [] for s in seats}        # 各家當下手牌（由牌譜直接記錄）
    wall   = [0]                            # 牌山剩餘
    dora   = [[]]                           # 寶牌指示牌
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
                       "wall": wall[0], "dora": list(dora[0]),
                       "rivers": {s: list(v) for s, v in rivers.items()},
                       "melds":  {s: list(v) for s, v in melds.items()},
                       "hands":  {s: list(v) for s, v in hands.items()}})

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
            for k, v in (e.get("hands") or {}).items():   # 各家當下手牌
                hands[int(k)] = list(v)
            if "wall" in e:
                wall[0] = e["wall"]
            if e.get("dora"):
                dora[0] = e["dora"]
            try:
                action = feed_text(key, lang, **kw)
            except Exception:
                action = key
            snap(action, _delay(e.get("_ts"), 0.4, 8.0, 1.1))   # 依實際出牌間隔
        elif t == "settle":
            win = e.get("win", "")
            winners = "、".join(uid2name.get(u, u) for u in (e.get("winners") or []))
            wd = e.get("wd", {}) or {}
            wh = e.get("wh", {}) or {}
            turn = -1
            d0 = _delay(e.get("_ts"), 1.6, 8.0, 2.0)
            if wd and win in ("ron", "dblron", "tsumo"):
                _ceremony(snap, win, e, wd, wh, uid2name, lang, d0)   # 和牌儀式（逐項揭曉）
            else:
                if win in ("ron", "dblron"):
                    res = "🀄 " + i18n.t("result.ron", lang, name=winners or "?",
                                         loser=uid2name.get(e.get("loser"), "?"))
                elif win in ("tsumo", "nagashi"):
                    res = "🀄 " + i18n.t("result.tsumo", lang, name=winners or "?")
                else:
                    res = i18n.t("result.draw_title", lang)
                for u, hd in wh.items():
                    if hd:
                        res += f"\n　{uid2name.get(u, u)}：{hd}"
                snap(f"**{res}**", d0)
            hand += 1
            dcount, turn = 0, 0
            rivers = {s: [] for s in seats}
            melds  = {s: [] for s in seats}
            hands  = {s: [] for s in seats}
            dora[0] = []

    return frames, seats, n


def _sort_hand(tiles: list[str]) -> str:
    return " ".join(sorted(tiles, key=lambda t: t.replace("🔴", "")))


def _key(f):
    return (f["hand"], f["turn"] if f["turn"] >= 0 else 1 << 30)


def nav(frames: list[dict], idx: int, what: str, direction: int) -> int:
    """what: 'step' | 'turn' | 'hand'；direction: +1 / -1。回傳目標索引。"""
    n = len(frames)
    if what == "step":
        return max(0, min(n - 1, idx + direction))
    cur = frames[idx]
    if what == "hand":
        if direction > 0:
            for j in range(idx + 1, n):
                if frames[j]["hand"] > cur["hand"]:
                    return j
            return n - 1
        for j in range(idx - 1, -1, -1):
            if frames[j]["hand"] < cur["hand"]:
                target = frames[j]["hand"]
                while j > 0 and frames[j - 1]["hand"] == target:
                    j -= 1
                return j
        return 0
    ck = _key(cur)                                   # turn（巡）
    if direction > 0:
        for j in range(idx + 1, n):
            if _key(frames[j]) > ck:
                return j
        return n - 1
    for j in range(idx - 1, -1, -1):
        if _key(frames[j]) < ck:
            tk = _key(frames[j])
            while j > 0 and _key(frames[j - 1]) == tk:
                j -= 1
            return j
    return 0


def render_frame(frames: list[dict], idx: int, seats: dict,
                 lang: str = i18n.DEFAULT, focus: int = None) -> str:
    f = frames[idx]
    order = sorted(seats)
    if focus is None or focus not in seats:
        focus = order[0]
    head = i18n.t("replay.hand", lang, n=f["hand"] + 1)
    if f["turn"] >= 0:
        head += "　" + i18n.t("replay.turn", lang, n=f["turn"] + 1)
    if f.get("wall"):
        head += "　" + i18n.t("replay.wall", lang, n=f["wall"])
    lines = [f"## 🎞️ {head}"]
    if f.get("dora"):
        lines.append(f"{i18n.t('board.dora', lang)}：" + " ".join(f["dora"]))
    for j, s in enumerate(order):
        if j:
            lines.append("─" * 18)
        mark = "▶ " if s == focus else "　"
        meld = ("　" + "　".join(f["melds"].get(s, []))) if f["melds"].get(s) else ""
        lines.append(f"## {mark}{i18n.t('wind.%d' % s, lang)}「{seats[s]}」{meld}")
        lines.append(" ".join(f["rivers"].get(s, [])) or "—")
    # 視角玩家的手牌（像遊玩中只看自己那副）
    hd = f.get("hands", {}).get(focus, [])
    lines.append("=" * 22)
    lines.append(f"👁 {seats[focus]}　{i18n.t('panel.your_hand', lang)}")
    lines.append(("# " + _sort_hand(hd)) if hd else i18n.t("panel.none", lang))
    lines.append("")
    lines.append(f"> {f['action']}")
    lines.append(f"`{i18n.t('replay.step', lang, i=idx + 1, total=len(frames))}`")
    return "\n".join(lines)
