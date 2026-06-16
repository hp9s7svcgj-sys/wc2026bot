import sqlite3
from datetime import datetime
import pytz

IRAN_TZ = pytz.timezone("Asia/Tehran")

class Database:
    def __init__(self, db_path="wc2026.db"):
        self.db_path = db_path
        self._init_db()

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    name TEXT,
                    username TEXT,
                    total_score INTEGER DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS matches (
                    match_id INTEGER PRIMARY KEY,
                    home TEXT NOT NULL,
                    away TEXT NOT NULL,
                    match_time TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    result_home INTEGER,
                    result_away INTEGER,
                    extra_time TEXT,
                    is_finished INTEGER DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS predictions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    match_id INTEGER NOT NULL,
                    home_goals INTEGER NOT NULL,
                    away_goals INTEGER NOT NULL,
                    extra_time TEXT,
                    score INTEGER DEFAULT 0,
                    is_scored INTEGER DEFAULT 0,
                    UNIQUE(user_id, match_id),
                    FOREIGN KEY(user_id) REFERENCES users(user_id),
                    FOREIGN KEY(match_id) REFERENCES matches(match_id)
                );

                CREATE TABLE IF NOT EXISTS semifinal_picks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    slot INTEGER NOT NULL,
                    team TEXT NOT NULL,
                    UNIQUE(user_id, slot)
                );

                CREATE TABLE IF NOT EXISTS champion_picks (
                    user_id INTEGER PRIMARY KEY,
                    team TEXT NOT NULL,
                    score INTEGER DEFAULT 0,
                    is_scored INTEGER DEFAULT 0
                );
            """)

    # ─── کاربران ────────────────────────────────────────────
    def add_user(self, user_id: int, name: str, username: str = None):
        with self._conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO users (user_id, name, username) VALUES (?, ?, ?)",
                (user_id, name, username)
            )

    # ─── بازی‌ها ─────────────────────────────────────────────
    def save_match(self, match_id: int, home: str, away: str, match_time: str, stage: str):
        with self._conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO matches (match_id, home, away, match_time, stage) VALUES (?, ?, ?, ?, ?)",
                (match_id, home, away, match_time, stage)
            )

    def get_match(self, match_id: int) -> dict:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM matches WHERE match_id = ?", (match_id,)).fetchone()
            return dict(row) if row else None

    def get_today_matches(self) -> list:
        today = datetime.now(IRAN_TZ).strftime("%Y-%m-%d")
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM matches WHERE match_time LIKE ? ORDER BY match_time",
                (f"{today}%",)
            ).fetchall()
            return [dict(r) for r in rows]

    def set_result(self, match_id: int, home: int, away: int, extra_time: str = None):
        with self._conn() as conn:
            conn.execute(
                "UPDATE matches SET result_home=?, result_away=?, extra_time=?, is_finished=1 WHERE match_id=?",
                (home, away, extra_time, match_id)
            )

    # ─── پیش‌بینی‌ها ──────────────────────────────────────────
    def save_prediction(self, user_id: int, match_id: int, home: int, away: int, extra_time: str = None):
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO predictions (user_id, match_id, home_goals, away_goals, extra_time)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(user_id, match_id) DO UPDATE SET
                   home_goals=excluded.home_goals, away_goals=excluded.away_goals, extra_time=excluded.extra_time""",
                (user_id, match_id, home, away, extra_time)
            )

    def get_prediction(self, user_id: int, match_id: int) -> dict:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM predictions WHERE user_id=? AND match_id=?",
                (user_id, match_id)
            ).fetchone()
            return dict(row) if row else None

    def get_match_predictions(self, match_id: int) -> list:
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT p.*, u.name FROM predictions p
                   JOIN users u ON p.user_id = u.user_id
                   WHERE p.match_id = ? AND p.is_scored = 0""",
                (match_id,)
            ).fetchall()
            return [dict(r) for r in rows]

    def update_prediction_score(self, pred_id: int, score: int):
        with self._conn() as conn:
            conn.execute(
                "UPDATE predictions SET score=?, is_scored=1 WHERE id=?",
                (score, pred_id)
            )
            # آپدیت امتیاز کل کاربر
            conn.execute(
                """UPDATE users SET total_score = (
                    SELECT COALESCE(SUM(score), 0) FROM predictions WHERE user_id = (
                        SELECT user_id FROM predictions WHERE id = ?
                    )
                ) WHERE user_id = (SELECT user_id FROM predictions WHERE id = ?)""",
                (pred_id, pred_id)
            )

    # ─── جدول امتیازات ─────────────────────────────────────────
    def get_leaderboard(self) -> list:
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT u.name, 
                   COALESCE(SUM(p.score), 0) + COALESCE(cp.score, 0) as score
                   FROM users u
                   LEFT JOIN predictions p ON u.user_id = p.user_id
                   LEFT JOIN champion_picks cp ON u.user_id = cp.user_id
                   GROUP BY u.user_id
                   ORDER BY score DESC"""
            ).fetchall()
            return [dict(r) for r in rows]

    # ─── آمار کاربر ─────────────────────────────────────────────
    def get_user_stats(self, user_id: int) -> dict:
        with self._conn() as conn:
            preds = conn.execute(
                """SELECT p.*, m.result_home, m.result_away FROM predictions p
                   JOIN matches m ON p.match_id = m.match_id
                   WHERE p.user_id = ? AND m.is_finished = 1""",
                (user_id,)
            ).fetchall()

            total = exact = diff = winner = wrong = 0
            for p in preds:
                total += p["score"]
                if p["score"] > 0:
                    ph, pa = p["home_goals"], p["away_goals"]
                    rh, ra = p["result_home"], p["result_away"]
                    if ph == rh and pa == ra:
                        exact += 1
                    elif (ph - pa) == (rh - ra):
                        diff += 1
                    else:
                        winner += 1
                else:
                    wrong += 1

            return {"total": total, "exact": exact, "diff": diff, "winner": winner, "wrong": wrong}

    # ─── نیمه‌نهایی ─────────────────────────────────────────────
    def save_semifinal_pick(self, user_id: int, slot: int, team: str):
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO semifinal_picks (user_id, slot, team) VALUES (?, ?, ?)",
                (user_id, slot, team)
            )

    def get_semifinal_picks(self, user_id: int) -> list:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM semifinal_picks WHERE user_id = ? ORDER BY slot",
                (user_id,)
            ).fetchall()
            return [dict(r) for r in rows]

    def score_semifinal_picks(self, actual_teams: list):
        """محاسبه امتیاز نیمه‌نهایی بعد از مشخص شدن تیم‌ها"""
        with self._conn() as conn:
            users = conn.execute("SELECT user_id FROM users").fetchall()
            for user in users:
                uid = user["user_id"]
                picks = conn.execute(
                    "SELECT team FROM semifinal_picks WHERE user_id = ?", (uid,)
                ).fetchall()
                picked_teams = [p["team"] for p in picks]
                correct = sum(1 for t in picked_teams if t in actual_teams)
                bonus = correct * 150
                conn.execute(
                    "UPDATE users SET total_score = total_score + ? WHERE user_id = ?",
                    (bonus, uid)
                )

    # ─── قهرمان ─────────────────────────────────────────────────
    def save_champion_pick(self, user_id: int, team: str):
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO champion_picks (user_id, team) VALUES (?, ?)",
                (user_id, team)
            )

    def get_champion_pick(self, user_id: int) -> dict:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM champion_picks WHERE user_id = ?", (user_id,)
            ).fetchone()
            return dict(row) if row else None

    def score_champion(self, winning_team: str):
        """محاسبه امتیاز قهرمان"""
        with self._conn() as conn:
            conn.execute(
                """UPDATE champion_picks SET score = 500, is_scored = 1
                   WHERE team = ? AND is_scored = 0""",
                (winning_team,)
            )
            conn.execute(
                """UPDATE users SET total_score = total_score + 500
                   WHERE user_id IN (
                       SELECT user_id FROM champion_picks WHERE team = ? AND is_scored = 1
                   )""",
                (winning_team,)
            )
