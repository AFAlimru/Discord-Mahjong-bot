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
import time
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
                voice_pack   TEXT,                              -- 個人角色語音（語音包資料夾名）
                ai_voice     TEXT                               -- 自己開的房裡電腦用的語音包（NULL＝沒設定過→預設；''＝電腦不出聲）
            );

            -- 伺服器設定
            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id         TEXT PRIMARY KEY,
                play_channel_id  TEXT,      -- 管理員用 /setup channel 指定的遊玩頻道
                guild_lang       TEXT,      -- 伺服器主要語言
                guide_dismissed  INTEGER NOT NULL DEFAULT 0   -- 管理員按過「刪除指南頻道」＝之後不再自動建
            );

            -- 大廳（/setup create 建的一套：類別＋按鈕大廳＋開房文字頻道＋配對語音）。
            -- 一台伺服器可以有好幾個，每種語言一個；從哪個大廳開房就用那個大廳的語言。
            CREATE TABLE IF NOT EXISTS guild_hubs (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id         TEXT NOT NULL,
                lang             TEXT,      -- 大廳語言（舊資料轉過來時未知＝NULL，啟動時依頻道名稱補上）
                category_id      TEXT,
                lobby_channel_id TEXT,
                hub_voice_id     TEXT,
                play_channel_id  TEXT,      -- 這個大廳的開房文字頻道
                created_at       TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_hubs_guild ON guild_hubs (guild_id);

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

            -- 比賽（0.9.1）：主辦建立 → 報名／報到 → 抽選 → 預賽（每輪重新分桌）→ 決賽
            CREATE TABLE IF NOT EXISTS tournaments (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                name             TEXT NOT NULL,
                guild_id         TEXT NOT NULL,
                channel_id       TEXT NOT NULL,       -- 開桌與公告的頻道
                organizer_id     TEXT NOT NULL,
                start_at         INTEGER NOT NULL,    -- 開賽時間（unix 秒，UTC）
                status           TEXT NOT NULL DEFAULT 'draft',
                -- 'draft' | 'registration' | 'running' | 'finished' | 'cancelled'
                visibility       TEXT NOT NULL DEFAULT 'public',    -- 'public' | 'private'
                join_mode        TEXT NOT NULL DEFAULT 'open',      -- 'open' | 'invite'（只有主辦能加人）
                max_entrants     INTEGER,
                require_checkin  INTEGER NOT NULL DEFAULT 1,
                checkin_minutes  INTEGER NOT NULL DEFAULT 30,
                rules            TEXT NOT NULL DEFAULT '{}',        -- JSON：規則預設／戰長／思考時間…（同房間設定的鍵）
                format           TEXT NOT NULL DEFAULT '{}',        -- JSON：預賽輪數／晉級人數／決賽輪數／每桌人數…
                state            TEXT NOT NULL DEFAULT '{}',        -- JSON：排程用的內部紀錄（已公告的輪次、重試時間）
                announce_message_id TEXT,
                created_at       INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_tour_guild ON tournaments (guild_id, status);

            CREATE TABLE IF NOT EXISTS tournament_entrants (
                tid            INTEGER NOT NULL REFERENCES tournaments(id) ON DELETE CASCADE,
                user_id        TEXT NOT NULL,
                username       TEXT NOT NULL,
                status         TEXT NOT NULL DEFAULT 'registered',
                -- 'registered' | 'selected' | 'waitlist' | 'withdrawn'
                checked_in_at  INTEGER,
                source         TEXT NOT NULL DEFAULT 'web',         -- 'web' | 'discord' | 'manual'
                draw_order     INTEGER,                             -- 抽選後的順序（候補補位照這個）
                created_at     INTEGER NOT NULL,
                PRIMARY KEY (tid, user_id)
            );

            CREATE TABLE IF NOT EXISTS tournament_tables (
                tid          INTEGER NOT NULL REFERENCES tournaments(id) ON DELETE CASCADE,
                stage        TEXT NOT NULL,                         -- 'prelim' | 'final'
                round_no     INTEGER NOT NULL,
                table_no     INTEGER NOT NULL,
                gid          TEXT,                                  -- 對局 id（開桌或綁定後才有）
                seats        TEXT NOT NULL DEFAULT '[]',            -- JSON：座位順序的 user_id，電腦是 "cpu"
                status       TEXT NOT NULL DEFAULT 'pending',       -- 'pending' | 'running' | 'finished' | 'void'
                closed       INTEGER NOT NULL DEFAULT 0,            -- void 後主辦決定不重打＝1（算這輪已結束）
                started_at   INTEGER,
                finished_at  INTEGER,
                PRIMARY KEY (tid, stage, round_no, table_no)
            );
            CREATE INDEX IF NOT EXISTS idx_tour_tables_gid ON tournament_tables (gid);

            CREATE TABLE IF NOT EXISTS tournament_results (
                tid          INTEGER NOT NULL REFERENCES tournaments(id) ON DELETE CASCADE,
                stage        TEXT NOT NULL,
                round_no     INTEGER NOT NULL,
                table_no     INTEGER NOT NULL,
                user_id      TEXT NOT NULL,
                seat         INTEGER NOT NULL,                      -- 0＝起家
                rank         INTEGER NOT NULL,                      -- 該桌順位（含電腦）
                final_points INTEGER NOT NULL,
                score        REAL NOT NULL,                         -- 精算 pt（小數 1 位）
                PRIMARY KEY (tid, stage, round_no, table_no, user_id)
            );
        """)
        try:                                       # 0.7.x 資料庫升級到 0.8：個人角色語音
            conn.execute("ALTER TABLE user_prefs ADD COLUMN voice_pack TEXT")
        except Exception:
            pass
        try:                                       # 0.8.5：電腦語音
            conn.execute("ALTER TABLE user_prefs ADD COLUMN ai_voice TEXT")
        except Exception:
            pass
        for col, ddl in (("showcase_game", "TEXT"), ("showcase_hand", "INTEGER")):
            try:                                   # 0.9.2：個人資訊卡的「最大和了」可以改選哪一次和牌
                conn.execute(f"ALTER TABLE user_activity ADD COLUMN {col} {ddl}")
            except Exception:
                pass
        try:                                       # 0.8.5：記住管理員刪過指南頻道
            conn.execute("ALTER TABLE guild_settings ADD COLUMN guide_dismissed INTEGER NOT NULL DEFAULT 0")
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
        if conn.execute("PRAGMA user_version").fetchone()[0] < 2:
            # 0.8.4 多語言大廳：把 guild_settings 裡原本那一個大廳搬到 guild_hubs（語言先留空，啟動時補）
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(guild_settings)").fetchall()}
            if "category_id" in cols:
                for r in conn.execute(
                        "SELECT guild_id, category_id, lobby_channel_id, hub_voice_id, play_channel_id "
                        "FROM guild_settings WHERE category_id IS NOT NULL").fetchall():
                    conn.execute(
                        "INSERT INTO guild_hubs (guild_id, lang, category_id, lobby_channel_id, hub_voice_id,"
                        " play_channel_id, created_at) VALUES (?, NULL, ?, ?, ?, ?, ?)",
                        (r["guild_id"], r["category_id"], r["lobby_channel_id"], r["hub_voice_id"],
                         r["play_channel_id"], datetime.utcnow().isoformat()))
            conn.execute("PRAGMA user_version = 2")
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


def get_user_ai_voice(user_id: str) -> str | None:
    """玩家設定的「電腦語音」：他當房主（或同桌）時電腦用的語音包；None＝電腦不出聲。"""
    with get_connection() as conn:
        row = conn.execute("SELECT ai_voice FROM user_prefs WHERE user_id=?",
                           (user_id,)).fetchone()
    return row["ai_voice"] if row else None


def set_user_ai_voice(user_id: str, pack: str | None) -> None:
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO user_prefs (user_id, ai_voice) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET ai_voice=excluded.ai_voice",
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


def get_guide_dismissed(guild_id: str) -> bool:
    """管理員是否按過「刪除指南頻道」（是＝重啟時不再自動建指南）。"""
    with get_connection() as conn:
        row = conn.execute("SELECT guide_dismissed FROM guild_settings WHERE guild_id=?",
                           (str(guild_id),)).fetchone()
    return bool(row and row["guide_dismissed"])


def set_guide_dismissed(guild_id: str, dismissed: bool) -> None:
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO guild_settings (guild_id, guide_dismissed) VALUES (?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET guide_dismissed=excluded.guide_dismissed",
            (str(guild_id), 1 if dismissed else 0))


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


def get_hubs(guild_id: str) -> list[dict]:
    """該伺服器的所有大廳（建立順序），每筆含 id／lang／category_id／lobby_channel_id／hub_voice_id／play_channel_id。"""
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM guild_hubs WHERE guild_id=? ORDER BY id",
                            (str(guild_id),)).fetchall()
    return [dict(r) for r in rows]


def add_hub(guild_id: str, lang: str, category_id, lobby_channel_id, hub_voice_id,
            play_channel_id) -> None:
    to_s = lambda x: str(x) if x else None
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO guild_hubs (guild_id, lang, category_id, lobby_channel_id, hub_voice_id,"
            " play_channel_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (str(guild_id), lang, to_s(category_id), to_s(lobby_channel_id), to_s(hub_voice_id),
             to_s(play_channel_id), datetime.utcnow().isoformat()))


def set_hub_lang(hub_id: int, lang: str) -> None:
    with get_connection() as conn:
        conn.execute("UPDATE guild_hubs SET lang=? WHERE id=?", (lang, hub_id))


def delete_hub(hub_id: int) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM guild_hubs WHERE id=?", (hub_id,))


def get_guild_setup(guild_id: str) -> dict:
    """第一個大廳的 category / 大廳頻道 / 配對語音（只需要「有沒有大廳」的地方用）。缺者為 None。"""
    hubs = get_hubs(guild_id)
    h = hubs[0] if hubs else {}
    return {"category_id": h.get("category_id"), "lobby_channel_id": h.get("lobby_channel_id"),
            "hub_voice_id": h.get("hub_voice_id")}


def get_all_guild_settings() -> dict:
    """一次讀出全部伺服器設定（後台列表用，避免逐台查詢）。guild_id -> dict。"""
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM guild_settings").fetchall()
    return {r["guild_id"]: dict(r) for r in rows}


def delete_guild_settings(guild_id: str) -> None:
    """移除該伺服器的全部設定（所有大廳、遊玩頻道、主要語言）。機器人退出並清理後用。"""
    with get_connection() as conn:
        conn.execute("DELETE FROM guild_settings WHERE guild_id=?", (guild_id,))
        conn.execute("DELETE FROM guild_hubs WHERE guild_id=?", (guild_id,))


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


def game_record_count(game_id: str) -> int:
    """這局的戰績筆數（正常打完才會有；被中途結束的沒有）。"""
    with get_connection() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM game_records WHERE game_id=?", (game_id,)).fetchone()
    return row["n"] if row else 0


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

CHECKIN_BASE  = 10        # 每日簽到基礎活躍度
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


# ─── 個人資訊卡的「最大和了」（可改選牌譜裡的某一次和牌）─────────────────────

def _win_from(a: dict, uid: str, game_id: str, hand_no: int, ts: str = "", room_no=None) -> dict | None:
    """由一筆結算事件取出 uid 的和牌資料；uid 沒有和牌＝None。"""
    wp = a.get("wp") or {}
    if uid not in wp:
        return None
    wd = (a.get("wd") or {}).get(uid) or {}
    return {
        "game_id": game_id, "hand_no": hand_no, "room_no": room_no, "ts": ts,
        "points": int(wp[uid] or 0),
        "name": wd.get("name") or "",
        "yaku": wd.get("yaku") or [], "yakuman": wd.get("yakuman") or [],
        "han": wd.get("han") or 0, "fu": wd.get("fu") or 0,
        "hand": (a.get("wh") or {}).get(uid) or "",
        "type": a.get("win") or "",
        "dora": a.get("dora") or [], "ura": a.get("ura") or [],
    }


def list_user_wins(user_id: str, limit: int = 300) -> list[dict]:
    """某玩家在自己打過的對局牌譜裡的所有和牌（新到舊，最多 limit 筆）。
    每筆 hand_no ＝ 該場第幾局（從 0 起，與回放的局序相同）。"""
    uid = str(user_id)
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT s.game_id, s.rn, s.action, s.timestamp, g.room_no FROM ("
            "  SELECT game_id, id, action, timestamp, "
            "         ROW_NUMBER() OVER (PARTITION BY game_id ORDER BY id) - 1 AS rn "
            "  FROM game_log WHERE action LIKE '%\"t\": \"settle\"%' "
            "    AND game_id IN (SELECT game_id FROM game_records WHERE user_id=? AND game_id IS NOT NULL)"
            ") s LEFT JOIN games g ON g.game_id = s.game_id "
            "WHERE s.action LIKE ? ORDER BY s.timestamp DESC, s.id DESC",
            (uid, f'%"{uid}"%')
        ).fetchall()
    out = []
    for r in rows:
        try:
            a = json.loads(r["action"])
        except Exception:
            continue
        w = _win_from(a, uid, r["game_id"], r["rn"], r["timestamp"] or "", r["room_no"])
        if w:
            out.append(w)
            if len(out) >= limit:
                break
    return out


def get_user_win(user_id: str, game_id: str, hand_no: int) -> dict | None:
    """指定的那一次和牌；這場不是該玩家打的、牌譜不存在、那局沒和牌＝None。"""
    uid = str(user_id)
    try:
        hand_no = int(hand_no)
    except (TypeError, ValueError):
        return None
    with get_connection() as conn:
        if not conn.execute("SELECT 1 FROM game_records WHERE game_id=? AND user_id=? LIMIT 1",
                            (game_id, uid)).fetchone():
            return None
        row = conn.execute("SELECT room_no FROM games WHERE game_id=?", (game_id,)).fetchone()
        rows = conn.execute("SELECT action, timestamp FROM game_log WHERE game_id=? "
                            "AND action LIKE '%\"t\": \"settle\"%' ORDER BY id", (game_id,)).fetchall()
    if not row or not (0 <= hand_no < len(rows)):
        return None
    try:
        a = json.loads(rows[hand_no]["action"])
    except Exception:
        return None
    return _win_from(a, uid, game_id, hand_no, rows[hand_no]["timestamp"] or "", row["room_no"])


def set_showcase_win(user_id: str, game_id: str, hand_no: int, username: str = None) -> None:
    """記下玩家要展示的和牌（呼叫端要先用 get_user_win 確認是他自己的）。"""
    now = datetime.utcnow().isoformat()
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO user_activity (user_id,username,showcase_game,showcase_hand,updated_at) "
            "VALUES (?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET "
            "showcase_game=excluded.showcase_game, showcase_hand=excluded.showcase_hand, "
            "updated_at=excluded.updated_at",
            (user_id, username, game_id, int(hand_no), now))


def clear_showcase_win(user_id: str) -> None:
    """回到自動（顯示打點最高的那次）。"""
    with get_connection() as conn:
        conn.execute("UPDATE user_activity SET showcase_game=NULL, showcase_hand=NULL WHERE user_id=?",
                     (user_id,))


def get_showcase_choice(user_id: str, act: dict | None = None) -> dict | None:
    """玩家選的和牌（還找得到才回傳）；沒選、牌譜已刪、那局不是他和的＝None。"""
    act = act if act is not None else (get_activity(user_id) or {})
    gid = act.get("showcase_game")
    if not gid or act.get("showcase_hand") is None:
        return None
    return get_user_win(user_id, gid, act["showcase_hand"])


def get_display_best_win(user_id: str, act: dict | None = None) -> dict | None:
    """資訊卡／個人頁顯示的「最大和了」：有選且還找得到就用選的，否則用自動記錄的最高和了。
    回傳 {points, name, hand, chosen, game_id, hand_no, room_no}；完全沒有和牌＝None。"""
    act = act if act is not None else (get_activity(user_id) or {})
    w = get_showcase_choice(user_id, act)
    if w:
        w["chosen"] = True
        return w
    pts = act.get("best_win", 0) or 0
    if not pts:
        return None
    return {"points": pts, "name": act.get("best_win_name") or "", "hand": act.get("best_win_hand") or "",
            "chosen": False, "game_id": None, "hand_no": None, "room_no": None}


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


# ─── 比賽（tournaments）──────────────────────────────────────────────────────

_TOUR_JSON = ("rules", "format", "state")
_TOUR_COLS = ("name", "guild_id", "channel_id", "organizer_id", "start_at", "status", "visibility",
              "join_mode", "max_entrants", "require_checkin", "checkin_minutes", "rules", "format",
              "state", "announce_message_id")


def _tour_row(r) -> dict:
    d = dict(r)
    for k in _TOUR_JSON:
        try:
            d[k] = json.loads(d.get(k) or "{}")
        except Exception:
            d[k] = {}
    d["require_checkin"] = bool(d["require_checkin"])
    return d


def tour_create(**f) -> int:
    f.setdefault("created_at", int(time.time()))
    for k in _TOUR_JSON:
        if k in f and not isinstance(f[k], str):
            f[k] = json.dumps(f[k], ensure_ascii=False)
    cols = [k for k in f if k in _TOUR_COLS or k == "created_at"]
    with get_connection() as conn:
        cur = conn.execute(
            f"INSERT INTO tournaments ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            [int(f[k]) if k == "require_checkin" else f[k] for k in cols])
        return cur.lastrowid


def tour_update(tid: int, **f) -> None:
    sets, vals = [], []
    for k, v in f.items():
        if k not in _TOUR_COLS:
            raise KeyError(k)
        if k in _TOUR_JSON and not isinstance(v, str):
            v = json.dumps(v, ensure_ascii=False)
        if k == "require_checkin":
            v = int(bool(v))
        sets.append(f"{k}=?")
        vals.append(v)
    if not sets:
        return
    with get_connection() as conn:
        conn.execute(f"UPDATE tournaments SET {','.join(sets)} WHERE id=?", vals + [tid])


def tour_get(tid: int) -> dict | None:
    with get_connection() as conn:
        r = conn.execute("SELECT * FROM tournaments WHERE id=?", (tid,)).fetchone()
    return _tour_row(r) if r else None


def tour_list(statuses=None, guild_id: str | None = None) -> list[dict]:
    q, a = "SELECT * FROM tournaments WHERE 1=1", []
    if statuses:
        q += f" AND status IN ({','.join('?' * len(statuses))})"
        a += list(statuses)
    if guild_id is not None:
        q += " AND guild_id=?"
        a.append(str(guild_id))
    with get_connection() as conn:
        rows = conn.execute(q + " ORDER BY start_at DESC, id DESC", a).fetchall()
    return [_tour_row(r) for r in rows]


def tour_ent_get(tid: int, user_id: str) -> dict | None:
    with get_connection() as conn:
        r = conn.execute("SELECT * FROM tournament_entrants WHERE tid=? AND user_id=?",
                         (tid, str(user_id))).fetchone()
    return dict(r) if r else None


def tour_ent_list(tid: int) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM tournament_entrants WHERE tid=? ORDER BY created_at, rowid",
                            (tid,)).fetchall()
    return [dict(r) for r in rows]


def tour_ent_put(tid: int, user_id: str, username: str, status: str, source: str,
                 created_at: int | None = None) -> None:
    """新增報名；已有紀錄（含 withdrawn）＝改回這個狀態並更新名稱／來源／報名時間，報到與抽選順序清掉。"""
    now = int(created_at if created_at is not None else time.time())
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO tournament_entrants (tid,user_id,username,status,source,created_at) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(tid,user_id) DO UPDATE SET username=excluded.username,"
            " status=excluded.status, source=excluded.source, created_at=excluded.created_at,"
            " checked_in_at=NULL, draw_order=NULL",
            (tid, str(user_id), username, status, source, now))


def tour_ent_set(tid: int, user_id: str, **f) -> None:
    ok = {"status", "checked_in_at", "draw_order", "username"}
    sets = [k for k in f if k in ok]
    if len(sets) != len(f):
        raise KeyError(set(f) - ok)
    with get_connection() as conn:
        conn.execute(f"UPDATE tournament_entrants SET {','.join(k + '=?' for k in sets)} "
                     "WHERE tid=? AND user_id=?", [f[k] for k in sets] + [tid, str(user_id)])


def _tbl_row(r) -> dict:
    d = dict(r)
    try:
        d["seats"] = json.loads(d.get("seats") or "[]")
    except Exception:
        d["seats"] = []
    d["closed"] = bool(d["closed"])
    return d


def tour_tbl_put(tid: int, stage: str, round_no: int, table_no: int, seats: list,
                 status: str = "pending", gid: str | None = None) -> None:
    with get_connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO tournament_tables (tid,stage,round_no,table_no,gid,seats,status,closed)"
            " VALUES (?,?,?,?,?,?,?,0)",
            (tid, stage, round_no, table_no, gid, json.dumps(seats), status))


def tour_tbl_set(tid: int, stage: str, round_no: int, table_no: int, **f) -> None:
    ok = {"gid", "seats", "status", "closed", "started_at", "finished_at"}
    if set(f) - ok:
        raise KeyError(set(f) - ok)
    if "seats" in f and not isinstance(f["seats"], str):
        f["seats"] = json.dumps(f["seats"])
    if "closed" in f:
        f["closed"] = int(bool(f["closed"]))
    with get_connection() as conn:
        conn.execute(f"UPDATE tournament_tables SET {','.join(k + '=?' for k in f)} "
                     "WHERE tid=? AND stage=? AND round_no=? AND table_no=?",
                     list(f.values()) + [tid, stage, round_no, table_no])


def tour_tbl_list(tid: int) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM tournament_tables WHERE tid=? "
            "ORDER BY (stage='final'), round_no, table_no", (tid,)).fetchall()
    return [_tbl_row(r) for r in rows]


def tour_tbl_by_gid(gid: str) -> dict | None:
    with get_connection() as conn:
        r = conn.execute("SELECT * FROM tournament_tables WHERE gid=? ORDER BY rowid DESC LIMIT 1",
                         (gid,)).fetchone()
    return _tbl_row(r) if r else None


def tour_tbl_delete(tid: int, stage: str, round_no: int, table_no: int | None = None) -> None:
    with get_connection() as conn:
        q = "DELETE FROM tournament_tables WHERE tid=? AND stage=? AND round_no=?"
        a = [tid, stage, round_no]
        if table_no is not None:
            q += " AND table_no=?"
            a.append(table_no)
        conn.execute(q, a)


def tour_res_replace(tid: int, stage: str, round_no: int, table_no: int, rows: list[dict]) -> None:
    """整桌結果換成 rows（先清掉舊的；rows 空＝只清掉）。"""
    with get_connection() as conn:
        conn.execute("DELETE FROM tournament_results WHERE tid=? AND stage=? AND round_no=? AND table_no=?",
                     (tid, stage, round_no, table_no))
        for r in rows:
            conn.execute(
                "INSERT INTO tournament_results (tid,stage,round_no,table_no,user_id,seat,rank,final_points,score)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (tid, stage, round_no, table_no, str(r["user_id"]), r["seat"], r["rank"],
                 r["final_points"], r["score"]))


def tour_res_list(tid: int) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM tournament_results WHERE tid=? "
                            "ORDER BY (stage='final'), round_no, table_no, rank", (tid,)).fetchall()
    return [dict(r) for r in rows]
