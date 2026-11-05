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
"""
settlement.py — 對局結算與金流

負責「和牌之後的點數移動」與「賽末積分馬點」：
  - 榮和：放銃者支付（含本場 +300×本場）、贏家收立直棒
  - 自摸：各家依莊閒分攤（含本場 +100×本場/家）、贏家收立直棒
  - 流局：聽牌罰符（聽牌者平分 3000，未聽牌者分攤）、立直棒留到下局
  - 連莊/輪莊、本場數、場次推進
  - 賽末順位與馬點積分（依官網公式 (結算點數-初始點數)/1000 + 馬點）

所有函式直接操作 mahjong_engine.GameState / PlayerState。
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import Optional

from .engine import GameState, PlayerState
from .scoring import ScoreResult, _round_up


# ─── 和牌結算 ───────────────────────────────────────────────────────────────

@dataclass
class SettleLog:
    """一次結算的點數變動明細，用於顯示。"""
    deltas: dict[int, int]          # seat -> 點數變化
    note: str = ""

    def describe(self, gs: GameState) -> str:
        # 依結算後分數標註目前順位 Emoji（同分依座次排序）
        order  = sorted(gs.players, key=lambda x: (-x.score, x.seat))
        rank   = {p.seat: i for i, p in enumerate(order)}
        medals = ["🥇", "🥈", "🥉", "4️⃣"]
        lines = []
        for p in gs.players:
            d = self.deltas.get(p.seat, 0)
            sign = "＋" if d >= 0 else "－"
            m = medals[rank[p.seat]] if rank[p.seat] < len(medals) else ""
            lines.append(f"{m} {p.username}：{sign}{abs(d)}　→　{p.score}")
        return "\n".join(lines)


def apply_ron(gs: GameState, winner_seat: int, loser_seat: int,
              result: ScoreResult) -> SettleLog:
    """榮和結算。"""
    deltas = {p.seat: 0 for p in gs.players}
    honba_bonus = 300 * gs.honba

    # 一般：放銃者付全額；有包牌（且放銃者不是責任者）時責任者分擔，見 _ron_pays
    for payer, amt in _ron_pays(gs, winner_seat, loser_seat, result, honba_bonus).items():
        deltas[winner_seat] += amt
        deltas[payer] -= amt

    # 立直棒全收
    stick = gs.riichi_sticks * 1000
    deltas[winner_seat] += stick
    gs.riichi_sticks = 0

    _apply_deltas(gs, deltas)
    return SettleLog(deltas, note=f"{result.name} {result.describe()}")


def apply_ron_multi(gs: GameState, winners: list[tuple[int, ScoreResult]],
                    loser_seat: int) -> SettleLog:
    """雙榮（ダブロン）／三響：同一張捨牌被兩家以上榮和，放銃者分別支付每一家。
    winners：[(座位, ScoreResult), …]（人數不限），第一位為頭跳側（依放銃者下家順序；收本場棒與立直棒）。"""
    deltas = {p.seat: 0 for p in gs.players}
    honba_bonus = 300 * gs.honba
    for i, (wseat, res) in enumerate(winners):
        # 本場僅付頭跳側一次；包牌時由責任者分擔（同 apply_ron）
        for payer, amt in _ron_pays(gs, wseat, loser_seat, res, honba_bonus if i == 0 else 0).items():
            deltas[wseat] += amt
            deltas[payer] -= amt
    # 立直棒全歸頭跳側
    stick = gs.riichi_sticks * 1000
    deltas[winners[0][0]] += stick
    gs.riichi_sticks = 0
    _apply_deltas(gs, deltas)
    note = "／".join(f"{r.name} {r.describe()}" for _, r in winners)
    return SettleLog(deltas, note=note)


def apply_tsumo(gs: GameState, winner_seat: int, result: ScoreResult) -> SettleLog:
    """自摸結算。"""
    deltas = {p.seat: 0 for p in gs.players}
    winner = gs.players[winner_seat]
    honba_each = 100 * gs.honba

    pays: dict[int, int] = {}       # 各家本來要付的（不含本場）
    honbas: dict[int, int] = {}     # 各家的本場
    for p in gs.players:
        if p.seat == winner_seat:
            continue
        if winner.is_dealer or not p.is_dealer:
            pays[p.seat] = result.tsumo_each_payment
        else:
            pays[p.seat] = result.tsumo_dealer_payment
        honbas[p.seat] = honba_each
    _pao_tsumo(gs, winner_seat, result, pays, honbas)   # 包牌：責任者代付該役滿的份與全部本場

    total = 0
    for seat, amt in pays.items():
        pay = amt + honbas[seat]
        deltas[seat] -= pay
        total += pay

    deltas[winner_seat] += total

    stick = gs.riichi_sticks * 1000
    deltas[winner_seat] += stick
    gs.riichi_sticks = 0

    _apply_deltas(gs, deltas)
    return SettleLog(deltas, note=f"{result.name} {result.describe()}")


# ─── 包牌（責任支付）────────────────────────────────────────────────────────

def attach_pao(result: ScoreResult, liab: dict, double_yakuman: bool, loser_seat: int | None = None) -> ScoreResult:
    """把這個贏家的包牌紀錄掛到和牌結果上（result.pao）。
    liab＝{役滿名: 責任者座位}（流程在碰／大明槓成立時記下）；只掛上這手牌真的有的役滿。
    該役滿的底點＝8000×倍數（倍數照規則：double_yakuman 才算雙倍，否則一律單倍）。
    榮和時放銃者就是責任者＝一般計算，不掛（loser_seat＝放銃者／被搶槓者；自摸傳 None）。"""
    result.pao = []
    if not liab:
        return result
    for name, mult in result.yakuman:
        seat = liab.get(name)
        if seat is None or seat == loser_seat:
            continue
        result.pao.append((name, seat, 8000 * (mult if double_yakuman else 1)))
    return result


def _ron_pays(gs: GameState, winner_seat: int, loser_seat: int, result: ScoreResult,
              honba_bonus: int) -> dict[int, int]:
    """榮和各家要付給贏家的金額 {座位: 金額}（含本場）。
    沒有包牌：放銃者付 ron_payment＋本場。
    有包牌（放銃者≠責任者）：包牌役滿的部分由責任者與放銃者各付一半，其餘照常由放銃者付；
    本場由責任者付（有多個包牌時算在第一個責任者）。"""
    pays = {loser_seat: result.ron_payment}
    honba_payer = loser_seat
    mult = 6 if gs.players[winner_seat].is_dealer else 4
    for _name, liable, base in result.pao:
        if liable == loser_seat or liable == winner_seat:
            continue
        part = _round_up(base * mult, 100)
        pays[loser_seat] -= part // 2
        pays[liable] = pays.get(liable, 0) + part // 2
        if honba_payer == loser_seat:
            honba_payer = liable
    if honba_bonus:
        pays[honba_payer] = pays.get(honba_payer, 0) + honba_bonus
    return {s: a for s, a in pays.items() if a}


def _pao_tsumo(gs: GameState, winner_seat: int, result: ScoreResult,
               pays: dict[int, int], honbas: dict[int, int]) -> None:
    """自摸的包牌：責任者代付其他人在「包牌役滿」那部分該付的份，並負擔全部本場（直接改 pays／honbas）。"""
    winner = gs.players[winner_seat]
    honba_moved = False
    for _name, liable, base in result.pao:
        if liable == winner_seat or liable not in pays:
            continue
        for p in gs.players:
            if p.seat == winner_seat or p.seat == liable:
                continue
            part = _round_up(base * (2 if (winner.is_dealer or p.is_dealer) else 1), 100)
            pays[p.seat] -= part
            pays[liable] += part
        if not honba_moved:
            honba_moved = True
            for s in list(honbas):
                if s != liable:
                    honbas[liable] += honbas[s]
                    honbas[s] = 0


# ─── 流局結算 ───────────────────────────────────────────────────────────────

def apply_ryuukyoku(gs: GameState, tenpai_seats: list[int]) -> SettleLog:
    """
    流局：聽牌罰符。
    有聽牌也有未聽（1 ~ 人數−1 人聽牌）時，未聽者合計支付 3000，聽牌者平分。
    全員聽牌或全員未聽：無點數移動。立直棒留到下一局。
    """
    deltas = {p.seat: 0 for p in gs.players}
    n = len(tenpai_seats)
    if 0 < n < len(gs.players):   # 同時有聽牌與未聽者（三麻全聽=3 人也排除，避免除以零）
        noten = [p.seat for p in gs.players if p.seat not in tenpai_seats]
        pay_each_noten = 3000 // len(noten)
        recv_each_tenpai = 3000 // n
        for s in noten:
            deltas[s] -= pay_each_noten
        for s in tenpai_seats:
            deltas[s] += recv_each_tenpai
    _apply_deltas(gs, deltas)
    return SettleLog(deltas, note="流局")


def apply_nagashi(gs: GameState, nagashi_seats: list[int]) -> SettleLog:
    """流局滿貫（流し満貫）：每位達成者以滿貫自摸計算收受（不另計聽牌罰符）。
    莊家滿貫＝每家 4000；閒家滿貫＝莊家付 4000、其餘閒家各付 2000。"""
    deltas = {p.seat: 0 for p in gs.players}
    for ws in nagashi_seats:
        winner = gs.players[ws]
        for p in gs.players:
            if p.seat == ws:
                continue
            pay = 4000 if (winner.is_dealer or p.is_dealer) else 2000
            deltas[p.seat] -= pay
            deltas[ws] += pay
    _apply_deltas(gs, deltas)
    return SettleLog(deltas, note="流局滿貫")


# ─── 場次推進（連莊/輪莊/本場）────────────────────────────────────────────────

def advance_after_win(gs: GameState, winner_seat: int) -> None:
    """和牌後推進場次。莊家和牌→連莊本場+1；閒家和牌→輪莊、本場歸0。"""
    dealer_won = gs.players[winner_seat].is_dealer
    if dealer_won:
        gs.honba += 1
    else:
        gs.honba = 0
        _rotate_dealer(gs)
    _reset_hand_flags(gs)


def advance_after_draw(gs: GameState, tenpai_seats: list[int]) -> None:
    """流局後推進場次。莊家聽牌→連莊；否則輪莊。本場一律 +1。"""
    dealer = gs.players[gs.dealer_seat]
    dealer_tenpai = dealer.seat in tenpai_seats
    gs.honba += 1
    if not dealer_tenpai:
        _rotate_dealer(gs)
    _reset_hand_flags(gs)


def advance_abortive(gs: GameState) -> None:
    """途中流局（九種九牌等）：莊家連莊、本場 +1，不移動點數、不輪莊。"""
    gs.honba += 1
    _reset_hand_flags(gs)


def _rotate_dealer(gs: GameState) -> None:
    """莊家順移到下一位；繞一圈則場風推進（東→南→西→北）。"""
    n = len(gs.players)
    new_dealer = (gs.dealer_seat + 1) % n
    gs.dealer_seat = new_dealer
    for p in gs.players:
        p.is_dealer = (p.seat == new_dealer)
    # 局數 +1；若回到起始莊（seat 0）代表進入下一個場風
    gs.round_num += 1
    if gs.round_num > n:
        gs.round_num = 1
        gs.round_wind += 1


def _reset_hand_flags(gs: GameState) -> None:
    for p in gs.players:
        p.riichi = False


def west_target(gs: GameState, rs, start_points: int | None = None) -> int:
    """南入／西入與和了止め的門檻：規則的 west_target（依人數），房間自訂了起始點數就跟著平移
    （門檻＋（自訂起始 − 規則起始））。"""
    m = rs.for_mode(len(gs.players) == 3)
    target = m["west_target"]
    if start_points:
        target += start_points - m["start"]
    return target


def is_game_over(gs: GameState, length: str = "tonpuu", tobi: bool = True,
                 rs=None, start_points: int | None = None, renchan: bool = False) -> bool:
    """
    判斷整局是否結束（在 advance_* 之後呼叫，gs 已是「下一局」的狀態）。
    length: "tonpuu"=東風戰（打完東場）、"hanchan"=半莊（打完南場）
    tobi:   擊飛規則。True 時只要有人點數 < 0 即結束；False 時忽略負分繼續打。
    rs／start_points／renchan：給了規則集 rs 才會套用下面兩條（不給＝舊行為）：
      ・rs.west（南入／西入，サドンデス）：預定最後一局（東風戰東4、半莊南4）打完，沒人達門檻就延長到下一個場風；
        延長期間每局結束只要有人 ≥ 門檻就結束；最多打到延長場風的最後一局（東風戰到南4、半莊到西4）。
        桌上的立直棒不算分數；擊飛照常終局（上面先判）。
      ・rs.agari_yame（和了止め／聽牌止め）：預定最後一局莊家要連莊（renchan＝莊家和了／流局聽牌），
        而且結算後莊家是單獨第一名且 ≥ 門檻 → 結束。途中流局不算（呼叫端傳 renchan=False）。
        延長局不用另外判：延長中有人 ≥ 門檻本來就結束。
    """
    if tobi and any(p.score < 0 for p in gs.players):
        return True
    end_wind = 1 if length == "tonpuu" else 2  # 1=東 2=南
    if rs is None:
        # 已超過該場風最後一局（例如東風戰：round_wind 已進到 2）
        return gs.round_wind > end_wind
    target = west_target(gs, rs, start_points)
    if rs.west:
        if gs.round_wind > end_wind + 1:        # 延長場風也打完了（東風戰南4、半莊西4 之後）
            return True
        if gs.round_wind > end_wind:            # 延長局：有人達門檻就結束，否則繼續
            return max(p.score for p in gs.players) >= target
    elif gs.round_wind > end_wind:
        return True
    # 和了止め：預定最後一局（東風戰東4／半莊南4，三麻東3／南3）莊家連莊時
    if (rs.agari_yame and renchan and gs.round_wind == end_wind
            and gs.round_num == len(gs.players)):
        dealer = gs.players[gs.dealer_seat]
        others = [p.score for p in gs.players if p.seat != dealer.seat]
        if dealer.score >= target and (not others or dealer.score > max(others)):
            return True
    return False


# ─── 金流：賽末順位與馬點積分 ─────────────────────────────────────────────────

# 順位馬（千點單位）。三麻把二、三位合併為中間 0。
UMA_YONMA         = (20, 10, -10, -20)   # 預設／半莊
UMA_YONMA_TONPUU  = (10, 5, -5, -10)     # 天鳳式四麻東風戰
UMA_SANMA         = (20, 0, -20)         # 三麻（天鳳東風/半莊相同）


def _pick_uma(is_sanma: bool, length: str, ruleset: str) -> tuple:
    """依模式/戰長/規則選順位馬。
    天鳳式：四麻東風 10-5、四麻半莊 20-10、三麻 20-0-20。
    預設(mixed)：四麻一律 20-10（沿用舊行為）。"""
    if is_sanma:
        return UMA_SANMA
    if ruleset == "tenhou" and length == "tonpuu":
        return UMA_YONMA_TONPUU
    return UMA_YONMA


@dataclass
class FinalRow:
    seat: int
    username: str
    rank: int
    score: int
    raw_pt: float        # (結算點數 - 返點)/1000
    uma: int
    total_pt: float      # raw_pt + 馬點（一位另含頭名賞）


def final_standings(gs: GameState, uma: tuple = None,
                    start_points: int = None,
                    return_points: int = None,
                    length: str = "hanchan", ruleset: str = "mixed",
                    rs=None) -> list[FinalRow]:
    """
    依精算規則計算賽末積分：
        精算 =（結算點數 − 返點）÷ 1000 ＋ 順位馬
        一位 = 其餘各家精算之和取相反數（自動吸收頭名賞 オカ 與小數進位差）

    原點/返點預設：四麻 25000/30000、三麻 35000/40000。
    順位馬依 length/ruleset 選擇（天鳳式四麻東風 10-5、半莊 20-10）。
    頭名賞 =（返點 − 原點）÷1000 × 人數，全數歸於一位。

    給了規則集 rs（rulesets.RuleSet）時，馬點／原點／返點改照規則集：
      ・房間自訂了起始點數 start_points → 返點＝自訂起始 +（規則返點 − 規則起始）；沒自訂就用規則的起始與返點。
      ・終局時桌上剩的立直棒：rs.leftover_sticks == "top" → 加進第一名的點數（並清掉立直棒）；"none" → 消失。
    不給 rs 時維持舊行為（依 length／ruleset 選馬、原點 25000／35000、返點＝原點 + 5000）。
    """
    n = len(gs.players)
    is_sanma = (n == 3)
    if rs is not None:
        m = rs.for_mode(is_sanma)
        if uma is None:
            uma = m["uma"]
        if start_points is None:
            start_points = m["start"]
            if return_points is None:
                return_points = m["return_pts"]
        elif return_points is None:
            return_points = start_points + (m["return_pts"] - m["start"])
        if rs.leftover_sticks == "top" and gs.riichi_sticks > 0 and gs.players:
            top = min(gs.players, key=lambda p: (-p.score, p.seat))
            top.score += gs.riichi_sticks * 1000
            gs.riichi_sticks = 0
    if uma is None:
        uma = _pick_uma(is_sanma, length, ruleset)
    if start_points is None:
        # 預設起始點數：四麻 25000、三麻 35000
        start_points = 35000 if is_sanma else 25000
    if return_points is None:
        # 返點＝起始點數 + 5000（維持頭名賞 5000/人）
        return_points = start_points + 5000

    ordered = sorted(gs.players, key=lambda p: (-p.score, p.seat))
    computed = []
    for idx, p in enumerate(ordered):
        u = uma[idx] if idx < len(uma) else 0
        pt = (p.score - return_points) / 1000.0 + u
        computed.append([p, idx + 1, u, pt])

    # 一位的精算 = 其餘各家精算之和取相反數（含頭名賞）
    if computed:
        sum_rest = sum(c[3] for c in computed[1:])
        computed[0][3] = -sum_rest

    rows: list[FinalRow] = []
    for p, rank, u, pt in computed:
        rows.append(FinalRow(
            seat=p.seat, username=p.username, rank=rank, score=p.score,
            raw_pt=(p.score - return_points) / 1000.0, uma=u, total_pt=pt,
        ))
    return rows


# ─── 內部 ───────────────────────────────────────────────────────────────────

def _apply_deltas(gs: GameState, deltas: dict[int, int]) -> None:
    for p in gs.players:
        p.score += deltas.get(p.seat, 0)
