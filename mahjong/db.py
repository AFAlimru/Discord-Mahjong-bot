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
database.py — SQLite database management for Mahjong Discord Bot
Stores game sessions, player stats, and game history.
"""

import sqlite3
import json
import os
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()
DATABASE_PATH = os.getenv("DATABASE_PATH", "mahjong.db")


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_db() -> None:
    """Create all tables if they do not exist."""
    with get_connection() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS games (
                game_id     TEXT PRIMARY KEY,
                guild_id    TEXT NOT NULL,
                channel_id  TEXT NOT NULL,
                state       TEXT NOT NULL DEFAULT 'waiting',
                -- 'waiting' | 'playing' | 'finished'
                game_data   TEXT NOT NULL DEFAULT '{}',
                -- JSON blob for full game state
                wall_seed   TEXT,
                -- SHA-256 seed string (fairness transparency)
                room_no     INTEGER,
                -- 房間流水號（對外顯示成 5 碼代碼，見 rooms.code）
                room_config TEXT,
                -- 房間設定 JSON（重連回復對局時用：length/tobi/start_points…）
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS game_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                game_id     TEXT NOT NULL REFERENCES games(game_id) ON DELETE CASCADE,
                turn        INTEGER NOT NULL,
                action      TEXT NOT NULL,
                -- JSON: {type, player_seat, tile, ...}
                timestamp   TEXT NOT NULL
            );

            -- 每位玩家每場一筆（牌譜/戰績來源，分三麻 sanma / 四麻 yonma）
            CREATE TABLE IF NOT EXISTS game_records (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                game_id      TEXT,
                user_id      TEXT NOT NULL,
                username     TEXT NOT NULL,
                mode         TEXT NOT NULL,        -- 'sanma' | 'yonma'
                rank         INTEGER NOT NULL,     -- 終局順位
                score        INTEGER NOT NULL,     -- 終局點數
                score_delta  INTEGER NOT NULL,     -- 得失點（可負）
                tsumo        INTEGER NOT NULL DEFAULT 0,
                ron          INTEGER NOT NULL DEFAULT 0,
                houju        INTEGER NOT NULL DEFAULT 0,        -- 放銃次數
                houju_points INTEGER NOT NULL DEFAULT 0,        -- 放銃失點（累計可加總）
                gain_points  INTEGER NOT NULL DEFAULT 0,        -- 獲得點數（每局正向變動之和）
                riichi       INTEGER NOT NULL DEFAULT 0,
                created_at   TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_records_user
                ON game_records (user_id, mode, id);

            -- 玩家偏好（顯示語言等）
            CREATE TABLE IF NOT EXISTS user_prefs (
                user_id      TEXT PRIMARY KEY,
                lang         TEXT NOT NULL DEFAULT 'zh_tw',
                skin         TEXT,                              -- 牌面樣式
                notify_queue INTEGER NOT NULL DEFAULT 0,        -- 有人排隊時私訊通知
                voice_pack   TEXT                               -- 個人角色語音（語音包資料夾名）
            );

            -- 伺服器設定
            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id         TEXT PRIMARY KEY,
                play_channel_id  TEXT,      -- 指定遊玩頻道
                category_id      TEXT,      -- 雀月類別
                lobby_channel_id TEXT,      -- 按鈕大廳
                hub_voice_id     TEXT,      -- 配對語音
                guild_lang       TEXT       -- 伺服器主要語言
            );

            -- 任務／活躍度（每日簽到、每日對局）
            CREATE TABLE IF NOT EXISTS user_activity (
                user_id      TEXT PRIMARY KEY,
                username     TEXT,
                activity     INTEGER NOT NULL DEFAULT 0,   -- 活躍度總點
                streak       INTEGER NOT NULL DEFAULT 0,   -- 連續簽到天數
                last_checkin TEXT,                         -- 最後簽到日 YYYY-MM-DD
                last_play    TEXT,                         -- 最後領「對局獎勵」日 YYYY-MM-DD
                best_win      INTEGER NOT NULL DEFAULT 0,  -- 最高和了打點
                best_win_name TEXT,                        -- 最高和了的等級名（滿貫/役滿…）
                best_win_hand TEXT,                        -- 最高和了的牌型（Unicode 牌字串）
                updated_at   TEXT
            );

            -- 段位／R（段位賽用，分三麻 sanma / 四麻 yonma）
            CREATE TABLE IF NOT EXISTS user_rating (
                user_id    TEXT NOT NULL,
                mode       TEXT NOT NULL,            -- 'sanma' | 'yonma'
                dan_idx    INTEGER NOT NULL DEFAULT 0,
                dan_pt     INTEGER NOT NULL DEFAULT 0,
                rate       REAL    NOT NULL DEFAULT 1500,
                games      INTEGER NOT NULL DEFAULT 0,
                username   TEXT,
                updated_at TEXT,
                PRIMARY KEY (user_id, mode)
            );
        """)
        try:                                       # 0.7.x 資料庫升級到 0.8：個人角色語音
            conn.execute("ALTER TABLE user_prefs ADD COLUMN voice_pack TEXT")
        except Exception:
            pass
        if conn.execute("PRAGMA user_version").fetchone()[0] < 1:
            # 0.8.1 段位改成每階 1～3 級：舊的 dan_idx 是「階」→ 換成該階的 1 級，pt 從起始值開始
            from . import rating
            for r in conn.execute("SELECT user_id, mode, dan_idx FROM user_rating").fetchall():
                idx = rating.level_index(min(int(r["dan_idx"] or 0), len(rating.TIERS) - 1), 1)
                conn.execute("UPDATE user_rating SET dan_idx=?, dan_pt=? WHERE user_id=? AND mode=?",
                             (idx, rating.start_pt(idx), r["user_id"], r["mode"]))
            conn.execute("PRAGMA user_version = 1")
    print(f"[DB] Database initialised at: {DATABASE_PATH}")


