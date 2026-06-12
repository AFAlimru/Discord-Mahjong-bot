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
"""設定與常數：環境變數、預設值、固定字串（不依賴其他套件模組）。"""
from __future__ import annotations
import os
from dotenv import load_dotenv

load_dotenv()


def _as_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "y", "on")


# ── Discord ───────────────────────────────────────────────────
TOKEN = os.getenv("DISCORD_TOKEN", "")

# ── 資料庫 ────────────────────────────────────────────────────
DATABASE_PATH = os.getenv("DATABASE_PATH", "mahjong.db")

# ── 後台（aiohttp，作者自用）───────────────────────────────────
WEB_ENABLED     = _as_bool(os.getenv("WEB_ENABLED"), True)
WEB_HOST        = os.getenv("WEB_HOST", "127.0.0.1")
WEB_PORT        = int(os.getenv("WEB_PORT", "8787"))
WEB_ADMIN_TOKEN = os.getenv("WEB_ADMIN_TOKEN", "")

# ── 遊戲常數 ──────────────────────────────────────────────────
WIND_LABELS          = ["東", "南", "西", "北"]
AI_NAMES             = ["小春", "小夏", "小秋", "小冬"]
DEFAULT_POINTS_YONMA = 25000
DEFAULT_POINTS_SANMA = 35000
