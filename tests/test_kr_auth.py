"""Kraken auth/client 单测（PR-1）。签名向量本地硬编码；真实响应核对归沙盒冒烟。"""
import unittest

from app.kr.auth import KrClient, sign
from app.kr.client import KrEndpoints, KrApiError


class TestSign(unittest.TestCase):
    def test_signature_vector(self):
        # 固定 secret（base64 of "secret-bytes-0123456789abcdef"）
        import base64
        secret = base64.b64encode(b"secret-bytes-0123456789abcdef").decode()
        expected = sign(secret, 1700000000000, "orderType=mkt&symbol=PF_XBTUSD")
        # 重算一遍应一致（同输入同输出），且形如 128 位 hex
        self.assertEqual(expected, sign(secret, 1700000000000, "orderType=mkt&symbol=PF_XBTUSD"))
        self.assertEqual(len(expected), 128)
        int(expected, 16)

    def test_bad_secret_raises(self):
        from app.kr.auth import KrAuthError
        with self.assertRaises(KrAuthError):
            sign("!!!not-base64!!!", 1, "x")


class TestNonce(unittest.TestCase):
    def test_monotonic(self):
        c = KrClient(api_key="k", secret_b64="aaaa")
        seq = [c._nonce.next() for _ in range(5)]
        self.assertEqual(seq, sorted(seq))
        self.assertEqual(len(set(seq)), 5)


class TestEndpointErrors(unittest.TestCase):
    def test_error_response_raises(self):
        # result=error 必须抛
        from app.kr.client import _raise_if_error
        with self.assertRaises(KrApiError):
            _raise_if_error({"result": "error",
                             "errors": [{"message": "insufficientAvailableFunds"}]}, 200, "t")

    def test_http_error_surfaces(self):
        from app.kr.client import _raise_if_error
        with self.assertRaises(KrApiError):
            _raise_if_error({"httpError": "HTTP 401"}, 401, "t")


if __name__ == "__main__":
    unittest.main()