def max_room_no() -> int:
    """目前資料庫中最大的房間編號（無則 0），供啟動後續號。"""
    with get_connection() as conn:
        row = conn.execute("SELECT MAX(room_no) AS m FROM games").fetchone()
    return (row["m"] or 0) if row else 0


# ─── Game CRUD ────────────────────────────────────────────────────────────────

def create_game(game_id: str, guild_id: str, channel_id: str,
                wall_seed: str | None = None, room_no: int | None = None) -> None:
    now = datetime.utcnow().isoformat()
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO games (game_id,guild_id,channel_id,state,game_data,wall_seed,room_no,created_at,updated_at) "
            "VALUES (?,?,?,'waiting','{}',?,?,?,?)",
            (game_id, guild_id, channel_id, wall_seed, room_no, now, now)
        )


def set_room_config(game_id: str, config: dict) -> None:
    """保存房間設定 JSON，供重連回復對局時還原（length/tobi/start_points…）。"""
    with get_connection() as conn:
        conn.execute(
            "UPDATE games SET room_config=? WHERE game_id=?",
            (json.dumps(config, ensure_ascii=False, default=str), game_id)
        )


def mark_interrupted(game_id: str) -> None:
    """標記對局為中斷（機器人重啟後待玩家決定繼續或結束）。"""
    now = datetime.utcnow().isoformat()
    with get_connection() as conn:
        conn.execute(
            "UPDATE games SET state='interrupted', updated_at=? WHERE game_id=?",
            (now, game_id)
        )


def get_unfinished_games() -> list[dict]:
    """進行中或中斷、尚未結算的對局（供重啟後回復）。已解析 game_data。"""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM games WHERE state IN ('playing','interrupted')"
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["game_data"] = json.loads(d["game_data"])
        except Exception:
            d["game_data"] = {}
        out.append(d)
    return out


def get_game(game_id: str) -> dict | None:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM games WHERE game_id=?", (game_id,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["game_data"] = json.loads(d["game_data"])
    return d


def update_game_state(game_id: str, state: str, game_data: dict) -> None:
    now = datetime.utcnow().isoformat()
    with get_connection() as conn:
        conn.execute(
            "UPDATE games SET state=?, game_data=?, updated_at=? WHERE game_id=?",
            (state, json.dumps(game_data, ensure_ascii=False), now, game_id)
        )


def finish_game(game_id: str, game_data: dict) -> None:
    update_game_state(game_id, "finished", game_data)


# ─── Game Log ─────────────────────────────────────────────────────────────────

def log_action(game_id: str, turn: int, action: dict) -> None:
    now = datetime.utcnow().isoformat()
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO game_log (game_id,turn,action,timestamp) VALUES (?,?,?,?)",
            (game_id, turn, json.dumps(action, ensure_ascii=False), now)
        )


# ─── 牌譜／戰績（game_records）──────────────────────────────────────────────

