"""第六轮审核复现的回归测试（2026-11-13）：面板未收盘会话泄漏 + 补账忽略部分平仓。

钉住两件事（都在 `app/hl/mr.py`，全离线：假 info + 临时 SQLite）：

A) `_fill_signal_status` 只认**已收盘完成**的会话（`mr_signal.completed_upto`）：
   盘中 15:30 ET，当天会话的末根 bar 是 15:00 → 尚未走完，不能拿来算 RSI2 /
   离场线 / last_session_date；一个已完成会话都没有时整块 `signal` 不生成。

B) `_adopt_exit`（有离场意图、交易所已空仓、台账仍 OPEN 的补账）必须与
   `_maybe_exit` 的正常决算同口径：把 `raw.partial_exits` 的已平片段盈亏加回去，
   剩余数量按 mark 结算，写回 raw 时**合并**（保留 partial_exits）。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from app.db import DB
from app.hl.mr import HlMrConfig, HlMrLeg, _partial_exits
from app.mr_signal import SessionBar

ET = ZoneInfo("America/New_York")
ADDR = "0x" + "22" * 20


def _ms(iso_local: str) -> int:
    """ET 本地时刻字符串 → 毫秒（测试里全部按 ET 写，避免手算 UTC 偏移）。"""
    return int(datetime.fromisoformat(iso_local).replace(tzinfo=ET).timestamp() * 1000)


def _session(date: str, close: float, *, last_bar_et: str = "15:00",
             bars: int = 6) -> SessionBar:
    last_bar_ms = _ms(f"{date}T{last_bar_et}:00")
    return SessionBar(date=date, open=close, high=close, low=close, close=close,
                      bars=bars, last_bar_ms=last_bar_ms, last_bar_et=last_bar_et)


class _Info:
    """只回 all_mids 的最小 info（_adopt_exit 只用 _mark）。"""

    def __init__(self, mid: float = 90.0):
        self.mid = mid

    def all_mids(self, dex: str = "") -> dict:
        return {"xyz:XYZ100": self.mid} if dex == "xyz" else {"XYZ100": self.mid}


def _leg(db, *, mid: float = 90.0, fee: float = 0.0, now_ms: int = 0) -> HlMrLeg:
    return HlMrLeg(db=db, info=_Info(mid), exchange=None, address_fn=lambda: ADDR,
                   config=HlMrConfig(taker_fee_rate=fee),
                   clock=lambda: now_ms / 1000.0)


# ---------------------------------------------------------------- A) 面板会话


class TestPanelOnlyUsesCompletedSessions(unittest.TestCase):
    """15:30 ET 的当天会话只有 5 根、末根 15:00 —— 它还没走完，不能进面板信号。"""

    #: 已完成会话（最后一个是上一交易日 11-09 周一）。
    DONE = [("2026-11-02", 95.0), ("2026-11-03", 94.0), ("2026-11-04", 93.0),
            ("2026-11-05", 92.0), ("2026-11-06", 91.0), ("2026-11-09", 88.0)]

    def _sessions(self, today_close: float = 70.0):
        rows = [_session(d, c) for d, c in self.DONE]
        # 2026-11-10（周二）盘中 15:30 ET：末根 bar 15:00，15:00+1h = 16:00 > 15:30
        rows.append(_session("2026-11-10", today_close, last_bar_et="15:00", bars=5))
        return rows

    def _leg(self):
        return HlMrLeg(db=None, info=None, exchange=None, address_fn=lambda: "0x0")

    def test_incomplete_today_is_ignored(self):
        leg = self._leg()
        sessions = self._sessions(today_close=70.0)     # 暴跌的「假收盘」必须被忽略
        st = {}
        now = _ms("2026-11-10T15:30:00")
        leg._fill_signal_status(st, sessions, [{"c": "70.0"}], now)
        sig = st["signal"]
        # 用上一已完成会话（周一 11-09），不是当天
        self.assertEqual(sig["last_session_date"], "2026-11-09")
        # 离场线 = 已完成会话最近 4 个收盘均值（93/92/91/88），不含 70
        self.assertAlmostEqual(sig["exit_line"], (93.0 + 92.0 + 91.0 + 88.0) / 4, places=6)
        # sessions_usable 仍是「可用会话总数」，口径不变
        self.assertEqual(sig["sessions_usable"], len(sessions))
        # 未收盘的 70 不参与 RSI2：与只用已完成会话算出来的值一致
        from app import mr_signal
        expect = mr_signal.session_signals(sessions[:6])[-1]
        self.assertAlmostEqual(sig["rsi2"], expect["rsi2"], places=9)
        self.assertEqual(sig["entry_flag"], bool(expect["entry"]))
        self.assertEqual(sig["exit_flag"], bool(expect["exit_above"]))
        # 11-09 收盘 RSI2=0 → 入场信号，窗口是 11-10 09:30–12:30；现在 15:30 已过
        self.assertTrue(sig["entry_flag"])
        self.assertEqual(sig["window"]["date"], "2026-11-10")
        self.assertEqual(sig["phase"], "expired")

    def test_no_completed_session_means_no_signal(self):
        leg = self._leg()
        # 只有当天这一根还没走完的会话 → k = 0 → 整块 signal 不生成
        st = {}
        now = _ms("2026-11-10T15:30:00")
        leg._fill_signal_status(st, self._sessions()[-1:], [{"c": "70.0"}], now)
        self.assertNotIn("signal", st)

    def test_all_completed_after_close(self):
        """16:30 ET：当天末根 15:00 的 bar 已走完（+1h=16:00 <= now）→ 用它。"""
        leg = self._leg()
        sessions = self._sessions(today_close=70.0)
        st = {}
        leg._fill_signal_status(st, sessions, [{"c": "70.0"}],
                               _ms("2026-11-10T16:30:00"))
        self.assertEqual(st["signal"]["last_session_date"], "2026-11-10")


# ------------------------------------------------------- B) 补账含部分平仓


class TestAdoptExitHonoursPartialExits(unittest.TestCase):
    """10 @ 100 入场，先平 4 @ 110，剩 6 按 mark=90 补账，费率 0 → 净 −20。"""

    def _setup(self, tmp: str, *, qty: float = 10.0, partial_qty: float = 4.0):
        db = DB(f"{tmp}/mr_adopt.db")
        now = _ms("2026-11-10T15:30:00")
        trade_id = db.mr_record_entry(
            signal_date="2026-11-06", entry_session="2026-11-09",
            qty=qty, qty_int=int(round(qty * 10 ** 4)), entry_price=100.0,
            entry_order_id="", entry_ts=now / 1000.0 - 3600.0,
            notional=qty * 100.0, margin=qty * 100.0, equity_at_entry=500.0,
            raw={"ref_price": 100.0, "fill_source": "xyz_dex_position"})
        db.mr_trade_append_partial(trade_id, qty=partial_qty, price=110.0,
                                   ts=now / 1000.0 - 600.0)
        db.hl_log_event("mr_exit_intent", "MR 离场下单意图",
                        data={"signal_date": "2026-11-06",
                              "exit_signal_date": "2026-11-10", "sz": qty,
                              "ref_price": 90.0}, ts=now / 1000.0)
        return db, trade_id, now

    def _closed(self, db, trade_id: int):
        return db.conn.execute("SELECT * FROM mr_trades WHERE id=?",
                               (trade_id,)).fetchone()

    def test_net_pnl_includes_partial_and_keeps_raw(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, trade_id, now = self._setup(tmp)
            leg = _leg(db, mid=90.0, fee=0.0, now_ms=now)
            row = db.mr_open_trade()
            self.assertIsNotNone(row)
            self.assertTrue(leg._adopt_exit(row, now, {}))
            closed = self._closed(db, trade_id)
            self.assertEqual(closed["state"], "CLOSED")
            # (110-100)*4 + (90-100)*6 = 40 - 60 = -20
            self.assertAlmostEqual(closed["net_pnl"], -20.0, places=6)
            self.assertAlmostEqual(closed["exit_price"], 90.0, places=6)
            raw = json.loads(closed["raw"])
            self.assertTrue(raw.get("adopted"))
            self.assertEqual(raw.get("price_source"), "all_mids")
            self.assertAlmostEqual(raw.get("partial_pnl"), 40.0, places=6)
            self.assertAlmostEqual(raw.get("remaining_qty"), 6.0, places=6)
            parts = _partial_exits(closed)
            self.assertGreaterEqual(len(parts), 1)
            self.assertAlmostEqual(parts[0]["qty"], 4.0, places=9)
            self.assertAlmostEqual(parts[0]["price"], 110.0, places=9)
            # 原 raw 的业务字段也还在（合并写回，不是覆盖）
            self.assertEqual(raw.get("fill_source"), "xyz_dex_position")

    def test_over_closed_partials_are_reconciled(self):
        """片段总量 > 台账 qty：剩余按 0 计，并在 raw 里留 reconciliation。"""
        with tempfile.TemporaryDirectory() as tmp:
            db, trade_id, now = self._setup(tmp, qty=3.0, partial_qty=4.0)
            leg = _leg(db, mid=90.0, fee=0.0, now_ms=now)
            self.assertTrue(leg._adopt_exit(db.mr_open_trade(), now, {}))
            closed = self._closed(db, trade_id)
            raw = json.loads(closed["raw"])
            self.assertEqual(raw["remaining_qty"], 0.0)
            self.assertIn("reconciliation", raw)
            self.assertAlmostEqual(raw["reconciliation"]["partial_qty"], 4.0, places=9)
            self.assertAlmostEqual(closed["net_pnl"], 40.0, places=6)   # 只剩片段盈亏

    def test_fee_covers_entry_and_remaining_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, trade_id, now = self._setup(tmp)
            leg = _leg(db, mid=90.0, fee=0.001, now_ms=now)
            self.assertTrue(leg._adopt_exit(db.mr_open_trade(), now, {}))
            closed = self._closed(db, trade_id)
            # fee = 0.001 * (notional 1000 + 6*90 + 4*110) = 0.001*1980 = 1.98
            self.assertAlmostEqual(closed["fee_quote"], 1.98, places=6)
            self.assertAlmostEqual(closed["net_pnl"], -21.98, places=6)


if __name__ == "__main__":
    unittest.main()
