"""Run log (sqlite). This is the expert-iteration corpus, the triage trail, and
the debugging history all at once — capture every attempt, accepted or not.
Per the founding doc, this DB is what later QLoRA fine-tuning trains on.
"""
from __future__ import annotations

import json
import sqlite3
import time

from .provers.base import ProofAttempt

_SCHEMA = """
CREATE TABLE IF NOT EXISTS attempts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL,
    goal_name   TEXT,
    tier        TEXT,
    model       TEXT,
    accepted    INTEGER,
    build_ok    INTEGER,
    audit_ok    INTEGER,
    wall_clock_s REAL,
    sampling    TEXT,
    lean_errors TEXT,
    axioms      TEXT,
    proof_text  TEXT
);
CREATE TABLE IF NOT EXISTS solved (
    goal_name   TEXT PRIMARY KEY,
    ts          REAL,
    tier        TEXT,
    proof_text  TEXT,
    goal_hash   TEXT DEFAULT ''
);
-- single-row liveness/heartbeat for the current (or last) `leanloop run`, so a
-- separate `leanloop status` (e.g. over SSH) can tell if a run is alive,
-- stalled, or finished, and resume from where it left off.
CREATE TABLE IF NOT EXISTS run_state (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    started_ts   REAL,
    last_beat_ts REAL,
    host         TEXT,
    pid          INTEGER,
    total        INTEGER,
    done         INTEGER,
    queued       INTEGER,
    current_goal TEXT,
    finished     INTEGER,
    finished_ts  REAL
);
"""


class RunDB:
    def __init__(self, path: str):
        # WAL so a reader (`status`/`watch`) never blocks the writer (the run).
        self.conn = sqlite3.connect(path, timeout=30)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.executescript(_SCHEMA)
        # migration: DBs created before goal_hash existed lack the column
        try:
            self.conn.execute("ALTER TABLE solved ADD COLUMN goal_hash TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass  # column already exists
        self.conn.commit()

    def log(self, a: ProofAttempt, *, goal_hash: str = "") -> None:
        self.conn.execute(
            "INSERT INTO attempts (ts, goal_name, tier, model, accepted, build_ok,"
            " audit_ok, wall_clock_s, sampling, lean_errors, axioms, proof_text)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (time.time(), a.goal_name, a.tier, a.model, int(a.accepted),
             int(a.build_ok), int(a.audit_ok), a.wall_clock_s,
             json.dumps(a.sampling), a.lean_errors[:8000], a.axioms[:4000],
             a.proof_text[:20000]),
        )
        if a.accepted:
            self.conn.execute(
                "INSERT OR REPLACE INTO solved (goal_name, ts, tier, proof_text, goal_hash)"
                " VALUES (?,?,?,?,?)",
                (a.goal_name, time.time(), a.tier, a.proof_text, goal_hash),
            )
        self.conn.commit()

    def is_solved(self, goal_name: str, goal_hash: str = "") -> bool:
        """Solved only counts for the SAME goal content. If the module later
        gains a new `sorry` (different hash), the cache must miss — otherwise a
        re-run would skip it and falsely report the goal closed."""
        cur = self.conn.execute(
            "SELECT goal_hash FROM solved WHERE goal_name=?", (goal_name,))
        row = cur.fetchone()
        if row is None:
            return False
        stored = row[0] or ""
        return stored == goal_hash if (stored or goal_hash) else True

    def stats(self) -> dict:
        cur = self.conn.execute(
            "SELECT tier, COUNT(*), SUM(accepted) FROM attempts GROUP BY tier")
        by_tier = {t: {"attempts": n, "accepted": s or 0} for t, n, s in cur.fetchall()}
        solved = self.conn.execute("SELECT COUNT(*) FROM solved").fetchone()[0]
        return {"solved_goals": solved, "by_tier": by_tier}

    # ---- liveness / heartbeat ---------------------------------------- #
    def run_begin(self, total: int, host: str, pid: int) -> None:
        now = time.time()
        self.conn.execute(
            "INSERT INTO run_state (id, started_ts, last_beat_ts, host, pid, total,"
            " done, queued, current_goal, finished, finished_ts)"
            " VALUES (1,?,?,?,?,?,0,0,'',0,NULL)"
            " ON CONFLICT(id) DO UPDATE SET started_ts=?, last_beat_ts=?, host=?,"
            " pid=?, total=?, done=0, queued=0, current_goal='', finished=0, finished_ts=NULL",
            (now, now, host, pid, total, now, now, host, pid, total))
        self.conn.commit()

    def run_progress(self, current_goal: str, done: int, queued: int) -> None:
        self.conn.execute(
            "UPDATE run_state SET last_beat_ts=?, current_goal=?, done=?, queued=?"
            " WHERE id=1", (time.time(), current_goal, done, queued))
        self.conn.commit()

    def run_end(self, done: int | None = None, queued: int | None = None) -> None:
        now = time.time()
        if done is None:
            self.conn.execute(
                "UPDATE run_state SET finished=1, finished_ts=?, last_beat_ts=? WHERE id=1",
                (now, now))
        else:
            self.conn.execute(
                "UPDATE run_state SET finished=1, finished_ts=?, last_beat_ts=?,"
                " done=?, queued=?, current_goal='' WHERE id=1",
                (now, now, done, queued if queued is not None else 0))
        self.conn.commit()

    def run_state(self) -> dict | None:
        cur = self.conn.execute(
            "SELECT started_ts, last_beat_ts, host, pid, total, done, queued,"
            " current_goal, finished, finished_ts FROM run_state WHERE id=1")
        row = cur.fetchone()
        if not row:
            return None
        keys = ["started_ts", "last_beat_ts", "host", "pid", "total", "done",
                "queued", "current_goal", "finished", "finished_ts"]
        return dict(zip(keys, row))

    def last_activity_ts(self) -> float | None:
        """Most recent attempt timestamp — the true liveness signal (Tier 0 logs
        per tactic, Tier 1 per sample, so this advances every few seconds)."""
        row = self.conn.execute("SELECT MAX(ts) FROM attempts").fetchone()
        return row[0] if row and row[0] is not None else None

    def recent_attempts(self, limit: int = 10) -> list[dict]:
        cur = self.conn.execute(
            "SELECT ts, goal_name, tier, accepted, build_ok, wall_clock_s, lean_errors"
            " FROM attempts ORDER BY id DESC LIMIT ?", (limit,))
        keys = ["ts", "goal_name", "tier", "accepted", "build_ok", "wall_clock_s", "lean_errors"]
        return [dict(zip(keys, r)) for r in cur.fetchall()]

    def solved_names(self) -> set[str]:
        return {r[0] for r in self.conn.execute("SELECT goal_name FROM solved")}

    def close(self) -> None:
        self.conn.close()
