"""第四轮审核（audit-r6）执行器类缺陷的回归测试。

R1 兜底平仓**部分成交**不能整笔关账（`_repair_missing_stop` / 开仓保护失败 / 离场兜底）
R2 离场先撤止损、平仓发不出去 → 必须立刻把止损挂回去（裸仓窗口 ≤ 一轮 reconcile 的旧行为）

全部离线：临时 sqlite + 复用 tests/test_hl_executor.py 的假件。
"""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from app.db import DB
from app.hl import (EV_ERROR, MODE_LIVE, PURPOSE_STOP, PURPOSE_EXIT_REHANG)
from app.hl.exchange import STATUS_ERROR, STATUS_FILLED, STATUS_RESTING
from tests.test_hl_executor import (FakeExchange, FakeInfo, FakeNotifier, _pos, _snap,
                                    _stop_order)
from app.hl.executor import Executor, ExecParams

UUID_STOP = "1f0c2b7a-3d44-4a90-9f6e-77aa12bc9d01"
UUID_FIX_STOP = "eeeeeeee-5555-4a90-9f6e-000000000005"


class Ex(FakeExchange):
    """在 FakeExchange 上加 `_open_orders_checked()`（平仓后残留止损复核用）。"""

    def __init__(self, *a, open_orders=None, trusted=True, **kw):
        super().__init__(*a, **kw)
        self.orders_now = list(open_orders or [])
        self.trusted = trusted

    def _open_orders_checked(self):
        return list(self.orders_now), bool(self.trusted)


def _fail(msg="transport timeout"):
    return {"ok": False, "status": STATUS_ERROR, "error": msg, "oid": None,
            "avg_px": None, "filled_sz": None, "raw": {}}


def _partial(filled, px):
    return {"ok": True, "status": STATUS_FILLED, "oid": 7, "avg_px": px,
            "filled_sz": filled, "error": None, "raw": {}}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(str(Path(self.tmp.name) / "t.db"))
        self.notifier = FakeNotifier()
        self.info = FakeInfo()
        self.ex = Ex()
        self.p = ExecParams(mode=MODE_LIVE, account_address="0xabc", private_key="0xkey",
                            equity=1000.0, risk_pct=0.02, leverage=5.0, code_rev="audit6",
                            churn_max=99)

    def tearDown(self):
        self.tmp.cleanup()

    def executor(self, **kw):
        for k, v in kw.items():
            setattr(self.p, k, v)
        return Executor(self.db, self.p, self.info, self.ex, notifier=self.notifier)

    def open_position(self):
        bar_ms = int(time.time() * 1000) - 5 * 60 * 1000 - 4 * 3600 * 1000
        pid = self.db.create_position(symbol="BTC_USDT", signal_bar_ms=bar_ms, side=1,
                                      entry_price=83000.0, stop_price=80600.0,
                                      stop_dist=0.03, contracts=1, coin_qty=0.001,
                                      notional=83.0, risk_u=20.0, equity_at_entry=1000.0)
        self.assertTrue(self.executor().on_entry(pid))
        self.db.hl_set_kill(False)
        return pid, int(self.db.position_by_id(pid)["signal_bar_ms"])

    def events(self, kind=None):
        return [dict(r) for r in self.db.hl_recent_events(100, kind=kind)]

    def msgs(self, needle):
        return [m for m in self.notifier.messages if needle in m]


class TestR1RepairPartialClose(Base):
    """`_repair_missing_stop` 的兜底平仓：只成交一部分时必须保留剩余量。"""

    def _prep(self):
        pid, bar_ms = self.open_position()
        self.db.hl_update_order("BTC_USDT", bar_ms, PURPOSE_STOP, oid=UUID_STOP,
                                status="resting")
        self.ex.stop = _fail("stop rejected")          # 补挂失败 → 走兜底平仓
        self.ex.orders_now = []
        row = dict(self.db.position_by_id(pid))
        return pid, row, float(row["hl_sz"])

    def _repair(self, row, sz, close_res):
        self.ex.exit = close_res
        snap = _snap(positions=[_pos("BTC", size=sz, entry=83000.0)])
        self.executor()._repair_missing_stop(row, snap, kill_before=False)

    def test_partial_close_keeps_row_open_with_remainder(self):
        pid, row, sz = self._prep()
        half = sz / 2
        self._repair(row, sz, _partial(half, 84500.0))
        row = self.db.position_by_id(pid)
        self.assertEqual(row["status"], "open",
                         "兜底平仓只成交一半时**不能**把整笔关账（否则剩余仓变孤儿仓）")
        self.assertAlmostEqual(float(row["hl_sz"]), sz - half, places=10)
        self.assertTrue(any("部分成交" in e["message"] for e in self.events(EV_ERROR)))
        self.assertTrue(self.msgs("仅平掉一部分"), "告警必须说清还有剩余量")

    def test_full_close_still_closes(self):
        pid, row, sz = self._prep()
        self._repair(row, sz, _partial(sz, 84500.0))
        self.assertEqual(self.db.position_by_id(pid)["status"], "closed")


