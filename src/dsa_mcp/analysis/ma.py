# -*- coding: utf-8 -*-
"""MA calculator —— **薄 re-export**（2026-10-31 迁移到共享库 fin-indicators）

函数实体已迁到 `fin_indicators.ma`（纯函数、无 IO 的中性指标库）。
本模块只做 re-export，保持 dsa-mcp 内部 import 路径与**公开 MCP 工具契约**
（`calculate_ma` 的返回 dict）不变。

⚠️ 不要在这里加业务逻辑 —— 共享库的准入规则见 fin-indicators/README.md。
"""
from fin_indicators.ma import calculate_ma  # noqa: F401

__all__ = ["calculate_ma"]
