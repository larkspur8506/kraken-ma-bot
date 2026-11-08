"""KrInfo 单测：snapshot / 持仓 / 挂单 / 成交 / 规格 / 取整。

fixture 形状全部按 **Kraken Derivatives v3 OpenAPI**（docs.kraken.com/openapi/
futures-rest.yaml，2026-11-08 抓取）写，不再按想象写：

- /account      → {"accounts": {"flex": {portfolioValue, availableMargin, initialMargin, …}}}
- /openpositions→ [{symbol, side("long"|"short"), size, price, unrealizedPnl, …}]
- /openorders   → [{order_id, side, unfilledSize, limitPrice, stopPrice, reduceOnly,
                    orderType, receivedTime(ISO)}]
- /fills        → [{fill_id, order_id, fillTime(ISO), price, size, side, sequence_id,
                    realized_pnl}]

对抗审查（2026-11-08）发现旧实现按 netQty/avgFillPrice/orderId/毫秒时间读，
全部读成 0/空 —— 本文件同时充当这些 P0 的回归测试。
"""
import unittest

from app.kr.info import KrInfo, KrInfoError


class _StubEp:
    def __init__(self, account=None, positions=None, orders=None, fills=None,
                 instruments=None, tickers=None):
        self._account = account or {}
        self._positions = positions or []
        self._orders_l = orders or []
        self._fills = fills if fills is not None else []
        self._instruments = instruments or []
        self._tickers = tickers or []
        self.fills_calls = []

    def account(self):
        return self._account

    def openpositions(self):
        return self._positions

    def openorders(self):
        return self._orders_l

    def fills(self, last_fill_time=None):
        """官方语义：不传 = 最近 100；传 = 该 fillTime 之前的 100 条（这里简化实现）。"""
        self.fills_calls.append(last_fill_time)
        if not last_fill_time:
            return list(self._fills[-100:])
        older = [f for f in self._fills if str(f.get("fillTime")) < str(last_fill_time)]
        return older[-100:]

    def instruments(self):
        return self._instruments

    def tickers(self):
        return self._tickers


# 真实 flex 账户形状（官方 AccountsExample 缩略）
ACC = {"result": "success", "accounts": {"flex": {
    "portfolioValue": 1000.0, "balanceValue": 980.0, "marginEquity": 950.0,
    "availableMargin": 800.0, "initialMargin": 200.0, "pnl": 20.0,
    "type": "multiCollateralMarginAccount"}}}


def _mk(ep):
    return KrInfo(endpoints=ep)


class TestSnapshot(unittest.TestCase):
    def test_full_shape(self):
        ep = _StubEp(account=ACC, positions=[{
            "symbol": "PF_XBTUSD", "side": "long", "size": 0.01,
            "price": 74000.0, "unrealizedPnl": 10.0, "unrealizedFunding": -0.5}],
            tickers=[{"symbol": "PF_XBTUSD", "markPrice": 75000.0}])
        snap = _mk(ep).snapshot("kr")
        # P0 回归：equity 必须来自 accounts.flex.portfolioValue
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
        self.assertAlmostEqual(p["mark_px"], 75000.0)
        self.assertAlmostEqual(p["position_value"], 750.0)
        self.assertFalse(p["isolated"])
        self.assertAlmostEqual(p["unrealized_pnl"], 10.0)
        self.assertAlmostEqual(p["funding_since_open"], -0.5)

    def test_short_position_maps_negative_szi(self):
        ep = _StubEp(account=ACC, positions=[{
            "symbol": "PF_ETHUSD", "side": "short", "size": 1.5,
            "price": 2000.0, "unrealizedPnl": -3.0}])
        p = _mk(ep).snapshot("kr")["positions"][0]
        self.assertEqual(p["side"], -1)
        self.assertAlmostEqual(p["mark_px"], 2002.0)      # entry + upnl/(side*size)
        self.assertLess(p["unrealized_pnl"], 0)

    def test_position_without_direction_fails_closed(self):
        ep = _StubEp(account=ACC, positions=[{
            "symbol": "PF_XBTUSD", "size": 0.01, "price": 74000.0}])
        with self.assertRaises(KrInfoError):
            _mk(ep).snapshot("kr")

    def test_openorders_trigger_mapping(self):
        ep = _StubEp(account=ACC, orders=[{
            "order_id": "abc-1", "symbol": "PF_ETHUSD", "side": "sell",
            "unfilledSize": 0.5, "limitPrice": 2100.0, "stopPrice": 2050.0,
            "reduceOnly": True, "orderType": "stop",
            "receivedTime": "2026-11-08T08:27:31.814Z"}])
        o = _mk(ep).snapshot("kr")["open_orders"][0]
        self.assertEqual(o["coin"], "ETH")
        self.assertEqual(o["oid"], "abc-1")
        self.assertEqual(o["is_trigger"], 1)
        self.assertAlmostEqual(o["trigger_px"], 2050.0)
        self.assertAlmostEqual(o["sz"], 0.5)              # unfilledSize → sz
        self.assertEqual(o["reduce_only"], 1)
        self.assertFalse(o["is_buy"])
        # ISO 时间必须解析成毫秒（旧代码恒 0）
        self.assertGreater(o["timestamp"], 1_700_000_000_000)

    def test_openorders_limit_not_trigger(self):
        ep = _StubEp(account=ACC, orders=[{
            "order_id": "abc-2", "symbol": "PF_XBTUSD", "side": "buy",
            "unfilledSize": 0.001, "limitPrice": 75000.0, "reduceOnly": False,
            "orderType": "lmt", "receivedTime": "2026-11-08T08:00:00Z"}])
        o = _mk(ep).snapshot("kr")["open_orders"][0]
        self.assertEqual(o["is_trigger"], 0)
        self.assertIsNone(o["trigger_px"])


