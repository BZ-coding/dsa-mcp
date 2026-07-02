"""
Tests for dsa-mcp ported algorithms.
"""
import pytest
import numpy as np
import pandas as pd


@pytest.fixture
def sample_df():
    np.random.seed(42)
    dates = pd.date_range(start="2025-01-01", periods=60, freq="D")
    prices = [10.0]
    for _ in range(59):
        prices.append(prices[-1] * (1 + np.random.randn() * 0.02 + 0.003))
    return pd.DataFrame({
        "date": dates,
        "open": prices,
        "high": [p * 1.01 for p in prices],
        "low": [p * 0.99 for p in prices],
        "close": prices,
        "volume": [np.random.randint(1000000, 5000000) for _ in prices],
    })


class TestTrendAnalyzer:
    def test_import(self):
        from dsa_mcp.analysis.trend import StockTrendAnalyzer
        assert StockTrendAnalyzer is not None

    def test_analyze(self, sample_df):
        from dsa_mcp.analysis.trend import StockTrendAnalyzer
        analyzer = StockTrendAnalyzer()
        result = analyzer.analyze(sample_df, "000001")
        assert result is not None
        assert result.code == "000001"
        assert result.current_price > 0
        assert 0 <= result.signal_score <= 100
        assert result.trend_status is not None
        assert result.ma_alignment is not None

    def test_to_dict(self, sample_df):
        from dsa_mcp.analysis.trend import StockTrendAnalyzer
        result = StockTrendAnalyzer().analyze(sample_df, "000001")
        d = result.to_dict()
        assert isinstance(d, dict)
        assert "code" in d
        assert "current_price" in d


class TestMA:
    def test_calculate(self, sample_df):
        from dsa_mcp.analysis.ma import calculate_ma
        result = calculate_ma("000001", sample_df, "5,10,20,60")
        assert result["code"] == "000001"
        assert "ma5" in result["ma"]
        assert "ma20" in result["ma"]
        assert result["ma"]["ma5"]["value"] > 0
        assert "混合" in result["ma_alignment"]

    def test_empty_df(self):
        from dsa_mcp.analysis.ma import calculate_ma
        result = calculate_ma("000001", pd.DataFrame())
        assert "error" in result


class TestVolume:
    def test_analyze(self, sample_df):
        from dsa_mcp.analysis.volume import get_volume_analysis
        result = get_volume_analysis("000001", sample_df)
        assert result["code"] == "000001"
        assert result["period_days"] == 60
        assert result["volume_ratio_vs_5d"] > 0


class TestPattern:
    def test_analyze(self, sample_df):
        from dsa_mcp.analysis.pattern import analyze_pattern
        result = analyze_pattern("000001", sample_df)
        assert result["code"] == "000001"
        assert isinstance(result["patterns"], list)


class TestRegistry:
    def test_tool_definition(self):
        from dsa_mcp.registry import ToolDefinition, ToolParameter, ToolRegistry
        assert ToolDefinition is not None
        registry = ToolRegistry()
        assert len(registry) == 0


