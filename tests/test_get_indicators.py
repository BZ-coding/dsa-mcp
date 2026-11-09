#!/usr/bin/env python3
"""get_indicators MCP 工具测试（步骤5）

覆盖：
  · 工具注册在 list_tools 里
  · 正常返回结构完整，rsi_method == "wilder"
  · 非法 symbol → error（不是抛异常）
  · 无数据 → error
  · days 越界被夹紧到 [20, 250]
  · **不枚举持仓约束**：工具 schema 只接受单个 symbol，
    且不存在任何"列出全部监控标的"的入口（防工具列表变成持仓枚举器）
"""
import asyncio
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


import dsa_mcp.server as _srv_mod  # noqa: E402

# ⚠️ 既有测试污染：`test_analysis.py::test_semantic_announcement_hk_fallback`
#    monkeypatch 了 `dsa_mcp.server._get_client` 却**从不恢复**（既有缺陷，本次不动它），
#    导致其后运行的任何测试都拿到 FakeClient → `_fetch_kline` 永远返回空。
#    这里在**导入时**（早于任何测试执行）抓住真实实现，每次调用前恢复。
_REAL_GET_CLIENT = _srv_mod._get_client

_LOOP = None


def run(coro):
    """⚠️ 必须复用同一个 event loop。

    `dsa_mcp.server._get_client()` 缓存了一个 httpx.AsyncClient，它绑定在
    创建时的 loop 上。若每次 `asyncio.run()` 新建 loop，第二次调用就会
    `Event loop is closed` —— 这是**测试脚手架**问题，生产 MCP 只有一个 loop。
    """
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
        # 同时清掉 server 里缓存的 httpx client：它绑定在**旧** loop 上，
        # 换 loop 后继续用它就会 "Event loop is closed"。
        _srv_mod._http_client = None
        # 还原被其它测试 monkeypatch 掉的 _get_client（见文件头说明）
        _srv_mod._get_client = _REAL_GET_CLIENT
    return _LOOP.run_until_complete(coro)


def test_tool_registered():
    from dsa_mcp.server import list_tools
    tools = run(list_tools())
    names = [t.name for t in tools]
    assert "get_indicators" in names, names
    t = next(x for x in tools if x.name == "get_indicators")
    assert "symbol" in t.inputSchema["properties"]
    assert t.inputSchema["required"] == ["symbol"]
    # schema 里不应出现任何"批量/全部"语义的参数
    for bad in ("symbols", "all", "watchlist", "portfolio", "list_all"):
        assert bad not in t.inputSchema["properties"], f"不应有 {bad} 参数"


def _8084_reachable() -> bool:
    """test_ok / test_days_clamped 需要真实 8084 /api/v1/history（CI 上没有）。

    模仿本仓既有模式（alert_daemon / daily_recap 的 "not deployed → skip"）：
    服务不可达就 skip，让 CI 只跑纯逻辑部分（schema / 校验 / 错误分支）。
    """
    import os
    import socket
    from urllib.parse import urlparse
    base = os.environ.get("FDS_BASE_URL", "http://localhost:8084")
    u = urlparse(base)
    try:
        with socket.create_connection((u.hostname, u.port or 80), timeout=2):
            return True
    except OSError:
        return False


_NO_8084 = pytest.mark.skipif(not _8084_reachable(), reason="8084 (FDS_BASE_URL) not reachable — data-dependent tests run only where the service exists (local/dev)")


@_NO_8084
def test_ok():
    from dsa_mcp.server import call_tool
    r = run(call_tool("get_indicators", {"symbol": "002202"}))
    d = json.loads(r[0].text)
    assert "error" not in d, d
    for k in ("symbol", "data_as_of", "bars", "close", "ma", "macd", "rsi",
              "rsi_method", "levels"):
        assert k in d, f"缺字段 {k}"
    assert d["rsi_method"] == "wilder"
    assert d["bars"] >= 20
    for k in ("ma5", "ma20", "bias_ma5_pct"):
        assert k in d["ma"]
    for k in ("dif", "dea", "bar", "death_cross", "golden_cross"):
        assert k in d["macd"]
    for k in ("rsi6", "rsi12", "rsi24"):
        assert k in d["rsi"]
    for k in ("high_20d", "low_20d", "at_20d_high", "at_20d_low"):
        assert k in d["levels"]


@pytest.mark.parametrize("bad", ["", "中文代码", "002202;DROP", "../../etc/passwd",
                                 "a" * 17, "002202/../x", "00 2202"])
def test_bad_symbol_returns_error(bad):
    from dsa_mcp.server import call_tool
    r = run(call_tool("get_indicators", {"symbol": bad}))
    d = json.loads(r[0].text)
    assert "error" in d, d


def test_no_data_returns_error():
    from dsa_mcp.server import call_tool
    r = run(call_tool("get_indicators", {"symbol": "999999ZZZ"}))
    d = json.loads(r[0].text)
    assert "error" in d, d


@_NO_8084
def test_days_clamped():
    from dsa_mcp.server import call_tool
    a = json.loads(run(call_tool("get_indicators", {"symbol": "002202", "days": 9999}))[0].text)
    b = json.loads(run(call_tool("get_indicators", {"symbol": "002202", "days": 250}))[0].text)
    assert a["bars"] == b["bars"], "days 越界应被夹紧到 250"
    c = json.loads(run(call_tool("get_indicators", {"symbol": "002202", "days": 1}))[0].text)
    assert c.get("bars", 0) >= 1


def test_no_portfolio_enumeration_tool():
    """持仓不暴露纪律：工具列表里不得有任何能枚举监控标的的入口。"""
    from dsa_mcp.server import list_tools
    names = {t.name for t in run(list_tools())}
    for forbidden in ("list_symbols", "get_watchlist", "list_portfolio",
                      "get_positions", "list_subscriptions", "get_all_symbols"):
        assert forbidden not in names, f"出现持仓枚举入口: {forbidden}"
