"""第四轮审核 P2-B / P2-C / P2-G / P3 + 外围 fail-closed 的定向回归。

编号与 `/opt/data/weekend-run/phase3/kraken-r4-audit.md` 一致：

- **P2-B**：entry 行停在 intent/planned/failed/skipped（成交后崩溃）时，交易所有同向仓
  + 成交里能找到入场证据（oid / cloid / 开仓腿）→ 必须按真仓纳入对账并补账；
  证据不足则维持原判（走「人工单？」告警），**不能**无条件把 pending 当真仓。
- **P2-C**：`take_profit` / 盈利侧触发单不算止损；方向/数量/trigger>0 的既有校验不回退；
  `_has_stop` 的旧调用（不传 entry_px）行为不变。
- **P2-G**：kill 已触发时的「对账异常」事件必须按状态去重（否则生产探针 events_max 每轮
  都变 → pending 每 5 分钟被覆盖重写）。
- **P3**：`_current_closed_bar_ms` 兜底返回**已收盘** bar；撤掉「DB 无对应行」的残留止损
  时文案要点明「可能是用户手工挂的 reduce-only 单」。
- **外围 fail-closed**：`KrEndpoints.openorders()` 缺键/类型不对要抛错；挂单里出现未注册
  合约时 `KrInfo` 判不可信（而不是「warning 一下仍然 trusted」）。

全部离线：临时 sqlite + 假 info/假 exchange，不连交易所、不下任何真单。
"""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from app.db import DB
from app.hl import (EV_KILL, EV_ORDER, MODE_LIVE, PURPOSE_ENTRY, PURPOSE_STOP,
                    ST_FILLED, ST_INTENT, ST_RESTING, ST_SKIPPED)
from app.hl.executor import Executor, ExecParams
from app.hl.exchange import STATUS_FILLED, STATUS_RESTING
from app.kr.client import KrApiError, KrEndpoints
from app.kr.exchange import make_cloid
from app.kr.info import KrInfo

BAR = 1791912000000                 # 固定信号 bar（不进任何时间窗口判据）
SYM, COIN = "BTC_USDT", "BTC"
DAY_MS = 4 * 3600 * 1000


def snap(equity=1000.0, positions=(), orders=(), trusted=True):
    return {"ts": time.time(), "address": "0xabc", "equity": equity,
            "equity_total": equity, "withdrawable": equity, "spot_ok": True,
            "margin_used": 10.0,
            "notional": sum(abs(p["size"] * p["entry_px"]) for p in positions),
            "positions": list(positions), "open_orders": list(orders),
            "orders_trusted": trusted}


def pos(coin=COIN, size=0.01, entry=83000.0, side=1):
    return {"coin": coin, "side": side, "size": size, "entry_px": entry,
            "mark_px": entry, "position_value": size * entry, "liq_px": None,
            "leverage": None, "isolated": False, "margin_used": size * entry / 100,
            "unrealized_pnl": 0.0, "roe": 0.0, "funding_since_open": 0.0}


def trig(oid, trigger, is_buy=False, sz=0.01, order_type="stp", reduce_only=1):
    """交易所挂单（`KrInfo._orders` 的归一形状）。"""
    return {"coin": COIN, "oid": oid, "is_buy": is_buy, "sz": sz, "limit_px": 0.0,
            "reduce_only": reduce_only, "trigger_px": trigger, "is_trigger": 1,
            "order_type": order_type, "timestamp": 0}


class FakeInfo:
    def __init__(self):
        self._snap = snap()
        self._fills = []

    def snapshot(self, address=None, mids=None):
        return dict(self._snap)

    def specs(self, coins=None):
        return {c: {"sz_decimals": 4, "max_leverage": 100} for c in (coins or [COIN])}

    def size_tick(self, coin):
        return 0.0001

    def user_fills(self, address=None):
        return list(self._fills)

    def agent_status(self, address=None):
        return {"kind": "kraken-api", "expires": None, "days_left": None}


