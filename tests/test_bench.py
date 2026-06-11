"""Bench metric aggregation."""
from leanloop.bench import summarize_closerate, summarize_throughput


def test_throughput_summary():
    samples = [
        {"gen_toks": 500, "gen_tps": 20.0, "prompt_tps": 1000.0, "total_s": 25.0},
        {"gen_toks": 600, "gen_tps": 30.0, "prompt_tps": 1200.0, "total_s": 20.0},
        {"gen_toks": 400, "gen_tps": 10.0, "prompt_tps": 800.0, "total_s": 40.0},
    ]
    s = summarize_throughput(samples)
    assert s["n"] == 3 and s["ok"] == 3
    assert s["gen_tps_median"] == 20.0
    assert s["gen_tps_min"] == 10.0 and s["gen_tps_max"] == 30.0
    assert s["gen_toks_median"] == 500


def test_throughput_ignores_failed_samples():
    samples = [
        {"gen_toks": 0, "gen_tps": 0.0, "prompt_tps": 0.0, "total_s": 1.0},  # failed
        {"gen_toks": 500, "gen_tps": 20.0, "prompt_tps": 1000.0, "total_s": 25.0},
        None,
    ]
    s = summarize_throughput(samples)
    assert s["n"] == 3 and s["ok"] == 1 and s["gen_tps_median"] == 20.0


def test_throughput_all_failed_is_safe():
    s = summarize_throughput([{"gen_toks": 0, "gen_tps": 0.0}])
    assert s["ok"] == 0 and s["gen_tps_median"] == 0.0


def test_closerate_summary():
    rows = [
        {"tier": "tactic", "accepted": True, "wall_s": 2.0},
        {"tier": "local", "accepted": True, "wall_s": 60.0},
        {"tier": "local", "accepted": False, "wall_s": 120.0},
        {"tier": "", "accepted": False, "wall_s": 90.0},
    ]
    cr = summarize_closerate(rows)
    assert cr["goals"] == 4 and cr["closed"] == 2 and cr["close_rate"] == 50.0
    assert cr["by_tactic"] == 1 and cr["by_local_prover"] == 1 and cr["open"] == 2
    assert cr["wall_s_median"] == 75.0     # median of [2,60,120,90] = (60+90)/2
    assert cr["wall_s_total"] == 272.0


def test_closerate_empty():
    cr = summarize_closerate([])
    assert cr["goals"] == 0 and cr["close_rate"] == 0.0
