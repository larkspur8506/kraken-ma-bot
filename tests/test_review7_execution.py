"""第七轮复审（review7）执行器 / DB 修复的定向回归（2026-11-13）。

钉住的行为（每一条都对应一份审计结论，改动前都能复现失败）：

A1 兜底平仓的「剩余量」
  - 1 个数量 tick 是**真实余仓**，不能当浮点噪声吞掉（旧实现 `remain <= tick` → 0）；
  - 回执缺 `filled_sz` / 非数字 / NaN / inf / ≤0 → 剩余量**未知**，既不得整笔关账，
    也不得写 `aborted`（「无仓量未知不得中止」）；
  - 上述三个出口（补挂失败兜底、开仓保护失败、离场兜底）都走同一拆分口径，
    且下一轮 `reconcile` 必须按真实余量收敛、不 kill。

A2 平仓结果未知（超时 / 响应丢失）
  - `KrExchange.place_order` 对 transport 异常、5xx/408/429、回包不可解析、
    「说成交但无 EXECUTION 明细」回 `status=unknown`（明确拒单仍是 `error`）；
  - 执行器见到 unknown：先强制刷新「可信持仓 / 挂单 / 成交」，再决定
    关账（证据齐全）/ 按**真实余量**恢复保护 / 持久化未知态；
  - 绝不盲重发平仓、绝不按旧量挂止损、绝不在空仓上挂孤儿触发单；
  - 重启（新建 Executor）后仍不重复下单。

A3 `hl_log_event` 与其它写方法共用同一把 RLock；`hl_set_kill` 的
   kill/origin/reason 三键一次事务写入（没有「kill=1 + 旧 origin」的撕裂窗口）。

B4 组合名义上限的分母用**全账户权益**（定仓基数仍扣 MR 敞口）。

aborted 终态：确认全平的中止行立即释放槽位（`db.open_position` 不再返回它），
   引擎侧对老行（status 仍是 pending）也能安全释放。

B7 已实现亏损熔断的告警不再承诺「核对后可在面板恢复」（滚动窗口仍越线会立刻再触发）。

全部离线：临时 sqlite + 复用 tests/test_hl_executor.py 的假 info / 假 exchange。
"""
from __future__ import annotations

import math
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import engine, market                                        # noqa: E402
from app.db import DB                                                # noqa: E402
from app.hl import MODE_LIVE, PURPOSE_STOP                           # noqa: E402
from app.hl.exchange import STATUS_ERROR, STATUS_FILLED, STATUS_RESTING  # noqa: E402
from app.hl.executor import Executor, ExecParams                     # noqa: E402
from app.hl.info import HlInfoError                                  # noqa: E402
from app.kr.exchange import KrExchange, KrApiError, STATUS_UNKNOWN   # noqa: E402
from tests.test_audit_r6_executor import Base, UUID_STOP, _fail, _partial  # noqa: E402
from tests.test_hl_executor import _pos, _snap                       # noqa: E402
from tests.test_engine import cfg as engine_cfg, entry_setup         # noqa: E402

BTC_TICK = 1e-4        # Kraken PF_XBTUSD 真实 contractValueTradePrecision=4


def _unknown_receipt(msg="response timeout after accepted order"):
    """平仓「请求已发出、响应丢失」的回执（KrExchange 真实分支会产出的形状）。"""
    return {"ok": False, "status": STATUS_UNKNOWN, "oid": None, "avg_px": None,
            "filled_sz": None, "raw": {}, "error": msg, "not_executed": False}


class Base7(Base):
    """在 r6 的 Base 上把 BTC 精度改成真值（tick=1e-4），并备好离场所需的假件。"""

    def setUp(self):
        super().setUp()
        self.info._specs["BTC"] = {"sz_decimals": 4, "max_leverage": 100,
                                   "only_isolated": False}
        # 闸门/静置/两击都是**类属性**（跨 Executor 实例存活，见 executor 顶部注释）：
        # 用例之间必须清掉，否则上一例刚成交的 15s 静置窗口会挡住这一例的动作。
        Executor._settle_until.clear()
        Executor._churn_hits.clear()
        Executor._churn_alerted.clear()
        Executor._size_strikes.clear()

    # ---------------------------------------------------------------- 造景
    def open(self, stop_fail=False):
        """建一个已确认的持仓（走真实 on_entry）。返回 (pid, bar_ms, sz)。"""
        pid, bar_ms = self.open_position()
        self.db.set_pos_hl(pid, hl_stop_oid=UUID_STOP)
        self.db.hl_update_order("BTC_USDT", bar_ms, PURPOSE_STOP, oid=UUID_STOP,
                                status="resting")
        self.ex.calls.clear()
        self.notifier.messages.clear()
        sz = float(self.db.position_by_id(pid)["hl_sz"])
        return pid, bar_ms, sz

    def fill_close(self, sz, px=84500.0, oid=UUID_STOP, ts=None):
        """落一笔真实的平仓成交（`_fill_close_proof` 的证据来源）。"""
        self.db.hl_upsert_fill(tid=f"t-{time.time()}-{sz}", symbol="BTC_USDT", coin="BTC",
                               side=-1, sz=sz, px=px, fee=0.01, fee_token="USD",
                               fee_quote=0.01, dir="Close Long", closed_pnl=-5.0,
                               reduce_only=True, oid=oid,
                               ts=float(ts if ts is not None else time.time()))

    def orders(self, trigger=None):
        out = [c for c in self.ex.calls if c[0] == "order"]
        if trigger is True:
            return [c for c in out if c[6] is not None]
        if trigger is False:
            return [c for c in out if c[6] is None]
        return out


