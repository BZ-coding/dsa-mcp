# -*- coding: utf-8 -*-
"""Volume-price analysis — ported from dSA."""
import logging
from typing import Optional
import pandas as pd

logger = logging.getLogger(__name__)


def get_volume_analysis(symbol: str, df: pd.DataFrame) -> dict:
    """Analyse volume-price relationship."""
    if df is None or df.empty or len(df) < 5:
        return {"error": f"Insufficient data for {symbol}"}

    close = df["close"] if "close" in df.columns else df.iloc[:, 4]
    volume = df["volume"] if "volume" in df.columns else df.iloc[:, 5]

    avg_vol_5 = float(volume.tail(5).mean())
    avg_vol_10 = float(volume.tail(10).mean())
    avg_vol_20 = float(volume.tail(20).mean()) if len(df) >= 20 else avg_vol_10
    latest_vol = float(volume.iloc[-1])
    vol_ratio_5d = round(latest_vol / avg_vol_5, 2) if avg_vol_5 > 0 else None

    price_up = close.diff() > 0
    up_days = df[price_up]
    down_days = df[~price_up]
    avg_up_vol = float(up_days["volume"].mean()) if len(up_days) > 0 else 0
    avg_down_vol = float(down_days["volume"].mean()) if len(down_days) > 0 else 0

    pattern = "未知"
    if avg_up_vol > avg_down_vol * 1.3:
        pattern = "量价配合良好（上涨放量、下跌缩量）"
    elif avg_down_vol > avg_up_vol * 1.3:
        pattern = "量价背离（下跌放量、上涨缩量，偏空）"
    elif vol_ratio_5d and vol_ratio_5d > 1.5:
        pattern = "近期明显放量"
    elif vol_ratio_5d and vol_ratio_5d < 0.6:
        pattern = "近期明显缩量"
    else:
        pattern = "量价关系中性"

    return {
        "code": symbol,
        "period_days": len(df),
        "latest_volume": latest_vol,
        "avg_volume_5d": round(avg_vol_5, 0),
        "avg_volume_20d": round(avg_vol_20, 0),
        "volume_ratio_vs_5d": vol_ratio_5d,
        "avg_up_day_volume": round(avg_up_vol, 0),
        "avg_down_day_volume": round(avg_down_vol, 0),
        "volume_price_pattern": pattern,
    }
