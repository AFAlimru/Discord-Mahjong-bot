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
"""對局流程：討論串建立/清理、反應收集、輪到行動、單局/多局迴圈、和牌儀式、開局。"""
from __future__ import annotations
import asyncio
import json
from collections import Counter
import discord

from .config import WIND_LABELS
from .engine import (
    GameState, PlayerState, Tile, Suit, Meld, MeldType,
    new_game, is_complete, is_tenpai, get_tenpai_waits,
)
from .rules import (
    count_tiles, get_chi_options, get_ankan_options, has_kita,
    get_shouminkan_options, parse_tile, ai_choose_discard, ai_should_pon,
    evaluate_win, hand_waits, tenpai_advice, tenpai_note_text,
    is_furiten, ai_should_ron, is_menzen, is_kyuushu_kyuuhai, wait_status,
)
from .render import (
    make_thread_board, make_hand_panel, _board_info, _action_feed,
    _log_action, result_body, dora_reveal_text, river_panel, river_message_text, feed_text,
)
from .ui import (
    HandHelpButton, ScoreButton, MeldButton, ActionLogButton,
    make_hand_view, FairnessButton, make_board_view,
)
from .state import (
    _games, _channel_games, _waiting, _room_owners, _room_configs, _user_game,
    _game_tasks, _lobbies, _threads, _input_queues, _input_thread, _thread_game,
    _action_logs, _bg_tasks,
)
from . import settlement as st
from . import db
from . import i18n
from . import tiles as T
from . import rooms


async def _delete_later(msg: discord.Message, delay: float) -> None:
    await asyncio.sleep(delay)
    try:
        await msg.delete()
    except Exception:
        pass


def _name_ref(p: PlayerState) -> str:
    """玩家稱呼：真人用 @ 提及（在「編輯訊息」裡顯示不會再觸發通知），AI 用名字。"""
    return f"<@{p.user_id}>" if not p.is_bot else p.username


def _is_nagashi(p: PlayerState) -> bool:
    """流局滿貫資格：牌河非空、沒有捨牌被鳴走，且全為么九牌（1/9/字牌）。"""
    if not p.discards or getattr(p, "discard_taken", False):
        return False
    return all(t.suit in (Suit.WIND, Suit.DRAGON) or t.value in (1, 9) for t in p.discards)


def _standings_lines(rows, lang: str) -> str:
    """最終順位文字（依語言）。"""
    medals = ["🥇", "🥈", "🥉", "4️⃣"]
    out = [f"# {i18n.t('standings.title', lang)}", ""]
    for r in rows:
        sign = "＋" if r.total_pt >= 0 else "－"
        out.append(
            f"{medals[r.rank - 1]} **{i18n.t('standings.rank', lang, rank=r.rank)}**　{r.username}"
            f"　{r.score} {i18n.t('score.point', lang)}　｜　"
            f"{i18n.t('standings.settle', lang)} {sign}{abs(r.total_pt):.1f}"
        )
    return "\n".join(out)


def _thread_langs(public, private: dict) -> list:
    """回傳 [(討論串, 語言)]：公開串用母本，各私人串用該玩家語言。"""
    targets = [(public, i18n.DEFAULT)]
    for uid, pt in private.items():
        targets.append((pt, i18n.get_user_lang(uid)))
    return targets


_skin_cache: dict[str, str] = {}   # uid → 牌風偏好（省 DB 查詢；/mahjong skin 會清掉）


def _apply_skin(uid: str) -> None:
    """依玩家偏好設定當前渲染牌風（私人面板／牌河／出牌鈕用；公開畫面維持預設）。"""
    sk = _skin_cache.get(uid)
    if sk is None:
        sk = db.get_user_skin(uid) or "default"
        _skin_cache[uid] = sk
    T.set_skin(sk)


async def setup_threads(gid: str, channel: discord.TextChannel, gs: GameState,
                        watch: bool = False) -> None:
    """建立公開討論串 + 每位真人玩家的私人討論串。
    watch=True（觀戰）：公開串當牌桌；否則當聊天/和牌資訊串（牌桌資訊改用按鈕看）。"""
    rlang = _room_configs.get(gid, {}).get("lang", i18n.DEFAULT)   # 公開牌桌顯示語言
    if watch:
        public = await channel.create_thread(
            name=f"🀄 {rooms.label(gid)}　{gs.round_label}",
            type=discord.ChannelType.public_thread,
        )
        board_msg = await public.send(make_thread_board(gs, "", rlang),
                                      view=make_board_view(gid, rlang))
    else:
        public = await channel.create_thread(
            name=f"💬 {rooms.label(gid)}　{gs.round_label}",
            type=discord.ChannelType.public_thread,
        )
        await public.send("💬 這裡會顯示每局和牌／流局結果，也可以自由聊天。")
        board_msg = None

    private: dict[str, discord.Thread] = {}
    hand_msg: dict[str, discord.Message] = {}
    river_msg: dict[str, discord.Message] = {}
    for p in gs.players:
        if p.is_bot:
            continue
        try:
            _rn = rooms.room_no(gid)
            _suffix = f" #{_rn:04d}" if _rn else ""
            _lang = i18n.get_user_lang(p.user_id)
            pt = await channel.create_thread(
                name=f"🀫 {i18n.t('thread.hand', _lang, name=p.username)}{_suffix}",
                type=discord.ChannelType.private_thread,
                invitable=False,
            )
            try:
                await pt.add_user(discord.Object(id=int(p.user_id)))
            except Exception:
                pass
            private[p.user_id] = pt
            _apply_skin(p.user_id)
            # 牌河獨立一則訊息（放面板上方），手牌面板另一則——避免合併超過 2000 字上限
            rm = await pt.send(river_message_text(gs, _lang))
            river_msg[p.user_id] = rm
            hm = await pt.send(make_hand_panel(p, i18n.t("panel.waiting_start", _lang), lang=_lang),
                               view=make_hand_view(gid, _lang))
            hand_msg[p.user_id] = hm
        except Exception as e:
            print(f"[threads] 建立 {p.username} 私人討論串失敗：{e}")

    _threads[gid] = {
        "public": public, "board_msg": board_msg,
        "private": private, "hand_msg": hand_msg, "river_msg": river_msg,
        "result_msg": None,   # 上一局和牌/流局訊息（換局時刪除）
    }
    # 討論串 → gid（讓指令能在討論串內使用）
    _thread_game[public.id] = gid
    for pt in private.values():
        _thread_game[pt.id] = gid


def _cleanup(gid: str, channel_id: str) -> None:
    gs = _games.pop(gid, None)
    if gs:
        for p in gs.players:
            _user_game.pop(p.user_id, None)
            _input_queues.pop(p.user_id, None)
            _input_thread.pop(p.user_id, None)
    th = _threads.get(gid)
    if th:
        pub = th.get("public")
        if pub:
            _thread_game.pop(pub.id, None)
        for pt in th.get("private", {}).values():
            _thread_game.pop(pt.id, None)
    _waiting.pop(gid, None)
    _room_owners.pop(gid, None)
    _room_configs.pop(gid, None)
    _game_tasks.pop(gid, None)
    _action_logs.pop(gid, None)
    _lobbies.pop(gid, None)
    _threads.pop(gid, None)
    rooms.unregister(gid)
    if _channel_games.get(channel_id) == gid:
        del _channel_games[channel_id]


async def _delete_announce(th: dict) -> None:
    """刪除主頻道的開局公告訊息。"""
    ann = th.get("announce")
    if ann is not None:
        try:
            await ann.delete()
        except Exception:
            pass


async def _archive_threads(th: dict) -> None:
    """對局結束後封存討論串，並刪除開局公告。"""
    await _delete_announce(th)
    threads = [th.get("public")] + list(th.get("private", {}).values())
    for t in threads:
        if t is None:
            continue
        try:
            await t.edit(archived=True, locked=True)
        except Exception:
            pass


async def _delete_threads(th: dict) -> int:
    """刪除對局的所有討論串與開局公告（用於 /end 強制結束）。回傳刪除失敗的數量。"""
    await _delete_announce(th)
    threads = [th.get("public")] + list(th.get("private", {}).values())
    failed = 0
    for t in threads:
        if t is None:
            continue
        try:
            await t.delete()
        except Exception as e:
            failed += 1
            print(f"[threads] 刪除討論串失敗（{getattr(t, 'name', '?')}）：{e!r}"
                  f"　← 多半是機器人缺『管理討論串』權限")
    return failed


async def _delete_threads_later(th: dict, delay: float = 60.0) -> None:
    """對局自然結束後，保留一段時間讓大家看結果，再刪除討論串。"""
    await asyncio.sleep(delay)
    for t in [th.get("public")] + list(th.get("private", {}).values()):
        if t is None:
            continue
        try:
            await t.delete()
        except Exception:
            pass


def _schedule_delete_threads(th: dict, delay: float = 60.0) -> None:
    """排程延遲刪除討論串，並保留任務參考避免被 GC。"""
    task = asyncio.create_task(_delete_threads_later(th, delay))
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


#  0.2 討論串：打字輸入解析 + 反應收集 + 對局迴圈
# ═══════════════════════════════════════════════════════════════

def _parse_turn_input(raw, player, can_tsumo, can_riichi, kita_ok, ankan_opts):
    """解析玩家輪到時打的字 → (ok, (action, arg), err)。"""
    s = raw.strip()
    low = s.lower()
    if low in ("tsumo", "自摸", "zimo", "ツモ", "つも"):
        return (True, ("tsumo", None), "") if can_tsumo else (False, None, "msg.cant_tsumo")
    if low in ("!n", "n!", "拔北", "kita", "北抜き", "北抜"):
        return (True, ("kita", None), "") if kita_ok else (False, None, "msg.cant_kita")
    if low.startswith(("riichi", "reach")) or s.startswith(("立直", "リーチ")):
        rest = s
        for kw in ("riichi", "reach", "立直", "リーチ", "RIICHI", "Reach"):
            rest = rest.replace(kw, "")
        rest = rest.strip()
        if not can_riichi:
            return (False, None, "msg.cant_riichi")
        t = parse_tile(rest, player.hand, player.drawn_tile)
        if not t:
            return (False, None, "msg.riichi_need_tile")
        test = list(player.hand)
        dt = player.drawn_tile
        if t == dt:
            dt = None
        elif t in test:
            test.remove(t)
        if not is_tenpai(test + ([dt] if dt else [])):
            return (False, None, "msg.riichi_not_tenpai")
        return (True, ("riichi", t), "")
    if s.startswith(("暗槓", "暗槓")) or low.startswith("ankan"):
        rest = s
        for kw in ("暗槓", "暗槓", "ankan", "ANKAN"):
            rest = rest.replace(kw, "")
        rest = rest.strip()
        t = parse_tile(rest, player.hand, player.drawn_tile) if rest else (ankan_opts[0] if ankan_opts else None)
        if t and any(o.suit == t.suit and o.value == t.value for o in ankan_opts):
            return (True, ("ankan", t), "")
        return (False, None, "msg.cant_ankan")
    t = parse_tile(s, player.hand, player.drawn_tile)
    if t:
        return (True, ("discard", t), "")
    return (False, None, "msg.unknown_input")


