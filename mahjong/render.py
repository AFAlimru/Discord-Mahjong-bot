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


def _wind(seat: int, lang: str, gs: GameState | None = None) -> str:
    """自風顯示：傳入 gs 時**相對莊家**（莊家＝東，換莊會輪轉；連莊不變）。"""
    if gs is not None:
        seat = (seat - gs.dealer_seat) % len(gs.players)
    return t(f"wind.{seat}", lang)


def _round(gs: GameState, lang: str) -> str:
    """局名（如 東1局），依語言。"""
    return t("board.round", lang, wind=t(f"wind.{gs.round_wind - 1}", lang), num=gs.round_num)


def _mode(gs: GameState, lang: str) -> str:
    return t("mode.sanma" if gs.is_sanma else "mode.yonma", lang)

# ═══════════════════════════════════════════════════════════════
#  Board embed
# ═══════════════════════════════════════════════════════════════

def _river_str(p, cap: int | None = None) -> str:
    """一家的牌河字串（只留最近 cap 張，None＝全部；被截掉時前面加 …）。
    立直宣言牌（p.riichi_idx；宣言牌被鳴走時改為下一張捨牌）橫放。"""
    ds = p.discards
    start = 0 if cap is None else max(0, len(ds) - cap)
    ri = getattr(p, "riichi_idx", -1)
    body = "".join(T.side(tt) if i == ri else T.of(tt)
                   for i, tt in enumerate(ds) if i >= start)
    return ("…" if start else "") + body


_MSG_LIMIT = 1950   # Discord 單則訊息上限 2000 字，留一點餘裕


def _river_plain(p) -> str:
    """牌河的 unicode 純字版（每張 1 字，橫放的立直牌一樣標不出來但順序完整）。
    只在自訂表情版放不進 2000 字時當後備，絕不截斷。"""
    return "".join(str(tt) for tt in p.discards)


def _fit_block(build, p) -> str:
    """組一家的訊息：build(river_str) → 完整文字。自訂表情版超過上限時，牌河改用 unicode 純字牌
    （每張 1 字，順序完整，不截斷）。"""
    if not p.discards:
        return build("—")
    full = _river_str(p)
    txt = build(full)
    if len(txt) <= _MSG_LIMIT:
        return txt
    return build(_river_plain(p))


def _ind_str(gs: GameState) -> str:
    """寶牌指示牌：已翻開的顯示牌面，未翻開的顯示牌背 🀫（需槓才翻）。"""
    total_ind = len(gs.dora_indicators)
    shown     = [T.of(tt) for tt in gs.dora_indicators[:gs.revealed_dora]]
    hidden    = [T.back()] * (total_ind - gs.revealed_dora)
    return "".join(shown + hidden)


def _turn_line(gs: GameState, lang: str) -> str:
    """標頭裡的「輪到誰」（▶ 風位「名字」）。放標頭而不放各家訊息，打牌時才不用多改一家的訊息。"""
    p = gs.players[gs.current_seat] if 0 <= gs.current_seat < len(gs.players) else None
    if p is None:
        return ""
    return f"▶ {_wind(p.seat, lang, gs)} {'🤖' if p.is_bot else ''}「{p.username}」"


def board_header_text(gs: GameState, lang: str = DEFAULT) -> str:
    """牌桌標頭訊息：局／模式、本場／立直棒／牌山、寶牌指示、輪到誰。"""
    return "\n".join([
        f"## 🀄 {_round(gs, lang)}　{_mode(gs, lang)}",
        t("board.summary", lang, honba=gs.honba, sticks=gs.riichi_sticks, wall=gs.tiles_left),
        f"# {_ind_str(gs)}",
        _turn_line(gs, lang),
    ]).rstrip("\n")


