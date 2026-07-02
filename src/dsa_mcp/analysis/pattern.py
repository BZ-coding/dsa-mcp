# -*- coding: utf-8 -*-
"""Candlestick pattern detection — ported from dSA."""
import logging
import numpy as np
import pandas as pd
from typing import Optional

logger = logging.getLogger(__name__)


def analyze_pattern(symbol: str, df: pd.DataFrame) -> dict:
    """Detect candlestick and chart patterns."""
    if df is None or df.empty or len(df) < 10:
        return {"error": f"Insufficient data for {symbol}"}

    o = df["open"].values if "open" in df.columns else df.iloc[:, 1].values
    h = df["high"].values if "high" in df.columns else df.iloc[:, 2].values
    l = df["low"].values if "low" in df.columns else df.iloc[:, 3].values
    c = df["close"].values if "close" in df.columns else df.iloc[:, 4].values
    v = df["volume"].values if "volume" in df.columns else df.iloc[:, 5].values
    n = len(c)

    def body(i): return abs(c[i] - o[i])
    def us(i): return h[i] - max(c[i], o[i])
    def ls(i): return min(c[i], o[i]) - l[i]
    def is_bullish(i): return c[i] > o[i]
    def is_bearish(i): return c[i] < o[i]

    avg_body = sum(body(i) for i in range(n)) / n if n > 0 else 1
    patterns_detected = []

    for i in range(max(0, n - 3), n):
        bd = body(i); uss = us(i); lss = ls(i)
        if bd < avg_body * 0.1 and (uss + lss) > bd * 3:
            patterns_detected.append({"pattern": "十字星 (Doji)", "type": "reversal_signal", "strength": "弱", "desc": "多空平衡"})
        if lss > body(i) * 2 and uss < body(i) * 0.5:
            label = "锤子线 (Hammer)" if i < 1 or c[i] >= c[i-1] else "上吊线 (Hanging Man)"
            patterns_detected.append({"pattern": label, "type": "reversal_signal", "strength": "中", "desc": "下影线长"})
        if uss > body(i) * 2 and lss < body(i) * 0.5:
            label = "流星线 (Shooting Star)" if is_bearish(i) else "倒锤子"
            patterns_detected.append({"pattern": label, "type": "reverse_signal", "strength": "中", "desc": "上影线长"})

    if n >= 3:
        i = n - 1
        if (is_bearish(i-2) and body(i-2) > avg_body * 1.5
                and body(i-1) < avg_body * 0.4
                and is_bullish(i) and body(i) > avg_body * 1.5
                and c[i] > (o[i-2] + c[i-2]) / 2):
            patterns_detected.append({"pattern": "早晨之星 (Morning Star)", "type": "bullish_reversal", "strength": "强", "desc": "三根K线底部反转"})
        if (is_bullish(i-2) and body(i-2) > avg_body * 1.5
                and body(i-1) < avg_body * 0.4
                and is_bearish(i) and body(i) > avg_body * 1.5
                and c[i] < (o[i-2] + c[i-2]) / 2):
            patterns_detected.append({"pattern": "黄昏之星 (Evening Star)", "type": "bearish_reversal", "strength": "强", "desc": "三根K线顶部反转"})
        if (is_bullish(i) and is_bearish(i-1) and o[i] < c[i-1] and c[i] > o[i-1]):
            patterns_detected.append({"pattern": "看涨吞没 (Bullish Engulfing)", "type": "bullish_reversal", "strength": "强", "desc": "阳线覆盖前阴线"})
        elif (is_bearish(i) and is_bullish(i-1) and o[i] > c[i-1] and c[i] < o[i-1]):
            patterns_detected.append({"pattern": "看跌吞没 (Bearish Engulfing)", "type": "bearish_reversal", "strength": "强", "desc": "阴线覆盖前阳线"})

    unique = list({p["pattern"]: p for p in patterns_detected}.values())
    return {
        "code": symbol,
        "period_days": len(df),
        "current_price": round(float(c[-1]), 2),
        "patterns_count": len(unique),
        "patterns": unique,
        "summary": "未发现明显形态" if not unique else "、".join(p["pattern"] for p in unique),
    }