async def collect_reactions_t(gs, gid, discard_tile, from_seat, thinking_time,
                              furiten_perm, furiten_temp):
    """討論串版反應收集：在各玩家私人討論串貼提示、等打字。回傳 (choice, uid, extra) 或 None。"""
    th        = _threads.get(gid, {})
    private   = th.get("private", {})
    next_seat = (from_seat + 1) % len(gs.players)
    from_name = gs.players[from_seat].username

    results = []   # (priority, seat, choice, extra)
    candidates = []
    for p in gs.players:
        if p.seat == from_seat:
            continue
        if p.is_bot:
            if ai_should_ron(gs, p, discard_tile, furiten_perm, furiten_temp):
                results.append((0, p.seat, "ron", None))
            elif (not p.riichi) and ai_should_pon(p.hand, discard_tile):
                results.append((1, p.seat, "pon", None))
            continue
        ron_ok = (not is_furiten(p, furiten_perm, furiten_temp)) and \
                 evaluate_win(gs, p, discard_tile, is_tsumo=False) is not None
        actions, chi_opts = [], []
        if ron_ok:
            actions.append("ron")
        if not p.riichi:   # 立直後不能副露（碰／吃／槓），只能榮和
            if count_tiles(p.hand, discard_tile) >= 2:
                actions.append("pon")
            if count_tiles(p.hand, discard_tile) >= 3:
                actions.append("kan")
            if p.seat == next_seat and not gs.is_sanma:
                chi_opts = get_chi_options(p.hand, discard_tile)
                if chi_opts:
                    actions.append("chi")
        if actions:
            candidates.append((p, actions, chi_opts))

    async def ask(p, actions, chi_opts):
        pt = private.get(p.user_id)
        if not pt:
            return None
        uid = p.user_id
        lang_p = i18n.get_user_lang(uid)
        fut = asyncio.get_event_loop().create_future()
        view = discord.ui.View(timeout=thinking_time + 5)

        def add_btn(label, style, choice, extra):
            b = discord.ui.Button(label=label, style=style)
            async def cb(inter):
                if str(inter.user.id) != uid:
                    await inter.response.send_message(
                        i18n.t("msg.not_your_reaction", i18n.get_user_lang(inter.user.id)), ephemeral=True)
                    return
                await inter.response.defer()
                if not fut.done():
                    fut.set_result((choice, extra))
            b.callback = cb
            view.add_item(b)

        CIRC = "①②③④⑤"
        # 吃的候選組合：牌面寫在訊息裡（放大），不塞進按鈕
        chi_combos = []
        for t1, t2 in chi_opts:
            srt = sorted([t1, t2, discard_tile], key=lambda t: (t.suit, t.value))
            chi_combos.append(" ".join(str(t) for t in srt))

        # 順序：吃、碰、槓、榮和、跳過（依玩家語言）
        _chi = i18n.t("action.chi", lang_p)
        if "chi" in actions:
            for idx, (t1, t2) in enumerate(chi_opts):
                lbl = _chi if len(chi_opts) == 1 else f"{_chi}{CIRC[idx]}"
                add_btn(lbl, discord.ButtonStyle.success, "chi", (t1, t2))  # 綠
        if "pon" in actions:
            add_btn(i18n.t("action.pon", lang_p), discord.ButtonStyle.primary, "pon", None)   # 藍
        if "kan" in actions:
            add_btn(i18n.t("action.kan", lang_p), discord.ButtonStyle.primary, "kan", None)   # 藍
        if "ron" in actions:
            add_btn(i18n.t("action.ron", lang_p), discord.ButtonStyle.danger, "ron", None)    # 紅
        add_btn(i18n.t("action.skip", lang_p), discord.ButtonStyle.secondary, "skip", None)   # 灰

        def prompt_text(rem):
            lines = [i18n.t("react.discarded", lang_p, who=from_name), f"# {discard_tile}"]
            if len(chi_combos) == 1:
                lines.append(i18n.t("react.chi_combo", lang_p, combo=chi_combos[0]))
            else:
                for idx, cmb in enumerate(chi_combos):
                    lines.append(i18n.t("react.chi_combo_n", lang_p, circ=CIRC[idx], combo=cmb))
            lines.append(i18n.t("react.choose", lang_p, n=rem))
            return "\n".join(lines)

        try:
            prompt_msg = await pt.send(prompt_text(int(thinking_time)), view=view)
        except Exception:
            return None

        async def cd():
            rem = int(thinking_time)
            while rem > 0:
                await asyncio.sleep(1)
                rem -= 1
                try:
                    await prompt_msg.edit(content=prompt_text(rem))
                except Exception:
                    pass

        cd_task = asyncio.create_task(cd())
        try:
            res = await asyncio.wait_for(fut, timeout=thinking_time)
        except asyncio.TimeoutError:
            res = ("skip", None)
        finally:
            cd_task.cancel()
        try:
            await prompt_msg.delete()
        except Exception:
            pass
        choice, extra = res
        if choice == "skip":
            return None
        pr = {"ron": 0, "pon": 1, "kan": 1, "chi": 2}.get(choice, 9)
        return (pr, p.seat, choice, extra)

    if candidates:
        human = await asyncio.gather(*[ask(p, a, c) for p, a, c in candidates])
        results.extend(r for r in human if r is not None)

    if not results:
        return None
    n = len(gs.players)
    rons = [r for r in results if r[2] == "ron"]
    # 三家和：同一張捨牌被三家同時榮和 → 途中流局
    if len(rons) >= 3:
        return ("sanchahou", None, None)
    # 雙榮（ダブロン）：兩家同時榮和，依頭跳排序（最近下家在前）回傳兩位
    if len(rons) == 2:
        rons.sort(key=lambda x: (x[1] - from_seat - 1) % n)
        return ("dblron", [gs.players[r[1]].user_id for r in rons], None)
    # 頭跳：同優先序時，取離捨牌者最近的下家（榮和＞碰槓＞吃）
    results.sort(key=lambda x: (x[0], (x[1] - from_seat - 1) % n))
    _, seat, choice, extra = results[0]
    return (choice, gs.players[seat].user_id, extra)


async def collect_chankan_t(gs, gid, kan_tile, kan_seat, thinking_time,
                            furiten_perm, furiten_temp):
    """討論串版搶槓（槍槓）窗口：可榮和此加槓牌者按「搶槓」。回傳搶槓者 user_id 或 None。"""
    th       = _threads.get(gid, {})
    private  = th.get("private", {})
    kan_name = gs.players[kan_seat].username
    results  = []        # seat
    candidates = []
    for p in gs.players:
        if p.seat == kan_seat:
            continue
        if is_furiten(p, furiten_perm, furiten_temp):
            continue
        if evaluate_win(gs, p, kan_tile, is_tsumo=False, is_chankan=True) is None:
            continue
        if p.is_bot:
            results.append(p.seat)       # AI 一律搶槓
        else:
            candidates.append(p)

    async def ask(p):
        pt = private.get(p.user_id)
        if not pt:
            return None
        uid = p.user_id
        lang_p = i18n.get_user_lang(uid)
        fut = asyncio.get_event_loop().create_future()
        view = discord.ui.View(timeout=thinking_time + 5)

        def add_btn(label, style, choice):
            b = discord.ui.Button(label=label, style=style)
            async def cb(inter):
                if str(inter.user.id) != uid:
                    await inter.response.send_message(
                        i18n.t("msg.not_your_reaction", i18n.get_user_lang(inter.user.id)), ephemeral=True)
                    return
                await inter.response.defer()
                if not fut.done():
                    fut.set_result(choice)
            b.callback = cb
            view.add_item(b)

        add_btn(i18n.t("react.chankan_btn", lang_p), discord.ButtonStyle.danger, "ron")
        add_btn(i18n.t("action.skip", lang_p), discord.ButtonStyle.secondary, "skip")

        def prompt_text(rem):
            return i18n.t("react.chankan", lang_p, name=kan_name, tile=kan_tile, n=rem)

        try:
            prompt_msg = await pt.send(prompt_text(int(thinking_time)), view=view)
        except Exception:
            return None

        async def cd():
            rem = int(thinking_time)
            while rem > 0:
                await asyncio.sleep(1)
                rem -= 1
                try:
                    await prompt_msg.edit(content=prompt_text(rem))
                except Exception:
                    pass

        cd_task = asyncio.create_task(cd())
        try:
            res = await asyncio.wait_for(fut, timeout=thinking_time)
        except asyncio.TimeoutError:
            res = "skip"
        finally:
            cd_task.cancel()
        try:
            await prompt_msg.delete()
        except Exception:
            pass
        return p.seat if res == "ron" else None

    if candidates:
        human = await asyncio.gather(*[ask(p) for p in candidates])
        results.extend(s for s in human if s is not None)

    if not results:
        return None
    results.sort()
    return gs.players[results[0]].user_id


async def _warn(thread, text: str) -> None:
    try:
        w = await thread.send(f"❌ {text}")
        asyncio.create_task(_delete_later(w, 4))
    except Exception:
        pass


async def _ping_turn(thread, uid: str) -> None:
    """在玩家私人討論串 @ 一下提醒輪到他，隨即刪除（通知已送出）。"""
    try:
        m = await thread.send(i18n.t("ping.your_turn", i18n.get_user_lang(uid), mention=f"<@{uid}>"))
        await asyncio.sleep(0.6)
        await m.delete()
    except Exception:
        pass


async def _ask_kita(gid, player, pt, hand_msg, timeout: float = 10.0) -> bool:
    """拔北詢問（像碰/吃）：面板顯示【拔北／跳過】，逾時視同跳過。回傳是否拔北。"""
    uid  = player.user_id
    lang = i18n.get_user_lang(uid)
    _apply_skin(uid)
    fut  = asyncio.get_event_loop().create_future()
    view = discord.ui.View(timeout=timeout + 5)

    def add(label, style, val):
        b = discord.ui.Button(label=label, style=style)
        async def cb(inter):
            if str(inter.user.id) != uid:
                await inter.response.send_message(
                    i18n.t("msg.not_your_turn", i18n.get_user_lang(inter.user.id)), ephemeral=True)
                return
            await inter.response.defer()
            if not fut.done():
                fut.set_result(val)
        b.callback = cb
        view.add_item(b)

    add("🀀 " + i18n.t("action.kita", lang), discord.ButtonStyle.success, True)
    add(i18n.t("action.skip", lang), discord.ButtonStyle.secondary, False)
    try:
        await hand_msg.edit(content=make_hand_panel(
            player, i18n.t("prompt.kita_ask", lang), "",
            _action_feed(gid, _games[gid], lang), lang=lang), view=view)
    except Exception:
        pass
    try:
        return await asyncio.wait_for(fut, timeout=timeout)
    except asyncio.TimeoutError:
        return False
    finally:
        view.stop()