class FakeExchange:
    def __init__(self):
        self.calls = []
        self.available = True
        self.init_error = ""
        self.orders_now = []
        self.trusted = True

    def mid(self, coin):
        return 83000.0

    def slippage_limit(self, coin, is_buy, mid=None):
        return 83000.0 * (1.003 if is_buy else 0.997)

    def _cloid(self, symbol, bar_ms, purpose):
        return make_cloid(symbol, bar_ms, purpose)

    def place_order(self, coin, is_buy, sz, limit_px, reduce_only=False, tif="Ioc",
                    trigger_px=None, cloid=None):
        self.calls.append({"coin": coin, "is_buy": is_buy, "sz": sz, "px": limit_px,
                           "reduce_only": reduce_only, "trigger_px": trigger_px})
        if trigger_px is not None:
            return {"ok": True, "status": STATUS_RESTING, "oid": "OID-STOP-NEW",
                    "avg_px": None, "filled_sz": None, "error": None, "raw": {},
                    "not_executed": False}
        return {"ok": True, "status": STATUS_FILLED, "oid": "OID-EXIT",
                "avg_px": limit_px, "filled_sz": sz, "error": None, "raw": {},
                "not_executed": False}

    def cancel(self, coin, oid):
        self.calls.append({"cancel": oid})
        return {"ok": True, "status": STATUS_RESTING, "oid": oid, "error": None}

    def _open_orders_checked(self):
        return list(self.orders_now), bool(self.trusted)

    def update_leverage(self, coin, leverage, cross=False):
        return {"ok": True, "status": STATUS_RESTING, "error": None}


class Notifier:
    def __init__(self):
        self.messages = []

    def send_message(self, text):
        self.messages.append(text)
        return True


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(str(Path(self.tmp.name) / "t.db"))
        self.notifier = Notifier()
        self.info = FakeInfo()
        self.ex = FakeExchange()
        self.p = ExecParams(mode=MODE_LIVE, account_address="0xabc", private_key="0xkey",
                            equity=1000.0, equity_min=20.0, risk_pct=0.0133, leverage=5.0,
                            code_rev="audit4-p2",
                            # 本文件不建模信号时效闸（另有专测）。
                            max_signal_age_min=0)

    def tearDown(self):
        self.tmp.cleanup()

    # ---------------------------------------------------------------- 构造
    def executor(self):
        """每轮一个新的 Executor（与生产 build_executor 一致 —— 内存标记跨轮无效）。"""
        return Executor(self.db, self.p, self.info, self.ex, notifier=self.notifier)

    def pending(self, side=1, entry=83000.0, stop=80600.0, sz=0.01):
        return self.db.create_position(symbol=SYM, signal_bar_ms=BAR, side=side,
                                       entry_price=entry, stop_price=stop,
                                       stop_dist=abs(entry - stop) / entry, contracts=1,
                                       coin_qty=sz, notional=sz * entry, risk_u=20.0,
                                       equity_at_entry=1000.0)

    def open_row(self, side=1, entry=83000.0, stop=80600.0, sz=0.01, stop_oid="S-1"):
        pid = self.pending(side=side, entry=entry, stop=stop, sz=sz)
        self.db.set_pos_hl(pid, hl_status="open", hl_entry_oid="E-1",
                           hl_stop_oid=stop_oid, hl_sz=sz)
        self.db.confirm_position(pid, entry, sz, stop, True)
        self.db.hl_record_intent(SYM, COIN, BAR, PURPOSE_ENTRY, side, sz, px=entry,
                                 status=ST_FILLED, oid="E-1")
        if stop_oid:
            self.db.hl_record_intent(SYM, COIN, BAR, PURPOSE_STOP, side, sz, px=stop,
                                     reduce_only=True, status=ST_RESTING, oid=stop_oid)
        return pid

    def fill(self, oid="E-1", sz=0.01, px=83000.0, side=1, dir_="Open Long",
             reduce_only=0, cloid="", ts=None):
        """落一笔已同步成交（DB 形状：side 是仓位方向整数、ts 是秒）。"""
        self.db.hl_upsert_fill(tid=f"tid-{oid}-{dir_}-{len(self.db.hl_recent_fills(5))}",
                               coin=COIN, symbol=SYM, side=side, sz=sz, px=px,
                               dir=dir_, reduce_only=reduce_only, oid=oid,
                               ts=int(ts if ts is not None else time.time()))

    def events(self, kind=None):
        return [dict(r) for r in self.db.hl_recent_events(200, kind=kind)]


