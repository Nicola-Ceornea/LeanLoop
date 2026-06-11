"""Run-state / liveness DB + status formatting (the monitoring + recovery layer)."""
import time

from leanloop.cli import _ago
from leanloop.db import RunDB
from leanloop.provers.base import ProofAttempt


def _att(goal, tier, accepted=False, build_ok=True):
    a = ProofAttempt(goal_name=goal, tier=tier, proof_text="x")
    a.accepted, a.build_ok = accepted, build_ok
    return a


def test_run_state_lifecycle(tmp_path):
    db = RunDB(str(tmp_path / "r.sqlite"))
    assert db.run_state() is None
    db.run_begin(total=5, host="gpu-box", pid=1234)
    st = db.run_state()
    assert st["total"] == 5 and st["host"] == "gpu-box" and st["finished"] == 0
    db.run_progress("Mod.A", done=2, queued=1)
    st = db.run_state()
    assert st["done"] == 2 and st["queued"] == 1 and st["current_goal"] == "Mod.A"
    db.run_end(done=4, queued=1)
    st = db.run_state()
    assert st["finished"] == 1 and st["done"] == 4 and st["finished_ts"] is not None
    db.close()


def test_run_begin_resets_prior_run(tmp_path):
    db = RunDB(str(tmp_path / "r.sqlite"))
    db.run_begin(3, "h", 1); db.run_end(done=3)
    db.run_begin(7, "h2", 2)        # a fresh run must reset finished + counts
    st = db.run_state()
    assert st["finished"] == 0 and st["total"] == 7 and st["done"] == 0
    db.close()


def test_last_activity_and_recent(tmp_path):
    db = RunDB(str(tmp_path / "r.sqlite"))
    assert db.last_activity_ts() is None
    db.log(_att("A", "tactic", accepted=False))
    db.log(_att("A", "local", accepted=True))
    assert db.last_activity_ts() is not None
    recent = db.recent_attempts(10)
    assert len(recent) == 2 and recent[0]["goal_name"] == "A"  # most-recent first
    assert recent[0]["accepted"] == 1
    db.close()


def test_solved_skip_enables_resume(tmp_path):
    db = RunDB(str(tmp_path / "r.sqlite"))
    db.log(_att("Mod.Done", "tactic", accepted=True))
    assert db.is_solved("Mod.Done")            # a re-run skips this goal
    assert "Mod.Done" in db.solved_names()
    db.close()


def test_wal_reader_does_not_block(tmp_path):
    # two connections (writer = run, reader = status) must coexist under WAL
    p = str(tmp_path / "r.sqlite")
    writer = RunDB(p)
    writer.run_begin(1, "h", 1)
    reader = RunDB(p)
    assert reader.run_state()["host"] == "h"
    writer.run_progress("G", 0, 0)
    assert reader.run_state()["current_goal"] == "G"
    writer.close(); reader.close()


def test_ago_formatting():
    now = time.time()
    assert _ago(None) == "never"
    assert _ago(now).endswith("s ago")
    assert "m" in _ago(now - 125)
    assert "h" in _ago(now - 7400)


def test_solved_cache_invalidated_on_goal_change(tmp_path):
    """Footgun found 2026-06-11: solved was keyed by name only — a module that
    gained a NEW sorry after being solved was skipped as 'cached'. The cache
    must miss when the goal content hash changes."""
    db = RunDB(str(tmp_path / "r.sqlite"))
    a = _att("Mod.A", "tactic", accepted=True)
    db.log(a, goal_hash="hash-v1")
    assert db.is_solved("Mod.A", "hash-v1")          # same content -> hit
    assert not db.is_solved("Mod.A", "hash-v2")      # changed content -> MISS
    assert not db.is_solved("Mod.B", "hash-v1")
    db.close()


def test_solved_cache_legacy_rows_still_hit_without_hash(tmp_path):
    # rows from pre-hash DBs (empty goal_hash) keep matching empty-hash queries
    db = RunDB(str(tmp_path / "r.sqlite"))
    db.log(_att("Mod.Old", "tactic", accepted=True), goal_hash="")
    assert db.is_solved("Mod.Old", "")
    assert not db.is_solved("Mod.Old", "newhash")    # but never a hashed query
    db.close()
