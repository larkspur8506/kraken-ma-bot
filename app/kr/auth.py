"""Kraken Futures REST 认证与 HTTP 客户端（stdlib only，与 app/hl/info.py 同风格）。

认证（Kraken Futures 官方文档口径）：
- 头：``APIKey`` / ``Nonce``（毫秒，进程内单调递增）/ ``Authent``。
- POST：Authent = HMAC-SHA512(key=base64.b64decode(secret),
  msg=str(nonce) + urlencode(body)) 的 hex。
- GET：msg = str(nonce) + request_path + ('?' + query 如果带参)。

TODO(sandbox)：拿到沙盒 key 后第一件事就是用真实响应核对 GET 的 msg 拼法
（官方文档对 GET 的描述有歧义：path+query 还是仅 query）——这是 PR-1 里
唯一「文档读不准、必须实测」的点，签错会 401，不会错单。
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
TIMEOUT_S = 15.0


class KrAuthError(RuntimeError):
    pass


class Nonce:
    """毫秒 nonce，进程内单调递增（同毫秒重号会被交易所拒）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last = 0

    def next(self) -> int:
        with self._lock:
            now = int(time.time() * 1000)
            self._last = max(self._last + 1, now)
            return self._last


def sign(secret_b64: str, nonce: int, msg: str) -> str:
    """Authent = HMAC-SHA512(base64decode(secret), str(nonce) + msg) 的 hex。"""
    try:
        key = base64.b64decode(secret_b64, validate=True)
    except Exception as e:                                       # noqa: BLE001
        raise KrAuthError(f"secret 不是合法 base64：{type(e).__name__}") from e
    mac = hmac.new(key, f"{nonce}{msg}".encode(), hashlib.sha512)
    return mac.hexdigest()


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

    def _auth_headers(self, nonce: int, msg: str) -> dict:
        if not self.api_key or not self.secret_b64:
            raise KrAuthError("Kraken API key/secret 未配置")
        return {"APIKey": self.api_key,
                "Nonce": str(nonce),
                "Authent": sign(self.secret_b64, nonce, msg),
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "kraken-ma-bot/0.1"}

    # ------------------------------------------------------------- private API
    def post(self, path: str, params: dict | None = None) -> tuple:
        url = f"{self.base_url}{path}"
        body = urllib.parse.urlencode(params or {})
        nonce = self._nonce.next()
        headers = self._auth_headers(nonce, body)
        return self._request(url, method="POST", data=body.encode(), headers=headers)

    def get_private(self, path: str, params: dict | None = None) -> tuple:
        qs = urllib.parse.urlencode(params or {})
        url = f"{self.base_url}{path}" + (f"?{qs}" if qs else "")
        nonce = self._nonce.next()
        msg = path + (f"?{qs}" if qs else "")
        headers = self._auth_headers(nonce, msg)
        return self._request(url, headers=headers)

    # ------------------------------------------------------------- public API
    def get_public(self, url: str, retries: int = 4) -> tuple:
        headers = {"User-Agent": "kraken-ma-bot/0.1", "Accept": "application/json"}
        return self._request(url, headers=headers, retries=retries)
