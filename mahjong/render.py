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
"""文字版面渲染：牌桌、私人手牌面板、牌河/點數/副露/動態文字（回傳字串，不做 I/O）。"""
from __future__ import annotations

from .config import WIND_LABELS
from .engine import GameState, PlayerState, Tile, Suit
from .state import _action_logs

# ═══════════════════════════════════════════════════════════════
#  Board embed
# ═══════════════════════════════════════════════════════════════

def make_board_text(gs: GameState, status: str = "", open_hand: bool = False) -> str:
    """牌局資訊（普通訊息版，用 # 標題放大牌面；牌與牌之間留空隔）
    open_hand=True 時公開每家手牌（觀戰模式）。"""
    mode = "三麻" if gs.is_sanma else "四麻"
    # 寶牌指示：已翻開的顯示牌面，未翻開的顯示牌背 🀫（需槓才翻）
    total_ind = len(gs.dora_indicators)
    shown     = [str(t) for t in gs.dora_indicators[:gs.revealed_dora]]
    hidden    = ["🀫"] * (total_ind - gs.revealed_dora)
    ind_str   = " ".join(shown + hidden)

    lines = [
        f"## 🀄 {gs.round_label}　{mode}",
        f"本場 {gs.honba} ・ 立直棒 {gs.riichi_sticks} ・ 牌山 {gs.tiles_left} 張",
        f"**寶牌指示**",
        f"# {ind_str}",
    ]
    for p in gs.players:
        wind     = WIND_LABELS[p.seat]
        cur      = "▶" if p.seat == gs.current_seat else "　"
        riichi   = "【立直】" if p.riichi else ""
        bot_mark = "🤖" if p.is_bot else ""
        kita     = f"　拔北×{p.kita}" if getattr(p, "kita", 0) else ""
        melds    = "　".join(str(m) for m in p.melds) if p.melds else "無"
        # 牌河：每張牌之間留空隔
        disc     = " ".join(str(t) for t in p.discards[-18:]) if p.discards else "—"
        lines.append(f"**{cur} {wind} {bot_mark}{p.username}{riichi}　（{p.score}點）**{kita}")
        lines.append(f"副露：{melds}")
        if open_hand:
            hand_tiles = sorted(p.hand, key=lambda t: (t.suit, t.value))
            drawn = f"　＋{p.drawn_tile}" if p.drawn_tile else ""
            hstr = " ".join(str(t) for t in hand_tiles) if hand_tiles else "—"
            lines.append(f"手牌（{len(p.hand)}）：{hstr}{drawn}")
        lines.append(f"🀫 牌河（{len(p.discards)}）")
        lines.append(f"## {disc}")
    if status:
        lines.append("")
        # 用 Discord 引言區塊（每行前綴 >）顯示狀態
        lines.append("\n".join(f"> {s}" for s in status.split("\n")))
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
#  0.2 討論串渲染
# ═══════════════════════════════════════════════════════════════

