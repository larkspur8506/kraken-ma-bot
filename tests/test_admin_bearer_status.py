"""管理鉴权契约（Authorization: Bearer）与 /api/hl/status.orders_trusted 的测试。

验收点（本轮 R4）：
1. 所有原来只认 ``?k=`` 的受保护路径都接受 Authorization Bearer 认证，
   错误/缺失凭据仍是 401；``?k=`` 与 ``cma_admin`` Cookie 保留（便于回滚）。
2. ``/api/hl/status`` 顶层 ``orders_trusted``：与 snap 同源，缺 snap/缺字段 = False。
3. 已认证（Cookie / Bearer）后渲染的管理 HTML 不含 token 真值（URL 与隐藏域都不带）。

**测试不得使用真 token**：本文件里的 ``TOKEN`` 是明显的假值，与任何部署环境无关。
全部走临时 sqlite DB + 假只读客户端，不触网。
"""
from __future__ import annotations

import importlib
import io
import logging
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TOKEN = "test-admin-token-not-real"

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.admin_ui import AdminDeps, build_admin_router
    from app.config import Config
    from app.hl.views import DashboardDeps, build_router as build_hl_router, status_payload
except ImportError:                       # pragma: no cover - CI 里必然已装
    FastAPI = TestClient = None
    AdminDeps = build_admin_router = Config = None
    DashboardDeps = build_hl_router = status_payload = None

NOW = 1_760_000_000.0
ADDR = "0xTESTADDRESS123456"


def fake_snapshot(*, orders_trusted="missing", equity=100.68):
    """固定快照。``orders_trusted="missing"`` 时不写该键（模拟老快照/字段缺失）。"""
    snap = {"ts": NOW, "address": ADDR, "equity_total": equity, "equity": equity,
            "spot_usdc": equity, "unified": True, "withdrawable": 90.0,
            "margin_used": 10.0, "notional": 500.0, "positions": [], "open_orders": []}
    if orders_trusted != "missing":
        snap["orders_trusted"] = bool(orders_trusted)
    return snap


class FakeInfo:
    def __init__(self, snapshot=None, raise_=False):
        self._snap = snapshot if snapshot is not None else fake_snapshot()
        self.raise_ = raise_

    def snapshot(self, address, mids=None):
        if self.raise_:
            raise RuntimeError("boom: network down")
        return self._snap

    def all_mids(self):
        if self.raise_:
            raise RuntimeError("boom: allMids down")
        return {"BTC": 100_000.0, "ETH": 3_000.0}


def bearer(value: str = TOKEN) -> dict:
    return {"Authorization": f"Bearer {value}"}


class BaseCase(unittest.TestCase):
    """临时 DB + 假 info + 管理面板路由 + HL 路由（打开 HL_DASH_REQUIRE_ADMIN）。"""

    def setUp(self):
        if FastAPI is None:
            self.skipTest("fastapi 未安装")
        self.tmp = tempfile.mkdtemp(prefix="bearer_")
        self._env_backup = {k: os.environ.get(k)
                            for k in ("RISK_PCT", "STOP_MAX", "HL_DASH_REQUIRE_ADMIN")}
        os.environ["RISK_PCT"] = "0.02"
        os.environ["STOP_MAX"] = "0.1"
        # /api/hl/* 默认不校验；本轮要验 Bearer 必须打开这个开关
        os.environ["HL_DASH_REQUIRE_ADMIN"] = "1"
        from app.db import DB
        self.db = DB(os.path.join(self.tmp, "t.db"))
        self.info = FakeInfo()
        cfg = Config()
        cfg.admin_token = TOKEN            # 与部署环境无关的假 token
        self.cfg_cache = {"cfg": cfg}
        self.hl_deps = DashboardDeps(
            db=self.db, config_fn=self._cfg, info=self.info,
            address_fn=lambda: ADDR, mode_fn=lambda: "off",
            base_url_fn=lambda: "https://api.kraken-test.example",
            now_fn=lambda: NOW)
        self.deps = AdminDeps(
            db=self.db, config_fn=self._cfg, hl_deps=self.hl_deps,
            mode_fn=lambda: "off", token_fn=lambda: TOKEN,
            address_fn=lambda: ADDR, private_key_fn=lambda: "",
            mids_fn=lambda: self.info.all_mids())
        app = FastAPI()
        app.include_router(build_admin_router(self.deps))
        app.include_router(build_hl_router(self.hl_deps))
        self.client = TestClient(app)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        for key, val in self._env_backup.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val

    def _cfg(self):
        return self.cfg_cache["cfg"].apply_db(self.db.get_config())


