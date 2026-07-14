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
from .i18n import t, DEFAULT
from . import tiles as T


def _wind(seat: int, lang: str) -> str:
    return t(f"wind.{seat}", lang)


def _round(gs: GameState, lang: str) -> str:
    """局名（如 東1局），依語言。"""
    return t("board.round", lang, wind=t(f"wind.{gs.round_wind - 1}", lang), num=gs.round_num)


def _mode(gs: GameState, lang: str) -> str:
    return t("mode.sanma" if gs.is_sanma else "mode.yonma", lang)

# ═══════════════════════════════════════════════════════════════
#  Board embed
# ═══════════════════════════════════════════════════════════════

def make_board_text(gs: GameState, status: str = "", open_hand: bool = False,
                    lang: str = DEFAULT) -> str:
    """牌局資訊（普通訊息版，用 # 標題放大牌面；牌與牌之間留空隔）
    open_hand=True 時公開每家手牌（觀戰模式）。"""
    # 寶牌指示：已翻開的顯示牌面，未翻開的顯示牌背 🀫（需槓才翻）
    total_ind = len(gs.dora_indicators)
    shown     = [T.of(tt) for tt in gs.dora_indicators[:gs.revealed_dora]]
    hidden    = [T.back()] * (total_ind - gs.revealed_dora)
    ind_str   = "".join(shown + hidden)

    lines = [
        f"## 🀄 {_round(gs, lang)}　{_mode(gs, lang)}",
        t("board.summary", lang, honba=gs.honba, sticks=gs.riichi_sticks, wall=gs.tiles_left),
        f"# {ind_str}",
    ]
    for p in gs.players:
        wind     = _wind(p.seat, lang)
        cur      = "▶" if p.seat == gs.current_seat else "　"
        riichi   = t("label.riichi_tag", lang) if p.riichi else ""
        bot_mark = "🤖" if p.is_bot else ""
        kita     = "　" + t("label.kita", lang, n=p.kita) if getattr(p, "kita", 0) else ""
        melds    = "　".join(T.meld(m) for m in p.melds) if p.melds else t("panel.none", lang)
        # 牌河：每張牌之間留空隔
        disc     = ("…" if len(p.discards) > 12 else "") + "".join(T.of(tt) for tt in p.discards[-12:]) if p.discards else "—"
        lines.append(f"**{cur} {wind} {bot_mark}{p.username}{riichi}　（{p.score}）**{kita}")
        lines.append(f"{t('panel.melds', lang)}：{melds}")
        if open_hand:
            hand_tiles = sorted(p.hand, key=lambda x: (x.suit, x.value))
            drawn = f"　＋{T.of(p.drawn_tile)}" if p.drawn_tile else ""
            hstr = "".join(T.of(tt) for tt in hand_tiles) if hand_tiles else "—"
            lines.append(f"{t('panel.your_hand', lang)}（{len(p.hand)}）：{hstr}{drawn}")
        lines.append(f"🀫 {t('panel.river', lang)}（{len(p.discards)}）")
        lines.append(f"## {disc}")
    if status:
        lines.append("")
        # 用 Discord 引言區塊（每行前綴 >）顯示狀態
        lines.append("\n".join(f"> {s}" for s in status.split("\n")))
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
#  0.2 討論串渲染
# ═══════════════════════════════════════════════════════════════

