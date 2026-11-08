"""Kraken Futures 业务端点封装（auth.py 之上的薄层，便于单测 mock）。

字段名以真实响应为准的规矩不变：这里所有响应字段都按 2026-11-08 的官方文档/
公开探针写入，拿到沙盒 key 后用真 payload 逐个核对（tests/fixtures 注释注明出处）。
"""
from __future__ import annotations

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

    # ------------------------------------------------------------ private
    def account(self) -> dict:
        resp, st = self.c.get_private(f"{API}/account")
        return _raise_if_error(resp, st, "account")

    def openpositions(self) -> list:
        resp, st = self.c.get_private(f"{API}/openpositions")
        return _raise_if_error(resp, st, "openpositions").get("openPositions") or []

    def openorders(self) -> list:
        resp, st = self.c.get_private(f"{API}/openorders")
        return _raise_if_error(resp, st, "openorders").get("openOrders") or []

    def fills(self, last_fill_time_ms: int = 0) -> list:
        """成交分页：lastFillTime=上次最大 fillTime，交易所按时间递增返回。"""
        params = {"lastFillTime": str(int(last_fill_time_ms))} if last_fill_time_ms else {}
        resp, st = self.c.get_private(f"{API}/fills", params)
        return _raise_if_error(resp, st, "fills").get("fills") or []

    def sendorder(self, params: dict) -> dict:
        resp, st = self.c.post(f"{API}/sendorder", params)
        return _raise_if_error(resp, st, "sendorder")

    def cancelorder(self, order_id: str) -> dict:
        resp, st = self.c.post(f"{API}/cancelorder", {"orderId": str(order_id)})
        return _raise_if_error(resp, st, "cancelorder")

    def cancelallorders(self) -> dict:
        resp, st = self.c.post(f"{API}/cancelallorders", {})
        return _raise_if_error(resp, st, "cancelallorders")

    # ------------------------------------------------------------ public
    def instruments(self) -> list:
        resp, st = self.c.get_public(f"{DEFAULT_BASE}{API}/instruments")
        return _raise_if_error(resp, st, "instruments").get("instruments") or []

    def tickers(self) -> list:
        resp, st = self.c.get_public(f"{DEFAULT_BASE}{API}/tickers")
        return _raise_if_error(resp, st, "tickers").get("tickers") or []

    def charts(self, trade_symbol: str, res: str, from_s: int, to_s: int,
              tick: str = "trade") -> tuple:
        """K 线：path 形式 /trade/{sym}/{res}，from/to 单位是**秒**（实测）。

        返回 (candles, more_candles)；candle 的 time 是毫秒、OHLCV 是字符串。
        单页上限 2000 根——翻页逻辑在 info.candles/charts_history 里做。
        """
        url = f"{CHARTS_BASE}/{tick}/{trade_symbol}/{res}?from={int(from_s)}&to={int(to_s)}"
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
