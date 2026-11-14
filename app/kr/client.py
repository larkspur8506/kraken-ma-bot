"""Kraken Futures 业务端点封装（auth.py 之上的薄层，便于单测 mock）。

字段名以真实响应为准的规矩不变：这里所有响应字段都按 2026-11-08 的官方文档/
公开探针写入，拿到沙盒 key 后用真 payload 逐个核对（tests/fixtures 注释注明出处）。
"""
from __future__ import annotations

# CHARTS_BASE 仅为向后兼容再导出（URL 现由 `self._base` 跟随客户端 base_url 拼装，
# 不再用常量拼接，否则 demo/自定义地址会打生产端点）。
from .auth import CHARTS_BASE, DEFAULT_BASE, KrClient, KrAuthError  # noqa: F401

API = "/derivatives/api/v3"


class KrApiError(RuntimeError):
    """交易所明确拒绝（HTTP 层通但 result=error），与网络异常分开。"""

    def __init__(self, message: str, status: int = 0, raw=None) -> None:
        super().__init__(message)
        self.status = status
        self.raw = raw


def _raise_if_error(resp: dict, status: int, what: str) -> dict:
    if not isinstance(resp, dict):
        raise KrApiError(f"{what}：响应不是 dict（{str(resp)[:120]}）", status, resp)
    if status >= 400:
        # 401/403（鉴权失败）在 Kraken 可能回 {"error":"...","message":"..."} 而不是
        # {"result":"error"}。**不在这里抛**，下游 `.get("openPositions")` 会得到 None →
        # 空列表 → fail-open（把「读不到」当成「空仓」）。对抗审查 P0。
        detail = (resp.get("error") or resp.get("message")
                  or resp.get("errors") or resp)
        raise KrApiError(f"{what}：HTTP {status} {str(detail)[:300]}", status, resp)
    if resp.get("result") == "error":
        errs = resp.get("errors") or []
        msg = "; ".join(str(e.get("message") or e) for e in errs) if errs \
            else str(resp)[:200]
        raise KrApiError(f"{what}：{msg}", status, resp)
    if resp.get("httpError"):
        raise KrApiError(f"{what}：{resp['httpError']}", status, resp)
    return resp


class KrEndpoints:
    """按端点分组的高层方法。全部返回原始 dict（归一化在 exchange/info 层做）。"""

    def __init__(self, client: KrClient | None = None) -> None:
        self.c = client or KrClient()

    @property
    def _base(self) -> str:
        """服务基址：**跟随客户端的 base_url**（demo / 自定义地址必须生效）。

        旧实现把 `DEFAULT_BASE` / `CHARTS_BASE` 常量拼死在 URL 里，配了
        demo-futures 也照样打生产公网端点 —— 影子盘会读到真账户数据。
        """
        return (getattr(self.c, "base_url", "") or DEFAULT_BASE).rstrip("/")

    @property
    def _charts_base(self) -> str:
        return f"{self._base}/api/charts/v1"

    # ------------------------------------------------------------ private
    def account(self) -> dict:
        """账户快照：端点名是 **/accounts（复数）**。

        对抗审查 P0：旧代码调 `/account`（单数）→ 404，所有账户读取失败，
        执行器 fail-closed 全程停手（不会错单，但系统完全不工作）。
        """
        resp, st = self.c.get_private(f"{API}/accounts")
        return _raise_if_error(resp, st, "accounts")

    def openpositions(self) -> list:
        resp, st = self.c.get_private(f"{API}/openpositions")
        data = _raise_if_error(resp, st, "openpositions")
        rows = data.get("openPositions")
        if not isinstance(rows, list):
            raise KrApiError("openpositions：缺 openPositions 或类型不是 list", st, resp)
        return rows

    def openorders(self) -> list:
        """挂单：缺键 / 类型不是 list 一律抛错（fail-closed）。

        旧写法 `.get("openOrders") or []` 把「降级响应/被截断的响应」当成**可信空表**：
        对账据此判「没有任何止损」→ 补挂一张重复止损，或（探针）报 stops_bad 授权
        人工补单。空盘必须以 `"openOrders": []` 的形式出现，缺键不是「没有挂单」。
        """
        resp, st = self.c.get_private(f"{API}/openorders")
        data = _raise_if_error(resp, st, "openorders")
        rows = data.get("openOrders")
        if not isinstance(rows, list):
            raise KrApiError(
                f"openorders：响应缺 openOrders 或类型不是 list（{type(rows).__name__}），"
                f"按不可信处理", st, resp)
        return rows

    def fills(self, last_fill_time: str | int | None = None) -> list:
        """成交分页（官方语义）：不传 = 最近 100 条（带 realized_pnl）；
        传 `lastFillTime`（原始 ISO 字符串）= 该时间**之前**的 100 条。
        """
        params = {"lastFillTime": str(last_fill_time)} if last_fill_time else {}
        resp, st = self.c.get_private(f"{API}/fills", params)
        return _raise_if_error(resp, st, "fills").get("fills") or []

    def sendorder(self, params: dict) -> dict:
        """下单：**写端点不重试**（retries=1）——重发可能就是第二张真实订单。"""
        resp, st = self.c.post(f"{API}/sendorder", params, retries=1)
        return _raise_if_error(resp, st, "sendorder")

    def cancelorder(self, order_id: str) -> dict:
        """撤单：新版参数名是 `order_id`（旧代码传 orderId → 交易所不认，撤单静默失败）。"""
        resp, st = self.c.post(f"{API}/cancelorder", {"order_id": str(order_id)})
        return _raise_if_error(resp, st, "cancelorder")

    def cancelallorders(self, symbol: str = "") -> dict:
        """撤全部挂单；带 symbol 时只撤该合约（Kraken 支持 per-symbol 撤单）。"""
        params = {"symbol": symbol} if symbol else {}
        resp, st = self.c.post(f"{API}/cancelallorders", params)
        return _raise_if_error(resp, st, "cancelallorders")

    # ------------------------------------------------------------ public
    def instruments(self) -> list:
        resp, st = self.c.get_public(f"{self._base}{API}/instruments")
        return _raise_if_error(resp, st, "instruments").get("instruments") or []

    def tickers(self) -> list:
        resp, st = self.c.get_public(f"{self._base}{API}/tickers")
        return _raise_if_error(resp, st, "tickers").get("tickers") or []

    def charts(self, trade_symbol: str, res: str, from_s: int, to_s: int,
              tick: str = "trade") -> tuple:
        """K 线：path 形式 /trade/{sym}/{res}，from/to 单位是**秒**（实测）。

        返回 (candles, more_candles)；candle 的 time 是毫秒、OHLCV 是字符串。
        单页上限 2000 根——翻页逻辑在 info.candles/charts_history 里做。
        """
        url = f"{self._charts_base}/{tick}/{trade_symbol}/{res}?from={int(from_s)}&to={int(to_s)}"
        resp, st = self.c.get_public(url)
        data = _raise_if_error(resp, st, "charts")
        return data.get("candles") or [], bool(data.get("more_candles"))

    def charts_history(self, trade_symbol: str, res: str, from_s: int, to_s: int,
                       max_pages: int = 40) -> list:
        """从 from_s 起向前翻页取满区间（2000 根/页 + more_candles 推进）。"""
        out: list = []
        frm = int(from_s)
        for _ in range(max(1, max_pages)):
            candles, more = self.charts(trade_symbol, res, frm, int(to_s))
            if not candles:
                break
            out.extend(candles)
            nxt = int(candles[-1]["time"] // 1000) + 1
            if not more or nxt >= int(to_s):
                break
            frm = nxt
        return out
