"""第三轮审核（audit-r3）执行器类缺陷的回归测试。

每条用例对应审核结论里的一条缺陷，命名前缀 T1..T8 与报告编号一一对应：

T1 离场第 1 步撤止损：**结果必须检查**（撤不掉不能把行写成 cancelled）
T2 exit:rehang 行必须有 oid（同一离场 bar 多次重挂要更新到最新）
T3 平仓关账后复核残留止损（trusted=False 要喊人；撤不掉要收成一条告警）
T4 `_has_stop` 严格化（方向/数量/trigger_px）
T5 读不到账户快照时告警必须真正推送
T6 部分成交后本地 hl_sz 立刻减到剩余量
T7 pending 行能升级成 open（或进 missing_stop）
T8 已实现亏损熔断（warn 24h 节流 / kill 只写一次）

全部离线：临时 sqlite + 复用 tests/test_hl_executor.py 的假件（同一套 _snap/_pos/
_stop_order，不自造与真实实现不符的 fixture）。
"""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from app.db import DB
from app.hl import (MODE_LIVE, PURPOSE_ENTRY, PURPOSE_EXIT_REHANG, PURPOSE_STOP,
                    EV_ERROR, EV_KILL, EV_RECONCILE)
from app.hl.executor import REALIZED_DD_WARN_KEY, Executor, ExecParams
from app.hl.exchange import STATUS_ERROR, STATUS_FILLED, STATUS_RESTING
from tests.test_hl_executor import (FakeExchange, FakeInfo, FakeNotifier, _pos, _snap,
                                    _stop_order)

UUID_STOP = "1f0c2b7a-3d44-4a90-9f6e-77aa12bc9d01"
UUID_REHANG_1 = "aaaaaaaa-1111-4a90-9f6e-000000000001"
UUID_REHANG_2 = "bbbbbbbb-2222-4a90-9f6e-000000000002"
UUID_EXIT_1 = "cccccccc-3333-4a90-9f6e-000000000003"
UUID_EXIT_2 = "dddddddd-4444-4a90-9f6e-000000000004"


class FakeEx(FakeExchange):
    """在 FakeExchange 上加两件事：`_open_orders_checked()`（平仓后复核用）与可控的撤单失败。"""

    def __init__(self, *a, open_orders=None, trusted=True, cancel_ok=True, **kw):
        super().__init__(*a, **kw)
        self.orders_now = list(open_orders or [])
        self.trusted = trusted
        self.cancel_ok = cancel_ok

    def _open_orders_checked(self):
        return list(self.orders_now), bool(self.trusted)

    def cancel(self, coin, oid):
        self.calls.append(("cancel", coin, oid))
        ok = self.cancel_ok(coin, oid) if callable(self.cancel_ok) else self.cancel_ok
        if not ok:
            return {"ok": False, "status": STATUS_ERROR, "oid": oid,
                    "error": "cancel rejected", "avg_px": None, "filled_sz": None, "raw": {}}
        return {"ok": True, "status": STATUS_RESTING, "oid": oid, "error": None,
                "avg_px": None, "filled_sz": None, "raw": {}}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(str(Path(self.tmp.name) / "t.db"))
        self.notifier = FakeNotifier()
        self.info = FakeInfo()
        self.ex = FakeEx()
        self.p = ExecParams(mode=MODE_LIVE, account_address="0xabc", private_key="0xkey",
                            equity=1000.0, risk_pct=0.02, leverage=5.0, code_rev="audit3")

    def tearDown(self):
        self.tmp.cleanup()

    def executor(self, **kw):
        for k, v in kw.items():
            setattr(self.p, k, v)
        return Executor(self.db, self.p, self.info, self.ex, notifier=self.notifier)

    def add_signal(self, symbol="BTC_USDT", side=1, entry=83000.0, stop=80600.0, bar_ms=None):
        if bar_ms is None:
            bar_ms = int(time.time() * 1000) - 5 * 60 * 1000 - 4 * 3600 * 1000
        return self.db.create_position(symbol=symbol, signal_bar_ms=bar_ms, side=side,
                                       entry_price=entry, stop_price=stop,
                                       stop_dist=abs(entry - stop) / entry, contracts=1,
                                       coin_qty=0.001, notional=entry * 0.001,
                                       risk_u=20.0, equity_at_entry=1000.0)

    def open_position(self, ex=None):
        """走真实 on_entry 建一个 open 仓（拿到真实 hl_sz / stop 行 / oid）。"""
        pid = self.add_signal()
        self.assertTrue((ex or self.executor()).on_entry(pid))
        return pid, int(self.db.position_by_id(pid)["signal_bar_ms"])

    def orders(self, purpose=None):
        return [r for r in self.db.hl_recent_orders(100)
                if purpose is None or r["purpose"] == purpose]

    def events(self, kind=None):
        return [dict(r) for r in self.db.hl_recent_events(100, kind=kind)]

    def msgs(self, needle):
        return [m for m in self.notifier.messages if needle in m]