# ---------------------------------------------------------------- 1. Bearer 鉴权
class TestBearerAuthAdmin(BaseCase):
    """管理面板：Bearer 替代 ?k=；错误/缺失仍 401。"""

    def test_admin_with_bearer_only_200(self):
        r = self.client.get("/admin", headers=bearer())
        self.assertEqual(r.status_code, 200)
        self.assertIn("账户权益", r.text)

    def test_records_tab_with_bearer_only_200(self):
        r = self.client.get("/admin?tab=records", headers=bearer())
        self.assertEqual(r.status_code, 200)
        self.assertIn("资金流水", r.text)

    def test_missing_credentials_is_401(self):
        self.assertEqual(self.client.get("/admin").status_code, 401)

    def test_wrong_bearer_is_401_and_not_echoed(self):
        r = self.client.get("/admin", headers=bearer("definitely-wrong-value"))
        self.assertEqual(r.status_code, 401)
        self.assertNotIn("definitely-wrong-value", r.text)

    def test_malformed_authorization_is_401(self):
        for hdr in ("Bearer", "Bearer    ", f"Basic {TOKEN}", TOKEN,
                    f"Bearer wrong {TOKEN}", ""):
            r = self.client.get("/admin", headers={"Authorization": hdr})
            self.assertEqual(r.status_code, 401, f"畸形头不该放行：{hdr!r}")

    def test_bearer_post_kill_works_without_k_or_cookie(self):
        self.assertFalse(self.db.hl_kill())
        r = self.client.post("/admin/switch/kill", data={"on": "1"}, headers=bearer(),
                             follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertTrue(self.db.hl_kill())

    def test_bearer_post_fallback_clear_works(self):
        self.db.set_config("hl_agent_valid_until_ms", "999999999999")
        r = self.client.post("/admin/hl/agent/fallback/clear", headers=bearer(),
                             follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self.db.get_config_str("hl_agent_valid_until_ms", ""), "")

    def test_bearer_post_renew_passes_auth(self):
        """认证通过后才会走到「未接线」的 400（而不是 401）。"""
        r = self.client.post("/admin/hl/agent/renew",
                             data={"apikey": "fake", "apisecret": "ZmFrZQ=="},
                             headers=bearer(), follow_redirects=False)
        self.assertEqual(r.status_code, 400)
        self.assertIn("未接线", r.text)

    def test_bearer_theme_and_rows_redirects(self):
        for path in ("/admin/theme?t=dark", "/admin/rows?n=50",
                     "/admin/list/rows?n=50&back=/admin/list/orders"):
            r = self.client.get(path, headers=bearer(), follow_redirects=False)
            self.assertEqual(r.status_code, 302, path)
            self.assertTrue(r.headers["location"].startswith("/admin"), path)

    def test_bearer_list_subpage_200(self):
        r = self.client.get("/admin/list/orders", headers=bearer())
        self.assertEqual(r.status_code, 200)
        self.assertIn("下单记录", r.text)

    def test_bearer_export_200(self):
        r = self.client.get("/admin/export?table=orders", headers=bearer())
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/csv", r.headers.get("content-type", ""))

    def test_unknown_subpage_still_404_after_auth(self):
        r = self.client.get("/admin/list/nope", headers=bearer())
        self.assertEqual(r.status_code, 404)

    def test_query_k_still_works(self):
        self.assertEqual(self.client.get("/admin", params={"k": TOKEN}).status_code, 200)

    def test_cookie_still_works(self):
        self.client.get("/admin", params={"k": TOKEN})     # 种 Cookie
        self.assertEqual(self.client.get("/admin").status_code, 200)

    def test_failed_auth_does_not_log_credential(self):
        buf = io.StringIO()
        target = logging.getLogger("app")
        h = logging.StreamHandler(buf)
        target.addHandler(h)
        old = target.level
        target.setLevel(logging.DEBUG)
        try:
            self.client.get("/admin", headers=bearer("leaked-secret-value"))
        finally:
            target.removeHandler(h)
            target.setLevel(old)
        self.assertNotIn("leaked-secret-value", buf.getvalue())


class TestBearerAuthStatusApi(BaseCase):
    """JSON API（HL_DASH_REQUIRE_ADMIN=1）：Bearer 是统一口径。"""

    def test_status_bearer_200(self):
        r = self.client.get("/api/hl/status", headers=bearer())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["schema_version"], 1)

    def test_status_without_credentials_401(self):
        self.assertEqual(self.client.get("/api/hl/status").status_code, 401)

    def test_status_wrong_bearer_401(self):
        r = self.client.get("/api/hl/status", headers=bearer("wrong-token"))
        self.assertEqual(r.status_code, 401)

    def test_all_json_endpoints_accept_bearer(self):
        for path in ("/api/hl/status", "/api/hl/equity", "/api/hl/why",
                     "/api/hl/snapshot"):
            r = self.client.get(path, headers=bearer())
            self.assertEqual(r.status_code, 200, path)

    def test_query_k_still_works_on_api(self):
        self.assertEqual(self.client.get("/api/hl/status",
                                         params={"k": TOKEN}).status_code, 200)