class TestR2ExitFailureRestoresProtection(Base):
    """离场失败但止损已撤：必须立刻挂回止损（或把仓位平掉）。"""

    def _exit(self, pid):
        return self.executor().on_exit(pid, "ma20", 84400.0)

    def test_rehang_stop_when_exit_order_fails(self):
        pid, bar_ms = self.open_position()
        self.db.hl_update_order("BTC_USDT", bar_ms, PURPOSE_STOP, oid=UUID_STOP,
                                status="resting")
        self.ex.cancel = lambda coin, oid: {"ok": True, "status": STATUS_RESTING, "oid": oid,
                                           "error": None, "avg_px": None,
                                           "filled_sz": None, "raw": None}
        self.ex.exit = _fail("transport timeout")
        self.ex.stop = {"ok": True, "status": STATUS_RESTING, "oid": UUID_FIX_STOP,
                        "avg_px": None, "filled_sz": None, "error": None, "raw": {}}
        self.info._snap = _snap(positions=[_pos("BTC")])
        self.notifier.messages.clear()
        self.assertTrue(self._exit(pid))
        row = self.db.position_by_id(pid)
        self.assertEqual(row["status"], "open", "平仓失败时仓位仍在，本地不能关账")
        trig = [c for c in self.ex.calls if c[0] == "order" and c[6] is not None]
        self.assertTrue(trig, "平仓失败后必须立刻重新挂出触发止损单")
        reh = [r for r in self.db.hl_recent_orders(100) if r["purpose"] == PURPOSE_EXIT_REHANG]
        self.assertEqual(len(reh), 1, "应急挂回的止损要落 exit:rehang 行")
        self.assertEqual(str(reh[0]["oid"]), UUID_FIX_STOP)
        self.assertTrue(self.msgs("已挂回"), "告警要说明止损已挂回，而不是只说失败")

    def test_flatten_when_rehang_also_fails_partial(self):
        pid, bar_ms = self.open_position()
        self.db.hl_update_order("BTC_USDT", bar_ms, PURPOSE_STOP, oid=UUID_STOP,
                                status="resting")
        sz = float(self.db.position_by_id(pid)["hl_sz"])
        seq = [_fail("timeout"), _partial(sz / 2, 84400.0)]

        def _exit_res(coin, is_buy, q, px):
            return seq.pop(0) if seq else _fail("多余调用")
        self.ex.exit = _exit_res
        self.ex.stop = _fail("stop rejected")
        self.info._snap = _snap(positions=[_pos("BTC")])
        self.notifier.messages.clear()
        self.assertTrue(self._exit(pid))
        row = self.db.position_by_id(pid)
        self.assertEqual(row["status"], "open",
                         "兜底市价单只成交一半 → 剩余量必须留在账上，不能整笔关账")
        self.assertAlmostEqual(float(row["hl_sz"]), sz / 2, places=10)

    def test_flatten_full_closes(self):
        pid, bar_ms = self.open_position()
        self.db.hl_update_order("BTC_USDT", bar_ms, PURPOSE_STOP, oid=UUID_STOP,
                                status="resting")
        sz = float(self.db.position_by_id(pid)["hl_sz"])
        seq = [_fail("timeout"), _partial(sz, 84400.0)]

        def _exit_res(coin, is_buy, q, px):
            return seq.pop(0) if seq else _fail("多余调用")
        self.ex.exit = _exit_res
        self.ex.stop = _fail("stop rejected")
        self.info._snap = _snap(positions=[_pos("BTC")])
        self.ex.orders_now = []
        self.notifier.messages.clear()
        self.assertTrue(self._exit(pid))
        self.assertEqual(self.db.position_by_id(pid)["status"], "closed")
        self.assertTrue(self.msgs("已改为市价平仓"))


if __name__ == "__main__":
    unittest.main()
