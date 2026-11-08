"""venue 契约回归测试（Kraken 版业务层 / B 组）。

钉住三件「真钱系统里会直接炸或静默算错」的事：

1. **oid 是 UUID 字符串，不是整数**。Kraken 的 order_id 形如
   ``06b9d509-965c-4788-b317-0e5ca11d56fb``；历史上执行器在撤单/对账里对它做了
   ``int()`` 强转 → 离场撤止损、人工平仓后撤残留止损全部 ValueError 崩溃（仓位裸奔）。
   这里用真实的 UUID 字符串走一遍撤单路径，断言不抛异常、且交易所收到的是**完整 UUID**。
2. **dir 消费大小写不敏感**。HL 原生输出 ``"Open Long"`` / ``"Close Short"``，Kraken
   适配层曾输出小写；生产库里的历史行也可能是小写。业务层必须两边都认，并且
   ``reduce_only`` 优先采信成交自带字段（KrInfo 按成交序列算，反手成交更准）。
3. **fee 落库**：``feeToken="USD"`` 必须折算成 U（``fee_quote`` 非 NULL），不再触发
   「未能折算」事件。

外加 agent 清仓价的取整必须走 venue 自己的 ``round_price``（Kraken tick=1，套 HL 的
≤5 位有效数字规则会产出非法价被拒单）。

全部离线：临时 sqlite + 假 info/交易所（复用 tests/test_hl_executor 的假件）。
"""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from app.db import DB
from app.hl import EV_ORDER, MODE_LIVE, PURPOSE_ENTRY, PURPOSE_STOP
from app.hl import agent as A
from app.hl import sizing as hl_sizing
from app.hl.exchange import STATUS_FILLED, STATUS_RESTING
from app.hl.executor import Executor, ExecParams
from tests.test_hl_executor import FakeExchange, FakeInfo, FakeNotifier, _pos, _snap, _stop_order

# Kraken 真实形态的订单号：带连字符的 UUID 字符串，int() 必炸。
UUID_OID = "06b9d509-965c-4788-b317-0e5ca11d56fb"
UUID_OID_STOP = "1f0c2b7a-3d44-4a90-9f6e-77aa12bc9d01"
UUID_OID_EXIT = "9d8e7f60-2211-4b3c-8a5d-0c1b2a3d4e5f"


class Base(unittest.TestCase):
    mode = MODE_LIVE

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(str(Path(self.tmp.name) / "t.db"))
        self.notifier = FakeNotifier()
        self.info = FakeInfo()
        self.ex = FakeExchange()
        self.p = ExecParams(mode=self.mode, account_address="0xabc", private_key="0xkey",
                            equity=1000.0, risk_pct=0.02, leverage=5.0, code_rev="testrev")

    def tearDown(self):
        self.tmp.cleanup()

    def executor(self, **kw):
        for k, v in kw.items():
            setattr(self.p, k, v)
        return Executor(self.db, self.p, self.info, self.ex, notifier=self.notifier)

    def add_signal(self, symbol="BTC_USDT", side=1, entry=83000.0, stop=80600.0, bar_ms=None):
        # bar_ms 取「刚刚收盘」的 4h bar：执行器有信号时效闸（默认 30 分钟内）。
        if bar_ms is None:
            bar_ms = int(time.time() * 1000) - 5 * 60 * 1000 - 4 * 3600 * 1000
        return self.db.create_position(symbol=symbol, signal_bar_ms=bar_ms, side=side,
                                       entry_price=entry, stop_price=stop,
                                       stop_dist=abs(entry - stop) / entry, contracts=1,
                                       coin_qty=0.001, notional=entry * 0.001,
                                       risk_u=20.0, equity_at_entry=1000.0)

    def open_position(self, symbol="BTC_USDT"):
        """走真实 on_entry 开仓，返回 (pid, bar_ms)。"""
        pid = self.add_signal(symbol=symbol)
        self.assertTrue(self.executor().on_entry(pid))
        bar_ms = int(self.db.position_by_id(pid)["signal_bar_ms"])
        return pid, bar_ms

    def events(self, kind=None):
        return [dict(r) for r in self.db.hl_recent_events(200, kind=kind)]

    def cancels(self):
        return [c for c in self.ex.calls if c[0] == "cancel"]


