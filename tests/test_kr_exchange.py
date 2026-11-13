"""KrExchange 单测：下单 payload 形状 / sendorder 响应归一化 / dry 语义 / 撤单参数。

响应与参数形状全部按 **Kraken Derivatives v3 OpenAPI**（2026-11-08 抓取）：
- sendStatus: {status, order_id, receivedTime, cliOrdId, orderEvents[]}
- orderEvents[]: {type: PLACE|EXECUTION|REJECT|CANCEL, order/price/amount/reason}
- cancelorder 参数名 order_id；cancelallorders 支持 symbol。
"""
import unittest

from app.kr.exchange import (KrExchange, _coerce_cloid, make_cloid,
                             parse_order_response, STATUS_DRY, STATUS_ERROR,
                             STATUS_FILLED, STATUS_RESTING, STATUS_UNKNOWN)


class _StubInfo:
    """生产 KrInfo 的价格取整/面值钩子（BTC tick=1 / ETH tick=0.1）。

    `contract_size_value=None` 模拟 /instruments 缺 contractSize：KrInfo.contract_size()
    会抛 KrInfoError（不再兜 1.0），place_order 必须据此拒单。
    """

    def __init__(self, contract_size_value=1.0):
        self._cs = contract_size_value

    def all_mids(self, dex=""):
        return {"BTC": 75000.0}

    def round_price(self, coin, px):
        tick = {"BTC": 1.0, "ETH": 0.1, "SOL": 0.01, "QQQ": 0.1}[coin]
        return round(round(float(px) / tick) * tick, 6)

    def contract_size(self, coin):
        if self._cs is None:
            from app.kr.info import KrInfoError
            raise KrInfoError(f"{coin} 取不到 contractSize（fail-closed）")
        return self._cs

    def open_orders_checked(self, address=""):
        return [{"coin": "BTC", "oid": "o-1"}], True


class _StubEndpoints:
    def __init__(self, resp=None, cancel_resp=None):
        self.resp = resp or {"result": "success"}
        self.cancel_resp = cancel_resp if cancel_resp is not None else {
            "result": "success", "cancelStatus": {"status": "cancelled", "order_id": "x"}}
        self.calls = []
        self.cancel_calls = []
        self.cancel_all_calls = []

    def sendorder(self, params):
        self.calls.append(params)
        return self.resp

    def openorders(self):
        return []

    def cancelorder(self, oid):
        self.cancel_calls.append(oid)
        if isinstance(self.cancel_resp, Exception):
            raise self.cancel_resp
        return self.cancel_resp

    def cancelallorders(self, symbol=""):
        self.cancel_all_calls.append(symbol)
        return {"result": "success"}


def _mk_ex(endpoints=None, info=None):
    return KrExchange(api_key="k", secret_b64="aaaa", dry_run=False,
                      info=info or _StubInfo(), endpoints=endpoints or _StubEndpoints())


def _exec_ok(oid="o-1", amount=0.001, price=74900.0, status="filled"):
    return {"result": "success", "sendStatus": {
        "status": status, "order_id": oid,
        "receivedTime": "2026-11-08T08:27:31.814Z",
        "orderEvents": [{"type": "PLACE", "order": {"orderId": oid}},
                        {"type": "EXECUTION", "executionId": "e-1",
                         "amount": amount, "price": price}]}}