# --------------------------------------------------------------------- P2-B
class TestP2BEntryEvidence(Base):
    """成交后崩溃：entry 行停在 intent，但交易所有真仓 → 必须按真仓对账。"""

    def test_intent_with_position_and_oid_fill_is_tracked(self):
        pid = self.pending()
        # place_order 成功、hl_update_order(FILLED) 没写成 → 行停在 intent，但 oid 已在
        self.db.hl_record_intent(SYM, COIN, BAR, PURPOSE_ENTRY, 1, 0.01, px=83000.0,
                                 status=ST_INTENT, oid="E-OID-9")
        self.fill(oid="E-OID-9")
        self.info._snap = snap(positions=[pos()],
                               orders=[trig("S-1", 80600.0, is_buy=False, sz=0.01)])
        rec = self.executor().reconcile()

        self.assertTrue(rec["ok"], f"真仓不该被判异常：{rec['problems']}")
        self.assertNotIn("人工单", "；".join(rec["problems"]))
        row = self.db.position_by_id(pid)
        self.assertEqual(row["status"], "open", "pending 必须补账成 open（引擎才判离场）")
        self.assertEqual(row["hl_status"], "open")
        self.assertAlmostEqual(float(row["hl_sz"]), 0.01, places=8)
        ev = [e for e in self.events("reconcile") if "入场证据" in e["message"]]
        self.assertTrue(ev, "补齐事件里要写明证据来源（oid/cloid/open_fill）")

    def test_intent_with_position_and_cloid_fill_is_tracked(self):
        """入场单连 oid 都没落库时，靠成交的 cliOrdId 认领（Kraken /fills 带该字段）。"""
        pid = self.pending()
        self.db.hl_record_intent(SYM, COIN, BAR, PURPOSE_ENTRY, 1, 0.01, px=83000.0,
                                 status=ST_INTENT)
        cloid = make_cloid(SYM, BAR, PURPOSE_ENTRY)
        # 实盘成交（还没进 DB）→ info.user_fills 路径
        self.info._fills = [{"coin": COIN, "px": 83000.0, "sz": 0.01, "side": "buy",
                             "time": int(time.time() * 1000), "dir": "Open Long",
                             "closedPnl": 0.0, "fee": 0.2, "feeToken": "USD",
                             "tid": "t-cloid", "oid": "E-UNKNOWN", "cloid": cloid,
                             "reduce_only": 0}]
        self.info._snap = snap(positions=[pos()],
                               orders=[trig("S-1", 80600.0, is_buy=False, sz=0.01)])
        rec = self.executor().reconcile()

        self.assertTrue(rec["ok"], rec["problems"])
        self.assertEqual(self.db.position_by_id(pid)["status"], "open")
        ev = [e for e in self.events("reconcile") if "cloid" in e["message"]]
        self.assertTrue(ev, "事件里应记明是靠 cloid 认领的")

    def test_skipped_without_position_not_tracked(self):
        """四轮 P1 的反例必须保持：on_entry 在读数据阶段退出 → 交易所什么都没有。"""
        pid = self.pending()
        self.db.hl_record_intent(SYM, COIN, BAR, PURPOSE_ENTRY, 1, 0.0, status=ST_SKIPPED)
        self.info._snap = snap(positions=[], orders=[])
        rec = self.executor().reconcile()

        self.assertTrue(rec["ok"], f"skipped 的 pending 不该算仓位：{rec['problems']}")
        self.assertEqual(rec["problems"], [])
        row = self.db.position_by_id(pid)
        self.assertEqual(row["status"], "pending")
        self.assertIsNone(row["hl_status"], "不该被标 mismatch")
        self.assertFalse(self.db.hl_kill())

    def test_intent_with_position_but_no_fill_evidence_keeps_manual_wording(self):
        """证据不足 → 维持现状：报「人工单？」而不是悄悄认领。"""
        pid = self.pending()
        self.db.hl_record_intent(SYM, COIN, BAR, PURPOSE_ENTRY, 1, 0.01, px=83000.0,
                                 status=ST_INTENT)
        self.info._snap = snap(positions=[pos()],
                               orders=[trig("S-1", 80600.0, is_buy=False, sz=0.01)])
        rec = self.executor().reconcile()

        self.assertFalse(rec["ok"])
        self.assertIn("人工单", "；".join(rec["problems"]))
        self.assertEqual(self.db.position_by_id(pid)["status"], "pending",
                         "证据不足时不许改仓位状态")
        self.assertFalse(self.db.hl_kill(), "reconcile 本身不动 kill（kill 由 run 置）")

    def test_direction_mismatch_not_claimed(self):
        """交易所仓方向与本地 pending 相反 → 不认领（不猜）。"""
        self.pending(side=1)
        self.db.hl_record_intent(SYM, COIN, BAR, PURPOSE_ENTRY, 1, 0.01, px=83000.0,
                                 status=ST_INTENT, oid="E-OID-9")
        self.fill(oid="E-OID-9", side=1)
        self.info._snap = snap(positions=[pos(side=-1)],
                               orders=[trig("S-1", 85000.0, is_buy=True, sz=0.01)])
        rec = self.executor().reconcile()
        self.assertFalse(rec["ok"])
        self.assertIn("人工单", "；".join(rec["problems"]))