# --------------------------------------------------------------------------- oid（P0）
class TestUuidOidNeverCoerced(Base):
    """P0：Kraken 的 order_id 是 UUID 字符串，任何 int() 强转都是崩溃/裸仓。"""

    def test_cancel_residual_stops_sends_full_uuid(self):
        """人工/外部平仓后撤残留止损：oid 必须原样（UUID）传给交易所。"""
        pid, bar_ms = self.open_position()
        self.db.hl_update_order("BTC_USDT", bar_ms, PURPOSE_STOP,
                                oid=UUID_OID, status=STATUS_RESTING)
        self.db.set_pos_hl(pid, hl_stop_oid=UUID_OID)
        ex = self.executor()
        self.ex.calls.clear()
        self.notifier.messages.clear()                     # 忽略开仓通知

        # 以前这里 int(uuid) → ValueError，直接把对账整轮打断（异常向上冒）。
        ex._cancel_residual_stops(self.db.position_by_id(pid), "BTC")

        self.assertEqual(self.cancels(), [("cancel", "BTC", UUID_OID)])
        self.assertEqual(self.db.hl_order("BTC_USDT", bar_ms, PURPOSE_STOP)["status"], "cancelled")
        self.assertTrue(any(UUID_OID in str(e["message"]) for e in self.events(EV_ORDER)))
        self.assertEqual(self.notifier.messages, [])       # 成功撤单不该喊人

    def test_exit_ma20_cancels_uuid_stop_oid(self):
        """离场第一步「撤止损」：UUID oid 不能崩，且必须原样发出。"""
        pid, bar_ms = self.open_position()
        self.db.hl_update_order("BTC_USDT", bar_ms, PURPOSE_STOP,
                                oid=UUID_OID_STOP, status=STATUS_RESTING)
        self.db.set_pos_hl(pid, hl_stop_oid=UUID_OID_STOP)
        self.ex.calls.clear()
        self.ex.exit = {"ok": True, "status": STATUS_FILLED, "oid": UUID_OID_EXIT,
                        "avg_px": 84500.0, "filled_sz": 0.00833, "error": None, "raw": {}}
        self.info._snap = _snap(positions=[_pos("BTC")])

        self.assertTrue(self.executor().on_exit(pid, "ma20", 84400.0))

        self.assertIn(("cancel", "BTC", UUID_OID_STOP), self.ex.calls)
        row = self.db.position_by_id(pid)
        self.assertEqual(row["status"], "closed")
        self.assertEqual(row["exit_reason"], "ma20")

    def test_exit_ioc_resting_cancels_uuid_exit_oid(self):
        """平仓 IOC 挂出未成交：撤掉自己那张单时 oid 同样是 UUID。"""
        pid, bar_ms = self.open_position()
        self.db.hl_update_order("BTC_USDT", bar_ms, PURPOSE_STOP,
                                oid=UUID_OID_STOP, status=STATUS_RESTING)
        self.ex.calls.clear()
        self.ex.exit = {"ok": True, "status": STATUS_RESTING, "oid": UUID_OID_EXIT,
                        "avg_px": None, "filled_sz": None, "error": None, "raw": {}}
        self.info._snap = _snap(positions=[_pos("BTC")])

        self.assertTrue(self.executor().on_exit(pid, "ma20", 84400.0))

        # 未成交 → 撤掉这张平仓单，再挂回止损；本地**不关账**。
        self.assertIn(("cancel", "BTC", UUID_OID_EXIT), self.ex.calls)
        self.assertEqual(self.db.position_by_id(pid)["status"], "open")

    def test_reconcile_anchors_uuid_stop_oid(self):
        """对账的 oid 锚定是 str() 比较：UUID 止损单的成交要能被认成 stop 归因。"""
        pid, _bar = self.open_position()
        row = self.db.position_by_id(pid)
        hl_sz = float(row["hl_sz"])
        self.db.set_pos_hl(pid, hl_stop_oid=UUID_OID_STOP)
        self.db.hl_upsert_fill(
            tid="kr-uuid-stop-fill", symbol="BTC_USDT", coin="BTC", side=-1, sz=hl_sz,
            px=80590.0, fee=0.03, fee_token="USD", fee_quote=0.03,
            dir="Close Long", closed_pnl=-20.0, hash="kr-uuid-stop-fill",
            reduce_only=1, oid=UUID_OID_STOP, ts=int(time.time()) + 1)
        self.info._snap = _snap(positions=[])              # 交易所已无仓

        self.executor().reconcile()

        row = self.db.position_by_id(pid)
        self.assertEqual(row["status"], "closed")
        self.assertEqual(row["exit_reason"], "stop")       # oid 命中 → stop，而不是 exchange
        self.assertAlmostEqual(float(row["exit_price"]), 80590.0)


