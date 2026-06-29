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
"""replay.py — 由牌譜（game_log）重建整場對局的完整文字稿，用於回放討論串。

牌譜記錄公開動作（摸打／吃碰槓／立直／和牌）與每局結算，沒有每位玩家每巡的完整手牌；
因此回放還原的是逐局／逐巡的「動作敘述 ＋ 各家牌河 ＋ 和牌結果」。

build_transcript(events) → 一串可直接逐則貼進討論串的訊息（每則 ≤ ~1900 字）。
"""
from __future__ import annotations

from . import i18n

DISCARD_KEYS = {"feed.discard", "feed.riichi_tsumogiri"}
CALL_TAKE    = {"feed.pon", "feed.chi", "feed.kan"}   # 從捨牌者牌河取走一張


def _chunk(lines: list[str], limit: int = 1900) -> list[str]:
    msgs, buf = [], ""
    for ln in lines:
        ln = ln[:limit]
        if buf and len(buf) + len(ln) + 1 > limit:
            msgs.append(buf)
            buf = ln
        else:
            buf = (buf + "\n" + ln) if buf else ln
    if buf:
        msgs.append(buf)
    return msgs


def build_transcript(events: list[dict], lang: str = i18n.DEFAULT) -> list[str]:
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
        return []

    roster = "　".join(f"{i18n.t('wind.%d' % s, lang)} {seats[s]}" for s in sorted(seats))
    lines = [f"# 🎞️ {i18n.t('replay.head', lang)}", roster, ""]

    hand, dcount, turn = 0, 0, 0
    rivers = {s: [] for s in seats}
    need_header = True

    for e in events:
        t = e.get("t")
        if t == "move":
            if need_header:
                lines.append(f"## 🀄 {i18n.t('replay.hand', lang, n=hand + 1)}")
                need_header = False
            key = e.get("key", "")
            kw  = e.get("kw", {}) or {}
            if key in DISCARD_KEYS:
                s = name2seat.get(kw.get("name"))
                if s is not None:
                    rivers.setdefault(s, []).append(kw.get("tile", "🀫"))
                    dcount += 1
                    nt = (dcount - 1) // max(1, n)
                    if nt > turn:
                        turn = nt
                        lines.append(f"　— {i18n.t('replay.turn', lang, n=turn + 1)} —")
            elif key in CALL_TAKE:
                s = name2seat.get(kw.get("loser"))
                if s is not None and rivers.get(s):
                    rivers[s].pop()
            try:
                action = feed_text(key, lang, **kw)
            except Exception:
                action = key
            lines.append(f"> {action}")
        elif t == "settle":
            win = e.get("win", "")
            winners = "、".join(uid2name.get(u, u) for u in (e.get("winners") or []))
            if win in ("ron", "dblron"):
                res = "🀄 " + i18n.t("result.ron", lang, name=winners or "?",
                                     loser=uid2name.get(e.get("loser"), "?"))
            elif win in ("tsumo", "nagashi"):
                res = "🀄 " + i18n.t("result.tsumo", lang, name=winners or "?")
            else:
                res = i18n.t("result.draw_title", lang)   # 本身已含 🀄
            lines.append(f"**{res}**")
            for s in sorted(seats):                    # 本局終了牌河
                lines.append(f"{seats[s]}　{i18n.t('panel.river', lang)}："
                             + (" ".join(rivers[s]) or "—"))
            lines.append("")
            hand += 1
            dcount, turn = 0, 0
            rivers = {s: [] for s in seats}
            need_header = True

    return _chunk(lines)