class TestPlaceOrder(unittest.TestCase):
    def test_ioc_payload_shape(self):
        ep = _StubEndpoints(_exec_ok())
        ex = _mk_ex(ep)
        res = ex.place_order("BTC", True, 0.001, 75200.0, cloid=make_cloid("BTC", 1, "entry"))
        self.assertTrue(res["ok"])
        self.assertEqual(res["status"], STATUS_FILLED)
        self.assertEqual(res["oid"], "o-1")                # P0: order_id 必须解析出来
        self.assertAlmostEqual(res["avg_px"], 74900.0)
        self.assertAlmostEqual(res["filled_sz"], 0.001)
        p = ep.calls[0]
        self.assertEqual(p["orderType"], "ioc")
        self.assertEqual(p["symbol"], "PF_XBTUSD")
        self.assertEqual(p["side"], "buy")
        self.assertEqual(p["limitPrice"], "75200.0")
        # 跨组契约：reduceOnly=false **不传键**（不是传字符串 "false"）
        self.assertNotIn("reduceOnly", p)
        self.assertIn("cliOrdId", p)

    def test_price_rounded_to_tick(self):
        """P0 回归：Kraken 只收 tick 整数倍（HL 的 5 位有效数字规则会拒单）。"""
        ep = _StubEndpoints(_exec_ok())
        _mk_ex(ep).place_order("BTC", True, 0.001, 75798.3)
        self.assertEqual(ep.calls[0]["limitPrice"], "75798.0")

    def test_stop_payload_shape(self):
        ep = _StubEndpoints({"result": "success", "sendStatus": {
            "status": "placed", "order_id": "o-2",
            "orderEvents": [{"type": "PLACE", "order": {"orderId": "o-2"}}]}})
        ex = _mk_ex(ep)
        res = ex.place_order("BTC", False, 0.001, 0.0, reduce_only=True,
                             trigger_px=73000.4, cloid=make_cloid("BTC", 2, "stop"))
        self.assertEqual(res["status"], STATUS_RESTING)
        self.assertEqual(res["oid"], "o-2")
        p = ep.calls[0]
        self.assertEqual(p["orderType"], "stp")
        self.assertEqual(p["triggerSignal"], "mark")
        self.assertEqual(p["stopPrice"], "73000.0")        # mark 触发价按 tick 取整
        self.assertEqual(p["reduceOnly"], "true")

    def test_stop_without_reduce_only_omits_key(self):
        """止损单不传 reduce_only → 同样不传该键（缺省即 false）。"""
        ep = _StubEndpoints({"result": "success", "sendStatus": {
            "status": "placed", "order_id": "o-2",
            "orderEvents": [{"type": "PLACE", "order": {"orderId": "o-2"}}]}})
        _mk_ex(ep).place_order("BTC", False, 0.001, 0.0, reduce_only=False,
                               trigger_px=73000.0)
        self.assertNotIn("reduceOnly", ep.calls[0])

    def test_ioc_reduce_only_sends_string_true(self):
        ep = _StubEndpoints(_exec_ok())
        _mk_ex(ep).place_order("BTC", False, 0.001, 75000.0, reduce_only=True)
        self.assertEqual(ep.calls[0]["reduceOnly"], "true")

    def test_reject_maps_to_error(self):
        ep = _StubEndpoints({"result": "error",
                             "errors": [{"message": "insufficientAvailableFunds"}]})
        ex = _mk_ex(ep)
        res = ex.place_order("BTC", True, 0.001, 75200.0)
        self.assertFalse(res["ok"])
        self.assertIn("insufficientAvailableFunds", res["error"])

    def test_rejected_status_maps_to_error(self):
        """sendStatus.status=invalidSize 必须判失败（旧实现会返回 RESTING）。"""
        resp = {"result": "success", "sendStatus": {
            "status": "invalidSize", "order_id": "o-9", "orderEvents": []}}
        out = parse_order_response(resp)
        self.assertFalse(out["ok"])
        self.assertEqual(out["status"], STATUS_ERROR)
        self.assertIn("invalidSize", out["error"])

    def test_ioc_not_executed_is_resting(self):
        """IOC 未成交（status=cancelled + REJECT IOC_WOULD_NOT_EXECUTE）→ RESTING，
        执行器据此撤单放弃，不会当成假成交。"""
        resp = {"result": "success", "sendStatus": {
            "status": "cancelled", "order_id": "o-10",
            "orderEvents": [{"type": "REJECT", "reason": "IOC_WOULD_NOT_EXECUTE",
                             "order": None, "uid": "o-10"}]}}
        out = parse_order_response(resp)
        self.assertTrue(out["ok"])
        self.assertEqual(out["status"], STATUS_RESTING)
        self.assertEqual(out["oid"], "o-10")
        self.assertTrue(out["not_executed"])          # P1-4：显式「没成交」，不是「挂单在场」

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
        resp = {"result": "success", "sendStatus": {"status": "filled", "order_id": "o-3",
                                                    "orderEvents": [
            {"type": "EXECUTION", "amount": 1.0, "price": 100.0},
            {"type": "EXECUTION", "amount": 3.0, "price": 200.0}]}}
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

    def test_cancel_uses_order_id_param(self):
        ep = _StubEndpoints()
        _mk_ex(ep).cancel("BTC", "abc-1")
        self.assertEqual(ep.cancel_calls, ["abc-1"])

    def test_cancel_all_uses_per_symbol(self):
        ep = _StubEndpoints()
        _mk_ex(ep).cancel_all("BTC")
        self.assertEqual(ep.cancel_all_calls, ["PF_XBTUSD"])


