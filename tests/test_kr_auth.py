"""Kraken auth/client 单测：签名算法、端点名/参数名、fail-closed。

签名口径以 **Kraken 官方 REST CLI 样例 + 2026-11-08 真实 key 实测**为准：
base64(HMAC-SHA512(secret, sha256(payload + "/api/v3/" + method)))，无 Nonce 头。
"""
import unittest

from app.kr.auth import KrClient, KrAuthError, sign
from app.kr.client import KrApiError, KrEndpoints


class TestSign(unittest.TestCase):
    def test_known_answer(self):
        """固定向量：任何算法漂移都会在这里炸（旧 nonce+path/hex 实现必挂）。"""
        import base64
        secret = base64.b64encode(b"0123456789abcdef0123456789abcdef").decode()
        got = sign(secret, b"size=1&symbol=PF_XBTUSD", "sendorder")
        self.assertEqual(
            got,
            "iYeEMdxSJLkfXMN9UA5eUtFiCi+5aGyNf6bLL6Wok4fbakQXvEnsov0OkECNWekNVzEjvDzd7gIX3B5KsfI0pA==")

    def test_empty_payload_get(self):
        import base64
        secret = base64.b64encode(b"k" * 32).decode()
        a = sign(secret, b"", "accounts")
        b = sign(secret, b"", "accounts")
        self.assertEqual(a, b)
        self.assertTrue(a.endswith("="))          # base64，不是 hex

    def test_bad_secret_raises(self):
        with self.assertRaises(KrAuthError):
            sign("!!!not-base64!!!", b"", "accounts")


class TestHeaders(unittest.TestCase):
    def test_no_nonce_header_and_base64_auth(self):
        import base64
        c = KrClient(api_key="pubkey",
                     secret_b64=base64.b64encode(b"x" * 32).decode())
        h = c._auth_headers("accounts", b"")
        self.assertEqual(h["APIKey"], "pubkey")
        self.assertNotIn("Nonce", h)
        self.assertTrue(h["Authent"].endswith("="))

    def test_method_extracted_from_path(self):
        self.assertEqual(KrClient._method_of("/derivatives/api/v3/accounts"), "accounts")
        self.assertEqual(KrClient._method_of("/derivatives/api/v3/sendorder"), "sendorder")


class TestRetryPolicy(unittest.TestCase):
    """A1/A2 回归：**写端点不重试**（POST 只发一次），GET 仍 3 次。

    Kraken 的 sendorder 重发 = 第二张真单；网络抖动时「宁可失败让人工/下一轮对账兜」
    也比「多下一张」便宜。
    """

    def _client(self):
        import base64
        return KrClient(api_key="k", secret_b64=base64.b64encode(b"x" * 32).decode())

    def test_post_defaults_to_single_attempt(self):
        c = self._client()
        seen = {}

        def fake(url, *, method="GET", data=b"", headers, retries=3):
            seen["retries"] = retries
            return {"result": "success"}, 200

        c._request = fake
        c.post("/derivatives/api/v3/sendorder", {"symbol": "PF_XBTUSD"})
        self.assertEqual(seen["retries"], 1)

    def test_post_retries_kwarg_passthrough(self):
        c = self._client()
        seen = {}

        def fake(url, *, method="GET", data=b"", headers, retries=3):
            seen["retries"] = retries
            return {"result": "success"}, 200

        c._request = fake
        c.post("/derivatives/api/v3/sendorder", {}, retries=3)
        self.assertEqual(seen["retries"], 3)

    def test_get_private_keeps_three_attempts(self):
        c = self._client()
        seen = {}

        def fake(url, *, method="GET", data=b"", headers, retries=3):
            seen["retries"] = retries
            return {"accounts": {}}, 200

        c._request = fake
        c.get_private("/derivatives/api/v3/accounts")
        self.assertEqual(seen["retries"], 3)

    def test_write_failure_is_not_resent(self):
        """真实调用一次（urlopen 抛网络错）：POST 只能有一次尝试。"""
        import urllib.error
        import urllib.request as urlreq

        c = self._client()
        calls = []

        class _Boom:
            def __call__(self, req, timeout=None):
                calls.append(req.full_url)
                raise urllib.error.URLError("network down")

        orig = urlreq.urlopen
        urlreq.urlopen = _Boom()
        try:
            resp, st = c.post("/derivatives/api/v3/sendorder", {"a": "b"})
        finally:
            urlreq.urlopen = orig
        self.assertEqual(len(calls), 1, "写端点重发会变成第二张真单")
        self.assertEqual(st, 0)
        self.assertIn("httpError", resp)


class TestEndpointPaths(unittest.TestCase):
    def test_account_uses_plural_accounts(self):
        calls = []

        class C:
            def get_private(self, path, params=None):
                calls.append(("GET", path, params))
                return {"accounts": {}}, 200

        KrEndpoints(C()).account()
        self.assertTrue(calls[0][1].endswith("/v3/accounts"), calls)

    def test_cancelorder_uses_order_id_param(self):
        calls = []

        class C:
            def post(self, path, params=None):
                calls.append((path, params))
                return {"cancelStatus": {}}, 200

        KrEndpoints(C()).cancelorder("abc-1")
        self.assertEqual(calls[0][1], {"order_id": "abc-1"})

    def test_fills_cursor_passed_through(self):
        calls = []

        class C:
            def get_private(self, path, params=None):
                calls.append(params)
                return {"fills": []}, 200

        ep = KrEndpoints(C())
        ep.fills("2026-11-08T08:27:31.814Z")
        ep.fills(None)
        self.assertEqual(calls, [{"lastFillTime": "2026-11-08T08:27:31.814Z"}, {}])


class TestFailClosed(unittest.TestCase):
    def test_401_raises(self):
        class C:
            def get_private(self, path, params=None):
                return {"error": "Invalid API key"}, 401

        with self.assertRaises(KrApiError):
            KrEndpoints(C()).openpositions()

    def test_result_error_raises(self):
        class C:
            def get_private(self, path, params=None):
                return {"result": "error", "errors": [{"message": "boom"}]}, 200

        with self.assertRaises(KrApiError):
            KrEndpoints(C()).openorders()


if __name__ == "__main__":
    unittest.main()
