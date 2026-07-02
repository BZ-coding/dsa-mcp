"""
dsa-mcp MCP Server — analysis tools ported from daily_stock_analysis (52k★)

Iron rule (zsd 2026-07-02): ALL data comes from 8084 MCP.
No direct database dSA DB imports.

Port: 8087 (stdio-based, managed by systemd)
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

_FDS_BASE = os.environ.get("FDS_MCP_URL", "http://localhost:8086")
_HTTP_TIMEOUT = 15.0
_http_client: httpx.AsyncClient | None = None


async def _get_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(timeout=_HTTP_TIMEOUT)
    return _http_client


async def _call_8084(tool_name: str, **params) -> dict:
    """Call 8084 MCP tool (JSON-RPC over HTTP)."""
    client = await _get_client()
    payload = {
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": {k: v for k, v in params.items() if v is not None}},
        "id": 1,
    }
    try:
        resp = await client.post(f"{_FDS_BASE}/api/v1/mcp", json=payload)
        resp.raise_for_status()
        data = resp.json()
        if "result" in data:
            return data["result"].get("content", [{}])[0].get("text", {})
        return {"error": data.get("error", {}).get("message", "unknown error")}
    except Exception as e:
        logger.warning(f"_call_8084({tool_name}) failed: {e}")
        return {"error": str(e)}


async def _fetch_kline(symbol: str, days: int = 60) -> list[dict]:
    """Fetch OHLCV from 8084 MCP."""
    result = await _call_8084("get_kline", symbol=symbol, days=days)
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except (json.JSONDecodeError, TypeError):
            return []
    if isinstance(result, dict) and "data" in result:
        return result["data"]
    if isinstance(result, list):
        return result
    return []


async def _fetch_quote(symbol: str) -> dict:
    """Fetch realtime quote from 8084 MCP."""
    result = await _call_8084("get_quote", symbol=symbol)
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except (json.JSONDecodeError, TypeError):
            return {}
    return result if isinstance(result, dict) else {}


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
    from mcp.types import Tool, ToolInputSchema
    tools = [
        Tool(
            name="calculate_macd",
            description="Calculate MACD indicator from K-line data. Fetches 60 days K-line from 8084 internally.",
            inputSchema=ToolInputSchema(
                type="object",
                properties={
                    "symbol": {"type": "string", "description": "Stock code, e.g. '600519'"},
                    "fast": {"type": "integer", "description": "Fast EMA period (default: 12)"},
                    "slow": {"type": "integer", "description": "Slow EMA period (default: 26)"},
                    "signal": {"type": "integer", "description": "Signal period (default: 9)"},
                },
                required=["symbol"],
            ),
        ),
        Tool(
            name="calculate_ma",
            description="Calculate moving averages (MA5/10/20/30/60/120/250 or custom periods) for a stock. Fetches K-line from 8084.",
            inputSchema=ToolInputSchema(
                type="object",
                properties={
                    "symbol": {"type": "string", "description": "Stock code"},
                    "periods": {"type": "string", "description": "Comma-separated periods (default: '5,10,20,60')"},
                },
                required=["symbol"],
            ),
        ),
        Tool(
            name="get_volume_analysis",
            description="Analyse volume-price relationship. Fetches K-line from 8084.",
            inputSchema=ToolInputSchema(
                type="object",
                properties={
                    "symbol": {"type": "string", "description": "Stock code"},
                },
                required=["symbol"],
            ),
        ),
        Tool(
            name="analyze_pattern",
            description="Detect candlestick patterns (Doji, Hammer, Star, Engulfing, Double Bottom, breakout). Fetches K-line from 8084.",
            inputSchema=ToolInputSchema(
                type="object",
                properties={
                    "symbol": {"type": "string", "description": "Stock code"},
                    "days": {"type": "integer", "description": "Lookback days (default: 60)"},
                },
                required=["symbol"],
            ),
        ),
        Tool(
            name="analyze_trend",
            description="Comprehensive technical trend analysis. Returns MACD, RSI, MA alignment, support/resistance, buy/sell signal.",
            inputSchema=ToolInputSchema(
                type="object",
                properties={
                    "symbol": {"type": "string", "description": "Stock code"},
                    "days": {"type": "integer", "description": "Lookback days (default: 60)"},
                },
                required=["symbol"],
            ),
        ),
        Tool(
            name="list_strategies",
            description="List all 15 trading strategies from dSA. Returns name + display_name + category for each.",
            inputSchema=ToolInputSchema(
                type="object",
                properties={},
            ),
        ),
        Tool(
            name="get_strategy",
            description="Get the full instructions text of a trading strategy by ID.",
            inputSchema=ToolInputSchema(
                type="object",
                properties={
                    "strategy_id": {"type": "string", "description": "Strategy ID (e.g. 'ma_golden_cross')"},
                },
                required=["strategy_id"],
            ),
        ),
        Tool(
            name="list_alert_types",
            description="List all available alert/rule types. Each has name, severity, description.",
            inputSchema=ToolInputSchema(
                type="object",
                properties={},
            ),
        ),
        Tool(
            name="check_alert",
            description="Check alerts for a symbol. Pure signal, no push. Data from 8084 MCP.",
            inputSchema=ToolInputSchema(
                type="object",
                properties={
                    "symbol": {"type": "string", "description": "Stock code to check"},
                    "rule_id": {"type": "string", "description": "Optional, check only this rule. None = all."},
                },
                required=["symbol"],
            ),
        ),
        Tool(
            name="get_agent_prompt",
            description="Get an agent prompt template (port from dSA). Agent names: technical, intel, risk, portfolio, decision, decision_chat.",
            inputSchema=ToolInputSchema(
                type="object",
                properties={
                    "agent_name": {"type": "string", "description": "Agent name"},
                },
                required=["agent_name"],
            ),
        ),
    ]
    return tools


# ── Tool implementations ──

@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list:
    from mcp.types import TextContent, ToolResult

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
        quote = await _fetch_quote(symbol)
        kline = await _fetch_kline(symbol, days=60)
        pd = _get_pd()
        df = pd.DataFrame(kline)
        from dsa_mcp.alerts.checker import check_alert as _check
        result = _check(symbol, quote, df, rule_id=rule_id)
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

    async def _run():
        async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
            await app.run(
                read_stream,
                write_stream,
                InitializationOptions(
                    server_name="dsa-mcp",
                    server_version="0.1.0",
                    capabilities={},
                ),
            )

    asyncio.run(_run())


if __name__ == "__main__":
    run()