def make_thread_board(gs: GameState, status: str = "", lang: str = DEFAULT) -> str:
    """公開討論串的牌桌：玩家整行放大、副露只在有時顯示、牌河放大。"""
    total_ind = len(gs.dora_indicators)
    shown     = [T.of(tt) for tt in gs.dora_indicators[:gs.revealed_dora]]
    hidden    = [T.back()] * (total_ind - gs.revealed_dora)
    ind_str   = "".join(shown + hidden)

    lines = [
        f"## 🀄 {_round(gs, lang)}　{_mode(gs, lang)}",
        t("board.summary", lang, honba=gs.honba, sticks=gs.riichi_sticks, wall=gs.tiles_left),
        f"# {ind_str}",
    ]
    for idx, p in enumerate(gs.players):
        if idx > 0:
            # 玩家之間：長分隔線 + 上下留白，明顯分開
            lines.append("")
            lines.append("━" * 35)
            lines.append("")
        wind     = _wind(p.seat, lang)
        cur      = "▶" if p.seat == gs.current_seat else "　"
        riichi   = t("label.riichi_tag", lang) if p.riichi else ""
        bot_mark = "🤖" if p.is_bot else ""
        kita     = "　" + t("label.kita", lang, n=p.kita) if getattr(p, "kita", 0) else ""
        # 玩家整行放大、加引言 >、名字用「」標註
        if p.riichi and getattr(p, "open_riichi", False):
            riichi = t("label.open_riichi_tag", lang)
        lines.append(f"> ## {cur} {wind} {bot_mark}「{p.username}」{riichi}（{p.score}）{kita}")
        if p.riichi and getattr(p, "open_riichi", False):   # 開立直：亮出手牌
            lines.append(t("label.open_hand", lang) + "：" +
                         T.render(sorted(p.hand, key=lambda x: (x.suit, x.value))))
        # 副露只在有時顯示
        if p.melds:
            lines.append(t("panel.melds", lang) + "：" + "　".join(T.meld(m) for m in p.melds))
        disc = ("…" if len(p.discards) > 12 else "") + "".join(T.of(tt) for tt in p.discards[-12:]) if p.discards else "—"
        lines.append(f"{t('panel.river', lang)}（{len(p.discards)}）：")
        lines.append(f"## {disc}")
    if status:
        lines.append("")
        lines.append("\n".join(f"> {s}" for s in status.split("\n")))
    return "\n".join(lines)


def _last_discard_info(gs: GameState, lang: str = DEFAULT) -> str:
    """上家剛打出的牌（含打牌者）；牌單獨一行放大。"""
    if gs is not None and gs.pending_discard is not None and gs.pending_from_seat >= 0:
        who = gs.players[gs.pending_from_seat].username
        head = t("feed.last_discard", lang, wind=_wind(gs.pending_from_seat, lang), who=who)
        # 表情牌可與文字同行，直接接在後面並整行放大
        return f"## {head}　{T.of(gs.pending_discard)}"
    return ""


_FEED_TRANS_PREFIX = ("action.", "feed.", "wind.", "term.")


def _log_action(gid: str, key: str, **kw) -> None:
    """記錄一筆玩家動作為「翻譯鍵 + 參數」（保留本局最近 40 筆，供逐人語言渲染）。"""
    if not key:
        return
    log = _action_logs.setdefault(gid, [])
    log.append((key, kw))
    del log[:-40]


def feed_text(key: str, lang: str = DEFAULT, **kw) -> str:
    """把一筆動態（key + 參數）依語言組成文字；參數值若本身是翻譯鍵也一併翻譯，
    純牌面字串（牌譜裡存的 unicode 牌）則換成自訂表情。"""
    kw2 = {}
    for f, v in kw.items():
        if isinstance(v, str):
            if v.startswith(_FEED_TRANS_PREFIX):
                v = t(v, lang)
            elif T.is_tiles(v):
                v = T.emojify(v)
        kw2[f] = v
    return t(key, lang, **kw2)


def _action_feed(gid: str, gs: GameState, lang: str = DEFAULT) -> str:
    """私人面板上方的動態：上家剛打出的牌（放大）+ 最新一筆動作（依玩家語言）。"""
    parts = []
    ld = _last_discard_info(gs, lang)
    if ld:
        parts.append(ld)
    log = _action_logs.get(gid, [])
    if log:
        key, kw = log[-1]
        parts.append(f"## 📜 {feed_text(key, lang, **kw)}")
    return "\n\n".join(parts)


def make_action_log_text(gid: str, lang: str = DEFAULT) -> str:
    """完整動態（給「看動態」按鈕的私密訊息用，依語言）。"""
    title = t("actionlog.title", lang)
    log = _action_logs.get(gid, [])
    if not log:
        return f"{title}\n{t('actionlog.empty', lang)}"
    return f"{title}\n" + "\n".join(f"> {feed_text(k, lang, **kw)}" for k, kw in log)


def _board_info(gs: GameState, lang: str = DEFAULT) -> str:
    """場況（含本場／立直棒／牌山）+ 寶牌指示（不另標「寶牌」字樣）。"""
    total_ind = len(gs.dora_indicators)
    shown  = [T.of(tt) for tt in gs.dora_indicators[:gs.revealed_dora]]
    hidden = [T.back()] * (total_ind - gs.revealed_dora)
    ind    = "".join(shown + hidden)
    summary = t("board.summary", lang, honba=gs.honba, sticks=gs.riichi_sticks, wall=gs.tiles_left)
    return f"# {_round(gs, lang)}　{summary}\n# {ind}"