def add_game_record(*, game_id: str, user_id: str, username: str, mode: str,
                    rank: int, score: int, score_delta: int,
                    tsumo: int = 0, ron: int = 0, houju: int = 0,
                    houju_points: int = 0, riichi: int = 0, gain_points: int = 0) -> None:
    now = datetime.utcnow().isoformat()
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO game_records (game_id,user_id,username,mode,rank,score,score_delta,"
            "tsumo,ron,houju,houju_points,gain_points,riichi,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (game_id, user_id, username, mode, rank, score, score_delta,
             tsumo, ron, houju, houju_points, gain_points, riichi, now)
        )


def get_mode_summary(user_id: str, mode: str) -> dict | None:
    """某玩家在指定模式（sanma/yonma）的累計戰績；無紀錄回 None。"""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS games, "
            "SUM(rank=1) AS r1, SUM(rank=2) AS r2, SUM(rank=3) AS r3, SUM(rank=4) AS r4, "
            "AVG(rank) AS avg_rank, SUM(tsumo) AS tsumo, SUM(ron) AS ron, "
            "SUM(houju) AS houju, SUM(houju_points) AS houju_points, "
            "SUM(gain_points) AS gain_points, "
            "SUM(riichi) AS riichi, SUM(score_delta) AS score_delta, "
            "MAX(username) AS username "
            "FROM game_records WHERE user_id=? AND mode=?",
            (user_id, mode)
        ).fetchone()
    if not row or not row["games"]:
        return None
    return dict(row)


def get_user_lang(user_id: str) -> str | None:
    with get_connection() as conn:
        row = conn.execute("SELECT lang FROM user_prefs WHERE user_id=?", (user_id,)).fetchone()
    return row["lang"] if row else None


def set_user_lang(user_id: str, lang: str) -> None:
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO user_prefs (user_id, lang) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET lang=excluded.lang",
            (user_id, lang)
        )


def get_user_skin(user_id: str) -> str | None:
    """玩家選用的牌風（None＝預設）。"""
    with get_connection() as conn:
        row = conn.execute("SELECT skin FROM user_prefs WHERE user_id=?", (user_id,)).fetchone()
    return row["skin"] if row else None


def set_user_skin(user_id: str, skin: str | None) -> None:
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO user_prefs (user_id, skin) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET skin=excluded.skin",
            (user_id, skin)
        )


def get_user_voice(user_id: str) -> str | None:
    """玩家選的角色語音（語音包資料夾名）；None＝不使用語音。"""
    with get_connection() as conn:
        row = conn.execute("SELECT voice_pack FROM user_prefs WHERE user_id=?",
                           (user_id,)).fetchone()
    return row["voice_pack"] if row else None


def set_user_voice(user_id: str, pack: str | None) -> None:
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO user_prefs (user_id, voice_pack) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET voice_pack=excluded.voice_pack",
            (user_id, pack)
        )


def get_notify_queue(user_id: str) -> bool:
    """是否訂閱「有人排隊」私訊通知。"""
    with get_connection() as conn:
        row = conn.execute("SELECT notify_queue FROM user_prefs WHERE user_id=?", (user_id,)).fetchone()
    return bool(row["notify_queue"]) if row else False


def set_notify_queue(user_id: str, on: bool) -> None:
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO user_prefs (user_id, notify_queue) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET notify_queue=excluded.notify_queue",
            (user_id, 1 if on else 0)
        )


def get_queue_subscribers() -> list[str]:
    """所有訂閱排隊通知的玩家 uid。"""
    with get_connection() as conn:
        rows = conn.execute("SELECT user_id FROM user_prefs WHERE notify_queue=1").fetchall()
    return [r["user_id"] for r in rows]


def get_play_channel(guild_id: str) -> str | None:
    """該伺服器指定的遊玩頻道（None＝不限制）。"""
    with get_connection() as conn:
        row = conn.execute("SELECT play_channel_id FROM guild_settings WHERE guild_id=?",
                           (guild_id,)).fetchone()
    return row["play_channel_id"] if row else None


def set_play_channel(guild_id: str, channel_id: str | None) -> None:
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO guild_settings (guild_id, play_channel_id) VALUES (?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET play_channel_id=excluded.play_channel_id",
            (guild_id, channel_id)
        )


def get_guild_lang(guild_id: str) -> str | None:
    """該伺服器主要語言（None＝未設定）。"""
    with get_connection() as conn:
        row = conn.execute("SELECT guild_lang FROM guild_settings WHERE guild_id=?",
                           (guild_id,)).fetchone()
    return row["guild_lang"] if row else None