# ====================================================================== A1
class TestA1RemainderSplit(Base7):
    """拆分口径：1 tick 是真实余仓；未知量一律 None（不推断）。"""

    def test_one_tick_remainder_is_not_swallowed(self):
        ex = self.executor()
        filled, remain = ex._filled_split("BTC", 0.0083, _partial(0.0082, 83000.0))
        self.assertAlmostEqual(filled, 0.0082, places=10)
        self.assertAlmostEqual(remain, BTC_TICK, places=10,
                               msg="剩 1 tick 是真余仓，不能当浮点噪声（A1）")

    def test_two_tick_remainder_kept(self):
        _, remain = self.executor()._filled_split("BTC", 0.0083, _partial(0.0081, 83000.0))
        self.assertAlmostEqual(remain, 2 * BTC_TICK, places=10)

    def test_full_fill_is_zero(self):
        _, remain = self.executor()._filled_split("BTC", 0.0083, _partial(0.0083, 83000.0))
        self.assertEqual(remain, 0.0)

    def test_float_dust_still_closes(self):
        """请求 0.00833、回执 0.008329999999999999（浮点往返）→ 仍算全平。"""
        _, remain = self.executor()._filled_split("BTC", 0.00833, _partial(0.008329999999999, 1.0))
        self.assertEqual(remain, 0.0)

    def test_bad_filled_sz_is_unknown(self):
        ex = self.executor()
        for bad in (None, "", "abc", float("nan"), float("inf"), 0, 0.0, -0.001):
            res = {"ok": True, "status": STATUS_FILLED, "avg_px": 1.0, "filled_sz": bad}
            filled, remain = ex._filled_split("BTC", 0.00833, res)
            self.assertIsNone(remain, f"filled_sz={bad!r} 必须判未知，不能推断全平")
            self.assertAlmostEqual(filled, 0.00833, places=10)

    # ------------------------------------------------------------ 调用点：补挂失败兜底
    def _repair(self, row, sz, close_res, snap_positions=None):
        self.ex.exit = close_res
        snap = _snap(positions=(snap_positions if snap_positions is not None
                                else [_pos("BTC", size=sz, entry=83000.0)]))
        self.executor()._repair_missing_stop(row, snap, kill_before=False)

    def test_repair_leaves_one_tick_open(self):
        pid, _bar, sz = self.open()
        self.ex.stop = _fail("stop rejected")
        self.ex.orders_now = []
        row = dict(self.db.position_by_id(pid))
        self._repair(row, sz, _partial(sz - BTC_TICK, 84500.0))

        got = self.db.position_by_id(pid)
        self.assertEqual(got["status"], "open",
                         "兜底平仓剩 1 tick 时**不能**整笔关账（旧实现这里写 closed）")
        self.assertAlmostEqual(float(got["hl_sz"]), BTC_TICK, places=10)
        self.assertTrue(any("部分成交" in e["message"] for e in self.events("error")))

    def test_repair_unknown_quantity_does_not_close(self):
        pid, _bar, sz = self.open()
        self.ex.stop = _fail("stop rejected")
        self.ex.orders_now = []
        row = dict(self.db.position_by_id(pid))
        bad = {"ok": True, "status": STATUS_FILLED, "avg_px": 84500.0, "filled_sz": None}
        self._repair(row, sz, bad)

        got = self.db.position_by_id(pid)
        self.assertEqual(got["status"], "open", "成交量未知时不得关账")
        self.assertAlmostEqual(float(got["hl_sz"]), sz, places=10, msg="量未知时不许改本地量")
        self.assertTrue(any("无法确认" in m for m in self.notifier.messages),
                        self.notifier.messages)

    # ------------------------------------------------------------ 下一轮对账行为
    def test_next_reconcile_repairs_one_tick_remainder_without_kill(self):
        pid, _bar, sz = self.open()
        self.ex.stop = _fail("stop rejected")
        self.ex.orders_now = []
        row = dict(self.db.position_by_id(pid))
        self._repair(row, sz, _partial(sz - BTC_TICK, 84500.0))

        # 下一轮：交易所只有那 1 tick，本地也已经是 1 tick，且**没有止损**
        # （生产里下一轮对账是 5 分钟后；这里显式清掉入场成交那条 15s 静置闸门）
        Executor._settle_until.clear()
        self.ex.stop = None                      # 这次允许补挂成功
        self.ex.orders_now = []
        self.ex.calls.clear()
        self.info._snap = _snap(positions=[_pos("BTC", size=BTC_TICK, entry=83000.0)])
        ex = self.executor()
        res = ex.reconcile(kill_before=False)
        # reconcile 只报告；补挂由调用方（run）按 missing_stop 派发 —— 与生产同序
        for r in res.get("missing_stop") or []:
            ex._repair_missing_stop(r, self.info._snap, kill_before=False)

        self.assertFalse(self.db.hl_kill(), "剩 1 tick 是正常余仓，不能 kill")
        trig = self.orders(trigger=True)
        self.assertEqual(len(trig), 1, f"必须按真实余量补挂止损：{self.ex.calls}")
        self.assertAlmostEqual(trig[0][3], BTC_TICK, places=10,
                               msg="补挂量必须是真实余量（不是旧量、不是 0）")
        self.assertEqual(self.db.position_by_id(pid)["status"], "open")
        # 补挂之后再过一轮：挂单表里有这张 1 tick 的止损 → 对账必须干净（不再报缺止损）
        self.info._snap = _snap(
            positions=[_pos("BTC", size=BTC_TICK, entry=80000.0)],
            orders=[{"coin": "BTC", "oid": "s-new", "is_buy": False, "sz": BTC_TICK,
                     "limit_px": 80600.0, "reduce_only": True, "trigger_px": 80600.0,
                     "is_trigger": True, "timestamp": 1}])
        res2 = self.executor().reconcile(kill_before=False)
        self.assertEqual(res2["problems"], [], "补挂止损后对账必须干净")


