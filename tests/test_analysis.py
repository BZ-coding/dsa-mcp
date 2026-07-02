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