# --------------------------------------------------------------------- P2-C
class TestP2CTakeProfit(Base):
    """止盈/盈利侧触发单不是止损。"""

    def test_take_profit_not_a_stop(self):
        s = snap(orders=[trig("TP-1", 90000.0, is_buy=False, sz=0.01,
                              order_type="take_profit")])
        self.assertFalse(self.executor()._has_stop(s, COIN, side=1, sz=0.01,
                                                   entry_px=81000.0),
                         "take_profit 不能当止损")

    def test_tp_alias_not_a_stop(self):
        s = snap(orders=[trig("TP-2", 80600.0, is_buy=False, sz=0.01, order_type="tp")])
        self.assertFalse(self.executor()._has_stop(s, COIN, side=1, sz=0.01,
                                                   entry_px=83000.0))

    def test_profit_side_stp_not_a_stop(self):
        """多仓、入场 81000、触发 83000（盈利侧）→ 不是下行保护。"""
        s = snap(orders=[trig("S-1", 83000.0, is_buy=False, sz=0.01)])
        self.assertFalse(self.executor()._has_stop(s, COIN, side=1, sz=0.01,
                                                   entry_px=81000.0))

    def test_loss_side_stp_is_a_stop(self):
        s = snap(orders=[trig("S-1", 80600.0, is_buy=False, sz=0.01)])
        self.assertTrue(self.executor()._has_stop(s, COIN, side=1, sz=0.01,
                                                  entry_px=81000.0))

    def test_short_profit_side_stp_not_a_stop(self):
        """空仓：止损必须在入场价**上方**；下方的是盈利侧。"""
        s = snap(orders=[trig("S-1", 76000.0, is_buy=True, sz=0.01)])
        self.assertFalse(self.executor()._has_stop(s, COIN, side=-1, sz=0.01,
                                                   entry_px=80000.0))
        s2 = snap(orders=[trig("S-1", 84000.0, is_buy=True, sz=0.01)])
        self.assertTrue(self.executor()._has_stop(s2, COIN, side=-1, sz=0.01,
                                                  entry_px=80000.0))

    def test_default_call_keeps_old_semantics(self):
        """旧调用（不传 entry_px / side）只判「有没有」：不加新的否决条件。"""
        s = snap(orders=[trig("S-1", 83000.0, is_buy=False, sz=0.01)])
        self.assertTrue(self.executor()._has_stop(s, COIN))

    def test_direction_and_size_checks_not_regressed(self):
        ex = self.executor()
        wrong_side = snap(orders=[trig("S-1", 80600.0, is_buy=True, sz=0.01)])
        self.assertFalse(ex._has_stop(wrong_side, COIN, side=1, sz=0.01, entry_px=81000.0),
                         "多仓的止损必须是卖单")
        too_small = snap(orders=[trig("S-1", 80600.0, is_buy=False, sz=0.001)])
        self.assertFalse(ex._has_stop(too_small, COIN, side=1, sz=0.01, entry_px=81000.0),
                         "数量不足不算保护")
        zero_trigger = snap(orders=[{"coin": COIN, "oid": "S-0", "is_buy": False,
                                     "sz": 0.01, "reduce_only": 1, "trigger_px": 0,
                                     "is_trigger": 1, "order_type": "stp"}])
        self.assertFalse(ex._has_stop(zero_trigger, COIN, side=1, sz=0.01,
                                      entry_px=81000.0))

    def test_reconcile_flags_missing_stop_when_only_take_profit(self):
        """父代理复现：只有一张 reduce-only take_profit 时旧逻辑 ok=True / missing_stop=0。"""
        pid = self.open_row(stop_oid=None)
        self.info._snap = snap(positions=[pos()],
                               orders=[trig("TP-1", 90000.0, is_buy=False, sz=0.01,
                                            order_type="take_profit")])
        rec = self.executor().reconcile()

        self.assertFalse(rec["ok"], "只有止盈单 = 没有下行保护，必须报异常")
        self.assertIn("没有止损", "；".join(rec["problems"]))
        self.assertEqual(len(rec.get("missing_stop") or []), 1)
        self.assertEqual(int(rec["missing_stop"][0]["id"]), int(pid))


