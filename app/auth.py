"""管理鉴权：一个口径，三种凭据。

统一口径是 Authorization Bearer 认证：凭据放在请求头里，不进 URL。
旧的 ``?k=`` 查询参数与 ``cma_admin`` Cookie 保留 —— 面板里的零 JS 链接、已发消息里
的旧书签、runbook 里的 curl 都还在用它们，回滚时也不需要改调用方。

**权限没有变化**：三条路径认的都是同一个 ADMIN_TOKEN 真值，只有「凭据从哪来」不同。
**任何分支都不许把收到的凭据写进日志/异常文案，也不许回显到响应里**：

- 已鉴权的 HTML / Location 一律不带 ``k``（链接、隐藏域、302 目标都不带）；
- 凭据来自 URL 时，由 :func:`set_session_cookie` 在**同一个响应**上种 HttpOnly
  ``cma_admin``，后续页内链接靠它继续访问。所以「只用 ``?k=`` 的第一跳」照常可用，
  但不再承诺「不支持 Cookie 的浏览器点页内链接还能带着密钥」。
"""
from __future__ import annotations

import hashlib
import hmac
from typing import Any, Optional
from urllib.parse import urlsplit

#: 面板 Cookie 名（与 admin_ui / main.hl_guard 一致）
COOKIE_NAME = "cma_admin"
#: 后向兼容的 query 参数名
QUERY_KEY = "k"
#: 会话 Cookie 有效期（30 天；每次凭 URL 认证时重种，滚动续期）
COOKIE_MAX_AGE = 30 * 86400
#: 写操作的 CSRF 令牌（表单隐藏域 / 请求头，二者都收）
CSRF_FIELD = "csrf"
CSRF_HEADER = "x-csrf-token"
#: CSRF 令牌的派生标签（换标签 = 所有在途表单失效，等价于轮换令牌）
_CSRF_LABEL = b"cma-csrf-v1"

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


def set_session_cookie(response: Any, request: Any, token: str, k: Optional[str] = None) -> Any:
    """凭据来自 URL 时，把 ADMIN_TOKEN 种成 ``cma_admin`` Cookie 后返回 response。

    HTML 与 Location 都不再回显 token（见 admin_ui ``_links_k`` / views ``admin_k``），
    所以「只用 ``?k=`` 的第一跳」必须在这一跳把会话写成 Cookie —— 否则页内链接
    全无凭据，第一跳之后处处 401。只种 Cookie，不把真值写进响应体/头部的可见位置。

    永远返回传进来的那个 response（便于直接 ``return set_session_cookie(...)``）。
    """
    if response is None or not token:
        return response
    if auth_source(request, token, k) != SRC_QUERY:
        # Bearer 每次自带凭据；Cookie 已存在则无需重种。只有 URL 凭据才需要换 Cookie。
        return response
    try:
        scheme = str(getattr(getattr(request, "url", None), "scheme", "") or "")
        fwd = _header(request, "x-forwarded-proto").lower()
    except Exception:                                             # noqa: BLE001
        scheme, fwd = "", ""
    response.set_cookie(COOKIE_NAME, token, httponly=True, max_age=COOKIE_MAX_AGE,
                        secure=(scheme == "https" or fwd == "https"), samesite="lax")
    return response


# ---------------------------------------------------------------- 写操作防护（CSRF / Origin）
# 为什么需要：``cma_admin`` 是 SameSite=Lax 的**环境凭据**。Lax 只挡跨站，挡不住
# **同站不同源**（kma.222677.xyz / 01.222677.xyz / lt.222677.xyz 是同一个 site）。
# 任一兄弟子域被接管，就能带着用户浏览器里的 Cookie 向面板 POST（改 Kraken 凭据、
# 拉闸）——第七轮审核实测「兄弟源 Origin + 测试 Cookie → 303 且 DB 真的变了」。
#
# 口径（2026-11-13 第二轮收紧；三条都不动已有表单）：
#   1. **跨源一律拒**：只要请求带了 Origin/Referer 且不同源（兄弟子域 / null / 畸形），
#      就拒 —— 有效 Bearer、显式 k 都不例外。凭据证明「谁」，来源证明「从哪来」，
#      两件事不能互相顶替。
#   2. **错误 Bearer 不降级**：带了 ``Authorization: Bearer`` 但值不对 → 拒，
#      不再拿同一请求里的 k/Cookie 当后备（旧版只看「头存在」）。
#   3. **Cookie 会话必须带 CSRF**：同源要、**没有 Origin/Referer 也要**。缺来源头
#      只说明「可能不是浏览器」，不能当成「是可信客户端」—— 否则 SameSite 就是唯一
#      一层，兄弟子域被接管时同样能写。
#   有效 Bearer / 显式 k 免 CSRF（自动化脚本没有来源头，且这两个值不会被浏览器
#   自动带上）；表单的隐藏域由 ADMIN_TOKEN 派生（见 :func:`csrf_token`），不含凭据。