async def wait_turn_action(gid, player, pt, hand_msg, thinking_time,
                           can_tsumo, can_riichi, kita_ok, ankan_opts,
                           prompt_base, tenpai_note, last_info="", board_info="",
                           riichi_locked=False, kakan_opts=None, kyuushu_ok=False,
                           banned: set = None):
    """輪到玩家：自摸/立直/暗槓/拔北用按鈕，出牌用打字。回傳 (action, arg) 或 None（逾時）。
    riichi_locked=True（已立直）：鎖手，打字無效，只能按自摸或逾時自動摸切。"""
    uid   = player.user_id
    lang  = i18n.get_user_lang(uid)
    _apply_skin(uid)   # 出牌鈕表情依玩家牌風
    # 面板用該玩家語言重算動態；場況已移到牌河訊息頂部（回合開始時刷新一次，牌山數即時）
    board_info = ""
    last_info  = _action_feed(gid, _games[gid], lang)
    rm = _threads.get(gid, {}).get("river_msg", {}).get(uid)
    if rm:
        try:
            await rm.edit(content=river_message_text(_games[gid], lang))
        except Exception:
            pass
    fut   = asyncio.get_event_loop().create_future()
    banned = banned or set()   # 食替禁止的 (suit,value)：這一打不能出
    state = {"riichi": False, "open": False, "rem": int(thinking_time)}
    view  = discord.ui.View(timeout=float(thinking_time) + 10)

    def panel(rem):
        _apply_skin(uid)   # 按鈕回呼在別的 context，重設牌風
        remain = i18n.t("prompt.remain", lang, n=rem)
        if riichi_locked:
            extra = f"{i18n.t('prompt.riichi_locked', lang)}　{remain}"
        elif state["riichi"]:
            extra = i18n.t("prompt.riichi_declared", lang)
        else:
            extra = f"{prompt_base}　{remain}"
        return make_hand_panel(player, extra, tenpai_note, last_info, board_info, lang=lang)

    async def refresh(rem):
        try:
            await hand_msg.edit(content=panel(rem), view=view)
        except Exception:
            pass

    action_btns: list = []          # 立直宣言中要停用的動作鈕（拔北/暗槓/加槓/九種/自摸）
    tile_btns:   list = []          # (按鈕, 牌, 是否剛摸到)

    def _keeps_tenpai(tile):
        """打出這張後是否仍聽牌（立直宣言牌的合法性）。"""
        test = list(player.hand)
        d = player.drawn_tile
        if tile == d:
            d = None
        elif tile in test:
            test.remove(tile)
        return is_tenpai(test + ([d] if d else []))

    def _apply_riichi_ui():
        """依宣言狀態切換按鈕：宣言中只留可宣言的牌（綠），其餘停用；取消則全部恢復。"""
        declaring = state["riichi"]
        for b in action_btns:
            b.disabled = declaring
        for b, tl, prim in tile_btns:
            if declaring:
                ok = _keeps_tenpai(tl)
                b.disabled = not ok
                b.style = discord.ButtonStyle.success if ok else discord.ButtonStyle.secondary
            else:
                b.disabled = (int(tl.suit), tl.value) in banned
                b.style = discord.ButtonStyle.primary if prim else discord.ButtonStyle.secondary

    def add_btn(label, style, kind, row=3):
        b = discord.ui.Button(label=label, style=style, row=row)
        async def cb(inter):
            if str(inter.user.id) != uid:
                await inter.response.send_message(
                    i18n.t("msg.not_your_turn", i18n.get_user_lang(inter.user.id)), ephemeral=True)
                return
            await inter.response.defer()
            if kind in ("riichi", "riichi_open"):
                if state["riichi"] and state["open"] == (kind == "riichi_open"):
                    state["riichi"] = False   # 同顆再按＝取消
                else:
                    state["riichi"] = True
                    state["open"]  = (kind == "riichi_open")
                _apply_riichi_ui()
                await refresh(state["rem"])
            elif not fut.done():
                fut.set_result(kind)
        b.callback = cb
        view.add_item(b)
        if kind not in ("riichi", "riichi_open"):
            action_btns.append(b)

    def add_tile_btn(tile, row, primary=False):
        """每張手牌一顆出牌鈕；立直宣言中則改為選擇立直宣言牌（需維持聽牌）。"""
        emoji = T.partial(tile)
        style = discord.ButtonStyle.primary if primary else discord.ButtonStyle.secondary
        b = discord.ui.Button(emoji=emoji, label=(None if emoji else tile.short),
                              style=style, row=row,
                              disabled=((int(tile.suit), tile.value) in banned))
        async def cb(inter, tile=tile):
            if str(inter.user.id) != uid:
                await inter.response.send_message(
                    i18n.t("msg.not_your_turn", i18n.get_user_lang(inter.user.id)), ephemeral=True)
                return
            await inter.response.defer()
            if riichi_locked:
                await _warn(pt, i18n.t("msg.riichi_locked_warn", lang))
                return
            if state["riichi"]:   # 立直宣言中：這張必須打了還聽牌
                if _keeps_tenpai(tile):
                    if not fut.done():
                        fut.set_result(("riichi_open" if state["open"] else "riichi", tile))
                else:
                    await _warn(pt, i18n.t("msg.riichi_not_tenpai", lang))
                return
            if not fut.done():
                fut.set_result(("discard", tile))
        b.callback = cb
        view.add_item(b)
        tile_btns.append((b, tile, primary))

    # ── 版面配置 ──────────────────────────────────────────────
    # 出牌鈕依花色分列：萬／條／餅／字各起一列（一列最多 5 顆，超過自動換列；
    # 花色太散導致超過 4 列時退回緊排）。動作鈕接在牌列下一列，資訊鈕再下一列
    # （版面放不下時本回合先不放資訊鈕，回合結束會恢復）。
    tile_rows: list[list] = []
    if not riichi_locked:
        _suit_key = {Suit.MAN: 0, Suit.SOU: 1, Suit.PIN: 2, Suit.WIND: 3, Suit.DRAGON: 3}
        _sh = sorted(player.hand, key=lambda x: (_suit_key[x.suit], x.suit, x.value))
        _groups: list[list] = []
        _prev = None
        for tl in _sh:
            k = _suit_key[tl.suit]
            if k != _prev:
                _groups.append([]); _prev = k
            _groups[-1].append(tl)
        for g in _groups:
            for i in range(0, len(g), 5):
                tile_rows.append(g[i:i + 5])
        d = player.drawn_tile
        if d is not None:   # 剛摸到的牌（藍色）放最後：接在末列或自成一列
            if tile_rows and len(tile_rows[-1]) < 5:
                tile_rows[-1].append(d)
            else:
                tile_rows.append([d])
        if len(tile_rows) > 4:   # 極端手牌（花色太散）：退回緊排
            _all = _sh + ([d] if d is not None else [])
            tile_rows = [_all[i:i + 5] for i in range(0, len(_all), 5)]

    has_actions = bool((riichi_locked and player.drawn_tile is not None) or kita_ok or
                       ankan_opts or kakan_opts or can_riichi or kyuushu_ok or can_tsumo)
    action_row = min(len(tile_rows), 4)
    info_row   = action_row + (1 if has_actions else 0)

    # 出牌鈕
    for r, row_tiles in enumerate(tile_rows):
        for tl in row_tiles:
            add_tile_btn(tl, row=r, primary=(tl is player.drawn_tile))

    # 動作鈕：摸切（立直後）、拔北、暗槓、加槓、立直、九種、自摸
    if riichi_locked and player.drawn_tile is not None:
        add_btn(i18n.t("action.tsumogiri", lang), discord.ButtonStyle.secondary,
                ("discard", player.drawn_tile), row=action_row)
    if kita_ok:
        add_btn(i18n.t("action.kita", lang), discord.ButtonStyle.secondary, ("kita", None), row=action_row)
    if ankan_opts:
        add_btn(i18n.t("action.ankan", lang), discord.ButtonStyle.secondary, ("ankan", ankan_opts[0]), row=action_row)
    if kakan_opts:
        add_btn(i18n.t("action.kakan", lang), discord.ButtonStyle.secondary, ("kakan", kakan_opts[0]), row=action_row)
    if can_riichi:
        add_btn(i18n.t("action.riichi", lang), discord.ButtonStyle.success, "riichi", row=action_row)   # 綠：宣言/取消
        if _room_configs.get(gid, {}).get("open_riichi"):
            add_btn(i18n.t("action.riichi_open", lang), discord.ButtonStyle.success, "riichi_open", row=action_row)
    if kyuushu_ok:
        add_btn(i18n.t("action.kyuushu", lang), discord.ButtonStyle.secondary, ("kyuushu", None), row=action_row)
    if can_tsumo:
        add_btn(i18n.t("action.tsumo", lang), discord.ButtonStyle.danger, ("tsumo", None), row=action_row)   # 紅

    # 資訊鈕：看點數／副露／動態／說明（牌河已獨立成訊息；版面滿了本回合先略過）
    if info_row <= 4:
        for _ib in (ScoreButton(gid, lang), MeldButton(gid, lang),
                    ActionLogButton(gid, lang), HandHelpButton(lang)):
            _ib.row = info_row
            view.add_item(_ib)

    await refresh(int(thinking_time))

    q = asyncio.Queue()
    _input_queues[uid] = q
    _input_thread[uid] = pt.id

    async def countdown():
        rem = int(thinking_time)
        while rem > 0:
            await asyncio.sleep(1)
            rem -= 1
            state["rem"] = rem
            if not state["riichi"]:
                await refresh(rem)

    async def reader():
        while not fut.done():
            raw, msg = await q.get()
            try:
                await msg.delete()
            except Exception:
                pass
            if riichi_locked:
                await _warn(pt, i18n.t("msg.riichi_locked_warn", lang))
                continue
            if state["riichi"]:
                t = parse_tile(raw, player.hand, player.drawn_tile)
                if not t:
                    await _warn(pt, i18n.t("msg.unknown_short", lang, raw=raw))
                    continue
                test = list(player.hand)
                dt = player.drawn_tile
                if t == dt:
                    dt = None
                elif t in test:
                    test.remove(t)
                if is_tenpai(test + ([dt] if dt else [])):
                    if not fut.done():
                        fut.set_result(("riichi", t))
                    return
                await _warn(pt, i18n.t("msg.riichi_not_tenpai", lang))
            else:
                ok, val, err = _parse_turn_input(raw, player, can_tsumo, can_riichi, kita_ok, ankan_opts)
                if ok:
                    if (isinstance(val, tuple) and val[0] == "discard" and val[1] is not None
                            and (int(val[1].suit), val[1].value) in banned):
                        await _warn(pt, i18n.t("msg.kuikae", lang))
                        continue
                    if not fut.done():
                        fut.set_result(val)
                    return
                await _warn(pt, i18n.t(err, lang, raw=raw))

    cd = asyncio.create_task(countdown())
    rd = asyncio.create_task(reader())
    try:
        result = await asyncio.wait_for(fut, timeout=float(thinking_time))
    except asyncio.TimeoutError:
        result = None
    finally:
        cd.cancel()
        rd.cancel()
        _input_queues.pop(uid, None)
        _input_thread.pop(uid, None)
        try:
            await hand_msg.edit(view=make_hand_view(gid, lang))   # 回合結束保留說明/看牌河
        except Exception:
            pass
    return result