# --------------------------------------------------------------------- P2-G
class TestP2GKillEventDedup(Base):
    """对账持续异常 + kill 已触发：事件只写一次（否则探针 events_max 每轮变）。"""

    def _dirty(self):
        # 交易所有 ETH 仓、本地无记录 → rec.ok=False（且不是 unknown）
        self.info._snap = snap(positions=[pos(coin="ETH")], orders=[])
        self.ex.orders_now = []

    def _clean(self):
        self.info._snap = snap(positions=[], orders=[])

    def test_kill_event_written_once_per_state(self):
        self._dirty()
        ex1 = self.executor()
        ex1._snapshot = lambda force=False: self.info.snapshot()
        ex1.run(force_snapshot=True)
        kills = self.events(EV_KILL)
        self.assertEqual(len(kills), 1, "首次触发必须写一条 kill 事件")
        self.assertIn("自动 kill switch", kills[0]["message"])
        self.assertTrue(self.db.hl_kill())

        ex2 = self.executor()                    # 生产里每轮都是新实例
        ex2._snapshot = lambda force=False: self.info.snapshot()
        ex2.run(force_snapshot=True)
        self.assertEqual(len(self.events(EV_KILL)), 1,
                         "kill 已触发时第二轮不许再写 kill 事件（刷屏源）")
        self.assertTrue(self.db.hl_kill(), "kill 本身不能被动到")

        # 恢复正常（状态变化）→ 之后再异常要能重新写一条
        self._clean()
        ex3 = self.executor()
        ex3._snapshot = lambda force=False: self.info.snapshot()
        ex3.db.hl_set_kill(False, "test reset", origin="manual")
        ex3.run(force_snapshot=True)
        self._dirty()
        ex4 = self.executor()
        ex4._snapshot = lambda force=False: self.info.snapshot()
        ex4.run(force_snapshot=True)
        self.assertEqual(len(self.events(EV_KILL)), 2,
                         "状态从正常变回异常要重新记一条——去重不能变成永久静音")