def set_guild_lang(guild_id: str, lang: str | None) -> None:
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO guild_settings (guild_id, guild_lang) VALUES (?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET guild_lang=excluded.guild_lang",
            (guild_id, lang)
        )


def get_guild_setup(guild_id: str) -> dict:
    """該伺服器的類別制設定（category / 大廳頻道 / 配對語音）。缺者為 None。"""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT category_id, lobby_channel_id, hub_voice_id "
            "FROM guild_settings WHERE guild_id=?", (guild_id,)).fetchone()
    if not row:
        return {"category_id": None, "lobby_channel_id": None, "hub_voice_id": None}
    return {"category_id": row["category_id"],
            "lobby_channel_id": row["lobby_channel_id"],
            "hub_voice_id": row["hub_voice_id"]}


def set_guild_setup(guild_id: str, category_id: str | None,
                    lobby_channel_id: str | None, hub_voice_id: str | None) -> None:
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO guild_settings (guild_id, category_id, lobby_channel_id, hub_voice_id) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET category_id=excluded.category_id, "
            "lobby_channel_id=excluded.lobby_channel_id, hub_voice_id=excluded.hub_voice_id",
            (guild_id, category_id, lobby_channel_id, hub_voice_id)
        )


def get_all_guild_settings() -> dict:
    """一次讀出全部伺服器設定（後台列表用，避免逐台查詢）。guild_id -> dict。"""
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM guild_settings").fetchall()
    return {r["guild_id"]: dict(r) for r in rows}


def delete_guild_settings(guild_id: str) -> None:
    """移除該伺服器的全部設定（大廳、遊玩頻道、主要語言）。機器人退出並清理後用。"""
    with get_connection() as conn:
        conn.execute("DELETE FROM guild_settings WHERE guild_id=?", (guild_id,))


def get_total_games(user_id: str) -> int:
    """某玩家所有模式的總對局場數。"""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM game_records WHERE user_id=?", (user_id,)
        ).fetchone()
    return (row["c"] or 0) if row else 0


def get_recent_records(user_id: str, mode: str, limit: int = 20, offset: int = 0) -> list[dict]:
    """近期戰績（含房號 room_no）。支援 offset 供分頁。"""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT gr.*, g.room_no FROM game_records gr "
            "LEFT JOIN games g ON gr.game_id = g.game_id "
            "WHERE gr.user_id=? AND gr.mode=? ORDER BY gr.id DESC LIMIT ? OFFSET ?",
            (user_id, mode, limit, offset)
        ).fetchall()
    return [dict(r) for r in rows]


def get_recent_games_any(user_id: str, limit: int = 25) -> list[dict]:
    """某玩家最近的對局（不分四麻／三麻），含房號 room_no；回放的房號自動完成用。"""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT gr.*, g.room_no FROM game_records gr "
            "LEFT JOIN games g ON gr.game_id = g.game_id "
            "WHERE gr.user_id=? AND g.room_no IS NOT NULL ORDER BY gr.id DESC LIMIT ?",
            (user_id, limit)
        ).fetchall()
    return [dict(r) for r in rows]


def count_records(user_id: str, mode: str) -> int:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM game_records WHERE user_id=? AND mode=?",
            (user_id, mode)).fetchone()
    return (row["c"] or 0) if row else 0


def get_game_by_room_no(room_no: int) -> dict | None:
    """以房號找對局（取最新一筆同號）。"""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM games WHERE room_no=? ORDER BY rowid DESC LIMIT 1", (room_no,)
        ).fetchone()
    return dict(row) if row else None


def get_game_logs(game_id: str) -> list[dict]:
    """某對局的所有牌譜事件（gamestart / move / settle），依序；每筆附 _ts 時間戳。"""
    out = []
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT action, timestamp FROM game_log WHERE game_id=? ORDER BY id", (game_id,)
        ).fetchall()
    for r in rows:
        try:
            a = json.loads(r["action"])
            a["_ts"] = r["timestamp"]
            out.append(a)
        except Exception:
            pass
    return out


def get_settle_logs(game_id: str) -> list[dict]:
    """某對局的每局結算事件（牌譜中 t=='settle' 者，依序）。"""
    out = []
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT action FROM game_log WHERE game_id=? ORDER BY id", (game_id,)
        ).fetchall()
    for r in rows:
        try:
            a = json.loads(r["action"])
            if a.get("t") == "settle":
                out.append(a)
        except Exception:
            pass
    return out


