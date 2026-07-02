# -*- coding: utf-8 -*-
"""
Alert checker — pure signal generation.

Iron rule: returns {triggered, signals[]} only. No push, no DB writes, no scheduling.
Data source: ALL from 8084 MCP (passed in as parameters).
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Optional

import yaml

logger = logging.getLogger(__name__)


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
    if quote and isinstance(quote, dict):
        current_price = quote.get("price") or (quote.get("data") or {}).get("price")

    # Pre-compute indicators
    ma5 = None
    ma20 = None
    volume_ratio = None
    if kline_df is not None and not kline_df.empty and len(kline_df) >= 20:
        close_series = close.astype(float)
        vol_series = vol.astype(float)
        ma5 = float(close_series.rolling(5).mean().iloc[-1])
        ma20 = float(close_series.rolling(20).mean().iloc[-1])
        avg_vol_5 = float(vol_series.tail(5).mean())
        if avg_vol_5 > 0:
            volume_ratio = float(vol_series.iloc[-1]) / avg_vol_5
        # MACD
        ema12 = close_series.ewm(span=12, adjust=False).mean()
        ema26 = close_series.ewm(span=26, adjust=False).mean()
        dif = ema12 - ema26
        dea = dif.ewm(span=9, adjust=False).mean()
        macd_bar = (dif - dea) * 2
        macd_death_cross = (dif.iloc[-2] >= dea.iloc[-2]) and (dif.iloc[-1] < dea.iloc[-1])
        # RSI
        delta = close_series.diff()
        gain = delta.where(delta > 0, 0.0).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0.0)).rolling(14).mean()
        rsi = 100 - (100 / (1 + gain / loss.where(loss != 0, 0.1))) if loss.iloc[-1] > 0 else 50
        rsi_val = float(rsi.iloc[-1])

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
            if abs(bias) > 5.0:
                triggered = True
                reason = f"乖离率 {bias:+.2f}% 超 5%"

        elif rid == "volume_breakout_3x" and volume_ratio and volume_ratio > 3.0:
            value = round(volume_ratio, 2)
            triggered = True
            reason = f"放量 {volume_ratio:.2f} 倍"

        elif rid == "price_break_20d_high" and kline_df is not None and not kline_df.empty and current_price:
            high_20d = float(high.astype(float).iloc[-21:-1].max()) if len(high) >= 21 else None
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
            if rsi_val > 70:
                value = round(rsi_val, 1)
                triggered = True
                reason = f"RSI {value} > 70, 超买"

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
            matched_anns = []
            for ann in announcements:
                title = ann.get("title", "")
                matched_kw = next((kw for kw in keywords if kw in title), None)
                if matched_kw:
                    matched_anns.append({
                        "ann": ann,
                        "kw": matched_kw,
                        "title": title,
                    })
            if not matched_anns:
                continue
            # 按 announcement_time 倒序, 最新优先, 但最多返 3 条避免一条 sym 一推就刷屏
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

    return {
        "symbol": symbol,
        "triggered": len(signals) > 0,
        "signals": signals,
    }
