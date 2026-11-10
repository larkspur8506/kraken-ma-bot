"""应用装配冒烟测试：import app.main 必须成功，且关键路由可调用。

这层专门用来抓「单测覆盖不到、只在装配时才炸」的问题
（例：DB() 少传 path 参数 → 容器启动即崩，而单测全绿）。
需要 fastapi/apscheduler 已安装（CI 里先 pip install -r requirements.txt）。
"""
import importlib
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestAppAssembly(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["DB_PATH"] = os.path.join(self.tmp, "smoke.db")
        os.environ["ENABLE_SCHEDULER"] = "false"
        os.environ["ADMIN_TOKEN"] = "smoke-admin"
        os.environ["TOKEN_SECRET"] = "smoke-secret"
        os.environ["BASE_URL"] = "https://smoke.example"
        os.environ["DRY_RUN"] = "true"

    def test_import_and_routes(self):
        try:
            import fastapi                                            # noqa: F401
        except ImportError:
            self.skipTest("fastapi 未安装（本地环境），CI 里会跑")
        mod = importlib.import_module("app.main")
        importlib.reload(mod)
        h = mod.health()
        self.assertEqual(h["status"], "ok")
        self.assertIn("BTC_USDT", h["symbols"])
        # 关键路由已注册。用 OpenAPI schema 取路径：跨 FastAPI 版本最稳，
        # 也顺带验证 include_router 进来的子路由确实生效（早期版本 app.routes 里是嵌套对象）。
        paths = self._route_paths(mod.app)
        for p in ("/health", "/admin", "/admin/rules", "/admin/logs"):
            self.assertIn(p, paths, f"路由 {p} 未注册")
        # 2026-10-30 PR B：人工确认页退役 —— 路由必须真的不在（防止有人「顺手加回」）
        self.assertNotIn("/c/{pid}", paths, "确认页已删除，不该再注册")
        # HL 监控面板（feat/hl-executor）也要在
        for p in ("/hl", "/api/hl/status", "/api/hl/why"):
            self.assertIn(p, paths, f"HL 路由 {p} 未注册")
        # 到期保护：清空手工兜底的显式入口
        self.assertIn("/admin/hl/agent/fallback/clear", paths)

    def test_home_screen_icon_routes(self):
        """主屏图标 / favicon / manifest：五条路由注册 + 真的能取到字节。"""
        try:
            import fastapi                                            # noqa: F401
            from fastapi.testclient import TestClient
        except ImportError:
            self.skipTest("fastapi 未安装（本地环境），CI 里会跑")
        mod = importlib.import_module("app.main")
        importlib.reload(mod)
        paths = self._route_paths(mod.app)
        for p in ("/apple-touch-icon.png", "/favicon.ico", "/icon-192.png",
                  "/icon-512.png", "/site.webmanifest"):
            self.assertIn(p, paths, f"图标路由 {p} 未注册")
        expect = {
            "/apple-touch-icon.png": "image/png",
            "/icon-192.png": "image/png",
            "/icon-512.png": "image/png",
            "/favicon.ico": "image/x-icon",
            "/site.webmanifest": "application/manifest+json",
        }
        with TestClient(mod.app) as c:
            for p, ctype in expect.items():
                r = c.get(p)
                self.assertEqual(r.status_code, 200, f"{p} 应 200")
                self.assertEqual(r.headers["content-type"], ctype, p)
                self.assertTrue(r.content, f"{p} 返回空内容")
                # 图标缓存一周（反代/CDN 不每次回源）
                self.assertEqual(r.headers.get("cache-control"), "public, max-age=604800", p)
            # manifest 内容要和静态文件一致（name/short_name/颜色/两个 any 图标）
            m = c.get("/site.webmanifest").json()
            self.assertEqual((m["name"], m["short_name"]), ("双均线交易台", "双均线交易台"))
            self.assertEqual(m["start_url"], "/")
            self.assertEqual(m["display"], "browser")
            self.assertEqual((m["background_color"], m["theme_color"]), ("#0d1117", "#0d1117"))
            self.assertEqual(sorted(i["sizes"] for i in m["icons"]), ["192x192", "512x512"])
            self.assertTrue(all(i["purpose"] == "any" for i in m["icons"]))

    def test_agent_check_kraken_no_expiry(self):
        """装配级：Kraken key 无有效期/无代理钱包授权，到期检查固定 ok，
        不得再触碰 app.hl.agent（旧 HL 授权体系整体退役）。"""
        try:
            import fastapi                                            # noqa: F401
        except ImportError:
            self.skipTest("fastapi 未安装")
        mod = importlib.import_module("app.main")
        importlib.reload(mod)
        from app.hl import agent as agent_mod

        def boom(*a, **kw):
            raise AssertionError("Kraken 版 _agent_check 不应再调用 app.hl.agent.check_once")

        orig = agent_mod.check_once
        agent_mod.check_once = boom
        try:
            res = mod._agent_check()
        finally:
            agent_mod.check_once = orig
        self.assertEqual(res.get("level"), "ok")
        self.assertEqual(res.get("source"), "kraken-key-no-expiry")

    def test_kr_renew_validation_and_cache_reset(self):
        """「更新 API」Kraken 版：空参/非 base64 拒绝；成功写 DB + 失效只读客户端。"""
        try:
            import fastapi                                            # noqa: F401
        except ImportError:
            self.skipTest("fastapi 未安装")
        import base64
        mod = importlib.import_module("app.main")
        importlib.reload(mod)
        ok, _ = mod._kr_renew("", "")
        self.assertFalse(ok)
        ok, msg = mod._kr_renew("kr-key-1", "!!!不是base64!!!")
        self.assertFalse(ok)
        self.assertIn("base64", msg)
        mod._hl_info = object()                   # 模拟已有缓存，成功后必须失效
        ok, _ = mod._kr_renew("kr-key-1", base64.b64encode(b"secret-bytes").decode())
        self.assertTrue(ok)
        self.assertIsNone(mod._hl_info, "保存凭据后只读客户端缓存必须失效")
        self.assertEqual(mod.db.get_config_str("kr_api_key"), "kr-key-1")

    def test_hl_keyform_not_mounted_on_kraken(self):
        """B10：Kraken 构建**不挂** /admin/hl/key。

        那个页面写的是 HL 代理钱包私钥，而 `kr_secret()` 的兜底链最后一环是
        `hl_private_key()` —— 从这里录进去的值会变成 Kraken 的 API secret，签名全错，
        页面却显示「✅ 已写入」。Kraken key 的唯一入口是面板「更新 API」（`_kr_renew`）。
        """
        try:
            import fastapi                                            # noqa: F401
        except ImportError:
            self.skipTest("fastapi 未安装")
        mod = importlib.import_module("app.main")
        importlib.reload(mod)
        paths = self._route_paths(mod.app)
        self.assertNotIn("/admin/hl/key", paths,
                         "Kraken 构建不该挂 HL 私钥录入页（会污染 kr_secret 回落链）")
        # 反向断言：Kraken 凭据的正规入口必须在（免得「两边都没了」也算通过）
        self.assertIn("/api/hl/status", paths)

    def test_mr_identity_tracks_kraken_credentials(self):
        """B7：MR 腿指纹 = (mode, base_url, sha256(kr_api_key:kr_secret))。

        旧实现把 `hl_params().private_key`（执行器的 HL 私钥）算进指纹：换 Kraken key
        不会重建腿（旧凭据继续用），而改一个与 MR 无关的 HL 私钥却会重建。
        """
        try:
            import fastapi                                            # noqa: F401
        except ImportError:
            self.skipTest("fastapi 未安装")
        mod = importlib.import_module("app.main")
        importlib.reload(mod)
        old_env = {k: os.environ.get(k) for k in ("KRAKEN_API_KEY", "KRAKEN_API_SECRET",
                                                  "HL_PRIVATE_KEY")}
        os.environ["KRAKEN_API_KEY"] = ""
        os.environ["KRAKEN_API_SECRET"] = ""          # 强制走 DB，避免开发机 env 干扰
        try:
            mod.db.set_config("kr_api_key", "kr-key-a")
            mod.db.set_config("kr_api_secret", "c2VjcmV0LWE=")
            ident = mod._mr_identity()
            self.assertEqual(len(ident), 3)           # (mode, base_url, fp)
            self.assertEqual(ident, mod._mr_identity())          # 稳定
            self.assertNotIn("kr-key-a", repr(ident))            # 不含明文
            # HL 私钥与 MR 腿无关：改它不重建腿
            mod.db.set_config("hl_private_key", "0x" + "ab" * 32)
            self.assertEqual(ident, mod._mr_identity())
            # 换 Kraken key → 指纹变化（下一轮 tick 重建腿）
            mod.db.set_config("kr_api_key", "kr-key-b")
            self.assertNotEqual(ident, mod._mr_identity())
        finally:
            for k, v in old_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_mr_leg_uses_configured_taker_fee(self):
        """B8：HlMrConfig.taker_fee_rate 必须来自配置，不能吃模块默认常量。"""
        try:
            import fastapi                                            # noqa: F401
        except ImportError:
            self.skipTest("fastapi 未安装")
        mod = importlib.import_module("app.main")
        importlib.reload(mod)
        # kr_taker_fee 由 Config 字段提供（env KR_TAKER_FEE 只在进程启动时读一次，
        # 这里直接改 Config 实例，等价于「运维改了费率档」）。
        old_fee = mod.config.kr_taker_fee
        mod.config.kr_taker_fee = 0.0009                         # 故意不等于任何默认值
        try:
            self.assertAlmostEqual(mod.current_config().kr_taker_fee, 0.0009)
            mod.db.set_config("hl_exec_mode", "dry")
            import app.hl.mr as mr_mod
            seen = {}
            orig = mr_mod.HlMrConfig

            class _Rec(orig):                          # 记录装配时传了哪些关键字参数
                def __init__(self, *a, **kw):
                    seen.update(kw)
                    super().__init__(*a, **kw)

            mr_mod.HlMrConfig = _Rec
            try:
                leg = mod._build_mr_leg()
            finally:
                mr_mod.HlMrConfig = orig
            self.assertIsNotNone(leg, "dry 模式应能离线装配 MR 腿")
            self.assertEqual(seen.get("taker_fee_rate"), 0.0009,
                             "taker_fee_rate 必须按关键字参数显式传入")
            self.assertAlmostEqual(leg.config.taker_fee_rate, 0.0009)
            self.assertEqual((leg.config.coin, leg.config.dex), ("QQQ", "kr"))
        finally:
            mod.config.kr_taker_fee = old_fee

    @staticmethod
    def _route_paths(app) -> set:
        return set(app.openapi().get("paths", {}).keys())

    def test_heartbeat_is_silent_beacon_by_default(self):
        """心跳默认只落库不推送（避免与每日巡检重复），但必须留下 kind='heartbeat_local' 供看守判断。"""
        try:
            import fastapi                                            # noqa: F401
        except ImportError:
            self.skipTest("fastapi 未安装（本地环境），CI 里会跑")
        mod = importlib.import_module("app.main")
        importlib.reload(mod)
        pushed = []

        class Boom:
            def send_message(self, text):
                pushed.append(text)
                return True

        mod.notifier = Boom()
        mod.db.set_config("heartbeat_push", 0)
        mod._job_heartbeat()
        self.assertEqual(pushed, [], "默认不应推送心跳")
        kinds = [r["kind"] for r in mod.db.recent_alerts(5)]
        self.assertIn("heartbeat_local", kinds)
        # 打开开关后应恢复推送
        mod.db.set_config("heartbeat_push", 1)
        mod._job_heartbeat()
        self.assertEqual(len(pushed), 1, "开关打开后应推送")
        self.assertIn("heartbeat", [r["kind"] for r in mod.db.recent_alerts(5)])

    def test_config_db_override_path(self):
        try:
            import fastapi                                            # noqa: F401
        except ImportError:
            self.skipTest("fastapi 未安装")
        mod = importlib.import_module("app.main")
        mod.db.set_config("equity", 2000)
        self.assertEqual(mod.current_config().equity, 2000.0)

# 2026-10-30 PR B 移除的用例（全部钉 POST/GET /c/{pid} 的行为）：
#   - test_edit_pages_summary_and_correction（摘要页 + 修改开/平仓表单）
#   - test_auto_closed_position_accepts_real_fill_once（自动关仓后补录真实成交价）
#   - test_confirm_actions_reject_wrong_status（确认动作的状态守卫）
# 确认页与 confirm_post 已删，这些路径不存在了；记录/核对一律走面板「记录」页的 hl_fills。
# 装配层保留上面的「/c/{pid} 不许再注册」反向断言作回归。


if __name__ == "__main__":
    unittest.main(verbosity=2)