def get_hand_rates(user_id: str, mode: str) -> dict:
    """由結算牌譜統計某玩家在該模式「有牌譜對局」的逐局數據，供和了率／放銃率／副露率／
    平均和了打點等。回傳 hands（總局數）、agari、agari_pts（和了打點和）、tsumo、ron、
    houju、riichi、furo（副露局數），以及 games（有牌譜場數）。"""
    with get_connection() as conn:
        gids = [r["game_id"] for r in conn.execute(
            "SELECT DISTINCT game_id FROM game_records WHERE user_id=? AND mode=?",
            (user_id, mode)).fetchall()]
    o = dict(hands=0, agari=0, agari_pts=0, tsumo=0, ron=0, houju=0, riichi=0, furo=0, games=0)
    for gid in gids:
        if not gid:
            continue
        logs = get_settle_logs(gid)
        if not logs:
            continue
        o["games"] += 1
        for a in logs:
            o["hands"] += 1
            if user_id in set(a.get("winners", []) or []):
                o["agari"] += 1
                o["agari_pts"] += int((a.get("wp", {}) or {}).get(user_id, 0))
                if a.get("win") in ("tsumo", "nagashi"):
                    o["tsumo"] += 1
                elif a.get("win") in ("ron", "dblron"):
                    o["ron"] += 1
            if a.get("loser") == user_id:
                o["houju"] += 1
            if user_id in set(a.get("riichi", []) or []):
                o["riichi"] += 1
            if user_id in set(a.get("furo", []) or []):
                o["furo"] += 1
    return o


# ─── 段位／R（段位賽）─────────────────────────────────────────────────────────

def get_rating(user_id: str, mode: str) -> dict | None:
    """某玩家在該模式的段位／R；無紀錄回 None。"""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM user_rating WHERE user_id=? AND mode=?", (user_id, mode)
        ).fetchone()
    return dict(row) if row else None


def save_rating(user_id: str, mode: str, dan_idx: int, dan_pt: int,
                rate: float, games: int, username: str = None) -> None:
    now = datetime.utcnow().isoformat()
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO user_rating (user_id,mode,dan_idx,dan_pt,rate,games,username,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(user_id,mode) DO UPDATE SET "
            "dan_idx=excluded.dan_idx, dan_pt=excluded.dan_pt, rate=excluded.rate, "
            "games=excluded.games, username=COALESCE(excluded.username, user_rating.username), "
            "updated_at=excluded.updated_at",
            (user_id, mode, dan_idx, dan_pt, rate, games, username, now)
        )


def apply_ranked_game(mode: str, results: list[tuple[str, int]],
                      names: dict[str, str] = None) -> dict[str, dict]:
    """段位賽結束後，依各座位順位更新真人的段位／R。
    results：[(user_id, rank), …]，含所有座位（bot 也要在，用以算對手平均）。
    names：  {user_id: 顯示名}（可選，順便更新）。回傳已更新者的新數據。"""
    from . import rating
    names = names or {}
    rows = {}
    for uid, _ in results:
        r = get_rating(uid, mode)
        if r is not None:
            rows[uid] = r
        elif not uid.startswith("ai_"):    # 真人但還沒紀錄 → 以新手起算
            rows[uid] = {"dan_idx": 0, "dan_pt": 0, "rate": rating.START_RATE, "games": 0}
    updated = rating.apply_game(rows, results, mode == "sanma")
    for uid, v in updated.items():
        save_rating(uid, mode, v["dan_idx"], v["dan_pt"], v["rate"], v["games"],
                    username=names.get(uid))
    return updated


def get_leaderboard(mode: str, limit: int = 50) -> list[dict]:
    """段位排行榜：先比段位階級、再比段位點數、再比 R。"""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM user_rating WHERE mode=? AND games>0 "
            "ORDER BY dan_idx DESC, dan_pt DESC, rate DESC LIMIT ?",
            (mode, limit)
        ).fetchall()
    return [dict(r) for r in rows]


# ─── 任務／活躍度 ─────────────────────────────────────────────────────────────

CHECKIN_BASE   = 10        # 每日簽到基礎活躍度
CHECKIN_STREAK = 2         # 每連續一天額外活躍度（上限見下）
CHECKIN_MAXBONUS = 7       # 連續加成天數上限
PLAY_REWARD    = 20        # 每日完成一場對局的活躍度


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def get_activity(user_id: str) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM user_activity WHERE user_id=?", (user_id,)
        ).fetchone()
    return dict(row) if row else None


