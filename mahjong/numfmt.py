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
"""數字縮寫：活躍度、段位 pt、累計得失點等越打越大的數字，用 k／M／B 顯示（例：1.2k、45.3k、1.25M）。
對局點數（25000 起算的終局分數、和了打點）照原樣顯示，不走這裡。"""
from __future__ import annotations
import math

_UNITS = ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "k"))


def compact(n) -> str:
    """1000 以下照原樣；以上縮寫成 k／M／B。小數位數依大小（1.25k、12.5k、125k），無條件捨去不進位。"""
    try:
        n = int(n or 0)
    except (TypeError, ValueError):
        return str(n)
    sign, v = ("-" if n < 0 else ""), abs(n)
    for size, unit in _UNITS:
        if v >= size:
            x = v / size
            digits = 2 if x < 10 else (1 if x < 100 else 0)
            x = math.floor(x * 10 ** digits) / 10 ** digits
            txt = f"{x:.{digits}f}".rstrip("0").rstrip(".") if digits else str(int(x))
            return f"{sign}{txt}{unit}"
    return f"{sign}{v}"


def signed(n) -> str:
    """帶正負號的縮寫：+1.2k／-350／+0。"""
    s = compact(n)
    return s if s.startswith("-") else "+" + s
