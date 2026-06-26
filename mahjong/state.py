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
"""跨模組共享的記憶體狀態（純資料，刻意不 import 其他套件模組以避免循環匯入）。"""
from __future__ import annotations

# game_id -> GameState
_games:         dict = {}
# channel_id -> game_id
_channel_games: dict = {}
# game_id -> [{"user_id","username","is_bot"}, ...]
_waiting:       dict = {}
# game_id -> 房主 user_id
_room_owners:   dict = {}
# game_id -> 房間設定（length/tobi/start_points/ruleset/thinking_time…）
_room_configs:  dict = {}
# user_id -> game_id（玩家目前所在牌局）
_user_game:     dict = {}
# game_id -> asyncio.Task（對局迴圈，供 /end 取消）
_game_tasks:    dict = {}
# game_id -> LobbyView（供 /join 更新大廳訊息）
_lobbies:       dict = {}
# 段位賽全域匹配佇列（跨伺服器）：mode -> [{"uid","name","user","lang","since"}]
_rank_queue:    dict = {"yonma": [], "sanma": []}

# ── 0.2 討論串 ──
# game_id -> {"public","board_msg","private":{uid:Thread},"hand_msg":{uid:Message},"announce"}
_threads:       dict = {}
# user_id -> asyncio.Queue（等待該玩家打字輸入時存在）
_input_queues:  dict = {}
# user_id -> 該玩家應輸入的討論串 channel id（驗證打字來源）
_input_thread:  dict = {}
# thread channel id -> game_id（讓 /end 等指令能在討論串內使用）
_thread_game:   dict = {}
# game_id -> 本局最近的玩家動作記錄（顯示在私人手牌面板上方），每局開始時清空
_action_logs:   dict = {}
# 背景任務（如延遲刪除討論串）暫存，避免被 GC
_bg_tasks:      set  = set()

# ── 房間編號（0.3）──
# game_id -> RoomMeta（房間#0001 等）
rooms:          dict = {}