# ----------------------------------------------------------------- dir（大小写不敏感）
class TestDirCaseInsensitive(Base):
    def _fill(self, tid, direction, **kw):
        base = {"tid": tid, "coin": "BTC", "dir": direction, "sz": 0.001, "px": 84000.0,
                "fee": 0.42, "feeToken": "USD", "closedPnl": 5.0,
                "hash": tid, "oid": UUID_OID, "time": int(time.time() * 1000)}
        base.update(kw)
        return base

    def test_sync_fills_lowercase_close_fee_usd(self):
        """小写 "close long" + feeToken=USD：side/reduce_only/fee_quote 全都要对。"""
        added = self.executor()._sync_fills(fills=[self._fill("kr-lower-1", "close long")])
        self.assertEqual(added, 1)
        row = dict(self.db.hl_recent_fills(5)[0])
        self.assertEqual(row["side"], 1)
        self.assertEqual(row["reduce_only"], 1)
        self.assertEqual(row["dir"], "close long")         # 原文保留，不归一化落库
        self.assertEqual(row["oid"], UUID_OID)             # UUID 原样落库
        self.assertAlmostEqual(row["fee_quote"], 0.42)      # USD 费用必须折算成 U
        self.assertFalse([e for e in self.events() if "未能折算" in str(e["message"])],
                         "feeToken=USD 不该触发「手续费未能折算」事件")

    def test_sync_fills_uppercase_open_short(self):
        """HL 原生大写仍是主格式：不能因为加 .lower() 把 side 判反。"""
        added = self.executor()._sync_fills(fills=[self._fill("kr-upper-1", "Open Short",
                                                              closedPnl=0.0)])
        self.assertEqual(added, 1)
        row = dict(self.db.hl_recent_fills(5)[0])
        self.assertEqual(row["side"], -1)
        self.assertEqual(row["reduce_only"], 0)

    def test_sync_fills_prefers_reduce_only_field(self):
        """成交自带 reduce_only 优先（反手/降仓时比 dir 语义更准）。"""
        self.executor()._sync_fills(fills=[self._fill("kr-ro-1", "Open Long", reduce_only=1)])
        self.assertEqual(dict(self.db.hl_recent_fills(5)[0])["reduce_only"], 1)

    def test_sync_fills_derives_reduce_only_without_field(self):
        self.executor()._sync_fills(fills=[self._fill("kr-ro-2", "close short")])
        self.assertEqual(dict(self.db.hl_recent_fills(5)[0])["reduce_only"], 1)

    def test_is_closing_leg_all_cases(self):
        ex = self.executor()
        cases = [
            ({"dir": "Close Long", "reduce_only": 0, "closed_pnl": 0.0}, True),
            ({"dir": "close long", "reduce_only": 0, "closed_pnl": 0.0}, True),
            ({"dir": "Open Long", "reduce_only": 0, "closed_pnl": 0.0}, False),
            ({"dir": "open short", "reduce_only": 0, "closed_pnl": 0.0}, False),
            # 小写 open 且 closedPnl 非 0：开仓腿仍然必须被 "open" 封死
            ({"dir": "open long", "reduce_only": 0, "closed_pnl": 3.0}, False),
            # ADL 之类既无 open 也无 close：靠 closedPnl 判
            ({"dir": "Auto-Deleveraging", "reduce_only": 0, "closed_pnl": -12.0}, True),
        ]
        for fill, want in cases:
            self.assertIs(ex._is_closing_leg(fill), want, fill)

    def test_sync_fills_accepts_krinfo_exact_payload(self):
        """A 组 KrInfo._fills() 的真实键集（含 side/closedPnl_src/fee_estimated/hash）直接吃。"""
        fill = {"coin": "BTC", "px": 84123.5, "sz": 0.00833, "side": "sell",
                "time": int(time.time() * 1000), "hash": "kr:f4b2c1d0-aaaa",
                "dir": "Close Long", "closedPnl": 12.34, "closedPnl_src": "exchange",
                "fee": 0.7003, "feeToken": "USD", "fee_estimated": 1,
                "tid": "f4b2c1d0-aaaa-bbbb-cccc-111122223333", "oid": UUID_OID,
                "reduce_only": 1}
        self.assertEqual(self.executor()._sync_fills(fills=[fill]), 1)
        row = dict(self.db.hl_recent_fills(5)[0])
        self.assertEqual(row["symbol"], "BTC_USDT")
        self.assertEqual(row["side"], 1)                  # "Long" → 多
        self.assertEqual(row["reduce_only"], 1)
        self.assertAlmostEqual(row["fee_quote"], 0.7003)   # feeToken=USD → 1:1
        self.assertAlmostEqual(row["closed_pnl"], 12.34)
        self.assertEqual(row["oid"], UUID_OID)
        self.assertFalse([e for e in self.events() if "未能折算" in str(e["message"])])

    def test_last_fill_px_accepts_lowercase_close(self):
        self.info._fills = [
            {"coin": "BTC", "px": "83010.0", "dir": "open long"},
            {"coin": "BTC", "px": "84420.5", "dir": "close long"},
        ]
        px, _fills = self.executor()._last_fill_px("BTC")
        self.assertAlmostEqual(px, 84420.5)

    def test_last_fill_px_uppercase_still_works(self):
        self.info._fills = [{"coin": "BTC", "px": "84420.5", "dir": "Close Long"}]
        px, _fills = self.executor()._last_fill_px("BTC")
        self.assertAlmostEqual(px, 84420.5)