async def play_hand_t(gid: str, channel: discord.TextChannel):
    """0.2：以討論串 + 打字輸入進行「一局」。回傳同 play_hand。"""
    gs            = _games[gid]
    config        = _room_configs.get(gid, {})
    thinking_time = config.get("thinking_time", 25)
    th            = _threads[gid]
    board_msg     = th["board_msg"]
    private       = th["private"]
    hand_msg      = th["hand_msg"]
    river_msg     = th.get("river_msg", {})
    _river_cache: dict[str, str] = {}   # uid → 上次送出的牌河文字（沒變就不編輯，省 API）
    _action_logs[gid] = []   # 每局開始清空動作記錄

    async def refresh_rivers():
        """把完整牌河同步到每位玩家面板上方的牌河訊息。"""
        for p in gs.players:
            if p.is_bot:
                continue
            rm = river_msg.get(p.user_id)
            if rm:
                lang = i18n.get_user_lang(p.user_id)
                _apply_skin(p.user_id)
                txt = river_message_text(gs, lang)
                if _river_cache.get(p.user_id) != txt:
                    try:
                        await rm.edit(content=txt)
                        _river_cache[p.user_id] = txt
                    except Exception:
                        pass

    async def refresh_feeds():
        """把最新動態同步到每位玩家的手牌面板，讓大家一直看得到（非自己回合也更新）。"""
        await refresh_rivers()
        for p in gs.players:
            if p.is_bot:
                continue
            hm = hand_msg.get(p.user_id)
            if hm:
                lang = i18n.get_user_lang(p.user_id)
                _apply_skin(p.user_id)
                try:
                    await hm.edit(content=make_hand_panel(
                        p, "", "", _action_feed(gid, gs, lang), lang=lang),
                        view=make_hand_view(gid, lang))
                except Exception:
                    pass

    async def render_board(key="", log=True, **kw):
        if log and key:
            _log_action(gid, key, **kw)   # 真人對局沒有牌桌，動作改記在私人面板上方
            try:                          # 逐手牌譜落地（供回放：含各家手牌、牌山、寶牌）
                db.log_action(gid, gs.turn, {
                    "t": "move", "key": key,
                    "kw": {k: str(v) for k, v in kw.items()},
                    "hands": {p.seat: [str(t) for t in p.hand] for p in gs.players},
                    "wall": gs.tiles_left,
                    "dora": [str(t) for t in gs.dora_indicators[:gs.revealed_dora]]})
            except Exception:
                pass
            await refresh_feeds()         # 每筆動作即時更新所有玩家面板的動態
        if board_msg is None:   # 真人對局沒有公開牌桌（資訊改用按鈕看）
            return
        T.set_skin(None)   # 公開牌桌一律預設牌風
        rlang = config.get("lang", i18n.DEFAULT)   # 觀戰牌桌用房間語言
        status = feed_text(key, rlang, **kw) if key else ""
        try:
            await board_msg.edit(content=make_thread_board(gs, status, rlang))
        except Exception:
            pass

    async def render_hand(p, prompt="", tenpai_note=""):
        hm = hand_msg.get(p.user_id)
        if hm:
            lang = i18n.get_user_lang(p.user_id)
            _apply_skin(p.user_id)
            try:
                await hm.edit(content=make_hand_panel(p, prompt, tenpai_note,
                                                      _action_feed(gid, gs, lang), lang=lang),
                              view=make_hand_view(gid, lang))
            except Exception:
                pass

    furiten_perm = {p.seat: False for p in gs.players}
    temp_furiten = {p.seat: False for p in gs.players}
    ippatsu      = {p.seat: False for p in gs.players}
    double_rii   = {p.seat: False for p in gs.players}
    any_call     = False
    rinshan_next = False
    no_draw      = False
    last_call    = None       # 剛鳴牌待打的上下文 (call_key, 被鳴者, 被鳴的牌)：打出時合併成一句動態
    allow_kuikae = bool(config.get("kuikae", False))
    kuikae_ban: set = set()   # 食替禁止的 (suit,value)：鳴牌後那一打不能出（現物＋筋）
    kan_count    = 0          # 全局已宣告的槓數（四槓散了用）
    kan_seats    = set()      # 宣告過槓的座位（同一人四槓＝四槓子，不流局）

    def _update_warn(p):
        """更新聽牌警示標籤（振聽／無役），面板上像【已立直】一樣顯眼（只有本人看得到）。"""
        no_yaku, furiten, ron_no = wait_status(gs, p, furiten_perm, temp_furiten)
        tags = []
        if no_yaku:
            tags.append("label.noyaku_tag")
        elif ron_no:
            tags.append("label.ron_noyaku_tag")
        if furiten:
            tags.append("label.furiten_tag")
        p.warn_tags = tags

    while True:
        if _games.get(gid) is not gs:
            return None
        player = gs.players[gs.current_seat]
        is_rinshan_draw = False
        temp_furiten[player.seat] = False
        _update_warn(player)   # 同巡振聽解除等，重算警示標籤

        if no_draw:
            no_draw = False
            player.drawn_tile = None
        else:
            tile = gs.draw_tile()
            if tile is None:
                tenpai_seats = [p.seat for p in gs.players if hand_waits(p)]
                nagashi = [p.seat for p in gs.players if _is_nagashi(p)]
                if nagashi:
                    return ("nagashi", nagashi, tenpai_seats)
                return ("draw", tenpai_seats)
            player.drawn_tile = tile
            is_rinshan_draw = rinshan_next
            rinshan_next = False
        ipp_start = ippatsu[player.seat]

        # ── AI ──────────────────────────────────────────────
        if player.is_bot:
            res = evaluate_win(
                gs, player, player.drawn_tile, is_tsumo=True, is_rinshan=is_rinshan_draw,
                is_tenhou=(player.is_dealer and not player.discards and not any_call),
                is_chiihou=((not player.is_dealer) and not player.discards and not any_call),
            ) if player.drawn_tile else None
            if res:
                hs = format_winning_hand(player, player.drawn_tile)
                await render_board("feed.tsumo", name=f"🤖 {player.username}")
                return ("tsumo", player.seat, res, hs)
            if gs.is_sanma and has_kita(player):
                if player.drawn_tile is not None:
                    player.hand.append(player.drawn_tile); player.drawn_tile = None
                for i, t in enumerate(player.hand):
                    if t.suit == Suit.WIND and t.value == 4:
                        player.hand.pop(i); break
                player.kita += 1
                await render_board("feed.kita", name=f"🤖 {player.username}", n=player.kita)
                await asyncio.sleep(0.6)
                continue
            drawn = player.drawn_tile
            if player.drawn_tile is not None:
                player.hand.append(player.drawn_tile); player.drawn_tile = None
            discard_tile = ai_choose_discard(player.hand)
            if discard_tile is None:
                return ("draw", [p.seat for p in gs.players if hand_waits(p)])
            if kuikae_ban and (int(discard_tile.suit), discard_tile.value) in kuikae_ban:
                _alt = [t for t in player.hand if (int(t.suit), t.value) not in kuikae_ban]
                if _alt:
                    discard_tile = ai_choose_discard(_alt) or _alt[0]
            player.hand.remove(discard_tile)
            player.discards.append(discard_tile)
            kuikae_ban = set()
            giri = "action.tsumogiri" if (drawn is not None and discard_tile == drawn) else "action.tegiri"
            nxt = _name_ref(gs.players[(player.seat + 1) % len(gs.players)])
            if last_call:   # 鳴牌後的打出：合併成「碰了…打出…」一句
                await render_board("feed.call_discard", name=f"🤖 {player.username}",
                                   call=last_call[0], loser=last_call[1], ctile=last_call[2],
                                   tile=discard_tile, giri=giri, nxt=nxt)
                last_call = None
            else:
                await render_board("feed.discard", name=f"🤖 {player.username}",
                                   word="feed.word_discard", tile=discard_tile, giri=giri, nxt=nxt)
            await asyncio.sleep(1.0)

        # ── Human（打字）────────────────────────────────────
        else:
            already_riichi = player.riichi
            lang_p = i18n.get_user_lang(player.user_id)
            can_tsumo  = bool(player.drawn_tile) and evaluate_win(
                gs, player, player.drawn_tile, is_tsumo=True, is_rinshan=is_rinshan_draw) is not None

            kyuushu_ok = False
            if already_riichi:
                # 立直後：鎖手，只能摸切或自摸，不可立直／暗槓／加槓／拔北
                adv = []
                can_riichi  = False
                ankan_opts  = []
                kakan_opts  = []
                kita_ok     = False
                tenpai_note = ""
                prompt_base = i18n.t("prompt.riichi_auto", lang_p)
                turn_time   = thinking_time if can_tsumo else 3
            else:
                adv = tenpai_advice(player)   # 14 張時：打哪張可進聽
                can_riichi = is_menzen(player) and bool(adv)
                ankan_opts = get_ankan_options(player.hand + ([player.drawn_tile] if player.drawn_tile else []))
                kakan_opts = get_shouminkan_options(player)   # 加槓：已碰且持有第 4 張
                kita_ok    = gs.is_sanma and has_kita(player)
                # 九種九牌：第一巡（未出牌、無人鳴牌）且 14 張含 9 種以上么九 → 可宣告途中流局
                kyuushu_ok = (len(player.discards) == 0 and not any_call
                              and player.drawn_tile is not None
                              and is_kyuushu_kyuuhai(player.hand + [player.drawn_tile]))
                # 進聽提示：打哪張可聽、聽哪些；並標註（無役）／（振聽）
                tenpai_note = tenpai_note_text(gs, player, adv, lang_p) if adv else ""
                prompt_base = i18n.t("prompt.discard", lang_p)
                turn_time   = thinking_time

            await render_board("feed.your_turn", log=False,
                               mention=f"<@{player.user_id}>", wind=f"wind.{player.seat}")

            pt = private.get(player.user_id)
            hm = hand_msg.get(player.user_id)
            ping_msg = None
            # 立直後是自動摸切，不必每巡 @；只有能自摸時才提醒
            if pt and (not already_riichi or can_tsumo):
                try:
                    ping_msg = await pt.send(
                        i18n.t("ping.your_turn", lang_p, mention=f"<@{player.user_id}>"))
                except Exception:
                    pass
            result = None
            if pt and hm and kita_ok:
                # 拔北改成像碰/吃的詢問：先問要不要拔，跳過（或逾時）就進正常出牌
                if await _ask_kita(gid, player, pt, hm):
                    result = ("kita", None)
                kita_ok = False   # 不再放拔北鈕（詢問已處理）
            if result is None and pt and hm:
                result = await wait_turn_action(
                    gid, player, pt, hm, turn_time,
                    can_tsumo, can_riichi, kita_ok, ankan_opts,
                    prompt_base, tenpai_note, _action_feed(gid, gs),
                    riichi_locked=already_riichi, kakan_opts=kakan_opts, kyuushu_ok=kyuushu_ok,
                    banned=kuikae_ban,
                )
            elif result is None:
                await render_hand(player, prompt_base, tenpai_note)
            if ping_msg:   # 出完牌（行動結束）才刪掉提醒
                try:
                    await ping_msg.delete()
                except Exception:
                    pass

            timed = result is None
            action, arg = ("discard", None) if timed else result

            # ── 九種九牌（途中流局）──
            if action == "kyuushu":
                await render_board("feed.kyuushu", name=player.username)
                return ("abort", "result.kyuushu")

            # ── Tsumo ──
            if action == "tsumo":
                res = evaluate_win(
                    gs, player, player.drawn_tile, is_tsumo=True, is_rinshan=is_rinshan_draw,
                    is_ippatsu=ippatsu[player.seat], is_double_riichi=double_rii[player.seat],
                    is_tenhou=(player.is_dealer and not player.discards and not any_call),
                    is_chiihou=((not player.is_dealer) and not player.discards and not any_call),
                )
                if res:
                    hs = format_winning_hand(player, player.drawn_tile)
                    await render_board("feed.tsumo", name=player.username)
                    return ("tsumo", player.seat, res, hs)
                action = "discard"  # 萬一無效退回出牌

            # ── 拔北 ──
            if action == "kita":
                if player.drawn_tile is not None:
                    player.hand.append(player.drawn_tile); player.drawn_tile = None
                for i, t in enumerate(player.hand):
                    if t.suit == Suit.WIND and t.value == 4:
                        player.hand.pop(i); break
                player.kita += 1
                await render_hand(player)
                await render_board("feed.kita", name=player.username, n=player.kita)
                continue

            # ── 暗槓 ──
            if action == "ankan" and ankan_opts:
                kan_tile = arg
                player.hand = [t for t in player.hand
                               if not (t.suit == kan_tile.suit and t.value == kan_tile.value)]
                if player.drawn_tile and player.drawn_tile.suit == kan_tile.suit and player.drawn_tile.value == kan_tile.value:
                    player.drawn_tile = None
                player.melds.append(Meld(MeldType.ANKAN, [Tile(kan_tile.suit, kan_tile.value)] * 4, -1))
                gs.open_dora()
                any_call = True
                kan_count += 1; kan_seats.add(player.seat)
                for s in ippatsu:
                    ippatsu[s] = False
                rinshan_next = True
                await render_hand(player)
                await render_board("feed.ankan", name=player.username, tile=kan_tile)
                continue

            # ── 加槓（小明槓）+ 搶槓 ──
            if action == "kakan" and arg is not None:
                kan_tile = arg
                await render_board("feed.kakan_declare", name=player.username, tile=kan_tile)
                robber = await collect_chankan_t(
                    gs, gid, kan_tile, player.seat, min(thinking_time, 12),
                    furiten_perm, temp_furiten,
                )
                if robber:
                    rp = next(p for p in gs.players if p.user_id == robber)
                    res = evaluate_win(
                        gs, rp, kan_tile, is_tsumo=False, is_chankan=True,
                        is_ippatsu=ippatsu[rp.seat], is_double_riichi=double_rii[rp.seat],
                        open_ron_yakuman=(getattr(rp, "open_riichi", False) and not player.riichi),
                    )
                    if res:
                        hs = format_winning_hand(rp, kan_tile)
                        await render_board("feed.chankan", name=rp.username,
                                           loser=player.username, tile=kan_tile)
                        return ("ron", rp.seat, player.seat, res, hs)
                # 放過搶槓 → 同巡振聽（立直者永久）
                _wk = (int(kan_tile.suit), kan_tile.value)
                for _p in gs.players:
                    if _p.seat != player.seat and _wk in hand_waits(_p):
                        temp_furiten[_p.seat] = True
                        if _p.riichi:
                            furiten_perm[_p.seat] = True
                        _update_warn(_p)
                # 完成加槓：移除第 4 張、碰子升級為槓
                if player.drawn_tile and player.drawn_tile.suit == kan_tile.suit and player.drawn_tile.value == kan_tile.value:
                    player.drawn_tile = None
                else:
                    for i, t in enumerate(player.hand):
                        if t.suit == kan_tile.suit and t.value == kan_tile.value:
                            player.hand.pop(i); break
                for m in player.melds:
                    if m.meld_type == MeldType.PON and m.tiles[0].suit == kan_tile.suit and m.tiles[0].value == kan_tile.value:
                        m.meld_type = MeldType.KAN
                        m.tiles = [Tile(kan_tile.suit, kan_tile.value)] * 4
                        break
                gs.open_dora()
                any_call = True
                kan_count += 1; kan_seats.add(player.seat)
                for s in ippatsu:
                    ippatsu[s] = False
                rinshan_next = True
                await render_hand(player)
                await render_board("feed.kakan", name=player.username, tile=kan_tile)
                continue

            # ── 出牌 / 立直 ──
            if timed or arg is None:
                discard_tile = player.drawn_tile if player.drawn_tile else player.hand[-1]
                if kuikae_ban and (int(discard_tile.suit), discard_tile.value) in kuikae_ban:
                    _alt = [t for t in player.hand if (int(t.suit), t.value) not in kuikae_ban]
                    if _alt:
                        discard_tile = _alt[-1]
            else:
                discard_tile = arg
            drawn = player.drawn_tile
            giri = "action.tsumogiri" if (drawn is not None and discard_tile == drawn) else "action.tegiri"

            if action in ("riichi", "riichi_open") and not player.riichi:
                player.riichi = True
                player.open_riichi = (action == "riichi_open")   # 開立直：亮手牌、2 飜
                gs.riichi_sticks += 1
                player.score -= 1000
                double_rii[player.seat] = (not any_call) and (len(player.discards) == 0)
                ippatsu[player.seat] = True
                word = "feed.word_riichi_open" if action == "riichi_open" else "feed.word_riichi"
            elif timed:
                word = "feed.word_timeout"
            else:
                word = "feed.word_discard"

            if player.drawn_tile is not None:
                player.hand.append(player.drawn_tile); player.drawn_tile = None
            if discard_tile in player.hand:
                player.hand.remove(discard_tile)
            player.discards.append(discard_tile)
            kuikae_ban = set()
            if ipp_start:
                ippatsu[player.seat] = False

            post_note = ""
            if not player.riichi:
                ws = hand_waits(player)   # 含副露的聽牌判定
                if ws:
                    wtiles = [Tile(Suit(s), v) for s, v in sorted(ws)]
                    waits = T.render(wtiles)
                    post_note = i18n.t("feed.tenpai_note", lang_p, waits=waits)
            _update_warn(player)   # 打完牌重算警示（振聽／無役）
            await render_hand(player, "", post_note)

            nxt = _name_ref(gs.players[(player.seat + 1) % len(gs.players)])
            if already_riichi:
                await render_board("feed.riichi_tsumogiri", name=player.username,
                                   tile=discard_tile, nxt=nxt)
            elif last_call:   # 鳴牌後的打出：合併成「碰了…打出…」一句
                await render_board("feed.call_discard", name=player.username,
                                   call=last_call[0], loser=last_call[1], ctile=last_call[2],
                                   tile=discard_tile, giri=giri, nxt=nxt)
                last_call = None
            else:
                await render_board("feed.discard", name=player.username, word=word,
                                   tile=discard_tile, giri=giri, nxt=nxt)

        gs.pending_discard   = discard_tile
        gs.pending_from_seat = player.seat

        # ── 反應（打字）──
        reaction = await collect_reactions_t(gs, gid, discard_tile, player.seat, thinking_time,
                                             furiten_perm, temp_furiten)
        wkey = (int(discard_tile.suit), discard_tile.value)
        for p in gs.players:
            if p.seat != player.seat and wkey in hand_waits(p):
                temp_furiten[p.seat] = True
                if p.riichi:
                    furiten_perm[p.seat] = True
                _update_warn(p)

        if reaction:
            rtype, r_uid, extra = reaction
            if rtype == "sanchahou":
                await render_board("feed.sanchahou")
                return ("abort", "result.sanchahou")
            if rtype == "dblron":
                # 雙榮：兩家同時榮和。r_uid 為依頭跳排序的兩個 uid
                winners = []
                for uid in r_uid:
                    wp = next(p for p in gs.players if p.user_id == uid)
                    res = evaluate_win(gs, wp, discard_tile, is_tsumo=False,
                                       is_ippatsu=ippatsu[wp.seat], is_double_riichi=double_rii[wp.seat],
                                       open_ron_yakuman=(getattr(wp, "open_riichi", False) and not player.riichi))
                    if res:
                        winners.append((wp.seat, res, format_winning_hand(wp, discard_tile)))
                if len(winners) >= 2:
                    await render_board("feed.dblron", n1=gs.players[winners[0][0]].username,
                                       n2=gs.players[winners[1][0]].username)
                    return ("dblron", winners, player.seat)
                if len(winners) == 1:
                    wseat, res, hs = winners[0]
                    await render_board("feed.ron", name=gs.players[wseat].username,
                                       loser=player.username, tile=discard_tile)
                    return ("ron", wseat, player.seat, res, hs)
                bad = next(p for p in gs.players if p.user_id == r_uid[0])
                await render_board("feed.ron_invalid", name=bad.username)
                reaction = None   # 皆無效 → 視同放過，落入下方振聽處理
            rp = next((p for p in gs.players if p.user_id == r_uid), None) if reaction else None
            from_name = player.username
            if rtype == "ron" and rp:
                res = evaluate_win(gs, rp, discard_tile, is_tsumo=False,
                                   is_ippatsu=ippatsu[rp.seat], is_double_riichi=double_rii[rp.seat],
                                   open_ron_yakuman=(getattr(rp, "open_riichi", False) and not player.riichi))
                if res:
                    hs = format_winning_hand(rp, discard_tile)
                    await render_board("feed.ron", name=rp.username, loser=from_name, tile=discard_tile)
                    return ("ron", rp.seat, player.seat, res, hs)
                await render_board("feed.ron_invalid", name=rp.username)
            elif rtype == "pon" and rp:
                removed, new_hand = 0, []
                for t in rp.hand:
                    if t.suit == discard_tile.suit and t.value == discard_tile.value and removed < 2:
                        removed += 1
                    else:
                        new_hand.append(t)
                rp.hand = new_hand
                rp.melds.append(Meld(MeldType.PON, [Tile(discard_tile.suit, discard_tile.value)] * 3, player.seat))
                if player.discards and player.discards[-1] == discard_tile:
                    player.discards.pop()   # 被碰走 → 從牌河移除
                player.discard_taken = True
                gs.current_seat = rp.seat
                no_draw = True
                any_call = True
                for s in ippatsu:
                    ippatsu[s] = False
                await render_hand(rp)
                await render_board("feed.pon", name=rp.username, loser=from_name, tile=discard_tile)
                last_call = ("term.pon", from_name, discard_tile)
                if not allow_kuikae:
                    kuikae_ban = {(int(discard_tile.suit), discard_tile.value)}
                continue
            elif rtype == "chi" and rp and extra:
                t1, t2 = extra
                rp.hand.remove(t1); rp.hand.remove(t2)
                meld_tiles = sorted([t1, t2, discard_tile], key=lambda t: (t.suit, t.value))
                rp.melds.append(Meld(MeldType.CHI, meld_tiles, player.seat))
                if player.discards and player.discards[-1] == discard_tile:
                    player.discards.pop()   # 被吃走 → 從牌河移除
                player.discard_taken = True
                gs.current_seat = rp.seat
                no_draw = True
                any_call = True
                for s in ippatsu:
                    ippatsu[s] = False
                await render_hand(rp)
                await render_board("feed.chi", name=rp.username, loser=from_name, tile=discard_tile)
                last_call = ("term.chi", from_name, discard_tile)
                if not allow_kuikae:
                    kuikae_ban = {(int(discard_tile.suit), discard_tile.value)}
                    _vals = sorted(t.value for t in meld_tiles)
                    if _vals[2] - _vals[0] == 2 and _vals[1] - _vals[0] == 1:   # 順子才有筋食替
                        if discard_tile.value == _vals[0] and _vals[2] + 1 <= 9:
                            kuikae_ban.add((int(discard_tile.suit), _vals[2] + 1))
                        elif discard_tile.value == _vals[2] and _vals[0] - 1 >= 1:
                            kuikae_ban.add((int(discard_tile.suit), _vals[0] - 1))
                continue
            elif rtype == "kan" and rp:
                removed, new_hand = 0, []
                for t in rp.hand:
                    if t.suit == discard_tile.suit and t.value == discard_tile.value and removed < 3:
                        removed += 1
                    else:
                        new_hand.append(t)
                rp.hand = new_hand
                rp.melds.append(Meld(MeldType.KAN, [Tile(discard_tile.suit, discard_tile.value)] * 4, player.seat))
                if player.discards and player.discards[-1] == discard_tile:
                    player.discards.pop()   # 被槓走 → 從牌河移除
                player.discard_taken = True
                gs.open_dora()
                gs.current_seat = rp.seat
                any_call = True
                kan_count += 1; kan_seats.add(rp.seat)
                for s in ippatsu:
                    ippatsu[s] = False
                rinshan_next = True
                await render_hand(rp)
                await render_board("feed.kan", name=rp.username, loser=from_name, tile=discard_tile)
                last_call = ("term.kan", from_name, discard_tile)
                continue

        # ── 途中流局檢查（一張牌捨出、無人鳴牌後）──
        n = len(gs.players)
        # 四風連打：四人第一巡捨同一張風牌、其間無人鳴牌
        if n == 4 and not any_call and all(len(p.discards) == 1 for p in gs.players):
            firsts = [p.discards[0] for p in gs.players]
            f0 = firsts[0]
            if f0.suit == Suit.WIND and all(d.suit == Suit.WIND and d.value == f0.value for d in firsts):
                await render_board("feed.suufon", name=player.username)
                return ("abort", "result.suufon")
        # 四家立直：全員立直且最後一張立直宣言牌無人榮和
        if all(p.riichi for p in gs.players):
            await render_board("feed.suucha_riichi")
            return ("abort", "result.suucha_riichi")
        # 四槓散了：場上累計四槓且由兩人以上宣告（同一人四槓＝四槓子，不流局）
        if kan_count >= 4 and len(kan_seats) >= 2:
            await render_board("feed.suukaikan")
            return ("abort", "result.suukaikan")

        gs.current_seat = (gs.current_seat + 1) % len(gs.players)