def board_player_text(gs: GameState, p, lang: str = DEFAULT, open_hand: bool = False) -> str:
    """公開牌桌「一家」的訊息：風位、名字、立直、點數、拔北、（開立直／觀戰亮手牌）、副露、完整牌河。"""
    wind     = _wind(p.seat, lang, gs)
    riichi   = t("label.riichi_tag", lang) if p.riichi else ""
    bot_mark = "🤖" if p.is_bot else ""
    kita     = "　" + t("label.kita", lang, n=p.kita) if getattr(p, "kita", 0) else ""
    opened   = p.riichi and getattr(p, "open_riichi", False)
    if opened:
        riichi = t("label.open_riichi_tag", lang)

    def build(disc: str) -> str:
        lines = [f"> ## {wind} {bot_mark}「{p.username}」{riichi}（{p.score}）{kita}"]
        if opened or open_hand:   # 開立直／觀戰：亮出手牌
            hand_tiles = sorted(p.hand, key=lambda x: (x.suit, x.value))
            hstr = T.render(hand_tiles) if hand_tiles else "—"
            drawn = f"　＋{T.of(p.drawn_tile)}" if (open_hand and p.drawn_tile) else ""
            lines.append(f"{t('label.open_hand', lang)}：{hstr}{drawn}")
        if p.melds:
            lines.append(t("panel.melds", lang) + "：" + "　".join(T.meld(m, p.seat) for m in p.melds))
        lines.append(f"{t('panel.river', lang)}（{len(p.discards)}）：")
        lines.append(f"## {disc}")
        return "\n".join(lines)
    return _fit_block(build, p)


def board_status_text(status: str = "") -> str:
    """公開牌桌底下的狀態訊息（引言區塊）；沒有狀態時放個佔位，訊息才不會是空的。"""
    if not status:
        return "> —"
    return "\n".join(f"> {s}" for s in status.split("\n"))


def board_parts(gs: GameState, lang: str = DEFAULT, open_hand: bool = False) -> list[str]:
    """公開牌桌分成多則：[標頭, 東家, 南家, …]（座位順序）。每家各一則、牌河完整不截斷。"""
    return [board_header_text(gs, lang)] +            [board_player_text(gs, p, lang, open_hand) for p in gs.players]


def make_board_text(gs: GameState, status: str = "", open_hand: bool = False,
                    lang: str = DEFAULT) -> str:
    """整桌合成一段文字（舊介面；實際發送請用 board_parts 分則送，免得超過 2000 字）。"""
    parts = board_parts(gs, lang, open_hand) + ([board_status_text(status)] if status else [])
    return "\n".join(parts)


def make_thread_board(gs: GameState, status: str = "", lang: str = DEFAULT) -> str:
    """同 make_board_text（不亮手牌）。"""
    return make_board_text(gs, status, False, lang)


def _last_discard_info(gs: GameState, lang: str = DEFAULT) -> str:
    """上家剛打出的牌（含打牌者）；牌單獨一行放大。"""
    if gs is not None and gs.pending_discard is not None and gs.pending_from_seat >= 0:
        who = gs.players[gs.pending_from_seat].username
        head = t("feed.last_discard", lang, wind=_wind(gs.pending_from_seat, lang, gs), who=who)
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
    """私人面板上方的動態：最新一筆動作（依玩家語言）。"""
    log = _action_logs.get(gid, [])
    if log:
        key, kw = log[-1]
        return f"## {feed_text(key, lang, **kw)}"
    return ""


def make_action_log_text(gid: str, lang: str = DEFAULT) -> str:
    """動態（給「看動態」按鈕的私密訊息用，依語言）。
    表情牌一多，整份可能破 Discord 2000 字上限——只保留「最近、放得下」的幾筆。"""
    title = t("actionlog.title", lang)
    log = _action_logs.get(gid, [])
    if not log:
        return f"{title}\n{t('actionlog.empty', lang)}"
    lines = [f"> {feed_text(k, lang, **kw)}" for k, kw in log]
    kept, total = [], len(title) + 1
    for ln in reversed(lines):            # 由新到舊塞，塞到接近上限為止
        if total + len(ln) + 1 > 1950:
            break
        kept.append(ln)
        total += len(ln) + 1
    kept.reverse()
    body = "\n".join(kept)
    if len(kept) < len(lines):
        body = "> …\n" + body            # 有省略時標一下
    return f"{title}\n{body}"


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


def river_player_text(gs: GameState, p, lang: str = DEFAULT) -> str:
    """私人牌河「一家」的訊息：風位／名字／立直／拔北、副露、（開立直）手牌、完整牌河。"""
    opened = p.riichi and getattr(p, "open_riichi", False)
    riichi = (t("label.open_riichi_tag" if opened else "label.riichi_tag", lang)
              if p.riichi else "")

    def build(disc: str) -> str:
        lines = [f"## {_wind(p.seat, lang, gs)}「{p.username}」{riichi}{_kita_tag(gs, p, lang)}"]
        if p.melds:   # 副露自成一行（名字下、牌河上）
            lines.append(t("panel.melds", lang) + "：" + "　".join(T.meld(m, p.seat) for m in p.melds))
        if opened:   # 開立直：亮出手牌（不標待牌）
            lines.append(t("label.open_hand", lang) + "：" +
                         T.render(sorted(p.hand, key=lambda x: (x.suit, x.value))))
        lines.append(f"# {disc}")
        return "\n".join(lines)
    return _fit_block(build, p)