# --------------------------------------------- 成交顺序 / not_executed（B1/B2/B3）
BASE_MS = 1_760_000_000_000        # 固定基准时刻（毫秒），避免依赖「现在」


def _mk_fill(i, coin="BTC", direction="Open Long", ts_offset_ms=60_000, **kw):
    """第 i 笔成交：时间 = BASE_MS + i*60s。"""
    f = {"tid": f"kr-{i}", "coin": coin, "dir": direction, "sz": 0.001, "px": 84000.0,
         "fee": 0.42, "feeToken": "USD", "closedPnl": 0.0, "hash": f"kr-{i}",
         "oid": UUID_OID, "time": BASE_MS + i * ts_offset_ms}
    f.update(kw)
    return f


class TestFillChronology(Base):
    """venue 契约：**成交列表的顺序不可信**（Kraken 升序 / HL 降序）。

    `_sync_fills` 以前取 `fills[:limit]` —— 在 Kraken 上永远是「最旧的一批」，
    窗口内成交一多，最新成交（含止损平仓）永远进不了库：手续费/净利/对账证据全缺。
    """

    def test_helper_sorts_ascending_keeps_untimed_slots(self):
        from app.hl.executor import _fills_time_ascending
        t1, t2, t3 = (_mk_fill(i) for i in (1, 2, 3))
        u1, u2 = {"tid": "u1", "coin": "BTC"}, {"tid": "u2", "coin": "BTC"}
        out = _fills_time_ascending([u1, t2, t3, u2, t1])
        # 有 time 的行按时间升序回填到「有 time 的槽位」，无 time 的行留在原位
        self.assertEqual([f["tid"] for f in out], ["u1", "kr-1", "kr-2", "u2", "kr-3"])

    def test_sync_fills_takes_latest_from_ascending_feed(self):
        """Kraken `/fills` 升序：必须取最后 limit 条（最新），不是前 limit 条。"""
        added = self.executor()._sync_fills(fills=[_mk_fill(i) for i in range(150)])
        self.assertEqual(added, 100)
        tids = {str(r["tid"]) for r in self.db.hl_recent_fills(500)}
        self.assertIn("kr-149", tids)
        self.assertIn("kr-50", tids)               # 最后 100 条 = 50..149
        self.assertNotIn("kr-49", tids)
        self.assertNotIn("kr-0", tids)

    def test_sync_fills_takes_latest_from_descending_feed(self):
        """HL 桩降序：同一窗口取到的集合必须与升序一致（顺序无关）。"""
        fills = list(reversed([_mk_fill(i) for i in range(150)]))
        self.assertEqual(self.executor()._sync_fills(fills=fills), 100)
        tids = {str(r["tid"]) for r in self.db.hl_recent_fills(500)}
        self.assertIn("kr-149", tids)
        self.assertNotIn("kr-0", tids)
        self.assertEqual(len(tids), 100)

    def test_sync_fills_untimed_rows_still_participate(self):
        """无 time 的行不能因为排序丢键：照原序参与（这里 limit 足够大，全部入库）。"""
        fills = [{"tid": "u1", "coin": "BTC", "dir": "Open Long", "sz": 0.001, "px": 84000.0,
                  "fee": 0.1, "feeToken": "USD", "closedPnl": 0.0},
                 _mk_fill(1)]
        self.assertEqual(self.executor()._sync_fills(fills=fills), 2)

    def test_last_fill_px_picks_latest_close(self):
        """两笔平仓成交（升序）→ 必须取**最新**那笔的价格（旧代码取第一条=最旧）。"""
        self.info._fills = [
            _mk_fill(1, direction="Open Long", px=83000.0),
            _mk_fill(2, direction="Close Long", px=80100.0),
            _mk_fill(3, direction="Open Long", px=82000.0),
            _mk_fill(4, direction="Close Long", px=84500.0),      # 最新平仓
        ]
        px, _raw = self.executor()._last_fill_px("BTC")
        self.assertAlmostEqual(px, 84500.0)

    def test_last_fill_px_picks_latest_close_descending(self):
        """降序（HL 原生）同样取最新，不依赖列表顺序。"""
        self.info._fills = [
            _mk_fill(4, direction="Close Long", px=84500.0),
            _mk_fill(2, direction="Close Long", px=80100.0),
        ]
        px, _raw = self.executor()._last_fill_px("BTC")
        self.assertAlmostEqual(px, 84500.0)

    def test_last_fill_px_respects_opened_at_lower_bound(self):
        """上上轮仓位的平仓成交（早于 opened_at−120s）不得拿来给本仓关账。"""
        opened_at = BASE_MS / 1000.0
        self.info._fills = [
            # 上一轮：早于本仓开仓 1 小时 —— 时间边界外的旧价
            _mk_fill(0, direction="Close Long", px=70000.0,
                     time=BASE_MS - 3600_000),
            _mk_fill(1, direction="Close Long", px=84500.0,
                     time=BASE_MS + 60_000),
        ]
        px, _raw = self.executor()._last_fill_px("BTC", since_ts=opened_at)
        self.assertAlmostEqual(px, 84500.0)

    def test_last_fill_px_no_evidence_inside_bound(self):
        """只有边界外的旧平仓成交 → 不给价（返回 None），而不是拿旧价关账。"""
        self.info._fills = [_mk_fill(0, direction="Close Long", px=70000.0,
                                     time=BASE_MS - 3600_000)]
        px, raw = self.executor()._last_fill_px("BTC", since_ts=BASE_MS / 1000.0)
        self.assertIsNone(px)
        self.assertTrue(raw)                       # 诊断用原文仍返回


