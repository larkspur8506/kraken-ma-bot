"""Kraken 离场撤单竞态回归（审计四轮 P1-B，2026-11-09）。

事故口径：`app/kr/exchange.py:cancel()` 把 Kraken `cancelStatus.status=="filled"`
正确返回成 `ok=True, status=STATUS_FILLED`（含义是「这张单已经**成交**，撤不到」）。
但 `on_exit` 的撤止损只检查 `ok`，于是：

  1. DB 把已成交的止损行写成 `cancelled`（与事实相反，破坏归因/关账）；
  2. 继续发 reduce-only 平仓单 —— 账户可能已经空仓，被拒后还喊「止损单已撤！
     请立即人工平仓」，可能诱导用户反手开新仓；
  3. 平仓 IOC 未成交时还会在零仓上重挂 `exit:rehang` 止损 → 孤儿单咬下一笔新仓。

本文件的验收点（每一条都是「不成立就失败」）：
  A 任何 purpose（stop / stop:fix / exit:rehang）撤单返回 filled →
    **绝不再发平仓单、绝不再发新止损单**；
  B 对应 DB 行落到 `filled`（不是 `cancelled`），后续轮次也不得被降级；
  C 能拿到真实平仓成交（与对账同一套 `_fill_close_proof` 口径）→ 按该价关账；
    证据不足 → 保持 open（交下一轮对账/宽限），**绝不**用 ref_price 猜价关账；
  D cancelled / notFound（适配层都映射为 ok+resting）仍走正常平仓；
    撤单失败仍保持原状态、不发平仓单；
  E 正常平仓的参数/口径不变（sz、reduce-only、方向、限价仍取 slippage_limit）。

只依赖相对路径（CI 容器无 /opt/data）；不 import app.main（模块级建库）。
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import DB
from app.hl import MODE_LIVE, PURPOSE_ENTRY, PURPOSE_EXIT, PURPOSE_EXIT_REHANG, PURPOSE_STOP
from app.hl.executor import Executor, ExecParams
from app.hl.exchange import STATUS_FILLED, STATUS_RESTING

BAR = 1791912000000
QTY = 0.005
ENTRY = 83000.0
STOP = 80600.0
REF_PRICE = 84400.0                # 信号参考价：绝不是成交价，任何关账用它都是猜
FILL_PX = 80650.0                  # 止损真实成交价
STOP_OID = "3f6a8d0e-1c2b-4f5a-9d7e-0a1b2c3d4e5f"      # Kraken 单号是 UUID 字符串
REHANG_OID = "7b1e2f3a-4c5d-6e7f-8a9b-0c1d2e3f4a5b"


def _snap(equity=1000.0, positions=(), orders=(), trusted=True):
    return {"ts": time.time(), "address": "0xabc", "equity": equity, "equity_total": equity,
            "withdrawable": equity, "spot_ok": True, "margin_used": 10.0, "notional": 0.0,
            "positions": list(positions), "open_orders": list(orders),
            "orders_trusted": trusted}


def _pos(coin="BTC", size=QTY, entry=ENTRY, side=1, upnl=0.0):
    return {"coin": coin, "side": side, "size": size, "entry_px": entry,
            "mark_px": entry + 50, "position_value": size * entry, "liq_px": 0.0,
            "leverage": 5, "isolated": True, "margin_used": size * entry / 5,
            "unrealized_pnl": upnl, "roe": 0.0}


def _closing_fills(sz=QTY, px=FILL_PX, oid=STOP_OID, age_s=30, coin="BTC"):
    """一笔由止损单打出的平仓成交（Kraken 适配层字段名与 _sync_fills 一致）。"""
    return [{"coin": coin, "oid": oid, "dir": "Close Long", "sz": sz, "px": px,
             "time": int((time.time() - age_s) * 1000), "tid": "T-1",
             "closedPnl": -11.75, "fee": 0.19, "feeToken": "USDC", "hash": "0xaaa",
             "reduce_only": 1}]


class FakeInfo:
    def __init__(self, snap=None, fills=None):
        self._snap = snap if snap is not None else _snap()
        self._fills = list(fills or [])
        self.user_fills_calls = 0
        self.snap_calls = 0

    def snapshot(self, address=None):
        self.snap_calls += 1
        return dict(self._snap)

    def specs(self, coins=None):
        return {c: {"sz_decimals": 5, "max_leverage": 40} for c in (coins or ["BTC"])}

    def user_fills(self, address=None):
        self.user_fills_calls += 1
        return list(self._fills)

    def user_fills_by_time(self, address=None, start_ms=0):
        return [f for f in self._fills if int(f.get("time") or 0) >= int(start_ms)]

    def all_mids(self):
        return {"BTC": ENTRY}

    def mid(self, coin):
        return ENTRY


class FakeExchange:
    def __init__(self, cancel_result=None, exit_result=None, stop_result=None,
                 orders_now=None, orders_trusted=True):
        self.calls = []
        self.cancel_result = cancel_result or {"ok": True, "status": STATUS_RESTING}
        self.exit_result = exit_result
        self.stop_result = stop_result
        # 交易所实时挂单表快照（`_open_orders_checked`）
        self.orders_now = list(orders_now or [])
        self.orders_trusted = orders_trusted

    def _open_orders_checked(self):
        return [dict(o) for o in self.orders_now], self.orders_trusted

    def mid(self, coin):
        return ENTRY

    def slippage_limit(self, coin, is_buy, mid=None):
        px = mid if mid else self.mid(coin)
        return px * (1 + 0.003) if is_buy else px * (1 - 0.003)

    def place_order(self, coin, is_buy, sz, limit_px, reduce_only=False, tif="Ioc",
                    trigger_px=None, cloid=None):
        self.calls.append({"kind": "order", "coin": coin, "is_buy": is_buy, "sz": sz,
                           "px": limit_px, "reduce_only": reduce_only,
                           "trigger_px": trigger_px, "cloid": str(cloid)})
        if trigger_px is not None:
            res = self.stop_result
        elif reduce_only:
            res = self.exit_result
        else:
            res = None
        if res is None:
            return {"ok": True, "status": STATUS_FILLED, "oid": "oid-auto", "avg_px": limit_px,
                    "filled_sz": sz, "error": None, "raw": {}}
        if callable(res):
            return res(coin, is_buy, sz, limit_px)
        return res

    def cancel(self, coin, oid):
        self.calls.append({"kind": "cancel", "coin": coin, "oid": oid})
        res = self.cancel_result
        # 允许按调用顺序给结果（第一张 filled、后续 cancelled）：列表弹出 / 可调用。
        if isinstance(res, list):
            res = res.pop(0) if res else {"ok": True, "status": STATUS_RESTING}
        elif callable(res):
            res = res(coin, oid)
        return dict(res, oid=oid)

    # ---- 断言辅助
    def orders(self):
        return [c for c in self.calls if c["kind"] == "order"]

    def closes(self):
        """reduce-only 且非触发的单 = 平仓单。"""
        return [c for c in self.orders() if c["reduce_only"] and c["trigger_px"] is None]

    def triggers(self):
        """带 trigger_px 的单 = 止损/触发单（含重挂）。"""
        return [c for c in self.orders() if c["trigger_px"] is not None]

    def cancels(self):
        return [c for c in self.calls if c["kind"] == "cancel"]


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
        self.info = FakeInfo(snap=_snap(positions=[_pos()]))
        self.ex = FakeExchange()
        self.p = ExecParams(mode=MODE_LIVE, account_address="0xabc", private_key="0xkey",
                            equity=1000.0, equity_min=20.0, risk_pct=0.02, leverage=5.0,
                            code_rev="audit4-p1b", max_signal_age_min=0)

    def tearDown(self):
        self.tmp.cleanup()

    def executor(self):
        return Executor(self.db, self.p, self.info, self.ex, notifier=self.notifier)

    def open_pos(self, side=1):
        pid = self.db.create_position(symbol="BTC_USDT", signal_bar_ms=BAR, side=side,
                                      entry_price=ENTRY, stop_price=STOP,
                                      stop_dist=abs(ENTRY - STOP) / ENTRY, contracts=1,
                                      coin_qty=QTY, notional=ENTRY * QTY, risk_u=20.0,
                                      equity_at_entry=1000.0)
        self.db.set_pos_hl(pid, hl_status="open", hl_entry_oid="entry-1",
                           hl_stop_oid=STOP_OID, hl_sz=QTY)
        self.db.confirm_position(pid, ENTRY, QTY, STOP, True)
        return pid

    def stop_row(self, symbol="BTC_USDT", bar_ms=BAR, purpose=PURPOSE_STOP):
        return self.db.hl_order(symbol, bar_ms, purpose)


class TestStopFilledOnCancel(Base):
    """A/B/C：stop 撤单返回 filled。"""

    def setUp(self):
        super().setUp()
        self.pid = self.open_pos()
        self.db.hl_record_intent("BTC_USDT", "BTC", BAR, PURPOSE_STOP, 1, QTY, px=STOP,
                                 reduce_only=True, status="resting", oid=STOP_OID)
        # 现实顺序：止损成交 → 交易所已无仓 → 执行器撤单拿到 cancelStatus=filled
        self.info._snap = _snap(positions=[])
        self.ex.cancel_result = {"ok": True, "status": STATUS_FILLED, "raw": {"cancelStatus": {}}}

    def test_no_close_no_rehang_and_row_is_filled(self):
        self.info._fills = _closing_fills()
        ex = self.executor()
        self.assertTrue(ex.on_exit(self.pid, "ma20", REF_PRICE))

        self.assertEqual(self.ex.closes(), [], "止损已成交：不得再发平仓单")
        self.assertEqual(self.ex.triggers(), [], "止损已成交：不得再重挂止损单")
        self.assertEqual(self.stop_row()["status"], "filled",
                         "成交过的止损行必须落 filled，不能写 cancelled")

    def test_closed_at_real_fill_px_not_ref_price(self):
        self.info._fills = _closing_fills()
        ex = self.executor()
        ex.on_exit(self.pid, "ma20", REF_PRICE)

        row = self.db.position_by_id(self.pid)
        self.assertEqual(row["status"], "closed", "有真实平仓成交时应关账")
        self.assertAlmostEqual(float(row["exit_price"]), FILL_PX, places=6,
                               msg="必须用成交价关账，不能用信号参考价")
        self.assertNotAlmostEqual(float(row["exit_price"]), REF_PRICE, places=6)
        self.assertEqual(row["exit_reason"], "stop")

    def test_no_fill_evidence_keeps_open_without_guessing(self):
        self.info._fills = []            # 成交回报还没到
        ex = self.executor()
        self.assertTrue(ex.on_exit(self.pid, "ma20", REF_PRICE))

        row = self.db.position_by_id(self.pid)
        self.assertEqual(row["status"], "open", "证据不足不得关账")
        self.assertIsNone(row["exit_price"], "证据不足不得写任何『猜出来的』平仓价")
        self.assertEqual(row["hl_status"], "closing",
                         "交易所已无仓、只差成交核对 → 置 closing 交宽限/对账口径")
        self.assertEqual(self.ex.closes(), [], "证据不足也不得继续发平仓单")
        self.assertEqual(self.ex.triggers(), [])
        self.assertEqual(self.stop_row()["status"], "filled")

    def test_alert_does_not_ask_to_manually_close(self):
        self.info._fills = _closing_fills()
        ex = self.executor()
        ex.on_exit(self.pid, "ma20", REF_PRICE)
        text = "\n".join(self.notifier.messages)
        self.assertEqual(self.stop_row()["status"], "filled")
        self.assertNotIn("请立即人工平仓", text,
                         "止损已成交，再喊人工平仓可能诱导反手开新仓")
        self.assertIn("BTC", text, "仍要有可读的离场提醒")


class TestStopFixFilledOnCancel(Base):
    """A/B：对账补挂的 stop:fix 报 filled，同样不许平仓/重挂。"""

    def setUp(self):
        super().setUp()
        self.pid = self.open_pos()
        self.db.hl_record_intent("BTC_USDT", "BTC", BAR, PURPOSE_STOP + ":fix", 1, QTY,
                                 px=STOP, reduce_only=True, status="resting", oid=STOP_OID)
        self.info._snap = _snap(positions=[])
        self.ex.cancel_result = {"ok": True, "status": STATUS_FILLED, "raw": {}}

    def test_fix_filled_blocks_close(self):
        self.info._fills = _closing_fills()
        ex = self.executor()
        self.assertTrue(ex.on_exit(self.pid, "ma20", REF_PRICE))

        self.assertEqual(self.ex.closes(), [])
        self.assertEqual(self.ex.triggers(), [])
        self.assertEqual(self.stop_row(purpose=PURPOSE_STOP + ":fix")["status"], "filled")


class TestExitRehangFilledOnCancel(Base):
    """A/B：上一轮重挂的 exit:rehang 报 filled，同样不许平仓/重挂。"""

    def setUp(self):
        super().setUp()
        self.pid = self.open_pos()
        # exit:rehang 记在**离场 bar**（当前收盘 bar），这里用 BAR 模拟同一 key
        self.db.hl_record_intent("BTC_USDT", "BTC", BAR, PURPOSE_EXIT_REHANG, 1, QTY,
                                 px=STOP, reduce_only=True, status="resting", oid=REHANG_OID)
        self.info._snap = _snap(positions=[])
        self.ex.cancel_result = {"ok": True, "status": STATUS_FILLED, "raw": {}}

    def test_rehang_filled_blocks_close(self):
        self.info._fills = _closing_fills(oid=REHANG_OID)
        ex = self.executor()
        self.assertTrue(ex.on_exit(self.pid, "ma20", REF_PRICE))

        self.assertEqual(self.ex.closes(), [])
        self.assertEqual(self.ex.triggers(), [])
        row = self.db.hl_order("BTC_USDT", BAR, PURPOSE_EXIT_REHANG)
        self.assertEqual(row["status"], "filled")
        self.assertEqual(row["oid"], REHANG_OID, "oid 必须原样保存（Kraken UUID 不许 int()）")


class TestNonFilledCancelStillCloses(Base):
    """D/E：cancelled / notFound 仍走正常平仓，参数口径不变。"""

    def setUp(self):
        super().setUp()
        self.pid = self.open_pos()
        self.db.hl_record_intent("BTC_USDT", "BTC", BAR, PURPOSE_STOP, 1, QTY, px=STOP,
                                 reduce_only=True, status="resting", oid=STOP_OID)
        self.info._snap = _snap(positions=[_pos()])

    def test_cancelled_maps_to_ok_resting_and_closes(self):
        # app/kr/exchange.py 把 cancelled 与 notFound 都映射成 ok+STATUS_RESTING
        self.ex.cancel_result = {"ok": True, "status": STATUS_RESTING,
                                 "raw": {"cancelStatus": {"status": "cancelled"}}}
        ex = self.executor()
        self.assertTrue(ex.on_exit(self.pid, "ma20", REF_PRICE))

        self.assertEqual(self.stop_row()["status"], "cancelled")
        closes = self.ex.closes()
        self.assertEqual(len(closes), 1, "撤单成功才允许平仓")
        self.assertAlmostEqual(closes[0]["sz"], QTY, places=6)
        self.assertTrue(closes[0]["reduce_only"])
        self.assertFalse(closes[0]["is_buy"], "平多应卖出")
        # 限价必须按平仓方向算滑点：卖 = mid×(1−s)
        self.assertAlmostEqual(closes[0]["px"], ENTRY * (1 - 0.003), places=6)
        self.assertEqual(self.db.position_by_id(self.pid)["status"], "closed")

    def test_not_found_also_closes(self):
        self.ex.cancel_result = {"ok": True, "status": STATUS_RESTING,
                                 "raw": {"cancelStatus": {"status": "notFound"}}}
        ex = self.executor()
        ex.on_exit(self.pid, "ma20", REF_PRICE)
        self.assertEqual(self.stop_row()["status"], "cancelled")
        self.assertEqual(len(self.ex.closes()), 1)

    def test_cancel_failure_keeps_row_and_close_still_attempted(self):
        """撤单失败：行保持 resting（本地仍认为单可能在场上）+ 记错误 + 大声告警。
        平仓继续（既有口径：撤不掉止损也得让人能离场），这里只钉住「行不许被写成
        cancelled」—— 那才是本轮要修的谎报。"""
        self.ex.cancel_result = {"ok": False, "status": "error",
                                 "error": "撤单状态不可识别（status=缺失）"}
        ex = self.executor()
        self.assertTrue(ex.on_exit(self.pid, "ma20", REF_PRICE))

        row = self.stop_row()
        self.assertEqual(row["status"], "resting",
                         "撤单失败时行必须保持原状态（本地仍认为它可能在场上）")
        self.assertTrue(row["error"])
        self.assertEqual(len(self.ex.closes()), 1, "撤单失败不改变既有平仓口径")
        self.assertEqual(self.ex.triggers(), [])
        self.assertIn("撤止损单失败", "\n".join(self.notifier.messages))


class TestFilledButPositionStillThere(Base):
    """A（保守支）：止损只成交了一部分 / 交易所仍有仓 → 更不许替它平仓或重挂。"""

    def setUp(self):
        super().setUp()
        self.pid = self.open_pos()
        self.db.hl_record_intent("BTC_USDT", "BTC", BAR, PURPOSE_STOP, 1, QTY, px=STOP,
                                 reduce_only=True, status="resting", oid=STOP_OID)
        self.info._snap = _snap(positions=[_pos(size=QTY / 2)])
        self.ex.cancel_result = {"ok": True, "status": STATUS_FILLED, "raw": {}}

    def test_position_still_open_blocks_close_and_rehang(self):
        self.info._fills = _closing_fills(sz=QTY / 2)
        ex = self.executor()
        self.assertTrue(ex.on_exit(self.pid, "ma20", REF_PRICE))

        self.assertEqual(self.ex.closes(), [], "交易所仍有仓：不得替它发平仓单")
        self.assertEqual(self.ex.triggers(), [], "不得重挂新止损（等人工核对）")
        self.assertEqual(self.stop_row()["status"], "filled")
        self.assertEqual(self.db.position_by_id(self.pid)["status"], "open")
        self.assertIn("仍需人工", "\n".join(self.notifier.messages))


class TestFilledButSnapshotUnreadable(Base):
    """C（保守支）：撤单 filled 但快照读不到 → 不关账、不发单，交下一轮。"""

    def setUp(self):
        super().setUp()
        self.pid = self.open_pos()
        self.db.hl_record_intent("BTC_USDT", "BTC", BAR, PURPOSE_STOP, 1, QTY, px=STOP,
                                 reduce_only=True, status="resting", oid=STOP_OID)
        self.ex.cancel_result = {"ok": True, "status": STATUS_FILLED, "raw": {}}

        from app.hl.info import HlInfoError

        def _boom(address=None):
            raise HlInfoError("snapshot down")
        self.info.snapshot = _boom

    def test_snapshot_down_keeps_open_without_orders(self):
        ex = self.executor()
        self.assertTrue(ex.on_exit(self.pid, "ma20", REF_PRICE))

        self.assertEqual(self.ex.orders(), [], "快照不可读时不得发任何单（含平仓/重挂）")
        self.assertEqual(self.stop_row()["status"], "filled")
        row = self.db.position_by_id(self.pid)
        self.assertEqual(row["status"], "open")
        self.assertIsNone(row["exit_price"])


class TestNoEvidenceThenStopEventDoesNotGuess(Base):
    """C（口径闭环）：证据不足置 closing 后，后续 stop 事件也不许按 ref_price 关账。"""

    def test_stop_event_after_closing_does_not_close_at_ref_price(self):
        pid = self.open_pos()
        self.db.hl_record_intent("BTC_USDT", "BTC", BAR, PURPOSE_STOP, 1, QTY, px=STOP,
                                 reduce_only=True, status="resting", oid=STOP_OID)
        self.info._snap = _snap(positions=[])
        self.info._fills = []
        self.ex.cancel_result = {"ok": True, "status": STATUS_FILLED, "raw": {}}
        ex = self.executor()
        ex.on_exit(pid, "ma20", REF_PRICE)
        self.assertEqual(self.db.position_by_id(pid)["hl_status"], "closing")

        # 4h 收盘止损事件进来：交易所无仓、仍拿不到成交价 → 只报「核对中」，不关账
        ex2 = self.executor()
        self.assertTrue(ex2.on_exit(pid, "stop", REF_PRICE))
        row = self.db.position_by_id(pid)
        self.assertEqual(row["status"], "open", "无成交证据时不得关账")
        self.assertIsNone(row["exit_price"], "绝不能用 ref_price 猜一个关账价")
        self.assertEqual(self.ex.closes(), [])


class TestFilledRowNotDowngraded(Base):
    """B（持续性）：已经记成 filled 的行，后续离场再撤到 notFound 也不许降级。

    造景取「止损只成交一部分」这条真实可达路径：第一轮撤单拿到 filled、交易所仍有仓
    → 行 filled、仓保持 open；第二轮仓已被平掉，再撤同一张单只能拿到 notFound ——
    此时若照旧写 cancelled，成交证据就没了。
    """

    def test_no_downgrade_on_later_round(self):
        pid = self.open_pos()
        self.db.hl_record_intent("BTC_USDT", "BTC", BAR, PURPOSE_STOP, 1, QTY, px=STOP,
                                 reduce_only=True, status="resting", oid=STOP_OID)
        # 第一轮：撤单 filled，交易所仍有半仓（部分成交）→ 仓保持 open，止损行 filled
        self.info._snap = _snap(positions=[_pos(size=QTY / 2)])
        self.info._fills = _closing_fills(sz=QTY / 2)
        self.ex.cancel_result = {"ok": True, "status": STATUS_FILLED, "raw": {}}
        ex = self.executor()
        ex.on_exit(pid, "ma20", REF_PRICE)
        self.assertEqual(self.stop_row()["status"], "filled")
        self.assertEqual(self.ex.closes(), [])

        # 第二轮：交易所已查无此单（notFound），本地行仍是 filled
        self.info._snap = _snap(positions=[])
        self.ex.cancel_result = {"ok": True, "status": STATUS_RESTING,
                                 "raw": {"cancelStatus": {"status": "notFound"}}}
        ex2 = self.executor()
        ex2.on_exit(pid, "ma20", REF_PRICE)
        self.assertEqual(self.stop_row()["status"], "filled",
                         "已成交的止损行不得被后续撤单结果改写成 cancelled")


class TestFilledRowLaterCancelNotFound(Base):
    """A/B（持续性 + 零新单）：本地止损行**已经是 filled**，下一轮离场再去撤同一张单

    只能拿到 notFound / cancelled（Kraken 适配层把它们都映射成 `ok=True, status=resting`）。
    此时必须和「撤单当场返回 filled」走**同一条**收尾：刷新快照 + 取成交证据关账，
    并且**绝不再发任何平仓单 / 重挂止损单**。

    真实到达路径就是部分成交：第一轮撤单 filled、交易所仍有剩余仓（行 filled、仓 open）；
    剩余仓被平掉后的下一轮，同一张单只剩 notFound。此时若把结果当 cancelled 交回调用方，
    调用方会对着零仓继续平仓、并可能重挂孤儿止损 —— 与「撤单返回 filled」完全同一种事故。
    """

    def setUp(self):
        super().setUp()
        self.pid = self.open_pos()
        self.db.hl_record_intent("BTC_USDT", "BTC", BAR, PURPOSE_STOP, 1, QTY, px=STOP,
                                 reduce_only=True, status="resting", oid=STOP_OID)
        # 第一轮已确认这张止损成交过：行 filled，仓因部分成交仍是 open
        self.db.hl_update_order("BTC_USDT", BAR, PURPOSE_STOP, status="filled")
        self.assertEqual(self.stop_row()["status"], "filled")
        # 下一轮：剩余仓已被平掉（交易所空仓），真实平仓成交已回流
        self.info._snap = _snap(positions=[])
        self.info._fills = _closing_fills()

    def _exit_with_cancel_status(self, raw_status):
        self.ex.cancel_result = {"ok": True, "status": STATUS_RESTING,
                                 "raw": {"cancelStatus": {"status": raw_status}}}
        ex = self.executor()
        self.assertTrue(ex.on_exit(self.pid, "ma20", REF_PRICE))
        # 零新单：撤单拿到 notFound/cancelled 也不能被当成「可以继续平仓」
        self.assertEqual(self.ex.closes(), [],
                         f"已 filled 的行再撤到 {raw_status}：不得继续发平仓单")
        self.assertEqual(self.ex.triggers(), [],
                         f"已 filled 的行再撤到 {raw_status}：不得在零仓上重挂止损")
        self.assertEqual(self.ex.orders(), [], "这条路径一张新单都不许发")
        self.assertEqual(self.stop_row()["status"], "filled",
                         "已成交的止损行不得被后续撤单结果降级成 cancelled")
        self.assertGreaterEqual(self.info.snap_calls, 2,
                                "必须走 filled 收尾：再刷新一次快照后才判断能否关账")

    def test_notfound_after_filled_walks_filled_path(self):
        self._exit_with_cancel_status("notFound")
        # 走 filled 分支的正常收尾：刷新快照 + 成交证据 → 按真实成交价关账
        row = self.db.position_by_id(self.pid)
        self.assertEqual(row["status"], "closed", "证据齐全时应按 filled 路径关账")
        self.assertAlmostEqual(float(row["exit_price"]), FILL_PX, places=6,
                               msg="必须用成交价关账，不能用信号参考价")
        self.assertEqual(row["exit_reason"], "stop")
        self.assertAlmostEqual(float(self.stop_row()["avg_px"]), FILL_PX, places=6,
                               msg="filled 路径要把成交均价回写到止损行")

    def test_cancelled_after_filled_walks_filled_path(self):
        self._exit_with_cancel_status("cancelled")
        row = self.db.position_by_id(self.pid)
        self.assertEqual(row["status"], "closed")
        self.assertAlmostEqual(float(row["exit_price"]), FILL_PX, places=6)
        self.assertEqual(self.stop_row()["status"], "filled")


class TestFilledRowWithFailedCancel(Base):
    """B（持续性 × 撤单失败）：行已 filled，本轮撤单 ok=False（超时/异常/不可识别）。

    撤单拿不到回执，**不能**推翻「这张止损已经成交」这个本地证据 —— 账户那一刻可能
    已经空仓。若按 ok=False 返回 `failed`，调用方会顺着「撤单失败」继续发 reduce-only
    平仓单，并在 IOC 未成时于零仓上重挂 `exit:rehang`（孤儿单咬下一笔新仓），同时
    告警喊人「手工撤单」—— 撤一张已经成交、根本不在场的单。

    验收：DB 仍 filled（不降级）、零新平仓/零重挂、走已成交收尾（刷新快照 + 成交证据
    关账），且告警不得要求人工撤单。
    """

    def setUp(self):
        super().setUp()
        self.pid = self.open_pos()
        self.db.hl_record_intent("BTC_USDT", "BTC", BAR, PURPOSE_STOP, 1, QTY, px=STOP,
                                 reduce_only=True, status="resting", oid=STOP_OID)
        # 上一轮已确认这张止损成交过（filled），仓因部分成交/回流延迟仍是 open
        self.db.hl_update_order("BTC_USDT", BAR, PURPOSE_STOP, status="filled")
        self.assertEqual(self.stop_row()["status"], "filled")
        # 本轮：交易所已无仓，真实平仓成交已回流
        self.info._snap = _snap(positions=[])
        self.info._fills = _closing_fills()

    def _assert_filled_wrapup(self, label: str):
        self.assertEqual(self.ex.orders(), [],
                         f"{label}：已 filled 的行撤单失败也不得发任何新单（平仓/重挂）")
        self.assertEqual(self.ex.closes(), [], f"{label}：不得继续平仓")
        self.assertEqual(self.ex.triggers(), [], f"{label}：不得重挂止损")
        self.assertEqual(self.stop_row()["status"], "filled",
                         f"{label}：撤单失败不得把已成交的行降级")
        text = "\n".join(self.notifier.messages)
        self.assertNotIn("人工撤单", text,
                         f"{label}：单已成交，喊人手工撤单会把人引到不存在的动作上")
        self.assertNotIn("手工撤单", text, label)
        # 走的是「已成交」收尾：刷新快照 + 成交证据 → 按真实成交价关账
        self.assertGreaterEqual(self.info.snap_calls, 2,
                                f"{label}：必须走 filled 收尾（重刷新快照）")
        row = self.db.position_by_id(self.pid)
        self.assertEqual(row["status"], "closed", f"{label}：证据齐全时应按 filled 路径关账")
        self.assertAlmostEqual(float(row["exit_price"]), FILL_PX, places=6,
                               msg=f"{label}：必须用成交价关账，不能用信号参考价")
        self.assertEqual(row["exit_reason"], "stop", label)

    def test_cancel_timeout_after_filled(self):
        self.ex.cancel_result = {"ok": False, "error": "撤单超时（10s 无回执）"}
        ex = self.executor()
        self.assertTrue(ex.on_exit(self.pid, "ma20", REF_PRICE))
        self._assert_filled_wrapup("撤单超时")

    def test_cancel_exception_after_filled(self):
        def _boom(coin, oid):
            raise TimeoutError("撤单请求超时")

        self.ex.cancel = _boom
        ex = self.executor()
        self.assertTrue(ex.on_exit(self.pid, "ma20", REF_PRICE))
        self._assert_filled_wrapup("撤单异常")

    def test_cancel_status_unrecognized_after_filled(self):
        self.ex.cancel_result = {"ok": False, "status": "error",
                                 "error": "撤单状态不可识别（status=缺失）"}
        ex = self.executor()
        self.assertTrue(ex.on_exit(self.pid, "ma20", REF_PRICE))
        self._assert_filled_wrapup("撤单状态不可识别")


class TestNoEvidenceCleansResidualStops(Base):
    """P1-B 残余闭环（1）：已确认交易所无仓、但成交证据不足这一支，也必须先清残留止损。

    `on_exit` 第一步撤到 filled 就 break 出 purpose 循环（stop:fix / exit:rehang 不再撤），
    随后 proof=None 只置 closing 并发「自动重试，无需人工操作」。若此刻交易所还挂着上一轮
    重挂的 reduce-only 触发单，这条告警就是假话：孤儿单会咬下一笔开仓。
    验收：残余单按 oid 被撤掉（DB 行落 cancelled），且仍然零新平仓 / 零重挂。
    """

    def setUp(self):
        super().setUp()
        self.pid = self.open_pos()
        self.db.hl_record_intent("BTC_USDT", "BTC", BAR, PURPOSE_STOP, 1, QTY, px=STOP,
                                 reduce_only=True, status="resting", oid=STOP_OID)
        self.db.hl_record_intent("BTC_USDT", "BTC", BAR, PURPOSE_EXIT_REHANG, 1, QTY,
                                 px=STOP, reduce_only=True, status="resting", oid=REHANG_OID)
        self.info._snap = _snap(positions=[])
        self.info._fills = []            # 成交回报还没回流 → proof=None
        # 第 1 次 cancel（离场撤 stop）= filled；第 2 次（清理残留 rehang）= 撤成功
        self.ex.cancel_result = [
            {"ok": True, "status": STATUS_FILLED, "raw": {}},
            {"ok": True, "status": STATUS_RESTING,
             "raw": {"cancelStatus": {"status": "cancelled"}}},
        ]
        self.ex.orders_now = [{"coin": "BTC", "oid": REHANG_OID, "is_trigger": True,
                               "reduce_only": True, "sz": QTY, "trigger_px": STOP}]

    def test_residual_stop_cancelled_before_closing_alert(self):
        ex = self.executor()
        self.assertTrue(ex.on_exit(self.pid, "ma20", REF_PRICE))

        self.assertIn({"kind": "cancel", "coin": "BTC", "oid": REHANG_OID}, self.ex.calls,
                      "break 出 purpose 循环后，交易所侧残留的 rehang 单仍必须被撤")
        self.assertEqual(self.db.hl_order("BTC_USDT", BAR, PURPOSE_EXIT_REHANG)["status"],
                         "cancelled", "撤掉的残留单要按 oid 落到 cancelled")
        self.assertEqual(self.stop_row()["status"], "filled",
                         "成交的那张止损行仍是 filled")
        # 零新单：既不许平仓，也不许重挂（含「清理后重挂」）
        self.assertEqual(self.ex.orders(), [], "这条路径一张新单都不许发")
        self.assertEqual(self.ex.closes(), [])
        self.assertEqual(self.ex.triggers(), [])
        row = self.db.position_by_id(self.pid)
        self.assertEqual(row["status"], "open", "证据不足仍不关账")
        self.assertEqual(row["hl_status"], "closing", "仍置 closing 交宽限/对账")
        self.assertIn("无需人工操作", "\n".join(self.notifier.messages),
                      "残留单已清干净，才可以说「无需人工操作」")


class TestResidualCancelFilledKeepsAttribution(Base):
    """P1-B 残余闭环（2）：清理残留止损时 cancel 返回 ok+filled → DB 行不得写 cancelled。

    `_verify_no_residual_stops` 用交易所挂单表发现残留触发单，但那张单可能在本轮已经
    成交（快照与撤单之间）。此时它在交易所已不在场，写 cancelled 会把「成交」抹成
    「撤销」—— 成交归因（哪张单、多少量、什么价）永久丢失。cancelled / notFound
    （适配层映射成 ok+resting）仍是 cancelled。
    """

    def setUp(self):
        super().setUp()
        self.pid = self.open_pos()
        self.purpose = PURPOSE_STOP + ":fix"
        self.residual_oid = "a1b2c3d4-0000-4000-8000-000000000001"
        self.db.hl_record_intent("BTC_USDT", "BTC", BAR, self.purpose, 1, QTY, px=STOP,
                                 reduce_only=True, status="resting", oid=self.residual_oid)
        self.ex.orders_now = [{"coin": "BTC", "oid": self.residual_oid, "is_trigger": True,
                               "reduce_only": True, "sz": QTY, "trigger_px": STOP}]

    def _verify(self):
        self.executor()._verify_no_residual_stops(self.db.position_by_id(self.pid), "BTC")

    def _row(self):
        return self.db.hl_order("BTC_USDT", BAR, self.purpose)

    def test_cancel_filled_keeps_row_filled(self):
        self.ex.cancel_result = {"ok": True, "status": STATUS_FILLED,
                                 "raw": {"cancelStatus": {"status": "filled"}}}
        self._verify()
        row = self._row()
        self.assertEqual(row["status"], "filled",
                         "撤单返回 filled = 这张单已成交，行必须落 filled（保留成交归因）")
        self.assertNotEqual(row["status"], "cancelled")
        self.assertIsNone(row["error"])

    def test_cancelled_and_notfound_still_marked_cancelled(self):
        for raw_status in ("cancelled", "notFound"):
            with self.subTest(raw_status=raw_status):
                self.db.hl_update_order("BTC_USDT", BAR, self.purpose,
                                        status="resting", error=None)
                self.ex.cancel_result = {"ok": True, "status": STATUS_RESTING,
                                         "raw": {"cancelStatus": {"status": raw_status}}}
                self._verify()
                self.assertEqual(self._row()["status"], "cancelled",
                                 f"{raw_status} 表示单确实不在场，仍按既有一口径落 cancelled")


if __name__ == "__main__":
    unittest.main()
