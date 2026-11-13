"""第七轮审核（2026-11-13）面板 / 认证 / 配置同源修复的回归测试。

覆盖（每条都对应报告里的一条发现，防止「改回去」）：

A. 写操作防护（第七轮 A4 / #1）
   - Cookie 会话 + 同源 Origin + CSRF → 放行；缺 CSRF → 403；
   - 同站兄弟源 / 跨站源 → 403（**且 DB 未被改动**）；
   - 缺 Origin/Referer 的 Cookie 会话 → **仍要 CSRF**（缺来源头 ≠ 可信客户端）；
   - 跨源一律 403，**有效 Bearer / 显式 k 也不例外**（来源与凭据是两件事）；
   - 错误 Bearer → 403 且**不降级**到同请求里的 k / Cookie 会话；
   - 表单里的 ``k``（显式凭据）在没有来源头时照常可用（runbook curl）；
   - 设置页三个写表单都带同一个 csrf 隐藏域，且 HTML 里没有 ADMIN_TOKEN 真值。
B. ``on`` 显式白名单（#7）：缺失/未知值 → 400 且不改状态（不再「漏写=解除」）。
C. 响应头（#1 附带）：``no-store`` + ``X-Frame-Options: DENY`` + CSP frame-ancestors。
D. API 卡三态（B2 + 信号 F3）：missing / configured / verified / read_failed；
   key/secret 成对同源（DB 优先），半对不混搭，响应里不出现凭据明文。
E. MR 行分层去重（B1）：只在 MR **已认领**时移除普通 QQQ 行；未认领仍可见且标异常；
   MR 停用时从 DB 台账只读展示 OPEN 仓并提示「不会自动离场」。
F. 无止损仓位的 R / 止损距离给「—」（#5）；不可达时持仓徽章「无法确认」（B3）。
G. 快照年龄与实际来源一致（#6）：只有真的用了快照才报年龄。
H. ``_sz_tick`` 不外呼 specs（#8）：快照带规格 / 只读缓存；capture 时把规格随快照落库。
I. 已实现亏损熔断解除复核（B7）：仍越阈值 → 拒绝并解释；不重置 30 天窗口。
J. 共享止损判据（stops.py）：非有限数不算保护；容差严格小于一个 tick（半格），
   完整差一个 tick 是真实裸量、不算覆盖。
   MR 运行时状态（action_required / order_gate_blocked / dry_run）必须真的渲染出来。

约定：**不触网**（KrInfo 被假件顶掉）、临时 sqlite 库、不写生产、不读真实 token。
本文件不 import app.main 于模块顶层（它 import 就会在默认 ``/app/data`` 建库）。
"""
from __future__ import annotations

import copy
import importlib
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TOKEN = "review7-test-token-not-real"       # 与任何部署环境无关的假值
NOW = 1_760_000_000.0
ADDR = "kraken"

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
except ImportError:                          # pragma: no cover - CI 必装
    FastAPI = None
    TestClient = None


# ---------------------------------------------------------------- 假交易所客户端
def base_snapshot(positions=(), orders=(), equity=1000.0, ts=NOW):
    """与 KrInfo.snapshot() 同口径的最小快照（含 orders_trusted）。"""
    return {"ts": ts, "address": ADDR, "equity_total": equity, "equity": equity,
            "spot_usdc": equity, "spot_hold": 0.0, "unified": True, "spot_ok": True,
            "withdrawable": equity, "margin_used": 10.0, "notional": 100.0,
            "positions": list(positions), "open_orders": list(orders),
            "orders_trusted": True}


def btc_pos(size=0.05, entry=100_000.0, mark=101_000.0):
    return {"coin": "BTC", "side": 1, "size": size, "entry_px": entry, "mark_px": mark,
            "position_value": size * mark, "liq_px": 90_000.0, "leverage": None,
            "isolated": False, "margin_used": 50.0, "unrealized_pnl": 50.0,
            "funding_since_open": 0.0}


def qqq_pos(size=1.0, entry=610.0, mark=620.0):
    return {"coin": "QQQ", "side": 1, "size": size, "entry_px": entry, "mark_px": mark,
            "position_value": size * mark, "liq_px": 0.0, "leverage": None,
            "isolated": False, "margin_used": 62.0, "unrealized_pnl": 10.0,
            "funding_since_open": 0.0}


def stop_order(coin="BTC", sz=0.05, trigger=97_000.0, is_buy=False):
    return {"coin": coin, "oid": "o-1", "is_buy": is_buy, "sz": sz, "limit_px": 0.0,
            "reduce_only": True, "trigger_px": trigger, "is_trigger": True,
            "order_type": "stop_loss", "timestamp": int(NOW * 1000)}


class FakeKrInfo:
    """KrInfo 替身：固定快照 + 规格；``raise_`` 模拟私有读失败。"""

    sz_decimals = 4

    def __init__(self, *a, **kw):
        self.snap = base_snapshot([btc_pos()], [stop_order()])
        self.raise_ = False
        self.spec_calls = 0

    def snapshot(self, address, mids=None):
        if self.raise_:
            raise RuntimeError("auth failed (fake)")
        return copy.deepcopy(self.snap)

    def all_mids(self):
        if self.raise_:
            raise RuntimeError("allMids failed (fake)")
        return {"BTC": 100_000.0, "QQQ": 620.0}

    def specs(self, coins=None):
        self.spec_calls += 1
        return {c: {"sz_decimals": self.sz_decimals, "szDecimals": self.sz_decimals}
                for c in (coins or [])}


def bearer(value=TOKEN):
    return {"Authorization": "Bearer " + value}