class TestNotExecuted(Base):
    """Kraken：cancelled / iocWouldNotExecute → `ok=True, RESTING, not_executed=True`。

    消费侧必须把「单从未进入委托簿」与「挂单在场」区分开，否则：
      * 入场：去撤一张不存在的单（还把 churn 计数当撤单记一次）；
      * 止损：台账记成 resting（以为有保护），实际仓位裸奔。
    """

    def _entry_not_executed(self):
        return {"ok": True, "status": STATUS_RESTING, "oid": UUID_OID, "avg_px": None,
                "filled_sz": None, "error": "", "raw": {}, "not_executed": True}

    def test_entry_not_executed_skips_cancel(self):
        pid = self.add_signal()
        self.ex.entry = self._entry_not_executed()
        self.assertTrue(self.executor().on_entry(pid))
        self.assertEqual([c for c in self.ex.calls if c[0] == "cancel"], [],
                         "not_executed 的单不在委托簿里，撤单是无效调用")
        # 只发了那一张入场 IOC：没有止损单、没有平仓单（整条链在入场失败处收束）
        orders = [c for c in self.ex.calls if c[0] == "order"]
        self.assertEqual(len(orders), 1)
        self.assertIsNone(orders[0][6])            # 不是触发单（没有止损）
        self.assertFalse(orders[0][5])             # 不是 reduce_only 平仓单
        bar_ms = int(self.db.position_by_id(pid)["signal_bar_ms"])
        self.assertEqual(self.db.hl_order("BTC_USDT", bar_ms, PURPOSE_ENTRY)["status"], "failed")

    def test_stop_not_executed_triggers_immediate_flatten(self):
        """止损 not_executed = 挂单失败 → 仓位无保护 → 必须走「立即平仓」闭环。"""
        pid = self.add_signal()
        self.ex.stop = {"ok": True, "status": STATUS_RESTING, "oid": UUID_OID_STOP,
                        "avg_px": None, "filled_sz": None, "error": "", "raw": {},
                        "not_executed": True}
        self.assertTrue(self.executor().on_entry(pid))
        bar_ms = int(self.db.position_by_id(pid)["signal_bar_ms"])
        # 台账：止损行必须 failed（绝不能记 resting 让面板以为有保护）
        self.assertEqual(self.db.hl_order("BTC_USDT", bar_ms, PURPOSE_STOP)["status"], "failed")
        self.assertEqual(self.db.position_by_id(pid)["hl_status"], "aborted")
        # 交易所侧只有一张平仓单（reduce_only、非触发单）
        closes = [c for c in self.ex.calls
                  if c[0] == "order" and c[5] is True and c[6] is None]
        self.assertEqual(len(closes), 1)
        self.assertTrue(any("止损" in str(m) for m in self.notifier.messages))

    def test_real_resting_stop_is_still_accepted(self):
        """反证：真挂上的止损（not_executed=False/缺键）不能被误判成失败。"""
        pid = self.add_signal()
        self.ex.stop = {"ok": True, "status": STATUS_RESTING, "oid": UUID_OID_STOP,
                        "avg_px": None, "filled_sz": None, "error": None, "raw": {}}
        self.assertTrue(self.executor().on_entry(pid))
        bar_ms = int(self.db.position_by_id(pid)["signal_bar_ms"])
        self.assertEqual(self.db.hl_order("BTC_USDT", bar_ms, PURPOSE_STOP)["status"], "resting")
        self.assertEqual(self.db.position_by_id(pid)["hl_status"], "open")


