"""Kraken Futures REST 认证与 HTTP 客户端（stdlib only，与 app/hl/info.py 同风格）。

认证（**按官方 CLI 样例实测通过**，2026-11-08 用真实 key 验过）：
- 头：``APIKey`` / ``Authent``（**没有 Nonce**）。
- 计算：``sha256(payload + "/api/v3/" + method)`` → ``HMAC-SHA512(key=base64decode(secret),
  msg=上面的 digest)`` → **base64** 编码，放进 ``Authent`` 头。
  - POST 的 payload = form-urlencoded body
  - GET 的 payload = query string（不含 "?"；无参数时为空串）
  - method = 端点名（``accounts`` / ``sendorder`` …），不含路径。
- 服务端地址：``https://futures.kraken.com/derivatives/api/v3/<method>``。

⚠️ 不要按「nonce + path」的旧文档实现：那样算出来的是 hex，且服务端一律回
``authenticationError``（2026-11-08 实测四种变体全灭，换成上面的算法立刻 200）。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

DEFAULT_BASE = "https://futures.kraken.com"
CHARTS_BASE = "https://futures.kraken.com/api/charts/v1"
API_SIGN_PREFIX = b"/api/v3/"
TIMEOUT_S = 15.0


class KrAuthError(RuntimeError):
    pass


class Nonce:  # 保留名字仅为向后兼容；Kraken v3 签名不用 nonce
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last = 0

    def next(self) -> int:
        with self._lock:
            now = int(time.time() * 1000)
            self._last = max(self._last + 1, now)
            return self._last


def sign(secret_b64: str, payload: bytes, method: str) -> str:
    """Authent = base64(HMAC-SHA512(base64decode(secret),
    sha256(payload + "/api/v3/" + method)))。

    payload：POST 是 urlencoded body，GET 是 query string（空则 b""）。
    method：端点名（"accounts" / "sendorder" …），不带路径。
    """
    try:
        key = base64.b64decode(secret_b64, validate=True)
    except Exception as e:                                       # noqa: BLE001
        raise KrAuthError(f"secret 不是合法 base64：{type(e).__name__}") from e
    digest = hashlib.sha256(payload + API_SIGN_PREFIX + method.encode()).digest()
    return base64.b64encode(hmac.new(key, digest, hashlib.sha512).digest()).decode()


class KrClient:
    """REST 客户端：私有端点走签名，公开端点免鉴权。网络错误重试 3 次（1/3/9s 退避）。

    返回 ``(payload_dict, http_status)``；HTTP 4xx/5xx 也返回（body 尽力解析），
    让上层按业务错误处理，而不是把「交易所明确拒绝」和网络抖动混为一谈。
    """

    def __init__(self, api_key: str = "", secret_b64: str = "",
                 base_url: str = DEFAULT_BASE, timeout: float = TIMEOUT_S) -> None:
        self.api_key = (api_key or "").strip()
        self.secret_b64 = (secret_b64 or "").strip()
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._nonce = Nonce()

    # ------------------------------------------------------------- internals
    @staticmethod
    def _retry_delays() -> tuple:
        return (1.0, 3.0, 9.0)

    def _request(self, url: str, *, method: str = "GET", data: bytes = b"",
                 headers: dict, retries: int = 3) -> tuple:
        err_txt = ""
        status = 0
        for attempt in range(max(1, retries)):
            req = urllib.request.Request(url, data=data if method == "POST" else None,
                                         method=method, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode("utf-8")), int(resp.status)
            except urllib.error.HTTPError as e:
                body = ""
                try:
                    body = e.read().decode("utf-8")[:500]
                except Exception:                                # noqa: BLE001
                    pass
                # 4xx 是交易所的明确答复（签名错/参数错），不重试
                if 400 <= e.code < 500:
                    try:
                        return json.loads(body), int(e.code)
                    except Exception:                            # noqa: BLE001
                        return {"httpError": body}, int(e.code)
                err_txt, status = f"HTTP {e.code}", int(e.code)
            except Exception as e:                               # noqa: BLE001
                err_txt, status = f"{type(e).__name__}: {e}", 0
            if attempt < retries - 1:
                time.sleep(self._retry_delays()[min(attempt, 2)])
        return {"httpError": err_txt or "unknown"}, status

    def _auth_headers(self, method: str, payload: bytes) -> dict:
        if not self.api_key or not self.secret_b64:
            raise KrAuthError("Kraken API key/secret 未配置")
        return {"APIKey": self.api_key,
                "Authent": sign(self.secret_b64, payload, method),
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "kraken-ma-bot/0.1"}

    # ------------------------------------------------------------- private API
    @staticmethod
    def _method_of(path: str) -> str:
        """`/derivatives/api/v3/sendorder` → `sendorder`（签名只用端点名）。"""
        return path.rstrip("/").rsplit("/", 1)[-1]

    def post(self, path: str, params: dict | None = None) -> tuple:
        url = f"{self.base_url}{path}"
        body = urllib.parse.urlencode(params or {})
        headers = self._auth_headers(self._method_of(path), body.encode())
        return self._request(url, method="POST", data=body.encode(), headers=headers)

    def get_private(self, path: str, params: dict | None = None) -> tuple:
        qs = urllib.parse.urlencode(params or {})
        url = f"{self.base_url}{path}" + (f"?{qs}" if qs else "")
        headers = self._auth_headers(self._method_of(path), qs.encode())
        return self._request(url, headers=headers)

    # ------------------------------------------------------------- public API
    def get_public(self, url: str, retries: int = 4) -> tuple:
        headers = {"User-Agent": "kraken-ma-bot/0.1", "Accept": "application/json"}
        return self._request(url, headers=headers, retries=retries)
