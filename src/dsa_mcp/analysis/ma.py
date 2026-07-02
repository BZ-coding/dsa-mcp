# -*- coding: utf-8 -*-
"""MA calculator — ported from dSA analysis_tools.py

Data dependency removed: accepts df (pd.DataFrame) as parameter."""
import logging
from typing import Optional, Any
import pandas as pd

logger = logging.getLogger(__name__)


def calculate_ma(symbol: str, df: pd.DataFrame, periods: Optional[str] = None) -> dict:
    """Calculate moving averages for arbitrary periods from OHLCV DataFrame."""
    if df is None or df.empty:
        return {"error": f"No data for {symbol}"}

    default_periods = [5, 10, 20, 30, 60, 120, 250]
    if periods:
        try:
            requested = [int(p.strip()) for p in periods.split(",") if p.strip().isdigit()]
            period_list = sorted(set(requested)) if requested else default_periods
        except Exception:
            period_list = default_periods
    else:
        period_list = default_periods

    close = df["close"] if "close" in df.columns else df.get("收盘", df.iloc[:, 4])
    current_price = float(close.iloc[-1])
    result = {
        "code": symbol,
        "current_price": round(current_price, 2),
        "data_points": len(df),
        "ma": {},
    }

    for period in period_list:
        if len(close) < period:
            result["ma"][f"ma{period}"] = None
            continue
        ma_val = float(close.rolling(window=period).mean().iloc[-1])
        bias = round((current_price - ma_val) / ma_val * 100, 2) if ma_val else None
        result["ma"][f"ma{period}"] = {
            "value": round(ma_val, 2),
            "bias_pct": bias,
            "price_above": current_price > ma_val,
        }

    ma_values = [v for v in result["ma"].values() if v is not None]
    above_count = sum(1 for v in ma_values if v["price_above"])
    result["above_ma_count"] = above_count
    result["total_ma_count"] = len(ma_values)
    result["ma_alignment"] = (
        "多头排列" if above_count == len(ma_values)
        else "空头排列" if above_count == 0
        else f"混合({above_count}/{len(ma_values)}条均线上方)"
    )
    return result