class TestReconcileOtherLegs(Base):
    """B4：同一账户里的 MR 腿仓（QQQ）不是「人工单」。"""

    def _snap_with(self, *coins):
        return _snap(positions=[_pos(c, size=1.0, entry=93.0) for c in coins],
                     orders=[_stop_order("BTC")])

    def test_mr_leg_position_is_not_manual(self):
        pid, _bar = self.open_position()                      # BTC 有本地台账
        self.info._snap = self._snap_with("BTC", "QQQ")
        res = self.executor().reconcile()
        self.assertEqual([p for p in res["problems"] if "QQQ" in p], [])
        self.assertTrue(res["ok"], res["problems"])           # 不再因此置 mismatch/kill
        self.assertFalse(self.db.hl_kill())

    def test_unknown_coin_still_reported(self):
        """反证：注册表里也没有的 coin 仍要报出来（fail-closed，不认识就得被人看见）。"""
        pid, _bar = self.open_position()
        self.info._snap = self._snap_with("BTC", "DOGE")
        res = self.executor().reconcile()
        self.assertTrue(any("DOGE" in p for p in res["problems"]), res["problems"])
        self.assertFalse(res["ok"])


class TestRepairUnderMachineKill(Base):
    """B6：机器触发的 kill（reconcile/size）不该挡住「补挂止损」这个减风险动作。"""

    def _row_and_snap(self):
        pid, _bar = self.open_position()
        row = dict(self.db.position_by_id(pid))
        snap = _snap(positions=[_pos("BTC")], orders=[])       # 交易所侧确实没有止损
        return pid, row, snap

    def test_size_kill_allows_stop_repair(self):
        pid, row, snap = self._row_and_snap()
        self.db.hl_set_kill(True, "数量对账不一致", origin="size")
        self.ex.calls.clear()
        self.executor()._repair_missing_stop(row, snap, kill_before=True)
        triggers = [c for c in self.ex.calls if c[0] == "order" and c[6] is not None]
        self.assertEqual(len(triggers), 1, "size kill 下仍必须补挂止损（减风险动作）")

    def test_manual_kill_still_blocks(self):
        pid, row, snap = self._row_and_snap()
        self.db.hl_set_kill(True, "人工", origin="manual")
        self.ex.calls.clear()
        self.executor()._repair_missing_stop(row, snap, kill_before=True)
        self.assertEqual(self.ex.calls, [], "人工 kill 期间自动化不许动交易所")