# ====================================================================== A2 KrExchange
class _StubInfo:
    def all_mids(self, dex=""):
        return {"BTC": 75000.0}

    def round_price(self, coin, px):
        return round(float(px), 1)

    def contract_size(self, coin):
        return 1.0


class _StubEndpoints:
    def __init__(self, exc=None, resp=None):
        self.exc, self.resp, self.calls = exc, resp, []

    def sendorder(self, params):
        self.calls.append(params)
        if self.exc is not None:
            raise self.exc
        return self.resp


class TestA2ExchangeUnknown(Base7):
    """`KrExchange.place_order` 必须区分「明确拒单」与「结果未知」（真实分支调用）。"""

    def _ex(self, **kw):
        return KrExchange(api_key="k", secret_b64="aaaa", dry_run=False,
                          info=_StubInfo(), endpoints=_StubEndpoints(**kw))

    def test_transport_exception_is_unknown(self):
        for exc in (TimeoutError("read timed out"), ConnectionError("reset by peer"),
                    ValueError("Expecting value: line 1 column 1")):
            res = self._ex(exc=exc).place_order("BTC", True, 0.001, 75000.0)
            self.assertFalse(res["ok"])
            self.assertEqual(res["status"], STATUS_UNKNOWN,
                             f"{type(exc).__name__} 之后不知道单子有没有被受理")

    def test_5xx_and_429_are_unknown_4xx_is_definite(self):
        for status, want in ((503, STATUS_UNKNOWN), (500, STATUS_UNKNOWN),
                             (429, STATUS_UNKNOWN), (408, STATUS_UNKNOWN),
                             (401, STATUS_ERROR), (400, STATUS_ERROR),
                             (422, STATUS_ERROR)):
            res = self._ex(exc=KrApiError("bad", status, {})).place_order(
                "BTC", True, 0.001, 75000.0)
            self.assertEqual(res["status"], want, f"HTTP {status}")

    def test_filled_without_execution_detail_is_unknown(self):
        ep = _StubEndpoints(resp={"result": "success", "sendStatus": {
            "status": "filled", "order_id": "o-1"}})
        ex = KrExchange(api_key="k", secret_b64="a", dry_run=False,
                        info=_StubInfo(), endpoints=ep)
        res = ex.place_order("BTC", True, 0.001, 75000.0)
        self.assertFalse(res["ok"])
        self.assertEqual(res["status"], STATUS_UNKNOWN)
        self.assertIsNone(res["avg_px"])
        self.assertIsNone(res["filled_sz"])

    def test_local_validation_errors_stay_definite(self):
        """发单前就失败（sz<=0 / cloid 非法 / contractSize 缺失）→ 明确失败。"""
        res = self._ex().place_order("BTC", True, 0.0, 75000.0)
        self.assertEqual(res["status"], STATUS_ERROR)