class TestFills(unittest.TestCase):
    def test_open_then_close(self):
        fills = [
            {"fill_id": "f1", "order_id": "o1", "symbol": "PF_XBTUSD", "side": "buy",
             "size": 0.01, "price": 74000.0, "fillTime": "2026-11-01T00:00:00.000Z",
             "sequence_id": "1", "realized_pnl": 0.0},
            {"fill_id": "f2", "order_id": "o2", "symbol": "PF_XBTUSD", "side": "sell",
             "size": 0.01, "price": 75000.0, "fillTime": "2026-11-02T00:00:00.000Z",
             "sequence_id": "2", "realized_pnl": 10.0},
        ]
        out = _mk(_StubEp(fills=fills)).user_fills("kr")
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["dir"], "open long")
        self.assertEqual(out[0]["reduce_only"], 0)
        self.assertEqual(out[1]["dir"], "close long")
        self.assertEqual(out[1]["reduce_only"], 1)
        self.assertAlmostEqual(out[1]["closedPnl"], 10.0)     # 交易所真值
        self.assertEqual(out[1]["closedPnl_src"], "exchange")
        self.assertEqual(out[0]["oid"], "o1")
        self.assertAlmostEqual(out[0]["fee"], 74000 * 0.01 * 0.000275)  # 估算口径（Kraken 6 级 taker 2.75bp）
        # ISO 时间必须解析（旧代码 0 → 按时段过滤永远为空）
        self.assertGreater(out[0]["time"], 1_700_000_000_000)

    def test_flip(self):
        fills = [
            {"fill_id": "f1", "symbol": "PF_ETHUSD", "side": "buy", "size": 1.0,
             "price": 2000.0, "fillTime": "2026-11-01T00:00:00Z"},
            {"fill_id": "f2", "symbol": "PF_ETHUSD", "side": "sell", "size": 2.0,
             "price": 2100.0, "fillTime": "2026-11-01T04:00:00Z"},
        ]
        out = _mk(_StubEp(fills=fills)).user_fills("kr")
        self.assertEqual(out[1]["dir"], "long > short")
        self.assertEqual(out[1]["reduce_only"], 1)
        self.assertAlmostEqual(out[1]["closedPnl"], 100.0)    # 平掉 1 ETH @2100 vs 2000

    def test_history_page_uses_derived_pnl(self):
        """历史页 realized_pnl 为 null → 用推演值并标记 derived。"""
        fills = [
            {"fill_id": "f1", "symbol": "PF_XBTUSD", "side": "buy", "size": 0.01,
             "price": 74000.0, "fillTime": "2026-11-01T00:00:00Z"},
            {"fill_id": "f2", "symbol": "PF_XBTUSD", "side": "sell", "size": 0.01,
             "price": 75000.0, "fillTime": "2026-11-01T04:00:00Z", "realized_pnl": None},
        ]
        out = _mk(_StubEp(fills=fills)).user_fills("kr")
        self.assertEqual(out[1]["closedPnl_src"], "derived")
        self.assertAlmostEqual(out[1]["closedPnl"], 10.0)

    def test_paginates_past_100(self):
        """>=100 条时必须用 lastFillTime 继续翻页（旧实现只取最近 100）。"""
        fills = []
        for i in range(150):
            fills.append({"fill_id": f"f{i}", "symbol": "PF_XBTUSD", "side": "buy",
                          "size": 0.001, "price": 70000.0 + i,
                          "fillTime": f"2026-10-{1 + i // 24:02d}T{i % 24:02d}:00:00.000Z"})
        ep = _StubEp(fills=fills)
        out = _mk(ep).user_fills("kr")
        self.assertEqual(len(out), 150)
        self.assertGreaterEqual(len(ep.fills_calls), 2)