class TestOrderResponseOid(unittest.TestCase):
    """P1-4：oid 兜底 + 「未成交」语义 + cliOrdId 长度/字符集。"""

    def test_stop_place_trigger_event_uid_is_oid(self):
        """止损单 PLACE 事件是 PlaceTriggerEvent（orderTrigger.uid），无 order.orderId。"""
        resp = {"result": "success", "sendStatus": {
            "status": "placed", "receivedTime": "2026-11-08T08:27:31.814Z",
            "orderEvents": [{"type": "PLACE",
                             "orderTrigger": {"uid": "trig-uuid-1", "stopPrice": 73000.0}}]}}
        out = parse_order_response(resp)
        self.assertTrue(out["ok"])
        self.assertEqual(out["status"], STATUS_RESTING)
        self.assertEqual(out["oid"], "trig-uuid-1")

    def test_place_event_order_id_is_oid(self):
        resp = {"result": "success", "sendStatus": {
            "status": "placed",
            "orderEvents": [{"type": "PLACE", "order": {"orderId": "evt-uuid-2"}}]}}
        self.assertEqual(parse_order_response(resp)["oid"], "evt-uuid-2")

    def test_event_top_level_uid_is_oid(self):
        resp = {"result": "success", "sendStatus": {
            "status": "placed",
            "orderEvents": [{"type": "CANCEL", "uid": "evt-uuid-3"}]}}
        self.assertEqual(parse_order_response(resp)["oid"], "evt-uuid-3")

    def test_top_level_order_id_still_wins(self):
        resp = {"result": "success", "sendStatus": {
            "status": "placed", "order_id": "top-level",
            "orderEvents": [{"type": "PLACE", "order": {"orderId": "from-event"}}]}}
        self.assertEqual(parse_order_response(resp)["oid"], "top-level")

    def test_stop_order_end_to_end_keeps_oid(self):
        ep = _StubEndpoints({"result": "success", "sendStatus": {
            "status": "placed",
            "orderEvents": [{"type": "PLACE", "orderTrigger": {"uid": "stop-uuid-9"}}]}})
        res = _mk_ex(ep).place_order("BTC", False, 0.001, 0.0, reduce_only=True,
                                     trigger_px=73000.0)
        self.assertEqual(res["oid"], "stop-uuid-9")
        self.assertFalse(res["not_executed"])

    def test_ioc_would_not_execute_flagged_not_executed(self):
        """status=iocWouldNotExecute → RESTING（不破坏 executor 的撤单放弃），但标 not_executed。"""
        resp = {"result": "success", "sendStatus": {
            "status": "iocWouldNotExecute",
            "orderEvents": [{"type": "REJECT", "reason": "IOC_WOULD_NOT_EXECUTE",
                             "uid": "gone-uuid"}]}}
        out = parse_order_response(resp)
        self.assertTrue(out["ok"])
        self.assertEqual(out["status"], STATUS_RESTING)
        self.assertTrue(out["not_executed"])
        self.assertEqual(out["oid"], "gone-uuid")

    def test_placed_is_not_flagged_not_executed(self):
        resp = {"result": "success", "sendStatus": {
            "status": "placed", "order_id": "o-1",
            "orderEvents": [{"type": "PLACE", "order": {"orderId": "o-1"}}]}}
        self.assertFalse(parse_order_response(resp)["not_executed"])

    def test_filled_is_not_flagged_not_executed(self):
        self.assertFalse(parse_order_response(_exec_ok())["not_executed"])

    def test_cloid_accepts_underscore_dot_colon_and_100_chars(self):
        self.assertEqual(_coerce_cloid("entry_1.btc:4h"), "entry_1.btc:4h")
        self.assertEqual(_coerce_cloid("a" * 100), "a" * 100)

    def test_cloid_rejects_too_long_and_whitespace(self):
        with self.assertRaises(ValueError):
            _coerce_cloid("a" * 101)
        with self.assertRaises(ValueError):
            _coerce_cloid("bad cloid")
        with self.assertRaises(ValueError):
            _coerce_cloid("bad\ncloid")

    def test_cloid_passes_through_place_order(self):
        ep = _StubEndpoints(_exec_ok())
        _mk_ex(ep).place_order("BTC", True, 0.001, 75200.0, cloid="entry:btc_4h.1")
        self.assertEqual(ep.calls[0]["cliOrdId"], "entry:btc_4h.1")