def csrf_token(token: str) -> str:
    """由 ADMIN_TOKEN 派生的 CSRF 令牌（无状态、不落库；换 token 即全部失效）。

    这是 HMAC **输出**，不是凭据本身：写进表单隐藏域不泄露 ADMIN_TOKEN。
    """
    t = str(token or "")
    if not t:
        return ""
    return hmac.new(t.encode("utf-8"), _CSRF_LABEL, hashlib.sha256).hexdigest()[:32]


def _host_of(value: str) -> str:
    """``scheme://host:port/path`` → 小写 ``host[:port]``（去掉默认端口）；坏值返回空串。"""
    try:
        parts = urlsplit(str(value or "").strip())
    except ValueError:
        return ""
    host = (parts.hostname or "").lower()
    if not host:
        return ""
    try:
        port = parts.port                      # 非数字端口会抛 ValueError
    except ValueError:
        # 端口解析不出来 = 这个 host 头本身就有问题：当作「不是本站」（fail-closed），
        # 而不是「没有端口」放过去。
        return ""
    try:
        default = 443 if parts.scheme == "https" else (80 if parts.scheme == "http" else 0)
    except ValueError:
        default = 0
    if port and port != default:
        # 显式写出的默认端口（:443/:80）与省略等价：Host 头常带、Origin 常不带，
        # 不归一化会把「同源」判成不同源。
        if port in (443, 80):
            return host
        return f"{host}:{port}"
    return host


def _request_host(request: Any) -> str:
    """本站 host：优先 Host 头（反代下才是外部域名），退回 URL netloc。"""
    host = _header(request, "host")
    if host:
        return _host_of(f"//{host}")
    try:
        return _host_of(str(getattr(request, "url", "") or ""))
    except Exception:                                             # noqa: BLE001
        return ""


def _request_scheme(request: Any) -> str:
    fwd = _header(request, "x-forwarded-proto").split(",")[0].strip().lower()
    if fwd in ("http", "https"):
        return fwd
    try:
        return str(getattr(getattr(request, "url", None), "scheme", "") or "").lower()
    except Exception:                                             # noqa: BLE001
        return ""


def _origin_parts(value: str) -> tuple:
    """``scheme://host[:port]`` → ``(host, scheme)``；畸形/带路径 → ``("", "")``。

    Origin 头按规范只有 scheme/host/port 三段：带 path/query/userinfo 的值不是浏览器
    会发的东西，来源不明一律 fail-closed（当「不是本站」）。
    """
    raw = str(value or "").strip()
    if not raw:
        return "", ""
    try:
        parts = urlsplit(raw)
    except ValueError:
        return "", ""
    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https"):
        return "", ""
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        return "", ""
    if parts.username or parts.password:
        return "", ""
    host = _host_of(raw)
    if not host:
        return "", ""
    return host, scheme


