"""Regression tests for daily_recap reading append-only alert history."""
import importlib.util
import json
from pathlib import Path


def _load_recap():
    path = Path("/home/zsd/.hermes/scripts/daily_recap.py")
    spec = importlib.util.spec_from_file_location("daily_recap_history", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_load_history_signals_preserves_signals_that_later_disappeared(tmp_path):
    mod = _load_recap()
    history = tmp_path / "alert_history.jsonl"
    rows = [
        {
            "ts": "2026-07-13T09:22:06",
            "groups": [{
                "sym": "hk09988",
                "signals": [{
                    "rule_id": "price_break_20d_high",
                    "severity": "high",
                    "sig_key": "price:price_break_20d_high",
                    "reason": "突破20日新高",
                }],
            }],
        },
        {
            "ts": "2026-07-12T09:22:06",
            "groups": [{
                "sym": "002202",
                "signals": [{"rule_id": "old", "severity": "low", "sig_key": "price:old"}],
            }],
        },
    ]
    history.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")

    signals = mod.load_history_signals(history, "2026-07-13")
    assert len(signals) == 1
    assert signals[0]["sym"] == "hk09988"
    assert signals[0]["rule_id"] == "price_break_20d_high"
    assert signals[0]["reason"] == "突破20日新高"