# --------------------------------------------------------------------- T1
class TestT1CancelResultChecked(Base):
    """离场撤止损：`self.ex.cancel` 的返回值必须检查。"""

    def _live_exit(self, cancel_ok):
        pid, bar_ms = self.open_position()
        self.db.hl_update_order("BTC_USDT", bar_ms, PURPOSE_STOP, oid=UUID_STOP,
                                status="resting")
        self.db.set_pos_hl(pid, hl_stop_oid=UUID_STOP)
        self.ex.cancel_ok = cancel_ok
        row = self.db.position_by_id(pid)
        self.ex.exit = {"ok": True, "status": STATUS_FILLED, "oid": UUID_EXIT_1,
                        "avg_px": 84500.0, "filled_sz": float(row["hl_sz"]),
                        "error": None, "raw": {}}
        self.info._snap = _snap(positions=[_pos("BTC")])
        self.notifier.messages.clear()
        self.assertTrue(self.executor().on_exit(pid, "ma20", 84400.0))
        return pid, bar_ms

    def test_cancel_failure_keeps_row_resting_and_alerts_once(self):
        pid, bar_ms = self._live_exit(cancel_ok=False)
        o = self.db.hl_order("BTC_USDT", bar_ms, PURPOSE_STOP)
        self.assertEqual(o["status"], "resting",
                         "撤单失败时**不能**把行改成 cancelled（那等于谎报已撤）")
        self.assertIn("cancel rejected", str(o["error"] or ""))
        self.assertTrue(any("撤止损" in e["message"] for e in self.events(EV_ERROR)),
                        "撤单失败要留 EV_ERROR")
        fails = self.msgs("撤止损单失败")
        self.assertEqual(len(fails), 1, f"同一轮只发一条汇总告警，实际 {len(fails)} 条")
        self.assertIn(UUID_STOP, fails[0])

    def test_cancel_success_marks_cancelled(self):
        """回归：撤单成功（ok=True，filled/cancelled/notFound 都算 ok）照旧落 cancelled。"""
        pid, bar_ms = self._live_exit(cancel_ok=True)
        self.assertEqual(self.db.hl_order("BTC_USDT", bar_ms, PURPOSE_STOP)["status"],
                         "cancelled")
        self.assertEqual(self.msgs("撤止损单失败"), [])