class TestExitPriceFallback(Base):
    """B5：平仓 IOC 限价算不出来时用**该 coin** 的标记价，不用旧 ref_price。"""

    def test_exit_uses_mark_px_when_limit_fails(self):
        pid, _bar = self.open_position()
        self.ex.calls.clear()

        def boom(coin, is_buy, mid=None):
            raise RuntimeError("tickSize 读不到")
        self.ex.slippage_limit = boom
        self.ex.exit = {"ok": True, "status": STATUS_FILLED, "oid": UUID_OID_EXIT,
                        "avg_px": 83000.0, "filled_sz": None, "error": None, "raw": {}}
        self.info._snap = _snap(positions=[_pos("BTC", size=0.01, entry=83000.0)])

        self.assertTrue(self.executor().on_exit(pid, "ma20", ref_price=70000.0))

        orders = [c for c in self.ex.calls if c[0] == "order"]
        self.assertEqual(len(orders), 1)
        # _pos() 的 mark_px = entry+50 = 83050（ref_price 70000 绝不许出现在限价里）
        self.assertEqual(orders[0][4], 83050.0)
        self.assertNotEqual(orders[0][4], 70000.0)

    def test_exit_refuses_to_send_without_mark_px(self):
        """连标记价都没有 → 这轮不发单（止损已撤，宁可喊人也不拿旧价乱发）。"""
        pid, _bar = self.open_position()
        self.ex.calls.clear()

        def boom(coin, is_buy, mid=None):
            raise RuntimeError("tickSize 读不到")
        self.ex.slippage_limit = boom
        self.info._snap = _snap(positions=[])        # 快照里也没有该 coin
        self.assertTrue(self.executor().on_exit(pid, "ma20", ref_price=70000.0))
        self.assertEqual([c for c in self.ex.calls if c[0] == "order"], [])
        self.assertTrue(any("未下平仓单" in str(m) for m in self.notifier.messages))


