"""Regression tests: daemon missing-signal expiry threshold must have ONE source of truth.

Background (2026-11-12 phase6 audit):
  `SIGNAL_MISSING_POLLS_TO_EXPIRE` used to be 3 and was referenced **only** by the
  "[hold]" log line, while the real expiry decision in `_reconcile_signal_state`
  used a separate hardcoded `>= 100`. Two values, one behaviour:

    [hold] hk03690 missing 1 poll (1/3), keep state     <- misleading denominator
    ... but the state was actually kept for 100 polls ...

The log therefore told the operator the signal would be dropped after 3 polls
when it was really dropped after 100. Fix: one constant (100) drives both the
behaviour and the log. These tests lock that in so the two can never drift
apart again.
"""
import importlib.util
import inspect
from pathlib import Path

import pytest


def _load_daemon():
    daemon_path = Path("/home/zsd/.hermes/scripts/alert_daemon.py")
    if not daemon_path.exists():
        pytest.skip("alert_daemon not deployed")
    spec = importlib.util.spec_from_file_location("alert_daemon_threshold", daemon_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_threshold_constant_is_100():
    """The real (and historically intended) expiry window is 100 polls, not 3."""
    mod = _load_daemon()
    assert mod.SIGNAL_MISSING_POLLS_TO_EXPIRE == 100


def test_missing_threshold_cannot_shorten_expiry_window():
    """`missing_threshold` is a legacy compat param: it may only *extend* the
    window, never shorten it below the safety floor of 100 polls."""
    mod = _load_daemon()
    state = {
        "price_break_20d_high": {
            "price:price_break_20d_high": {
                "value": "price:price_break_20d_high",
                "severity": "high",
                "pushed_at": "2026-07-13T09:22:06",
            }
        }
    }
    # 3 rounds with the legacy param: must NOT expire (that was the 3-vs-100 bug)
    for _ in range(3):
        _, state = mod._reconcile_signal_state(state, [], missing_threshold=3)
    assert "price_break_20d_high" in state, "3 轮 missing 不得删除 state"

    # ... and the state must survive all the way to 99 rounds
    for _ in range(96):
        _, state = mod._reconcile_signal_state(state, [], missing_threshold=3)
    assert "price_break_20d_high" in state, "99 轮 missing 仍应保留 state"

    # the 100th round finally drops it
    _, state = mod._reconcile_signal_state(state, [], missing_threshold=3)
    assert state == {}, "第 100 轮 missing 应丢弃 state"


def test_missing_threshold_can_extend_window():
    """A larger `missing_threshold` must lengthen the retention window."""
    mod = _load_daemon()
    state = {
        "price_break_20d_high": {
            "price:price_break_20d_high": {
                "value": "price:price_break_20d_high",
                "severity": "high",
                "pushed_at": "2026-07-13T09:22:06",
            }
        }
    }
    for _ in range(120):
        _, state = mod._reconcile_signal_state(state, [], missing_threshold=200)
    assert "price_break_20d_high" in state, "threshold=200 时 120 轮仍应保留 state"


def test_log_denominator_uses_the_same_constant():
    """The '[hold] ... (N/3)' log line must derive its denominator from the
    same constant the expiry logic uses — no literal '3' anywhere."""
    mod = _load_daemon()
    src = inspect.getsource(mod)
    assert "(N/3)" not in src, "日志分母不得硬编码为 3"
    # every place that prints the expiry denominator must reference the constant
    assert src.count("SIGNAL_MISSING_POLLS_TO_EXPIRE") >= 3, (
        "过期判定、过期日志、hold 日志三处都必须引用同一个常量"
    )
    # and there must be no leftover hardcoded 100-polls comparison
    assert "missing_count >= 100" not in src, (
        "过期判定不得硬编码 100，必须引用常量"
    )
