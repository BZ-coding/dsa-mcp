#!/usr/bin/env python3
"""bias_ma5_over_5pct 符号误判修复的专项回归（2026-11-09）

背景：checker.py 里该规则原为 `if abs(bias) > 5.0`，把**负乖离**也当
"乖离率超5%" 推送 —— 2026-11-09 美团 13:52 跌破 MA5 至 -5.01%，
被报成"乖离超5%"（语义完全颠倒：负乖离是回调信号，不是追高风险）。

修复：只对**正向**乖离（bias > +5%）触发。
本测试守住三条：
  ① 正向乖离 > +5% 必须触发（功能不回退）
  ② 负乖离 < -5% 绝不触发（本次误报根因）
  ③ |bias| ≤ 5% 的中性区间不触发
"""
import sys
from pathlib import Path

import pandas as pd

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


def _run(closes, last_price):
    df = mk(closes)
    quote = {"symbol": "TEST", "price": last_price}
    return check_alert("TEST", quote, df, rule_id="bias_ma5_over_5pct")


def test_positive_bias_over_5pct_triggers():
    """① 正向乖离 +5% 以上必须触发（修复不能把功能改没了）。"""
    # 40 根 K 线收在 100，最后 2 根拉到 110：
    # MA5 = (100+100+100+110+110)/5 = 104 → bias = (110-104)/104 ≈ +5.77%
    closes = [100.0] * 38 + [110.0, 110.0]
    r = _run(closes, 110.0)
    assert r["triggered"] is True, "正向乖离 +5.77% 必须触发"
    sig = r["signals"][0]
    assert sig["value"] > 5.0, f"value 应为正乖离，got {sig['value']}"
    assert "勿追高" in sig["reason"], "reason 应保持追高警示语义"


def test_negative_bias_never_triggers():
    """② 负乖离 -5% 以下绝不触发（2026-11-09 误报根因）。

    复刻生产事故：美团 13:52 价格跌破 MA5 达 -5.01%。
    构造：38 根 100 + 2 根 90 → MA5 = (300+180)/5 = 96 → bias ≈ -6.25%。
    修复前 abs() 会触发（误报），修复后必须静默。
    """
    closes = [100.0] * 38 + [90.0, 90.0]
    r = _run(closes, 90.0)
    assert r["triggered"] is False, (
        f"负乖离 {r['signals'][0]['value'] if r['signals'] else '(无信号)'} "
        "是回调信号，不是追高风险，绝不能触发 bias_ma5_over_5pct"
    )


def test_neutral_band_does_not_trigger():
    """③ 中性区间（|bias| ≤ 5%）双向都不触发。"""
    closes = [100.0] * 40          # bias = 0
    r = _run(closes, 100.0)
    assert r["triggered"] is False, "零乖离不应触发"