async def match_loop_t(gid: str, channel: discord.TextChannel) -> None:
    """0.2：討論串版多局對戰。"""
    config       = _room_configs.get(gid, {})
    length       = config.get("length", "tonpuu")
    tobi         = config.get("tobi", True)
    players_info = _waiting.get(gid, [])
    channel_id   = str(channel.id) if channel is not None else gid   # 段位賽 DM 無頻道
    th           = _threads[gid]
    public       = th["public"]
    tsumo_ct  = Counter()   # 自摸次數
    ron_ct    = Counter()   # 榮和次數
    riichi_ct = Counter()   # 立直次數
    houju_ct  = Counter()   # 放銃次數
    houju_pts = Counter()   # 放銃失點
    gain_pts  = Counter()   # 累計獲得點數（每局正向點數變動之和）
    best_win  = {}          # uid -> (打點, 等級名)：本場最高和了
    shown_all_last = False
    end_wind = 1 if length == "tonpuu" else 2   # 1=東風戰 2=半莊

    try:
        while True:
            outcome = await play_hand_t(gid, channel)
            if outcome is None:
                return
            gs = _games[gid]
            tenpai = None
            header_key, header_kw = "", {}
            hand_str = ""
            draw_key = "result.draw_title"
            dbl_winners = None   # 雙榮：[(seat, result, hand_str), …]，否則 None

            # 本局立直者：須在 advance_*（會清除 riichi 旗標）之前統計
            riichi_uids = [p.user_id for p in gs.players if p.riichi]
            for uid in riichi_uids:
                riichi_ct[uid] += 1
            win_seats: list[int] = []   # 本局贏家座位（牌譜結算用）
            loser_seat = None           # 本局放銃者座位

            if outcome[0] == "tsumo":
                _, wseat, result, hand_str = outcome
                log = st.apply_tsumo(gs, wseat, result)
                header_key, header_kw = "result.tsumo", {"name": gs.players[wseat].username}
                tsumo_ct[gs.players[wseat].user_id] += 1
                win_seats = [wseat]
                st.advance_after_win(gs, wseat)
            elif outcome[0] == "ron":
                _, wseat, lseat, result, hand_str = outcome
                log = st.apply_ron(gs, wseat, lseat, result)
                header_key, header_kw = "result.ron", {
                    "name": gs.players[wseat].username, "loser": gs.players[lseat].username}
                ron_ct[gs.players[wseat].user_id] += 1
                houju_ct[gs.players[lseat].user_id] += 1
                houju_pts[gs.players[lseat].user_id] += max(0, -log.deltas.get(lseat, 0))
                win_seats, loser_seat = [wseat], lseat
                st.advance_after_win(gs, wseat)
            elif outcome[0] == "dblron":
                # 雙榮（ダブロン）：兩家同時榮和、放銃者分別支付
                _, dbl_winners, lseat = outcome
                log = st.apply_ron_multi(gs, [(s, r) for s, r, _ in dbl_winners], lseat)
                for s, _, _ in dbl_winners:
                    ron_ct[gs.players[s].user_id] += 1
                houju_ct[gs.players[lseat].user_id] += 1
                houju_pts[gs.players[lseat].user_id] += max(0, -log.deltas.get(lseat, 0))
                win_seats, loser_seat = [s for s, _, _ in dbl_winners], lseat
                # 莊家若在贏家中→連莊，否則輪莊
                dealer = gs.dealer_seat
                st.advance_after_win(gs, dealer if any(s == dealer for s, _, _ in dbl_winners)
                                     else dbl_winners[0][0])
            elif outcome[0] == "nagashi":
                _, nagashi_seats, tenpai = outcome
                log = st.apply_nagashi(gs, nagashi_seats)
                from .scoring import ScoreResult
                winner = gs.players[nagashi_seats[0]]
                pts = 12000 if winner.is_dealer else 8000
                result = ScoreResult(yaku=[("流局滿貫", 5)], han=5, fu=0,
                                     points=pts, name="流局滿貫", valid=True)
                names = "、".join(gs.players[s].username for s in nagashi_seats)
                header_key, header_kw = "result.nagashi", {"names": names}
                hand_str = "　".join(str(t) for t in winner.discards)
                for s in nagashi_seats:
                    tsumo_ct[gs.players[s].user_id] += 1
                win_seats = list(nagashi_seats)
                st.advance_after_draw(gs, tenpai)
            elif outcome[0] == "abort":
                # 途中流局（九種九牌／四風連打／四槓散了／四家立直／三家和）：
                # 莊家連莊、本場 +1，無點數移動、無聽牌罰符；立直棒留到下一局
                draw_key = outcome[1]
                log = st.SettleLog({p.seat: 0 for p in gs.players}, note=draw_key)
                result = None
                st.advance_abortive(gs)
            else:
                _, tenpai = outcome
                log = st.apply_ryuukyoku(gs, tenpai)
                result = None
                st.advance_after_draw(gs, tenpai)
            # 本局獲得點數（正向變動）累加
            for s, d in log.deltas.items():
                if d > 0:
                    gain_pts[gs.players[s].user_id] += d

            # 牌譜：每局結算事件（以 user_id 記錄，供 /mahjong repair 重算戰績與進階數據）
            try:
                def _detail(r):                          # 和了詳情（供回放和牌儀式）
                    return {"yaku": [[n, h] for n, h in (r.yaku or [])],
                            "yakuman": [n for n, *_ in (r.yakuman or [])],
                            "han": r.han, "fu": r.fu, "points": r.points, "name": r.name}
                if outcome[0] == "dblron":
                    win_points = {gs.players[s].user_id: r.points for s, r, _ in dbl_winners}
                    win_names  = {gs.players[s].user_id: r.name for s, r, _ in dbl_winners}
                    win_hands  = {gs.players[s].user_id: hs for s, _, hs in dbl_winners}
                    win_detail = {gs.players[s].user_id: _detail(r) for s, r, _ in dbl_winners}
                elif win_seats and result is not None and outcome[0] in ("tsumo", "ron"):
                    win_points = {gs.players[s].user_id: result.points for s in win_seats}
                    win_names  = {gs.players[s].user_id: result.name for s in win_seats}
                    win_hands  = {gs.players[s].user_id: hand_str for s in win_seats}
                    win_detail = {gs.players[s].user_id: _detail(result) for s in win_seats}
                else:
                    win_points, win_names, win_hands, win_detail = {}, {}, {}, {}
                for _uid, _pts in win_points.items():   # 累積本場最高和了
                    if _pts > best_win.get(_uid, (0,))[0]:
                        best_win[_uid] = (_pts, win_names.get(_uid, ""), win_hands.get(_uid, ""))
                _is_riichi = any(u in riichi_uids for u in win_detail)
                db.log_action(gid, gs.turn, {
                    "t": "settle",
                    "win": outcome[0],
                    "winners": [gs.players[s].user_id for s in win_seats],
                    "loser": gs.players[loser_seat].user_id if loser_seat is not None else None,
                    "riichi": riichi_uids,
                    "furo": [p.user_id for p in gs.players if p.melds],   # 本局有副露者
                    "wp": win_points,                                    # 和了打點（贏家）
                    "wh": win_hands,                                     # 和了牌型（贏家）
                    "wd": win_detail,                                    # 和了詳情（役/飜符/點，供回放儀式）
                    "dora": [str(t) for t in gs.dora_indicators[:gs.revealed_dora]],
                    "ura": ([str(t) for t in gs.ura_indicators[:gs.revealed_dora]] if _is_riichi else []),
                    "deltas": {gs.players[s].user_id: int(d) for s, d in log.deltas.items()},
                })
            except Exception as e:
                print(f"[gamelog] settle 記錄失敗：{e}")

            db.update_game_state(gid, "playing", gs.to_dict())

            # 一局結束：先把各家手牌面板刪掉，讓結果獨佔畫面（下一局再重建）
            for uid, hm in list(th.get("hand_msg", {}).items()):
                if hm:
                    try:
                        await hm.delete()
                    except Exception:
                        pass
                th["hand_msg"][uid] = None

            # 和牌：公開串與各私人串都做「逐一揭曉」的和牌儀式；流局則送結果文字
            # 公開串用母本語言，各私人串用該玩家語言。每則 = (訊息, 完整文字, 語言)
            # 公開串訊息保留為紀錄、私人串訊息換局前刪除，故分開收集。
            private = th.get("private", {})
            pub_count = 1 if public is not None else 0   # DM 段位賽無公開串
            targets = ([(public, i18n.DEFAULT)] if public is not None else []) + \
                      [(private[uid], i18n.get_user_lang(uid)) for uid in private]
            pub_pairs, priv_pairs = [], []

            if dbl_winners is not None:
                # 雙榮：每個串依序揭曉兩位贏家（最後一位才附上合計分數表）
                async def run_dbl(ch, lg):
                    out = []
                    for i, (seat, res, hs) in enumerate(dbl_winners):
                        hkw = {"name": gs.players[seat].username,
                               "loser": gs.players[lseat].username}
                        out.append(await win_ceremony(
                            ch, gs, "result.ron", hkw, hs, res, log, lg,
                            show_log=(i == len(dbl_winners) - 1)))
                    return out
                res_lists = await asyncio.gather(
                    *[run_dbl(ch, lg) for ch, lg in targets], return_exceptions=True)
                for idx, rl in enumerate(res_lists):
                    if isinstance(rl, Exception):
                        continue
                    (pub_pairs if idx < pub_count else priv_pairs).extend(rl)
            elif result is not None:
                cer = await asyncio.gather(
                    *[win_ceremony(ch, gs, header_key, header_kw, hand_str, result, log, lg)
                      for ch, lg in targets],
                    return_exceptions=True,
                )
                for idx, c in enumerate(cer):
                    if isinstance(c, Exception):
                        continue
                    (pub_pairs if idx < pub_count else priv_pairs).append(c)
            else:
                if public is not None:
                    pub_text = result_body("", "", None, log, gs, tenpai, i18n.DEFAULT, draw_key)
                    pub_pairs.append((await public.send(pub_text), pub_text, i18n.DEFAULT))
                for uid, pt in private.items():
                    lg = i18n.get_user_lang(uid)
                    txt = result_body("", "", None, log, gs, tenpai, lg, draw_key)
                    try:
                        priv_pairs.append((await pt.send(txt), txt, lg))
                    except Exception:
                        pass

            pairs = pub_pairs + priv_pairs
            result_msg = pub_pairs[0][0] if pub_pairs else None
            priv_msgs  = [p[0] for p in priv_pairs]

            # 全部顯示完 → 保留完整結果，倒數 5 秒；最後一局改顯示「結束對局」
            over = st.is_game_over(gs, length, tobi)
            await _result_countdown(pairs, 5, "countdown.end" if over else "countdown.next_hand")
            if over:
                break

            # 換下一局：公開串保留每局結果（不刪），只清掉各私人串的結果副本
            for m in priv_msgs:
                if m:
                    try:
                        await m.delete()
                    except Exception:
                        pass

            # 本局結束、最終局開打前秀「ALL LAST」（gs 已輪莊到下一局的局數）
            # 四人東風＝東4 前（東3 結束）、三人東風＝東3 前（東2 結束）、半莊則為南場對應局
            # 公開串 + 各私人手牌串都秀，玩家在自己的討論串也看得到
            if (not shown_all_last and gs.round_wind == end_wind
                    and gs.round_num == len(gs.players)):
                shown_all_last = True
                banners = []
                for tch, lg in _thread_langs(public, th.get("private", {})):
                    if tch is None:
                        continue
                    try:
                        banners.append(await tch.send(f"# 🏁 {i18n.t('result.all_last', lg)}"))
                    except Exception:
                        pass
                await asyncio.sleep(2.5)
                for b in banners:
                    try:
                        await b.delete()
                    except Exception:
                        pass

            new_gs = deal_next_hand(gid, players_info, gs)
            _games[gid] = new_gs
            try:                                          # 每局牌山落地（供日後完整重現手牌）
                db.log_action(gid, new_gs.turn,
                              {"t": "handstart", "wall": new_gs.wall_seed})
            except Exception:
                pass
            if th.get("board_msg"):
                try:
                    await th["board_msg"].edit(
                        content=make_thread_board(new_gs, "", config.get("lang", i18n.DEFAULT)))
                except Exception:
                    pass
            for p in new_gs.players:
                if p.is_bot:
                    continue
                pt = th["private"].get(p.user_id)
                if not pt:
                    continue
                try:
                    _lang = i18n.get_user_lang(p.user_id)
                    _apply_skin(p.user_id)
                    # 換局：先刪上一局的牌河訊息與手牌面板，再發新的一組（牌河在上）
                    for _om in (th.get("river_msg", {}).get(p.user_id),
                                th["hand_msg"].get(p.user_id)):
                        if _om:
                            try:
                                await _om.delete()
                            except Exception:
                                pass
                    th.setdefault("river_msg", {})[p.user_id] = await pt.send(
                        river_message_text(new_gs, _lang))
                    th["hand_msg"][p.user_id] = await pt.send(
                        make_hand_panel(p, lang=_lang),
                        view=make_hand_view(gid, _lang))
                except Exception:
                    pass
            await asyncio.sleep(1)

        gs = _games[gid]
        rows = st.final_standings(gs, start_points=config.get("start_points"),
                                  length=config.get("length", "hanchan"),
                                  ruleset=config.get("ruleset", "mixed"))
        # 最終順位：公開串用母本、各私人手牌串用該玩家語言（確保自己討論串也看得到）
        for tch, lg in _thread_langs(public, th.get("private", {})):
            if tch is None:
                continue
            try:
                v = discord.ui.View(timeout=None)
                v.add_item(FairnessButton(gid))
                await tch.send(_standings_lines(rows, lg), view=v)
            except Exception as e:
                print(f"[standings] 發送最終順位失敗：{e}")
        # 之後才寫資料庫與個人統計
        try:
            db.finish_game(gid, gs.to_dict())
        except Exception as e:
            print(f"[db] finish_game 失敗：{e}")
        start_pts = config.get("start_points")
        if start_pts is None:
            start_pts = 35000 if gs.is_sanma else 25000
        mode = "sanma" if len(gs.players) == 3 else "yonma"
        rank_of = {p.user_id: i + 1 for i, p in enumerate(sorted(gs.players, key=lambda p: -p.score))}
        for p in gs.players:
            if p.is_bot:
                continue
            try:
                db.add_game_record(
                    game_id=gid, user_id=p.user_id, username=p.username, mode=mode,
                    rank=rank_of[p.user_id], score=p.score, score_delta=p.score - start_pts,
                    tsumo=tsumo_ct[p.user_id], ron=ron_ct[p.user_id],
                    houju=houju_ct[p.user_id], houju_points=houju_pts[p.user_id],
                    riichi=riichi_ct[p.user_id], gain_points=gain_pts[p.user_id],
                )
            except Exception as e:
                print(f"[stats] add_game_record 失敗：{e}")

        # 任務：每日第一場對局 → 發活躍度；並更新最高和了
        for p in gs.players:
            if p.is_bot:
                continue
            try:
                db.reward_play(p.user_id, p.username)
                if p.user_id in best_win:
                    pts, nm, hand = best_win[p.user_id]
                    db.update_best_win(p.user_id, pts, nm, hand, p.username)
            except Exception as e:
                print(f"[task] reward/best_win 失敗：{e}")

        # 段位賽：更新段位／R，並把變化通知各玩家
        if config.get("ranked"):
            try:
                from . import rating as _rt
                results = [(p.user_id, rank_of[p.user_id]) for p in gs.players]
                names   = {p.user_id: p.username for p in gs.players}
                before  = {p.user_id: db.get_rating(p.user_id, mode)
                           for p in gs.players if not p.is_bot}
                updated = db.apply_ranked_game(mode, results, names)
                for uid, v in updated.items():
                    pt = th.get("private", {}).get(uid)
                    if not pt:
                        continue
                    lg = i18n.get_user_lang(uid)
                    b  = before.get(uid) or {}
                    dr = v["rate"] - b.get("rate", _rt.START_RATE)
                    try:
                        await pt.send(i18n.t(
                            "rank.result", lg, dan=_rt.dan_name(v["dan_idx"]),
                            pt=v["dan_pt"], rate=v["rate"],
                            dr=(f"+{dr:.1f}" if dr >= 0 else f"{dr:.1f}")))
                    except Exception:
                        pass
            except Exception as e:
                print(f"[rank] 段位結算失敗：{e}")

        if th.get("is_dm"):
            pass   # 段位賽走 DM，不封存／刪除（DM 自然保留）
        elif th.get("board_msg") is not None:
            # 觀戰：保留約一分鐘讓大家看結果，再刪除討論串
            await _delete_announce(th)
            _schedule_delete_threads(th, 60)
        else:
            # 真人對局：封存（保留供回顧）
            await _archive_threads(th)
    finally:
        _cleanup(gid, channel_id)


