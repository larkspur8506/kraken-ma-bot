"""KrEndpoints / KrClient 层回归：端点名、参数名、鉴权失败的 fail-closed。

对抗审查（2026-11-08）：端点名与参数名按 HL 习惯猜会 404 / 静默失败，
这些断言钉住 Kraken OpenAPI 的真实形态。
"""
import unittest

from app.kr.client import KrApiError, KrEndpoints


class _FakeClient:
    def __init__(self, resp=None, status=200):
        self.resp = resp if resp is not None else {"result": "success"}
        self.status = status
        self.path = None
        self.params = None

    def get_private(self, path, params=None):
        self.path, self.params = path, params
        return self.resp, self.status

    def post(self, path, params=None):
        self.path, self.params = path, params
        return self.resp, self.status

    def get_public(self, url, retries=4):
        self.path = url
        return self.resp, self.status


class TestEndpointPaths(unittest.TestCase):
    def test_account_uses_plural_accounts(self):
        c = _FakeClient({"accounts": {"flex": {}}})
        KrEndpoints(c).account()
        self.assertTrue(c.path.endswith("/v3/accounts"), c.path)

    def test_cancelorder_uses_order_id_param(self):
        c = _FakeClient({"cancelStatus": {}})
        KrEndpoints(c).cancelorder("abc-1")
        self.assertEqual(c.params, {"order_id": "abc-1"})

    def test_cancelallorders_symbol_scoped(self):
        c = _FakeClient({"cancelStatus": {}})
        KrEndpoints(c).cancelallorders("PF_XBTUSD")
        self.assertEqual(c.params, {"symbol": "PF_XBTUSD"})

    def test_cancelallorders_all_when_no_symbol(self):
        c = _FakeClient({"cancelStatus": {}})
        KrEndpoints(c).cancelallorders()
        self.assertEqual(c.params, {})

    def test_fills_cursor_passed_through(self):
        c = _FakeClient({"fills": []})
        KrEndpoints(c).fills("2026-11-08T08:27:31.814Z")
        self.assertEqual(c.params, {"lastFillTime": "2026-11-08T08:27:31.814Z"})
        KrEndpoints(c).fills(None)
        self.assertEqual(c.params, {})


class TestFailClosed(unittest.TestCase):
    def test_401_raises_not_silent_empty(self):
        c = _FakeClient({"error": "Invalid API key"}, status=401)
        with self.assertRaises(KrApiError) as ctx:
            KrEndpoints(c).openpositions()
        self.assertIn("401", str(ctx.exception))

    def test_403_raises(self):
        c = _FakeClient({"message": "permission denied"}, status=403)
        with self.assertRaises(KrApiError):
            KrEndpoints(c).account()

    def test_result_error_raises(self):
        c = _FakeClient({"result": "error", "errors": [{"message": "boom"}]})
        with self.assertRaises(KrApiError) as ctx:
            KrEndpoints(c).openorders()
        self.assertIn("boom", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