# --------------------------------------------------------------------- T2
class TestT2RehangOid(Base):
    """exit:rehang 行必须带 oid（否则下次离场撤残留止损 `and r['oid']` 永远为假）。"""

    def test_rehang_row_has_oid(self):
        pid, _bar = self.open_position()
        self.ex.stop = {"ok": True, "status": STATUS_RESTING, "oid": UUID_REHANG_1,
                        "avg_px": None, "filled_sz": None, "error": None, "raw": {}}
        row = self.db.position_by_id(pid)
        self.ex.exit = {"ok": True, "status": STATUS_RESTING, "oid": UUID_EXIT_1,
                        "avg_px": None, "filled_sz": None, "error": None, "raw": {}}
        self.info._snap = _snap(positions=[_pos("BTC")])
        ex = self.executor()
        self.assertTrue(ex.on_exit(pid, "ma20", 84400.0))
        rows = self.orders(PURPOSE_EXIT_REHANG)
        self.assertEqual(len(rows), 1)
        self.assertEqual(str(rows[0]["oid"]), UUID_REHANG_1,
                         "重挂止损的 oid 必须落库（以前恒为 NULL）")
        self.assertEqual(rows[0]["status"], STATUS_RESTING)

    def test_second_rehang_same_bar_updates_oid(self):
        """同 (symbol, 离场 bar, purpose) 第二次重挂：INSERT OR IGNORE 不写，必须靠 update。"""
        pid, _bar = self.open_position()
        self.p.churn_max = 99                     # 本用例只关心 oid，不要被 churn 熔断干扰
        self.info._snap = _snap(positions=[_pos("BTC")])
        self.ex.exit = {"ok": True, "status": STATUS_RESTING, "oid": UUID_EXIT_1,
                        "avg_px": None, "filled_sz": None, "error": None, "raw": {}}
        self.ex.stop = {"ok": True, "status": STATUS_RESTING, "oid": UUID_REHANG_1,
                        "avg_px": None, "filled_sz": None, "error": None, "raw": {}}
        self.assertTrue(self.executor().on_exit(pid, "ma20", 84400.0))
        # 第二次离场：撤掉刚挂的 rehang，再用新 oid 重挂
        self.ex.exit = {"ok": True, "status": STATUS_RESTING, "oid": UUID_EXIT_2,
                        "avg_px": None, "filled_sz": None, "error": None, "raw": {}}
        self.ex.stop = {"ok": True, "status": STATUS_RESTING, "oid": UUID_REHANG_2,
                        "avg_px": None, "filled_sz": None, "error": None, "raw": {}}
        self.assertTrue(self.executor().on_exit(pid, "ma20", 84400.0))
        rows = self.orders(PURPOSE_EXIT_REHANG)
        self.assertEqual(len(rows), 1, "同一 bar 只有一个 rehang 键（幂等）")
        self.assertEqual(str(rows[0]["oid"]), UUID_REHANG_2,
                         "重挂后 oid 必须是**最新**那张，否则撤残留止损会撤错单")


# --------------------------------------------------------------------- T3
class TestT3VerifyResidualStops(Base):
    """平仓关账后复核交易所侧残留 reduce-only 止损。"""

    def _open_with_stop(self):
        pid, bar_ms = self.open_position()
        self.db.hl_update_order("BTC_USDT", bar_ms, PURPOSE_STOP, oid=UUID_STOP,
                                status="resting")
        return pid, bar_ms

    def test_untrusted_order_book_alerts_without_cancel(self):
        pid, _bar = self._open_with_stop()
        self.ex.trusted = False
        self.ex.orders_now = [_stop_order("BTC")]
        self.ex.calls.clear()
        self.executor()._verify_no_residual_stops(self.db.position_by_id(pid), "BTC")
        self.assertEqual([c for c in self.ex.calls if c[0] == "cancel"], [],
                         "挂单表不可信时绝不能凭猜撤单")
        hits = self.msgs("平仓后挂单表读取失败")
        self.assertEqual(len(hits), 1)
        self.assertIn("Kraken", hits[0])

    def test_residual_stop_is_cancelled_and_row_closed(self):
        pid, bar_ms = self._open_with_stop()
        self.ex.trusted = True
        self.ex.orders_now = [_stop_order("BTC")]          # oid=99 的 reduce-only 触发单
        self.db.hl_update_order("BTC_USDT", bar_ms, PURPOSE_STOP, oid=99, status="resting")
        self.executor()._verify_no_residual_stops(self.db.position_by_id(pid), "BTC")
        self.assertIn(("cancel", "BTC", 99), self.ex.calls)
        self.assertEqual(self.db.hl_order("BTC_USDT", bar_ms, PURPOSE_STOP)["status"],
                         "cancelled", "按 oid 回写对应 DB 行")

    def test_other_coin_is_untouched(self):
        pid, _bar = self._open_with_stop()
        self.ex.trusted = True
        self.ex.orders_now = [_stop_order("ETH", trigger=2400.0)]
        self.ex.calls.clear()
        self.executor()._verify_no_residual_stops(self.db.position_by_id(pid), "BTC")
        self.assertEqual([c for c in self.ex.calls if c[0] == "cancel"], [],
                         "只撤本 coin 的挂单，绝不 cancel_all")

    def test_cancel_failure_collects_one_alert(self):
        pid, bar_ms = self._open_with_stop()
        self.ex.trusted = True
        self.ex.orders_now = [_stop_order("BTC"), _stop_order("BTC")]
        self.ex.orders_now[0]["oid"] = "oid-a"
        self.ex.orders_now[1]["oid"] = "oid-b"
        self.ex.cancel_ok = False
        self.notifier.messages.clear()
        self.executor()._verify_no_residual_stops(self.db.position_by_id(pid), "BTC")
        hits = self.msgs("reduce-only")
        self.assertEqual(len(hits), 1, f"撤不掉的要收成一条告警，实际 {len(hits)} 条")
        self.assertIn("oid-a", hits[0])
        self.assertIn("oid-b", hits[0])

    def test_close_path_calls_verify(self):
        """集成：全量平仓成功后确实走了一次复核（orders_now 里的残留单被撤）。"""
        pid, bar_ms = self._open_with_stop()
        self.ex.trusted = True
        row = self.db.position_by_id(pid)
        self.ex.exit = {"ok": True, "status": STATUS_FILLED, "oid": UUID_EXIT_1,
                        "avg_px": 84500.0, "filled_sz": float(row["hl_sz"]),
                        "error": None, "raw": {}}
        self.info._snap = _snap(positions=[_pos("BTC")])
        self.ex.orders_now = [_stop_order("BTC")]
        self.executor().on_exit(pid, "ma20", 84400.0)
        self.assertIn(("cancel", "BTC", 99), self.ex.calls)