def river_header_text(gs: GameState, lang: str = DEFAULT) -> str:
    """私人牌河的標頭訊息：場況（局／本場／立直棒／牌山＋寶牌）＋輪到誰。"""
    head = "\n".join("> " + ln for ln in _board_info(gs, lang).split("\n"))
    turn = _turn_line(gs, lang)
    return head + (f"\n> {turn}" if turn else "")


def river_parts(gs: GameState, lang: str = DEFAULT) -> list[str]:
    """私人牌河分成多則：[場況標頭, 東家, 南家, …]，每家一則、牌河完整不截斷。"""
    return [river_header_text(gs, lang)] + [river_player_text(gs, p, lang) for p in gs.players]


def river_panel(gs: GameState, lang: str = DEFAULT, cap: int | None = None) -> str:
    """各家牌河合成一段（舊介面，只給需要單一字串的地方；cap 已不使用）。"""
    return "\n".join(["🀫 " + t("panel.river", lang)] +
                     [river_player_text(gs, p, lang) for p in gs.players])


def river_message_text(gs: GameState, lang: str = DEFAULT) -> str:
    """舊介面：整份牌河合成一段文字（實際發送請用 river_parts 分則送）。"""
    return "\n".join(river_parts(gs, lang))


def make_hand_panel(player: PlayerState, prompt: str = "", tenpai_note: str = "",
                    last_info: str = "", board_info: str = "", river_info: str = "",
                    lang: str = DEFAULT) -> str:
    """私人討論串的個人面板：牌河、場況/寶牌、手牌｜剛摸到的牌、副露、提示。"""
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
    # 副露（碰/吃/槓後）換行顯示
    if player.melds:
        lines.append(t("panel.melds", lang) + "：" + "　".join(T.meld(m, player.seat) for m in player.melds))
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


def _kita_tag(gs: GameState, p, lang: str) -> str:
    """名字旁的標記：三麻顯示拔北數（拔過才顯示），四麻不顯示。"""
    n = getattr(p, "kita", 0)
    return f"（{t('label.kita', lang, n=n)}）" if gs.is_sanma and n else ""


def make_river_text(gs: GameState, lang: str = DEFAULT) -> str:
    """各家牌河（給「看牌河」按鈕的私密訊息用）。"""
    lines = [f"**{t('river.title', lang)}**"]
    for idx, p in enumerate(gs.players):
        if idx > 0:
            lines.append("─" * 24)   # 玩家之間分隔線
        disc = _river_str(p, 15) if p.discards else t("river.none", lang)
        lines.append(f"{_wind(p.seat, lang, gs)} {p.username}{_kita_tag(gs, p, lang)}")
        lines.append(f"# {disc}")
    lines.append("")
    lines.append(t("river.snapshot", lang))
    return "\n".join(lines)


def result_body(header: str, hand_str: str, result, log, gs: GameState,
                tenpai=None, lang: str = DEFAULT, draw_key: str = "result.draw_title") -> str:
    """一局結果的靜態文字（送到聊天串與各私人討論串）。"""
    hand_str = T.emojify_hand(hand_str)
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
        lines.append(f"{medals[i]} {_wind(p.seat, lang, gs)}　{bot}{p.username}：**{p.score}** {t('score.point', lang)}")
    return "\n".join(lines)


def make_meld_text(gs: GameState, lang: str = DEFAULT) -> str:
    """各家副露＋風位（給「看副露」按鈕用）。"""
    lines = [f"**{t('meld.title', lang)}**"]
    any_meld = False
    for p in gs.players:
        if p.melds:
            any_meld = True
            melds = "　".join(T.meld(m, p.seat) for m in p.melds)
            lines.append(f"{_wind(p.seat, lang, gs)}　{p.username}：{melds}")
    if not any_meld:
        lines.append(t("meld.none", lang))
    return "\n".join(lines)
