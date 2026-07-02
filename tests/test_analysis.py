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
        assert len(rules) == 11
