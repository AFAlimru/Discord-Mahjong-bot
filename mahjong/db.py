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
        """)
    print(f"[DB] Database initialised at: {DATABASE_PATH}")


# ─── Game CRUD ────────────────────────────────────────────────────────────────

def create_game(game_id: str, guild_id: str, channel_id: str,
                wall_seed: str | None = None) -> None:
    now = datetime.utcnow().isoformat()
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO games (game_id,guild_id,channel_id,state,game_data,wall_seed,created_at,updated_at) "
            "VALUES (?,?,?,'waiting','{}',?,?,?)",
            (game_id, guild_id, channel_id, wall_seed, now, now)
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


if __name__ == "__main__":
    init_db()