class TestOrderStatusMachine(unittest.TestCase):
    """跨组契约（2026-11-08）：只认交易所明说的状态，编造成交价一律 fail-closed。"""

    def test_placed_resting_not_executed_false(self):
        out = parse_order_response({"result": "success", "sendStatus": {
            "status": "placed", "order_id": "o-1"}})
        self.assertTrue(out["ok"])
        self.assertEqual(out["status"], STATUS_RESTING)
        self.assertFalse(out["not_executed"])

    def test_untouched_and_edited_are_resting(self):
        for st in ("untouched", "edited"):
            out = parse_order_response({"result": "success", "sendStatus": {
                "status": st, "order_id": "o-1"}})
            self.assertTrue(out["ok"], st)
            self.assertEqual(out["status"], STATUS_RESTING, st)
            self.assertFalse(out["not_executed"], st)

    def test_cancelled_is_resting_not_executed(self):
        out = parse_order_response({"result": "success", "sendStatus": {
            "status": "cancelled", "order_id": "o-1"}})
        self.assertTrue(out["ok"])
        self.assertEqual(out["status"], STATUS_RESTING)
        self.assertTrue(out["not_executed"])

    def test_filled_without_execution_detail_fails_closed(self):
        """说 filled 却没 EXECUTION 明细 → ok=False（绝不编造成交价/量）。

        status 是 `unknown` 而不是 `error`（2026-11-13 复审 A2）：交易所明说「成交了」，
        只是成交价/量拿不到 —— 这不是「明确没下单」，执行器**不能**按旧仓位量重挂
        止损/重发平仓；它能做的只有刷新持仓/成交后再判。判据仍 fail-closed：
        不编价、不编量。
        """
        out = parse_order_response({"result": "success", "sendStatus": {
            "status": "filled", "order_id": "o-1"}})
        self.assertFalse(out["ok"])
        self.assertEqual(out["status"], STATUS_UNKNOWN)
        self.assertIsNone(out["avg_px"])
        self.assertIsNone(out["filled_sz"])
        self.assertIn("EXECUTION", out["error"])

    def test_partially_filled_without_detail_fails_closed(self):
        out = parse_order_response({"result": "success", "sendStatus": {
            "status": "partiallyFilled", "order_id": "o-1"}})
        self.assertFalse(out["ok"])
        self.assertIn("partiallyFilled", out["error"])

    def test_partially_filled_with_execution_is_filled(self):
        out = parse_order_response({"result": "success", "sendStatus": {
            "status": "partiallyFilled", "order_id": "o-1",
            "orderEvents": [{"type": "EXECUTION", "amount": 0.5, "price": 100.0}]}})
        self.assertTrue(out["ok"])
        self.assertEqual(out["status"], STATUS_FILLED)
        self.assertAlmostEqual(out["filled_sz"], 0.5)

    def test_missing_status_fails_closed(self):
        out = parse_order_response({"result": "success", "sendStatus": {
            "order_id": "o-1", "orderEvents": []}})
        self.assertFalse(out["ok"])
        self.assertIn("status", out["error"])

    def test_empty_status_fails_closed(self):
        out = parse_order_response({"result": "success", "sendStatus": {
            "status": "  ", "order_id": "o-1"}})
        self.assertFalse(out["ok"])

    def test_missing_send_status_fails_closed(self):
        out = parse_order_response({"result": "success"})
        self.assertFalse(out["ok"])

    def test_contract_keys_unchanged(self):
        """键集合是跨组契约，B 组按这些键取值 —— 不能增删。"""
        out = parse_order_response(_exec_ok())
        self.assertEqual(set(out), {"ok", "status", "oid", "avg_px", "filled_sz",
                                    "raw", "error", "not_executed"})

    def test_oid_is_string_not_int(self):
        """oid 一律字符串（uuid/uid），禁止 int() 归一。"""
        out = parse_order_response({"result": "success", "sendStatus": {
            "status": "placed", "order_id": "6f1c2b9e-aaaa-bbbb-cccc-1234567890ab"}})
        self.assertIsInstance(out["oid"], str)
        numeric = parse_order_response({"result": "success", "sendStatus": {
            "status": "placed", "order_id": 12345}})
        self.assertEqual(numeric["oid"], "12345")
        self.assertIsInstance(numeric["oid"], str)


