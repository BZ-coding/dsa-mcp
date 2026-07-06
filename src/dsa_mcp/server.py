"""
dsa-mcp MCP Server — analysis tools ported from daily_stock_analysis (52k★)

Iron rule (zsd 2026-07-02): ALL data comes from 8084 REST (/api/v1/history +
/api/v1/data). No direct database / no dSA DB imports. Aggregation of intraday
ticks into daily K-line happens in-process.

Protocol: stdio (managed by systemd as dsa-mcp.service)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

import httpx
import yaml

# Ported modules
from dsa_mcp.analysis.trend import StockTrendAnalyzer, TrendAnalysisResult
from dsa_mcp.analysis.ma import calculate_ma as _calc_ma
from dsa_mcp.analysis.volume import get_volume_analysis as _calc_volume
from dsa_mcp.analysis.pattern import analyze_pattern as _analyze_pattern

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────
# 8084 MCP client — ALL data goes through this
# ──────────────────────────────────────────────────

_FDS_REST = os.environ.get("FDS_BASE_URL", "http://localhost:8084")
_HTTP_TIMEOUT = 15.0
_http_client: httpx.AsyncClient | None = None


async def _get_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(timeout=_HTTP_TIMEOUT)
    return _http_client


async def _fetch_kline(symbol: str, days: int = 60) -> list[dict]:
    """
    Fetch OHLCV from 8084 REST and aggregate intraday ticks into daily K-line.

    8084 history?data_type=price returns intraday snapshots (~every 5min during
    trading hours). Group by trade_date, produce open/high/low/close/volume.

    Schema expected by analysis tools (StockTrendAnalyzer):
      [{"date": "YYYY-MM-DD", "open": float, "high": float, "low": float,
        "close": float, "volume": float}, ...]
    """
    client = await _get_client()
    try:
        resp = await client.get(
            f"{_FDS_REST}/api/v1/history",
            params={"symbol": symbol, "data_type": "price", "days": days},
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception as e:
        logger.warning(f"_fetch_kline({symbol}) failed: {e}")
        return []

    items = payload.get("items", [])
    if not items:
        return []

    pd_mod = _get_pd()
    df = pd_mod.DataFrame(items)
    if df.empty or "trade_time" not in df.columns:
        return []

    df["trade_time"] = pd_mod.to_datetime(df["trade_time"], errors="coerce")
    df["date"] = df["trade_time"].dt.date.astype(str)

    daily = (
        df.groupby("date", sort=True)
        .agg(
            open=("open", "first"),
            high=("high", "max"),
            low=("low", "min"),
            close=("price", "last"),
            volume=("volume", "sum"),
        )
        .reset_index()
        .to_dict(orient="records")
    )
    return daily


async def _fetch_quote(symbol: str) -> dict:
    """Fetch realtime quote from 8084 REST (/api/v1/data?data_type=price)."""
    client = await _get_client()
    try:
        resp = await client.get(
            f"{_FDS_REST}/api/v1/data",
            params={
                "source": "akshare",
                "symbol": symbol,
                "data_type": "price",
                "fresh": "false",
            },
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.warning(f"_fetch_quote({symbol}) failed: {e}")
        return {}

async def _fetch_fund_flow(symbol: str, days: int = 5) -> list[dict]:
    """
    Fetch single-symbol fund flow from 8084 REST.

    A 股 → /data?data_type=fund_flow&symbol=002202 (eastmoney 主力排行)
    港股 → akshare 不支持 hk fund_flow, 返 []

    Returns list of {rank_data, main_net_inflow, change_pct, super_large_net, large_net, medium_net, small_net}.
    Used by fund-flow alert rules (main_inflow_surge, main_outflow_surge).
    """
    is_hk = symbol.startswith("hk")
    if is_hk:
        return []  # akshare 不支持 hk 资金流
    client = await _get_client()
    try:
        resp = await client.get(
            f"{_FDS_REST}/api/v1/data",
            params={
                "source": "akshare",
                "symbol": symbol,
                "data_type": "fund_flow",
                "flow_type": "main_fund_rank",
                "days": days,
                "limit": days,
                "fresh": "false",
            },
        )
        resp.raise_for_status()
        items = resp.json().get("items", []) or []
        return items
    except Exception as e:
        logger.warning(f"_fetch_fund_flow({symbol}) failed: {e}")
        return []


async def _fetch_announcements(symbol: str, days: int = 90) -> list[dict]:
    """
    Fetch company announcements from 8084 REST.

    A 股 → /data?data_type=announcement (akshare 巨潮)
    港股 → 优先 /data?data_type=hkex_announcement (港交所披露易, 标的专属)
           fallback → /data?data_type=stock_news (东方财富聚合流, Phase 5c 兼容)

    Returns list of {id, title, announcement_time, link, source}.
    Used by semantic alert rules (insider_reduction, earnings_warning,
    regulatory_penalty, lockup_expiry, major_event).
    """
    client = await _get_client()
    is_hk = symbol.startswith("hk")

    items: list[dict] = []
    primary_type = None

    # 港股: 优先 hkex_announcement (Phase 7, 2026-07-06)
    if is_hk:
        try:
            resp = await client.get(
                f"{_FDS_REST}/api/v1/data",
                params={
                    "source": "akshare",
                    "symbol": symbol,
                    "data_type": "hkex_announcement",
                    "fresh": "false",
                },
            )
            resp.raise_for_status()
            items = resp.json().get("items", []) or []
            primary_type = "hkex_announcement"
        except Exception as e:
            logger.warning(f"_fetch_announcements({symbol} hkex) failed: {e}")

    # 港股 fallback + A 股: stock_news / announcement
    if not items:
        if is_hk:
            fallback_type = "stock_news"
        else:
            fallback_type = "announcement"
        try:
            resp = await client.get(
                f"{_FDS_REST}/api/v1/data",
                params={
                    "source": "akshare",
                    "symbol": symbol,
                    "data_type": fallback_type,
                    "fresh": "false",
                },
            )
            resp.raise_for_status()
            items = resp.json().get("items", []) or []
            primary_type = fallback_type
        except Exception as e:
            logger.warning(f"_fetch_announcements({symbol} {fallback_type}) failed: {e}")

    # A 股 announcement 可能 404 (新 symbol 还没拉过), fallback 到 stock_news
    if not items and not is_hk:
        try:
            resp = await client.get(
                f"{_FDS_REST}/api/v1/data",
                params={
                    "source": "akshare",
                    "symbol": symbol,
                    "data_type": "stock_news",
                    "fresh": "false",
                },
            )
            resp.raise_for_status()
            items = resp.json().get("items", []) or []
            primary_type = "stock_news"
        except Exception as e:
            logger.warning(f"_fetch_announcements({symbol} fallback stock_news) failed: {e}")

    if not items:
        return []

    # Filter by recency (time field varies: announcement_time / published)
    from datetime import datetime, timedelta
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()[:10]
    out: list[dict] = []
    for it in items:
        ts = it.get("announcement_time") or it.get("published") or ""
        if str(ts)[:10] >= cutoff:
            out.append({
                "id": it.get("id"),
                "title": it.get("title", ""),
                "announcement_time": ts,
                "link": it.get("link", ""),
                "source": primary_type,  # 标记 source (announcement / stock_news) 便于审计
            })
    return out


# ──────────────────────────────────────────────────
# Import pandas lazily (heavy)
# ──────────────────────────────────────────────────

def _get_pd():
    import pandas as pd
    return pd


# ──────────────────────────────────────────────────
# MCP server
# ──────────────────────────────────────────────────

from mcp.server import Server

app = Server("dsa-mcp")


# ── Tool 1: calculate_macd ──


@app.list_tools()
async def list_tools():
    """Return tool descriptions for MCP protocol."""
    from mcp.types import Tool
    tools = [
        Tool(
            name="calculate_macd",
            description="Calculate MACD indicator from K-line data. Fetches 60 days K-line from 8084 internally.",
            inputSchema={
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "Stock code, e.g. '600519'"},
                    "fast": {"type": "integer", "description": "Fast EMA period (default: 12)"},
                    "slow": {"type": "integer", "description": "Slow EMA period (default: 26)"},
                    "signal": {"type": "integer", "description": "Signal period (default: 9)"},
                },
                "required": ["symbol"],
            },
        ),
        Tool(
            name="calculate_ma",
            description="Calculate moving averages (MA5/10/20/30/60/120/250 or custom periods) for a stock. Fetches K-line from 8084.",
            inputSchema={
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "Stock code"},
                    "periods": {"type": "string", "description": "Comma-separated periods (default: '5,10,20,60')"},
                },
                "required": ["symbol"],
            },
        ),
        Tool(
            name="get_volume_analysis",
            description="Analyse volume-price relationship. Fetches K-line from 8084.",
            inputSchema={
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "Stock code"},
                },
                "required": ["symbol"],
            },
        ),
        Tool(
            name="analyze_pattern",
            description="Detect candlestick patterns (Doji, Hammer, Star, Engulfing, Double Bottom, breakout). Fetches K-line from 8084.",
            inputSchema={
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "Stock code"},
                    "days": {"type": "integer", "description": "Lookback days (default: 60)"},
                },
                "required": ["symbol"],
            },
        ),
        Tool(
            name="analyze_trend",
            description="Comprehensive technical trend analysis. Returns MACD, RSI, MA alignment, support/resistance, buy/sell signal.",
            inputSchema={
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "Stock code"},
                    "days": {"type": "integer", "description": "Lookback days (default: 60)"},
                },
                "required": ["symbol"],
            },
        ),
        Tool(
            name="list_strategies",
            description="List all 15 trading strategies from dSA. Returns name + display_name + category for each.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="get_strategy",
            description="Get the full instructions text of a trading strategy by ID.",
            inputSchema={
                "type": "object",
                "properties": {
                    "strategy_id": {"type": "string", "description": "Strategy ID (e.g. 'ma_golden_cross')"},
                },
                "required": ["strategy_id"],
            },
        ),
        Tool(
            name="list_alert_types",
            description="List all available alert/rule types. Each has name, severity, description.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="check_alert",
            description="Check alerts for a symbol. Pure signal, no push. Data from 8084 MCP.",
            inputSchema={
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "Stock code to check"},
                    "rule_id": {"type": "string", "description": "Optional, check only this rule. None = all."},
                },
                "required": ["symbol"],
            },
        ),
        Tool(
            name="get_agent_prompt",
            description="Get an agent prompt template (port from dSA). Agent names: technical, intel, risk, portfolio, decision, decision_chat.",
            inputSchema={
                "type": "object",
                "properties": {
                    "agent_name": {"type": "string", "description": "Agent name"},
                },
                "required": ["agent_name"],
            },
        ),
    ]
    return tools


# ── Tool implementations ──

@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list:
    from mcp.types import TextContent

    symbol = arguments.get("symbol", "")
    days = arguments.get("days", 60)
    
    if name == "calculate_macd":
        fast = arguments.get("fast", 12)
        slow = arguments.get("slow", 26)
        signal = arguments.get("signal", 9)
        kline = await _fetch_kline(symbol, days=60)
        pd = _get_pd()
        df = pd.DataFrame(kline)
        if df.empty:
            return [TextContent(type="text", text=json.dumps({"error": f"No K-line data for {symbol}"}))]
        analyzer = StockTrendAnalyzer()
        df = analyzer._calculate_macd(df)
        last = df.iloc[-1]
        return [TextContent(type="text", text=json.dumps({
            "dif": round(float(last.get("dif", 0)), 4),
            "dea": round(float(last.get("dea", 0)), 4),
            "macd": round(float(last.get("macd_bar", 0)), 4),
        }))]

    if name == "calculate_ma":
        periods_str = arguments.get("periods", "5,10,20,60,120,250")
        kline = await _fetch_kline(symbol, days=120)
        pd = _get_pd()
        df = pd.DataFrame(kline)
        if df.empty:
            return [TextContent(type="text", text=json.dumps({"error": f"No K-line data for {symbol}"}))]
        result = _calc_ma(symbol, df, periods=periods_str)
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]

    if name == "get_volume_analysis":
        kline = await _fetch_kline(symbol, days=60)
        pd = _get_pd()
        df = pd.DataFrame(kline)
        if df.empty:
            return [TextContent(type="text", text=json.dumps({"error": f"No K-line data for {symbol}"}))]
        result = _calc_volume(symbol, df)
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]

    if name == "analyze_pattern":
        kline = await _fetch_kline(symbol, days=days)
        pd = _get_pd()
        df = pd.DataFrame(kline)
        if df.empty:
            return [TextContent(type="text", text=json.dumps({"error": f"No K-line data for {symbol}"}))]
        result = _analyze_pattern(symbol, df)
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]

    if name == "analyze_trend":
        kline = await _fetch_kline(symbol, days=days)
        pd = _get_pd()
        df = pd.DataFrame(kline)
        if df.empty or len(df) < 20:
            return [TextContent(type="text", text=json.dumps({"error": f"Insufficient data for {symbol}"}))]
        analyzer = StockTrendAnalyzer()
        result = analyzer.analyze(df, symbol)
        return [TextContent(type="text", text=json.dumps(result.to_dict(), ensure_ascii=False))]

    if name == "list_strategies":
        strat_dir = Path(__file__).resolve().parent / "strategies"
        strategies = []
        for f in sorted(strat_dir.glob("*.yaml")):
            with open(f) as fh:
                d = yaml.safe_load(fh)
            if isinstance(d, dict):
                strategies.append({
                    "id": d.get("name", f.stem),
                    "display_name": d.get("display_name", f.stem),
                    "category": d.get("category"),
                    "description": d.get("description", ""),
                })
        return [TextContent(type="text", text=json.dumps(strategies, ensure_ascii=False))]

    if name == "get_strategy":
        strat_id = arguments.get("strategy_id", "")
        p = Path(__file__).resolve().parent / "strategies" / f"{strat_id}.yaml"
        if not p.exists():
            return [TextContent(type="text", text=json.dumps({"error": f"Strategy '{strat_id}' not found"}))]
        with open(p) as f:
            d = yaml.safe_load(f)
        return [TextContent(type="text", text=json.dumps(d.get("instructions", str(d)), ensure_ascii=False))]

    if name == "list_alert_types":
        alert_dir = Path(__file__).resolve().parent / "alerts"
        rules_path = alert_dir / "rules.yaml"
        if rules_path.exists():
            with open(rules_path) as f:
                rules = yaml.safe_load(f)
            return [TextContent(type="text", text=json.dumps(rules or [], ensure_ascii=False))]
        return [TextContent(type="text", text=json.dumps([]))]

    if name == "check_alert":
        rule_id = arguments.get("rule_id")
        # Parallel data fetch: quote + kline + announcements + fund_flow
        import asyncio
        quote_task = _fetch_quote(symbol)
        kline_task = _fetch_kline(symbol, days=60)
        announcements_task = _fetch_announcements(symbol, days=90)
        fund_flow_task = _fetch_fund_flow(symbol, days=5)
        quote, kline, announcements, fund_flow = await asyncio.gather(
            quote_task, kline_task, announcements_task, fund_flow_task
        )
        pd = _get_pd()
        df = pd.DataFrame(kline)
        from dsa_mcp.alerts.checker import check_alert as _check
        result = _check(symbol, quote, df, announcements=announcements, rule_id=rule_id, fund_flow=fund_flow)
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]

    if name == "get_agent_prompt":
        agent_name = arguments.get("agent_name", "")
        prompt_path = Path(__file__).resolve().parent / "prompts" / f"{agent_name}.md"
        if not prompt_path.exists():
            return [TextContent(type="text", text=json.dumps({"error": f"Agent '{agent_name}' not found"}))]
        with open(prompt_path) as f:
            return [TextContent(type="text", text=f.read())]

    raise ValueError(f"Unknown tool: {name}")


def run():
    """Entry point for uvicorn/mcp-run."""
    import mcp.server.stdio
    from mcp.server.models import InitializationOptions
    from mcp.types import ServerCapabilities, ToolsCapability

    async def _run():
        async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
            await app.run(
                read_stream,
                write_stream,
                InitializationOptions(
                    server_name="dsa-mcp",
                    server_version="0.1.0",
                    # Advertise tool capability so MCP clients (e.g. hermes)
                    # know to query tools/list. With `capabilities={}` they
                    # skip discovery and the server's tools go un-registered.
                    capabilities=ServerCapabilities(tools=ToolsCapability()),
                ),
            )

    asyncio.run(_run())


if __name__ == "__main__":
    run()