def make_thread_board(gs: GameState, status: str = "") -> str:
    """公開討論串的牌桌：玩家整行放大、副露只在有時顯示、牌河放大。"""
    mode      = "三麻" if gs.is_sanma else "四麻"
    total_ind = len(gs.dora_indicators)
    shown     = [str(t) for t in gs.dora_indicators[:gs.revealed_dora]]
    hidden    = ["🀫"] * (total_ind - gs.revealed_dora)
    ind_str   = " ".join(shown + hidden)

    lines = [
        f"## 🀄 {gs.round_label}　{mode}",
        f"本場 {gs.honba} ・ 立直棒 {gs.riichi_sticks} ・ 牌山 {gs.tiles_left} 張",
        f"# {ind_str}",
    ]
    for idx, p in enumerate(gs.players):
        if idx > 0:
            # 玩家之間：長分隔線 + 上下留白，明顯分開
            lines.append("")
            lines.append("━" * 35)
            lines.append("")
        wind     = WIND_LABELS[p.seat]
        cur      = "▶" if p.seat == gs.current_seat else "　"
        riichi   = "【立直】" if p.riichi else ""
        bot_mark = "🤖" if p.is_bot else ""
        kita     = f"　拔北×{p.kita}" if getattr(p, "kita", 0) else ""
        # 玩家整行放大、加引言 >、名字用「」標註
        lines.append(f"> ## {cur} {wind} {bot_mark}「{p.username}」{riichi}（{p.score}點）{kita}")
        # 副露只在有時顯示
        if p.melds:
            lines.append("副露：" + "　".join(str(m) for m in p.melds))
        disc = " ".join(str(t) for t in p.discards[-18:]) if p.discards else "—"
        lines.append(f"牌河（{len(p.discards)}）：")
        lines.append(f"## {disc}")
    if status:
        lines.append("")
        lines.append("\n".join(f"> {s}" for s in status.split("\n")))
    return "\n".join(lines)


def _last_discard_info(gs: GameState) -> str:
    """上家剛打出的牌（含打牌者）；牌單獨一行放大。"""
    if gs is not None and gs.pending_discard is not None and gs.pending_from_seat >= 0:
        who = gs.players[gs.pending_from_seat].username
        return f"🀫 上家：{WIND_LABELS[gs.pending_from_seat]} {who} 打出\n# {gs.pending_discard}"
    return ""


def _log_action(gid: str, text: str) -> None:
    """記錄一筆玩家動作（保留本局最近 40 筆，供「看動態」按鈕顯示完整記錄）。"""
    if not text:
        return
    log = _action_logs.setdefault(gid, [])
    log.append(text)
    del log[:-40]


def _action_feed(gid: str, gs: GameState) -> str:
    """私人面板上方的動態：上家剛打出的牌（放大）+ 最新一筆動作。
    完整動態請按「📜 看動態」按鈕。"""
    parts = []
    ld = _last_discard_info(gs)
    if ld:
        parts.append(ld)
    log = _action_logs.get(gid, [])
    if log:
        parts.append(f"📜 {log[-1]}")
    return "\n\n".join(parts)


def make_action_log_text(gid: str) -> str:
    """完整動態（給「看動態」按鈕的私密訊息用）。"""
    log = _action_logs.get(gid, [])
    if not log:
        return "📜 **本局動態**\n（尚無動作）"
    return "📜 **本局動態**\n" + "\n".join(f"> {t}" for t in log)


def _board_info(gs: GameState) -> str:
    """場況 + 寶牌指示（顯示在手牌上方；不另標「寶牌」字樣）。"""
    total_ind = len(gs.dora_indicators)
    shown  = [str(t) for t in gs.dora_indicators[:gs.revealed_dora]]
    hidden = ["🀫"] * (total_ind - gs.revealed_dora)
    ind    = " ".join(shown + hidden)
    return (f"# {gs.round_label}　本場 {gs.honba}　牌山 {gs.tiles_left}\n"
            f"# {ind}")


def dora_reveal_text(gs: GameState, is_riichi: bool = False) -> str:
    """和牌時揭曉寶牌指示牌（已翻開顯示牌面、未翻開顯示 🀫）；立直才加裏寶牌指示。"""
    total = len(gs.dora_indicators)
    rev   = gs.revealed_dora
    dora  = [str(t) for t in gs.dora_indicators[:rev]] + ["🀫"] * (total - rev)
    parts = [f"[寶牌：{' '.join(dora)}]"]
    if is_riichi:
        ura_face = [str(t) for t in gs.ura_indicators[:rev]]
        ura      = ura_face + ["🀫"] * (total - len(ura_face))
        parts.append(f"[裏寶牌：{' '.join(ura)}]")
    return "　".join(parts)   # 寶牌與裏寶牌並排


