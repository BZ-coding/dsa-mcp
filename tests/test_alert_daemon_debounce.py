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


def test_cross_day_persistent_signal_not_repushed():
    """Phase J (2026-08-18): 跨日不重推 — 昨天推过的持续信号, 今天 00:00 后
    不得全量重推. 原 bug: 8-18 00:03:31 推送 15 条 = 15/15 与 8-17 00:04:01
    完全相同; 同一公告 (ann:229874) 8-13 发布被推到 8-18 共 4 次, 美团回购
    17810 自 7-07 共推 27 次.
    """
    mod = _load_daemon()
    # 跨日 state: pushed_at 是昨天 (必然过 cooldown), missing_count=0 (持续状态)
    state = {
        "price_break_20d_high": {
            "price:price_break_20d_high": {
                "value": "price:price_break_20d_high",
                "severity": "high",
                "pushed_at": "2026-08-17T00:04:01",
            }
        }
    }
    signal = {
        "rule_id": "price_break_20d_high",
        "severity": "high",
        "reason": "收盘价 114.0 突破 20 日最高 113.0",
    }
    to_push, restored = mod._reconcile_signal_state(state, [signal], missing_threshold=3)
    assert to_push == [], "跨日后昨天已推过的持续信号不得重推 (Phase J)"
    meta = restored["price_break_20d_high"]["price:price_break_20d_high"]
    assert meta["pushed_at"] == "2026-08-17T00:04:01"


def test_cross_day_severity_escalation_still_pushes():
    """Phase J: 跨日 + severity 升级仍要推 (风险加剧必须通知)."""
    mod = _load_daemon()
    state = {
        "price_break_20d_high": {
            "price:price_break_20d_high": {
                "value": "price:price_break_20d_high",
                "severity": "medium",
                "pushed_at": "2026-08-17T00:04:01",
            }
        }
    }
    signal = {
        "rule_id": "price_break_20d_high",
        "severity": "high",
        "reason": "收盘价 114.0 突破 20 日最高 113.0",
    }
    to_push, restored = mod._reconcile_signal_state(state, [signal], missing_threshold=3)
    assert len(to_push) == 1, "severity 升级必须重推"
    meta = restored["price_break_20d_high"]["price:price_break_20d_high"]
    assert meta["severity"] == "high"


def test_failed_push_retried_next_poll():
    """Phase J: pushed_at 为空 (上次推送失败) → 下轮重试, 不能静默丢弃."""
    mod = _load_daemon()
    state = {
        "price_break_20d_high": {
            "price:price_break_20d_high": {
                "value": "price:price_break_20d_high",
                "severity": "high",
                "pushed_at": "",
            }
        }
    }
    signal = {
        "rule_id": "price_break_20d_high",
        "severity": "high",
        "reason": "收盘价 114.0 突破 20 日最高 113.0",
    }
    to_push, _ = mod._reconcile_signal_state(state, [signal], missing_threshold=3)
    assert len(to_push) == 1, "从未成功推送的信号必须重试"


def test_announcement_cross_day_not_repushed():
    """Phase J: 公告类 (离散事件) 跨日更不能重推 — 同一公告只推一次."""
    mod = _load_daemon()
    state = {
        "major_event": {
            "ann:major_event:229874": {
                "value": "ann:major_event:229874",
                "severity": "medium",
                "pushed_at": "2026-08-17T00:04:01",
            }
        }
    }
    signal = {
        "rule_id": "major_event",
        "severity": "medium",
        "announcement_id": "229874",
        "reason": '公告标题含 "担保": 关于全资子公司金风国际为其全资子公司金风巴西提供担保的公告',
    }
    to_push, _ = mod._reconcile_signal_state(state, [signal], missing_threshold=3)
    assert to_push == []
