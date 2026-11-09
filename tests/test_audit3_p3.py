"""第三轮审计 P3 回归测试（低危批量）。

覆盖：
L1  exchange.place_order 拒绝 sz<=0 / px<=0
L2  keyform 备份 chmod 600、message 转义（源码断言）
L3  watchdog 无地址时不跳过 24h 失败单检查
L4  admin_ui _rows 读取失败返回 _error 标记（不再显示「暂无记录」）
L5  权益曲线降采样（>2×width 点时抽样）
L6  _admin_required 认 cookie（口径在 app/auth.py；views.py 不自己读 Cookie）
L7  MIN_BARS_REQUIRED = BAND_WINDOW + WARMUP_BARS + 6
L9  _retry_failed_alerts 按 (symbol, kind, bar_ms) 去重
L10 彩排脚本存在且不含 notifier 网络推送
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = Path(__file__).resolve().parent.parent


class TestPlaceOrderValidation(unittest.TestCase):
    def test_zero_size_rejected(self):
        from app.hl.exchange import HlExchange
        ex = HlExchange(account_address="", private_key="", dry_run=True)
        r = ex.place_order("BTC", True, 0.0, 100.0)
        self.assertFalse(r["ok"])
        self.assertIn("正", r["error"])

    def test_negative_price_rejected(self):
        from app.hl.exchange import HlExchange
        ex = HlExchange(account_address="", private_key="", dry_run=True)
        r = ex.place_order("BTC", True, 0.001, -5.0)
        self.assertFalse(r["ok"])

    def test_non_numeric_rejected(self):
        from app.hl.exchange import HlExchange
        ex = HlExchange(account_address="", private_key="", dry_run=True)
        r = ex.place_order("BTC", True, "abc", 100.0)   # type: ignore[arg-type]
        self.assertFalse(r["ok"])


class TestKeyformHardening(unittest.TestCase):
    def test_backup_chmod_and_escape_in_source(self):
        src = (ROOT / "app" / "hl" / "keyform.py").read_text(encoding="utf-8")
        self.assertIn("os.chmod(bak, 0o600)", src)
        self.assertIn("_html_escape(str(message))", src)


class TestWatchdogNoEarlyReturn(unittest.TestCase):
    def test_missing_address_still_checks_stuck_orders(self):
        src = (ROOT / "app" / "hl" / "watchdog.py").read_text(encoding="utf-8")
        # 地址缺失分支后不得再有无条件 return —— 5)/6)/7)/8) 必须照常执行
        i = src.find('add("未配置 HL_ACCOUNT_ADDRESS')
        seg = src[i:i + 600]
        first_block = seg.split("\n\n")[0]
        self.assertNotIn("return notes", first_block,
                         "地址缺失不应提前 return")
        # 且后面的「近 24h 失败单」检查不被该分支包裹
        j = src.find("# 5) 近 24h 失败单")
        self.assertGreater(j, 0)
        five_on = src[j:j + 500]
        self.assertIn("failed", five_on)


class TestAdminRowsError(unittest.TestCase):
    def test_rows_error_marker(self):
        src = (ROOT / "app" / "admin_ui.py").read_text(encoding="utf-8")
        self.assertIn('"_error"', src)


class TestEquitySvgDownsample(unittest.TestCase):
    def test_downsample_in_source(self):
        src = (ROOT / "app" / "hl" / "views.py").read_text(encoding="utf-8")
        self.assertIn("降采样", src)

    def test_downsample_behavior(self):
        # 2026-10-22（feat/pnl-curve）：_equity_svg → _pnl_svg，降采样逻辑不变
        from app.hl.views import _pnl_svg
        pts = [[i * 60.0, 100.0 + (i % 7)] for i in range(5000)]
        svg = _pnl_svg(pts, [], width=760)
        self.assertIn("<svg", svg)
        # 抽样后 polyline 点数应远小于 5000（2×760=1520 上限）
        import re
        # 匹配 polyline（第一个 points= 是 polygon 面积填充，含闭合点）
        m = re.search(r"<polyline points='([^']+)'", svg)
        self.assertTrue(m, f"未找到 polyline points：{svg[:200]}")
        n = m.group(1).count(" ") + 1
        self.assertLessEqual(n, 1520, f"降采样失效：{n} 点")


class TestAdminRequiredCookie(unittest.TestCase):
    """L6 `_admin_required` 认 cookie —— 口径在 ``app/auth.py``，不在 views.py。

    旧断言钉死「views.py 源码里有 ``request.cookies.get("cma_admin")``」：那是实现
    细节，凭据读取搬进统一鉴权模块（app/auth.py）之后它只会误报。这里分两层验：
    1) 结构：views 走 ``authn.is_admin``，自己不再读 Cookie；凭据读取只在 auth.py；
    2) 行为：只带 ``cma_admin`` Cookie（无 ?k=、无 Bearer）时 ``_admin_required`` 放行，
       凭据不对/缺失时仍拒绝。
    """

    def test_credential_reading_lives_in_auth_module(self):
        views = (ROOT / "app" / "hl" / "views.py").read_text(encoding="utf-8")
        self.assertIn("authn.is_admin(", views)
        self.assertNotIn('request.cookies.get("cma_admin")', views,
                         "凭据读取不该在 views.py 里复制一份")
        auth = (ROOT / "app" / "auth.py").read_text(encoding="utf-8")
        self.assertIn('COOKIE_NAME = "cma_admin"', auth)
        self.assertIn("request.cookies.get(COOKIE_NAME)", auth)

    def _deps(self):
        from app.hl.views import DashboardDeps

        class _Cfg:
            admin_token = "cookie-only-tk"

        return DashboardDeps(db=None, config_fn=lambda: _Cfg(), info=None,
                             address_fn=lambda: "", mode_fn=lambda: "off")

    class _Req:
        def __init__(self, cookie=""):
            self.headers = {}
            self.query_params = {}
            self.cookies = {"cma_admin": cookie} if cookie else {}

    def test_cookie_only_is_auth_ok(self):
        import app.hl.views as views

        def need_auth(req):
            return views._admin_required(self._deps(), "", req)   # type: ignore[arg-type]

        os.environ["HL_DASH_REQUIRE_ADMIN"] = "1"
        try:
            self.assertFalse(need_auth(self._Req("cookie-only-tk")),
                             "只有 Cookie 的会话应通过（与 main.hl_guard 同口径）")
            self.assertTrue(need_auth(self._Req("wrong")), "错的 Cookie 不该放行")
            self.assertTrue(need_auth(self._Req()), "没有凭据不该放行")
        finally:
            os.environ.pop("HL_DASH_REQUIRE_ADMIN", None)


class TestMinBars(unittest.TestCase):
    def test_min_bars(self):
        from app import strategy
        self.assertEqual(strategy.MIN_BARS_REQUIRED,
                         strategy.BAND_WINDOW + strategy.WARMUP_BARS + 6)


class TestRetryDedup(unittest.TestCase):
    def test_dedup_in_source(self):
        src = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
        self.assertIn("_seen.add(key)", src)


class TestRehearseScript(unittest.TestCase):
    def test_script_exists_and_isolated(self):
        p = ROOT / "scripts" / "hl_rehearse.py"
        self.assertTrue(p.exists())
        src = p.read_text(encoding="utf-8")
        self.assertIn("StdoutNotifier", src)
        # 不 import 生产 main（notifier 直发源）
        self.assertNotIn("from app.main import", src)
        self.assertIn("tempfile", src)


if __name__ == "__main__":
    unittest.main()