# ====================================================================== A2 执行器
class TestA2ExitUnknownRecovery(Base7):
    """平仓结果未知：先取证，再动作。"""

    def _exit(self, pid):
        return self.executor().on_exit(pid, "ma20", 84400.0)

    def test_close_filled_response_lost_closes_from_fill_proof(self):
        """已成交、响应丢失：不得再发任何单；凭成交证据关账。"""
        pid, _bar, sz = self.open()
        self.fill_close(sz, px=84500.0)

        def lost(coin, is_buy, q, px):
            # 交易所已经成交并空仓，但响应丢了
            self.info._snap = _snap(positions=[])
            return _unknown_receipt()
        self.ex.exit = lost
        self.info._snap = _snap(positions=[_pos("BTC", size=sz, entry=83000.0)])

        self.assertTrue(self._exit(pid))

        row = self.db.position_by_id(pid)
        self.assertEqual(row["status"], "closed", "证据齐全 → 按成交价关账")
        self.assertAlmostEqual(float(row["exit_price"]), 84500.0, places=4)
        self.assertEqual(self.orders(trigger=True), [],
                         "空仓上不许挂 reduce-only 触发单（孤儿单会咬下一笔仓）")
        self.assertEqual(len(self.orders(trigger=False)), 1, "只允许那一次平仓尝试")
        text = "\n".join(self.notifier.messages)
        self.assertIn("已按成交价", text)
        self.assertNotIn("仓位继续受保护", text)

    def test_partial_fill_response_lost_rehangs_only_real_remainder(self):
        """部分成交、响应丢失：只按**交易所真实余量**恢复保护。"""
        pid, _bar, sz = self.open()
        half = round(sz / 2, 4)

        def lost(coin, is_buy, q, px):
            self.info._snap = _snap(positions=[_pos("BTC", size=half, entry=83000.0)])
            return _unknown_receipt()
        self.ex.exit = lost
        self.info._snap = _snap(positions=[_pos("BTC", size=sz, entry=83000.0)])

        self.assertTrue(self._exit(pid))

        row = self.db.position_by_id(pid)
        self.assertEqual(row["status"], "open")
        self.assertAlmostEqual(float(row["hl_sz"]), half, places=6,
                               msg="本地量必须收敛到交易所真实余量")
        trig = self.orders(trigger=True)
        self.assertEqual(len(trig), 1)
        self.assertAlmostEqual(trig[0][3], half, places=6,
                               msg="挂回止损的量必须是真实余量（旧实现按旧量 sz 挂）")
        self.assertEqual(len(self.orders(trigger=False)), 1, "不许重发平仓单")
        self.assertIn("真实余量", "\n".join(self.notifier.messages))

    def test_existing_covering_stop_means_no_new_order(self):
        pid, _bar, sz = self.open()

        def lost(coin, is_buy, q, px):
            self.info._snap = _snap(positions=[_pos("BTC", size=sz, entry=83000.0)],
                                    orders=[{"coin": "BTC", "oid": "s-2", "is_buy": False,
                                             "sz": sz, "limit_px": 80600.0,
                                             "reduce_only": True, "trigger_px": 80600.0,
                                             "is_trigger": True, "timestamp": 1}])
            return _unknown_receipt()
        self.ex.exit = lost
        self.info._snap = _snap(positions=[_pos("BTC", size=sz, entry=83000.0)])

        self._exit(pid)
        self.assertEqual(self.orders(trigger=True), [], "已有覆盖止损时不许重复挂")
        self.assertEqual(len(self.orders(trigger=False)), 1)
        self.assertIn("已有止损覆盖", "\n".join(self.notifier.messages))

    def test_snapshot_unreadable_persists_unknown_and_sends_nothing(self):
        """刷新失败 → 持久化未知态、不宣称已保护、不再发单。"""
        pid, _bar, sz = self.open()
        ex = self.executor()
        orig = self.info.snapshot
        calls = {"n": 0}

        def flaky(address, mids=None):
            calls["n"] += 1
            if calls["n"] >= 2:                # 第 1 次 = on_exit 入口那一次
                raise HlInfoError("accounts timeout")
            return orig(address, mids)
        self.info.snapshot = flaky
        self.ex.exit = _unknown_receipt()

        self.assertTrue(ex.on_exit(pid, "ma20", 84400.0))

        row = self.db.position_by_id(pid)
        self.assertEqual(row["status"], "open", "读不准时不许关账")
        self.assertEqual(row["hl_status"], "closing", "必须落成「待核对」，让对账继续接手")
        self.assertTrue(str(self.db.get_config_str(f"hl_exit_unknown:{pid}", "") or "").strip(),
                        "未知态要持久化（重启后可查）")
        self.assertEqual(self.orders(trigger=True), [])
        self.assertEqual(len(self.orders(trigger=False)), 1)
        text = "\n".join(self.notifier.messages)
        self.assertIn("未知", text)
        self.assertNotIn("继续受保护", text)

    def test_flat_without_fill_evidence_enters_grace_instead_of_closing(self):
        """空仓但拿不到成交证据：进宽限（closing），绝不用参考价关账。"""
        pid, _bar, sz = self.open()

        def lost(coin, is_buy, q, px):
            self.info._snap = _snap(positions=[])
            return _unknown_receipt()
        self.ex.exit = lost
        self.info._snap = _snap(positions=[_pos("BTC", size=sz, entry=83000.0)])
        self.ex.orders_now = []

        self.assertTrue(self._exit(pid))
        row = self.db.position_by_id(pid)
        self.assertEqual(row["status"], "open", "无证据不许关账")
        self.assertEqual(row["hl_status"], "closing")
        self.assertIsNone(row["exit_price"])
        self.assertEqual(self.orders(trigger=True), [])
        self.assertIn("宽限", "\n".join(self.notifier.messages))

    def test_rehang_stop_unknown_does_not_send_second_close(self):
        """重挂止损结果也未知：绝不继续发兜底平仓，持久化 closing 交对账收敛。"""
        pid, _bar, sz = self.open()

        def lost(coin, is_buy, q, px):
            self.info._snap = _snap(positions=[_pos("BTC", size=sz, entry=83000.0)])
            return _unknown_receipt()
        self.ex.exit = lost
        # 重挂止损单同样「已发出但响应丢失」：FakeExchange 的 trigger_px 分支返回 self.ex.stop
        self.ex.stop = _unknown_receipt("stop rehang response lost")
        self.info._snap = _snap(positions=[_pos("BTC", size=sz, entry=83000.0)])
        self.ex.orders_now = []

        self.assertTrue(self._exit(pid))
        row = self.db.position_by_id(pid)
        self.assertEqual(row["status"], "open", "未知结果不许关账")
        self.assertEqual(row["hl_status"], "closing", "必须落成待核对")
        self.assertTrue(str(self.db.get_config_str(f"hl_exit_unknown:{pid}", "") or "").strip())
        closes = self.orders(trigger=False)
        self.assertEqual(len(closes), 1, "只允许最初那次平仓尝试；重挂未知后不得再发平仓单")
        text = "\n".join(self.notifier.messages)
        self.assertNotIn("继续受保护", text)

    def test_restart_after_unknown_does_not_resend(self):
        """结果未知落成 closing 后，重启（新 Executor）不得重复下单。"""
        pid, _bar, sz = self.open()
        ex = self.executor()
        orig = self.info.snapshot
        calls = {"n": 0}

        def flaky(address, mids=None):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise HlInfoError("accounts timeout")
            return orig(address, mids)
        self.info.snapshot = flaky
        self.ex.exit = _unknown_receipt()
        ex.on_exit(pid, "ma20", 84400.0)
        self.ex.calls.clear()
        self.notifier.messages.clear()

        # 「重启」：全新的 Executor 实例（内存态全丢，只剩 DB）
        self.info.snapshot = orig
        again = self.executor()
        self.assertFalse(again.on_exit(pid, "ma20", 84400.0),
                         "closing 行必须让出接管权（不许重复平仓）")
        self.assertEqual(self.ex.calls, [], "重启后不许发任何单")

    def test_known_rejection_still_rehangs_for_old_size(self):
        """反证：明确拒单（status=error）仍走原路径 —— 按当前量挂回止损。"""
        pid, _bar, sz = self.open()
        self.ex.exit = _fail("insufficient margin")
        self.ex.stop = {"ok": True, "status": STATUS_RESTING, "oid": "s-9", "avg_px": None,
                        "filled_sz": None, "error": None, "raw": {}}
        self.info._snap = _snap(positions=[_pos("BTC", size=sz, entry=83000.0)])

        self.assertTrue(self._exit(pid))
        trig = self.orders(trigger=True)
        self.assertEqual(len(trig), 1)
        self.assertAlmostEqual(trig[0][3], sz, places=8)
        self.assertIn("已挂回", "\n".join(self.notifier.messages))