# ═══════════════════════════════════════════════════════════════
#  Match loop（多局：東風戰／半莊）
# ═══════════════════════════════════════════════════════════════

def deal_next_hand(gid: str, players_info: list[dict], prev: GameState) -> GameState:
    """開新一局：重洗牌山、重新配牌，但保留分數、莊家、場風局數、本場、立直棒。"""
    gs = new_game(gid, players_info, prev.is_sanma)
    gs.round_wind    = prev.round_wind
    gs.round_num     = prev.round_num
    gs.honba         = prev.honba
    gs.riichi_sticks = prev.riichi_sticks
    gs.dealer_seat   = prev.dealer_seat
    for p, pp in zip(gs.players, prev.players):
        p.score     = pp.score
        p.is_dealer = (p.seat == prev.dealer_seat)
    gs.current_seat = prev.dealer_seat   # 莊家先摸
    return gs


def format_winning_hand(player: PlayerState, win_tile: Tile) -> str:
    """和牌手牌顯示：門前手牌（含空隔）＋副露，並標出和牌張。"""
    hand_sorted = sorted(player.hand, key=lambda t: (t.suit, t.value))
    parts = [" ".join(str(t) for t in hand_sorted)]
    if player.melds:
        parts.append("　".join(str(m) for m in player.melds))
    return "　".join(parts) + f"　| {win_tile}"