def _save_activity(user_id: str, username, activity: int, streak: int,
                   last_checkin, last_play) -> None:
    now = datetime.utcnow().isoformat()
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO user_activity (user_id,username,activity,streak,last_checkin,last_play,updated_at) "
            "VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "username=COALESCE(excluded.username, user_activity.username), "
            "activity=excluded.activity, streak=excluded.streak, "
            "last_checkin=excluded.last_checkin, last_play=excluded.last_play, "
            "updated_at=excluded.updated_at",
            (user_id, username, activity, streak, last_checkin, last_play, now)
        )


def checkin(user_id: str, username: str = None) -> dict:
    """每日簽到。回傳 {already, reward, streak, activity}。"""
    today = _today()
    row = get_activity(user_id) or {"activity": 0, "streak": 0,
                                    "last_checkin": None, "last_play": None}
    if row["last_checkin"] == today:
        return {"already": True, "reward": 0,
                "streak": row["streak"], "activity": row["activity"]}
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    streak = row["streak"] + 1 if row["last_checkin"] == yesterday else 1
    reward = CHECKIN_BASE + min(streak, CHECKIN_MAXBONUS) * CHECKIN_STREAK
    activity = (row["activity"] or 0) + reward
    _save_activity(user_id, username, activity, streak, today, row["last_play"])
    return {"already": False, "reward": reward, "streak": streak, "activity": activity}


def reward_play(user_id: str, username: str = None) -> int:
    """每日第一場對局結束時呼叫；當天已領過回 0，否則回獎勵點數。"""
    today = _today()
    row = get_activity(user_id)
    if row and row["last_play"] == today:
        return 0
    activity = ((row["activity"] if row else 0) or 0) + PLAY_REWARD
    streak   = row["streak"] if row else 0
    last_ci  = row["last_checkin"] if row else None
    _save_activity(user_id, username, activity, streak, last_ci, today)
    return PLAY_REWARD


def update_best_win(user_id: str, points: int, name: str = "", hand: str = "",
                    username: str = None) -> None:
    """更新玩家的最高和了（只在打點更大時覆寫，連牌型一起存）。"""
    now = datetime.utcnow().isoformat()
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO user_activity (user_id,username,best_win,best_win_name,best_win_hand,updated_at) "
            "VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "username=COALESCE(excluded.username, user_activity.username), "
            "best_win_name=CASE WHEN excluded.best_win > user_activity.best_win "
            "THEN excluded.best_win_name ELSE user_activity.best_win_name END, "
            "best_win_hand=CASE WHEN excluded.best_win > user_activity.best_win "
            "THEN excluded.best_win_hand ELSE user_activity.best_win_hand END, "
            "best_win=MAX(user_activity.best_win, excluded.best_win), "
            "updated_at=excluded.updated_at",
            (user_id, username, points, name, hand, now)
        )


def get_activity_leaderboard(limit: int = 100) -> list[dict]:
    """活躍度排行榜：依活躍度、連續簽到由高到低。"""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM user_activity WHERE activity>0 "
            "ORDER BY activity DESC, streak DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_known_players(limit: int = 1000) -> list[dict]:
    """所有「玩過這機器人」的人：有對局紀錄或簽到過的都算。
    回傳 [{uid, username, games, activity, checked_in}]，依對局數→活躍度排序。"""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT uid, MAX(username) AS username, SUM(games) AS games, "
            "MAX(activity) AS activity, MAX(checked_in) AS checked_in FROM ("
            "  SELECT user_id AS uid, MAX(username) AS username, COUNT(*) AS games, "
            "         0 AS activity, 0 AS checked_in FROM game_records GROUP BY user_id"
            "  UNION ALL"
            "  SELECT user_id, username, 0, activity, "
            "         CASE WHEN last_checkin IS NOT NULL THEN 1 ELSE 0 END "
            "  FROM user_activity"
            ") GROUP BY uid ORDER BY games DESC, activity DESC LIMIT ?",
            (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def task_status(user_id: str) -> dict:
    """今日任務狀態：{checkin, played, activity, streak}。"""
    today = _today()
    row = get_activity(user_id)
    if not row:
        return {"checkin": False, "played": False, "activity": 0, "streak": 0}
    return {"checkin": row["last_checkin"] == today,
            "played":  row["last_play"] == today,
            "activity": row["activity"] or 0, "streak": row["streak"] or 0}


if __name__ == "__main__":
    init_db()