def same_origin(request: Any) -> tuple:
    """请求是否同源。返回 ``(ok, reason)``；``reason == "no-origin"`` 表示没有来源头。

    Origin 优先，缺失时看 Referer（部分客户端/旧浏览器只发 Referer）。判据：

    - ``Origin: null`` / 畸形 Origin / 兄弟子域 / 跨站 → **拒绝**；
    - 同 host 但 https→http 降级 → 拒绝（换协议 = 换源）；反方向不拒：TLS 终止在反代
      而 ``X-Forwarded-Proto`` 没传时，本站 scheme 会显示成 http，把这种部署判成
      跨源等于把面板锁死；
    - 两个头都没有 → ``(True, "no-origin")``：**这只说明「没有可判的来源」**，
      调用方必须按 Cookie 会话继续要 CSRF（见 :func:`write_denied`），不许直接放行。
    """
    origin = _header(request, "origin").strip()
    want = _request_host(request)
    if origin:
        if origin.lower() == "null":
            return False, "Origin: null（沙箱/跨源页面）"
        got, scheme = _origin_parts(origin)
        if not got or not want or got != want:
            return False, f"Origin 与本站不同源（{got or '?'} ≠ {want or '?'}）"
        if _request_scheme(request) == "https" and scheme != "https":
            return False, "Origin 的协议与本站不一致"
        return True, ""
    ref = _header(request, "referer").strip()
    if ref:
        got = _host_of(ref)
        if not got or not want or got != want:
            return False, f"Referer 与本站不同源（{got or '?'} ≠ {want or '?'}）"
        try:
            ref_scheme = urlsplit(ref).scheme.lower()
        except ValueError:
            ref_scheme = ""
        if _request_scheme(request) == "https" and ref_scheme and ref_scheme != "https":
            return False, "Referer 的协议与本站不一致"
        return True, ""
    return True, "no-origin"


def has_bearer_header(request: Any) -> bool:
    """请求里有没有 ``Authorization: Bearer …``（**只看形态，不看值**）。

    用来识别「拿着错误的 Bearer 来试」：这种情况**不许降级**成同一请求里的 k/Cookie
    会话（旧实现只判「头存在」，等于给了一条免检通道）。
    """
    raw = _header(request, "authorization").strip()
    if not raw:
        return False
    parts = raw.split(None, 1)
    return bool(parts) and parts[0].strip().lower() == "bearer"


def write_denied(request: Any, token: str, k: Optional[str] = None,
                 csrf: Optional[str] = None) -> str:
    """Cookie 会话的写操作防护判定：返回**拒绝原因**（空串 = 放行）。

    调用方在鉴权**之后**调用（认证失败仍走 401，不要用本函数代替鉴权）：

        err = authn.write_denied(request, token, k, csrf)
        if err: raise HTTPException(403, "写操作被拒：" + err)

    判定顺序（fail-closed）：

    1. 未认证 → 拒；
    2. 带了 ``Bearer`` 头但值不对 → 拒（**不降级**为同请求里的 k/Cookie 会话）；
    3. 有 Origin/Referer 且不同源（含兄弟子域、``null``、畸形）→ 拒，
       **显式 k / 有效 Bearer 也不例外**：来源不明的请求不该因为「它带了凭据」就免检，
       凭据只能证明「谁」，来源证明的是「从哪来」；
    4. 有效 Bearer / 显式 k → 放行（自动化脚本没有来源头；Bearer 免 CSRF）；
    5. Cookie 会话 → **同源也要 CSRF、没有来源头也要 CSRF**。缺 Origin 只说明
       「可能不是浏览器」，不能当成「是可信客户端」（旧版在这里直接放行，
       等于让 SameSite 成为唯一一层）。
    """
    src = auth_source(request, token, k)
    if src is None:
        return "未认证"
    if src != SRC_BEARER and has_bearer_header(request):
        return "Authorization: Bearer 无效（不会降级为 Cookie/k 会话）"
    ok, why = same_origin(request)
    if not ok:
        return why
    if src in (SRC_BEARER, SRC_QUERY):
        return ""
    supplied = str(csrf or "").strip() or _header(request, CSRF_HEADER).strip()
    if not matches(supplied, csrf_token(token)):
        return "CSRF 令牌缺失或无效（刷新面板后重试）"
    return ""


#: 布尔开关表单字段的合法取值（``on``）。**缺失或未知一律 None**，由调用方 400。
#: 拉闸/MR 开关这类字段「猜错」的代价是解除熔断，绝不拿默认值兜。
ON_TRUE = frozenset({"1", "true", "on", "yes"})
ON_FALSE = frozenset({"0", "false", "off", "no"})


def parse_on_flag(value: Any) -> Optional[bool]:
    """把表单 ``on`` 解析成布尔；缺失/未知返回 None（调用方必须拒绝，不得当解除）。

    旧的 ``on in ("1","true","on")`` + ``Form("0")`` 组合让「漏写 on」等于「解除」，
    一条手滑的 curl 就能解锁熔断（第七轮 #7）。这里两个方向都显式白名单。
    """
    if value is None:
        return None
    raw = str(value).strip().lower()
    if raw in ON_TRUE:
        return True
    if raw in ON_FALSE:
        return False
    return None
