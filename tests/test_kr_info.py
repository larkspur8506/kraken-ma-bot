"""KrInfo 单测（PR-1）：snapshot/fills/订单映射。fixture 按官方文档字段手写，
拿到沙盒 key 后用真实 payload 核对（TODO(sandbox)，按仓规矩先抓真 payload 再改断言）。"""
import unittest

from app.kr.info import KrInfo, KrInfoError


class _StubEp:
    def __init__(self, account=None, positions=None, orders=None, fills=None,
                 instruments=None, tickers=None):
        self._account = account or {}
        self._positions = positions or []
        self._orders_l = orders or []
        self._fills = fills or []
        self._instruments = instruments or []
        self._tickers = tickers or []

    def account(self):
        return self._account

    def openpositions(self):
        return self._positions

    def openorders(self):
        return self._orders_l

    def fills(self, last_fill_time_ms=0):
        return list(self._fills)

    def instruments(self):
        return self._instruments

    def tickers(self):
        return self._tickers


ACC = {"pv": 1000.0, "av": 800.0, "im": 200.0}


def _mk(ep):
    return KrInfo(endpoints=ep)


class TestSnapshot(unittest.TestCase):
    def test_full_shape(self):
        ep = _StubEp(account=ACC, positions=[{
            "symbol": "PF_XBTUSD", "side": "long", "netQty": 0.01,
            "avgFillPrice": 74000.0, "markPrice": 75000.0,
            "calculatedUnrealizedPnl": 10.0,
            "liquidationThresholdPrice": 60000.0}])
        snap = _mk(ep).snapshot("kr")
        self.assertAlmostEqual(snap["equity_total"], 1000.0)
        self.assertAlmostEqual(snap["equity"], 1000.0)
        self.assertAlmostEqual(snap["withdrawable"], 800.0)
        self.assertAlmostEqual(snap["margin_used"], 200.0)
        self.assertTrue(snap["unified"])
        self.assertTrue(snap["orders_trusted"])
        p = snap["positions"][0]
        self.assertEqual(p["coin"], "BTC")
        self.assertEqual(p["side"], 1)
        self.assertAlmostEqual(p["size"], 0.01)
        self.assertAlmostEqual(p["position_value"], 750.0)
        self.assertFalse(p["isolated"])
        self.assertAlmostEqual(p["unrealized_pnl"], 10.0)

    def test_openorders_trigger_mapping(self):
        ep = _StubEp(account=ACC, orders=[{
            "orderId": "abc-1", "symbol": "PF_ETHUSD", "side": "sell",
            "qty": 0.5, "limitPrice": 2100.0, "stopPrice": 2050.0,
            "reduceOnly": True, "timestamp": 1700000000000}])
        orders = _mk(ep).snapshot("kr")["open_orders"]
        o = orders[0]
        self.assertEqual(o["coin"], "ETH")
        self.assertEqual(o["oid"], "abc-1")
        self.assertEqual(o["is_trigger"], 1)
        self.assertAlmostEqual(o["trigger_px"], 2050.0)
        self.assertEqual(o["reduce_only"], 1)
        self.assertTrue(o["is_buy"] is False)


class TestFills(unittest.TestCase):
    def test_open_then_close(self):
        fills = [
            {"symbol": "PF_XBTUSD", "side": "buy", "size": 0.01, "price": 74000.0,
             "fillTime": 1700000000000, "seq": 1},
            {"symbol": "PF_XBTUSD", "side": "sell", "size": 0.01, "price": 75000.0,
             "fillTime": 1700003600000, "seq": 2},
        ]
        out = _mk(_StubEp(fills=fills)).user_fills("kr")
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["dir"], "open long")
        self.assertEqual(out[0]["reduce_only"], 0)
        self.assertEqual(out[1]["dir"], "close long")
        self.assertEqual(out[1]["reduce_only"], 1)
        self.assertAlmostEqual(out[1]["closedPnl"], 10.0)     # (75000-74000)*0.01
        self.assertAlmostEqual(out[0]["fee"], 74000 * 0.01 * 0.0005)  # 估算口径

    def test_flip(self):
        fills = [
            {"symbol": "PF_ETHUSD", "side": "buy", "size": 1.0, "price": 2000.0,
             "fillTime": 1, "seq": 1},
            {"symbol": "PF_ETHUSD", "side": "sell", "size": 2.0, "price": 2100.0,
             "fillTime": 2, "seq": 2},
        ]
        out = _mk(_StubEp(fills=fills)).user_fills("kr")
        self.assertEqual(out[1]["dir"], "long > short")
        self.assertEqual(out[1]["reduce_only"], 1)
        self.assertAlmostEqual(out[1]["closedPnl"], 100.0)    # 平掉 1 ETH @2100 vs 2000


class TestSpecs(unittest.TestCase):
    def test_specs_from_instruments(self):
        ep = _StubEp(instruments=[
            {"symbol": "PF_XBTUSD", "tickSize": 1, "minimumTradeSize": 0.0001},
            {"symbol": "PF_QQQXUSD", "tickSize": 0.1, "minimumTradeSize": 0.1},
        ])
        specs = _mk(ep).specs()
        self.assertEqual(specs["BTC"]["szDecimals"], 4)
        self.assertEqual(specs["QQQ"]["szDecimals"], 1)
        self.assertIn("maxLeverage", specs["BTC"])

    def test_unknown_symbol_ignored(self):
        ep = _StubEp(instruments=[{"symbol": "PF_LTCUSD", "tickSize": 0.1}])
        self.assertEqual(_mk(ep).specs(), {})


class TestFailClosed(unittest.TestCase):
    def test_account_zero_raises(self):
        with self.assertRaises(KrInfoError):
            _mk(_StubEp(account={"pv": 0.0})).account_equity("kr")

    def test_orders_failure_untrusted(self):
        class _Bad(_StubEp):
            def openorders(self):
                raise KrInfoError("boom")
        snap = _mk(_Bad(account=ACC)).snapshot("kr")
        self.assertFalse(snap["orders_trusted"])
        self.assertEqual(snap["open_orders"], [])


if __name__ == "__main__":
    unittest.main()