async def win_ceremony(channel: discord.TextChannel, gs: GameState,
                       header_key: str, header_kw: dict, hand_str: str, result,
                       log: "st.SettleLog", lang: str = i18n.DEFAULT, show_log: bool = True):
    """和牌儀式（依 lang）：先放標題，再放手牌，逐一揭曉役種，最後公布等級與點數。
    show_log=False 時不附最終分數表（雙榮時只在最後一位附上合計）。"""
    head = f"# 🎉 {i18n.t(header_key, lang, **header_kw)}"
    msg = await channel.send(head)          # ① 先只放榮和／自摸標題
    await asyncio.sleep(1.0)
    # 立直則一併揭曉裏寶牌（以原始（中文）役名判斷）
    names = [n for n, *_ in (result.yaku or [])] + [n for n, *_ in (result.yakuman or [])]
    is_riichi = any("立直" in (n or "") for n in names)
    top = f"{head}\n{dora_reveal_text(gs, is_riichi, lang)}\n## {T.emojify(hand_str)}"
    try:
        await msg.edit(content=top)         # ② 揭曉寶牌/裏寶牌 + 手牌
    except Exception:
        pass
    await asyncio.sleep(1.0)

    shown: list[str] = []
    if result.yakuman:
        items = [(n, None) for n, _ in result.yakuman]
    else:
        items = [(n, h) for n, h in result.yaku]

    for name, han in items:
        disp = i18n.yaku(name, lang)
        shown.append(f"・**{disp}**" if han is None else f"・{disp}　{han}飜")
        try:
            await msg.edit(content=top + "\n" + "\n".join(shown))
        except Exception:
            pass
        await asyncio.sleep(0.9)

    await asyncio.sleep(0.4)
    pts = i18n.t("win.points", lang, n=result.points)
    if result.yakuman:
        score_line = f"## ✨ {i18n.yaku(result.name, lang)}　{pts}"
    else:
        nm = f"　{i18n.yaku(result.name, lang)}" if result.name else ""
        score_line = f"## {i18n.t('win.han_fu', lang, han=result.han, fu=result.fu)}{nm}　{pts}"
    body = top + "\n" + "\n".join(shown) + f"\n\n{score_line}"
    if show_log:
        body += "\n\n" + log.describe(gs)
    try:
        await msg.edit(content=body)
    except Exception:
        pass
    return msg, body, lang


async def _result_countdown(pairs: list, secs: int = 5,
                            tail_key: str = "countdown.next_hand") -> None:
    """一局結果全部顯示完後，保留完整結果並於尾端倒數（每則訊息依其語言）。
    pairs：[(訊息, 完整文字, 語言), ...]；tail_key：倒數後動作的翻譯鍵。"""
    for n in range(secs, 0, -1):
        for m, base, lg in pairs:
            if not m:
                continue
            try:
                line = i18n.t("countdown.line", lg, n=n, tail=i18n.t(tail_key, lg))
                await m.edit(content=f"{base}\n\n{line}")
            except Exception:
                pass
        await asyncio.sleep(1)
    # 倒數結束後還原為乾淨結果（保留下來的公開紀錄不會殘留倒數字樣）
    for m, base, lg in pairs:
        if m:
            try:
                await m.edit(content=base)
            except Exception:
                pass


# ═══════════════════════════════════════════════════════════════
#  Launch game
# ═══════════════════════════════════════════════════════════════

async def launch_game(gid: str, channel: discord.TextChannel) -> None:
    config       = _room_configs[gid]
    is_sanma     = config["is_sanma"]
    players_info = _waiting[gid]

    gs = new_game(gid, players_info, is_sanma, start_points=config.get("start_points"))
    _games[gid] = gs
    for p in gs.players:
        _user_game[p.user_id] = gid

    db.create_game(gid, str(channel.guild.id), str(channel.id), gs.wall_seed,
                   room_no=rooms.room_no(gid))
    db.update_game_state(gid, "playing", gs.to_dict())
    # 牌譜：開局事件（名單／模式／起始點數），供日後重算與重播
    try:
        db.log_action(gid, 0, {
            "t": "gamestart",
            "mode": "sanma" if is_sanma else "yonma",
            "start": config.get("start_points") or (35000 if is_sanma else 25000),
            "players": {p.user_id: {"seat": p.seat, "name": p.username, "bot": p.is_bot}
                        for p in gs.players},
            "wall": gs.wall_seed,                         # 第一局牌山（供日後完整重現手牌）
        })
    except Exception as e:
        print(f"[gamelog] gamestart 記錄失敗：{e}")
    try:
        db.set_room_config(gid, config)   # 保存設定，供重連回復對局
    except Exception as e:
        print(f"[launch] 保存房間設定失敗：{e}")
    rooms.set_status(gid, "playing")

    # 0.2：建立公開討論串 + 每位真人的私人討論串（觀戰模式才用牌桌）
    try:
        await setup_threads(gid, channel, gs, watch=config.get("open_hand", False))
    except Exception as e:
        await channel.send(f"❌ 建立討論串失敗：{e}\n（需確認 bot 有「建立公開／私人討論串、管理討論串」權限）")
        _cleanup(gid, str(channel.id))
        return

    announce = await channel.send(
        f"🀄 {rooms.label(gid)} 遊戲開始！牌桌在討論串 {_threads[gid]['public'].mention}，"
        f"真人玩家請到自己的私人討論串出牌（直接打字）。"
    )
    _threads[gid]["announce"] = announce
    _game_tasks[gid] = asyncio.create_task(match_loop_t(gid, channel))


# ═══════════════════════════════════════════════════════════════
#  段位賽（DM 制、跨伺服器匹配）
# ═══════════════════════════════════════════════════════════════