# ---------------------------------------------------------------- 2. orders_trusted
class TestOrdersTrustedPayload(BaseCase):
    def _payload(self, **snap_kw):
        self.hl_deps.info = FakeInfo(fake_snapshot(**snap_kw))
        return status_payload(self.hl_deps)

    def test_true(self):
        self.assertIs(self._payload(orders_trusted=True)["orders_trusted"], True)

    def test_false(self):
        self.assertIs(self._payload(orders_trusted=False)["orders_trusted"], False)

    def test_missing_key_defaults_false(self):
        self.assertIs(self._payload(orders_trusted="missing")["orders_trusted"], False)

    def test_unreachable_snapshot_defaults_false(self):
        self.hl_deps.info = FakeInfo(raise_=True)
        self.assertIs(status_payload(self.hl_deps)["orders_trusted"], False)

    def test_top_level_is_boolean_not_truthy_string(self):
        p = self._payload(orders_trusted=True)
        self.assertIsInstance(p["orders_trusted"], bool)


class TestOrdersTrustedOverHttp(BaseCase):
    """契约：监控从 /api/hl/status **顶层**读布尔 orders_trusted。"""

    def _status(self):
        r = self.client.get("/api/hl/status", headers=bearer())
        self.assertEqual(r.status_code, 200)
        return r.json()

    def test_true_false_missing_over_http(self):
        for given, want in ((True, True), (False, False), ("missing", False)):
            self.hl_deps.info = FakeInfo(fake_snapshot(orders_trusted=given))
            j = self._status()
            self.assertIs(j["orders_trusted"], want, f"snap.orders_trusted={given!r}")
            self.assertIn("orders_trusted", j)