class TestSpecs(unittest.TestCase):
    def test_specs_use_contract_value_precision(self):
        ep = _StubEp(instruments=[
            {"symbol": "PF_XBTUSD", "tickSize": 1, "contractValueTradePrecision": 4,
             "minimumTradeSize": None},
            {"symbol": "PF_QQQXUSD", "tickSize": 0.1, "contractValueTradePrecision": 3},
        ])
        specs = _mk(ep).specs()
        # P0 回归：数量精度不是 tickSize（XBT tick=1 → 旧代码给出 0 位小数）
        self.assertEqual(specs["BTC"]["sz_decimals"], 4)
        self.assertEqual(specs["BTC"]["szDecimals"], 4)
        self.assertAlmostEqual(specs["BTC"]["price_tick"], 1.0)
        self.assertEqual(specs["QQQ"]["sz_decimals"], 3)
        self.assertIn("maxLeverage", specs["BTC"])

    def test_unknown_symbol_ignored(self):
        ep = _StubEp(instruments=[{"symbol": "PF_LTCUSD", "tickSize": 0.1}])
        self.assertEqual(_mk(ep).specs(), {})

    def test_round_price_to_tick(self):
        ep = _StubEp(instruments=[
            {"symbol": "PF_XBTUSD", "tickSize": 1, "contractValueTradePrecision": 4},
            {"symbol": "PF_ETHUSD", "tickSize": 0.1, "contractValueTradePrecision": 3},
        ])
        info = _mk(ep)
        self.assertEqual(info.round_price("BTC", 75798.3), 75798.0)
        self.assertEqual(info.round_price("BTC", 75798.7), 75799.0)
        self.assertAlmostEqual(info.round_price("ETH", 2234.67), 2234.7)

    def test_round_price_without_tick_fails_closed(self):
        with self.assertRaises(KrInfoError):
            _mk(_StubEp()).round_price("BTC", 75000.0)


class TestUserStateHLShape(unittest.TestCase):
    """PR-2：user_state_dex 必须给 HL 原生 assetPositions 形状（MR 腿消费方）。"""

    def test_positions_mapped_to_hl_shape(self):
        ep = _StubEp(account=ACC, positions=[{
            "symbol": "PF_QQQXUSD", "side": "long", "size": 0.5,
            "price": 650.0, "unrealizedPnl": 5.0}])
        st = _mk(ep).user_state_dex("kr", "kr")
        self.assertAlmostEqual(st["marginSummary"]["accountValue"], 1000.0)
        item = st["assetPositions"][0]["position"]
        self.assertEqual(item["coin"], "QQQ")
        self.assertAlmostEqual(item["szi"], 0.5)
        self.assertAlmostEqual(item["entryPx"], 650.0)
        self.assertAlmostEqual(item["positionValue"], 330.0)
        self.assertEqual(item["leverage"]["type"], "cross")

    def test_candles_hl_keys(self):
        class _CandlesEp(_StubEp):
            def charts_history(self, sym, res, from_s, to_s):
                return [{"time": 1700000000000, "open": 1, "high": 2, "low": 0.5,
                         "close": 1.5, "volume": 9}]
        row = _mk(_CandlesEp()).candles("QQQ", "1h", 1, 2)[0]
        for k in ("t", "o", "h", "l", "c", "v"):
            self.assertIn(k, row)

    def test_registry_dex_qualified_name(self):
        from app import symbol_registry as registry
        self.assertEqual(registry.kr_symbol("xyz:XYZ100"), "PF_QQQXUSD")
        self.assertEqual(registry.kr_symbol("XYZ100"), "PF_QQQXUSD")


class TestFailClosed(unittest.TestCase):
    def test_account_zero_raises(self):
        with self.assertRaises(KrInfoError):
            _mk(_StubEp(account={"accounts": {"flex": {"portfolioValue": 0.0}}})).account_equity("kr")

    def test_flat_shape_still_supported(self):
        """老扁平形状（迁移期兼容）仍可读，避免存量测试/面板桩全炸。"""
        self.assertAlmostEqual(
            _mk(_StubEp(account={"pv": 500.0, "av": 400.0, "im": 100.0})).account_equity("kr"),
            500.0)

    def test_orders_failure_untrusted(self):
        class _Bad(_StubEp):
            def openorders(self):
                raise KrInfoError("boom")
        snap = _mk(_Bad(account=ACC)).snapshot("kr")
        self.assertFalse(snap["orders_trusted"])
        self.assertEqual(snap["open_orders"], [])

    def test_account_failure_raises_venue_error(self):
        from app.kr.client import KrApiError

        class _Bad(_StubEp):
            def account(self):
                raise KrApiError("boom")
        with self.assertRaises(KrInfoError):
            _mk(_Bad()).snapshot("kr")


if __name__ == "__main__":
    unittest.main()
