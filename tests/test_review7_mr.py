"""第七轮审核（B5/B6/F4/F7）修复的回归测试 — MR 腿 + 到期清仓。

钉住六件事（全部离线：临时 SQLite + 假 info/exchange，不发单、不连生产）：

A) **B5 超窗恢复**：离场意图超过 6h 后，「台账 OPEN + 交易所空仓」不能永久
   recon_err；用「该 signal_date 的历史意图 + 真实成交/持仓证据」认领，证据不足时
   明确 `action_required`（原因 + 已有证据 + 恢复路径），并且**不重复下单**。

B) **B6 片段核对与来源**：正常收尾与 `_adopt_exit` 共用一套决算 —— 台账 qty − Σ片段
   与本次成交证据核对，缺口写 `reconciliation` 且 `net_pnl_complete=False`（不静默
   记成「确定盈亏」）；片段价优先回执 `avg_px`，回落到限价/参考价时标来源。

C) **布尔口径**：`mr_enabled` 统一 strip/lower（" 1 "、"TRUE"、"On"、"yes"）。

D) **风控覆盖（F7）**：MR 下单写 `hl_orders` 影子行（purpose=mr_entry/mr_exit），
   入场执行既有的「当日下单上限 / 近一小时失败」两道闸（阈值复用现有 env）。

E) **IOC 未知结果禁止无证重发**：回执丢失时先按持仓/成交取证；证据不足只挂
   `action_required`，下一轮不得凭旧状态直接重发。

F) **到期清仓（F4）**：Kraken 形状下（snapshot 已含全账户、user_state_dex 同源）
   每仓只平一次、跨轮 closed 累计、MR 台账按真实成交价/配置费率关账、raw 合并保留
   片段；`agent.check_once` 对无到期概念的 venue 直接短路。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from app import mr_signal
from app.db import DB
from app.hl import agent as A
from app.hl import mr as M
from app.hl.executor import ExecParams

ET = ZoneInfo("America/New_York")
ADDR = "kraken"
#: 2026-11-13 12:00 ET（周五，场内）——离场/入场判定都用这个时刻
NOW = int(datetime(2026, 11, 13, 12, 0, tzinfo=ET).timestamp() * 1000)


def _ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso).replace(tzinfo=ET).timestamp() * 1000)


def sess(date: str, close: float):
    return mr_signal.SessionBar(
        date=date, open=close, high=close, low=close, close=close, bars=6,
        last_bar_ms=_ms(f"{date}T15:00:00"), last_bar_et="15:00")


#: 离场信号在场：最后一根 110 > 前四根收盘均值 102.5；入场会话 2026-11-05
EXIT_SESSIONS = [sess("2026-11-05", 100.0), sess("2026-11-06", 101.0),
                 sess("2026-11-09", 102.0), sess("2026-11-10", 103.0),
                 sess("2026-11-11", 104.0), sess("2026-11-12", 110.0)]

#: 入场信号在场（RSI2 极低）+ 窗口开在 2026-11-13 09:30~12:30 ET
ENTRY_SESSIONS = [sess("2026-11-04", 70.0), sess("2026-11-05", 68.0),
                  sess("2026-11-06", 66.0), sess("2026-11-09", 64.0),
                  sess("2026-11-10", 62.0), sess("2026-11-11", 60.0),
                  sess("2026-11-12", 55.0)]


class Info:
    """MR 腿用到的 info 子集（账户/持仓/成交/meta）。"""

    def __init__(self, *, szi: float = 0.0, entry: float = 100.0, mid: float = 110.0,
                 equity: float = 1000.0, fills=None, fills_api: str = "by_time",
                 fills_raises: bool = False, account_raises: bool = False,
                 total_ntl: str = "0", coin: str = "QQQ"):
        self.szi = float(szi)
        self.entry = float(entry)
        self.mid = float(mid)
        self.equity = float(equity)
        self.fills = list(fills or [])
        self.fills_api = fills_api
        self.fills_raises = fills_raises
        self.account_raises = account_raises
        self.total_ntl = total_ntl
        self.coin = coin

    # --- 账户 -----------------------------------------------------------
    def all_mids(self, dex: str = ""):
        return {self.coin: self.mid}

    def account_equity(self, address):
        if self.account_raises:
            raise RuntimeError("account read failed")
        return self.equity

    def user_state_dex(self, address, dex):
        pos = []
        if self.szi:
            pos = [{"position": {"coin": self.coin, "szi": str(self.szi),
                                 "entryPx": str(self.entry),
                                 "positionValue": str(abs(self.szi) * self.mid),
                                 "unrealizedPnl": "0",
                                 "leverage": {"marginUsed": "10"}}}]
        return {"marginSummary": {"marginUsed": "10", "totalNtlPos": self.total_ntl},
                "assetPositions": pos}

    def user_state(self, address):
        return self.user_state_dex(address, "kr")

    def perp_meta_dex(self, dex):
        return [{"name": self.coin, "szDecimals": 3, "maxLeverage": 5}]

    def round_price(self, coin, px):
        return round(float(px) * 10) / 10.0

    def candles(self, *a):
        return []

    def user_funding_history(self, *a, **kw):
        return []

    # --- 成交 -----------------------------------------------------------
    def user_fills_by_time(self, address, start_ms, end_ms=None, **_):
        if self.fills_api != "by_time":
            raise AttributeError("by_time 未实现")
        return self._filtered(start_ms, end_ms)

    def user_fills(self, address):
        if self.fills_api != "full":
            raise AttributeError("user_fills 未实现")
        return self._filtered(None, None)

    def _filtered(self, start_ms, end_ms):
        if self.fills_raises:
            raise RuntimeError("fills read failed")
        out = []
        for f in self.fills:
            t = float(f.get("time") or 0.0)
            if start_ms and t < start_ms:
                continue
            if end_ms and t > end_ms:
                continue
            out.append(dict(f))
        return out


class Ex:
    """写路径替身：默认「真实成交」并把仓位同步到 info。"""

    available = True
    init_error = ""
    dry_run = False

    def __init__(self, info: Info, *, entry=None, exit_=None):
        self.info = info
        self.calls = []
        self.entry = entry or self._fill_entry
        self.exit = exit_ or self._fill_exit

    def _fill_entry(self, coin, is_buy, sz, limit_px, **kw):
        self.info.szi += sz
        return {"ok": True, "status": "filled", "oid": "o-entry", "avg_px": limit_px,
                "filled_sz": sz, "error": "", "raw": {}}

    def _fill_exit(self, coin, is_buy, sz, limit_px, **kw):
        before = abs(self.info.szi)
        self.info.szi = max(0.0, before - sz)
        return {"ok": True, "status": "filled", "oid": "o-exit", "avg_px": limit_px,
                "filled_sz": min(sz, before), "error": "", "raw": {}}

    def place_order(self, coin, is_buy, sz, limit_px, reduce_only=False, tif="Ioc",
                    cloid=None, trigger_px=None):
        self.calls.append({"coin": coin, "is_buy": is_buy, "sz": sz, "px": limit_px,
                           "reduce_only": reduce_only, "cloid": cloid})
        fn = self.entry if is_buy and not reduce_only else self.exit
        return fn(coin, is_buy, sz, limit_px, reduce_only=reduce_only, cloid=cloid)

    def update_leverage(self, coin, lev, cross=True):
        return {"ok": True, "status": "ok", "error": ""}

    def mid(self, coin):
        return self.info.mid

    def slippage_limit(self, coin, is_buy, mid):
        return mid


def make_leg(db, info, ex=None, *, fee: float = 0.0, clock_ms: int = NOW):
    return M.HlMrLeg(db=db, info=info, exchange=ex or Ex(info), address_fn=lambda: ADDR,
                     config=M.HlMrConfig(coin="QQQ", dex="kr", taker_fee_rate=fee,
                                         fill_confirm_s=0.0, fill_poll_s=0.0),
                     clock=lambda: clock_ms / 1000.0)


def mr_entry(db, *, qty=10.0, entry=100.0, signal_date="2026-11-05",
             entry_ts: float = 0.0, raw=None):
    return db.mr_record_entry(
        signal_date=signal_date, entry_session="2026-11-05", qty=qty,
        qty_int=int(round(qty * 10 ** 4)), entry_price=entry, entry_order_id="e1",
        entry_ts=float(entry_ts or (NOW / 1000.0 - 3 * 86400)),
        notional=qty * entry, margin=qty * entry, equity_at_entry=1000.0,
        raw=raw if raw is not None else {"ref_price": entry, "cloid": "entry-cloid"})


def exit_intent(db, *, signal_date="2026-11-05", ts=None, sz=10.0):
    db.hl_log_event("mr_exit_intent", "MR 离场下单意图",
                    data={"signal_date": signal_date, "exit_signal_date": "2026-11-13",
                          "sz": sz, "ref_price": 110.0, "cloid": "exit-cloid"},
                    ts=float(ts if ts is not None else NOW / 1000.0 - 60.0))


def entry_intent(db, *, signal_date="2026-11-12", ts=None, sz=2.0, cloid="entry-cloid"):
    db.hl_log_event("mr_entry_intent", "MR 入场下单意图",
                    data={"signal_date": signal_date, "entry_session": "2026-11-13",
                          "planned_sz": sz, "ref_price": 100.0, "equity": 1000.0,
                          "cloid": cloid},
                    ts=float(ts if ts is not None else NOW / 1000.0 - 60.0))


def fill(*, t, px, sz, side="sell", coin="QQQ", tid=None, cloid="", reduce_only=1,
         dir_=None):
    direction = dir_ or ("Close Long" if side == "sell" else "Open Long")
    return {"coin": coin, "px": px, "sz": sz, "side": side, "time": int(t),
            "dir": direction, "reduce_only": reduce_only, "oid": "o1",
            "cloid": cloid, "tid": tid or f"f{int(t)}-{px}-{sz}", "fee": 0.0}


def row_of(db, trade_id):
    return db.conn.execute("SELECT * FROM mr_trades WHERE id=?", (trade_id,)).fetchone()


def raw_of(db, trade_id):
    row = row_of(db, trade_id)
    return json.loads(row["raw"] or "{}")


class MrBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(os.path.join(self.tmp.name, "t.db"))
        self.db.set_config("mr_enabled", "1")
        for key in ("HL_MAX_ORDERS_PER_DAY", "HL_MAX_FAIL_STREAK",
                    "HL_MAX_TOTAL_NOTIONAL_MULT", "KR_TAKER_FEE"):
            os.environ.pop(key, None)

    def tearDown(self):
        self.tmp.cleanup()
        for key in ("HL_MAX_ORDERS_PER_DAY", "HL_MAX_FAIL_STREAK",
                    "HL_MAX_TOTAL_NOTIONAL_MULT", "KR_TAKER_FEE"):
            os.environ.pop(key, None)


# ============================================================ C) 布尔口径

class TestMrEnabledBool(MrBase):
    def test_whitespace_and_case_are_normalised(self):
        leg = make_leg(self.db, Info())
        for value in ("1", " 1 ", "TRUE", "True", " on", "Yes ", "1\n"):
            self.db.set_config("mr_enabled", value)
            self.assertTrue(leg._enabled(), f"{value!r} 应视为启用")

    def test_unknown_values_stay_disabled(self):
        leg = make_leg(self.db, Info())
        for value in ("0", "", "  ", "off", "false", "garbage", None):
            self.db.set_config("mr_enabled", value if value is not None else "")
            self.assertFalse(leg._enabled(), f"{value!r} 应视为停用")


# ============================================================ A) B5 超窗恢复

class TestStaleExitRecovery(MrBase):
    """离场意图超过 6h：不能永久 recon_err，但也**不能凭空编造成交**（B5）。"""

    def test_stale_intent_without_any_evidence_is_not_closed(self):
        """超窗 + 成交接口可用但查不到 + 无回执 → 台账保持 OPEN + 明确人工出口。

        旧实现（本轮前一版）在这里按 mark 估算关账：那是一笔**编出来的**成交价，
        会直接进面板「累计已实现」。B5 要求「证据不足 → 明确人工核对」。
        """
        tid = mr_entry(self.db, qty=10.0, entry=100.0)
        exit_intent(self.db, ts=NOW / 1000.0 - 8 * 3600.0)      # 8h 前
        info = Info(szi=0.0, mid=110.0, fills=[])
        leg = make_leg(self.db, info)
        status = {}
        self.assertFalse(leg._adopt_exit(self.db.mr_open_trade(), NOW, status))
        row = row_of(self.db, tid)
        self.assertEqual(row["state"], "OPEN", "证据不足时不许关账")
        self.assertIsNone(row["exit_price"])
        self.assertIsNone(row["net_pnl"], "不许写入估算净利")
        self.assertEqual(status["action_required"]["kind"], "exit_adopt_stale_no_evidence")
        self.assertIn("不按参考价", status["action_required"]["reason"])
        self.assertEqual(status["recon"], "err")
        self.assertTrue(any(r["kind"] == M.ACTION_REQUIRED_KIND
                            for r in self.db.hl_recent_events(5)))
        # 没发任何单
        self.assertEqual(leg.exchange.calls, [])

    def test_stale_intent_with_fills_closes_at_real_vwap(self):
        tid = mr_entry(self.db, qty=10.0, entry=100.0)
        ts = NOW / 1000.0
        exit_intent(self.db, ts=ts - 8 * 3600.0)
        info = Info(szi=0.0, mid=110.0, fills=[
            fill(t=(ts - 9 * 3600.0) * 1000, px=109.8, sz=6.0),
            fill(t=(ts - 9 * 3600.0 + 60) * 1000, px=109.9, sz=4.0),
        ])
        leg = make_leg(self.db, info)
        status = {}
        self.assertTrue(leg._adopt_exit(self.db.mr_open_trade(), NOW, status))
        row = row_of(self.db, tid)
        self.assertAlmostEqual(row["exit_price"], (109.8 * 6 + 109.9 * 4) / 10, places=9)
        raw = raw_of(self.db, tid)
        self.assertEqual(raw["price_source"], "fills")
        self.assertFalse(raw["estimated"])
        self.assertTrue(raw["net_pnl_complete"])
        self.assertAlmostEqual(raw["fill_qty"], 10.0, places=9)
        self.assertEqual(len(raw["fill_ids"]), 2)
        self.assertEqual(status["adopted"], "exit")

    def test_stale_intent_falls_back_to_order_receipt(self):
        """成交查不到（分页/读失败）但离场回执已落库 → 用回执均价关账（订单证据）。"""
        tid = mr_entry(self.db, qty=10.0, entry=100.0)
        info = Info(szi=0.0, mid=110.0, fills=[])
        leg = make_leg(self.db, info)
        shadow = leg._shadow_begin(M.MR_PURPOSE_EXIT, side=-1, sz=10.0,
                                   px=109.7, reduce_only=True, cloid="exit-cloid-1",
                                   signal_date="2026-11-05", now_ms=NOW - 9 * 3600_000)
        leg._shadow_finish(shadow, status="filled", oid="o-exit", filled_sz=10.0,
                           avg_px=109.7)
        exit_intent(self.db, ts=NOW / 1000.0 - 8 * 3600.0)
        status = {}
        self.assertTrue(leg._adopt_exit(self.db.mr_open_trade(), NOW, status))
        row = row_of(self.db, tid)
        self.assertEqual(row["state"], "CLOSED")
        self.assertAlmostEqual(row["exit_price"], 109.7, places=9)
        raw = raw_of(self.db, tid)
        self.assertEqual(raw["price_source"], "order_receipt")
        self.assertFalse(raw["estimated"])
        self.assertTrue(raw["net_pnl_complete"])
        self.assertEqual(raw["order_oid"], "o-exit")
        self.assertEqual(leg.exchange.calls, [])

    def test_stale_intent_with_partial_order_receipt_flags_gap(self):
        """回执只覆盖 6/10 → 缺口按 mark 估算但净利标未完成（不许假确定）。"""
        tid = mr_entry(self.db, qty=10.0, entry=100.0)
        info = Info(szi=0.0, mid=110.0, fills=[])
        leg = make_leg(self.db, info)
        shadow = leg._shadow_begin(M.MR_PURPOSE_EXIT, side=-1, sz=10.0,
                                   px=109.7, reduce_only=True, cloid="exit-cloid-2",
                                   signal_date="2026-11-05", now_ms=NOW - 9 * 3600_000)
        leg._shadow_finish(shadow, status="filled", oid="o-exit", filled_sz=6.0,
                           avg_px=109.7)
        exit_intent(self.db, ts=NOW / 1000.0 - 8 * 3600.0)
        status = {}
        self.assertTrue(leg._adopt_exit(self.db.mr_open_trade(), NOW, status))
        raw = raw_of(self.db, tid)
        self.assertAlmostEqual(raw["settled_qty"], 6.0, places=9)
        self.assertAlmostEqual(raw["gap_qty"], 4.0, places=9)
        self.assertFalse(raw["net_pnl_complete"])
        self.assertTrue(raw["estimated"])
        self.assertEqual(status["action_required"]["kind"], "exit_adopt_partial_order")

    def test_no_intent_is_not_adopted(self):
        tid = mr_entry(self.db, qty=10.0, entry=100.0)
        leg = make_leg(self.db, Info(szi=0.0))
        status = {}
        self.assertFalse(leg._adopt_exit(self.db.mr_open_trade(), NOW, status))
        self.assertEqual(row_of(self.db, tid)["state"], "OPEN")
        self.assertEqual(status["action_required"]["kind"], "exit_adopt_no_intent")
        self.assertEqual(leg.exchange.calls, [])


class TestFreshExitEstimateIsLabelled(MrBase):
    """6h 内无成交证据仍按旧口径关账，但必须标成估算且净利未完成。"""

    def test_mark_estimate_is_not_reported_as_certain(self):
        tid = mr_entry(self.db, qty=10.0, entry=100.0)
        exit_intent(self.db, ts=NOW / 1000.0 - 1800.0)          # 30 分钟前
        leg = make_leg(self.db, Info(szi=0.0, mid=110.0, fills=[]))
        status = {}
        self.assertTrue(leg._adopt_exit(self.db.mr_open_trade(), NOW, status))
        row = row_of(self.db, tid)
        self.assertEqual(row["state"], "CLOSED")
        raw = raw_of(self.db, tid)
        self.assertEqual(raw["price_source"], "all_mids")
        self.assertTrue(raw["estimated"])
        self.assertFalse(raw["net_pnl_complete"],
                         "按参考价估算出来的净利不是「确定已实现」")
        self.assertFalse(status["net_pnl_complete"])
        self.assertEqual(status["action_required"]["kind"], "exit_adopt_mark_estimate")


class TestStaleEntryRecovery(MrBase):
    """入场意图超 6h：有成交证据才认领，否则明确 action_required（不自动「吃」仓）。"""

    def test_stale_intent_with_matching_fills_adopts(self):
        ts = NOW / 1000.0
        entry_intent(self.db, ts=ts - 8 * 3600.0, sz=2.0)
        info = Info(szi=2.0, entry=100.5, fills=[
            fill(t=(ts - 8 * 3600.0 + 5000) * 1000, px=100.4, sz=2.0,
                 side="buy", reduce_only=0, dir_="Open Long")])
        leg = make_leg(self.db, info)
        status = {}
        self.assertTrue(leg._adopt_entry({"szi": 2.0, "entry_px": 100.5}, NOW, status))
        row = self.db.mr_open_trade()
        self.assertIsNotNone(row)
        self.assertEqual(row["signal_date"], "2026-11-12")
        raw = json.loads(row["raw"])
        self.assertEqual(raw["fill_source"], "exchange_fills")
        self.assertEqual(raw["fill_ids"], [info.fills[0]["tid"]])
        self.assertEqual(status["adopted"], "entry")
        self.assertEqual(leg.exchange.calls, [])

    def test_stale_intent_without_fills_is_action_required(self):
        ts = NOW / 1000.0
        entry_intent(self.db, ts=ts - 8 * 3600.0, sz=2.0)
        info = Info(szi=2.0, entry=100.5, fills_api="none")
        leg = make_leg(self.db, info)
        status = {}
        self.assertFalse(leg._adopt_entry({"szi": 2.0, "entry_px": 100.5}, NOW, status))
        self.assertIsNone(self.db.mr_open_trade())
        self.assertEqual(status["action_required"]["kind"], "entry_adopt_stale")
        self.assertIn("成交证据", status["action_required"]["reason"])
        self.assertEqual(status["recon"], "err")
        self.assertEqual(leg.exchange.calls, [])

    def test_fresh_intent_still_adopts_without_fills(self):
        """6h 窗口内的原口径不变（窗口只保留给入场侧取 signal_date）。"""
        entry_intent(self.db, ts=NOW / 1000.0 - 3600.0, sz=2.0)
        info = Info(szi=2.0, entry=100.5, fills_api="none")
        leg = make_leg(self.db, info)
        status = {}
        self.assertTrue(leg._adopt_entry({"szi": 2.0, "entry_px": 100.5}, NOW, status))
        self.assertIsNotNone(self.db.mr_open_trade())


# ============================================================ B) B6 片段核对

class TestSettlementGap(MrBase):
    """正常收尾必须核对「台账 qty − Σ片段」与实际成交，缺口不静默。"""

    def test_gap_is_reconciled_and_flagged(self):
        tid = mr_entry(self.db, qty=100.0, entry=100.0, entry_ts=NOW / 1000.0 - 86400)
        info = Info(szi=60.0, mid=110.0, fills_api="none")

        def _exit(coin, is_buy, sz, limit_px, **kw):
            info.szi = 0.0
            return {"ok": True, "status": "filled", "oid": "o-e", "avg_px": 109.8,
                    "filled_sz": 60.0, "error": "", "raw": {}}

        leg = make_leg(self.db, info, Ex(info, exit_=_exit))
        status = {"enabled": True, "kill": False}
        out = leg._maybe_exit(NOW, EXIT_SESSIONS, [{"c": "110"}],
                              self.db.mr_open_trade(),
                              {"szi": 60.0, "entry_px": 100.0, "unrealized_pnl": 0.0,
                               "margin_used": 10.0, "position_value": 6600.0}, status)
        self.assertEqual(out["state"], "exited")
        row = row_of(self.db, tid)
        raw = raw_of(self.db, tid)
        self.assertFalse(raw["net_pnl_complete"])
        self.assertTrue(raw["estimated"])
        self.assertEqual(raw["reconciliation"]["gap_qty"], 40.0)
        self.assertEqual(raw["reconciliation"]["ledger_qty"], 100.0)
        self.assertEqual(raw["settled_qty"], 60.0)
        self.assertEqual(out["action_required"]["kind"], "exit_settle_gap")
        # 缺口没有静默丢：按 mark 估算入账，但标为未完成
        expect = (109.8 - 100.0) * 60.0 + (110.0 - 100.0) * 40.0
        self.assertAlmostEqual(raw["net_pnl"], expect, places=6)
        self.assertAlmostEqual(row["net_pnl"], expect, places=6)

    def test_no_gap_when_partials_are_recorded(self):
        tid = mr_entry(self.db, qty=100.0, entry=100.0, entry_ts=NOW / 1000.0 - 86400)
        self.db.mr_trade_append_partial(tid, qty=40.0, price=109.8,
                                        ts=NOW / 1000.0 - 3600, price_source="avg_px")
        info = Info(szi=60.0, mid=110.0, fills_api="none")

        def _exit(coin, is_buy, sz, limit_px, **kw):
            info.szi = 0.0
            return {"ok": True, "status": "filled", "oid": "o-e", "avg_px": 109.9,
                    "filled_sz": 60.0, "error": "", "raw": {}}

        leg = make_leg(self.db, info, Ex(info, exit_=_exit))
        status = {"enabled": True, "kill": False}
        leg._maybe_exit(NOW, EXIT_SESSIONS, [{"c": "110"}], self.db.mr_open_trade(),
                        {"szi": 60.0, "entry_px": 100.0, "unrealized_pnl": 0.0,
                         "margin_used": 10.0, "position_value": 6600.0}, status)
        raw = raw_of(self.db, tid)
        self.assertTrue(raw["net_pnl_complete"])
        self.assertNotIn("reconciliation", raw)
        self.assertAlmostEqual(raw["net_pnl"], 40 * 9.8 + 60 * 9.9, places=6)
        # raw 合并写回：入场时写的 cloid/ref_price 还在（不是整体覆盖）
        self.assertEqual(raw["cloid"], "entry-cloid")
        self.assertAlmostEqual(raw["ref_price"], 100.0, places=6)
        self.assertAlmostEqual(raw["partial_pnl"], 392.0, places=6)
        self.assertEqual(len(raw["partial_exits"]), 1)


class TestFragmentPriceSource(MrBase):
    """片段价：回执 avg_px 优先，回落到限价/参考价时**必须标来源**。"""

    def _partial_exit(self, *, avg_px):
        tid = mr_entry(self.db, qty=100.0, entry=100.0, entry_ts=NOW / 1000.0 - 86400)
        info = Info(szi=100.0, mid=110.0, fills_api="none")

        def _exit(coin, is_buy, sz, limit_px, **kw):
            info.szi = 60.0                     # 只成交 40
            res = {"ok": True, "status": "filled", "oid": "o-e", "filled_sz": 40.0,
                   "error": "", "raw": {}}
            if avg_px is not None:
                res["avg_px"] = avg_px
            return res

        leg = make_leg(self.db, info, Ex(info, exit_=_exit))
        status = {"enabled": True, "kill": False}
        out = leg._maybe_exit(NOW, EXIT_SESSIONS, [{"c": "110"}],
                              self.db.mr_open_trade(),
                              {"szi": 100.0, "entry_px": 100.0, "unrealized_pnl": 0.0,
                               "margin_used": 10.0, "position_value": 11000.0}, status)
        self.assertEqual(out["state"], "exit_not_filled")
        parts = raw_of(self.db, tid)["partial_exits"]
        self.assertEqual(len(parts), 1)
        self.assertAlmostEqual(parts[0]["qty"], 40.0, places=9)
        return parts[0], leg.exchange.calls[0]["px"]

    def test_uses_receipt_avg_px(self):
        part, limit_px = self._partial_exit(avg_px=109.8)
        self.assertAlmostEqual(part["price"], 109.8, places=9)
        self.assertEqual(part["price_source"], "avg_px")
        # 卖 IOC 的限价一定 ≤ 真实成交价：片段价不能再用下单前的 mark(110)
        self.assertLessEqual(part["price"], 110.0)
        self.assertAlmostEqual(limit_px, 109.8, places=6)
        self.assertTrue(part.get("cloid"), "片段要留可追溯的 cloid")

    def test_falls_back_to_limit_price_with_source(self):
        part, limit_px = self._partial_exit(avg_px=None)
        self.assertAlmostEqual(part["price"], limit_px, places=6)
        self.assertEqual(part["price_source"], "limit_px")


class TestFragmentMetadataThroughDbApi(MrBase):
    """片段来源/单号由 `db.mr_trade_append_partial()` 原子落库（不再直写 db.conn）。"""

    class _NoConnDb:
        """只暴露公开方法、**不暴露** `.conn` 的 DB 外壳。

        旧实现里 `_append_fragment()` 先调 db 方法、再拿 `db.conn` 补写一次 UPDATE；
        这层外壳让「偷偷用内部连接」直接 AttributeError。
        """

        def __init__(self, db):
            self.mr_trade_append_partial = db.mr_trade_append_partial
            self.hl_log_event = db.hl_log_event

    def test_db_api_stores_source_oid_cloid(self):
        tid = mr_entry(self.db, qty=10.0, entry=100.0)
        self.db.mr_trade_append_partial(tid, qty=2.5, price=109.5, ts=1.0,
                                        price_source="avg_px", oid="o-1", cloid="c-1")
        part = raw_of(self.db, tid)["partial_exits"][0]
        self.assertEqual(part["price_source"], "avg_px")
        self.assertEqual(part["oid"], "o-1")
        self.assertEqual(part["cloid"], "c-1")
        self.assertAlmostEqual(part["qty"], 2.5, places=9)

    def test_three_key_call_stays_backwards_compatible(self):
        """老调用（只有 qty/price/ts）不写来源键，但数据必须落库。"""
        tid = mr_entry(self.db, qty=10.0, entry=100.0)
        self.db.mr_trade_append_partial(tid, qty=1.0, price=101.0, ts=2.0)
        part = raw_of(self.db, tid)["partial_exits"][0]
        self.assertNotIn("price_source", part)
        self.assertNotIn("oid", part)
        self.assertAlmostEqual(part["price"], 101.0, places=9)

    def test_two_fragments_keep_both_sources(self):
        """并发/连续追加不许互相覆盖来源（旧实现是「读-改-写跨出锁」）。"""
        tid = mr_entry(self.db, qty=10.0, entry=100.0)
        self.db.mr_trade_append_partial(tid, qty=1.0, price=101.0, ts=1.0,
                                        price_source="avg_px", cloid="c-a")
        self.db.mr_trade_append_partial(tid, qty=2.0, price=102.0, ts=2.0,
                                        price_source="limit_px", cloid="c-b")
        parts = raw_of(self.db, tid)["partial_exits"]
        self.assertEqual([p["price_source"] for p in parts], ["avg_px", "limit_px"])
        self.assertEqual([p["cloid"] for p in parts], ["c-a", "c-b"])

    def test_append_fragment_needs_no_internal_conn(self):
        tid = mr_entry(self.db, qty=10.0, entry=100.0)
        leg = make_leg(self._NoConnDb(self.db), Info(szi=10.0))
        leg._append_fragment(tid, qty=1.5, price=100.5, ts=3.0,
                             price_source="limit_px", oid=7, cloid="c-9")
        part = raw_of(self.db, tid)["partial_exits"][-1]
        self.assertAlmostEqual(part["qty"], 1.5, places=9)
        self.assertEqual(part["price_source"], "limit_px")
        self.assertEqual(part["oid"], "7")          # Kraken 单号是 UUID，一律 str
        self.assertEqual(part["cloid"], "c-9")


class TestUnknownEntryAcrossRestart(MrBase):
    """进程重启（新 leg 实例）+ 结果未知的入场尝试：只认交易所事实，绝不重发。"""

    def _enter(self, leg, info, now=NOW):
        return leg._maybe_enter(now, ENTRY_SESSIONS, [{"c": "55"}],
                                {"equity_total": 1000.0, "mr_margin_used": 0.0},
                                {"enabled": True, "kill": False})

    def test_stuck_row_is_visible_beyond_the_recent_orders_window(self):
        """260 行主腿订单之后，MR 的未知尝试仍必须被认出来（窗口无 LIMIT）。"""
        leg = make_leg(self.db, Info(szi=0.0))
        leg._shadow_begin(M.MR_PURPOSE_ENTRY, side=1, sz=1.0, px=100.0,
                          reduce_only=False, cloid="c-stuck",
                          signal_date="2026-11-12", now_ms=NOW - 20 * 3600_000)
        for i in range(260):                     # 主腿每根 bar 的行，塞满 200 行窗口
            self.db.hl_record_intent("BTC_USDT", "BTC", 1_000_000 + i, "entry", 1, 1.0,
                                     status="filled")
        recent = self.db.hl_recent_orders(200)
        self.assertEqual(len(recent), 200)
        self.assertFalse([r for r in recent
                          if r["purpose"] == M.MR_PURPOSE_ENTRY
                          and r["status"] in (M.ST_INTENT, M.ST_SENT)],
                         "前提：未知尝试已被挤出最近 200 行窗口")
        found = leg._mr_attempts(M.MR_PURPOSE_ENTRY, "2026-11-12",
                                 (M.ST_INTENT, M.ST_SENT))
        self.assertEqual(len(found), 1, "未知尝试不能被最近 200 行挤出查询窗口")

    def test_restart_with_filled_attempt_adopts_instead_of_resending(self):
        info = Info(szi=0.0, account_raises=True)

        def _lost(coin, is_buy, sz, limit_px, **kw):
            info.szi = sz                        # 交易所真的成交了，但回执丢了
            return {"ok": False, "status": "error", "error": "read timeout", "raw": {}}

        ex = Ex(info, entry=_lost)
        out = self._enter(make_leg(self.db, info, ex), info)
        self.assertEqual(out["state"], "action_required")
        self.assertIsNone(self.db.mr_open_trade())
        self.assertEqual(len(ex.calls), 1)

        # 「重启」：新实例（内存状态全丢）+ 账户这次读得到，且仓在
        info.account_raises = False
        leg2 = make_leg(self.db, info, ex)
        out2 = self._enter(leg2, info, now=NOW + 300_000)
        self.assertEqual(len(ex.calls), 1, "重启后按持仓补记，不许发第二张")
        self.assertEqual(out2["state"], "entered")
        self.assertEqual(out2.get("adopted"), "entry")
        row = self.db.mr_open_trade()
        self.assertIsNotNone(row)
        self.assertAlmostEqual(row["qty"], info.szi, places=9)

    def test_restart_with_flat_account_allows_exactly_one_retry(self):
        info = Info(szi=0.0, account_raises=True)

        def _lost(coin, is_buy, sz, limit_px, **kw):
            return {"ok": False, "status": "error", "error": "read timeout", "raw": {}}

        ex = Ex(info, entry=_lost)
        self._enter(make_leg(self.db, info, ex), info)
        self.assertEqual(len(ex.calls), 1)

        # 重启 + 账户读得到且**确认无仓** → IOC 早已过期，允许**一次**重试
        info.account_raises = False
        ex.entry = ex._fill_entry
        leg2 = make_leg(self.db, info, ex)
        out2 = self._enter(leg2, info, now=NOW + 300_000)
        self.assertEqual(len(ex.calls), 2, "确认无仓后才允许重试")
        self.assertEqual(out2["state"], "entered")
        # 旧的未知尝试已收口（不能留在 intent/sent 上继续挡门/报卡单）
        self.assertEqual(self.db.hl_stuck_orders(-1.0), [])
        # 台账已在 → 后续 tick 幂等，不再下单
        leg3 = make_leg(self.db, info, ex)
        self._enter(leg3, info, now=NOW + 600_000)
        self.assertEqual(len(ex.calls), 2)


# ============================================================ D) 风控覆盖

class TestOrderGateAndShadowRows(MrBase):
    def _enter(self, info=None, ex=None, status=None, now=NOW):
        info = info or Info(szi=0.0)
        ex = ex or Ex(info)
        leg = make_leg(self.db, info, ex)
        st = status if status is not None else {"enabled": True, "kill": False}
        out = leg._maybe_enter(now, ENTRY_SESSIONS, [{"c": "55"}],
                               {"equity_total": 1000.0, "mr_margin_used": 0.0}, st)
        return leg, ex, out

    def test_entry_writes_shadow_row(self):
        leg, ex, out = self._enter()
        self.assertEqual(out["state"], "entered", out)
        rows = [r for r in self.db.hl_recent_orders(10) if r["purpose"] == "mr_entry"]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["status"], "filled")
        self.assertEqual(row["coin"], "QQQ")
        self.assertEqual(json.loads(row["raw"])["signal_date"], "2026-11-12")
        self.assertGreater(row["filled_sz"], 0)
        self.assertEqual(len(ex.calls), 1)

    def test_daily_order_cap_blocks_entry(self):
        os.environ["HL_MAX_ORDERS_PER_DAY"] = "1"
        rid = self.db.hl_record_intent("QQQ", "QQQ", 1, "mr_entry", 1, 1.0,
                                       status="filled")
        self.db.conn.execute("UPDATE hl_orders SET created_at=? WHERE id=?",
                             (NOW / 1000.0, rid))
        self.db.conn.commit()
        leg, ex, out = self._enter()
        self.assertEqual(out["state"], "order_gate_blocked")
        self.assertIn("当日下单", out["error"])
        self.assertEqual(ex.calls, [], "被闸门拦下时不许发单")
        self.assertIsNone(self.db.mr_open_trade())
        # 也不许把当天唯一信号写成 SKIPPED（刹车会自解，窗口内还有后续 tick）
        self.assertIsNone(self.db.mr_trade_by_signal("2026-11-12"))

    def test_fail_streak_blocks_entry_but_not_exit(self):
        os.environ["HL_MAX_FAIL_STREAK"] = "2"
        now_s = NOW / 1000.0
        for i in range(2):
            rid = self.db.hl_record_intent("QQQ", "QQQ", 10 + i, "mr_exit", -1, 1.0,
                                           status="failed")
            self.db.conn.execute("UPDATE hl_orders SET created_at=? WHERE id=?",
                                 (now_s - 60 * (i + 1), rid))
        self.db.conn.commit()
        leg, ex, out = self._enter()
        self.assertEqual(out["state"], "order_gate_blocked")
        self.assertIn("失败", out["error"])
        self.assertEqual(ex.calls, [])

        # 离场不受这两道闸门限制：有仓 + 离场信号 → 照常发单
        tid = mr_entry(self.db, qty=10.0, entry=100.0, entry_ts=now_s - 86400)
        info = Info(szi=10.0)
        ex2 = Ex(info)
        leg2 = make_leg(self.db, info, ex2)
        st = {"enabled": True, "kill": False}
        out2 = leg2._maybe_exit(NOW, EXIT_SESSIONS, [{"c": "110"}],
                                self.db.mr_open_trade(),
                                {"szi": 10.0, "entry_px": 100.0, "unrealized_pnl": 0.0,
                                 "margin_used": 10.0, "position_value": 1100.0}, st)
        self.assertEqual(out2["state"], "exited")
        self.assertEqual(len(ex2.calls), 1)
        self.assertEqual(row_of(self.db, tid)["state"], "CLOSED")


# ==================================================== E) IOC 未知结果不重发

class TestIocUnknownNoResend(MrBase):
    def _enter(self, info, ex, now=NOW):
        leg = make_leg(self.db, info, ex)
        st = {"enabled": True, "kill": False}
        out = leg._maybe_enter(now, ENTRY_SESSIONS, [{"c": "55"}],
                               {"equity_total": 1000.0, "mr_margin_used": 0.0}, st)
        return leg, out

    def test_entry_unknown_blocks_resend_until_evidence(self):
        info = Info(szi=0.0, account_raises=True)

        def _lost(coin, is_buy, sz, limit_px, **kw):
            if info.account_raises:
                # 账户读不到 = 回执/持仓都未知
                return {"ok": False, "status": "error", "error": "read timeout",
                        "raw": {}, "filled_sz": None, "avg_px": None}
            info.szi += sz
            return {"ok": True, "status": "filled", "oid": "o", "avg_px": limit_px,
                    "filled_sz": sz, "error": "", "raw": {}}

        ex = Ex(info, entry=_lost)
        leg, out = self._enter(info, ex)
        self.assertEqual(out["state"], "action_required")
        self.assertEqual(out["action_required"]["kind"], "entry_result_unknown")
        self.assertEqual(len(ex.calls), 1)
        pending = [r for r in self.db.hl_recent_orders(10)
                   if r["purpose"] == "mr_entry" and r["status"] == "sent"]
        self.assertEqual(len(pending), 1, "结果未知必须留 sent 影子行")
        self.assertIsNone(self.db.mr_open_trade())

        # 账户仍读不到 → 再跑一轮也不许重发
        leg2, out2 = self._enter(info, ex, now=NOW + 300_000)
        self.assertEqual(out2["state"], "action_required")
        self.assertEqual(len(ex.calls), 1, "读不到账户时禁止无证重发")

        # 读得到账户且确认无仓 → 上一张 IOC 已过期，允许重试
        info.account_raises = False
        info.entry = 100.0
        leg3, out3 = self._enter(info, ex, now=NOW + 600_000)
        self.assertEqual(len(ex.calls), 2, "证据确认无仓后才允许重发")
        self.assertEqual(out3["state"], "entered", out3)
        self.assertIsNotNone(self.db.mr_open_trade())

    def test_entry_receipt_lost_but_filled_is_adopted_once(self):
        info = Info(szi=0.0)

        def _lost_but_filled(coin, is_buy, sz, limit_px, **kw):
            info.szi = sz                       # 交易所真的成交了
            return {"ok": False, "status": "error", "error": "read timeout",
                    "raw": {}, "filled_sz": None, "avg_px": None}

        ex = Ex(info, entry=_lost_but_filled)
        leg, out = self._enter(info, ex)
        self.assertEqual(out["state"], "entered")
        self.assertEqual(out.get("adopted"), "entry")
        self.assertEqual(len(ex.calls), 1, "已成交就不许再发第二张")
        row = self.db.mr_open_trade()
        self.assertIsNotNone(row)
        self.assertGreater(row["qty"], 0)

    def test_exit_unknown_does_not_resend(self):
        mr_entry(self.db, qty=10.0, entry=100.0, entry_ts=NOW / 1000.0 - 86400)
        info = Info(szi=10.0, fills_api="none", account_raises=False)

        def _lost(coin, is_buy, sz, limit_px, **kw):
            info.account_raises = True          # 回执丢失 + 持仓读失败
            return {"ok": False, "status": "error", "error": "read timeout",
                    "raw": {}}

        ex = Ex(info, exit_=_lost)
        leg = make_leg(self.db, info, ex)
        st = {"enabled": True, "kill": False}
        pos = {"szi": 10.0, "entry_px": 100.0, "unrealized_pnl": 0.0,
               "margin_used": 10.0, "position_value": 1100.0}
        out = leg._maybe_exit(NOW, EXIT_SESSIONS, [{"c": "110"}],
                              self.db.mr_open_trade(), pos, st)
        self.assertEqual(out["state"], "action_required")
        self.assertEqual(len(ex.calls), 1)

        # 下一轮：pending sent + 成交查询不可用 → 不许重发
        info.account_raises = False
        out2 = leg._maybe_exit(NOW + 300_000, EXIT_SESSIONS, [{"c": "110"}],
                               self.db.mr_open_trade(), pos, st)
        self.assertEqual(out2["state"], "action_required")
        self.assertEqual(len(ex.calls), 1, "成交证据不可用时不许凭旧状态重发")

    def test_exit_unknown_block_expires_so_the_position_is_never_abandoned(self):
        """成交查询长期不可用：阻塞有上限，超时后允许下一张（否则仓位永久裸奔）。

        这里钉的是「阻塞比无证重发更危险」：IOC 几秒就定局，30 分钟后还查不到成交
        说明取证不可用；继续不发单 = MR（不挂止损）的仓位无限期没有离场。
        """
        mr_entry(self.db, qty=10.0, entry=100.0, entry_ts=NOW / 1000.0 - 86400)
        info = Info(szi=10.0, fills_api="none")

        def _lost(coin, is_buy, sz, limit_px, **kw):
            info.account_raises = True          # 回执丢失 + 持仓读失败
            return {"ok": False, "status": "error", "error": "read timeout", "raw": {}}

        ex = Ex(info, exit_=_lost)
        leg = make_leg(self.db, info, ex)
        st = {"enabled": True, "kill": False}
        pos = {"szi": 10.0, "entry_px": 100.0, "unrealized_pnl": 0.0,
               "margin_used": 10.0, "position_value": 1100.0}
        leg._maybe_exit(NOW, EXIT_SESSIONS, [{"c": "110"}], self.db.mr_open_trade(),
                        pos, st)
        self.assertEqual(len(ex.calls), 1)

        # 30 分钟内：仍在飞 → 不重发
        info.account_raises = False
        out2 = leg._maybe_exit(NOW + 10 * 60_000, EXIT_SESSIONS, [{"c": "110"}],
                               self.db.mr_open_trade(), pos, st)
        self.assertEqual(len(ex.calls), 1)
        self.assertEqual(out2["action_required"]["kind"], "exit_result_unknown")

        # 超过 30 分钟：旧尝试收口成 failed → 允许下一张离场（reduce-only，零新敞口）
        ex.exit = ex._fill_exit
        out3 = leg._maybe_exit(NOW + 31 * 60_000, EXIT_SESSIONS, [{"c": "110"}],
                               self.db.mr_open_trade(), pos, st)
        self.assertEqual(len(ex.calls), 2)
        self.assertEqual(out3["state"], "exited")
        self.assertEqual(self.db.hl_stuck_orders(-1.0), [])
        self.assertTrue(any(r["kind"] == "mr_exit_unknown_expired"
                            for r in self.db.hl_recent_events(10)))


# ==================================================== F) 到期清仓（Kraken 形状）

class KrInfo:
    """Kraken 形状的 info：snapshot 含全账户，user_state_dex 返回**同一个**账户。"""

    venue_address_free = True

    def __init__(self, positions, *, mid=100.0):
        self.positions = [dict(p) for p in positions]
        self.mid = mid
        self.snapshot_calls = 0
        self.dex_calls = 0

    def snapshot(self, address, mids=None):
        self.snapshot_calls += 1
        return {"positions": [dict(p) for p in self.positions], "open_orders": []}

    def user_state_dex(self, address, dex):
        self.dex_calls += 1
        return {"assetPositions": [{"position": {"coin": p["coin"], "szi": p["side"] * p["size"],
                                                "entryPx": p.get("entry_px", p["mark_px"]),
                                                "positionValue": p["size"] * p["mark_px"],
                                                "unrealizedPnl": "0"}}
                                   for p in self.positions],
                "marginSummary": {"marginUsed": "0"}}

    def specs(self, coins=None):
        return {p["coin"]: {"sz_decimals": 4, "price_tick": 0.1,
                            "szDecimals": 4} for p in self.positions}

    def perp_meta_dex(self, dex):
        return []

    def round_price(self, coin, px):
        return round(float(px) * 10) / 10.0

    def price_tick(self, coin):
        return 0.1

    def close(self, coin):
        self.positions = [p for p in self.positions if p["coin"] != coin]


class KrEx:
    def __init__(self, info, *, leave=None):
        self.info = info
        self.leave = set(leave or ())     # 这些 coin 第一次「成交了但交易所仍有仓」
        self.calls = []

    def cancel_all_orders(self):
        return {"ok": True, "status": "resting", "error": None,
                "raw": {"oids": [], "failed": []}}

    def mid(self, coin):
        return 94.0 if "QQQ" in coin.upper() else 83000.0

    def slippage_limit(self, coin, is_buy, mid):
        return self.mid(coin)

    def place_order(self, coin, is_buy, sz, px, reduce_only=False, tif="Ioc",
                    trigger_px=None, cloid=None):
        self.calls.append({"coin": coin, "is_buy": is_buy, "sz": sz, "px": px})
        if coin in self.leave:
            self.leave.discard(coin)      # 只让第一轮平不掉（模拟部分成交/拒单）
        else:
            self.info.close(coin)
        return {"ok": True, "status": "filled", "oid": f"o-{coin}", "avg_px": px,
                "filled_sz": sz, "error": None, "raw": {}}


def kr_pos(coin, size, side=1, mark=83000.0):
    return {"coin": coin, "size": size, "side": side, "mark_px": mark,
            "entry_px": mark}


class TestFlattenKrakenShape(MrBase):
    def test_each_position_is_closed_once(self):
        """snapshot 与 user_state_dex 同源 → 不许把每个仓平两次。"""
        info = KrInfo([kr_pos("PF_XBTUSD", 0.01, 1, 83000.0),
                       kr_pos("PF_QQQXUSD", 2.5, 1, 94.0)])
        ex = KrEx(info)
        params = ExecParams(mode="live", account_address="kraken", private_key="x",
                            code_rev="t")
        res = A.flatten_all(self.db, params, info, ex, None)
        self.assertTrue(res["ok"], res)
        orders = [c["coin"] for c in ex.calls]
        self.assertEqual(sorted(orders), ["PF_QQQXUSD", "PF_XBTUSD"])
        self.assertEqual(len(res["closed"]), 2)

    def test_mr_ledger_uses_fill_price_config_fee_and_keeps_raw(self):
        os.environ["KR_TAKER_FEE"] = "0.0005"
        info = KrInfo([kr_pos("PF_QQQXUSD", 2.5, 1, 94.0)])
        ex = KrEx(info)
        params = ExecParams(mode="live", account_address="kraken", private_key="x",
                            code_rev="t")
        tid = self.db.mr_record_entry(
            signal_date="2026-11-05", entry_session="2026-11-06", qty=2.5,
            qty_int=25000, entry_price=93.0, entry_order_id="e1",
            entry_ts=NOW / 1000.0 - 86400, notional=232.5, margin=232.5,
            equity_at_entry=1000.0,
            raw={"ref_price": 93.0, "cloid": "mr-entry-cloid",
                 "partial_exits": [{"qty": 0.5, "price": 94.5, "ts": 1.0}]})
        res = A.flatten_all(self.db, params, info, ex, None)
        self.assertTrue(res["ok"], res)
        row = row_of(self.db, tid)
        self.assertEqual(row["state"], "CLOSED")
        self.assertEqual(row["exit_reason"], A.EXIT_REASON_EXPIRY)
        # 价格来自真实成交（KrEx.mid 94），不是写死 HL 名取不到后回落入场价 93
        self.assertAlmostEqual(row["exit_price"], 94.0, places=9)
        # 费率走配置（0.0005），不是 HL 往返常数 0.0009
        covered = 2.5 - 0.5
        fee = 0.0005 * (232.5 + covered * 94.0 + 0.5 * 94.5)
        self.assertAlmostEqual(row["fee_quote"], fee, places=9)
        self.assertAlmostEqual(row["net_pnl"],
                               (94.5 - 93.0) * 0.5 + (94.0 - 93.0) * covered - fee,
                               places=9)
        raw = raw_of(self.db, tid)
        self.assertEqual(raw["price_source"], "avg_px")
        self.assertFalse(raw["estimated"])
        # raw 合并：入场时写的字段与片段都还在（旧实现整体覆盖 → 片段丢失）
        self.assertEqual(raw["cloid"], "mr-entry-cloid")
        self.assertEqual(len(raw["partial_exits"]), 1)
        self.assertAlmostEqual(raw["partial_pnl"], 0.75, places=9)

    def test_mr_ledger_price_source_is_labelled_when_receipt_lacks_average(self):
        """回执没有成交均价 → 用限价关账，但来源必须标成 limit_px（估算）。"""
        info = KrInfo([kr_pos("PF_QQQXUSD", 2.5, 1, 94.0)])
        ex = KrEx(info)
        params = ExecParams(mode="live", account_address="kraken", private_key="x",
                            code_rev="t")
        tid = self.db.mr_record_entry(
            signal_date="2026-11-05", entry_session="2026-11-06", qty=2.5,
            qty_int=25000, entry_price=93.0, entry_order_id="e1",
            entry_ts=NOW / 1000.0 - 86400, notional=232.5, margin=232.5,
            equity_at_entry=1000.0)

        def _no_price(coin, is_buy, sz, px, **kw):
            info.close(coin)                     # 平掉了，但回执没有均价
            return {"ok": True, "status": "filled", "oid": "o", "avg_px": None,
                    "filled_sz": sz, "error": None, "raw": {}}

        ex.place_order = _no_price
        res = A.flatten_all(self.db, params, info, ex, None)
        self.assertTrue(res["ok"], res)
        raw = raw_of(self.db, tid)
        self.assertEqual(raw["price_source"], "limit_px")
        self.assertTrue(raw["estimated"])
        self.assertTrue(raw["net_pnl_complete"])
        # 限价（=venue 的滑点限价 94）不是「确定的成交价」：标了估算就算如实
        self.assertAlmostEqual(row_of(self.db, tid)["exit_price"], 94.0, places=9)

    def test_closed_accumulates_across_retry_rounds(self):
        info = KrInfo([kr_pos("PF_XBTUSD", 0.01, 1, 83000.0),
                       kr_pos("PF_ETHUSD", 0.5, 1, 2500.0)])
        ex = KrEx(info, leave={"PF_ETHUSD"})     # 第一轮 ETH 平不掉
        params = ExecParams(mode="live", account_address="kraken", private_key="x",
                            code_rev="t")
        res = A.flatten_all(self.db, params, info, ex, None, retries=1)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["attempts"], 2)
        coins = sorted(c["coin"] for c in res["closed"])
        self.assertEqual(coins, ["PF_ETHUSD", "PF_XBTUSD"],
                         "重试轮不能把前一轮真平掉的记录丢掉")


class TestAgentCheckShortCircuits(unittest.TestCase):
    """check_once：无到期概念的 venue 直接短路（不读兜底、不清仓、不置 kill）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(os.path.join(self.tmp.name, "a.db"))
        # 故意留一个 HL 时代的手工兜底值（1 天后到期 → 旧逻辑会进 cleanup）
        self.db.set_config("hl_agent_valid_until_ms",
                           str(int(NOW + 86400 * 1000)))

    def tearDown(self):
        self.tmp.cleanup()

    def test_cleanup_never_runs(self):
        calls = []

        class Spot:
            venue_address_free = True

            def extra_agents(self, address):
                calls.append("extra_agents")
                raise AssertionError("Kraken 下不该读 extraAgents")

            def snapshot(self, address, mids=None):
                calls.append("snapshot")
                return {"positions": [], "open_orders": []}

        def _cleanup(reason=""):
            calls.append("cleanup")
            return {"ok": True, "closed": [{"coin": "PF_XBTUSD", "sz": 1}], "dry": False}

        state = A.check_once(self.db, SimpleNamespace(), None, Spot(),
                             address="kraken", cleanup=_cleanup, mode="live",
                             now_ms=NOW)
        self.assertEqual(state["source"], A.KRAKEN_SOURCE)
        self.assertEqual(state["level"], "no_expiry")
        self.assertEqual(state["cleanup"], "none")
        self.assertIsNone(state["valid_until_ms"])
        self.assertEqual(calls, [], "短路后不许读账户/清仓")
        self.assertFalse(self.db.hl_kill(), "短路不许置 kill")
        self.assertEqual(A.read_state(self.db)["source"], A.KRAKEN_SOURCE)

    def test_hl_shaped_info_is_not_short_circuited(self):
        """反证：HL 形状（有 extraAgents、无 venue_address_free）仍走原逻辑。"""
        class Hl:
            def extra_agents(self, address):
                return [{"name": "Hermes", "address": "0xabc", "validUntil": None}]

            def snapshot(self, address, mids=None):
                return {"positions": [], "open_orders": []}

        state = A.check_once(self.db, SimpleNamespace(), None, Hl(),
                             address="0xabc", now_ms=NOW)
        self.assertEqual(state["source"], "api")
        self.assertEqual(state["level"], "no_expiry")     # validUntil=null = 不限期


if __name__ == "__main__":
    unittest.main()
