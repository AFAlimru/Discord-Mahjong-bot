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

# 機器人管理員（Discord 使用者 id，逗號分隔）：能管理所有比賽。沒設定＝只有各比賽的主辦能管理
ADMIN_IDS = {x.strip() for x in os.getenv("ADMIN_DISCORD_ID", "").split(",") if x.strip()}
# 一般玩家能不能自己建立比賽（預設只有管理員）
TOURNAMENT_PLAYER_CREATE = os.getenv("TOURNAMENT_PLAYER_CREATE", "").strip().lower() in ("1", "true", "yes")

# ── 網站（公開網址；大廳面板的「🌐 網頁」按鈕用，留空則不顯示）──
WEB_BASE_URL = os.getenv("WEB_BASE_URL", "").strip()

# ── 支援伺服器邀請連結（指南／大廳按鈕用）──
SUPPORT_URL = os.getenv("SUPPORT_URL", "https://discord.gg/QNJMBhDrcj").strip()

# ── 0.7.2 音效（FFmpeg 直接播檔 + 資料夾語音包）──
# 音效檔根目錄；底下每個子資料夾＝一個語音包（見 sfx.py）。
SOUNDS_DIR = os.getenv("SOUNDS_DIR", "assets/sounds")
# 電腦語音的預設語音包（沒人設定過「電腦語音」時用；資料夾不存在＝電腦不出聲）
DEFAULT_AI_VOICE = os.getenv("DEFAULT_AI_VOICE", "AI曼波").strip()

# ── 遊戲常數 ──────────────────────────────────────────────────
WIND_LABELS          = ["東", "南", "西", "北"]
DEFAULT_POINTS_YONMA = 25000
DEFAULT_POINTS_SANMA = 35000