# ---------------------------------------------------------------- 全 app（真实路由）
class FullAppCase(unittest.TestCase):
    """真实 ``app.main`` 组装（含 admin_ui + /api/hl/* + 两个 curl 口径路由）。

    类级一份：DB 用临时文件，KrInfo 顶成假件（**绝不触网**），调度器关闭。
    用例之间共享同一个库，所以 ``setUp`` 显式重置用到的开关/凭据。
    """

    @classmethod
    def setUpClass(cls):
        if TestClient is None:
            raise unittest.SkipTest("fastapi 未安装")
        cls.tmp = tempfile.mkdtemp(prefix="review7_")
        cls._env = {k: os.environ.get(k) for k in (
            "DB_PATH", "ENABLE_SCHEDULER", "ADMIN_TOKEN", "TOKEN_SECRET", "BASE_URL",
            "DRY_RUN", "HL_ACCOUNT_ADDRESS", "HL_EXEC_MODE", "PANEL_SNAPSHOT",
            "KRAKEN_API_KEY", "KRAKEN_API_SECRET", "HL_DASH_REQUIRE_ADMIN")}
        os.environ.update({
            "DB_PATH": os.path.join(cls.tmp, "r7.db"), "ENABLE_SCHEDULER": "false",
            "ADMIN_TOKEN": TOKEN, "TOKEN_SECRET": "review7-secret",
            "BASE_URL": "https://review7.example", "DRY_RUN": "true",
            "HL_ACCOUNT_ADDRESS": ADDR, "HL_EXEC_MODE": "off", "PANEL_SNAPSHOT": "0",
        })
        os.environ.pop("HL_DASH_REQUIRE_ADMIN", None)
        import app.hl.info as _hi
        import app.kr.info as _kri
        cls._real_infos = (_hi.HlInfo, _kri.KrInfo)
        _hi.HlInfo = FakeKrInfo
        _kri.KrInfo = FakeKrInfo
        importlib.reload(importlib.import_module("app.config"))
        cls.mod = importlib.reload(importlib.import_module("app.main"))
        cls.app = cls.mod.app
        cls.client = TestClient(cls.app)

    @classmethod
    def tearDownClass(cls):
        import app.hl.info as _hi
        import app.kr.info as _kri
        _hi.HlInfo, _kri.KrInfo = cls._real_infos
        for k, v in cls._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # ---- 每个用例的干净起点 ----
    def setUp(self):
        db = self.mod.db
        # 共享一个库，所以把「会累积」的表清干净（成交/快照会跨用例串味）
        db.conn.execute("DELETE FROM hl_fills")
        db.conn.execute("DELETE FROM panel_snapshots")
        db.conn.commit()
        db.hl_set_kill(False, "", origin="manual")
        db.set_config("mr_enabled", "0")
        db.set_config("kr_api_key", "")
        db.set_config("kr_api_secret", "")
        db.set_config("hl_exec_mode", "off")
        os.environ.pop("KRAKEN_API_KEY", None)
        os.environ.pop("KRAKEN_API_SECRET", None)
        self.mod._CRED_PROBE = {}
        self.mod._hl_info = None
        self.mod._panel_info = None
        self.client.cookies.clear()

    # ---- 辅助 ----
    @property
    def csrf(self):
        return self.mod.authn.csrf_token(TOKEN)

    def login(self):
        """只用 ?k= 第一跳拿 Cookie 会话（与浏览器一致）。"""
        r = self.client.get("/admin", params={"k": TOKEN})
        self.assertEqual(r.status_code, 200)
        self.assertIn("cma_admin", r.headers.get("set-cookie", ""))
        return self.client

    def settings_html(self):
        r = self.client.get("/admin?tab=settings", headers=bearer())
        self.assertEqual(r.status_code, 200)
        return r.text


