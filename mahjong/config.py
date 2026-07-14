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

# ── Discord ───────────────────────────────────────────────────
TOKEN = os.getenv("DISCORD_TOKEN", "")
# 開發用：設定後，斜線指令只同步到這個測試伺服器（即時生效，免等全域約 1 小時傳播）。
# 正式機留空 → 走全域同步。
DEV_GUILD_ID = os.getenv("DEV_GUILD_ID", "").strip()

# ── 資料庫 ────────────────────────────────────────────────────
DATABASE_PATH = os.getenv("DATABASE_PATH", "mahjong.db")

# ── 網站（公開網址；大廳面板的「🌐 網頁」按鈕用，留空則不顯示）──
WEB_BASE_URL = os.getenv("WEB_BASE_URL", "").strip()

# ── 支援伺服器邀請連結（指南／大廳按鈕用）──
SUPPORT_URL = os.getenv("SUPPORT_URL", "https://discord.gg/QNJMBhDrcj").strip()

# ── 遊戲常數 ──────────────────────────────────────────────────
WIND_LABELS          = ["東", "南", "西", "北"]
AI_NAMES             = ["小春", "小夏", "小秋", "小冬"]
DEFAULT_POINTS_YONMA = 25000
DEFAULT_POINTS_SANMA = 35000
