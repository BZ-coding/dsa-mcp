"""Regression tests for alert-daemon transient-signal debounce."""
import importlib.util
from pathlib import Path

import pytest


def _load_daemon():
    daemon_path = Path("/home/zsd/.hermes/scripts/alert_daemon.py")
    if not daemon_path.exists():
        pytest.skip("alert_daemon not deployed")
    spec = importlib.util.spec_from_file_location("alert_daemon_debounce", daemon_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _prev_state():
    return {
        "price_break_20d_high": {
            "price:price_break_20d_high": {
                "value": "price:price_break_20d_high",
                "severity": "high",
                "pushed_at": "2026-07-13T09:22:06",
            }
        }
    }


def test_one_transient_missing_poll_keeps_signal_state_without_repush():
    mod = _load_daemon()
    to_push, held = mod._reconcile_signal_state(_prev_state(), [], missing_threshold=3)
    assert to_push == []
    meta = held["price_break_20d_high"]["price:price_break_20d_high"]
    assert meta["missing_count"] == 1

    signal = {
        "rule_id": "price_break_20d_high",
        "severity": "high",
        "reason": "收盘价 114.0 突破 20 日最高 113.0",
    }
    to_push, restored = mod._reconcile_signal_state(held, [signal], missing_threshold=3)
    assert to_push == [], "短暂缺失后恢复不应当作新事件重推"
    meta = restored["price_break_20d_high"]["price:price_break_20d_high"]
    assert "missing_count" not in meta
    assert meta["pushed_at"] == "2026-07-13T09:22:06"


def test_three_consecutive_missing_polls_expire_signal_state():
    mod = _load_daemon()
    state = _prev_state()
    for expected in (1, 2):
        _, state = mod._reconcile_signal_state(state, [], missing_threshold=3)
        meta = state["price_break_20d_high"]["price:price_break_20d_high"]
        assert meta["missing_count"] == expected
    _, state = mod._reconcile_signal_state(state, [], missing_threshold=3)
    assert state == {}