# --------------------------------------------------------------------- T4
class TestT4HasStopStrict(Base):
    """`_has_stop` 不能 fail-open：方向、数量、trigger_px 都要看。"""

    def test_old_signature_still_works(self):
        ex = self.executor()
        snap = _snap(orders=[_stop_order("BTC")])
        self.assertTrue(ex._has_stop(snap, "BTC"), "默认参数 = 旧语义（只判有没有）")

    def test_wrong_direction_is_not_a_stop(self):
        ex = self.executor()
        long_snap = _snap(orders=[{"coin": "BTC", "oid": 1, "is_buy": True, "sz": 0.01,
                                  "reduce_only": True, "trigger_px": 80600.0,
                                  "is_trigger": True}])
        self.assertFalse(ex._has_stop(long_snap, "BTC", side=1),
                         "多仓的止损必须是卖单（is_buy=False）")
        self.assertTrue(ex._has_stop(long_snap, "BTC", side=-1),
                        "同一张买单对空仓就是正确方向的止损")

    def test_insufficient_size_is_not_a_stop(self):
        ex = self.executor()
        snap = _snap(orders=[_stop_order("BTC")])       # sz=0.01
        self.assertFalse(ex._has_stop(snap, "BTC", side=1, sz=0.05),
                         "只剩 1/5 的量的触发单保护不了整个仓")
        self.assertTrue(ex._has_stop(snap, "BTC", side=1, sz=0.01))

    def test_zero_trigger_px_is_not_a_stop(self):
        ex = self.executor()
        bad = _stop_order("BTC")
        bad["trigger_px"] = 0.0
        self.assertFalse(ex._has_stop(_snap(orders=[bad]), "BTC"))

    def test_reconcile_reports_missing_stop_for_wrong_direction(self):
        pid, _bar = self.open_position()
        self.info._snap = _snap(
            positions=[_pos("BTC")],
            orders=[{"coin": "BTC", "oid": 1, "is_buy": True, "sz": 0.01,
                     "reduce_only": True, "trigger_px": 80600.0, "is_trigger": True}])
        rec = self.executor().reconcile(self.info._snap)
        self.assertEqual(len(rec["missing_stop"]), 1)
        self.assertTrue(any("没有止损挂单" in p for p in rec["problems"]))


