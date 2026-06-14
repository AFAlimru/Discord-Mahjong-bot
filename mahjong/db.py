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
from datetime import datetime
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
                -- 人類可讀房間流水號（房間#0001）
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS players (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                game_id     TEXT NOT NULL REFERENCES games(game_id) ON DELETE CASCADE,
                user_id     TEXT NOT NULL,
                username    TEXT NOT NULL,
                seat        INTEGER NOT NULL,
                -- 0=East 1=South 2=West 3=North
                score       INTEGER NOT NULL DEFAULT 25000,
                is_bot      INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS stats (
                user_id     TEXT PRIMARY KEY,
                username    TEXT NOT NULL,
                games_played INTEGER NOT NULL DEFAULT 0,
                wins         INTEGER NOT NULL DEFAULT 0,
                tsumo_wins   INTEGER NOT NULL DEFAULT 0,
                ron_wins     INTEGER NOT NULL DEFAULT 0,
                riichi_count INTEGER NOT NULL DEFAULT 0,
                total_score  INTEGER NOT NULL DEFAULT 0,
                updated_at   TEXT NOT NULL
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
        """)
        # 舊資料庫補欄位（已存在則略過）
        try:
            conn.execute("ALTER TABLE games ADD COLUMN room_no INTEGER")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE game_records ADD COLUMN gain_points INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass
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


def get_game(game_id: str) -> dict | None:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM games WHERE game_id=?", (game_id,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["game_data"] = json.loads(d["game_data"])
    return d


def get_active_game_in_channel(channel_id: str) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM games WHERE channel_id=? AND state IN ('waiting','playing')",
            (channel_id,)
        ).fetchone()
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


# ─── Player CRUD ──────────────────────────────────────────────────────────────

def add_player(game_id: str, user_id: str, username: str,
               seat: int, score: int = 25000, is_bot: bool = False) -> None:
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO players (game_id,user_id,username,seat,score,is_bot) VALUES (?,?,?,?,?,?)",
            (game_id, user_id, username, seat, score, 1 if is_bot else 0)
        )


def get_players(game_id: str) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM players WHERE game_id=? ORDER BY seat", (game_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def update_player_score(game_id: str, user_id: str, score: int) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE players SET score=? WHERE game_id=? AND user_id=?",
            (score, game_id, user_id)
        )


# ─── Stats CRUD ───────────────────────────────────────────────────────────────

def upsert_stats(user_id: str, username: str, *,
                 win: bool = False, tsumo: bool = False,
                 ron: bool = False, riichi: bool = False,
                 score_delta: int = 0) -> None:
    now = datetime.utcnow().isoformat()
    with get_connection() as conn:
        existing = conn.execute(
            "SELECT * FROM stats WHERE user_id=?", (user_id,)
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO stats (user_id,username,games_played,wins,tsumo_wins,ron_wins,"
                "riichi_count,total_score,updated_at) VALUES (?,?,1,?,?,?,?,?,?)",
                (user_id, username,
                 1 if win else 0, 1 if tsumo else 0, 1 if ron else 0,
                 1 if riichi else 0, score_delta, now)
            )
        else:
            conn.execute(
                "UPDATE stats SET username=?, games_played=games_played+1, "
                "wins=wins+?, tsumo_wins=tsumo_wins+?, ron_wins=ron_wins+?, "
                "riichi_count=riichi_count+?, total_score=total_score+?, updated_at=? "
                "WHERE user_id=?",
                (username, 1 if win else 0, 1 if tsumo else 0, 1 if ron else 0,
                 1 if riichi else 0, score_delta, now, user_id)
            )


def get_stats(user_id: str) -> dict | None:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM stats WHERE user_id=?", (user_id,)).fetchone()
    return dict(row) if row else None


def get_leaderboard(limit: int = 10) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM stats ORDER BY total_score DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


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


def get_total_games(user_id: str) -> int:
    """某玩家所有模式的總對局場數。"""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM game_records WHERE user_id=?", (user_id,)
        ).fetchone()
    return (row["c"] or 0) if row else 0


def get_recent_records(user_id: str, mode: str, limit: int = 20) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM game_records WHERE user_id=? AND mode=? ORDER BY id DESC LIMIT ?",
            (user_id, mode, limit)
        ).fetchall()
    return [dict(r) for r in rows]


if __name__ == "__main__":
    init_db()