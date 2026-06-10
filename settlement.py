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

from mahjong_engine import GameState, PlayerState
from scoring import ScoreResult


# ─── 和牌結算 ───────────────────────────────────────────────────────────────

@dataclass
class SettleLog:
    """一次結算的點數變動明細，用於顯示。"""
    deltas: dict[int, int]          # seat -> 點數變化
    note: str = ""

    def describe(self, gs: GameState) -> str:
        lines = []
        for p in gs.players:
            d = self.deltas.get(p.seat, 0)
            sign = "＋" if d >= 0 else "－"
            lines.append(f"{p.username}：{sign}{abs(d)}　→　{p.score}")
        return "\n".join(lines)


def apply_ron(gs: GameState, winner_seat: int, loser_seat: int,
              result: ScoreResult) -> SettleLog:
    """榮和結算。"""
    deltas = {p.seat: 0 for p in gs.players}
    honba_bonus = 300 * gs.honba

    pay = result.ron_payment + honba_bonus
    deltas[winner_seat] += pay
    deltas[loser_seat] -= pay

    # 立直棒全收
    stick = gs.riichi_sticks * 1000
    deltas[winner_seat] += stick
    gs.riichi_sticks = 0

    _apply_deltas(gs, deltas)
    return SettleLog(deltas, note=f"{result.name} {result.describe()}")


def apply_tsumo(gs: GameState, winner_seat: int, result: ScoreResult) -> SettleLog:
    """自摸結算。"""
    deltas = {p.seat: 0 for p in gs.players}
    winner = gs.players[winner_seat]
    honba_each = 100 * gs.honba

    total = 0
    for p in gs.players:
        if p.seat == winner_seat:
            continue
        if winner.is_dealer:
            pay = result.tsumo_each_payment + honba_each
        else:
            if p.is_dealer:
                pay = result.tsumo_dealer_payment + honba_each
            else:
                pay = result.tsumo_each_payment + honba_each
        deltas[p.seat] -= pay
        total += pay

    deltas[winner_seat] += total

    stick = gs.riichi_sticks * 1000
    deltas[winner_seat] += stick
    gs.riichi_sticks = 0

    _apply_deltas(gs, deltas)
    return SettleLog(deltas, note=f"{result.name} {result.describe()}")


# ─── 流局結算 ───────────────────────────────────────────────────────────────

def apply_ryuukyoku(gs: GameState, tenpai_seats: list[int]) -> SettleLog:
    """
    流局：聽牌罰符。
    聽牌人數 1~3 時，未聽牌者合計支付 3000，聽牌者平分。
    0 或 4 人聽牌：無點數移動。立直棒留到下一局。
    """
    deltas = {p.seat: 0 for p in gs.players}
    n = len(tenpai_seats)
    if 1 <= n <= 3:
        noten = [p.seat for p in gs.players if p.seat not in tenpai_seats]
        pay_each_noten = 3000 // len(noten)
        recv_each_tenpai = 3000 // n
        for s in noten:
            deltas[s] -= pay_each_noten
        for s in tenpai_seats:
            deltas[s] += recv_each_tenpai
    _apply_deltas(gs, deltas)
    return SettleLog(deltas, note="流局")


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


def is_game_over(gs: GameState, length: str = "tonpuu", tobi: bool = True) -> bool:
    """
    判斷整局是否結束。
    length: "tonpuu"=東風戰（打完東場）、"hanchan"=半莊（打完南場）
    tobi:   擊飛規則。True 時只要有人點數 < 0 即結束；False 時忽略負分繼續打。
    """
    if tobi and any(p.score < 0 for p in gs.players):
        return True
    end_wind = 1 if length == "tonpuu" else 2  # 1=東 2=南
    # 已超過該場風最後一局（例如東風戰：round_wind 已進到 2）
    if gs.round_wind > end_wind:
        return True
    return False


# ─── 金流：賽末順位與馬點積分 ─────────────────────────────────────────────────

# 順位馬（10-20，天鳳式；千點單位）。三麻把二、三位合併為中間 0。
UMA_YONMA = (20, 10, -10, -20)
UMA_SANMA = (20, 0, -20)


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
                    return_points: int = None) -> list[FinalRow]:
    """
    依精算規則計算賽末積分：
        精算 =（結算點數 − 返點）÷ 1000 ＋ 順位馬
        一位 = 其餘各家精算之和取相反數（自動吸收頭名賞 オカ 與小數進位差）

    原點/返點預設：四麻 25000/30000、三麻 35000/40000。
    頭名賞 =（返點 − 原點）÷1000 × 人數，全數歸於一位。
    """
    n = len(gs.players)
    is_sanma = (n == 3)
    if uma is None:
        uma = UMA_SANMA if is_sanma else UMA_YONMA
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