def dora_reveal_text(gs: GameState, is_riichi: bool = False, lang: str = DEFAULT) -> str:
    """和牌時揭曉寶牌指示牌（已翻開顯示牌面、未翻開顯示 🀫）；立直才加裏寶牌指示。"""
    total = len(gs.dora_indicators)
    rev   = gs.revealed_dora
    dora  = [T.of(tt) for tt in gs.dora_indicators[:rev]] + [T.back()] * (total - rev)
    parts = [f"[{''.join(dora)}]"]   # 不再標「寶牌」字樣，直接顯示
    if is_riichi:
        ura_face = [T.of(tt) for tt in gs.ura_indicators[:rev]]
        ura      = ura_face + [T.back()] * (total - len(ura_face))
        parts.append(f"[{''.join(ura)}]")
    return "　".join(parts)   # 寶牌與裏寶牌並排


def river_panel(gs: GameState, lang: str = DEFAULT, cap: int | None = None) -> str:
    """各家牌河（獨立的牌河訊息用）。cap=None 顯示完整牌河，否則各家只留最近 cap 張。"""
    lines = [f"🀫 {t('panel.river', lang)}"]
    for idx, p in enumerate(gs.players):
        if idx > 0:
            lines.append("─" * 20)
        cur    = "▶ " if p.seat == gs.current_seat else ""
        opened = p.riichi and getattr(p, "open_riichi", False)
        riichi = (t("label.open_riichi_tag" if opened else "label.riichi_tag", lang)
                  if p.riichi else "")
        shown  = p.discards if cap is None else p.discards[-cap:]
        trim   = "…" if len(shown) < len(p.discards) else ""
        disc   = trim + "".join(T.of(tt) for tt in shown) if p.discards else "—"
        lines.append(f"## {cur}{_wind(p.seat, lang)}「{p.username}」{riichi}（{len(p.discards)}）")
        if p.melds:   # 副露自成一行（名字下、牌河上）
            lines.append(t("panel.melds", lang) + "：" + "　".join(T.meld(m) for m in p.melds))
        if opened:   # 開立直：亮出手牌（不標待牌）
            lines.append(t("label.open_hand", lang) + "：" +
                         T.render(sorted(p.hand, key=lambda x: (x.suit, x.value))))
        lines.append(f"# {disc}")
    return "\n".join(lines)


def river_message_text(gs: GameState, lang: str = DEFAULT) -> str:
    """牌河訊息（面板上方獨立的一則訊息）：最上方是場況（局／本場／立直棒／牌山＋寶牌），
    下面接完整牌河。優先完整顯示；表情牌每張近 30 字元，超過 Discord 2000 字上限時
    逐步只留各家最近 N 張。"""
    head = "\n".join("> " + ln for ln in _board_info(gs, lang).split("\n")) + "\n" + "=" * 35 + "\n"
    txt = head + river_panel(gs, lang, None)
    for cap in (18, 15, 12, 10, 8, 6):
        if len(txt) <= 1950:
            return txt
        txt = head + river_panel(gs, lang, cap)
    return txt


def make_hand_panel(player: PlayerState, prompt: str = "", tenpai_note: str = "",
                    last_info: str = "", board_info: str = "", river_info: str = "",
                    lang: str = DEFAULT) -> str:
    """私人討論串的個人面板：牌河、場況/寶牌、手牌｜剛摸到的牌、副露、提示。"""
    uni, names = player.hand_display_with_names()
    hand_e = T.render(sorted(player.hand, key=lambda x: (x.suit, x.value)))  # 手牌用自訂表情
    sep = "=" * 35
    lines = []
    if board_info:   # 場況／寶牌移到最上面
        lines.append("\n".join("> " + ln for ln in board_info.split("\n")))
        lines.append(sep)
    if river_info:
        lines.append(river_info)
        lines.append(sep)
    if last_info:
        lines.append(last_info)
        lines.append(sep)
    if player.riichi:   # 去掉「你的手牌」標題，立直時仍標註
        lines.append(f"# {t('label.riichi_done', lang)}")
    warn = getattr(player, "warn_tags", None)   # 振聽／無役警示（同【已立直】的顯眼標註）
    if warn:
        rendered = []
        for w in warn:
            if isinstance(w, (tuple, list)):   # (key, kw)：參數若是牌字串就轉表情
                k, kw = w
                kw2 = {a: (T.emojify(b) if isinstance(b, str) and T.is_tiles(b) else b)
                       for a, b in kw.items()}
                rendered.append(t(k, lang, **kw2))
            else:
                rendered.append(t(w, lang))
        lines.append("# " + "　".join(rendered))
    # 手牌 ｜ 剛摸到的牌（| 右邊為剛摸到）
    if player.drawn_tile:
        dt = T.of(player.drawn_tile)
        lines.append(f"# {hand_e} | {dt}" if hand_e else f"# | {dt}")
    else:
        lines.append(f"# {hand_e}" if hand_e else t("panel.none", lang))
    if names:
        lines.append(names)
    # 副露（碰/吃/槓後）換行顯示
    if player.melds:
        lines.append(t("panel.melds", lang) + "：" + "　".join(T.meld(m) for m in player.melds))
    if tenpai_note:
        lines.append("")
        lines.append(tenpai_note)
    if prompt:
        lines.append("")
        lines.append(prompt)
    text = "\n".join(lines)
    # Discord 訊息上限 2000 字：超長就逐級降載（先捨牌河，再捨動態），確保面板一定更新得了
    if len(text) > 1950 and river_info:
        return make_hand_panel(player, prompt, tenpai_note, last_info, board_info, "", lang)
    if len(text) > 1950 and last_info:
        return make_hand_panel(player, prompt, tenpai_note, "", board_info, "", lang)
    return text