class TestPlaceOrderContractSize(unittest.TestCase):
    """A4：contractSize 缺失 → contract_size() 抛错 → place_order 拒单（不兜 1.0）。"""

    def test_missing_contract_size_rejects_order(self):
        ep = _StubEndpoints(_exec_ok())
        ex = _mk_ex(ep, info=_StubInfo(contract_size_value=None))
        res = ex.place_order("BTC", True, 0.001, 75000.0)
        self.assertFalse(res["ok"])
        self.assertIn("contractSize", res["error"])
        self.assertEqual(ep.calls, [], "拒单不能真的发出 sendorder")

    def test_contract_size_not_one_rejects_order(self):
        ep = _StubEndpoints(_exec_ok())
        res = _mk_ex(ep, info=_StubInfo(contract_size_value=0.1)).place_order(
            "BTC", True, 0.001, 75000.0)
        self.assertFalse(res["ok"])
        self.assertEqual(ep.calls, [])


class TestCancelStatus(unittest.TestCase):
    """A3：撤单只看 cancelStatus.status（官方三值），不再用错误文本猜 filled/notFound。"""

    def _ex(self, cancel_resp):
        ep = _StubEndpoints(cancel_resp=cancel_resp)
        return _mk_ex(ep), ep

    def test_cancelled_is_ok_resting(self):
        ex, ep = self._ex({"result": "success", "cancelStatus": {"status": "cancelled",
                                                                 "order_id": "abc"}})
        out = ex.cancel("BTC", "abc")
        self.assertTrue(out["ok"])
        self.assertEqual(out["status"], STATUS_RESTING)
        self.assertEqual(ep.cancel_calls, ["abc"])

    def test_not_found_is_ok_idempotent(self):
        """notFound = 已撤/本就不存在 → 幂等成功（重试安全）。"""
        ex, _ = self._ex({"result": "success", "cancelStatus": {"status": "notFound"}})
        out = ex.cancel("BTC", "abc")
        self.assertTrue(out["ok"])
        self.assertEqual(out["status"], STATUS_RESTING)

    def test_filled_maps_to_filled(self):
        ex, _ = self._ex({"result": "success", "cancelStatus": {"status": "filled"}})
        out = ex.cancel("BTC", "abc")
        self.assertTrue(out["ok"])
        self.assertEqual(out["status"], STATUS_FILLED)

    def test_missing_cancel_status_fails(self):
        ex, _ = self._ex({"result": "success"})
        out = ex.cancel("BTC", "abc")
        self.assertFalse(out["ok"])
        self.assertIn("缺失", out["error"])

    def test_unknown_cancel_status_fails(self):
        ex, _ = self._ex({"result": "success", "cancelStatus": {"status": "weird"}})
        out = ex.cancel("BTC", "abc")
        self.assertFalse(out["ok"])
        self.assertIn("weird", out["error"])

    def test_api_error_no_longer_guessed_as_filled(self):
        """HTTP/业务错误里带 'filled' 字样也不再猜成成交（旧实现的隐患）。"""
        from app.kr.client import KrApiError
        ex, _ = self._ex(KrApiError("order already filled", 200, {}))
        out = ex.cancel("BTC", "abc")
        self.assertFalse(out["ok"])
        self.assertIn("撤单失败", out["error"])


class TestOpenOrdersChecked(unittest.TestCase):
    """A3：无 info → ([], False)；有 info → 复用归一形状。"""

    def test_no_info_is_untrusted(self):
        ex = KrExchange(api_key="k", secret_b64="a", dry_run=False,
                        info=None, endpoints=_StubEndpoints())
        self.assertEqual(ex._open_orders_checked(), ([], False))

    def test_info_shape_used(self):
        ex = _mk_ex()
        orders, trusted = ex._open_orders_checked()
        self.assertTrue(trusted)
        self.assertEqual(orders[0]["oid"], "o-1")

    def test_info_failure_is_untrusted(self):
        class _Bad(_StubInfo):
            def open_orders_checked(self, address=""):
                raise RuntimeError("boom")
        ex = _mk_ex(info=_Bad())
        self.assertEqual(ex._open_orders_checked(), ([], False))


if __name__ == "__main__":
    unittest.main()