# --------------------------------------------------------------------- T5
class TestT5SnapshotFailAlertPushed(Base):
    """读不到账户快照：告警必须**推送**（旧代码只 append 后 return，永远发不出去）。"""

    def test_alert_is_pushed(self):
        self.info = FakeInfo(raise_on_snapshot="boom")
        out = self.executor().run()
        self.assertTrue(out, "返回值仍应包含该文案")
        hits = [m for m in self.notifier.messages if "读不到 Kraken 账户快照" in m]
        self.assertEqual(len(hits), 1, f"必须真的推出去，实际 {len(hits)} 条")


# --------------------------------------------------------------------- T6
class TestT6PartialFillReducesHlSz(Base):
    """部分成交离场后本地 hl_sz 必须立刻减到剩余量。"""

    def test_hl_sz_becomes_remain(self):
        pid, _bar = self.open_position()
        row = self.db.position_by_id(pid)
        total = float(row["hl_sz"])
        filled = round(total / 2, 8)
        remain = total - filled
        self.ex.exit = {"ok": True, "status": STATUS_FILLED, "oid": UUID_EXIT_1,
                        "avg_px": 84500.0, "filled_sz": filled, "error": None, "raw": {}}
        self.ex.stop = {"ok": True, "status": STATUS_RESTING, "oid": UUID_REHANG_1,
                        "avg_px": None, "filled_sz": None, "error": None, "raw": {}}
        self.info._snap = _snap(positions=[_pos("BTC")])
        self.assertTrue(self.executor().on_exit(pid, "ma20", 84400.0))
        after = self.db.position_by_id(pid)
        self.assertAlmostEqual(float(after["hl_sz"]), remain, places=8,
                               msg="本地量不减 → 下轮按原量平仓 + 数量对账误 kill")
        self.assertEqual(after["status"], "open", "剩余量还在交易所，本地不关账")
        evs = [e for e in self.events() if "部分成交" in e["message"]]
        self.assertTrue(any("remain" in (e["data"] or "") for e in evs),
                        f"事件里要带 remain：{[e['message'] for e in evs]}")


# --------------------------------------------------------------------- T7
class TestT7PendingUpgradesToOpen(Base):
    """pending 行（成交后崩溃）要能升级成 open，或在缺止损时进 missing_stop。"""

    def _pending_with_entry(self):
        pid = self.add_signal()
        bar_ms = int(self.db.position_by_id(pid)["signal_bar_ms"])
        self.db.hl_record_intent("BTC_USDT", "BTC", bar_ms, PURPOSE_ENTRY, 1, 0.01,
                                 status="filled", oid="entry-oid")
        return pid

    def test_pending_with_stop_becomes_open(self):
        pid = self._pending_with_entry()
        snap = _snap(positions=[_pos("BTC")], orders=[_stop_order("BTC")])
        rec = self.executor().reconcile(snap)
        row = self.db.position_by_id(pid)
        self.assertEqual(row["hl_status"], "open")
        self.assertAlmostEqual(float(row["hl_sz"]), 0.01, places=6,
                               msg="按交易所 size 反填 hl_sz")
        self.assertTrue(rec["ok"], f"补齐后不该再有 problems：{rec['problems']}")
        self.assertTrue(any("pending 行补齐" in e["message"]
                            for e in self.events(EV_RECONCILE)))

    def test_pending_without_stop_goes_to_missing_stop(self):
        pid = self._pending_with_entry()
        snap = _snap(positions=[_pos("BTC")], orders=[])
        rec = self.executor().reconcile(snap)
        self.assertEqual([int(r["id"]) for r in rec["missing_stop"]], [pid])
        self.assertEqual(self.db.position_by_id(pid)["status"], "pending",
                         "没有止损就不升级（等补挂成功路径自己升）")

    def test_pending_without_entry_order_is_ignored(self):
        pid = self.add_signal()                  # 没有 entry 单 → 交易所本来就没仓
        snap = _snap(positions=[_pos("BTC")], orders=[_stop_order("BTC")])
        self.executor().reconcile(snap)
        row = self.db.position_by_id(pid)
        self.assertEqual(row["hl_status"] or "", "", "没发过单的 pending 行不许被当成真实仓位")

    def test_pending_with_opposite_side_is_ignored(self):
        pid = self._pending_with_entry()
        snap = _snap(positions=[_pos("BTC", side=-1)], orders=[_stop_order("BTC")])
        self.executor().reconcile(snap)
        self.assertNotEqual(self.db.position_by_id(pid)["hl_status"], "open",
                            "方向不一致时不猜（留给现有 mismatch 逻辑）")