# ------------------------------------------------------------------- agent 清仓取整
class _FlatInfo:
    """清仓用的假 info：第一次快照有仓，平完后复查为空。"""

    def __init__(self, positions, tick_px=76000.0):
        self._positions = list(positions)
        self._tick_px = tick_px
        self.round_calls = []
        self.snapshot_calls = 0

    def snapshot(self, address, mids=None):
        self.snapshot_calls += 1
        positions = self._positions if self.snapshot_calls == 1 else []
        return {"ts": time.time(), "address": address, "equity": 1000.0,
                "positions": positions, "open_orders": []}

    def specs(self, coins=None):
        return {"BTC": {"sz_decimals": 5}}

    def round_price(self, coin, px):
        # Kraken 语义：返回 tick 整数倍（PF_XBTUSD tick=1）
        self.round_calls.append((coin, px))
        return float(self._tick_px)


class _NoRoundInfo(_FlatInfo):
    """没有 venue round_price（HL 版 info）→ 必须回退 hl_sizing.round_price。"""

    def __init__(self, positions, tick_px=76000.0):
        super().__init__(positions, tick_px)
        self.round_price = None            # getattr 拿到的不是 callable


class _FlatEx:
    def __init__(self):
        self.calls = []

    def cancel_all_orders(self):
        return {"ok": True, "error": None, "raw": {"oids": [], "failed": []}}

    def mid(self, coin):
        return 83000.0

    def slippage_limit(self, coin, is_buy, mid=None):
        px = 83000.0 if mid is None else mid
        return px * (1 + 0.003) if is_buy else px * (1 - 0.003)

    def place_order(self, coin, is_buy, sz, px, reduce_only=False, tif="Ioc",
                    trigger_px=None, cloid=None):
        self.calls.append(("order", coin, is_buy, sz, px, reduce_only))
        return {"ok": True, "status": "filled", "oid": UUID_OID_EXIT, "avg_px": px,
                "filled_sz": sz, "error": None, "raw": {}}


class TestFlattenPriceRounding(Base):
    def _params(self):
        return ExecParams(mode=MODE_LIVE, account_address="0xabc", private_key="0xkey")

    def test_flatten_uses_venue_round_price(self):
        info = _FlatInfo([_pos("BTC", 0.01)])
        ex = _FlatEx()
        res = A.flatten_all(self.db, self._params(), info, ex, self.notifier)

        self.assertTrue(res["ok"], res)
        orders = [c for c in ex.calls if c[0] == "order"]
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0][4], 76000.0)        # venue 取的价，不是 HL 规则
        self.assertEqual(info.round_calls[0][0], "BTC")
        # 反证：HL 规则会把「平多」的滑点限价算成 83000×0.997≈82751 这类非 tick 价
        hl_rule = hl_sizing.round_price(ex.slippage_limit("BTC", False, 83000.0), 5)
        self.assertNotEqual(orders[0][4], hl_rule)

    def test_flatten_falls_back_to_hl_round_when_venue_missing(self):
        info = _NoRoundInfo([_pos("BTC", 0.01)])
        ex = _FlatEx()
        res = A.flatten_all(self.db, self._params(), info, ex, self.notifier)

        self.assertTrue(res["ok"], res)
        orders = [c for c in ex.calls if c[0] == "order"]
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0][4],
                         hl_sizing.round_price(ex.slippage_limit("BTC", False, 83000.0), 5))


if __name__ == "__main__":
    unittest.main()