# ---------------------------------------------------------------- 3. HTML 不含 token
class TestNoTokenInRenderedHtml(BaseCase):
    """已认证（Cookie / Bearer）后，管理 HTML 不再带 token 真值。"""

    def _cookie_session(self):
        r = self.client.get("/admin", params={"k": TOKEN})     # 种 cma_admin
        self.assertEqual(r.status_code, 200)
        return self.client

    def test_cookie_session_overview_has_no_token(self):
        c = self._cookie_session()
        r = c.get("/admin")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(TOKEN, r.text)
        self.assertNotIn("name='k'", r.text)

    def test_cookie_session_records_has_no_token(self):
        c = self._cookie_session()
        r = c.get("/admin?tab=records")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(TOKEN, r.text)
        self.assertNotIn("name='k'", r.text)

    def test_bearer_session_has_no_token(self):
        r = self.client.get("/admin", headers=bearer())
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(TOKEN, r.text)
        self.assertNotIn("name='k'", r.text)

    def test_cookie_subpage_has_no_token(self):
        c = self._cookie_session()
        r = c.get("/admin/list/orders")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(TOKEN, r.text)

    def test_cookie_post_redirect_has_no_token(self):
        c = self._cookie_session()
        r = c.post("/admin/switch/kill", data={"on": "0"}, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertNotIn(TOKEN, r.headers.get("location", ""))

    def test_links_still_work_after_suppression(self):
        """去掉 k 后，Cookie 会话里的页签链接必须仍然能打开（不能 401）。"""
        c = self._cookie_session()
        r = c.get("/admin?tab=records")
        self.assertEqual(r.status_code, 200)
        r = c.get("/admin/list/orders")
        self.assertEqual(r.status_code, 200)

    def test_first_page_with_k_still_works(self):
        """向后兼容：只用 ?k= 的第一跳照常可用（此时 HTML 可能带 k —— 见剩余泄露点）。"""
        r = self.client.get("/admin", params={"k": TOKEN})
        self.assertEqual(r.status_code, 200)
        self.assertIn("账户权益", r.text)


# ---------------------------------------------------------------- 4. 全 app 的 hl_guard
class TestFullAppBearer(unittest.TestCase):
    """真实 app.main 组装下：hl_guard 也必须认 Bearer（否则 /api/hl/* 到不了路由）。"""

    def setUp(self):
        if FastAPI is None:
            self.skipTest("fastapi 未安装")
        self.tmp = tempfile.mkdtemp(prefix="bearer_full_")
        self._env = {k: os.environ.get(k) for k in
                     ("DB_PATH", "ENABLE_SCHEDULER", "ADMIN_TOKEN", "TOKEN_SECRET",
                      "BASE_URL", "DRY_RUN", "HL_ACCOUNT_ADDRESS",
                      "HL_DASH_REQUIRE_ADMIN")}
        os.environ["DB_PATH"] = os.path.join(self.tmp, "full.db")
        os.environ["ENABLE_SCHEDULER"] = "false"
        os.environ["ADMIN_TOKEN"] = TOKEN
        os.environ["TOKEN_SECRET"] = "full-secret"
        os.environ["BASE_URL"] = "https://full.example"
        os.environ["DRY_RUN"] = "true"
        os.environ["HL_ACCOUNT_ADDRESS"] = ADDR
        os.environ.pop("HL_DASH_REQUIRE_ADMIN", None)
        import app.hl.info as _hi
        import app.kr.info as _kri

        class _FakeHlInfo:
            def __init__(self, *a, **kw):
                pass

            def snapshot(self, address, mids=None):
                return fake_snapshot(orders_trusted=True)

            def all_mids(self):
                return {}

        self._real = _hi.HlInfo
        self._real_kr = _kri.KrInfo
        _hi.HlInfo = _FakeHlInfo
        # main.get_hl_info() 真正构造的是 Kraken 只读客户端（KrInfo）——不顶掉它，
        # 全 app 的快照会因「凭据未配置」失败，orders_trusted 恒 False（测不到合约）。
        _kri.KrInfo = _FakeHlInfo

    def tearDown(self):
        import app.hl.info as _hi
        import app.kr.info as _kri
        _hi.HlInfo = self._real
        _kri.KrInfo = self._real_kr
        shutil.rmtree(self.tmp, ignore_errors=True)
        for key, val in self._env.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val

    def _app(self):
        importlib.reload(importlib.import_module("app.config"))
        mod = importlib.import_module("app.main")
        importlib.reload(mod)
        return mod

    def test_status_via_bearer_only(self):
        mod = self._app()
        with TestClient(mod.app) as c:
            self.assertEqual(c.get("/api/hl/status").status_code, 401)
            r = c.get("/api/hl/status", headers=bearer())
            self.assertEqual(r.status_code, 200)
            self.assertIs(r.json()["orders_trusted"], True)

    def test_admin_via_bearer_only(self):
        mod = self._app()
        with TestClient(mod.app) as c:
            r = c.get("/admin", headers=bearer())
            self.assertEqual(r.status_code, 200)
            self.assertNotIn(TOKEN, r.text)


if __name__ == "__main__":                # pragma: no cover
    unittest.main()
