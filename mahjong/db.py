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
                -- 人類可讀房間流水號（房間#0001）
                room_config TEXT,
                -- 房間設定 JSON（重連回復對局時用：length/tobi/start_points…）
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

            -- 玩家偏好（顯示語言等）
            CREATE TABLE IF NOT EXISTS user_prefs (
                user_id TEXT PRIMARY KEY,
                lang    TEXT NOT NULL DEFAULT 'zh_tw'
            );

            -- 任務／活躍度（每日簽到、每日對局）
            CREATE TABLE IF NOT EXISTS user_activity (
                user_id      TEXT PRIMARY KEY,
                username     TEXT,
                activity     INTEGER NOT NULL DEFAULT 0,   -- 活躍度總點
                streak       INTEGER NOT NULL DEFAULT 0,   -- 連續簽到天數
                last_checkin TEXT,                         -- 最後簽到日 YYYY-MM-DD
                last_play    TEXT,                         -- 最後領「對局獎勵」日 YYYY-MM-DD
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
        # 舊資料庫補欄位（已存在則略過）
        try:
            conn.execute("ALTER TABLE games ADD COLUMN room_no INTEGER")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE games ADD COLUMN room_config TEXT")
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


def _counts_from_settles(logs: list[dict]) -> dict[str, dict]:
    """由結算牌譜重算每位玩家的各項次數（與對局中即時統計的口徑一致）。"""
    agg: dict[str, dict] = {}
    for a in logs:
        deltas  = a.get("deltas", {}) or {}
        winners = set(a.get("winners", []) or [])
        loser   = a.get("loser")
        riichi  = set(a.get("riichi", []) or [])
        win     = a.get("win", "")
        uids = set(deltas) | winners | riichi | ({loser} if loser else set())
        for uid in uids:
            c = agg.setdefault(uid, dict(tsumo=0, ron=0, houju=0,
                                         houju_points=0, gain_points=0, riichi=0))
            if uid in winners:
                if win in ("tsumo", "nagashi"):
                    c["tsumo"] += 1
                elif win in ("ron", "dblron"):
                    c["ron"] += 1
            if loser and uid == loser:
                c["houju"] += 1
                c["houju_points"] += max(0, -int(deltas.get(uid, 0)))
            c["gain_points"] += max(0, int(deltas.get(uid, 0)))
            if uid in riichi:
                c["riichi"] += 1
    return agg


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


def repair_game_records() -> dict:
    """掃描並修復 game_records（只動可由現有資料推導的欄位）：
      1) 去重：同 game_id+user_id 多筆 → 留最後一筆、刪其餘
      2) 重算次數：對有結算牌譜（settle）的對局，依牌譜重算自摸／榮和／放銃／
         放銃失點／獲得點數／立直，覆寫回紀錄（順位、終局點數不動，因牌譜含 bot 較完整但
         順位本就以全員計算正確）
      3) 收尾：放銃失點／獲得點數的負值歸零
    回傳統計報告。"""
    COUNT_COLS = ("tsumo", "ron", "houju", "houju_points", "gain_points", "riichi")
    rep = {"scanned": 0, "games": 0, "dups": 0, "counter_fixed": 0,
           "clamp_fixed": 0, "no_log_games": 0}
    with get_connection() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM game_records ORDER BY id").fetchall()]
        rep["scanned"] = len(rows)
        by_game: dict[str, list] = {}
        for r in rows:
            by_game.setdefault(r["game_id"], []).append(r)
        rep["games"] = len(by_game)

        for gid, recs in by_game.items():
            # 1) 去重：同 user_id 多筆 → 留 id 最大者
            seen: dict[str, list] = {}
            for r in recs:
                seen.setdefault(r["user_id"], []).append(r)
            kept = []
            for uid, lst in seen.items():
                lst.sort(key=lambda x: x["id"])
                for dead in lst[:-1]:
                    conn.execute("DELETE FROM game_records WHERE id=?", (dead["id"],))
                    rep["dups"] += 1
                kept.append(lst[-1])

            # 2) 由結算牌譜重算次數
            logs = get_settle_logs(gid) if gid else []
            if logs:
                agg = _counts_from_settles(logs)
                for r in kept:
                    c = agg.get(r["user_id"])
                    if not c:
                        continue
                    if tuple(r[k] for k in COUNT_COLS) != tuple(c[k] for k in COUNT_COLS):
                        conn.execute(
                            "UPDATE game_records SET tsumo=?,ron=?,houju=?,houju_points=?,"
                            "gain_points=?,riichi=? WHERE id=?",
                            (c["tsumo"], c["ron"], c["houju"], c["houju_points"],
                             c["gain_points"], c["riichi"], r["id"]))
                        rep["counter_fixed"] += 1
                        r.update(c)
            else:
                rep["no_log_games"] += 1

            # 3) 負值歸零
            for r in kept:
                fixes = {k: 0 for k in ("houju_points", "gain_points") if r[k] < 0}
                if fixes:
                    for k, v in fixes.items():
                        conn.execute(f"UPDATE game_records SET {k}=? WHERE id=?", (v, r["id"]))
                    rep["clamp_fixed"] += 1
    return rep


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


def get_activity_leaderboard(limit: int = 100) -> list[dict]:
    """活躍度排行榜：依活躍度、連續簽到由高到低。"""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM user_activity WHERE activity>0 "
            "ORDER BY activity DESC, streak DESC LIMIT ?", (limit,)
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