class TestAlerts:
    def test_checker(self, sample_df):
        from dsa_mcp.alerts.checker import check_alert
        result = check_alert("000001", {"price": 11.0}, sample_df)
        assert "symbol" in result
        assert "triggered" in result
        assert "signals" in result

    def test_rules_yaml(self):
        import yaml
        from pathlib import Path
        rules_path = Path(__file__).resolve().parent.parent / "src" / "dsa_mcp" / "alerts" / "rules.yaml"
        assert rules_path.exists()
        with open(rules_path) as f:
            rules = yaml.safe_load(f)
        assert isinstance(rules, list)
        assert len(rules) >= 12  # Phase 5b added major_event
        # 至少 5 条语义规则 (announcement_title)
        ann_rules = [
            r for r in rules
            if any(c.get("field") == "announcement_title" for c in r.get("conditions", []))
        ]
        assert len(ann_rules) >= 5

    def test_semantic_alert_announcement_match(self):
        """Phase 5b: checker.py 关键词匹配 announcement 标题"""
        from dsa_mcp.alerts.checker import check_alert
        anns = [
            {"title": "关于公司大股东减持股份的公告", "announcement_time": "2026-07-01", "link": "http://x"},
            {"title": "2025年年度报告", "announcement_time": "2026-07-02", "link": "http://y"},
        ]
        r = check_alert("000001", {}, None, announcements=anns)
        assert r["triggered"]
        ids = [s["rule_id"] for s in r["signals"]]
        assert "insider_reduction" in ids
        assert "major_event" not in ids  # 普通年报不算 major_event
        # 验证 signal 包含 link + source
        sig = next(s for s in r["signals"] if s["rule_id"] == "insider_reduction")
        assert sig["source"] == "announcement"
        assert sig["link"] == "http://x"

    def test_semantic_alert_multiple_keywords(self):
        """any_of 关键词列表匹配"""
        from dsa_mcp.alerts.checker import check_alert
        anns = [{"title": "公司被证监会立案调查", "announcement_time": "2026-07-02", "link": "http://z"}]
        r = check_alert("000001", {}, None, announcements=anns)
        assert r["triggered"]
        ids = [s["rule_id"] for s in r["signals"]]
        assert "regulatory_penalty" in ids

    def test_semantic_multiple_announcements_unique_ids(self):
        """Phase 5c: 同一 rule 命中多条公告 → 每条 announcement_id 独立 signal"""
        from dsa_mcp.alerts.checker import check_alert
        # keywords: 重大资产重组 / 收购 / 合并 / 股份回购 / 股权激励 / 股东大会决议 / 停牌 / 复牌 / 担保
        anns = [
            {"id": 100, "title": "关于为子公司提供担保的公告", "announcement_time": "2026-06-30", "link": "http://a"},
            {"id": 101, "title": "关于公司股份回购的公告", "announcement_time": "2026-06-29", "link": "http://b"},
            {"id": 102, "title": "关于召开股东大会决议的通知", "announcement_time": "2026-06-28", "link": "http://c"},
        ]
        r = check_alert("000001", {}, None, announcements=anns)
        assert r["triggered"]
        major_sigs = [s for s in r["signals"] if s["rule_id"] == "major_event"]
        # 3 条公告 → 3 条 major_event signals
        assert len(major_sigs) == 3, f"expected 3, got {len(major_sigs)}"
        ann_ids = {s.get("announcement_id") for s in major_sigs}
        assert ann_ids == {100, 101, 102}, f"ann_ids mismatch: {ann_ids}"

    def test_semantic_announcement_hk_fallback(self):
        """Phase 5c: 港股用 stock_news fallback (akshare 无 hk announcement)"""
        import asyncio
        from dsa_mcp.server import _fetch_announcements
        # 不真跑 HTTP, 仅验证 is_hk 分支路径
        # 通过 monkey-patch httpx 拦截
        import dsa_mcp.server as srv
        called = {"primary": None, "fallback": None}
        class FakeResp:
            def __init__(self, items):
                self._items = items
            def raise_for_status(self): pass
            def json(self):
                return {"items": self._items}
        class FakeClient:
            async def get(self, url, params=None):
                dt = (params or {}).get("data_type", "")
                called["primary" if called["primary"] is None else "fallback"] = dt
                if dt == "stock_news":
                    return FakeResp([
                        {"id": 999, "title": "美团回购公告", "published": "2026-07-01 10:00:00", "link": "http://hk"},
                    ])
                return FakeResp([])
            async def aclose(self): pass
        async def fake_get_client():
            return FakeClient()
        srv._http_client = None
        srv._get_client = fake_get_client
        items = asyncio.run(_fetch_announcements("hk03690", days=30))
        assert called["primary"] == "stock_news", f"expected stock_news primary for hk, got {called}"
        assert len(items) == 1
        assert items[0]["id"] == 999
        assert items[0]["title"] == "美团回购公告"
        assert items[0]["source"] == "stock_news"
        srv._http_client = None

    def test_alert_daemon_dedup_dict_nested(self):
        """Phase 5c: alert_daemon dedup state 字典嵌套 + 公告多 unique id 保留"""
        import json
        import sys
        from pathlib import Path
        daemon_path = Path("/home/zsd/.hermes/scripts/alert_daemon.py")
        if not daemon_path.exists():
            pytest.skip("alert_daemon not deployed (expected at ~/.hermes/scripts/)")
        # 模拟 push 流程
        spec = __import__("importlib.util").util.spec_from_file_location("ad", daemon_path)
        mod = __import__("importlib.util").util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        # _signal_value 测试
        sig_ann1 = {"rule_id": "major_event", "announcement_id": "100", "reason": "担保"}
        sig_ann2 = {"rule_id": "major_event", "announcement_id": "101", "reason": "回购"}
        v1 = mod._signal_value(sig_ann1)
        v2 = mod._signal_value(sig_ann2)
        assert v1 != v2, "不同 announcement_id 应有不同 value"
        assert v1.startswith("ann:major_event:")
        # 价量
        sig_tech = {"rule_id": "ma5_below_ma20", "reason": "MA5(67) < MA20(71)"}
        v3 = mod._signal_value(sig_tech)
        assert not v3.startswith("ann:"), "价量 rule 不应该用 ann: prefix"

    def test_daily_recap_load_today_signals(self):
        """Phase B: daily_recap 从 state 抽今日 signals"""
        from pathlib import Path
        recap_path = Path("/home/zsd/.hermes/scripts/daily_recap.py")
        if not recap_path.exists():
            pytest.skip("daily_recap.py not deployed")
        ns = {"__name__": "recap", "__file__": str(recap_path)}
        exec(recap_path.read_text(encoding="utf-8"), ns)
        # 模拟 state
        fake_state = {
            "002202": {
                "major_event": {
                    "ann:major_event:100": {"value": "ann:major_event:100", "severity": "medium", "pushed_at": "2026-07-02T10:30:00"},
                    "ann:major_event:101": {"value": "ann:major_event:101", "severity": "medium", "pushed_at": "2026-07-02T10:31:00"},
                }
            },
            "hk03690": {
                "ma5_below_ma20": {
                    "ma5_below_ma20|MA5(67.95)<MA20(71.09)": {
                        "value": "ma5_below_ma20|MA5(67.95)<MA20(71.09)",
                        "severity": "medium",
                        "pushed_at": "2026-07-02T10:32:00",
                    }
                }
            },
            "hk09988": {
                "ma5_below_ma20": {
                    "ma5_below_ma20|MA5(92)<MA20(101)": {
                        "value": "ma5_below_ma20|MA5(92)<MA20(101)",
                        "severity": "medium",
                        "pushed_at": "2026-07-01T10:32:00",  # 昨日, 不计入
                    }
                }
            },
        }
        sigs = ns["load_today_signals"](fake_state, "2026-07-02")
        assert len(sigs) == 3, f"expected 3, got {len(sigs)}"
        syms = [s["sym"] for s in sigs]
        assert "002202" in syms
        assert "hk03690" in syms
        assert "hk09988" not in syms, "昨日推送不应计入今日"

    def test_daily_recap_format_signals_table(self):
        """Phase B: format_signals_table 输出 md 表格"""
        from pathlib import Path
        recap_path = Path("/home/zsd/.hermes/scripts/daily_recap.py")
        if not recap_path.exists():
            pytest.skip("daily_recap.py not deployed")
        ns = {"__name__": "recap", "__file__": str(recap_path)}
        exec(recap_path.read_text(encoding="utf-8"), ns)
        sigs = [
            {"sym": "002202", "rule_id": "major_event", "severity": "medium",
             "value": "ann:major_event:100", "pushed_at": "2026-07-02T10:30:00"},
            {"sym": "hk03690", "rule_id": "ma5_below_ma20", "severity": "medium",
             "value": "ma5_below_ma20|MA5(67.95)<MA20(71.09)", "pushed_at": "2026-07-02T10:32:00"},
        ]
        md = ns["format_signals_table"](sigs)
        assert "| 标的 |" in md
        assert "| 规则 |" in md
        assert "002202" in md
        assert "major_event" in md
        assert "公告 #100" in md, "announcement_id 应显示为 '公告 #100'"
        assert "MA5(67.95)<MA20(71.09)" in md

    def test_daily_recap_format_signals_table_empty(self):
        """Phase B: 空 signals 输出占位文本"""
        from pathlib import Path
        recap_path = Path("/home/zsd/.hermes/scripts/daily_recap.py")
        if not recap_path.exists():
            pytest.skip("daily_recap.py not deployed")
        ns = {"__name__": "recap", "__file__": str(recap_path)}
        exec(recap_path.read_text(encoding="utf-8"), ns)
        md = ns["format_signals_table"]([])
        assert "当日无 alert" in md or "无推送" in md
