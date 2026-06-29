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
"""replay.py — 由牌譜（game_log）重建可逐步瀏覽的回放。

牌譜記錄的是公開動作（摸打／吃碰槓／立直／和牌）與每局結算，沒有每位玩家每巡的
完整手牌；因此回放重現的是「各家牌河逐步堆疊 ＋ 動作敘述 ＋ 和牌牌型」。

frame（每一步）：{"hand": 局index, "turn": 巡index(-1=結算), "rivers": {seat:[牌字串]},
                 "action": 動作敘述文字}
"""
from __future__ import annotations

from . import i18n

DISCARD_KEYS = {"feed.discard", "feed.riichi_tsumogiri"}
CALL_TAKE    = {"feed.pon", "feed.chi", "feed.kan"}   # 從捨牌者牌河取走一張


def build_frames(events: list[dict], lang: str = i18n.DEFAULT):
    """回傳 (frames, seats, n)。seats: {seat: 名字}；n: 人數。"""
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

    frames = []
    hand, dcount = 0, 0
    rivers = {s: [] for s in seats}

    def snap(turn, action, result=None):
        frames.append({"hand": hand, "turn": turn, "action": action,
                       "rivers": {s: list(v) for s, v in rivers.items()},
                       "result": result})

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
            elif key in CALL_TAKE:
                s = name2seat.get(kw.get("loser"))
                if s is not None and rivers.get(s):
                    rivers[s].pop()        # 被鳴走 → 從牌河移除
            try:
                action = feed_text(key, lang, **kw)
            except Exception:
                action = key
            snap(max(0, (dcount - 1) // max(1, n)), action)
        elif t == "settle":
            win = e.get("win", "")
            winners = [uid2name.get(u, u) for u in (e.get("winners") or [])]
            if win in ("tsumo", "ron", "dblron", "nagashi"):
                txt = i18n.t("result.ron" if win in ("ron", "dblron") else "result.tsumo",
                             lang, name="、".join(winners) or "?",
                             loser=uid2name.get(e.get("loser"), "?"))
            else:
                txt = i18n.t("result.draw_title", lang)
            snap(-1, "🀄 " + txt, result=e)
            hand += 1
            dcount = 0
            rivers = {s: [] for s in seats}

    return frames, seats, n


def render_frame(frames: list[dict], idx: int, seats: dict, lang: str = i18n.DEFAULT) -> str:
    f = frames[idx]
    head = i18n.t("replay.hand", lang, n=f["hand"] + 1)
    if f["turn"] >= 0:
        head += "　" + i18n.t("replay.turn", lang, n=f["turn"] + 1)
    lines = [f"## 🎞️ {head}", ""]
    for s in sorted(seats):
        river = " ".join(f["rivers"].get(s, [])) or "—"
        lines.append(f"**{seats[s]}**")
        lines.append(river)
    lines.append("")
    lines.append(f"> {f['action']}")
    lines.append(f"`{i18n.t('replay.step', lang, i=idx + 1, total=len(frames))}`")
    return "\n".join(lines)


# ── 導覽：步／巡／局 的目標索引 ───────────────────────────────────────────────
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
                target = frames[j]["hand"]              # 跳到上一局的第一格
                while j > 0 and frames[j - 1]["hand"] == target:
                    j -= 1
                return j
        return 0
    # turn（巡）
    ck = _key(cur)
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