def make_river_text(gs: GameState, lang: str = DEFAULT) -> str:
    """各家牌河（給「看牌河」按鈕的私密訊息用）。"""
    lines = [f"**{t('river.title', lang)}**"]
    for idx, p in enumerate(gs.players):
        if idx > 0:
            lines.append("─" * 24)   # 玩家之間分隔線
        trim = "…" if len(p.discards) > 15 else ""
        disc = trim + "".join(T.of(tt) for tt in p.discards[-15:]) if p.discards else t("river.none", lang)
        lines.append(f"{_wind(p.seat, lang)} {p.username}（{len(p.discards)}）")
        lines.append(f"# {disc}")
    lines.append("")
    lines.append(t("river.snapshot", lang))
    return "\n".join(lines)


def result_body(header: str, hand_str: str, result, log, gs: GameState,
                tenpai=None, lang: str = DEFAULT, draw_key: str = "result.draw_title") -> str:
    """一局結果的靜態文字（送到聊天串與各私人討論串）。"""
    hand_str = T.emojify(hand_str)
    if result is None:
        title = t(draw_key, lang)
        if tenpai is None:   # 途中流局（如九種九牌）：無聽牌罰符、無點數移動
            return f"# {title}"
        tnames = "、".join(gs.players[s].username for s in (tenpai or [])) or t("result.nobody", lang)
        return (f"# {title}\n"
                f"{t('result.tenpai_list', lang, names=tnames)}\n\n{log.describe(gs)}")
    top = f"# 🎉 {header}\n## {hand_str}"
    if result.yakuman:
        yaku = "\n".join(f"・**{n}**" for n, _ in result.yakuman)
        score_line = f"## ✨ {result.name}　{t('win.points', lang, n=result.points)}"
    else:
        yaku = "\n".join(f"・{n} {h}飜" for n, h in result.yaku)
        nm = f"　{result.name}" if result.name else ""
        score_line = (f"## {t('win.han_fu', lang, han=result.han, fu=result.fu)}{nm}　"
                      f"{t('win.points', lang, n=result.points)}")
    return top + "\n" + yaku + f"\n\n{score_line}\n\n" + log.describe(gs)


def make_score_text(gs: GameState, lang: str = DEFAULT) -> str:
    """各家點數與目前順位（依點數排序）。"""
    medals = ["🥇", "🥈", "🥉", "4️⃣"]
    ranked = sorted(gs.players, key=lambda p: -p.score)
    lines = [f"**{t('score.title', lang)}**"]
    for i, p in enumerate(ranked):
        bot = "🤖" if p.is_bot else ""
        lines.append(f"{medals[i]} {_wind(p.seat, lang)}　{bot}{p.username}：**{p.score}** {t('score.point', lang)}")
    return "\n".join(lines)


def make_meld_text(gs: GameState, lang: str = DEFAULT) -> str:
    """各家副露＋風位（給「看副露」按鈕用）。"""
    lines = [f"**{t('meld.title', lang)}**"]
    any_meld = False
    for p in gs.players:
        if p.melds:
            any_meld = True
            melds = "　".join(T.meld(m) for m in p.melds)
            lines.append(f"{_wind(p.seat, lang)}　{p.username}：{melds}")
    if not any_meld:
        lines.append(t("meld.none", lang))
    return "\n".join(lines)