# ====================================================================== aborted 终态
class TestAbortedTerminal(Base7):
    def _abort_flow(self, close_res):
        pid = self.add_signal_like()
        self.ex.stop = _fail("trigger rejected")
        self.ex.exit = close_res
        self.executor().on_entry(pid)
        return pid

    def add_signal_like(self, offset_ms=0):
        """造一个 pending 行（不经过 walk），与引擎建行同形。"""
        bar_ms = int(time.time() * 1000) - 4 * 3600 * 1000 + int(offset_ms)
        return self.db.create_position(symbol="BTC_USDT", signal_bar_ms=bar_ms, side=1,
                                       entry_price=83000.0, stop_price=80600.0,
                                       stop_dist=0.03, contracts=1, coin_qty=0.001,
                                       notional=83.0, risk_u=20.0, equity_at_entry=1000.0)

    def test_full_flatten_marks_aborted_and_releases_slot(self):
        pid = self._abort_flow(None)          # FakeExchange 默认 = 全量成交、量已知
        row = self.db.position_by_id(pid)
        self.assertEqual(row["hl_status"], "aborted")
        self.assertIsNone(self.db.open_position("BTC_USDT"),
                          "确认全平的中止行必须立即释放槽位")
        self.assertEqual(len(self.db.hl_recent_orders(10)), 2, "审计行仍要在（entry+stop）")

    def test_unknown_quantity_does_not_abort(self):
        bad = {"ok": True, "status": STATUS_FILLED, "avg_px": 83050.0, "filled_sz": None}
        pid = self._abort_flow(bad)
        row = self.db.position_by_id(pid)
        self.assertNotEqual(row["hl_status"], "aborted",
                            "成交量未知时不得中止（可能还剩仓）")
        self.assertIn("余量未知", "\n".join(self.notifier.messages))

    def test_unknown_outcome_does_not_abort(self):
        pid = self._abort_flow(_unknown_receipt())
        row = self.db.position_by_id(pid)
        self.assertNotEqual(row["hl_status"], "aborted")
        self.assertIn("结果未知", "\n".join(self.notifier.messages))

    def test_one_tick_left_keeps_open_with_real_size(self):
        """开仓保护失败 → 兜底平仓只剩 1 tick：必须留在 open 且量 = 真实余量。"""
        # 先正常开一仓拿到真实定仓量（BTC @83000、2% 风险 ≈ 0.0083）
        pid = self.add_signal_like()
        self.ex.stop = {"ok": True, "status": STATUS_RESTING, "oid": "s-1", "avg_px": None,
                        "filled_sz": None, "error": None, "raw": {}}
        self.executor().on_entry(pid)
        sz = float(self.db.position_by_id(pid)["hl_sz"])
        self.assertGreater(sz, BTC_TICK)

        Executor._settle_until.clear()          # 等价于「15s 静置窗口已过」
        pid2 = self.add_signal_like(offset_ms=4 * 3600 * 1000)
        self.ex.stop = _fail("trigger rejected")
        self.ex.exit = _partial(sz - BTC_TICK, 83050.0)
        self.executor().on_entry(pid2)

        row = self.db.position_by_id(pid2)
        self.assertEqual(row["hl_status"], "open", "剩 1 tick 不能中止")
        self.assertEqual(row["status"], "pending", "仓没被确认过（中止流程未走完）")
        self.assertAlmostEqual(float(row["hl_sz"]), BTC_TICK, places=10)


