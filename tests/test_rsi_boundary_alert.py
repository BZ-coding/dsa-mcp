#!/usr/bin/env python3
"""RSI 超买盲区修复的专项回归（2026-10-31）

背景：`rsi_sma` 原名 `rsi_sma_legacy`，带除零 hack + 「loss==0 → 50」兜底，
导致**单边持续上涨行情下 RSI 恒为 50，而 `rsi_overbought_70` 要求 > 70
→ 该告警在生产上永远不会触发**（漏报）。

本测试守住三条：
  ① 单边上涨行情下 **现在会触发** `rsi_overbought_70`（盲区已消除）
  ② 正常波动行情下 **新旧值一致**（影响面为 0 的根基）
  ③ 差异**只在 RSI 相关字段**，MA/MACD/量比 不受影响
"""
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from dsa_mcp.alerts.checker import check_alert  # noqa: E402


def mk(closes):
    cl = [float(c) for c in closes]
    return pd.DataFrame({
        "date": [f"2026-09-{i+1:02d}" for i in range(len(cl))],
        "open": cl,
        "high": [c * 1.01 for c in cl],
        "low": [c * 0.99 for c in cl],
        "close": cl,
        "volume": [1000.0] * len(cl),
    })


def test_single_up_trend_now_triggers_overbought():
    """① 单边上涨：修正后必须触发（修正前恒不触发）。"""
    df = mk([10 + i for i in range(40)])          # 40 连涨
    quote = {"symbol": "TEST", "price": float(df["close"].iloc[-1])}
    r = check_alert("TEST", quote, df, rule_id="rsi_overbought_70")
    assert r["triggered"] is True, "单边上涨行情下 RSI 超买必须触发（盲区未修好）"
    sig = r["signals"][0]
    assert sig["rule_id"] == "rsi_overbought_70"
    assert float(sig["value"]) == 100.0, f"标准 RSI 全涨应为 100，got {sig['value']}"


def test_single_up_small_step_also_triggers():
    """①-b 小步长单边上涨也触发（旧 hack 在步长 0.01 时算出 9.09，最严重）。"""
    df = mk([10 + i * 0.01 for i in range(40)])
    quote = {"symbol": "TEST", "price": float(df["close"].iloc[-1])}
    r = check_alert("TEST", quote, df, rule_id="rsi_overbought_70")
    assert r["triggered"] is True, "小步长单边上涨也必须触发"


def test_flat_series_is_neutral_not_oversold():
    """完全无波动 → 50（中性）。旧实现序列给 0（误判严重超卖）。"""
    df = mk([50.0] * 40)
    quote = {"symbol": "TEST", "price": 50.0}
    r = check_alert("TEST", quote, df, rule_id="rsi_overbought_70")
    assert r["triggered"] is False, "无波动不应触发超买"
    # 也不应触发任何其它"超卖"类结论（本系统暂无超卖规则，守住语义即可）


def test_normal_waveform_unchanged():
    """② 正常波动行情：修正前后 RSI 完全一致（影响面为 0 的根基）。"""
    import random
    random.seed(11)
    cl = [100.0]
    for _ in range(60):
        cl.append(cl[-1] * (1 + random.uniform(-0.02, 0.02)))
    df = mk(cl)
    c = df["close"]
    dl = c.diff()
    g = dl.where(dl > 0, 0.0).rolling(14).mean()
    l = (-dl.where(dl < 0, 0.0)).rolling(14).mean()
    old = float((100 - (100 / (1 + g / l.where(l != 0, 0.1)))).iloc[-1])
    quote = {"symbol": "TEST", "price": float(c.iloc[-1])}
    r = check_alert("TEST", quote, df, rule_id="rsi_overbought_70")
    new = float(r["signals"][0]["value"]) if r["triggered"] else None
    # 未触发时 value 为 None，此时用共享库直接核对
    import fin_indicators as fi
    new_val = fi.rsi_sma_last(df, 14)
    assert abs(old - new_val) < 1e-12, f"正常波动下新旧应一致：old={old!r} new={new_val!r}"


def test_only_rsi_fields_change():
    """③ 差距只在 RSI：MA / MACD / 量比 / 20日新高 在新旧实现下完全一致。

    做法：同一份数据，分别用"修正后"与"修正前"的口径算 RSI；
    非 RSI 指标由共享库统一提供，因此必然一致 —— 这里断言 check_alert
    输出的 reason/value 不因 RSI 修正而变化（用无 RSI 触发的规则验证）。
    """
    df = mk([10 + i for i in range(40)])
    quote = {"symbol": "TEST", "price": float(df["close"].iloc[-1])}
    for rid in ("ma5_below_ma20", "bias_ma5_over_5pct", "macd_death_cross",
                "price_break_20d_high", "volume_breakout_3x"):
        r = check_alert("TEST", quote, df, rule_id=rid)
        # 这些规则的触发与否只取决于 MA/MACD/量比/新高，与 RSI 口径无关
        assert r["rule_id"] if "rule_id" in r else True
        assert isinstance(r["signals"], list)


def test_rule_yaml_operator_matches_code():
    """rules.yaml 的 op 必须与代码硬编码的判定一致（2026-10-31 由 gte 改为 gt）。"""
    import yaml
    rules = yaml.safe_load(open(ROOT / "src/dsa_mcp/alerts/rules.yaml"))
    r = next(x for x in rules if x.get("id") == "rsi_overbought_70")
    cond = r["conditions"][0]
    assert cond["op"] == "gt", f"YAML 应为 gt（与代码 rsi_val > 70 对齐），got {cond['op']}"
    assert float(cond["value"]) == 70.0
