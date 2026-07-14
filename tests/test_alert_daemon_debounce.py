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


def test_transient_missing_after_cooldown_still_dedups():
    """§H.3: §I 短暂缺失恢复场景, 即便跨日 + 跨 4h cooldown 也应静默.

    §H (commit 15993db) 原修复只覆盖"今日已推过 + cooldown 内" 两种场景.
    但 §I (Phase I) hold 信号 (missing_count > 0) 跨日恢复时, prev_ts 是昨天,
    4h cooldown 早已过, §H daily dedup 不生效 → 重推 → 实际线上表现为
    "跨日早盘短抖恢复 spam".

    测试目的: 锁定 §H.3 修复, missing_count > 0 时也走 daily dedup 静默,
    除非 severity 升级.
    """
    mod = _load_daemon()
    # 1) 跨日 state (pushed_at = 昨天, 必然过 4h cooldown)
    state = {
        "price_break_20d_high": {
            "price:price_break_20d_high": {
                "value": "price:price_break_20d_high",
                "severity": "high",
                "pushed_at": "2026-07-13T09:22:06",
            }
        }
    }
    # 2) 短暂缺失 1 轮 → §I hold 加 missing_count=1
    _, held = mod._reconcile_signal_state(state, [], missing_threshold=3)
    assert held["price_break_20d_high"]["price:price_break_20d_high"]["missing_count"] == 1
    # 3) 信号恢复 → §H.3 应静默 (不能因为跨日 + 跨 cooldown 就重推)
    signal = {
        "rule_id": "price_break_20d_high",
        "severity": "high",
        "reason": "收盘价 114.0 突破 20 日最高 113.0",
    }
    to_push, restored = mod._reconcile_signal_state(held, [signal], missing_threshold=3)
    assert to_push == [], "跨日 + 跨 cooldown + §I 短抖恢复仍应静默 (Phase H.3)"
    meta = restored["price_break_20d_high"]["price:price_break_20d_high"]
    assert "missing_count" not in meta  # 信号已恢复, missing_count 移除
    assert meta["pushed_at"] == "2026-07-13T09:22:06"  # pushed_at 保持, 不被新推覆盖