class _Notifier:
    def __init__(self):
        self.msgs = []

    def send_message(self, m):
        self.msgs.append(m)
        return True


class TestEngineAbortedCompat(unittest.TestCase):
    """引擎侧对老 aborted 行（status 仍 pending）安全释放槽位。"""

    SYMBOL = "BTC_USDT"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(str(Path(self.tmp.name) / "t.db"))
        self.c = engine_cfg()
        self.c.symbols = [self.SYMBOL]
        self.c.dry_run = False
        self.bars, self.i = entry_setup()
        self.signal_ms = self.bars[self.i].open_ms
        self.feed = self.bars
        self.notifier = _Notifier()
        self._orig = market.fetch
        market.fetch = self._fetch

    def tearDown(self):
        market.fetch = self._orig
        self.tmp.cleanup()

    def _fetch(self, symbol, now_ms=None, allow_fallback=False):
        return market.Feed(symbol=symbol, source="hl-candle", bars=self.feed,
                           fetched_at_ms=now_ms if now_ms is not None else self.feed[-1].open_ms)

    def test_aborted_pending_row_is_ignored_and_slot_freed(self):
        pid = self.db.create_position(symbol=self.SYMBOL, signal_bar_ms=self.signal_ms,
                                      side=1, entry_price=83000.0, stop_price=80600.0,
                                      stop_dist=0.03, contracts=1, coin_qty=0.001,
                                      notional=83.0, risk_u=20.0, equity_at_entry=1000.0)
        self.db.set_pos_hl(pid, hl_status="aborted")

        # 前置：槽位已经不被它占着（DB 读路径）
        self.assertIsNone(self.db.open_position(self.SYMBOL))

        engine.tick(self.db, self.c, self.notifier, now_ms=self.signal_ms + 60_000)

        # 老行兼容分支（不经 db.open_position 的读路径也能收口）—— 直接喂行验证
        row = self.db.position_by_id(pid)
        self.assertEqual(row["hl_status"], "aborted", "审计标记不能被抹掉")

        # 关键行为：不再按 pending 占槽 12 小时，也不会发「待确认仓位已作废」误导文案
        self.assertFalse([m for m in self.notifier.msgs if "待确认仓位已作废" in m],
                         self.notifier.msgs)
        self.assertIsNone(self.db.open_position(self.SYMBOL))

    def test_engine_branch_releases_when_row_is_handed_in(self):
        """引擎分支本身：把 aborted 行直接喂给 _tick_symbol 也必须释放。"""
        pid = self.db.create_position(symbol=self.SYMBOL, signal_bar_ms=self.signal_ms,
                                      side=1, entry_price=83000.0, stop_price=80600.0,
                                      stop_dist=0.03, contracts=1, coin_qty=0.001,
                                      notional=83.0, risk_u=20.0, equity_at_entry=1000.0)
        self.db.set_pos_hl(pid, hl_status="aborted")
        row = self.db.position_by_id(pid)
        # 绕开 DB 过滤（模拟别的读路径/老 DB 层把行交进来）
        with mock.patch.object(self.db, "open_position", lambda symbol: row):
            engine._tick_symbol(self.db, self.c, self.notifier, self.SYMBOL,
                                self.signal_ms + 60_000, None, [])
        self.assertEqual(self.db.position_by_id(pid)["status"], "ignored",
                         "引擎必须把 aborted 老行作废释放")
        msgs = [r["message"] for r in self.db.hl_recent_events(20)]
        self.assertTrue(any("开仓中止行" in m for m in msgs), msgs)


