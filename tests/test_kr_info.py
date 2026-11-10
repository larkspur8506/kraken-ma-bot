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
            instruments=[{"symbol": "PF_XBTUSD", "tickSize": 1,
                          "contractValueTradePrecision": 4,
                          "marginLevels": [{"numNonContractUnits": 0.0, "initialMargin": 0.01},
                                           {"numNonContractUnits": 1e6, "initialMargin": 0.02}]}],
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
        # P1-4 回归：保证金 = 名义 × 真实 IM(1%)，不是 value/5；全仓无逐仓杠杆 → None
        self.assertAlmostEqual(p["margin_used"], 750.0 * 0.01)
        self.assertAlmostEqual(p["initial_margin"], 0.01)
        self.assertIsNone(p["leverage"])
        self.assertAlmostEqual(p["roe"], 10.0 / 7.5)

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

    def test_unknown_position_coin_fails_closed(self):
        """未知合约**有量**的持仓 → 抛错（不能读成空仓，否则对账会加倍开仓）。"""
        ep = _StubEp(account=ACC, positions=[{
            "symbol": "PF_LTCUSD", "side": "long", "size": 5.0, "price": 90.0}])
        with self.assertRaises(KrInfoError) as ctx:
            _mk(ep).snapshot("kr")
        self.assertIn("PF_LTCUSD", str(ctx.exception))

    def test_zero_size_unknown_row_ignored(self):
        """0 张的未知合约行 = 没有敞口，不该把整轮读挂掉。"""
        ep = _StubEp(account=ACC, positions=[
            {"symbol": "PF_LTCUSD", "side": "long", "size": 0.0},
            {"symbol": "PF_XBTUSD", "side": "long", "size": 0.01, "price": 74000.0}],
            tickers=[{"symbol": "PF_XBTUSD", "markPrice": 75000.0}])
        snap = _mk(ep).snapshot("kr")
        self.assertEqual([p["coin"] for p in snap["positions"]], ["BTC"])

    def test_liq_px_is_none(self):
        """官方 OpenPositionJson 无强平价字段 → 恒 None（不编公式）。"""
        ep = _StubEp(account=ACC, positions=[{
            "symbol": "PF_XBTUSD", "side": "long", "size": 0.01, "price": 74000.0,
            "liquidationThresholdPrice": 60000.0}],
            tickers=[{"symbol": "PF_XBTUSD", "markPrice": 75000.0}])
        self.assertIsNone(_mk(ep).snapshot("kr")["positions"][0]["liq_px"])

    def test_funding_zero_not_falling_back_to_other_field(self):
        """unrealizedFunding=0.0 是真实值，不能被 `or` 跳到 realizedFundingPnl。"""
        ep = _StubEp(account=ACC, positions=[{
            "symbol": "PF_XBTUSD", "side": "long", "size": 0.01, "price": 74000.0,
            "unrealizedFunding": 0.0, "realizedFundingPnl": -3.5}],
            tickers=[{"symbol": "PF_XBTUSD", "markPrice": 75000.0}])
        self.assertAlmostEqual(_mk(ep).snapshot("kr")["positions"][0]["funding_since_open"], 0.0)

    def test_funding_falls_back_when_unrealized_missing(self):
        ep = _StubEp(account=ACC, positions=[{
            "symbol": "PF_XBTUSD", "side": "long", "size": 0.01, "price": 74000.0,
            "realizedFundingPnl": -3.5}],
            tickers=[{"symbol": "PF_XBTUSD", "markPrice": 75000.0}])
        self.assertAlmostEqual(_mk(ep).snapshot("kr")["positions"][0]["funding_since_open"], -3.5)

    def test_unknown_order_coin_skipped_with_warning(self):
        """未知合约**挂单** → warning + 跳过（挂单不是敞口，不影响仓位判定）。"""
        ep = _StubEp(account=ACC, orders=[
            {"order_id": "x-1", "symbol": "PF_LTCUSD", "side": "buy",
             "unfilledSize": 1.0, "limitPrice": 90.0, "orderType": "lmt"},
            {"order_id": "x-2", "symbol": "PF_XBTUSD", "side": "sell",
             "unfilledSize": 0.01, "limitPrice": 76000.0, "orderType": "lmt"}])
        with self.assertLogs("app.kr.info", level="WARNING") as cap:
            snap = _mk(ep).snapshot("kr")
        self.assertEqual([o["oid"] for o in snap["open_orders"]], ["x-2"])
        self.assertTrue(any("PF_LTCUSD" in line for line in cap.output))

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
        # 跨组契约：dir 必须是 HL 原生大小写（executor 判 "Long"/"Close"、面板 _FILL_BS 精确键）
        self.assertEqual(out[0]["dir"], "Open Long")
        self.assertEqual(out[0]["reduce_only"], 0)
        self.assertEqual(out[1]["dir"], "Close Long")
        self.assertEqual(out[1]["reduce_only"], 1)
        self.assertAlmostEqual(out[1]["closedPnl"], 10.0)     # 交易所真值
        self.assertEqual(out[1]["closedPnl_src"], "exchange")
        self.assertEqual(out[0]["oid"], "o1")
        self.assertAlmostEqual(out[0]["fee"], 74000 * 0.01 * 0.000275)  # 估算口径（Kraken 6 级 taker 2.75bp）
        # P0-3：fee 已是 USD 金额，feeToken 必须给 USD，否则 executor 记成「未知币种」
        self.assertEqual(out[0]["feeToken"], "USD")
        self.assertAlmostEqual(out[1]["fee"], 75000 * 0.01 * 0.000275)
        self.assertEqual(out[1]["feeToken"], "USD")
        # ISO 时间必须解析（旧代码 0 → 按时段过滤永远为空）
        self.assertGreater(out[0]["time"], 1_700_000_000_000)

    def test_maker_fill_uses_maker_fee(self):
        """fillType=maker → 按 maker 费率（0.75bp）估，不再一律 taker。"""
        fills = [{"fill_id": "m1", "symbol": "PF_XBTUSD", "side": "buy", "size": 0.01,
                  "price": 74000.0, "fillTime": "2026-11-01T00:00:00Z", "fillType": "maker"}]
        out = _mk(_StubEp(fills=fills)).user_fills("kr")
        self.assertAlmostEqual(out[0]["fee"], 74000 * 0.01 * 0.000075)
        self.assertEqual(out[0]["feeToken"], "USD")
        self.assertEqual(out[0]["fee_estimated"], 1)

    def test_taker_fill_uses_taker_fee(self):
        fills = [{"fill_id": "t1", "symbol": "PF_XBTUSD", "side": "buy", "size": 0.01,
                  "price": 74000.0, "fillTime": "2026-11-01T00:00:00Z", "fillType": "taker"}]
        out = _mk(_StubEp(fills=fills)).user_fills("kr")
        self.assertAlmostEqual(out[0]["fee"], 74000 * 0.01 * 0.000275)

    def test_open_short_then_close_short(self):
        fills = [
            {"fill_id": "s1", "symbol": "PF_ETHUSD", "side": "sell", "size": 1.0,
             "price": 2000.0, "fillTime": "2026-11-01T00:00:00Z"},
            {"fill_id": "s2", "symbol": "PF_ETHUSD", "side": "buy", "size": 1.0,
             "price": 1900.0, "fillTime": "2026-11-01T04:00:00Z", "realized_pnl": 100.0},
        ]
        out = _mk(_StubEp(fills=fills)).user_fills("kr")
        self.assertEqual(out[0]["dir"], "Open Short")
        self.assertEqual(out[0]["reduce_only"], 0)
        self.assertEqual(out[1]["dir"], "Close Short")
        self.assertEqual(out[1]["reduce_only"], 1)
        self.assertEqual(out[1]["feeToken"], "USD")

    def test_partial_reduce_keeps_open_dir_case(self):
        """部分平仓（仓位没归零）也必须是平仓腿语义，不能被当成开仓。"""
        fills = [
            {"fill_id": "p1", "symbol": "PF_XBTUSD", "side": "buy", "size": 0.02,
             "price": 70000.0, "fillTime": "2026-11-01T00:00:00Z"},
            {"fill_id": "p2", "symbol": "PF_XBTUSD", "side": "sell", "size": 0.01,
             "price": 71000.0, "fillTime": "2026-11-01T04:00:00Z"},
        ]
        out = _mk(_StubEp(fills=fills)).user_fills("kr")
        self.assertEqual(out[1]["dir"], "Close Long")
        self.assertEqual(out[1]["reduce_only"], 1)
        self.assertTrue("Long" in out[1]["dir"] and "Close" in out[1]["dir"])

    def test_flip(self):
        fills = [
            {"fill_id": "f1", "symbol": "PF_ETHUSD", "side": "buy", "size": 1.0,
             "price": 2000.0, "fillTime": "2026-11-01T00:00:00Z"},
            {"fill_id": "f2", "symbol": "PF_ETHUSD", "side": "sell", "size": 2.0,
             "price": 2100.0, "fillTime": "2026-11-01T04:00:00Z"},
        ]
        out = _mk(_StubEp(fills=fills)).user_fills("kr")
        # 反手成交按被平掉的旧仓方向给平仓腿（不能输出 "long > short" 这种描述串）
        self.assertEqual(out[1]["dir"], "Close Long")
        self.assertEqual(out[1]["reduce_only"], 1)
        self.assertAlmostEqual(out[1]["closedPnl"], 100.0)    # 平掉 1 ETH @2100 vs 2000

    def test_short_flip(self):
        fills = [
            {"fill_id": "g1", "symbol": "PF_ETHUSD", "side": "sell", "size": 1.0,
             "price": 2000.0, "fillTime": "2026-11-01T00:00:00Z"},
            {"fill_id": "g2", "symbol": "PF_ETHUSD", "side": "buy", "size": 3.0,
             "price": 1900.0, "fillTime": "2026-11-01T04:00:00Z"},
        ]
        out = _mk(_StubEp(fills=fills)).user_fills("kr")
        self.assertEqual(out[1]["dir"], "Close Short")
        self.assertEqual(out[1]["reduce_only"], 1)

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
        # 无 marginLevels → 兜底 IM 10%（保守），不是旧代码写死的 5x
        self.assertAlmostEqual(specs["BTC"]["initial_margin"], 0.10)
        self.assertEqual(specs["BTC"]["maxLeverage"], 10)

    def test_initial_margin_from_instruments(self):
        """P1-4：max_leverage 必须来自 /instruments 的 IM（BTC 1%≈100x、QQQX 5%=20x）。"""
        ep = _StubEp(instruments=[
            {"symbol": "PF_XBTUSD", "tickSize": 1, "contractValueTradePrecision": 4,
             "marginLevels": [{"numNonContractUnits": 0.0, "initialMargin": 0.01,
                               "maintenanceMargin": 0.005},
                              {"numNonContractUnits": 1e6, "initialMargin": 0.02}]},
            {"symbol": "PF_QQQXUSD", "tickSize": 0.1, "contractValueTradePrecision": 3,
             "retailMarginLevels": [{"numNonContractUnits": 0.0, "initialMargin": 0.05},
                                    {"numNonContractUnits": 25000, "initialMargin": 0.1}]},
        ])
        specs = _mk(ep).specs()
        self.assertAlmostEqual(specs["BTC"]["initial_margin"], 0.01)
        self.assertEqual(specs["BTC"]["max_leverage"], 100)
        self.assertEqual(specs["BTC"]["maxLeverage"], 100)     # MR 腿 perp_meta_dex 读它
        self.assertAlmostEqual(specs["QQQ"]["initial_margin"], 0.05)
        self.assertEqual(specs["QQQ"]["maxLeverage"], 20)

    def test_explicit_max_leverage_override_wins(self):
        ep = _StubEp(instruments=[
            {"symbol": "PF_XBTUSD", "tickSize": 1, "contractValueTradePrecision": 4,
             "marginLevels": [{"initialMargin": 0.01}]}])
        info = KrInfo(endpoints=ep, max_leverage={"BTC": 5})
        self.assertEqual(info.specs()["BTC"]["maxLeverage"], 5)

    def test_perp_meta_dex_exposes_real_max_leverage(self):
        ep = _StubEp(instruments=[
            {"symbol": "PF_XBTUSD", "tickSize": 1, "contractValueTradePrecision": 4,
             "marginLevels": [{"initialMargin": 0.01}]}])
        meta = _mk(ep).perp_meta_dex("kr")
        self.assertEqual(meta[0]["name"], "BTC")
        self.assertEqual(meta[0]["maxLeverage"], 100)

    def test_position_margin_uses_real_im(self):
        """保证金 = 名义 × 真实 IM，ROE = upnl / 真实保证金，leverage 全仓给 None。"""
        ep = _StubEp(account=ACC, positions=[{
            "symbol": "PF_QQQXUSD", "side": "long", "size": 100.0,
            "price": 600.0, "unrealizedPnl": 40.0}],
            instruments=[{"symbol": "PF_QQQXUSD", "tickSize": 0.1,
                          "contractValueTradePrecision": 3,
                          "marginLevels": [{"initialMargin": 0.05},
                                           {"initialMargin": 0.1}]}],
            tickers=[{"symbol": "PF_QQQXUSD", "markPrice": 600.0}])
        p = _mk(ep).snapshot("kr")["positions"][0]
        self.assertAlmostEqual(p["position_value"], 60000.0)
        self.assertAlmostEqual(p["margin_used"], 3000.0)       # 60000 × 5%（不是 /5 = 12000）
        self.assertAlmostEqual(p["roe"], 40.0 / 3000.0)
        self.assertIsNone(p["leverage"])
        self.assertFalse(p["isolated"])

    def test_position_margin_fallback_without_instruments(self):
        """读不到 /instruments 时按 10% 保守兜底（不能除零、不能算成 0 占用）。"""
        ep = _StubEp(account=ACC, positions=[{
            "symbol": "PF_XBTUSD", "side": "long", "size": 0.01,
            "price": 74000.0, "unrealizedPnl": 10.0}],
            tickers=[{"symbol": "PF_XBTUSD", "markPrice": 75000.0}])
        p = _mk(ep).snapshot("kr")["positions"][0]
        self.assertAlmostEqual(p["margin_used"], 750.0 * 0.10)
        self.assertIsNone(p["leverage"])

    def test_unknown_symbol_ignored(self):
        ep = _StubEp(instruments=[{"symbol": "PF_LTCUSD", "tickSize": 0.1}])
        self.assertEqual(_mk(ep).specs(), {})

    def test_missing_contract_size_kept_zero_and_raises(self):
        """跨组契约：contractSize 缺失 → 保留 0（**不再兜 1.0**），contract_size() 抛错。

        兜成 1.0 会让「本系统按币量下单」这个前提在非 1 面值合约上悄悄成立
        = 按面值倍数超量下单。
        """
        ep = _StubEp(instruments=[{"symbol": "PF_XBTUSD", "tickSize": 1,
                                   "contractValueTradePrecision": 4}])
        info = _mk(ep)
        self.assertEqual(info.specs()["BTC"]["contract_size"], 0.0)
        with self.assertRaises(KrInfoError):
            info.contract_size("BTC")

    def test_explicit_contract_size_preserved(self):
        ep = _StubEp(instruments=[{"symbol": "PF_XBTUSD", "tickSize": 1,
                                   "contractValueTradePrecision": 4,
                                   "contractSize": 1.0}])
        self.assertAlmostEqual(_mk(ep).contract_size("BTC"), 1.0)

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