async def setup_dms(gid: str, gs: GameState, users: dict,
                    greeting: str = "rank.matched") -> None:
    """DM 對局：以每位玩家的 DM 作為私人手牌通道（無公開串）。users: {uid: discord.User}。
    greeting：開場白翻譯鍵（段位賽＝rank.matched；DM 打電腦＝dm.start）。"""
    private: dict[str, discord.abc.Messageable] = {}
    hand_msg: dict[str, discord.Message] = {}
    river_msg: dict[str, discord.Message] = {}
    for p in gs.players:
        if p.is_bot:
            continue
        _lang = i18n.get_user_lang(p.user_id)
        user  = users.get(p.user_id)
        dm    = (user.dm_channel or await user.create_dm()) if user else None
        if dm is None:
            raise RuntimeError(f"無法建立 {p.username} 的 DM 頻道")
        await dm.send(i18n.t(greeting, _lang))
        _apply_skin(p.user_id)
        rm = await dm.send(river_message_text(gs, _lang))
        river_msg[p.user_id] = rm
        hm = await dm.send(make_hand_panel(p, i18n.t("panel.waiting_start", _lang), lang=_lang),
                           view=make_hand_view(gid, _lang))
        private[p.user_id]  = dm
        hand_msg[p.user_id] = hm
    _threads[gid] = {"public": None, "board_msg": None, "is_dm": True,
                     "private": private, "hand_msg": hand_msg, "river_msg": river_msg,
                     "result_msg": None}


async def launch_dm_game(gid: str, user) -> None:
    """DM 休閒對局（跟電腦打）：玩家一人＋AI 補滿，全程走 DM（同段位賽的 DM 制，不計段位）。
    呼叫前 _waiting[gid]（含 AI）、_room_configs[gid]、_room_owners[gid] 需已就緒。"""
    config = _room_configs.get(gid, {})
    info   = _waiting.get(gid, [])
    gs = new_game(gid, info, config.get("is_sanma", False))
    _games[gid] = gs
    for p in gs.players:
        if not p.is_bot:
            _user_game[p.user_id] = gid
    rooms.register(gid, "dm", "dm")
    try:
        db.create_game(gid, "dm", "dm", gs.wall_seed, room_no=rooms.room_no(gid))
        db.update_game_state(gid, "playing", gs.to_dict())
        db.set_room_config(gid, config)
        db.log_action(gid, 0, {
            "t": "gamestart", "mode": "sanma" if gs.is_sanma else "yonma",
            "start": config.get("start_points"),
            "players": {p.user_id: {"seat": p.seat, "name": p.username, "bot": p.is_bot}
                        for p in gs.players},
            "wall": gs.wall_seed,
        })
    except Exception as e:
        print(f"[dm] launch DB 失敗：{e}")
    try:
        await setup_dms(gid, gs, {str(user.id): user}, greeting="dm.start")
    except Exception as e:
        print(f"[dm] setup_dms 失敗：{e}")
        _cleanup(gid, gid)
        return
    rooms.set_status(gid, "playing")
    _game_tasks[gid] = asyncio.create_task(match_loop_t(gid, None))


async def launch_ranked_game(players: list[dict], mode: str) -> None:
    """配對成功 → 開一場段位賽（全真人、DM 制）。players: [{"uid","name","user","lang"}]。"""
    import uuid
    is_sanma = (mode == "sanma")
    gid  = str(uuid.uuid4())[:8]
    info = [{"user_id": p["uid"], "username": p["name"], "is_bot": False} for p in players]
    _waiting[gid]      = info
    _room_configs[gid] = {
        "is_sanma": is_sanma, "thinking_time": 30, "max_players": len(players),
        "length": "hanchan", "tobi": True, "ruleset": "tenhou", "start_points": None,
        "kuikae": False, "open_riichi": False,   # 段位賽：禁食替、無開立直
        "lang": i18n.DEFAULT, "ranked": True, "open_hand": False,
    }
    gs = new_game(gid, info, is_sanma)
    _games[gid] = gs
    for p in gs.players:
        _user_game[p.user_id] = gid
    rooms.register(gid, "dm", "dm")
    try:
        db.create_game(gid, "dm", "dm", gs.wall_seed, room_no=rooms.room_no(gid))
        db.update_game_state(gid, "playing", gs.to_dict())
        db.set_room_config(gid, _room_configs[gid])
        db.log_action(gid, 0, {
            "t": "gamestart", "mode": mode, "start": 35000 if is_sanma else 25000,
            "players": {p.user_id: {"seat": p.seat, "name": p.username, "bot": p.is_bot}
                        for p in gs.players},
            "wall": gs.wall_seed,
        })
    except Exception as e:
        print(f"[rank] launch DB 失敗：{e}")
    users = {p["uid"]: p["user"] for p in players}
    try:
        await setup_dms(gid, gs, users)
    except Exception as e:
        print(f"[rank] setup_dms 失敗：{e}")
        for p in players:           # 失敗 → 通知並清掉狀態
            try:
                await p["user"].send(i18n.t("rank.dm_fail", p.get("lang", i18n.DEFAULT)))
            except Exception:
                pass
        _cleanup(gid, gid)
        return
    rooms.set_status(gid, "playing")
    _game_tasks[gid] = asyncio.create_task(match_loop_t(gid, None))


# ═══════════════════════════════════════════════════════════════
#  重連回復：機器人重啟後，把中斷的對局交給玩家決定繼續／結束
# ═══════════════════════════════════════════════════════════════

_recovery_done = False


async def recover_interrupted_games(bot) -> None:
    """啟動後掃描 DB 中仍進行中的對局（記憶體已無對應任務 → 已中斷），
    在原頻道貼出「繼續／結束」提示。每個行程只跑一次。"""
    global _recovery_done
    if _recovery_done:
        return
    _recovery_done = True
    try:
        games = db.get_unfinished_games()
    except Exception as e:
        print(f"[recover] 讀取未完成對局失敗：{e}")
        return
    for g in games:
        gid = g["game_id"]
        if gid in _games:                      # 仍在記憶體中（短暫重連）→ 不算中斷
            continue
        if g.get("channel_id") == "dm":        # 段位賽 DM 局：跨伺服器無法重建 → 直接結束
            try:
                db.finish_game(gid, g["game_data"])
            except Exception:
                pass
            continue
        try:
            cid = int(g["channel_id"])
            ch = bot.get_channel(cid) or await bot.fetch_channel(cid)
        except Exception:
            ch = None
        if ch is None:
            continue
        try:
            snap = GameState.from_dict(g["game_data"])
        except Exception as e:
            print(f"[recover] {gid} 還原狀態失敗：{e}")
            continue
        human_ids = [p.user_id for p in snap.players if not p.is_bot]
        if not human_ids:                      # 全 AI 局，無人可決定 → 直接結束
            try:
                db.finish_game(gid, g["game_data"])
            except Exception:
                pass
            continue
        try:
            rooms.register_existing(gid, g.get("guild_id"), g["channel_id"], g.get("room_no"))
        except Exception:
            pass
        try:
            db.mark_interrupted(gid)
        except Exception:
            pass
        mentions = " ".join(f"<@{u}>" for u in human_ids)
        try:
            await ch.send(
                f"⚠️ {rooms.label(gid)} 因機器人重新啟動而中斷。{mentions}\n"
                f"要從**目前比分繼續**（中斷的那一手作廢、重新發牌續打），"
                f"還是直接**結束並結算目前順位**？",
                view=RecoveryView(gid, human_ids),
            )
        except Exception as e:
            print(f"[recover] {gid} 發送提示失敗：{e}")


class RecoveryView(discord.ui.View):
    """中斷對局的「繼續／結束」選擇。"""

    def __init__(self, gid: str, human_ids: list[str]):
        super().__init__(timeout=None)
        self.gid = gid
        self.human_ids = {str(u) for u in human_ids}

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if str(interaction.user.id) not in self.human_ids:
            await interaction.response.send_message("只有這局的玩家可以操作。", ephemeral=True)
            return False
        return True

    def _lock(self):
        for c in self.children:
            c.disabled = True

    @discord.ui.button(label="繼續對局", style=discord.ButtonStyle.success, emoji="▶️")
    async def cont(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.gid in _games:
            await interaction.response.send_message("這局已經在進行中了。", ephemeral=True)
            return
        self._lock()
        await interaction.response.edit_message(view=self)
        try:
            await resume_game(self.gid, interaction.channel)
        except Exception as e:
            print(f"[recover] resume_game 失敗：{e}")
            try:
                await interaction.followup.send(f"❌ 無法繼續對局：{e}", ephemeral=True)
            except Exception:
                pass

    @discord.ui.button(label="結束對局", style=discord.ButtonStyle.danger, emoji="🏁")
    async def end(self, interaction: discord.Interaction, button: discord.ui.Button):
        self._lock()
        await interaction.response.edit_message(view=self)
        try:
            await finalize_interrupted(self.gid, interaction.channel)
        except Exception as e:
            print(f"[recover] finalize 失敗：{e}")


def _players_info_from(gs: GameState) -> list[dict]:
    return [{"user_id": p.user_id, "username": p.username, "is_bot": p.is_bot}
            for p in gs.players]


async def resume_game(gid: str, channel: discord.TextChannel) -> None:
    """從 DB 快照接續對局：保留比分、莊家、場局、本場，重新發下一手並重建討論串。"""
    g = db.get_game(gid)
    if not g or g["state"] == "finished":
        return
    snap = GameState.from_dict(g["game_data"])
    config = {}
    try:
        config = json.loads(g.get("room_config") or "{}")
    except Exception:
        pass
    length = config.get("length", "tonpuu")
    tobi = config.get("tobi", True)

    # 若快照已達結束條件，改為直接結算
    if st.is_game_over(snap, length, tobi):
        await finalize_interrupted(gid, channel)
        return

    players_info = _players_info_from(snap)
    _room_configs[gid] = config
    _waiting[gid] = players_info
    try:
        rooms.register_existing(gid, g.get("guild_id"), g["channel_id"], g.get("room_no"),
                                status="playing")
    except Exception:
        pass

    gs = deal_next_hand(gid, players_info, snap)   # 中斷那手作廢，發下一手（比分照舊）
    _games[gid] = gs
    for p in gs.players:
        _user_game[p.user_id] = gid
    _channel_games[str(channel.id)] = gid
    db.update_game_state(gid, "playing", gs.to_dict())

    try:
        await setup_threads(gid, channel, gs, watch=config.get("open_hand", False))
    except Exception as e:
        await channel.send(f"❌ 重建討論串失敗：{e}")
        _cleanup(gid, str(channel.id))
        return

    announce = await channel.send(
        f"🀄 {rooms.label(gid)} 已從中斷處接續！牌桌在 {_threads[gid]['public'].mention}，"
        f"真人玩家請到自己的私人討論串出牌。"
    )
    _threads[gid]["announce"] = announce
    _game_tasks[gid] = asyncio.create_task(match_loop_t(gid, channel))


async def finalize_interrupted(gid: str, channel: discord.TextChannel) -> None:
    """直接結束中斷對局：以目前比分結算並公布最終順位。"""
    g = db.get_game(gid)
    if not g or g["state"] == "finished":
        return
    gs = GameState.from_dict(g["game_data"])
    config = {}
    try:
        config = json.loads(g.get("room_config") or "{}")
    except Exception:
        pass
    rows = st.final_standings(gs, start_points=config.get("start_points"),
                              length=config.get("length", "hanchan"),
                              ruleset=config.get("ruleset", "mixed"))
    medals = ["🥇", "🥈", "🥉", "4️⃣"]
    lines = ["# 🏁 最終順位（對局中斷結算）", ""]
    for r in rows:
        sign = "＋" if r.total_pt >= 0 else "－"
        lines.append(f"{medals[r.rank - 1]} **第 {r.rank} 位**　{r.username}"
                     f"　{r.score} 點　｜　精算 {sign}{abs(r.total_pt):.1f}")
    try:
        await channel.send("\n".join(lines))
    except Exception as e:
        print(f"[recover] 發送最終順位失敗：{e}")
    try:
        db.finish_game(gid, gs.to_dict())
    except Exception as e:
        print(f"[recover] finish_game 失敗：{e}")
    rooms.unregister(gid)


# ═══════════════════════════════════════════════════════════════
#  Lobby View  (with AI button)
# ═══════════════════════════════════════════════════════════════