# ====================================================================== A3 DB 锁
class TestA3DbLocks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(str(Path(self.tmp.name) / "t.db"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_hl_log_event_concurrent_with_other_writes(self):
        """事件写入与其它写方法并发：0 异常、成功次数 == 落库行数。"""
        n = 300
        errors = []

        def ev():
            for i in range(n):
                try:
                    self.db.hl_log_event("heartbeat", f"hb {i}")
                except Exception as e:                              # noqa: BLE001
                    errors.append(f"ev:{type(e).__name__}:{e}")

        def cfg():
            for i in range(n):
                try:
                    self.db.set_config(f"k{i}", i)
                    self.db.hl_add_snapshot({"ts": time.time(), "equity": 1000.0,
                                             "positions": []})
                except Exception as e:                              # noqa: BLE001
                    errors.append(f"cfg:{type(e).__name__}:{e}")

        ts = [threading.Thread(target=ev), threading.Thread(target=cfg)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()

        self.assertEqual(errors, [], f"共享连接下并发写必须 0 异常（前 {len(errors)} 条）")
        rows = self.db.conn.execute("SELECT count(*) FROM hl_events").fetchone()[0]
        self.assertEqual(rows, n, "成功的调用必须一条不少地落库")

    def test_kill_origin_is_written_atomically(self):
        """kill 三键一次事务：不许逐键调用 set_config（那会留下撕裂窗口）。"""
        real = self.db.set_config
        calls = []

        def spy(key, value):
            calls.append(key)
            return real(key, value)

        self.db.set_config = spy
        try:
            self.db.hl_set_kill(True, "对账异常", origin="reconcile")
        finally:
            self.db.set_config = real
        self.assertEqual(calls, [], "hl_set_kill 不得再走逐键 set_config（A3）")
        self.assertTrue(self.db.hl_kill())
        self.assertEqual(self.db.hl_kill_origin(), "reconcile")

        # 解除：kill=0 + origin 复位，同样原子
        self.db.hl_set_kill(False, origin="manual")
        self.assertFalse(self.db.hl_kill())
        self.assertEqual(self.db.hl_kill_origin(), "manual")
        self.assertEqual(self.db.get_config_str("hl_exec_kill_reason"), "")

    def test_reader_never_sees_kill_with_stale_origin(self):
        """并发读者不许读到「kill=1 但 origin 还是上一轮的值」。

        读侧必须用**一次** `get_config()`（单次加锁的 SELECT）取整行，而不是
        `hl_kill()` + `hl_kill_origin()` 两次读 —— 后者即使写入原子，两次读之间
        也会被写者插进来（那是读侧撕裂，不是写侧撕裂）。
        """
        stop = threading.Event()
        bad = []

        def reader():
            while not stop.is_set():
                cfg = self.db.get_config()
                on = str(cfg.get("hl_exec_kill", "0")).strip('"') in ("1", "true")
                origin = str(cfg.get("hl_exec_kill_origin", "")).strip('"')
                if on and origin != "reconcile":
                    bad.append(origin)

        th = threading.Thread(target=reader)
        th.start()
        try:
            for _ in range(400):
                self.db.hl_set_kill(True, "x", origin="reconcile")
                self.db.hl_set_kill(False, origin="manual")
        finally:
            stop.set()
            th.join()
        self.assertEqual(bad, [], f"读到 {len(bad)} 次撕裂状态")


# ====================================================================== B4 / B7
class TestB4NotionalCapBase(Base7):
    """组合总名义上限的分母 = 全账户权益；定仓基数仍扣 MR 敞口。"""

    def _plan(self, notional):
        return SimpleNamespace(side=1, sz=0.01, notional=notional, below_min_notional=False,
                               stop_dist=0.02, leverage=5.0, max_safe_leverage=10.0)

    def test_cap_uses_full_equity_not_mr_deducted(self):
        mr = 500.0
        self.p.mr_margin_fn = lambda: mr
        self.p.max_total_notional_mult = 2.0
        ex = self.executor()
        snap = _snap(equity=1000.0, positions=[], orders=[], notional=mr)
        snap["equity_total"] = 1000.0

        self.assertAlmostEqual(ex.effective_equity(snap), 500.0, places=6,
                               msg="定仓基数必须继续扣掉 MR 敞口（既有设计）")

        gates = ex._entry_gates("BTC_USDT", self._plan(600.0), snap, equity=500.0)
        self.assertFalse([g for g in gates if "总名义" in g],
                         f"500(MR)+600 相对全账户 1000×2 不该被拒：{gates}")

        gates = ex._entry_gates("BTC_USDT", self._plan(1600.0), snap, equity=500.0)
        self.assertTrue([g for g in gates if "总名义" in g],
                        "2100 > 1000×2 必须拒（上限本身没被放宽）")

    def test_cap_falls_back_to_sizing_equity_without_trustworthy_total(self):
        self.p.max_total_notional_mult = 2.0
        ex = self.executor()
        snap = _snap(equity=1000.0, positions=[], orders=[], notional=0.0)
        snap["spot_ok"] = False                 # 权益不可信 → 不能用 equity_total
        gates = ex._entry_gates("BTC_USDT", self._plan(1500.0), snap, equity=500.0)
        self.assertTrue([g for g in gates if "总名义" in g])


class TestB7RealizedDdCopy(Base7):
    def test_alert_does_not_promise_panel_recovery(self):
        self.db.hl_upsert_fill(tid="loss-1", symbol="BTC_USDT", coin="BTC", side=1,
                               sz=1.0, px=1.0, fee=0.0, fee_token="USD", fee_quote=0.0,
                               dir="Close Long", closed_pnl=-250.0, reduce_only=True,
                               ts=time.time() - 3600)
        alerts = []
        ex = self.executor()
        snap = _snap(equity=1000.0)
        snap["equity_total"] = 1000.0
        out = ex._realized_dd_guard(snap, alerts)

        self.assertEqual(out["action"], "kill")
        self.assertTrue(self.db.hl_kill())
        text = "\n".join(str(a) for a in alerts)
        self.assertNotIn("核对后可在面板恢复", text, "文案不得承诺面板恢复")
        self.assertIn("滚动", text)
        self.assertIn("再次触发", text)


class TestJudgeCloseFillsTickBoundary(Base7):
    """`_judge_close_fills` 的容差也必须**小于一个 tick**（0/1/2 tick 全链同口径）。

    它判的是「成交合计 vs 持仓量」：差 1 tick 说明成交记录还缺一笔（或者真的只平了
    qty−1tick），拿不完整合计去关整仓会按偏小的量算均价/已实现盈亏。旧容差 `tol=tick`
    把这一格吞掉 —— 与 `_filled_split` 旧值 `remain <= tick` 是同一类错误。
    """

    def test_exact_fills_are_proof(self):
        pid, _bar, sz = self.open()
        self.fill_close(sz, px=84500.0)
        row = dict(self.db.position_by_id(pid))
        lo = float(row["opened_at"] or row["created_at"]) - 120.0
        proof = self.executor()._judge_close_fills(row, "BTC_USDT", "BTC", sz, lo)
        self.assertIsNotNone(proof)
        self.assertAlmostEqual(proof["px"], 84500.0, places=4)

    def test_one_tick_short_is_not_proof(self):
        pid, _bar, sz = self.open()
        self.fill_close(sz - BTC_TICK, px=84500.0)
        row = dict(self.db.position_by_id(pid))
        lo = float(row["opened_at"] or row["created_at"]) - 120.0
        self.assertIsNone(
            self.executor()._judge_close_fills(row, "BTC_USDT", "BTC", sz, lo),
            "成交合计差 1 tick（真实余仓）不能当「已全平」证据")

    def test_two_tick_short_is_not_proof(self):
        pid, _bar, sz = self.open()
        self.fill_close(sz - 2 * BTC_TICK, px=84500.0)
        row = dict(self.db.position_by_id(pid))
        lo = float(row["opened_at"] or row["created_at"]) - 120.0
        self.assertIsNone(
            self.executor()._judge_close_fills(row, "BTC_USDT", "BTC", sz, lo))

    def test_float_dust_is_proof(self):
        pid, _bar, sz = self.open()
        self.fill_close(sz - 1e-12, px=84500.0)
        row = dict(self.db.position_by_id(pid))
        lo = float(row["opened_at"] or row["created_at"]) - 120.0
        self.assertIsNotNone(
            self.executor()._judge_close_fills(row, "BTC_USDT", "BTC", sz, lo),
            "浮点往返噪声（远小于 1 tick）仍算全平")


class TestClosingRecoveryChain(Base7):
    """持久化 `hl_exit_unknown` 之后的下一轮 `run` 必须能收口（不能永远 closing）。"""

    def _mark_unknown(self, pid):
        self.db.set_pos_hl(pid, hl_status="closing")
        self.db.set_config(f"hl_exit_unknown:{pid}", '{"why": "test"}')

    def test_next_run_reopens_with_exchange_size_and_clears_flag(self):
        pid, _bar, sz = self.open()
        half = round(sz / 2, 4)
        self._mark_unknown(pid)
        # 交易所有半个仓（部分成交），已有一张覆盖真实余量的止损
        self.info._snap = _snap(
            positions=[_pos("BTC", size=half, entry=83000.0)],
            orders=[{"coin": "BTC", "oid": "s-half", "is_buy": False, "sz": half,
                     "limit_px": 80600.0, "reduce_only": True, "trigger_px": 80600.0,
                     "is_trigger": True, "timestamp": 1}])
        self.ex.calls.clear()

        alerts = self.executor().run()

        row = self.db.position_by_id(pid)
        self.assertEqual(row["hl_status"], "open", "下一轮必须解除待核对，不能永远 closing")
        self.assertAlmostEqual(float(row["hl_sz"]), half, places=9,
                               msg="未知路径不改量；恢复时必须按交易所真实量收敛")
        self.assertEqual(self.orders(), [], f"恢复过程不许发任何单：{self.ex.calls}")
        self.assertFalse(self.db.hl_kill())
        self.assertEqual(str(self.db.get_config_str(f"hl_exit_unknown:{pid}", "") or ""), "",
                         "已收口 → 恢复态键必须清掉（证据在 hl_events）")

    def test_next_run_closes_when_fill_evidence_arrives(self):
        pid, _bar, sz = self.open()
        self._mark_unknown(pid)
        self.info._snap = _snap(positions=[])          # 交易所已空仓
        self.fill_close(sz, px=84600.0)                # 成交后来同步进来了
        self.ex.calls.clear()

        self.executor().run()

        row = self.db.position_by_id(pid)
        self.assertEqual(row["status"], "closed")
        self.assertAlmostEqual(float(row["exit_price"]), 84600.0, places=4)
        self.assertEqual(self.orders(), [], "只认证据关账，绝不重发平仓单")
        self.assertEqual(str(self.db.get_config_str(f"hl_exit_unknown:{pid}", "") or ""), "")
        self.assertFalse(self.db.hl_kill())


class TestExitUnknownDirectionGuard(Base7):
    """交易所在该标的上是**反向仓** → 不是这一笔，绝不套本地 side 去挂止损/平仓。"""

    def test_opposite_exchange_side_is_left_to_human(self):
        pid, _bar, sz = self.open()

        def lost(coin, is_buy, q, px):
            self.info._snap = _snap(positions=[_pos("BTC", size=sz, side=-1, entry=83000.0)])
            return _unknown_receipt()
        self.ex.exit = lost
        self.info._snap = _snap(positions=[_pos("BTC", size=sz, entry=83000.0)])

        self.assertTrue(self.executor().on_exit(pid, "ma20", 84400.0))

        row = self.db.position_by_id(pid)
        self.assertEqual(row["hl_status"], "closing", "不知道是谁的仓 → 停手等人工")
        self.assertEqual(self.orders(trigger=True), [], "不许给反向仓挂本策略的止损")
        self.assertEqual(len(self.orders(trigger=False)), 1, "不许重发平仓单")
        self.assertIn("方向", "\n".join(self.notifier.messages))


if __name__ == "__main__":
    unittest.main()


