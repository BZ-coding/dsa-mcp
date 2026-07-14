"""Regression tests for daily_recap reading append-only alert history."""
import importlib.util
import json
import os
from pathlib import Path


def _load_recap():
    # 优先加载仓源 (CI runner 上没部署路径; 本地/生产都能用)
    repo_path = Path(__file__).resolve().parent.parent / "scripts" / "daily_recap.py"
    # fallback 部署路径 (兼容老 alert_daemon.py 同 pattern 的 ~/.hermes/scripts/)
    deploy_path = Path(os.path.expanduser("~/.hermes/scripts/daily_recap.py"))
    for p in (repo_path, deploy_path):
        if p.exists():
            spec = importlib.util.spec_from_file_location("daily_recap_history", p)
            assert spec is not None and spec.loader is not None
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    import pytest
    pytest.skip(f"daily_recap.py not found (tried {repo_path} and {deploy_path})")


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
