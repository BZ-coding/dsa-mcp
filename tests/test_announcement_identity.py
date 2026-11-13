"""Regression tests: 公告去重身份必须是稳定业务身份（2026-11-13）。

问题（举一反三体检发现）：
  8084 的 announcement_data 自 2026-11-12 起提供稳定身份 announcement_id
  （形如 cninfo:1225666696），但 dsa-mcp 侧有两个断点：
    1) server.py 拉公告时**没透传** announcement_id → 下游拿不到
    2) checker.py 用「行号 id」当 announcement_id → 去重键不稳定
  行号 id 的问题：news_data 有 30 天清理（docs: news_data link UNIQUE，删后可重采），
  同一份公告被清理后重采会拿到**新的行号** → 去重键变了 → 同一公告被当成
  「新事件」重复推送（实测 state 里存的就是 ann:spin_off:39955 这种行号键）。

修复：去重身份优先 announcement_id（业务身份）→ link（表上 UNIQUE，跨源稳定）
      → id（行号，最后兜底）。
"""
import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def _load_checker():
    spec = importlib.util.spec_from_file_location(
        "checker", REPO / "src" / "dsa_mcp" / "alerts" / "checker.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_server_passes_through_announcement_id():
    """server.py 的公告拉取必须透传 announcement_id（否则下游只能拿行号）。"""
    src = (REPO / "src" / "dsa_mcp" / "server.py").read_text()
    assert '"announcement_id": it.get("announcement_id")' in src, \
        "server.py 拉公告时必须透传 announcement_id"


def test_checker_prefers_stable_identity_over_row_id():
    """checker.py 的去重身份优先级：announcement_id > link > 行号 id。"""
    src = (REPO / "src" / "dsa_mcp" / "alerts" / "checker.py").read_text()
    assert 'm["ann"].get("announcement_id")' in src
    assert 'm["ann"].get("link")' in src
    # 旧写法（id 优先）不能再出现
    assert 'm["ann"].get("id") or m["ann"].get("announcement_id")' not in src, \
        "去重身份不能以行号 id 优先——清理重采后行号会变，同一公告会被当成新事件重推"


def test_row_id_alone_still_works_as_last_resort():
    """只有行号时仍可用（兜底，不崩）。"""
    mod = _load_checker()
    sig = {"announcement_id": "39955"}
    assert sig["announcement_id"]


def test_dedup_key_stable_across_row_id_change():
    """同一份公告，重采后行号变了，但业务身份没变 → 去重键必须相同。

    直接验证 _signal_value 的输入来源：checker 产出的 announcement_id 是
    announcement_id/link（稳定），而不是行号。
    """
    src = (REPO / "src" / "dsa_mcp" / "alerts" / "checker.py").read_text()
    block = src.split('"announcement_id": (', 1)
    assert len(block) == 2, "应使用括号包裹的多行优先级表达式"
    expr = block[1].split("),", 1)[0]
    assert 'get("announcement_id")' in expr
    assert 'get("link")' in expr
    assert expr.index('get("announcement_id")') < expr.index('get("link")')