# ---------------------------------------------------------------- A. 写操作防护
class TestCookieWriteGuard(FullAppCase):
    def test_same_origin_with_csrf_allowed(self):
        self.login()
        r = self.client.post("/admin/switch/kill", data={"on": "1", "csrf": self.csrf},
                             headers={"Origin": "http://testserver"},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertTrue(self.mod.db.hl_kill())

    def test_same_origin_without_csrf_rejected(self):
        self.login()
        r = self.client.post("/admin/switch/kill", data={"on": "1"},
                             headers={"Origin": "http://testserver"},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 403)
        self.assertFalse(self.mod.db.hl_kill(), "被拒的写操作不许改状态")

    def test_sibling_subdomain_origin_rejected(self):
        """SameSite 挡不住同站兄弟源（kma/01/lt.222677.xyz 同一个 site）。"""
        self.login()
        self.mod.db.set_config("mr_enabled", "1")
        r = self.client.post("/admin/hl/mr", data={"on": "0", "csrf": self.csrf},
                             headers={"Origin": "https://other.222677.xyz"},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.mod.db.get_config_str("mr_enabled"), "1")

    def test_cross_site_origin_rejected(self):
        self.login()
        r = self.client.post("/admin/hl/agent/renew",
                             data={"apikey": "ATTACKER", "apisecret": "ZmFrZQ==",
                                   "csrf": self.csrf},
                             headers={"Origin": "https://evil.test"},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.mod.db.get_config_str("kr_api_key", ""), "",
                         "跨源请求不许写凭据")

    def test_null_origin_rejected(self):
        self.login()
        r = self.client.post("/admin/switch/kill", data={"on": "1", "csrf": self.csrf},
                             headers={"Origin": "null"}, follow_redirects=False)
        self.assertEqual(r.status_code, 403)

    def test_same_origin_referer_accepted(self):
        self.login()
        r = self.client.post("/admin/switch/kill", data={"on": "1", "csrf": self.csrf},
                             headers={"Referer": "http://testserver/admin?tab=settings"},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertTrue(self.mod.db.hl_kill())

    def test_cross_site_referer_rejected(self):
        self.login()
        r = self.client.post("/admin/switch/kill", data={"on": "1", "csrf": self.csrf},
                             headers={"Referer": "https://evil.test/x"},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 403)

    def test_missing_origin_still_requires_csrf(self):
        """Cookie 会话 + 完全没有 Origin/Referer → **仍要 CSRF**。

        缺来源头只说明「可能不是浏览器」（curl / TestClient / 旧 Safari 同源表单），
        不能当成「是可信客户端」：旧版在这里直接放行，等于把 SameSite 当成唯一一层
        —— 兄弟子域被接管时同样能写。真表单自带隐藏 csrf（见设置页三表单用例），
        所以正常浏览器路径不受影响；runbook 的 ``-d on=1&k=`` 走显式 k，也不受影响。
        """
        self.login()
        r = self.client.post("/admin/switch/kill", data={"on": "1"},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 403, "缺来源头的 Cookie 写必须带 CSRF")
        self.assertFalse(self.mod.db.hl_kill(), "被拒的写操作不许改状态")

    def test_missing_origin_with_csrf_is_allowed(self):
        """带 CSRF 的 Cookie 写（无来源头）照常成功：非浏览器客户端一样能过。"""
        self.login()
        r = self.client.post("/admin/switch/kill", data={"on": "1", "csrf": self.csrf},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertTrue(self.mod.db.hl_kill())

    def test_csrf_via_header_without_origin(self):
        """CSRF 也能从 ``X-CSRF-Token`` 头来（无 HTML 表单的客户端用）。"""
        self.login()
        r = self.client.post("/admin/switch/kill", data={"on": "1"},
                             headers={"X-CSRF-Token": self.csrf},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertTrue(self.mod.db.hl_kill())

    def test_wrong_bearer_does_not_bypass_guard(self):
        """错误的 Bearer **不能**因为「头存在」就绕过同源校验（只认值）。"""
        self.login()
        r = self.client.post("/admin/switch/kill", data={"on": "1", "csrf": self.csrf},
                             headers={"Origin": "https://other.222677.xyz",
                                      **bearer("definitely-wrong")},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 403)
        self.assertFalse(self.mod.db.hl_kill())

    def test_valid_bearer_allows_without_origin(self):
        r = self.client.post("/admin/switch/kill", data={"on": "1"}, headers=bearer(),
                             follow_redirects=False)
        self.assertEqual(r.status_code, 303, "自动化（有效 Bearer）不需要 Origin/CSRF")
        self.assertTrue(self.mod.db.hl_kill())

    def test_valid_bearer_does_not_bypass_cross_origin(self):
        """有效 Bearer 免 CSRF，但**不能**把跨源请求变成合法请求。

        凭据证明「谁」，来源证明「从哪来」：来源不明就 fail-closed。
        自动化脚本不带 Origin，所以正常调度/脚本不受影响。
        """
        self.mod.db.set_config("mr_enabled", "0")
        r = self.client.post("/admin/hl/mr", data={"on": "1"},
                             headers={"Origin": "https://evil.test", **bearer()},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.mod.db.get_config_str("mr_enabled"), "0")

    def test_query_k_does_not_bypass_cross_origin(self):
        """显式 k 也不能在恶意 Origin 下当绕过（k 可能从 Referer/日志里泄露过）。"""
        self.mod.db.set_config("mr_enabled", "0")
        r = self.client.post("/admin/hl/mr?k=" + TOKEN, data={"on": "1"},
                             headers={"Origin": "https://other.222677.xyz"},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.mod.db.get_config_str("mr_enabled"), "0")

    def test_wrong_bearer_does_not_downgrade_to_form_k(self):
        """带错误 Bearer + 有效表单 k：不许降级成 k 会话（错头就是错头）。"""
        r = self.client.post("/admin/hl/kill", data={"on": "1", "k": TOKEN},
                             headers=bearer("nope-not-the-token"),
                             follow_redirects=False)
        self.assertEqual(r.status_code, 403)
        self.assertFalse(self.mod.db.hl_kill())

    def test_wrong_bearer_does_not_downgrade_to_cookie(self):
        """带错误 Bearer + 有效 Cookie + 同源 + 正确 CSRF：仍然 403（不降级）。"""
        self.login()
        r = self.client.post("/admin/switch/kill", data={"on": "1", "csrf": self.csrf},
                             headers={"Origin": "http://testserver",
                                      **bearer("nope-not-the-token")},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 403)
        self.assertFalse(self.mod.db.hl_kill())

    def test_malformed_origin_rejected(self):
        """畸形 Origin（带路径 / userinfo / 非 http(s)）一律 403。"""
        self.login()
        for bad in ("http://testserver/admin?tab=settings",      # 带路径/查询
                    "http://user:pw@testserver",                 # 带 userinfo
                    "file://testserver",                         # 非 http(s)
                    "http://testserver:notaport"):               # 坏端口
            r = self.client.post("/admin/switch/kill", data={"on": "1", "csrf": self.csrf},
                                 headers={"Origin": bad}, follow_redirects=False)
            self.assertEqual(r.status_code, 403, bad)
            self.assertFalse(self.mod.db.hl_kill(), bad)

    def test_form_k_still_works_for_runbook_curl(self):
        r = self.client.post("/admin/hl/kill", data={"on": "1", "k": TOKEN},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertTrue(self.mod.db.hl_kill())

    def test_no_credentials_is_still_401(self):
        r = self.client.post("/admin/switch/kill", data={"on": "1"},
                             headers={"Origin": "https://evil.test"},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 401, "先认人（401）再判来源（403）")

    def test_all_three_forms_carry_same_csrf_and_no_token(self):
        html = self.settings_html()
        self.assertEqual(html.count("name='csrf'"), 3,
                         "三个写表单（拉闸 / MR / 更新 API）必须一致带 CSRF 隐藏域")
        self.assertEqual(html.count(f"value='{self.csrf}'"), 3, "三个表单用同一个令牌")
        self.assertNotIn(TOKEN, html, "CSRF 令牌是派生值，ADMIN_TOKEN 不许进 HTML")

    def test_fallback_clear_requires_csrf_on_cookie_session(self):
        self.mod.db.set_config("hl_agent_valid_until_ms", "999999999999")
        self.login()
        r = self.client.post("/admin/hl/agent/fallback/clear",
                             headers={"Origin": "http://testserver"},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.mod.db.get_config_str("hl_agent_valid_until_ms", ""),
                         "999999999999")

    def test_renew_works_from_the_panel_form(self):
        """同源 + 表单 csrf = 真实浏览器路径，必须照常保存（不许把正常表单拦掉）。"""
        self.login()
        r = self.client.post("/admin/hl/agent/renew",
                             data={"apikey": "panel-key-1",
                                   "apisecret": "cGFuZWwtc2VjcmV0",   # base64
                                   "csrf": self.csrf},
                             headers={"Origin": "http://testserver"},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self.mod.db.get_config_str("kr_api_key", ""), "panel-key-1")
        self.assertNotIn("panel-key-1", r.headers.get("location", ""))

    def test_fallback_clear_works_from_the_panel_form(self):
        self.mod.db.set_config("hl_agent_valid_until_ms", "999999999999")
        self.login()
        r = self.client.post("/admin/hl/agent/fallback/clear",
                             data={"csrf": self.csrf},
                             headers={"Origin": "http://testserver"},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self.mod.db.get_config_str("hl_agent_valid_until_ms", ""), "")


# ---------------------------------------------------------------- B. on 白名单
class TestOnFlagStrict(FullAppCase):
    def test_missing_on_is_400_and_keeps_state(self):
        self.mod.db.hl_set_kill(True, "已触发", origin="manual")
        r = self.client.post("/admin/hl/kill", data={}, headers=bearer(),
                             follow_redirects=False)
        self.assertEqual(r.status_code, 400)
        self.assertTrue(self.mod.db.hl_kill(), "漏写 on 不许把 kill 解除")

    def test_unknown_on_is_400_and_keeps_state(self):
        self.mod.db.hl_set_kill(True, "已触发", origin="manual")
        r = self.client.post("/admin/hl/kill", data={"on": "yes-please"}, headers=bearer(),
                             follow_redirects=False)
        self.assertEqual(r.status_code, 400)
        self.assertTrue(self.mod.db.hl_kill())

    def test_mr_unknown_on_is_400(self):
        self.mod.db.set_config("mr_enabled", "1")
        r = self.client.post("/admin/hl/mr", data={"on": "maybe"}, headers=bearer(),
                             follow_redirects=False)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.mod.db.get_config_str("mr_enabled"), "1")

    def test_panel_kill_route_missing_on_is_400(self):
        self.mod.db.hl_set_kill(True, "已触发", origin="manual")
        self.login()
        r = self.client.post("/admin/switch/kill",
                             data={"csrf": self.csrf}, headers={"Origin": "http://testserver"},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 400)
        self.assertTrue(self.mod.db.hl_kill())

    def test_explicit_values_are_accepted(self):
        for raw, want in (("1", True), ("TRUE", True), ("on", True), (" yes ", True),
                          ("0", False), ("false", False), ("OFF", False), ("no", False)):
            r = self.client.post("/admin/hl/kill", data={"on": raw}, headers=bearer(),
                                 follow_redirects=False)
            self.assertEqual(r.status_code, 303, raw)
            self.assertIs(self.mod.db.hl_kill(), want, raw)


# ---------------------------------------------------------------- C. 响应头
class TestSecurityHeaders(FullAppCase):
    def test_admin_page_no_store_and_no_frames(self):
        r = self.client.get("/admin", headers=bearer())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers.get("cache-control"), "no-store")
        self.assertEqual(r.headers.get("x-frame-options"), "DENY")
        self.assertIn("frame-ancestors 'none'", r.headers.get("content-security-policy", ""))

    def test_status_api_no_store(self):
        r = self.client.get("/api/hl/status", headers=bearer())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers.get("cache-control"), "no-store")

    def test_health_is_not_no_store(self):
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        self.assertNotEqual(r.headers.get("cache-control"), "no-store")


# ---------------------------------------------------------------- D. API 卡三态
class TestApiCredThreeState(FullAppCase):
    KEY = "review7-key-AAAA"
    SEC = "cmV2aWV3Ny1zZWNyZXQ="          # base64("review7-secret")

    def _settings(self):
        return self.settings_html()

    def test_missing_creds_shows_not_configured(self):
        html = self._settings()
        self.assertIn("未配置", html)
        self.assertNotIn("已配置 · 无到期", html, "无凭据不许再显示「已配置」")

    def test_db_pair_not_verified_until_probe(self):
        self.mod.db.set_config("kr_api_key", self.KEY)
        self.mod.db.set_config("kr_api_secret", self.SEC)
        state = self.mod._agent_state()
        self.assertTrue(state["configured"])
        self.assertFalse(state["verified"])
        self.assertEqual(state["cred_source"], "db")
        self.assertIn("已配置 · 未验证", self._settings())

    def test_verified_after_successful_private_read(self):
        self.mod.db.set_config("kr_api_key", self.KEY)
        self.mod.db.set_config("kr_api_secret", self.SEC)
        self.mod._note_cred_probe(True)
        state = self.mod._agent_state()
        self.assertTrue(state["verified"])
        self.assertLessEqual(state["verify_age_s"], 5)
        self.assertIn("已配置 · 已验证", self._settings())

    def test_failed_read_is_reported(self):
        self.mod.db.set_config("kr_api_key", self.KEY)
        self.mod.db.set_config("kr_api_secret", self.SEC)
        self.mod._note_cred_probe(False, "KrAuthError")
        state = self.mod._agent_state()
        self.assertFalse(state["verified"])
        self.assertIs(state["last_read_ok"], False)
        self.assertIn("读取失败", self._settings())

    def test_half_pair_fails_closed_without_mixing_sources(self):
        """DB 只有 key + env 只有 secret：绝不拼对不同来源的两半。"""
        self.mod.db.set_config("kr_api_key", self.KEY)
        os.environ["KRAKEN_API_SECRET"] = self.SEC
        self.assertEqual(self.mod.kr_credentials(), ("", "", ""))
        self.assertEqual(self.mod.kr_api_key(), "")
        self.assertFalse(self.mod._agent_state()["configured"])
        self.assertIn("未配置", self.settings_html())

    def test_db_pair_wins_over_env_pair(self):
        self.mod.db.set_config("kr_api_key", self.KEY)
        self.mod.db.set_config("kr_api_secret", self.SEC)
        os.environ["KRAKEN_API_KEY"] = "env-key"
        os.environ["KRAKEN_API_SECRET"] = "env-secret"
        key, sec, src = self.mod.kr_credentials()
        self.assertEqual((key, sec, src), (self.KEY, self.SEC, "db"))

    def test_env_pair_used_when_db_empty(self):
        os.environ["KRAKEN_API_KEY"] = "env-key"
        os.environ["KRAKEN_API_SECRET"] = "env-secret"
        self.assertEqual(self.mod.kr_credentials(), ("env-key", "env-secret", "env"))

    def test_card_never_echoes_credentials(self):
        self.mod.db.set_config("kr_api_key", self.KEY)
        self.mod.db.set_config("kr_api_secret", self.SEC)
        html = self.settings_html()
        self.assertNotIn(self.KEY, html)
        self.assertNotIn(self.SEC, html)

    def test_status_api_agent_block_is_same_tristate(self):
        """/api/hl/status 的 agent 块与设置页卡片同源（一个生产者）。"""
        j = self.client.get("/api/hl/status", headers=bearer()).json()
        self.assertEqual(j["agent"]["source"], "kraken-key-no-expiry")
        self.assertIs(j["agent"]["configured"], False)
        self.mod.db.set_config("kr_api_key", self.KEY)
        self.mod.db.set_config("kr_api_secret", self.SEC)
        self.mod._note_cred_probe(True)
        j = self.client.get("/api/hl/status", headers=bearer()).json()
        self.assertIs(j["agent"]["configured"], True)
        self.assertIs(j["agent"]["verified"], True)


# ---------------------------------------------------------------- E/F/G/H 面板数据
class PanelPayloadCase(unittest.TestCase):
    """status_payload / _render_body / _sz_tick 的纯离线用例（临时库 + 假 info）。"""

    def setUp(self):
        from app.db import DB
        from app.hl.views import DashboardDeps
        self.tmp = tempfile.mkdtemp(prefix="review7_panel_")
        self.db = DB(os.path.join(self.tmp, "p.db"))
        self.info = FakeKrInfo()
        self.mr = None
        self.deps = DashboardDeps(
            db=self.db,
            config_fn=lambda: type("C", (), {"symbols": ["BTC_USDT", "ETH_USDT"],
                                             "equity": 1000.0})(),
            info=self.info, address_fn=lambda: ADDR, mode_fn=lambda: "live",
            now_fn=lambda: NOW, mr_fn=lambda: self.mr)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def payload(self):
        from app.hl.views import status_payload
        return status_payload(self.deps)

    def html(self, p=None):
        from app.hl.views import _render_body
        return _render_body(p if p is not None else self.payload())

    def set_snap(self, positions=(), orders=(), **kw):
        self.info.snap = base_snapshot(positions, orders, **kw)

    # ---- E. MR 分层去重 ----
    def test_mr_claimed_removes_duplicate_qqq_row(self):
        self.set_snap([btc_pos(), qqq_pos()], [stop_order()])
        self.mr = {"enabled": True, "state": "holding", "recon": "ok",
                   "pos": {"qty": 1.0, "entry_price": 610.0, "mark": 620.0,
                           "upnl": 10.0, "upnl_pct": 1.64, "margin": 62.0,
                           "leverage": 10, "exit_line": 600.0, "dist_to_exit_pct": 3.2}}
        p = self.payload()
        self.assertEqual([x["coin"] for x in p["positions"]], ["BTC"],
                         "MR 已认领时，普通 QQQ 行让给 MR 行（同一张仓不显示两行）")
        html = self.html(p)
        self.assertEqual(html.count("<b>QQQ</b>"), 1, "表格里只许出现一行 QQQ")

    def test_unclaimed_qqq_stays_visible_and_flagged(self):
        """MR 没认领的真实 QQQ 仓必须可见，并且是异常（不许按币种一律删）。"""
        self.set_snap([qqq_pos()], [])
        self.mr = {"enabled": True, "state": "flat", "recon": "ok"}
        p = self.payload()
        qqq = [x for x in p["positions"] if x["coin"] == "QQQ"]
        self.assertEqual(len(qqq), 1, "没有 MR 持仓时交易所 QQQ 仓必须保留")
        self.assertTrue(qqq[0]["mr_unclaimed"])
        html = self.html(p)
        self.assertIn("未认领", html)
        self.assertIn("QQQ 异常", html, "徽章要把异常仓写出来")

    def test_qqq_qty_mismatch_is_not_claimed(self):
        """台账说 2 张、交易所 1 张：不算认领（数量对不上一律按异常展示）。"""
        self.set_snap([qqq_pos(size=1.0)], [])
        self.mr = {"enabled": True, "state": "holding", "recon": "err",
                   "pos": {"qty": 2.0, "entry_price": 610.0, "mark": 620.0}}
        p = self.payload()
        qqq = [x for x in p["positions"] if x["coin"] == "QQQ"]
        self.assertEqual(len(qqq), 1)
        self.assertTrue(qqq[0]["mr_unclaimed"])

    def test_mr_off_shows_db_open_ledger_read_only(self):
        self.db.mr_record_entry(signal_date="2026-11-10", entry_session="2026-11-11",
                                qty=1.0, qty_int=1, entry_price=610.0,
                                entry_order_id="oid-1", entry_ts=NOW - 3600,
                                notional=610.0, margin=61.0, equity_at_entry=1000.0)
        self.set_snap([qqq_pos()], [])
        # mode=off：运行时状态只有 mode_off，没有 pos
        self.mr = {"enabled": False, "state": "mode_off", "recon": "unknown", "error": ""}
        p = self.payload()
        self.assertIn("ledger_pos", p["mr"])
        self.assertEqual(p["mr"]["ledger_pos"]["qty"], 1.0)
        self.assertEqual([x for x in p["positions"] if x["coin"] == "QQQ"], [],
                         "台账认领后不重复显示普通 QQQ 行")
        html = self.html(p)
        self.assertIn("台账", html)
        self.assertIn("不会自动离场", html, "MR 停用必须在行上提示不会自动离场")
        self.assertIn("QQQ", html)

    def test_mr_off_without_ledger_stays_hidden(self):
        from app.hl.views import _mr_view
        self.set_snap([], [])
        self.mr = {"enabled": False, "state": "mode_off", "recon": "unknown"}
        p = self.payload()
        self.assertNotIn("ledger_pos", p["mr"])
        self.assertIsNone(_mr_view({"mr": p["mr"]}))

    # ---- E2. MR 运行时状态必须真的显示（第七轮复审） ----
    def test_action_required_is_rendered_with_reason(self):
        """``state=action_required`` 原来渲染成「空仓」：必须显示「需人工核对」+ 原因。"""
        self.set_snap([], [])
        self.mr = {"enabled": True, "state": "action_required", "recon": "ok", "error": "",
                   "action_required": {"kind": "entry_result_unknown",
                                       "reason": "入场结果未知且账户读失败 → 本轮不重发"}}
        p = self.payload()
        self.assertEqual(p["mr"]["state"], "action_required")
        self.assertIn("action_required", p["mr"])
        html = self.html(p)
        self.assertIn("需人工核对", html)
        self.assertIn("entry_result_unknown", html, "kind 要写出来（按提示查证据）")
        self.assertIn("本轮不重发", html, "原因要写出来，不能只给一个状态词")

    def test_order_gate_blocked_is_rendered(self):
        """``state=order_gate_blocked``：显示「风控闸门拦下」并带上闸门原因。"""
        self.set_snap([], [])
        self.mr = {"enabled": True, "state": "order_gate_blocked", "recon": "ok",
                   "error": "当日下单数已达上限（3）"}
        html = self.html()
        self.assertIn("风控闸门拦下", html)
        self.assertIn("当日下单数已达上限", html)

    def test_dry_run_is_rendered(self):
        """``state=dry_run``：影子模式下「一张真单都没发」必须写出来。"""
        self.set_snap([], [])
        self.mr = {"enabled": True, "state": "dry_run", "recon": "ok",
                   "error": "影子模式：未真实下单"}
        html = self.html()
        self.assertIn("影子模式", html)
        self.assertIn("未真实下单", html)

    def test_action_required_on_holding_row_shows_in_subm_line(self):
        """持仓行也要带原因：否则「持仓中」会把「这轮其实没按预期工作」盖掉。"""
        self.set_snap([qqq_pos()], [])
        self.mr = {"enabled": True, "state": "action_required", "recon": "ok",
                   "pos": {"qty": 1.0, "entry_price": 610.0, "mark": 620.0},
                   "action_required": {"kind": "exit_result_unknown",
                                       "reason": "离场回执丢失且账户读失败"}}
        html = self.html()
        self.assertIn("exit_result_unknown", html)
        self.assertIn("离场回执丢失", html)

    def test_api_json_carries_state_fields(self):
        """/api/hl/status 的 mr 块原样带 state/error/action_required（监控可用）。"""
        from app.hl.views import build_router
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        self.set_snap([], [])
        self.mr = {"enabled": True, "state": "action_required", "recon": "ok",
                   "error": "", "action_required": {"kind": "entry_result_unknown",
                                                    "reason": "读账户失败"}}
        app = FastAPI()
        app.include_router(build_router(self.deps))
        body = TestClient(app).get("/api/hl/status").json()
        self.assertEqual(body["mr"]["state"], "action_required")
        self.assertEqual(body["mr"]["action_required"]["kind"], "entry_result_unknown")

    def test_mr_off_ledger_mismatch_keeps_both_rows_visible(self):
        """MR 停用 + 台账数量与交易所不符：两张都不许藏（一张标台账、一张标未认领）。"""
        self.db.mr_record_entry(signal_date="2026-11-10", entry_session="2026-11-11",
                                qty=2.0, qty_int=2, entry_price=610.0,
                                entry_order_id="oid-1", entry_ts=NOW - 3600,
                                notional=1220.0, margin=122.0, equity_at_entry=1000.0)
        self.set_snap([qqq_pos(size=1.0)], [])
        self.mr = {"enabled": False, "state": "mode_off", "recon": "unknown", "error": ""}
        p = self.payload()
        qqq = [x for x in p["positions"] if x["coin"] == "QQQ"]
        self.assertEqual(len(qqq), 1, "数量对不上 = 未认领，普通行必须保留")
        self.assertTrue(qqq[0]["mr_unclaimed"])
        html = self.html(p)
        self.assertIn("未认领", html)
        self.assertIn("台账", html, "台账那一行也要显示（它说明 MR 以为持 2 张）")

    # ---- F. 无止损 → R / 距离给 — ----
    def test_no_stop_has_no_fake_r_or_distance(self):
        self.set_snap([btc_pos()], [])
        p = self.payload()
        pos = p["positions"][0]
        self.assertFalse(pos["has_stop"])
        self.assertIsNone(pos["r_unrealized"], "无止损不许编 +0.00R")
        self.assertIsNone(pos["stop_dist_pct"], "无止损不许编 0.00%")
        html = self.html(p)
        row = html.split("<b>BTC</b>", 1)[1].split("</tr>", 1)[0]
        self.assertNotIn("+0.00R", row)
        self.assertIn("<td class='num'>—</td>", row, "R 与止损距离两列都应给「—」")

    def test_unreachable_positions_badge_is_unknown(self):
        self.info.raise_ = True
        p = self.payload()
        self.assertFalse(p["network"]["reachable"])
        html = self.html(p)
        self.assertIn("持仓 <b>无法确认</b>", html)
        self.assertNotIn("持仓 <b>空仓</b>", html)

    def test_reachable_empty_positions_badge_says_flat(self):
        self.set_snap([], [])
        html = self.html()
        self.assertIn("持仓 <b>空仓</b>", html)

    # ---- G. 快照年龄与来源 ----
    def test_age_only_reported_when_snapshot_used(self):
        from app.panel_store import PanelSnapshotStore, SnapshotInfo
        store = PanelSnapshotStore(self.db)
        snap = base_snapshot([], [])
        store.write(snap, {"BTC": 100_000.0}, ts=NOW - 960)      # 16 分钟前 → 过期
        self.deps.info = SnapshotInfo(store, self.info, clock=lambda: NOW)
        p = self.payload()
        self.assertIsNone(p["panel_snapshot_age_s"], "过期快照 = 本次用的是实时值")
        self.assertEqual(p["data_source"], "live")

    def test_age_reported_for_fresh_snapshot(self):
        from app.panel_store import PanelSnapshotStore, SnapshotInfo
        store = PanelSnapshotStore(self.db)
        store.write(base_snapshot([], []), {"BTC": 100_000.0}, ts=NOW - 120)
        self.deps.info = SnapshotInfo(store, self.info, clock=lambda: NOW)
        p = self.payload()
        self.assertAlmostEqual(p["panel_snapshot_age_s"], 120.0, places=1)
        self.assertEqual(p["data_source"], "snapshot")

    def test_admin_payload_age_follows_data_source(self):
        """get_payload 只认数据自带的年龄，不再拿「库里最新一行多旧」冒充。"""
        from app.admin_ui import AdminDeps, get_payload
        from app.panel_store import PanelSnapshotStore, SnapshotInfo
        store = PanelSnapshotStore(self.db)
        store.write(base_snapshot([], []), {}, ts=NOW - 960)
        deps = AdminDeps(db=self.db, config_fn=lambda: None, hl_deps=self.deps,
                         mode_fn=lambda: "live", token_fn=lambda: TOKEN,
                         address_fn=lambda: ADDR, private_key_fn=lambda: "")
        deps.hl_deps.info = SnapshotInfo(store, self.info, clock=lambda: NOW)
        self.assertNotIn("panel_age_s", get_payload(deps))
        store.write(base_snapshot([], []), {}, ts=NOW - 60)
        deps.hl_deps.info = SnapshotInfo(store, self.info, clock=lambda: NOW)
        self.assertAlmostEqual(get_payload(deps)["panel_age_s"], 60.0, places=1)

    # ---- H. 数量精度零网络 ----
    def test_sz_tick_prefers_snapshot_spec(self):
        from app.hl.views import _sz_tick
        calls = []

        class Stub(FakeKrInfo):
            def specs(self, coins=None):
                calls.append(coins)
                return {}

        got = _sz_tick(Stub(), "BTC", {"sz_ticks": {"BTC": 0.0001}})
        self.assertEqual(got, 0.0001)
        self.assertEqual(calls, [], "快照带了规格就不许再问 specs")

    def test_sz_tick_uses_cached_specs_without_fetching(self):
        from app.hl.views import _sz_tick

        class Cold(FakeKrInfo):
            _specs_cache = None

        info = Cold()
        self.assertIsNone(_sz_tick(info, "BTC"), "缓存未热 → 精度未知（不外呼）")
        self.assertEqual(info.spec_calls, 0)
        info._specs_cache = {"BTC": {"sz_decimals": 4}}
        self.assertEqual(_sz_tick(info, "BTC"), 0.0001)
        self.assertEqual(info.spec_calls, 0, "只读缓存，不触发 /instruments")

    def test_sz_tick_never_calls_specs_even_without_cache_attr(self):
        """降级适配层（只有 specs()、没有缓存属性）也不许被问 —— 渲染零外呼。"""
        from app.hl.views import _sz_tick

        class OnlySpecs(FakeKrInfo):
            """没有任何缓存属性，只有 specs()；被调用就是外呼（冷缓存 /instruments）。"""

        info = OnlySpecs()
        info.__dict__.pop("_specs_cache", None)      # 确保没有缓存属性
        self.assertIsNone(_sz_tick(info, "BTC"),
                          "没有可读缓存 → 精度未知，宁严不松")
        self.assertEqual(info.spec_calls, 0, "渲染层绝不调用 specs()")

    def test_stop_judgement_uses_snapshot_tick(self):
        """止损张数容差走快照里的精度（老快照没有该键 → 相对容差，判更严）。"""
        from tests.test_hl_views import FakeInfo as PlainInfo
        self.set_snap([btc_pos(size=0.05)], [stop_order(sz=0.049995)])
        self.deps.info = PlainInfo(self.info.snap)      # 无 specs、无缓存属性
        p = self.payload()
        self.assertFalse(p["positions"][0]["has_stop"],
                         "没有规格时退化成相对容差：差 5e-6 不算覆盖")
        snap = base_snapshot([btc_pos(size=0.05)], [stop_order(sz=0.049995)])
        snap["sz_ticks"] = {"BTC": 0.0001}
        self.deps.info = PlainInfo(snap)
        self.assertTrue(self.payload()["positions"][0]["has_stop"],
                        "快照带精度时一个 tick 内的舍入算覆盖")


# ---------------------------------------------------------------- H2. capture 写规格
class TestCaptureSnapshotSzTicks(FullAppCase):
    def test_capture_writes_sz_ticks(self):
        self.mod._hl_info = None
        self.mod._snapshot_store = None
        self.mod._snapshot_info = None
        ok = self.mod.capture_panel_snapshot("test")
        self.assertTrue(ok)
        row = self.mod.db.panel_snapshot_latest()
        self.assertIn("sz_ticks", row["snapshot"])
        self.assertEqual(row["snapshot"]["sz_ticks"]["BTC"], 0.0001)

    def test_capture_marks_credentials_verified(self):
        self.mod.db.set_config("kr_api_key", "k-1")
        self.mod.db.set_config("kr_api_secret", "czEtc2VjcmV0")
        self.mod._CRED_PROBE = {}
        self.mod.capture_panel_snapshot("test")
        self.assertTrue(self.mod._agent_state()["verified"])


# ---------------------------------------------------------------- I. 已实现亏损熔断
class TestRealizedDdClear(FullAppCase):
    def _arm(self, net, equity=1000.0):
        db = self.mod.db
        db.hl_upsert_fill(tid=f"f-{net}", symbol="BTC_USDT", coin="BTC", side=1,
                          sz=0.01, px=100_000.0, fee=0.0, fee_token="USD", fee_quote=0.0,
                          dir="Close Long", closed_pnl=net, hash=f"h-{net}",
                          reduce_only=True, ts=int(time.time()) - 3600)
        db.panel_snapshot_write(base_snapshot([], [], equity=equity), {},
                                None, time.time() - 60, "test")
        db.hl_set_kill(True, "已实现亏损熔断", origin="realized_dd")

    def test_refuses_clear_while_over_threshold(self):
        self._arm(-216.5)
        r = self.client.post("/admin/hl/kill", data={"on": "0"}, headers=bearer(),
                             follow_redirects=False)
        self.assertEqual(r.status_code, 400)
        body = r.json()["detail"]
        self.assertIn("未解除", body)
        self.assertIn("-216.50", body, "必须把剩余亏损写出来")
        self.assertIn("熔断线", body)
        self.assertTrue(self.mod.db.hl_kill(), "拒绝解除时 kill 必须保持")
        self.assertEqual(self.mod.db.hl_kill_origin(), "realized_dd",
                         "拒绝解除不许改 origin（也不许重置窗口）")

    def test_panel_route_refuses_with_message(self):
        self._arm(-216.5)
        self.login()
        r = self.client.post("/admin/switch/kill",
                             data={"on": "0", "csrf": self.csrf},
                             headers={"Origin": "http://testserver"},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertTrue(self.mod.db.hl_kill())
        loc = r.headers["location"]
        self.assertIn("tab=settings", loc)
        self.assertIn("%E6%9C%AA%E8%A7%A3%E9%99%A4", loc, "跳回设置页并带回拒绝说明")

    def test_clears_when_window_recovered(self):
        self._arm(-10.0)
        r = self.client.post("/admin/hl/kill", data={"on": "0"}, headers=bearer(),
                             follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertFalse(self.mod.db.hl_kill())

    def test_no_confirmation_baseline_written(self):
        """「确认基线」= 改风控口径，本轮明确不做：不许留下任何 clear_at 键。"""
        self._arm(-216.5)
        self.client.post("/admin/hl/kill", data={"on": "0"}, headers=bearer(),
                         follow_redirects=False)
        cfg = self.mod.db.get_config()
        self.assertFalse([k for k in cfg if "realized_dd_clear" in k or "dd_clear" in k])


# ---------------------------------------------------------------- J. 同源判定（纯函数）
class TestAuthOriginUnits(unittest.TestCase):
    """`app/auth.py` 的同源判定是纯函数：畸形输入也不许 500。"""

    class _Req:
        """最小请求替身：只要 headers / url / cookies / query_params。"""

        def __init__(self, headers=None, scheme="https", host="kma.example"):
            self.headers = dict(headers or {})
            self.cookies = {}
            self.query_params = {}
            self.url = type("U", (), {"scheme": scheme, "netloc": host})()

    def test_malformed_host_port_is_not_same_origin(self):
        from app import auth
        ok, why = auth.same_origin(
            self._Req({"host": "kma.example:abc", "origin": "https://kma.example"}))
        self.assertFalse(ok, why)

    def test_default_port_forms_are_same_origin(self):
        from app import auth
        for host, origin in (("kma.example:443", "https://kma.example"),
                             ("kma.example", "https://kma.example:443")):
            ok, why = auth.same_origin(self._Req({"host": host, "origin": origin}))
            self.assertTrue(ok, f"{host} vs {origin}: {why}")

    def test_scheme_downgrade_rejected(self):
        from app import auth
        ok, _ = auth.same_origin(
            self._Req({"host": "kma.example", "origin": "http://kma.example"}))
        self.assertFalse(ok)

    def test_subdomain_is_not_same_origin(self):
        from app import auth
        ok, _ = auth.same_origin(
            self._Req({"host": "kma.example", "origin": "https://01.kma.example"}))
        self.assertFalse(ok)

    def test_csrf_token_is_derived_not_the_secret(self):
        from app import auth
        tok = "admin-token-value"
        csrf = auth.csrf_token(tok)
        self.assertEqual(len(csrf), 32)
        self.assertNotIn(tok, csrf)
        self.assertNotEqual(auth.csrf_token("other"), csrf)

    def test_parse_on_flag_whitelist(self):
        from app import auth
        for bad in (None, "", "maybe", "2", "-1"):
            self.assertIsNone(auth.parse_on_flag(bad), repr(bad))
        self.assertTrue(auth.parse_on_flag(" ON "))
        self.assertFalse(auth.parse_on_flag("Off"))


# ---------------------------------------------------------------- K. 止损判据
class TestStopsNumerics(unittest.TestCase):
    def _o(self, **kw):
        o = {"coin": "BTC", "oid": 1, "is_buy": False, "sz": 1.0, "reduce_only": True,
             "trigger_px": 90.0, "is_trigger": True, "order_type": "stop_loss"}
        o.update(kw)
        return o

    def test_non_finite_size_is_not_protection(self):
        from app.hl.stops import has_covering_stop
        for bad in (float("inf"), float("nan")):
            self.assertFalse(has_covering_stop([self._o(sz=bad)], "BTC", side=1, sz=1.0,
                                               entry_px=100.0), repr(bad))

    def test_non_finite_trigger_is_not_protection(self):
        from app.hl.stops import has_covering_stop
        for bad in (float("inf"), float("nan")):
            self.assertFalse(has_covering_stop([self._o(trigger_px=bad)], "BTC", side=1,
                                               sz=1.0, entry_px=100.0), repr(bad))

    def test_complete_tick_shortfall_is_not_covered(self):
        """**完整差一个 tick = 真实裸量，不是噪声**：容差严格小于一格。

        交易所的数量按 tick 格点下发，两个真实数量的差只可能是 0 或 ≥1 格；
        完整少一格就是少一格（旧版把它当舍入吞掉，等于把「少挂一手」算成满仓）。
        """
        from app.hl.stops import has_covering_stop
        self.assertFalse(has_covering_stop([self._o(sz=0.0001)], "BTC", side=1,
                                           sz=0.0002, entry_px=100.0, sz_tick=0.0001),
                         "两个 tick 的仓少挂一个 tick（50% 裸量）绝不算覆盖")
        self.assertFalse(has_covering_stop([self._o(sz=0.0082)], "BTC", side=1,
                                           sz=0.0083, entry_px=100.0, sz_tick=0.0001),
                         "常规仓位也一样：完整差一个 tick 不算覆盖")

    def test_sub_tick_difference_is_float_noise_only(self):
        """半格以内只是浮点表示误差（同一格点），仍算覆盖。"""
        from app.hl.stops import has_covering_stop
        self.assertTrue(has_covering_stop([self._o(sz=0.049995)], "BTC", side=1,
                                          sz=0.05, entry_px=100.0, sz_tick=0.00001),
                        "半个 tick 是同一格点上的表示误差")
        self.assertTrue(has_covering_stop([self._o(sz=0.0083 - 1e-15)], "BTC", side=1,
                                          sz=0.0083, entry_px=100.0, sz_tick=0.0001),
                        "浮点尾差不能被当成缺口")

    def test_tolerance_is_independent_of_position_ratio(self):
        """容差只看真实精度（半格），不再有「持仓 10%」这种与精度无关的比例。

        同一条判据在「有 tick」时对大小仓位一致：1 tick 的缺口永远不算覆盖，
        不会出现「大仓的 1 tick 算噪声、小仓的 1 tick 算裸量」两套结论。
        """
        from app.hl.stops import has_covering_stop
        for size, have in ((0.0002, 0.0001), (0.01, 0.0099), (1.0, 0.9999)):
            self.assertFalse(has_covering_stop([self._o(sz=have)], "BTC", side=1,
                                               sz=size, entry_px=100.0, sz_tick=0.0001),
                             f"sz={size} have={have}")


if __name__ == "__main__":                   # pragma: no cover
    unittest.main()
