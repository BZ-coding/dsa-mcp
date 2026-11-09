# -*- coding: utf-8 -*-
"""
Alert checker — pure signal generation.

Iron rule: returns {triggered, signals[]} only. No push, no DB writes, no scheduling.
Data source: ALL from 8084 MCP (passed in as parameters).
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

import yaml

import fin_indicators  # 中性指标计算库（纯函数，无 IO）

logger = logging.getLogger(__name__)

# ── RSI 超买判定参数（**唯一真源**，2026-10-31）──────────────────────────
# ⚠️ 这三个值必须与 `alerts/rules.yaml` 里 `rsi_overbought_70` 的
#    `conditions[0]`（field/op/value）保持一致。
#    `tests/test_rule_yaml_operator_matches_code.py` 做**双向交叉断言**：
#    既从本文件做 AST 解析读实际比较运算，也从 YAML 读声明，任一侧漂移都会红。
#    （2026-10-31 前 YAML 写的是 gte、代码是严格大于，属于"声明与行为不一致"。）
RSI_OVERBOUGHT_THRESHOLD = 70.0
RSI_OVERBOUGHT_OP = "gt"          # gt = 严格大于（rsi_val > 阈值）
RSI_FIELD = "rsi_14"



CUSTOM_RULES_PATH = Path.home() / ".hermes" / "custom_alert_rules.json"


def _load_custom_rules() -> list:
    """App 自定义告警规则（JSON）。失败返回空，不影响内置规则。"""
    try:
        if not CUSTOM_RULES_PATH.exists():
            return []
        data = json.loads(CUSTOM_RULES_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception as exc:
        logger.warning("读取自定义告警规则失败: %s", exc)
        return []


def _load_rules(path: Optional[Path] = None) -> list:
    """Load alert rules from YAML."""
    if path is None:
        path = Path(__file__).resolve().parent / "rules.yaml"
    if not path.exists():
        return []
    with open(path) as f:
        rules = yaml.safe_load(f)
    return rules if isinstance(rules, list) else []


def check_alert(
    symbol: str,
    quote: dict,
    kline_df: "pd.DataFrame | None",
    announcements: Optional[list] = None,
    rule_id: Optional[str] = None,
    fund_flow: Optional[list] = None,
) -> dict:
    """
    Check alerts for a symbol. Pure signal generation.

    Args:
        symbol: Stock code
        quote: Realtime quote dict from 8084
        kline_df: OHLCV DataFrame from 8084 (can be empty)
        announcements: List of {title, announcement_time, link} from 8084
        rule_id: If set, check only this rule. None = all.

    Returns:
        {symbol, triggered, signals: [{rule_id, severity, name, value, reason, source, link}]}
    """
    import pandas as pd

    rules = _load_rules()
    if rule_id:
        rules = [r for r in rules if r.get("id") == rule_id]

    signals = []
    if kline_df is not None and not kline_df.empty:
        close = kline_df["close"] if "close" in kline_df.columns else kline_df.iloc[:, 4]
        vol = kline_df["volume"] if "volume" in kline_df.columns else kline_df.iloc[:, 5]
        high = kline_df["high"] if "high" in kline_df.columns else kline_df.iloc[:, 2]
        low = kline_df["low"] if "low" in kline_df.columns else kline_df.iloc[:, 3]

    current_price = None
    change_pct_val = None
    if quote and isinstance(quote, dict):
        current_price = (
            quote.get("price")
            or quote.get("last")
            or (quote.get("data") or {}).get("price")
        )
        change_pct_val = quote.get("change_pct")

    # Pre-compute indicators —— 实体在共享库 fin_indicators（纯函数，无 IO）
    #
    # ⚠️⚠️ **RSI 口径注意**：告警这里用的是 `rsi_sma`（SMA 平滑），
    #    **不是** 看板/`get_indicators` 用的 Wilder 平滑。两套口径**都正确**，
    #    区别只在平滑方式（SMA vs Wilder），边界处理完全一致。
    #
    #    📌 **2026-10-31 口径修正**：此前这里是 `rsi_sma_legacy`，带除零 hack
    #    （`loss.where(loss != 0, 0.1)`）+ 末值 "loss==0 → 50" 兜底，
    #    导致**单边上涨行情下 RSI 恒为 50 → `rsi_overbought_70` 永不触发**（漏报）。
    #    已改为标准定义：avg_loss==0 → 100 / avg_gain==0 → 0 / 两者都 0 → 50。
    #    → **2026-10-31 之前的告警历史里 RSI 值基于缺陷口径，不可与之后的记录直接比较。**
    #    影响面实测：3 个有日线的标的、227 个有效交易日，新旧值 **0 天不同**。
    ma5 = None
    ma20 = None
    volume_ratio = None
    rsi_val = None
    bias_val = None
    if kline_df is not None and not kline_df.empty and len(kline_df) >= 20:
        mas = fin_indicators.ma_series(kline_df, (5, 20))
        ma5 = float(mas['MA5'].iloc[-1])
        ma20 = float(mas['MA20'].iloc[-1])
        volume_ratio = fin_indicators.volume_stats(kline_df, short=5, long=10)['ratio_short']
        # MACD
        macd = fin_indicators.macd_series(kline_df)
        dif = macd['MACD_DIF']
        dea = macd['MACD_DEA']
        macd_death_cross = (dif.iloc[-2] >= dea.iloc[-2]) and (dif.iloc[-1] < dea.iloc[-1])
        # RSI（历史口径，见上面的口径注意）
        rsi_val = fin_indicators.rsi_sma_last(kline_df, 14)
        if current_price and ma5 and ma5 > 0:
            bias_val = (current_price - ma5) / ma5 * 100

    for rule in rules:
        rid = rule.get("id", "")
        rname = rule.get("name", rid)
        severity = rule.get("severity", "low")
        conditions = rule.get("conditions", [])

        triggered = False
        value = None
        reason = ""

        if rid == "bias_ma5_over_5pct" and current_price and ma5 and ma5 > 0:
            bias = (current_price - ma5) / ma5 * 100
            value = round(bias, 2)
            # 2026-11-09 修复：原 `abs(bias) > 5.0` 把 -5.01% 负乖离也当成"超5%"推送
            # （11-09 美团 13:52 跌破 MA5 至 -5.01% 被报成"乖离率超5%"）。
            # 负乖离 = 跌破均线的回调信号，不是追高风险；本规则只管**正向**乖离
            # （价格远在 MA5 上方）。深度回调类提醒若将来需要，应另立规则而非共用本条。
            if bias > 5.0:
                triggered = True
                reason = f"乖离率 {bias:+.2f}% 超 +5%（价格远高于 MA5，勿追高）"

        elif rid == "volume_breakout_3x" and volume_ratio and volume_ratio > 3.0:
            value = round(volume_ratio, 2)
            triggered = True
            reason = f"放量 {volume_ratio:.2f} 倍"

        elif rid == "price_break_20d_high" and kline_df is not None and not kline_df.empty and current_price:
            high_20d = fin_indicators.high_low_n(high, n=20)['high']
            if high_20d and current_price > high_20d:
                value = round(current_price, 2)
                triggered = True
                reason = f"收盘价 {value} 突破 20 日最高 {high_20d}"

        elif rid == "macd_death_cross" and kline_df is not None and not kline_df.empty:
            if macd_death_cross:
                value = round(float(dif.iloc[-1]), 4)
                triggered = True
                reason = "MACD 死叉"

        elif rid == "rsi_overbought_70" and kline_df is not None and not kline_df.empty:
            if rsi_val > RSI_OVERBOUGHT_THRESHOLD:
                value = round(rsi_val, 1)
                triggered = True
                reason = f"RSI {value} > {RSI_OVERBOUGHT_THRESHOLD:g}, 超买"

        elif rid == "ma5_below_ma20" and ma5 and ma20:
            if ma5 < ma20:
                value = round(ma5, 2)
                triggered = True
                reason = f"MA5({ma5:.2f}) < MA20({ma20:.2f}), 空头排列"

        if triggered:
            signals.append({
                "rule_id": rid,
                "severity": severity,
                "name": rname,
                "value": value,
                "reason": reason,
                "source": "technical" if current_price is not None or volume_ratio is not None else "unknown",
                "link": None,
            })

    # ── App 自定义规则（通用条件求值） ──
    custom_values = {
        "price": current_price,
        "change_pct": change_pct_val,
        "bias_ma5_pct": bias_val,
        "rsi_14": rsi_val,
        "volume_ratio": volume_ratio,
        "ma5": ma5,
        "ma20": ma20,
    }
    for crule in _load_custom_rules():
        if not crule.get("enabled", True):
            continue
        rule_symbol = str(crule.get("symbol") or "")
        if rule_symbol and rule_symbol != symbol:
            continue
        field = crule.get("field")
        op = crule.get("op")
        threshold = crule.get("value")
        value = custom_values.get(field)
        if value is None or threshold is None:
            continue
        try:
            threshold = float(threshold)
        except (TypeError, ValueError):
            continue
        hit = (
            (op == "gt" and value > threshold)
            or (op == "gte" and value >= threshold)
            or (op == "lt" and value < threshold)
            or (op == "lte" and value <= threshold)
        )
        if not hit:
            continue
        label = crule.get("field_label") or field
        signals.append({
            "rule_id": f"custom:{crule.get('id', '')}",
            "severity": crule.get("severity", "low"),
            "name": crule.get("name") or f"{label} {op} {threshold:g}",
            "value": round(value, 4),
            "reason": f"{label} {value:.4g} {op} {threshold:g}",
            "source": "custom",
            "link": None,
        })

    # ── Semantic rules (announcement title matching) ──
    _target_rule = rule_id or ""
    ann_rules = [
        r for r in rules
        if any(c.get("field") == "announcement_title" for c in r.get("conditions", []))
    ]
    if _target_rule:
        ann_rules = [r for r in ann_rules if r.get("id") == _target_rule]

    if announcements and ann_rules:
        for arule in ann_rules:
            rid = arule.get("id", "")
            rname = arule.get("name", rid)
            severity = arule.get("severity", "low")
            keywords = []
            for c in arule.get("conditions", []):
                v = c.get("value")
                if isinstance(v, list):
                    keywords.extend(v)
                elif isinstance(v, str):
                    keywords.append(v)
            if not keywords:
                continue
            # Phase E: exclude_keywords 用于排除误判场景 (e.g. 南向资金减持)
            exclude_keywords = arule.get("exclude_keywords") or []
            matched_anns = []
            for ann in announcements:
                title = ann.get("title", "")
                # 命中关键词
                matched_kw = next((kw for kw in keywords if kw in title), None)
                if not matched_kw:
                    continue
                # 排除关键词命中 → 跳过
                if any(ekw in title for ekw in exclude_keywords):
                    continue
                matched_anns.append({
                    "ann": ann,
                    "kw": matched_kw,
                    "title": title,
                })
            if not matched_anns:
                continue
            matched_anns.sort(key=lambda x: x["ann"].get("announcement_time", ""), reverse=True)
            for m in matched_anns[:3]:
                signals.append({
                    "rule_id": rid,
                    "severity": severity,
                    "name": rname,
                    "value": m["kw"],
                    "reason": f"公告标题含 \"{m['kw']}\": {m['title'][:60]}",
                    "source": "announcement",
                    "link": m["ann"].get("link"),
                    "announcement_time": m["ann"].get("announcement_time"),
                    "announcement_id": m["ann"].get("id") or m["ann"].get("announcement_id"),
                })

    # ── Fund flow rules (main_inflow / main_outflow surge) ──
    ff_rules = [
        r for r in rules
        if any(c.get("field") == "main_net_inflow" for c in r.get("conditions", []))
    ]
    if _target_rule:
        ff_rules = [r for r in ff_rules if r.get("id") == _target_rule]

    if fund_flow and ff_rules:
        # 用最新一天数据判断
        latest = fund_flow[0]
        main_inflow = float(latest.get("main_net_inflow", 0) or 0)
        change_pct = float(latest.get("change_pct", 0) or 0)
        rank_date = latest.get("rank_data", "")

        for frule in ff_rules:
            rid = frule.get("id", "")
            rname = frule.get("name", rid)
            severity = frule.get("severity", "low")
            for cond in frule.get("conditions", []):
                op = cond.get("op")
                threshold = cond.get("value")
                if op == "gte" and main_inflow >= threshold:
                    signals.append({
                        "rule_id": rid,
                        "severity": severity,
                        "name": rname,
                        "value": main_inflow,
                        "reason": f"{rank_date}: 主力净流入 {main_inflow/1e8:.2f} 亿 (涨幅 {change_pct*100:+.2f}%)",
                        "source": "fund_flow",
                        "rank_data": rank_date,
                        "fund_flow_id": latest.get("id"),
                    })
                elif op == "lte" and main_inflow <= threshold:
                    signals.append({
                        "rule_id": rid,
                        "severity": severity,
                        "name": rname,
                        "value": main_inflow,
                        "reason": f"{rank_date}: 主力净流出 {main_inflow/1e8:.2f} 亿 (涨幅 {change_pct*100:+.2f}%)",
                        "source": "fund_flow",
                        "rank_data": rank_date,
                        "fund_flow_id": latest.get("id"),
                    })

    return {
        "symbol": symbol,
        "triggered": len(signals) > 0,
        "signals": signals,
    }