# --------------------------------------------------------------------- P3
class TestP3ClosedBarAndManualStopWording(Base):
    def test_current_closed_bar_is_really_closed(self):
        """没有 bar_state 时，兜底必须是**已收盘** bar 的开盘（旧实现返回进行中那根）。"""
        v = self.executor()._current_closed_bar_ms(SYM)
        now_ms = int(time.time() * 1000)
        self.assertEqual(v % DAY_MS, 0, "必须落在 4h 网格上")
        self.assertLessEqual(v + DAY_MS, now_ms + 1000, "这根 bar 必须已经收盘")
        self.assertGreaterEqual(now_ms - v, DAY_MS)

    def test_residual_stop_without_db_row_says_manual(self):
        row = self.db.position_by_id(self.open_row(stop_oid=None))
        self.ex.orders_now = [{"coin": COIN, "oid": "MANUAL-1", "is_trigger": 1,
                               "reduce_only": 1, "sz": 0.01, "trigger_px": 80000.0}]
        ok = self.executor()._verify_no_residual_stops(row, COIN)
        self.assertTrue(ok)
        self.assertTrue([c for c in self.ex.calls if c.get("cancel") == "MANUAL-1"],
                        "残留单仍要撤（平仓后它只会咬下一笔仓）")
        msgs = [e["message"] for e in self.events(EV_ORDER)]
        joined = "；".join(msgs)
        self.assertIn("MANUAL-1", joined)
        self.assertIn("手工", joined, "必须点明可能是用户手工挂的 reduce-only 单，别静默")


# --------------------------------------------------------------------- 外围 fail-closed
class _C:
    """KrEndpoints 的最小假 client。"""

    def __init__(self, resp, status=200):
        self.resp, self.status = resp, status
        self.base_url = ""

    def get_private(self, path, params=None):
        return self.resp, self.status


class TestOuterFailClosed(unittest.TestCase):
    def test_missing_openorders_key_raises(self):
        with self.assertRaises(KrApiError) as ctx:
            KrEndpoints(_C({"result": "success"})).openorders()
        self.assertIn("openOrders", str(ctx.exception))

    def test_non_list_openorders_raises(self):
        with self.assertRaises(KrApiError):
            KrEndpoints(_C({"result": "success", "openOrders": {"bad": 1}})).openorders()

    def test_empty_list_is_trusted_empty(self):
        self.assertEqual(
            KrEndpoints(_C({"result": "success", "openOrders": []})).openorders(), [])


class _Ep:
    def __init__(self, orders):
        self._orders_l = orders

    def account(self):
        return {"result": "success", "accounts": {"flex": {
            "portfolioValue": 1000.0, "availableMargin": 800.0, "initialMargin": 200.0}}}

    def openpositions(self):
        return []

    def openorders(self):
        return self._orders_l

    def instruments(self):
        return []

    def tickers(self):
        return []


class TestKrInfoOrdersUntrusted(unittest.TestCase):
    """挂单里出现未注册合约 → 整张表不可信（旧实现只 warning 仍 trusted=True）。"""

    def test_unknown_contract_marks_untrusted(self):
        info = KrInfo(endpoints=_Ep([
            {"symbol": "PF_ADAUSD", "order_id": "X-1", "side": "sell",
             "unfilledSize": 10, "stopPrice": 1.0, "reduceOnly": True,
             "orderType": "stp"},
        ]))
        orders, trusted = info.open_orders_checked("")
        self.assertFalse(trusted, "表不完整就不能说「可信」")
        self.assertEqual(orders, [])

    def test_snapshot_propagates_untrusted(self):
        info = KrInfo(endpoints=_Ep([
            {"symbol": "PF_ADAUSD", "order_id": "X-1", "side": "sell",
             "unfilledSize": 10, "stopPrice": 1.0, "reduceOnly": True,
             "orderType": "stp"},
        ]))
        s = info.snapshot("kr", mids={})
        self.assertFalse(s["orders_trusted"], "对账/探针据此才走「无法确认」而不是补挂")

    def test_known_orders_stay_trusted(self):
        info = KrInfo(endpoints=_Ep([
            {"symbol": "PF_XBTUSD", "order_id": "X-2", "side": "sell",
             "unfilledSize": 0.01, "stopPrice": 80000.0, "reduceOnly": True,
             "orderType": "stp"},
        ]))
        orders, trusted = info.open_orders_checked("")
        self.assertTrue(trusted)
        self.assertEqual(orders[0]["coin"], COIN)
        self.assertEqual(orders[0]["order_type"], "stp")


if __name__ == "__main__":
    unittest.main()
