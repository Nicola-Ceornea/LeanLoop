"""Interactive-frontier queue + the two bugs found while building it:
the argparse `-c`-before-subcommand clobber, and tactic_battery [] vs None.
"""
from leanloop import frontier_queue, tactic_battery
from leanloop.cli import _config_path
from leanloop.provers.base import Goal

GOAL_SRC = "namespace S\ntheorem t (a : Nat) : a + 0 = a := by sorry\nend S\n"


# --- tactic battery: None = default cascade, [] = explicitly disabled -------- #
def test_battery_none_uses_defaults():
    assert len(tactic_battery.candidates(GOAL_SRC, None)) > 0


def test_battery_empty_list_disables():
    assert tactic_battery.candidates(GOAL_SRC, []) == []


# --- frontier queue round-trip ---------------------------------------------- #
def test_enqueue_then_list_get(tmp_path):
    qd = str(tmp_path / "q")
    goal = Goal(name="S.Good", file_text=GOAL_SRC, target_module="S.Good")
    task = frontier_queue.enqueue(
        goal, module_stem="S.Good", project_root=str(tmp_path),
        queue_dir=qd, config_path="leanloop.toml",
        allowed_axioms=["propext", "Classical.choice", "Quot.sound"],
        whitelist=["keccak256_pure"], feedback="omega failed: ...")
    # files exist
    assert task.md_path.exists() and task.json_path.exists()
    md = task.md_path.read_text()
    # the task pins the exact signature + criteria + whitelisted-axiom rule
    assert "theorem t (a : Nat) : a + 0 = a" in md
    assert "keccak256_pure" in md and "No `sorry`" in md
    assert "omega failed" in md            # local context carried in
    # listing + lookup
    pending = frontier_queue.list_tasks(qd, pending_only=True)
    assert len(pending) == 1 and pending[0].goal_name == "S.Good"
    assert frontier_queue.get_task(qd, "S.Good").module_stem == "S.Good"


def test_mark_solved_removes_from_pending(tmp_path):
    qd = str(tmp_path / "q")
    goal = Goal(name="S.Good", file_text=GOAL_SRC, target_module="S.Good")
    task = frontier_queue.enqueue(
        goal, module_stem="S.Good", project_root=str(tmp_path), queue_dir=qd,
        config_path="", allowed_axioms=["propext"], whitelist=[])
    frontier_queue.mark_solved(task)
    assert frontier_queue.list_tasks(qd, pending_only=True) == []
    assert len(frontier_queue.list_tasks(qd)) == 1   # still present, marked solved


def test_task_goal_reads_project_file(tmp_path):
    qd = str(tmp_path / "q")
    (tmp_path / "S").mkdir()
    (tmp_path / "S" / "Good.lean").write_text(GOAL_SRC)
    goal = Goal(name="S.Good", file_text=GOAL_SRC, target_module="S.Good")
    task = frontier_queue.enqueue(
        goal, module_stem="S.Good", project_root=str(tmp_path), queue_dir=qd,
        config_path="", allowed_axioms=["propext"], whitelist=[])
    g = frontier_queue.task_goal(task)
    assert g.name == "S.Good" and "theorem t" in g.file_text


# --- argparse: -c must survive BEFORE the subcommand (the clobber bug) ------- #
def test_config_flag_before_subcommand(monkeypatch):
    import argparse
    # rebuild the same parser shape as cli.main and confirm -c before the
    # subcommand is not clobbered by the subparser's default.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-c", "--config", default=argparse.SUPPRESS)
    p = argparse.ArgumentParser(parents=[common])
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("frontier", parents=[common])
    args_before = p.parse_args(["-c", "my.toml", "frontier"])
    args_after = p.parse_args(["frontier", "-c", "my.toml"])
    assert _config_path(args_before) == "my.toml"
    assert _config_path(args_after) == "my.toml"
