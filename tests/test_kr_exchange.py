"""KrExchange 单测（PR-1）：下单 payload 形状 / sendorder 响应归一化 / dry 语义。

响应形状依据 2026-11-08 官方文档（sendorder/sendStatus/orderEvents）。
"""
import unittest

from app.kr.exchange import (KrExchange, make_cloid, parse_order_response,
                             STATUS_DRY, STATUS_FILLED, STATUS_RESTING)


class _StubInfo:
    def all_mids(self, dex=""):
        return {"BTC": 75000.0}


class _StubEndpoints:
    def __init__(self, resp=None):
        self.resp = resp or {"result": "success"}
        self.calls = []

    def sendorder(self, params):
        self.calls.append(params)
        return self.resp

    def openorders(self):
        return []

    def cancelorder(self, oid):
        self.canceled = oid
        return {"result": "success"}


def _mk_ex(endpoints=None):
    return KrExchange(api_key="k", secret_b64="aaaa", dry_run=False,
                      info=_StubInfo(), endpoints=endpoints or _StubEndpoints())


class TestPlaceOrder(unittest.TestCase):
    def test_ioc_payload_shape(self):
        ep = _StubEndpoints({"result": "success", "sendStatus": {
            "orderId": "o-1",
            "orderEvents": [{"orderEventStatus": "FILLED", "filledQty": 0.001,
                             "filledPricePrice": 74900.0}]}})
        ex = _mk_ex(ep)
        res = ex.place_order("BTC", True, 0.001, 75200.0, cloid=make_cloid("BTC", 1, "entry"))
        self.assertTrue(res["ok"])
        self.assertEqual(res["status"], STATUS_FILLED)
        self.assertAlmostEqual(res["avg_px"], 74900.0)
        self.assertAlmostEqual(res["filled_sz"], 0.001)
        p = ep.calls[0]
        self.assertEqual(p["orderType"], "ioc")
        self.assertEqual(p["symbol"], "PF_XBTUSD")
        self.assertEqual(p["side"], "buy")
        self.assertEqual(p["limitPrice"], "75200.0")
        self.assertEqual(p["reduceOnly"], "false")
        self.assertIn("cliOrdId", p)

    def test_stop_payload_shape(self):
        ep = _StubEndpoints({"result": "success", "sendStatus": {
            "orderId": "o-2", "orderEvents": [{"orderEventStatus": "PLACED"}]}})
        ex = _mk_ex(ep)
        res = ex.place_order("BTC", False, 0.001, 0.0, reduce_only=True,
                             trigger_px=73000.0, cloid=make_cloid("BTC", 2, "stop"))
        self.assertEqual(res["status"], STATUS_RESTING)
        p = ep.calls[0]
        self.assertEqual(p["orderType"], "stp")
        self.assertEqual(p["triggerSignal"], "mark")
        self.assertEqual(p["stopPrice"], "73000.0")
        self.assertEqual(p["reduceOnly"], "true")

    def test_reject_maps_to_error(self):
        ep = _StubEndpoints({"result": "error",
                             "errors": [{"message": "insufficientAvailableFunds"}]})
        ex = _mk_ex(ep)
        res = ex.place_order("BTC", True, 0.001, 75200.0)
        self.assertFalse(res["ok"])
        self.assertIn("insufficientAvailableFunds", res["error"])

    def test_dry_run_sends_nothing(self):
        ep = _StubEndpoints()
        ex = KrExchange(api_key="k", secret_b64="a", dry_run=True, info=_StubInfo(),
                        endpoints=ep)
        res = ex.place_order("BTC", True, 0.001, 75000.0)
        self.assertEqual(res["status"], STATUS_DRY)
        self.assertEqual(ep.calls, [])

    def test_unknown_coin_rejected(self):
        ex = _mk_ex()
        res = ex.place_order("DOGE", True, 1.0, 1.0)
        self.assertFalse(res["ok"])

    def test_partial_fill_weighted_avg(self):
        resp = {"result": "success", "sendStatus": {"orderId": "o-3", "orderEvents": [
            {"orderEventStatus": "FILLED", "filledQty": 1.0, "filledPricePrice": 100.0},
            {"orderEventStatus": "FILLED", "filledQty": 3.0, "filledPricePrice": 200.0}]}}
        out = parse_order_response(resp)
        self.assertEqual(out["status"], STATUS_FILLED)
        self.assertAlmostEqual(out["avg_px"], 175.0)
        self.assertAlmostEqual(out["filled_sz"], 4.0)

    def test_slippage_limit(self):
        ex = _mk_ex()
        self.assertAlmostEqual(ex.slippage_limit("BTC", True, 100.0), 100.3)
        self.assertAlmostEqual(ex.slippage_limit("BTC", False, 100.0), 99.7)

    def test_update_leverage_noop_ok(self):
        res = _mk_ex().update_leverage("BTC", 5, cross=False)
        self.assertTrue(res["ok"])


if __name__ == "__main__":
    unittest.main()