# --------------------------------------------------------------------- T8
class TestT8RealizedDdGuard(Base):
    """已实现亏损熔断：只读 hl_fills，kill 只写一次，warn 24h 节流。"""

    def _fill(self, pnl, fee=0.0, ts=None, tid=None):
        self.db.hl_upsert_fill(
            tid=tid or f"t-{pnl}-{time.time()}", symbol="BTC_USDT", coin="BTC", side=-1,
            sz=0.01, px=83000.0, fee=fee, fee_token="USD", fee_quote=fee,
            dir="Close Long", closed_pnl=pnl, oid="o-1",
            ts=int(ts if ts is not None else time.time()))

    def test_kill_once_at_threshold(self):
        self._fill(-250.0, fee=10.0)                 # 净 -260 / 1000 = -26%
        ex = self.executor()
        alerts = []
        snap = {"ts": time.time(), "equity_total": 1000.0}
        r1 = ex._realized_dd_guard(snap, alerts)
        self.assertEqual(r1["action"], "kill")
        self.assertTrue(self.db.hl_kill())
        self.assertEqual(self.db.hl_kill_origin(), "realized_dd")
        self.assertEqual(len(alerts), 1)
        self.assertIn("已实现亏损熔断", alerts[0])
        kills = [e for e in self.events(EV_KILL) if "已实现净额" in e["message"]]
        self.assertEqual(len(kills), 1)
        # 第二轮：已有 kill → 不重复写、不再推
        alerts2 = []
        r2 = ex._realized_dd_guard(snap, alerts2)
        self.assertEqual(r2["action"], "already_killed")
        self.assertEqual(alerts2, [])

    def test_warn_throttled_24h(self):
        self._fill(-120.0)                           # -12%
        ex = self.executor()
        snap = {"ts": time.time(), "equity_total": 1000.0}
        alerts = []
        r = ex._realized_dd_guard(snap, alerts)
        self.assertEqual(r["action"], "warn")
        self.assertEqual(len(alerts), 1)
        key, text = alerts[0]
        self.assertEqual(key, REALIZED_DD_WARN_KEY)
        self.assertIn("未触发熔断", text)
        # 模拟「推送成功后写节流键」（_run_locked 末尾循环的行为）
        self.db.set_config(REALIZED_DD_WARN_KEY, str(int(time.time())))
        alerts2 = []
        r2 = ex._realized_dd_guard(snap, alerts2)
        self.assertEqual(r2["action"], "warn_throttled")
        self.assertEqual(alerts2, [])

    def test_no_judgement_without_equity(self):
        self._fill(-500.0)
        alerts = []
        self.assertIsNone(self.executor()._realized_dd_guard({"equity_total": 0.0}, alerts))
        self.assertEqual(alerts, [])

    def test_window_excludes_old_fills(self):
        self._fill(-500.0, ts=time.time() - 40 * 86400, tid="old")   # 40 天前：窗外
        alerts = []
        r = self.executor()._realized_dd_guard({"equity_total": 1000.0}, alerts)
        self.assertEqual(r["action"], "none")
        self.assertFalse(self.db.hl_kill())

    def test_wired_into_run(self):
        """接线检查：run() 里 L1 之后、DD 之前真的调用了这条线。"""
        self._fill(-300.0)
        out = self.executor().run()
        self.assertTrue(self.db.hl_kill())
        self.assertEqual(self.db.hl_kill_origin(), "realized_dd")
        self.assertTrue(any("已实现亏损熔断" in a for a in out))


if __name__ == "__main__":
    unittest.main()
