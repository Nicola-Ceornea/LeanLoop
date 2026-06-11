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
    proof_text  TEXT
);
"""


class RunDB:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def log(self, a: ProofAttempt) -> None:
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
                "INSERT OR REPLACE INTO solved (goal_name, ts, tier, proof_text)"
                " VALUES (?,?,?,?)",
                (a.goal_name, time.time(), a.tier, a.proof_text),
            )
        self.conn.commit()

    def is_solved(self, goal_name: str) -> bool:
        cur = self.conn.execute("SELECT 1 FROM solved WHERE goal_name=?", (goal_name,))
        return cur.fetchone() is not None

    def stats(self) -> dict:
        cur = self.conn.execute(
            "SELECT tier, COUNT(*), SUM(accepted) FROM attempts GROUP BY tier")
        by_tier = {t: {"attempts": n, "accepted": s or 0} for t, n, s in cur.fetchall()}
        solved = self.conn.execute("SELECT COUNT(*) FROM solved").fetchone()[0]
        return {"solved_goals": solved, "by_tier": by_tier}

    def close(self) -> None:
        self.conn.close()