def make_hand_panel(player: PlayerState, prompt: str = "", tenpai_note: str = "",
                    last_info: str = "", board_info: str = "") -> str:
    """私人討論串的個人面板：場況/寶牌、手牌｜剛摸到的牌、副露、提示。"""
    uni, names = player.hand_display_with_names()
    sep = "=" * 35
    lines = []
    if board_info:
        lines.append("\n".join("> " + ln for ln in board_info.split("\n")))
        lines.append(sep)
    if last_info:
        lines.append(last_info)
        lines.append(sep)
    lines.append("# 你的手牌" + ("　【已立直】" if player.riichi else ""))
    # 手牌 ｜ 剛摸到的牌（| 右邊為剛摸到）
    if player.drawn_tile:
        lines.append(f"# {uni} | {player.drawn_tile}" if uni else f"# | {player.drawn_tile}")
    else:
        lines.append(f"# {uni}" if uni else "（無）")
    if names:
        lines.append(names)
    # 副露（碰/吃/槓後）換行顯示
    if player.melds:
        lines.append("副露：" + "　".join(str(m) for m in player.melds))
    if tenpai_note:
        lines.append("")
        lines.append(tenpai_note)
    if prompt:
        lines.append("")
        lines.append(prompt)
    return "\n".join(lines)


def make_river_text(gs: GameState) -> str:
    """各家牌河（給「看牌河」按鈕的私密訊息用）。"""
    lines = ["**🀫 各家牌河**"]
    for idx, p in enumerate(gs.players):
        if idx > 0:
            lines.append("─" * 24)   # 玩家之間分隔線
        disc = " ".join(str(t) for t in p.discards) if p.discards else "（無）"
        lines.append(f"{WIND_LABELS[p.seat]} {p.username}（{len(p.discards)}）")
        lines.append(f"# {disc}")
    lines.append("")
    lines.append("＊這是當下快照；關閉後再按一次可看最新。")
    return "\n".join(lines)


def result_body(header: str, hand_str: str, result, log, gs: GameState,
                tenpai=None) -> str:
    """一局結果的靜態文字（送到聊天串與各私人討論串）。"""
    if result is None:
        tnames = "、".join(gs.players[s].username for s in (tenpai or [])) or "無人"
        return f"# 🀄 流局\n聽牌：{tnames}\n\n{log.describe(gs)}"
    top = f"# 🎉 {header}\n## {hand_str}"
    if result.yakuman:
        yaku = "\n".join(f"・**{n}**" for n, _ in result.yakuman)
        score_line = f"## ✨ {result.name}　{result.points} 點"
    else:
        yaku = "\n".join(f"・{n} {h}飜" for n, h in result.yaku)
        nm = f"　{result.name}" if result.name else ""
        score_line = f"## {result.han} 飜 {result.fu} 符{nm}　{result.points} 點"
    return top + "\n" + yaku + f"\n\n{score_line}\n\n" + log.describe(gs)


def make_score_text(gs: GameState) -> str:
    """各家點數與目前順位（依點數排序）。"""
    medals = ["🥇", "🥈", "🥉", "4️⃣"]
    ranked = sorted(gs.players, key=lambda p: -p.score)
    lines = ["**📊 點數 / 順位**"]
    for i, p in enumerate(ranked):
        bot = "🤖" if p.is_bot else ""
        lines.append(f"{medals[i]} {WIND_LABELS[p.seat]}　{bot}{p.username}：**{p.score}** 點")
    return "\n".join(lines)


def make_meld_text(gs: GameState) -> str:
    """各家副露＋風位（給「看副露」按鈕用）。"""
    lines = ["**🀜 各家副露**"]
    any_meld = False
    for p in gs.players:
        if p.melds:
            any_meld = True
            melds = "　".join(str(m) for m in p.melds)
            lines.append(f"{WIND_LABELS[p.seat]}　{p.username}：{melds}")
    if not any_meld:
        lines.append("（目前無人副露）")
    return "\n".join(lines)
