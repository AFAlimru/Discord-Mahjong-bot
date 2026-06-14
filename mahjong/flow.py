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
    is_furiten, ai_should_ron, is_menzen,
)
from .render import (
    make_thread_board, make_hand_panel, _board_info, _action_feed,
    _log_action, result_body, dora_reveal_text,
)
from .ui import (
    HandHelpButton, RiverButton, ScoreButton, MeldButton, ActionLogButton,
    make_hand_view, FairnessButton,
)
from .state import (
    _games, _channel_games, _waiting, _room_owners, _room_configs, _user_game,
    _game_tasks, _lobbies, _threads, _input_queues, _input_thread, _thread_game,
    _action_logs, _bg_tasks,
)
from . import settlement as st
from . import db
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


async def setup_threads(gid: str, channel: discord.TextChannel, gs: GameState,
                        watch: bool = False) -> None:
    """建立公開討論串 + 每位真人玩家的私人討論串。
    watch=True（觀戰）：公開串當牌桌；否則當聊天/和牌資訊串（牌桌資訊改用按鈕看）。"""
    if watch:
        public = await channel.create_thread(
            name=f"🀄 {rooms.label(gid)}　{gs.round_label}",
            type=discord.ChannelType.public_thread,
        )
        board_msg = await public.send(make_thread_board(gs, "🀄 遊戲開始，輪到莊家。"))
    else:
        public = await channel.create_thread(
            name=f"💬 {rooms.label(gid)}　{gs.round_label}",
            type=discord.ChannelType.public_thread,
        )
        await public.send("💬 這裡會顯示每局和牌／流局結果，也可以自由聊天。")
        board_msg = None

    private: dict[str, discord.Thread] = {}
    hand_msg: dict[str, discord.Message] = {}
    for p in gs.players:
        if p.is_bot:
            continue
        try:
            _rn = rooms.room_no(gid)
            _suffix = f" #{_rn:04d}" if _rn else ""
            pt = await channel.create_thread(
                name=f"🀫 {p.username} 的手牌{_suffix}",
                type=discord.ChannelType.private_thread,
                invitable=False,
            )
            try:
                await pt.add_user(discord.Object(id=int(p.user_id)))
            except Exception:
                pass
            private[p.user_id] = pt
            hm = await pt.send(make_hand_panel(p, "（等待開始）", board_info=_board_info(gs)),
                               view=make_hand_view(gid))
            hand_msg[p.user_id] = hm
        except Exception as e:
            print(f"[threads] 建立 {p.username} 私人討論串失敗：{e}")

    _threads[gid] = {
        "public": public, "board_msg": board_msg,
        "private": private, "hand_msg": hand_msg,
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


async def _delete_threads(th: dict) -> None:
    """刪除對局的所有討論串與開局公告（用於 /end 強制結束）。"""
    await _delete_announce(th)
    threads = [th.get("public")] + list(th.get("private", {}).values())
    for t in threads:
        if t is None:
            continue
        try:
            await t.delete()
        except Exception:
            pass


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
    if low in ("tsumo", "自摸", "zimo"):
        return (True, ("tsumo", None), "") if can_tsumo else (False, None, "現在無法自摸")
    if low in ("!n", "n!", "拔北", "kita"):
        return (True, ("kita", None), "") if kita_ok else (False, None, "現在無法拔北")
    if low.startswith(("riichi", "reach")) or s.startswith("立直"):
        rest = s
        for kw in ("riichi", "reach", "立直", "RIICHI", "Reach"):
            rest = rest.replace(kw, "")
        rest = rest.strip()
        if not can_riichi:
            return (False, None, "現在無法立直")
        t = parse_tile(rest, player.hand, player.drawn_tile)
        if not t:
            return (False, None, "立直需指定要打的牌，例如：立直 5m")
        test = list(player.hand)
        dt = player.drawn_tile
        if t == dt:
            dt = None
        elif t in test:
            test.remove(t)
        if not is_tenpai(test + ([dt] if dt else [])):
            return (False, None, "打出該牌後未聽牌，不能立直")
        return (True, ("riichi", t), "")
    if s.startswith(("暗槓", "暗槓")) or low.startswith("ankan"):
        rest = s
        for kw in ("暗槓", "暗槓", "ankan", "ANKAN"):
            rest = rest.replace(kw, "")
        rest = rest.strip()
        t = parse_tile(rest, player.hand, player.drawn_tile) if rest else (ankan_opts[0] if ankan_opts else None)
        if t and any(o.suit == t.suit and o.value == t.value for o in ankan_opts):
            return (True, ("ankan", t), "")
        return (False, None, "無法暗槓該牌")
    t = parse_tile(s, player.hand, player.drawn_tile)
    if t:
        return (True, ("discard", t), "")
    return (False, None, f"看不懂「{raw}」，請打出要丟的牌（如 5m、東、中）")


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
        fut = asyncio.get_event_loop().create_future()
        view = discord.ui.View(timeout=thinking_time + 5)

        def add_btn(label, style, choice, extra):
            b = discord.ui.Button(label=label, style=style)
            async def cb(inter):
                if str(inter.user.id) != uid:
                    await inter.response.send_message("❌ 不是你的回應", ephemeral=True)
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

        # 順序：吃、碰、槓、榮和、跳過
        if "chi" in actions:
            for idx, (t1, t2) in enumerate(chi_opts):
                lbl = "吃" if len(chi_opts) == 1 else f"吃{CIRC[idx]}"
                add_btn(lbl, discord.ButtonStyle.success, "chi", (t1, t2))  # 綠
        if "pon" in actions:
            add_btn("碰", discord.ButtonStyle.primary, "pon", None)         # 藍
        if "kan" in actions:
            add_btn("槓", discord.ButtonStyle.primary, "kan", None)         # 藍
        if "ron" in actions:
            add_btn("榮和", discord.ButtonStyle.danger, "ron", None)        # 紅
        add_btn("跳過", discord.ButtonStyle.secondary, "skip", None)        # 灰

        def prompt_text(rem):
            lines = [f"## ⬇️ {from_name} 打出", f"# {discard_tile}"]
            if len(chi_combos) == 1:
                lines.append(f"# 吃 {chi_combos[0]}")
            else:
                for idx, cmb in enumerate(chi_combos):
                    lines.append(f"# 吃{CIRC[idx]} {cmb}")
            lines.append(f"請選擇（剩 {rem} 秒）")
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
    results.sort(key=lambda x: x[0])
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
        fut = asyncio.get_event_loop().create_future()
        view = discord.ui.View(timeout=thinking_time + 5)

        def add_btn(label, style, choice):
            b = discord.ui.Button(label=label, style=style)
            async def cb(inter):
                if str(inter.user.id) != uid:
                    await inter.response.send_message("❌ 不是你的回應", ephemeral=True)
                    return
                await inter.response.defer()
                if not fut.done():
                    fut.set_result(choice)
            b.callback = cb
            view.add_item(b)

        add_btn("搶槓榮和", discord.ButtonStyle.danger, "ron")
        add_btn("跳過", discord.ButtonStyle.secondary, "skip")

        def prompt_text(rem):
            return (f"## 🀄 {kan_name} 宣告加槓\n# {kan_tile}\n"
                    f"可**搶槓榮和**！請選擇（剩 {rem} 秒）")

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
        m = await thread.send(f"<@{uid}> 輪到你了！")
        await asyncio.sleep(0.6)
        await m.delete()
    except Exception:
        pass


async def wait_turn_action(gid, player, pt, hand_msg, thinking_time,
                           can_tsumo, can_riichi, kita_ok, ankan_opts,
                           prompt_base, tenpai_note, last_info="", board_info="",
                           riichi_locked=False, kakan_opts=None):
    """輪到玩家：自摸/立直/暗槓/拔北用按鈕，出牌用打字。回傳 (action, arg) 或 None（逾時）。
    riichi_locked=True（已立直）：鎖手，打字無效，只能按自摸或逾時自動摸切。"""
    uid   = player.user_id
    fut   = asyncio.get_event_loop().create_future()
    state = {"riichi": False, "rem": int(thinking_time)}
    view  = discord.ui.View(timeout=float(thinking_time) + 10)
    # 資訊按鈕：看牌河 / 看點數 / 看副露（輪到自己時也能看）；說明最後加（最右）
    view.add_item(RiverButton(gid))
    view.add_item(ScoreButton(gid))
    view.add_item(MeldButton(gid))
    view.add_item(ActionLogButton(gid))

    def panel(rem):
        if riichi_locked:
            extra = f"🀄 **已立直**：將自動摸切，可按「自摸」　（剩 {rem} 秒）"
        elif state["riichi"]:
            extra = "🀄 已宣告立直，請**打字**打出宣言牌（再按一次「立直」可取消）"
        else:
            extra = f"{prompt_base}　（剩 {rem} 秒）"
        return make_hand_panel(player, extra, tenpai_note, last_info, board_info)

    async def refresh(rem):
        try:
            await hand_msg.edit(content=panel(rem), view=view)
        except Exception:
            pass

    def add_btn(label, style, kind):
        b = discord.ui.Button(label=label, style=style)
        async def cb(inter):
            if str(inter.user.id) != uid:
                await inter.response.send_message("❌ 不是你的回合", ephemeral=True)
                return
            await inter.response.defer()
            if kind == "riichi":
                state["riichi"] = not state["riichi"]   # 再按一次取消立直
                await refresh(state["rem"])
            elif not fut.done():
                fut.set_result(kind)
        b.callback = cb
        view.add_item(b)

    # 順序：說明、摸切、拔北、暗槓、加槓、立直、自摸
    view.add_item(HandHelpButton())   # 說明
    if player.drawn_tile is not None:
        add_btn("摸切", discord.ButtonStyle.secondary, ("discard", player.drawn_tile))
    if kita_ok:
        add_btn("拔北", discord.ButtonStyle.secondary, ("kita", None))
    if ankan_opts:
        add_btn("暗槓", discord.ButtonStyle.secondary, ("ankan", ankan_opts[0]))
    if kakan_opts:
        add_btn("加槓", discord.ButtonStyle.secondary, ("kakan", kakan_opts[0]))
    if can_riichi:
        add_btn("立直", discord.ButtonStyle.secondary, "riichi")
    if can_tsumo:
        add_btn("自摸", discord.ButtonStyle.danger, ("tsumo", None))       # 紅

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
                await _warn(pt, "立直中只能摸切（可按「自摸」），無法換牌或副露")
                continue
            if state["riichi"]:
                t = parse_tile(raw, player.hand, player.drawn_tile)
                if not t:
                    await _warn(pt, f"看不懂「{raw}」")
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
                await _warn(pt, "打出該牌後未聽牌，不能立直")
            else:
                ok, val, err = _parse_turn_input(raw, player, can_tsumo, can_riichi, kita_ok, ankan_opts)
                if ok:
                    if not fut.done():
                        fut.set_result(val)
                    return
                await _warn(pt, err)

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
            await hand_msg.edit(view=make_hand_view(gid))   # 回合結束保留說明/看牌河
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
    _action_logs[gid] = []   # 每局開始清空動作記錄

    async def refresh_feeds():
        """把最新動態同步到每位玩家的手牌面板，讓大家一直看得到（非自己回合也更新）。"""
        feed = _action_feed(gid, gs)
        bi = _board_info(gs)
        for p in gs.players:
            if p.is_bot:
                continue
            hm = hand_msg.get(p.user_id)
            if hm:
                try:
                    await hm.edit(content=make_hand_panel(p, "", "", feed, bi),
                                  view=make_hand_view(gid))
                except Exception:
                    pass

    async def render_board(status="", log=True):
        if log:
            _log_action(gid, status)   # 真人對局沒有牌桌，動作改記在私人面板上方
            await refresh_feeds()      # 每筆動作即時更新所有玩家面板的動態
        if board_msg is None:   # 真人對局沒有公開牌桌（資訊改用按鈕看）
            return
        try:
            await board_msg.edit(content=make_thread_board(gs, status))
        except Exception:
            pass

    async def render_hand(p, prompt="", tenpai_note=""):
        hm = hand_msg.get(p.user_id)
        if hm:
            try:
                await hm.edit(content=make_hand_panel(p, prompt, tenpai_note,
                                                      _action_feed(gid, gs), _board_info(gs)),
                              view=make_hand_view(gid))
            except Exception:
                pass

    furiten_perm = {p.seat: False for p in gs.players}
    temp_furiten = {p.seat: False for p in gs.players}
    ippatsu      = {p.seat: False for p in gs.players}
    double_rii   = {p.seat: False for p in gs.players}
    any_call     = False
    rinshan_next = False
    no_draw      = False

    while True:
        if _games.get(gid) is not gs:
            return None
        player = gs.players[gs.current_seat]
        is_rinshan_draw = False
        temp_furiten[player.seat] = False

        if no_draw:
            no_draw = False
            player.drawn_tile = None
        else:
            tile = gs.draw_tile()
            if tile is None:
                tenpai_seats = [p.seat for p in gs.players if is_tenpai(p.hand)]
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
                await render_board(f"🎉 🤖 {player.username} 自摸！")
                return ("tsumo", player.seat, res, hs)
            if gs.is_sanma and has_kita(player):
                if player.drawn_tile is not None:
                    player.hand.append(player.drawn_tile); player.drawn_tile = None
                for i, t in enumerate(player.hand):
                    if t.suit == Suit.WIND and t.value == 4:
                        player.hand.pop(i); break
                player.kita += 1
                await render_board(f"🤖 {player.username} 拔北 ×{player.kita}")
                await asyncio.sleep(0.6)
                continue
            drawn = player.drawn_tile
            if player.drawn_tile is not None:
                player.hand.append(player.drawn_tile); player.drawn_tile = None
            discard_tile = ai_choose_discard(player.hand)
            if discard_tile is None:
                return ("draw", [p.seat for p in gs.players if is_tenpai(p.hand)])
            player.hand.remove(discard_tile)
            player.discards.append(discard_tile)
            giri = "摸切" if (drawn is not None and discard_tile == drawn) else "手切"
            nxt = _name_ref(gs.players[(player.seat + 1) % len(gs.players)])
            await render_board(f"🤖 {player.username} 出牌「{discard_tile}」（{giri}），輪到 {nxt}")
            await asyncio.sleep(1.0)

        # ── Human（打字）────────────────────────────────────
        else:
            already_riichi = player.riichi
            can_tsumo  = bool(player.drawn_tile) and evaluate_win(
                gs, player, player.drawn_tile, is_tsumo=True, is_rinshan=is_rinshan_draw) is not None

            if already_riichi:
                # 立直後：鎖手，只能摸切或自摸，不可立直／暗槓／加槓／拔北
                adv = []
                can_riichi  = False
                ankan_opts  = []
                kakan_opts  = []
                kita_ok     = False
                tenpai_note = ""
                prompt_base = "🀄 **已立直**：自動摸切（可自摸）"
                turn_time   = thinking_time if can_tsumo else 3
            else:
                adv = tenpai_advice(player)   # 14 張時：打哪張可進聽
                can_riichi = is_menzen(player) and bool(adv)
                ankan_opts = get_ankan_options(player.hand + ([player.drawn_tile] if player.drawn_tile else []))
                kakan_opts = get_shouminkan_options(player)   # 加槓：已碰且持有第 4 張
                kita_ok    = gs.is_sanma and has_kita(player)
                # 進聽提示：打哪張可聽、聽哪些；並標註（無役）／（振聽）
                tenpai_note = tenpai_note_text(gs, player, adv) if adv else ""
                prompt_base = "✍️ **打字**丟牌；其他行動請看「說明」"
                turn_time   = thinking_time

            await render_board(f"輪到 <@{player.user_id}>（{WIND_LABELS[player.seat]}）出牌", log=False)

            pt = private.get(player.user_id)
            hm = hand_msg.get(player.user_id)
            ping_msg = None
            # 立直後是自動摸切，不必每巡 @；只有能自摸時才提醒
            if pt and (not already_riichi or can_tsumo):
                try:
                    ping_msg = await pt.send(f"<@{player.user_id}> 輪到你了！")
                except Exception:
                    pass
            result = None
            if pt and hm:
                result = await wait_turn_action(
                    gid, player, pt, hm, turn_time,
                    can_tsumo, can_riichi, kita_ok, ankan_opts,
                    prompt_base, tenpai_note, _action_feed(gid, gs), _board_info(gs),
                    riichi_locked=already_riichi, kakan_opts=kakan_opts,
                )
            else:
                await render_hand(player, prompt_base, tenpai_note)
            if ping_msg:   # 出完牌（行動結束）才刪掉提醒
                try:
                    await ping_msg.delete()
                except Exception:
                    pass

            timed = result is None
            action, arg = ("discard", None) if timed else result

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
                    await render_board(f"🎉 {player.username} 自摸！")
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
                await render_board(f"{player.username} 拔北 ×{player.kita}")
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
                for s in ippatsu:
                    ippatsu[s] = False
                rinshan_next = True
                await render_hand(player)
                await render_board(f"{player.username} 暗槓 {kan_tile}")
                continue

            # ── 加槓（小明槓）+ 搶槓 ──
            if action == "kakan" and arg is not None:
                kan_tile = arg
                await render_board(f"{player.username} 宣告加槓 {kan_tile}…（搶槓確認中）")
                robber = await collect_chankan_t(
                    gs, gid, kan_tile, player.seat, min(thinking_time, 12),
                    furiten_perm, temp_furiten,
                )
                if robber:
                    rp = next(p for p in gs.players if p.user_id == robber)
                    res = evaluate_win(
                        gs, rp, kan_tile, is_tsumo=False, is_chankan=True,
                        is_ippatsu=ippatsu[rp.seat], is_double_riichi=double_rii[rp.seat],
                    )
                    if res:
                        hs = format_winning_hand(rp, kan_tile)
                        await render_board(f"🎉 {rp.username} 搶槓！榮和 {player.username} 加槓的 {kan_tile}")
                        return ("ron", rp.seat, player.seat, res, hs)
                # 放過搶槓 → 同巡振聽（立直者永久）
                _wk = (int(kan_tile.suit), kan_tile.value)
                for _p in gs.players:
                    if _p.seat != player.seat and _wk in hand_waits(_p):
                        temp_furiten[_p.seat] = True
                        if _p.riichi:
                            furiten_perm[_p.seat] = True
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
                for s in ippatsu:
                    ippatsu[s] = False
                rinshan_next = True
                await render_hand(player)
                await render_board(f"{player.username} 加槓 {kan_tile}！摸嶺上牌。")
                continue

            # ── 出牌 / 立直 ──
            if timed or arg is None:
                discard_tile = player.drawn_tile if player.drawn_tile else player.hand[-1]
            else:
                discard_tile = arg
            drawn = player.drawn_tile
            giri = "摸切" if (drawn is not None and discard_tile == drawn) else "手切"

            if action == "riichi" and not player.riichi:
                player.riichi = True
                gs.riichi_sticks += 1
                player.score -= 1000
                double_rii[player.seat] = (not any_call) and (len(player.discards) == 0)
                ippatsu[player.seat] = True
                word = "宣告立直！"
            elif timed:
                word = "超時自動打出"
            else:
                word = "出牌了"

            if player.drawn_tile is not None:
                player.hand.append(player.drawn_tile); player.drawn_tile = None
            if discard_tile in player.hand:
                player.hand.remove(discard_tile)
            player.discards.append(discard_tile)
            if ipp_start:
                ippatsu[player.seat] = False

            post_note = ""
            if is_tenpai(player.hand) and not player.riichi:
                waits = get_tenpai_waits(player.hand)
                post_note = f"🀄 **聽牌！** 待牌：{' '.join(str(t) for t in waits)}"
            await render_hand(player, "", post_note)

            nxt = _name_ref(gs.players[(player.seat + 1) % len(gs.players)])
            if already_riichi:
                await render_board(f"{player.username} 立直摸切「{discard_tile}」，輪到 {nxt}")
            else:
                await render_board(f"{player.username} {word}「{discard_tile}」（{giri}），輪到 {nxt}")

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

        if reaction:
            rtype, r_uid, extra = reaction
            rp = next((p for p in gs.players if p.user_id == r_uid), None)
            from_name = player.username
            if rtype == "ron" and rp:
                res = evaluate_win(gs, rp, discard_tile, is_tsumo=False,
                                   is_ippatsu=ippatsu[rp.seat], is_double_riichi=double_rii[rp.seat])
                if res:
                    hs = format_winning_hand(rp, discard_tile)
                    await render_board(f"🎉 {rp.username} 榮和 {from_name} 的 {discard_tile}！")
                    return ("ron", rp.seat, player.seat, res, hs)
                await render_board(f"❌ {rp.username} 榮和無效")
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
                await render_board(f"{rp.username} 碰了 {from_name} 的 {discard_tile}！請出牌。")
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
                await render_board(f"{rp.username} 吃了 {from_name} 的 {discard_tile}！請出牌。")
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
                for s in ippatsu:
                    ippatsu[s] = False
                rinshan_next = True
                await render_hand(rp)
                await render_board(f"{rp.username} 槓了 {from_name} 的 {discard_tile}！摸嶺上牌。")
                continue

        gs.current_seat = (gs.current_seat + 1) % len(gs.players)


async def match_loop_t(gid: str, channel: discord.TextChannel) -> None:
    """0.2：討論串版多局對戰。"""
    config       = _room_configs.get(gid, {})
    length       = config.get("length", "tonpuu")
    tobi         = config.get("tobi", True)
    players_info = _waiting.get(gid, [])
    channel_id   = str(channel.id)
    th           = _threads[gid]
    public       = th["public"]
    tsumo_ct  = Counter()   # 自摸次數
    ron_ct    = Counter()   # 榮和次數
    riichi_ct = Counter()   # 立直次數
    houju_ct  = Counter()   # 放銃次數
    houju_pts = Counter()   # 放銃失點
    gain_pts  = Counter()   # 累計獲得點數（每局正向點數變動之和）
    shown_all_last = False
    end_wind = 1 if length == "tonpuu" else 2   # 1=東風戰 2=半莊

    try:
        while True:
            outcome = await play_hand_t(gid, channel)
            if outcome is None:
                return
            gs = _games[gid]
            tenpai = None
            header = ""
            hand_str = ""

            if outcome[0] == "tsumo":
                _, wseat, result, hand_str = outcome
                log = st.apply_tsumo(gs, wseat, result)
                header = f"{gs.players[wseat].username}　自摸！"
                tsumo_ct[gs.players[wseat].user_id] += 1
                st.advance_after_win(gs, wseat)
            elif outcome[0] == "ron":
                _, wseat, lseat, result, hand_str = outcome
                log = st.apply_ron(gs, wseat, lseat, result)
                header = f"{gs.players[wseat].username}　榮和！（放銃：{gs.players[lseat].username}）"
                ron_ct[gs.players[wseat].user_id] += 1
                houju_ct[gs.players[lseat].user_id] += 1
                houju_pts[gs.players[lseat].user_id] += max(0, -log.deltas.get(lseat, 0))
                st.advance_after_win(gs, wseat)
            elif outcome[0] == "nagashi":
                _, nagashi_seats, tenpai = outcome
                log = st.apply_nagashi(gs, nagashi_seats)
                from .scoring import ScoreResult
                winner = gs.players[nagashi_seats[0]]
                pts = 12000 if winner.is_dealer else 8000
                result = ScoreResult(yaku=[("流局滿貫", 5)], han=5, fu=0,
                                     points=pts, name="流局滿貫", valid=True)
                names = "、".join(gs.players[s].username for s in nagashi_seats)
                header = f"🌊 流局滿貫！{names}"
                hand_str = "　".join(str(t) for t in winner.discards)
                for s in nagashi_seats:
                    tsumo_ct[gs.players[s].user_id] += 1
                st.advance_after_draw(gs, tenpai)
            else:
                _, tenpai = outcome
                log = st.apply_ryuukyoku(gs, tenpai)
                result = None
                st.advance_after_draw(gs, tenpai)
            # 本局獲得點數（正向變動）累加
            for s, d in log.deltas.items():
                if d > 0:
                    gain_pts[gs.players[s].user_id] += d
            # 本局立直者（換局前 riichi 旗標仍在）
            for p in gs.players:
                if p.riichi:
                    riichi_ct[p.user_id] += 1

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
            if result is not None:
                channels = [public] + list(th.get("private", {}).values())
                cer = await asyncio.gather(
                    *[win_ceremony(ch, gs, header, hand_str, result, log) for ch in channels],
                    return_exceptions=True,
                )
                all_msgs, result_body_text = [], ""
                for c in cer:
                    if isinstance(c, Exception):
                        continue
                    m, b = c
                    all_msgs.append(m)
                    result_body_text = b
                result_msg = all_msgs[0] if all_msgs else None
                priv_msgs  = all_msgs[1:]
            else:
                result_body_text = result_body("", "", None, log, gs, tenpai)
                result_msg = await public.send(result_body_text)
                priv_msgs = []
                for pt in th.get("private", {}).values():
                    try:
                        priv_msgs.append(await pt.send(result_body_text))
                    except Exception:
                        pass

            # 全部顯示完 → 保留完整結果，倒數 5 秒；最後一局改顯示「結束對局」
            over = st.is_game_over(gs, length, tobi)
            await _result_countdown([result_msg, *priv_msgs], result_body_text, 5,
                                    "結束對局" if over else "進入下一局")
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
                for tch in [public] + list(th.get("private", {}).values()):
                    if tch is None:
                        continue
                    try:
                        banners.append(await tch.send("# 🏁 ALL LAST　最終局"))
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
            if th.get("board_msg"):
                try:
                    await th["board_msg"].edit(
                        content=make_thread_board(new_gs, f"🀄 {new_gs.round_label} 開始，輪到莊家。"))
                except Exception:
                    pass
            for p in new_gs.players:
                if p.is_bot:
                    continue
                pt = th["private"].get(p.user_id)
                if not pt:
                    continue
                try:
                    th["hand_msg"][p.user_id] = await pt.send(
                        make_hand_panel(p, board_info=_board_info(new_gs)),
                        view=make_hand_view(gid))
                except Exception:
                    pass
            await asyncio.sleep(1)

        gs = _games[gid]
        rows = st.final_standings(gs, start_points=config.get("start_points"))
        medals = ["🥇", "🥈", "🥉", "4️⃣"]
        lines = ["# 🏁 最終順位", ""]
        for r in rows:
            sign = "＋" if r.total_pt >= 0 else "－"
            lines.append(f"{medals[r.rank - 1]} **第 {r.rank} 位**　{r.username}"
                         f"　{r.score} 點　｜　精算 {sign}{abs(r.total_pt):.1f}")
        # 先把最終順位貼出來（公開串 + 各私人手牌串都貼，確保玩家在自己的討論串也看得到；
        # 即使後續寫 DB 出錯也不影響顯示）
        standings_text = "\n".join(lines)
        for tch in [public] + list(th.get("private", {}).values()):
            if tch is None:
                continue
            try:
                v = discord.ui.View(timeout=None)
                v.add_item(FairnessButton(gid))
                await tch.send(standings_text, view=v)
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
        if th.get("board_msg") is not None:
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
    return "　".join(parts) + f"　🟦[{win_tile}]"


async def win_ceremony(channel: discord.TextChannel, gs: GameState,
                       header: str, hand_str: str, result, log: "st.SettleLog") -> None:
    """和牌儀式：先放標題，再放手牌，接著逐一揭曉役種，最後公布等級與點數。"""
    head = f"# 🎉 {header}"
    msg = await channel.send(head)          # ① 先只放榮和／自摸標題
    await asyncio.sleep(1.0)
    # 立直則一併揭曉裏寶牌
    names = [n for n, *_ in (result.yaku or [])] + [n for n, *_ in (result.yakuman or [])]
    is_riichi = any("立直" in (n or "") for n in names)
    top = f"{head}\n{dora_reveal_text(gs, is_riichi)}\n## {hand_str}"
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
        shown.append(f"・**{name}**" if han is None else f"・{name}　{han}飜")
        try:
            await msg.edit(content=top + "\n" + "\n".join(shown))
        except Exception:
            pass
        await asyncio.sleep(0.9)

    await asyncio.sleep(0.4)
    if result.yakuman:
        score_line = f"## ✨ {result.name}　{result.points} 點"
    else:
        nm = f"　{result.name}" if result.name else ""
        score_line = f"## {result.han} 飜 {result.fu} 符{nm}　{result.points} 點"
    body = top + "\n" + "\n".join(shown) + f"\n\n{score_line}\n\n" + log.describe(gs)
    try:
        await msg.edit(content=body)
    except Exception:
        pass
    return msg, body


async def _result_countdown(msgs: list, base: str, secs: int = 5,
                            tail: str = "進入下一局") -> None:
    """一局結果全部顯示完後，保留完整結果並於尾端倒數；tail 為倒數後的動作描述。"""
    for n in range(secs, 0, -1):
        for m in msgs:
            if not m:
                continue
            try:
                await m.edit(content=f"{base}\n\n⏳ {n} 秒後{tail}…")
            except Exception:
                pass
        await asyncio.sleep(1)
    # 倒數結束後還原為乾淨結果（保留下來的公開紀錄不會殘留倒數字樣）
    for m in msgs:
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
    rows = st.final_standings(gs, start_points=config.get("start_points"))
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
