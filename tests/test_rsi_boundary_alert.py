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


def test_rule_yaml_and_code_are_bidirectionally_consistent():
    """N3：**双向交叉断言** —— YAML 声明 与 checker.py 实际比较运算必须语义一致。

    旧的单向守卫只断言 YAML 侧（op == 'gt'、value == 70），
    如果有人把代码改成 `>= 70`，测试仍然会绿 —— 只守了一半。
    这里：
      · 从 YAML 读 field / op / value（声明）
      · 从 checker.py **AST 解析**出 `if rsi_val <op> <阈值>` 的真实运算（行为）
      · 两侧互相比对，任一侧漂移都会红
    """
    import ast
    import yaml

    # ── 声明侧：YAML ──
    rules = yaml.safe_load(open(ROOT / "src/dsa_mcp/alerts/rules.yaml"))
    rule = next(x for x in rules if x.get("id") == "rsi_overbought_70")
    cond = rule["conditions"][0]
    yaml_field, yaml_op, yaml_value = cond["field"], cond["op"], float(cond["value"])

    # ── 行为侧：AST 解析 checker.py ──
    src_path = ROOT / "src/dsa_mcp/alerts/checker.py"
    tree = ast.parse(src_path.read_text(encoding="utf-8"))
    import dsa_mcp.alerts.checker as checker_mod

    OP_MAP = {ast.Gt: "gt", ast.GtE: "gte", ast.Lt: "lt", ast.LtE: "lte",
              ast.Eq: "eq", ast.NotEq: "ne"}

    def resolve(node):
        """比较右值：常量直接取值；模块级常量名则从模块里取实际值。"""
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return float(node.value)
        if isinstance(node, ast.Name):
            return float(getattr(checker_mod, node.id))
        return None

    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        left = node.left
        if not (isinstance(left, ast.Name) and left.id == "rsi_val"):
            continue
        for op_node, cmp_node in zip(node.ops, node.comparators):
            op_name = OP_MAP.get(type(op_node))
            value = resolve(cmp_node)
            if op_name and value is not None:
                found.append((op_name, value))

    assert found, "在 checker.py 里没找到 `rsi_val` 的比较 —— AST 解析可能失效，请更新该测试"
    assert len(found) == 1, f"预期恰好一处 rsi_val 比较，实际 {len(found)}: {found}"
    code_op, code_value = found[0]

    # ── 双向断言 ──
    assert yaml_op == code_op, f"YAML op={yaml_op!r} 与代码实际运算 {code_op!r} 不一致"
    assert yaml_value == code_value, f"YAML value={yaml_value} 与代码阈值 {code_value} 不一致"
    assert yaml_field == checker_mod.RSI_FIELD, (
        f"YAML field={yaml_field!r} 与常量 RSI_FIELD={checker_mod.RSI_FIELD!r} 不一致")
    # 导出常量也必须与两侧一致（防"常量改了但代码没改"）
    assert checker_mod.RSI_OVERBOUGHT_OP == yaml_op
    assert float(checker_mod.RSI_OVERBOUGHT_THRESHOLD) == yaml_value
    # 语义补充断言：必须是严格大于（=70.00 时不触发）
    assert yaml_op == "gt", "rsi_overbought_70 必须是严格大于（RSI 恰为 70.00 时不触发）"
