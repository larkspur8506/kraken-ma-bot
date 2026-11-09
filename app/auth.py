"""管理鉴权：一个口径，三种凭据。

统一口径是 Authorization Bearer 认证：凭据放在请求头里，不进 URL。
旧的 ``?k=`` 查询参数与 ``cma_admin`` Cookie 保留 —— 面板里的零 JS 链接、已发消息里
的旧书签、runbook 里的 curl 都还在用它们，回滚时也不需要改调用方。

**权限没有变化**：三条路径认的都是同一个 ADMIN_TOKEN 真值，只有「凭据从哪来」不同。
**任何分支都不许把收到的凭据写进日志/异常文案**（R4 前的 P1-C 策略不变）。
"""
from __future__ import annotations

import hmac
from typing import Any, Optional

#: 面板 Cookie 名（与 admin_ui / main.hl_guard 一致）
COOKIE_NAME = "cma_admin"
#: 后向兼容的 query 参数名
QUERY_KEY = "k"

#: 凭据来源（`auth_source` 的返回值）；只有 cookie/bearer 时才认为
#: 「这个会话下次请求也会自动带上凭据」，渲染 HTML 时可以不带 k。
SRC_BEARER = "bearer"
SRC_QUERY = "query"
SRC_COOKIE = "cookie"


def _header(request: Any, name: str) -> str:
    try:
        return str(request.headers.get(name) or "")
    except Exception:                                             # noqa: BLE001
        return ""


def bearer_token(request: Any) -> Optional[str]:
    """从 Authorization 头取 Bearer 真值；头缺失/非 Bearer/空值一律 None。"""
    if request is None:
        return None
    raw = _header(request, "authorization").strip()
    if not raw:
        return None
    parts = raw.split(None, 1)
    if len(parts) != 2 or parts[0].strip().lower() != "bearer":
        return None
    token = parts[1].strip()
    return token or None


def query_token(request: Any) -> str:
    if request is None:
        return ""
    try:
        return str(request.query_params.get(QUERY_KEY) or "")
    except Exception:                                             # noqa: BLE001
        return ""


def cookie_token(request: Any) -> str:
    if request is None:
        return ""
    try:
        return str(request.cookies.get(COOKIE_NAME) or "")
    except Exception:                                             # noqa: BLE001
        return ""


def matches(supplied: Any, token: Any) -> bool:
    """常量时间比较；两边都非空才算命中（空串 = 没提供，不是「空 token 也算对」）。"""
    s, t = str(supplied or ""), str(token or "")
    if not s or not t:
        return False
    return hmac.compare_digest(s.encode("utf-8"), t.encode("utf-8"))


def bearer_ok(request: Any, token: str) -> bool:
    return matches(bearer_token(request), token)


def cookie_ok(request: Any, token: str) -> bool:
    return matches(cookie_token(request), token)


def auth_source(request: Any, token: str, k: Optional[str] = None) -> Optional[str]:
    """返回命中的凭据来源；都不命中返回 None（调用方负责 401）。

    顺序 = 统一口径优先：Bearer → 显式 k → query k → Cookie。
    这里用「任一命中即通过」而不是「第一个非空值必须命中」：浏览器里留着一个过期
    的 ``?k=`` 时，不该把有效的 Cookie 会话打成 401（旧写法的这个毛刺在审计里
    出现过）。权限集合没变 —— 任何一条都得是 ADMIN_TOKEN 真值。
    """
    if bearer_ok(request, token):
        return SRC_BEARER
    if matches(k, token):
        return SRC_QUERY
    if matches(query_token(request), token):
        return SRC_QUERY
    if cookie_ok(request, token):
        return SRC_COOKIE
    return None


def is_admin(request: Any, token: str, k: Optional[str] = None) -> bool:
    if not token:
        return False
    return auth_source(request, token, k) is not None


def keeps_credential_in_url(request: Any, token: str) -> bool:
    """这个会话的下一次请求还会自动带凭据吗（Cookie/Bearer）？

    True ⇒ 渲染的链接/隐藏域不必再带 ``k``，token 不该出现在 HTML 里。
    """
    return cookie_ok(request, token) or bearer_ok(request, token)
