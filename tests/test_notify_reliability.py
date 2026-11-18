"""KR 通知可靠性与文案的回归测试（全离线：假 Kraken info/exchange + 临时 SQLite）。

钉住的行为：
  * MR 告警落 `alert_logs`：发送失败 = sent_successfully=0（可被 main._retry_failed_alerts
    补推），进程重启后仍能续推；
  * action_required 的**事件去重**不得顺手把「没送达的推送」一起去掉：
    同一 (kind, signal_date) 送达后不再重发，失败则下一轮/重启后继续重试；
  * 不同事件（kind 或 signal_date 不同）不互相吞掉；
  * 成交回报（MR 入场/离场）受面板 `fill_push` 控制：关 = 只落库不推送（记成功，
    不进补推队列）；开 = 正常推送，失败进补推队列；
  * 文案：`【Kraken·主腿】` / `【Kraken·MR】` 标题、策略离场与盈亏符号无关、
    无 Markdown `**`、Kraken funding 明确「不可用」、USD 计价。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime

from app.db import DB
from app.hl.executor import fmt_entry_alert, fmt_exit_alert
from app.hl.mr import ACTION_REQUIRED_KIND, HlMrConfig, HlMrLeg

from tests.test_mr_hl import (ADDR, ENTRY_NOW_MS, EXIT_NOW_MS, FakeExchange,
                              FakeInfo)


class FakeNotifier:
    def __init__(self, ok=True, raises=False):
        self.ok = ok
        self.raises = raises
        self.messages = []

    def send_message(self, text):
        self.messages.append(text)
        if self.raises:
            raise RuntimeError("notifier boom")
        return self.ok


def _db_path(tmpdir: str) -> str:
    return f"{tmpdir}/notify_{abs(hash(tmpdir)) % 99999}.db"


def _leg(tmpdir: str, *, notifier=None, **kw):
    db = DB(_db_path(tmpdir))
    info = FakeInfo(**kw)
    ex = FakeExchange()
    ex.info = info
    leg = HlMrLeg(db=db, info=info, exchange=ex, address_fn=lambda: ADDR,
                  config=HlMrConfig(fill_confirm_s=0.0, slippage_warn_bps=1000),
                  clock=lambda: EXIT_NOW_MS / 1000.0, notifier=notifier)
    return leg, db, ex, info


def _rebuild(db: DB, tmpdir: str, *, notifier=None, **kw):
    """同一 DB 上新建一条腿 = 进程重启。"""
    info = FakeInfo(**kw)
    ex = FakeExchange()
    ex.info = info
    return HlMrLeg(db=db, info=info, exchange=ex, address_fn=lambda: ADDR,
                   config=HlMrConfig(fill_confirm_s=0.0, slippage_warn_bps=1000),
                   clock=lambda: EXIT_NOW_MS / 1000.0, notifier=notifier)


def _action_rows(db: DB):
    return [r for r in db.recent_alerts(100)
            if str(r["kind"] or "").startswith("mr_action_required")]


def _failed(db: DB, prefix: str = ""):
    return [r for r in db.failed_alerts_since(0)
            if str(r["kind"] or "").startswith(prefix)]


class TestActionRequiredDurability(unittest.TestCase):
    def test_failed_send_is_retried_then_success_suppresses_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            n = FakeNotifier(ok=False)
            leg, db, ex, info = _leg(tmp, notifier=n)
            status = {}
            leg._action_required(status, kind="exit_quantity_conflict",
                                 reason="回执与持仓变化不符", signal_date="2026-04-16")
            self.assertEqual(len(n.messages), 1)
            self.assertEqual(len(_failed(db, "mr_action_required")), 1,
                             "失败必须落成待补推行（sent_successfully=0）")
            self.assertEqual(len(db.hl_recent_events(10, kind=ACTION_REQUIRED_KIND)), 1)

            n.ok = True                    # 通道恢复 → 下一轮重试必须真的发出去
            leg._action_required(status, kind="exit_quantity_conflict",
                                 reason="回执与持仓变化不符", signal_date="2026-04-16")
            self.assertEqual(len(n.messages), 2)
            self.assertEqual(_failed(db, "mr_action_required"), [],
                             "送达成功后旧失败行必须一起了结，避免补推重复")
            self.assertEqual(len(db.hl_recent_events(10, kind=ACTION_REQUIRED_KIND)), 1,
                             "事件仍然只落一条")

            leg._action_required(status, kind="exit_quantity_conflict",
                                 reason="回执与持仓变化不符", signal_date="2026-04-16")
            self.assertEqual(len(n.messages), 2, "已送达的同一事件不得重发")

    def test_notifier_exception_is_failure_and_retries_after_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            leg1, db, ex, info = _leg(tmp, notifier=FakeNotifier(raises=True))
            leg1._action_required({}, kind="exit_fragment_uncommitted",
                                  reason="片段未能原子落库", signal_date="2026-04-15")
            self.assertEqual(len(_failed(db, "mr_action_required")), 1)

            # 进程重启：关连接、重新打开同一 DB 文件；内存去重全清，但事件行还在 ——
            # 旧实现会在这里 return，推送永远补不回来。
            db.conn.close()
            db2 = DB(_db_path(tmp))
            n = FakeNotifier(ok=True)
            leg2 = _rebuild(db2, tmp, notifier=n)
            leg2._action_required({}, kind="exit_fragment_uncommitted",
                                  reason="片段未能原子落库", signal_date="2026-04-15")
            self.assertEqual(len(n.messages), 1)
            self.assertEqual(_failed(db2, "mr_action_required"), [])
            self.assertEqual(len(db2.hl_recent_events(10, kind=ACTION_REQUIRED_KIND)), 1)

    def test_distinct_events_are_not_suppressed(self):
        with tempfile.TemporaryDirectory() as tmp:
            n = FakeNotifier(ok=True)
            leg, db, ex, info = _leg(tmp, notifier=n)
            for kind, date in (("a", "2026-04-15"), ("b", "2026-04-15"),
                               ("a", "2026-04-16")):
                leg._action_required({}, kind=kind, reason="r", signal_date=date)
            self.assertEqual(len(n.messages), 3, "不同 kind/信号日各自成事件")
            self.assertEqual(len({r["kind"] for r in _action_rows(db)}), 3,
                             "alert_logs 的 kind 必须按事件区分，不能共用一行")

    def test_message_states_action_certainty_and_next_step(self):
        with tempfile.TemporaryDirectory() as tmp:
            n = FakeNotifier(ok=True)
            leg, db, ex, info = _leg(tmp, notifier=n)
            leg._action_required({}, kind="exit_settle_gap",
                                 reason="缺口 0.5 张按 mark 估算", signal_date="2026-04-17")
            msg = n.messages[-1]
            self.assertIn("【Kraken·MR】", msg)
            self.assertIn("需人工核对", msg)
            for want in ("原因", "现状", "说明", "缺口 0.5 张", "不是成交确认"):
                self.assertIn(want, msg)
            self.assertNotIn("**", msg)


class TestFillPushSwitch(unittest.TestCase):
    def _enter(self, leg, info, db):
        info.now_ms = ENTRY_NOW_MS
        db.set_config("mr_enabled", "1")
        return leg.tick(now_ms=ENTRY_NOW_MS)

    def test_entry_fill_off_is_logged_but_not_pushed(self):
        with tempfile.TemporaryDirectory() as tmp:
            n = FakeNotifier(ok=True)
            leg, db, ex, info = _leg(tmp, notifier=n)
            db.set_config("fill_push", "0")
            self._enter(leg, info, db)
            self.assertEqual(n.messages, [], "fill_push 关 → 成交回报不推送")
            rows = [r for r in db.recent_alerts(50)
                    if str(r["kind"] or "").startswith("mr_fill_entry")]
            self.assertEqual(len(rows), 1, "仍要留审计行")
            self.assertEqual(int(rows[0]["sent_successfully"]), 1,
                             "关掉是用户意图，必须记成功，不能进补推队列")
            self.assertEqual(_failed(db, "mr_fill"), [])

    def test_entry_fill_on_and_failure_is_retryable(self):
        with tempfile.TemporaryDirectory() as tmp:
            n = FakeNotifier(ok=False)
            leg, db, ex, info = _leg(tmp, notifier=n)
            db.set_config("fill_push", "1")
            self._enter(leg, info, db)
            self.assertEqual(len(n.messages), 1)
            self.assertIn("【Kraken·MR】", n.messages[0])
            self.assertEqual(len(_failed(db, "mr_fill_entry")), 1,
                             "开关开着但没送达 → 必须可补推")

    def test_exit_fill_off_is_logged_but_not_pushed(self):
        with tempfile.TemporaryDirectory() as tmp:
            n = FakeNotifier(ok=True)
            leg, db, ex, info = _leg(tmp, notifier=n, mr_szi="2.68", mid=95.5)
            db.set_config("mr_enabled", "1")
            db.set_config("fill_push", "0")
            db.mr_record_entry(
                signal_date="2026-04-15", entry_session="2026-04-16", qty=2.68,
                qty_int=26800, entry_price=93.0, entry_order_id="100",
                entry_ts=ENTRY_NOW_MS / 1000.0, notional=2.68 * 93.0, margin=249.24,
                equity_at_entry=500.0)
            status = leg.tick(now_ms=EXIT_NOW_MS)
            self.assertEqual(status["state"], "exited")
            self.assertEqual(n.messages, [])
            rows = [r for r in db.recent_alerts(50)
                    if str(r["kind"] or "").startswith("mr_fill_exit")]
            self.assertEqual(len(rows), 1)
            self.assertEqual(int(rows[0]["sent_successfully"]), 1)

    def test_warning_is_not_gated_by_fill_push(self):
        with tempfile.TemporaryDirectory() as tmp:
            n = FakeNotifier(ok=True)
            leg, db, ex, info = _leg(tmp, notifier=n)
            db.set_config("fill_push", "0")
            leg._alert("腿自动停用：滑点超限", kind="mr_alert")
            self.assertEqual(len(n.messages), 1, "异常/风控告警不受成交开关影响")


class TestMrCloseAlertContent(unittest.TestCase):
    def _exit(self, leg, db, info, ex=None):
        db.set_config("mr_enabled", "1")
        db.mr_record_entry(
            signal_date="2026-04-15", entry_session="2026-04-16", qty=2.68,
            qty_int=26800, entry_price=93.0, entry_order_id="100",
            entry_ts=ENTRY_NOW_MS / 1000.0, notional=2.68 * 93.0, margin=249.24,
            equity_at_entry=500.0)
        return leg.tick(now_ms=EXIT_NOW_MS)

    def test_funding_unavailable_is_stated_not_zero_percent(self):
        with tempfile.TemporaryDirectory() as tmp:
            n = FakeNotifier(ok=True)
            leg, db, ex, info = _leg(tmp, notifier=n, mr_szi="2.68", mid=95.5)
            info.funding_supported = False
            status = self._exit(leg, db, info)
            self.assertEqual(status["state"], "exited")
            msg = n.messages[-1]
            self.assertIn("【Kraken·MR】", msg)
            self.assertIn("不可用", msg)
            self.assertIn("未计入", msg)
            self.assertNotIn("0.0%", msg, "不可用不能写成 funding 0.0%")
            self.assertNotIn("USDC", msg, "Kraken 侧金额用 USD")
            self.assertIn("USD", msg)

    def test_close_alert_has_qty_prices_and_hold(self):
        with tempfile.TemporaryDirectory() as tmp:
            n = FakeNotifier(ok=True)
            leg, db, ex, info = _leg(tmp, notifier=n, mr_szi="2.68", mid=95.5)
            self._exit(leg, db, info)
            msg = n.messages[-1]
            for want in ("数量", "2.68", "开仓", "93.00", "离场", "持仓",
                         "1 天 6 小时 30 分钟", "净盈亏", "费用", "信号日",
                         "成交时间", "已扣估算手续费"):
                self.assertIn(want, msg, f"离场文案缺 {want}")
            self.assertNotIn("**", msg)
            self.assertNotIn("MR: MR", msg)

    def test_estimated_exit_pnl_is_labeled(self):
        with tempfile.TemporaryDirectory() as tmp:
            n = FakeNotifier(ok=True)
            leg, db, ex, info = _leg(tmp, notifier=n, mr_szi="2.68", mid=95.5)
            real_place = ex.place_order

            def place(coin, is_buy, sz, limit_px, **kw):
                res = real_place(coin, is_buy, sz, limit_px, **kw)
                res["avg_px"] = None           # 回执无成交均价 → 按限价估算
                return res

            ex.place_order = place
            status = self._exit(leg, db, info)
            self.assertEqual(status["state"], "exited")
            self.assertFalse(status["net_pnl_complete"])
            msg = n.messages[-1]
            self.assertIn("未完成", msg)
            self.assertIn("估算", msg)
            self.assertIn("核对", msg)

    def test_partial_exit_is_not_reported_as_full_fill(self):
        with tempfile.TemporaryDirectory() as tmp:
            from app.hl.mr import _mr_exit_text
            msg = _mr_exit_text(
                coin="QQQ", filled=2.0, ledger_qty=2.68, partial_qty=0.0,
                gap_qty=0.68, entry_px=93.0, exit_px=95.0,
                entry_ts=ENTRY_NOW_MS / 1000.0, now_s=EXIT_NOW_MS / 1000.0,
                net_pnl=3.5, fee_est=0.12, complete=False,
                funding_paid=None, price_source="limit_px", signal_date="2026-04-17")
            self.assertIn("成交 2 / 台账 2.68", msg)
            self.assertIn("0.68 张按标记价估算", msg)
            self.assertNotIn("0.0%", msg)


class TestMainLegFillTitles(unittest.TestCase):
    def _entry(self):
        return fmt_entry_alert(coin="BTC", side=1, sz=0.00095, entry_px=83545.0,
                               stop=81456.0, stop_dist=0.025, notional=79.4,
                               margin=15.9, liq=67720.0, risk_u=2.0, equity=100.0,
                               signal_px=83544.5, oid=111, stop_oid=222,
                               when=datetime(2026, 10, 14, 13, 35), tz_label="北京时间")

    def _exit(self, reason, net_sign=1):
        now = datetime(2026, 10, 14, 13, 35)
        return fmt_exit_alert(coin="BTC", side=1, entry_px=83545.0,
                              exit_px=86000.0 if net_sign > 0 else 80000.0, sz=0.00095,
                              reason=reason, r_multiple=1.0 * net_sign,
                              gross_pnl=2.33 * net_sign, fees=0.07,
                              opened_at=now.timestamp() - 3600, when=now,
                              tz_label="北京时间")

    def test_entry_title_is_main_leg_and_plain_text(self):
        txt = self._entry()
        self.assertIn("【Kraken·主腿】", txt)
        self.assertIn("已开仓", txt)
        self.assertIn("USD", txt)
        self.assertNotIn("**", txt)
        self.assertNotIn("USDC", txt)

    def test_strategy_exit_label_ignores_pnl_sign(self):
        for sign in (1, -1):
            txt = self._exit("ma20", sign)
            self.assertIn("策略离场", txt)
            self.assertNotIn("策略止盈", txt)
            self.assertIn("【Kraken·主腿】", txt)
            self.assertNotIn("**", txt)
            self.assertNotIn("USDC", txt)
            # 主腿净额是估算口径：手续费为估算、资金费未计入
            self.assertIn("已扣估算手续费", txt)
            self.assertIn("未计资金费", txt)
            self.assertIn("手续费估算", txt)

    def test_stop_exit_title_is_main_leg(self):
        txt = self._exit("stop", -1)
        self.assertIn("【Kraken·主腿】🛑 止损离场", txt)


class TestMrPartialExitNotification(unittest.TestCase):
    """确认过的部分离场：片段已原子落库 → 必须有一条「部分成交」成交回报。

    旧实现只写 `mr_exit_partial` 事件 + 一条「下轮重试」告警，用户看不到「已经平掉
    多少、还剩多少」；这条推送按成交回报口径（受 fill_push 控制、可补推、同单不重发）。
    """

    def _setup(self, tmp, *, notifier, fill_push="1"):
        leg, db, ex, info = _leg(tmp, notifier=notifier, mr_szi="2.68", mid=95.5)
        db.set_config("mr_enabled", "1")
        db.set_config("fill_push", fill_push)
        db.mr_record_entry(
            signal_date="2026-04-15", entry_session="2026-04-16", qty=2.68,
            qty_int=26800, entry_price=93.0, entry_order_id="100",
            entry_ts=ENTRY_NOW_MS / 1000.0, notional=2.68 * 93.0, margin=249.24,
            equity_at_entry=500.0)
        real_place = ex.place_order

        def place(coin, is_buy, sz, limit_px, **kw):
            res = real_place(coin, is_buy, sz, limit_px, **kw)
            if not is_buy:
                info.mr_szi = "1.48"           # 交易所只剩 1.48 张
                res.update(filled_sz=1.2, avg_px=limit_px, status="filled")
            return res

        ex.place_order = place
        return leg, db, ex, info

    def test_partial_exit_notifies_qty_remaining_and_no_pnl(self):
        with tempfile.TemporaryDirectory() as tmp:
            n = FakeNotifier(ok=True)
            leg, db, ex, info = self._setup(tmp, notifier=n)
            status = leg.tick(now_ms=EXIT_NOW_MS)
            self.assertEqual(status["state"], "exit_not_filled")
            row = db.mr_open_trade()
            self.assertEqual(row["state"], "OPEN", "部分成交不得写出 CLOSED")
            partials = json.loads(row["raw"])["partial_exits"]
            self.assertAlmostEqual(partials[0]["qty"], 1.2, places=6)

            msgs = [m for m in n.messages if "离场部分成交" in m]
            self.assertEqual(len(msgs), 1, "片段落库后必须有一条部分成交回报")
            msg = msgs[0]
            for want in ("【Kraken·MR】", "部分成交", "本次成交  1.2 张 · 交易所剩余 1.48 张",
                         "未全部平仓", "93.00", "95.36", "来源 avg_px",
                         "信号日    2026-04-15", "2026-04-18 04:10:00 北京时间"):
                self.assertIn(want, msg, f"部分成交文案缺 {want}")
            self.assertNotIn("净盈亏    ", msg, "部分成交不能编造净盈亏")
            self.assertNotIn("**", msg)

            rows = [r for r in db.recent_alerts(50)
                    if str(r["kind"] or "").startswith("mr_fill_exit_partial")]
            self.assertEqual(len(rows), 1)
            self.assertEqual(int(rows[0]["sent_successfully"]), 1)
            kind = str(rows[0]["kind"])
            self.assertIn(f":{row['id']}:", kind, "事件键必须含 trade_id")
            self.assertIn(":101", kind, "事件键必须含本单 oid/cloid")

            # 同一单的重复回执：同 kind+key 不得再发
            key = kind.split("mr_fill_exit_partial:", 1)[1]
            before = len(n.messages)
            leg._alert("离场部分成交（重复回执）", kind="mr_fill_exit_partial",
                       key=key, fill=True, dedupe=True)
            self.assertEqual(len(n.messages), before)

    def test_partial_exit_notification_respects_fill_push(self):
        with tempfile.TemporaryDirectory() as tmp:
            n = FakeNotifier(ok=True)
            leg, db, ex, info = self._setup(tmp, notifier=n, fill_push="0")
            leg.tick(now_ms=EXIT_NOW_MS)
            self.assertEqual([m for m in n.messages if "离场部分成交" in m], [])
            rows = [r for r in db.recent_alerts(50)
                    if str(r["kind"] or "").startswith("mr_fill_exit_partial")]
            self.assertEqual(len(rows), 1)
            self.assertEqual(int(rows[0]["sent_successfully"]), 1,
                             "关掉是用户意图 → 记成功，不进补推队列")

    def test_partial_exit_notification_failure_is_retryable(self):
        with tempfile.TemporaryDirectory() as tmp:
            n = FakeNotifier(ok=False)
            leg, db, ex, info = self._setup(tmp, notifier=n)
            leg.tick(now_ms=EXIT_NOW_MS)
            self.assertEqual(len(_failed(db, "mr_fill_exit_partial")), 1)



class TestRecoveryPartialNotification(unittest.TestCase):
    """`_resolve_exit_unknown` 按成交补记片段 → 与正常部分离场同一事件口径。"""

    def test_recovery_notifies_persisted_fragment_once_across_restart(self):
        from tests.test_review7_mr import (NOW as R_NOW, Info as RInfo, fill,
                                           make_leg, mr_entry)
        from app.hl import mr as M
        with tempfile.TemporaryDirectory() as tmp:
            path = f"{tmp}/rec.db"
            db = DB(path)
            trade_id = mr_entry(db, qty=10, entry=100)
            rec_fill = fill(t=R_NOW - 1000, px=109.9, sz=4, cloid="c", tid="f1")
            info = RInfo(szi=10, entry=100, mid=110, fills=[rec_fill])
            leg = make_leg(db, info)
            n = FakeNotifier(ok=True)
            leg.notifier = n
            key = leg._shadow_begin(M.MR_PURPOSE_EXIT, side=-1, sz=10, px=110,
                                    reduce_only=True, cloid="c",
                                    signal_date="2026-11-05", now_ms=R_NOW - 60000)
            leg._shadow_finish(key, status="sent")
            pending = leg._mr_attempts(M.MR_PURPOSE_EXIT, "2026-11-05",
                                       ("intent", "sent"))
            self.assertTrue(pending)

            status = leg._resolve_exit_unknown(db.mr_open_trade(), {"szi": 6},
                                               R_NOW + 300000, pending, {})
            self.assertEqual(status, "clear")
            msgs = [m for m in n.messages if "离场部分成交（按成交补记）" in m]
            self.assertEqual(len(msgs), 1)
            msg = msgs[0]
            for want in ("【Kraken·MR】", "本次补记  4 张",
                         "累计已补记 4 / 台账 10 张", "109.90", "来源 fills",
                         "补记时间", "信号日    2026-11-05"):
                self.assertIn(want, msg, f"补记回报缺 {want}")
            self.assertNotIn("净盈亏    ", msg, "未结算不得编造净盈亏")
            rows = [r for r in db.recent_alerts(50)
                    if str(r["kind"] or "").startswith("mr_fill_exit_partial")]
            self.assertEqual(len(rows), 1)
            self.assertIn(f":{trade_id}:", str(rows[0]["kind"]))
            parts = json.loads(db.mr_open_trade()["raw"])["partial_exits"]
            self.assertEqual(len(parts), 1)
            self.assertAlmostEqual(parts[0]["qty"], 4.0, places=6)

            # 重启 + 重复回执（同一张单的旧 pending 再走一遍）：不重复补记、不重复推送
            db.conn.close()
            db2 = DB(path)
            info2 = RInfo(szi=6, entry=100, mid=110, fills=[rec_fill])
            leg2 = make_leg(db2, info2)
            n2 = FakeNotifier(ok=True)
            leg2.notifier = n2
            leg2._resolve_exit_unknown(db2.mr_open_trade(), {"szi": 6},
                                       R_NOW + 300000, pending, {})
            self.assertEqual([m for m in n2.messages if "部分成交" in m], [],
                             "同一 trade_id+oid/cloid 不得重复推送")
            parts2 = json.loads(db2.mr_open_trade()["raw"])["partial_exits"]
            self.assertEqual(len(parts2), 1, "重复回执不得改写台账片段")
            self.assertAlmostEqual(sum(p["qty"] for p in parts2), 4.0, places=6)


class TestRetryFailedAlerts(unittest.TestCase):
    """补推通道：成功后同一 MR 事件的**所有**失败行一起了结，别的 event 不动。"""

    EVENT_A = "mr_action_required:exit_settle_gap:2026-04-17"
    EVENT_B = "mr_action_required:exit_result_unknown:2026-04-17"

    def _db(self, tmp):
        return DB(f"{tmp}/retry_{abs(hash(tmp)) % 99999}.db")

    def test_retry_once_then_clears_all_rows_of_same_event(self):
        from app.notify import retry_failed_alerts
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(tmp)
            db.log_alert("QQQ", None, self.EVENT_A, "event A", False)
            db.log_alert("QQQ", None, self.EVENT_A, "event A", False)
            db.log_alert("QQQ", None, self.EVENT_B, "event B", False)
            n = FakeNotifier(ok=True)
            sent = retry_failed_alerts(db, n, max_age_h=24.0, limit=1)   # 最新事件 B
            self.assertEqual(n.messages, ["event B"])
            self.assertEqual(len(sent), 1)
            rows = db.recent_alerts(20)
            a_rows = [r for r in rows if r["kind"] == self.EVENT_A]
            self.assertEqual([int(r["sent_successfully"]) for r in a_rows], [0, 0],
                             "补推 B 不得把 A 标已送达")

            sent2 = retry_failed_alerts(db, n, max_age_h=24.0, limit=5)  # A 的两行
            self.assertEqual(n.messages, ["event B", "event A"],
                             "同事件两行只补推一条")
            self.assertEqual(len(sent2), 1)
            a_rows = [r for r in db.recent_alerts(20) if r["kind"] == self.EVENT_A]
            self.assertTrue(all(int(r["sent_successfully"]) == 1 for r in a_rows),
                            "同一事件的两条失败行都必须了结，否则下轮重放")
            retry_failed_alerts(db, n, max_age_h=24.0, limit=5)
            self.assertEqual(n.messages, ["event B", "event A"], "不得重放")
            self.assertEqual(_failed(db), [])

    def test_fill_push_off_settles_fill_rows_as_suppressed(self):
        from app.notify import retry_failed_alerts
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(tmp)
            db.log_alert("QQQ", None, "mr_fill_exit:7", "fill failed", False)
            db.log_alert("QQQ", None, self.EVENT_A, "action failed", False)
            n = FakeNotifier(ok=True)
            retry_failed_alerts(db, n, fill_push=False)
            self.assertEqual(n.messages, ["action failed"])
            fill_row = [r for r in db.recent_alerts(20) if r["kind"].startswith("mr_fill_exit:7")][0]
            self.assertEqual(int(fill_row["sent_successfully"]), 1,
                             "关着开关的待补推成交要就地了结")
            self.assertTrue(str(fill_row["kind"]).endswith(":suppressed"),
                            "kind 后缀让面板显示「未推送（已关闭）」")
            self.assertEqual(_failed(db), [], "不能把用户不想看的旧成交留成待补推")

            n2 = FakeNotifier(ok=True)
            retry_failed_alerts(db, n2, fill_push=True)
            self.assertEqual(n2.messages, [], "重新打开后不得补发已关闭的旧成交")

    def test_suppressed_fill_row_is_never_retried(self):
        from app.notify import retry_failed_alerts
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(tmp)
            db.log_alert("QQQ", None, "mr_fill_entry:9", "fill suppressed by user", True)
            n = FakeNotifier(ok=True)
            retry_failed_alerts(db, n, fill_push=True)
            self.assertEqual(n.messages, [], "用户关掉时已记成功，之后不得补发旧成交")


class TestActionRequiredWording(unittest.TestCase):
    def test_copy_is_conditional_not_a_promise(self):
        with tempfile.TemporaryDirectory() as tmp:
            n = FakeNotifier(ok=True)
            leg, db, ex, info = _leg(tmp, notifier=n)
            leg._action_required({}, kind="exit_adopt_stale_no_evidence",
                                 reason="查不到可归属的成交", signal_date="2026-04-17")
            msg = n.messages[-1]
            self.assertIn("原因      查不到可归属的成交", msg)
            self.assertIn("不是成交确认", msg)
            self.assertIn("人工核对", msg)
            # 不许对全部 caller 做「停手 / 下轮自动解决」的承诺（有的同 tick 就估算关账）
            for forbidden in ("停手", "下一轮", "自动核对", "自愈", "改台账"):
                self.assertNotIn(forbidden, msg)


class TestExecTimestamp(unittest.TestCase):
    """成交时间固定用 Asia/Shanghai，且与信号日分开。"""

    def test_entry_and_exit_show_beijing_timestamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            n = FakeNotifier(ok=True)
            leg, db, ex, info = _leg(tmp, notifier=n)
            info.now_ms = ENTRY_NOW_MS
            db.set_config("mr_enabled", "1")
            leg.tick(now_ms=ENTRY_NOW_MS)
            entry_msg = [m for m in n.messages if "入场成交" in m][0]
            self.assertIn("2026-04-16 21:40:00 北京时间", entry_msg)
            self.assertIn("信号日", entry_msg)

            db.mr_record_entry(
                signal_date="2026-04-15", entry_session="2026-04-16", qty=2.68,
                qty_int=26800, entry_price=93.0, entry_order_id="100",
                entry_ts=ENTRY_NOW_MS / 1000.0, notional=2.68 * 93.0, margin=249.24,
                equity_at_entry=500.0)
            info.mr_szi = "2.68"
            leg.tick(now_ms=EXIT_NOW_MS)
            exit_msg = [m for m in n.messages if "离场成交" in m][0]
            self.assertIn("2026-04-18 04:10:00 北京时间", exit_msg)
            self.assertIn("信号日    2026-04-17", exit_msg)


class TestFundingCashflowLabel(unittest.TestCase):
    def test_positive_value_is_labelled_net_pay_per_existing_contract(self):
        from app.hl.mr import _mr_exit_text
        msg = _mr_exit_text(
            coin="QQQ", filled=2.68, ledger_qty=2.68, partial_qty=0.0, gap_qty=0.0,
            entry_px=93.0, exit_px=95.0, entry_ts=ENTRY_NOW_MS / 1000.0,
            now_s=EXIT_NOW_MS / 1000.0, net_pnl=5.0, fee_est=0.14, complete=True,
            funding_paid=0.03, price_source="avg_px", signal_date="2026-04-17")
        # `_funding_paid` 的既有契约：正 = 已付出；不改数值，只把标签写清楚
        self.assertIn("净付 0.03 USD", msg)
        self.assertIn("按交易所流水口径", msg)
        self.assertNotIn("已付", msg)
        # 净额一侧必须带估算口径
        self.assertIn("≈+5.00 USD", msg)
        self.assertIn("已扣估算手续费", msg)


class TestMrAdoptionNotifications(unittest.TestCase):
    """恢复/补记也是成交类通知：跟随 fill_push + 可补推 + 同事件不重发。"""

    def _adopt_entry(self, tmp, *, notifier, fill_push):
        leg, db, ex, info = _leg(tmp, notifier=notifier)
        info.now_ms = ENTRY_NOW_MS
        db.set_config("mr_enabled", "1")
        db.set_config("fill_push", fill_push)
        ex.fill = False
        leg.tick(now_ms=ENTRY_NOW_MS)
        info.mr_szi = "2.68"
        status = leg.tick(now_ms=ENTRY_NOW_MS + 300_000)
        return status, leg, db, ex, info

    def test_adoption_entry_respects_fill_push_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            n = FakeNotifier(ok=True)
            status, leg, db, ex, info = self._adopt_entry(tmp, notifier=n, fill_push="0")
            self.assertEqual(status.get("adopted"), "entry")
            self.assertEqual([m for m in n.messages if "补记" in m], [])
            rows = [r for r in db.recent_alerts(50)
                    if str(r["kind"] or "").startswith("mr_fill_entry_adopt")]
            self.assertEqual(len(rows), 1)
            self.assertEqual(int(rows[0]["sent_successfully"]), 1)

    def test_adoption_entry_pushes_with_timestamp_when_on(self):
        with tempfile.TemporaryDirectory() as tmp:
            n = FakeNotifier(ok=True)
            status, leg, db, ex, info = self._adopt_entry(tmp, notifier=n, fill_push="1")
            msgs = [m for m in n.messages if "补记" in m]
            self.assertEqual(len(msgs), 1)
            self.assertIn("【Kraken·MR】", msgs[0])
            self.assertIn("补记时间  2026-04-16 21:45:00 北京时间", msgs[0])
            self.assertNotIn("成交时间", msgs[0], "补记不能伪装成成交时刻")
            rows = [r for r in db.recent_alerts(50)
                    if str(r["kind"] or "").startswith("mr_fill_entry_adopt")]
            self.assertEqual(len(rows), 1)

    def test_adoption_exit_respects_fill_push_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            n = FakeNotifier(ok=True)
            leg, db, ex, info = _leg(tmp, notifier=n, mr_szi="2.68", mid=95.5)
            db.set_config("mr_enabled", "1")
            db.set_config("fill_push", "0")
            db.mr_record_entry(
                signal_date="2026-04-15", entry_session="2026-04-16", qty=2.68,
                qty_int=26800, entry_price=93.0, entry_order_id="100",
                entry_ts=ENTRY_NOW_MS / 1000.0, notional=2.68 * 93.0, margin=249.24,
                equity_at_entry=500.0)
            ex.fill = False
            leg.tick(now_ms=EXIT_NOW_MS)
            info.mr_szi = "0"
            status = leg.tick(now_ms=EXIT_NOW_MS + 300_000)
            self.assertEqual(status.get("adopted"), "exit")
            self.assertEqual([m for m in n.messages if "补记" in m], [])
            rows = [r for r in db.recent_alerts(50)
                    if str(r["kind"] or "").startswith("mr_fill_exit_adopt")]
            self.assertEqual(len(rows), 1)
            self.assertEqual(int(rows[0]["sent_successfully"]), 1)

    def test_adoption_exit_note_and_time_are_truthful_for_mark_estimate(self):
        with tempfile.TemporaryDirectory() as tmp:
            n = FakeNotifier(ok=True)
            leg, db, ex, info = _leg(tmp, notifier=n, mr_szi="2.68", mid=95.5)
            db.set_config("mr_enabled", "1")
            db.set_config("fill_push", "1")
            db.mr_record_entry(
                signal_date="2026-04-15", entry_session="2026-04-16", qty=2.68,
                qty_int=26800, entry_price=93.0, entry_order_id="100",
                entry_ts=ENTRY_NOW_MS / 1000.0, notional=2.68 * 93.0, margin=249.24,
                equity_at_entry=500.0)
            ex.fill = False
            leg.tick(now_ms=EXIT_NOW_MS)
            info.mr_szi = "0"
            status = leg.tick(now_ms=EXIT_NOW_MS + 300_000)
            self.assertEqual(status.get("adopted"), "exit")
            msg = [m for m in n.messages if "离场成交" in m][-1]
            self.assertIn("补记时间", msg)
            self.assertNotIn("成交时间", msg, "补记不能伪装成成交时刻")
            self.assertIn("按当前证据补记台账", msg)
            self.assertIn("价格来源 all_mids", msg)
            self.assertIn("未完成", msg, "估算价关账必须保留未完成口径")


class TestTelegramPlainText(unittest.TestCase):
    """通知边界统一去 `**`：engine._emit 等路径也经过 notifier，不靠各调用点自觉。"""

    def test_plain_text_only_strips_bold_markers(self):
        from app.notify import plain_text
        self.assertEqual(plain_text("a *b* **c**"), "a *b* c")
        self.assertEqual(plain_text("【Kraken】🛑 **未能自动处理**"), "【Kraken】🛑 未能自动处理")

    def test_telegram_payload_has_no_markdown_bold(self):
        from unittest import mock
        from app.notify import TelegramNotifier
        payloads = []

        def fake_post(url, payload, timeout=10):
            payloads.append(payload)
            return True, '{"ok": true}'

        with mock.patch("app.notify._post_json", side_effect=fake_post):
            ok = TelegramNotifier("tok", "42").send_message(
                "【Kraken】🛑 止损单丢失，**未能自动处理**\n第二行")
        self.assertTrue(ok)
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]["text"],
                         "【Kraken】🛑 止损单丢失，未能自动处理\n第二行")
        self.assertNotIn("parse_mode", payloads[0])


class TestAlertEventKeys(unittest.TestCase):
    """每条 MR 告警都要有自己的事件键（`mr_alert` 不能全挤进一个事件）。"""

    def test_distinct_unkeyed_warnings_retry_separately(self):
        from app.notify import retry_failed_alerts
        with tempfile.TemporaryDirectory() as tmp:
            n = FakeNotifier(ok=False)
            leg, db, ex, info = _leg(tmp, notifier=n)
            leg._alert("读账户失败：503")
            leg._alert("读合约规格失败：timeout")
            rows = [r for r in db.recent_alerts(20)
                    if str(r["kind"] or "").startswith("mr_alert:auto:")]
            self.assertEqual(len(rows), 2)
            self.assertEqual(len({r["kind"] for r in rows}), 2, "不同告警不得共用事件键")

            n2 = FakeNotifier(ok=True)
            retry_failed_alerts(db, n2, limit=1)
            self.assertEqual(len(n2.messages), 1)
            left = [r for r in _failed(db) if str(r["kind"]).startswith("mr_alert")]
            self.assertEqual(len(left), 1, "清一条不得把另一条也标已送达")
            retry_failed_alerts(db, n2, limit=5)
            self.assertEqual(len(n2.messages), 2)
            self.assertEqual(_failed(db), [])

    def test_same_text_in_next_hour_is_a_new_event(self):
        from app.notify import retry_failed_alerts
        with tempfile.TemporaryDirectory() as tmp:
            n = FakeNotifier(ok=False)
            leg, db, ex, info = _leg(tmp, notifier=n)
            t = {"v": EXIT_NOW_MS / 1000.0}
            leg.clock = lambda: t["v"]
            leg._alert("退避重试仍失败：交易所 503")
            t["v"] += 3600.0
            leg._alert("退避重试仍失败：交易所 503")
            kinds = {r["kind"] for r in db.recent_alerts(20)
                     if str(r["kind"]).startswith("mr_alert:auto:")}
            self.assertEqual(len(kinds), 2, "跨小时再发生是另一个事件")
            n2 = FakeNotifier(ok=True)
            retry_failed_alerts(db, n2, limit=5)
            self.assertEqual(len(n2.messages), 2)
            self.assertEqual(_failed(db), [])


class TestSuppressedDisplay(unittest.TestCase):
    def test_suppressed_row_is_not_shown_as_delivered(self):
        from app.admin_ui import _alert_kind_text, _alert_state
        self.assertEqual(_alert_state("mr_fill_exit:7:suppressed", True), "未推送（已关闭）")
        self.assertEqual(_alert_state("mr_fill_exit:7", True), "已送达")
        self.assertEqual(_alert_state("mr_fill_exit:7", False), "失败")
        self.assertEqual(_alert_kind_text("exec_alert_fills:suppressed"), "执行器成交回报")

    def test_suffixed_exec_alert_fills_is_still_not_retried(self):
        from app.notify import retry_failed_alerts
        with tempfile.TemporaryDirectory() as tmp:
            db = DB(f"{tmp}/sup_{abs(hash(tmp)) % 99999}.db")
            db.log_alert(None, None, "exec_alert_fills:suppressed", "成交回报", True)
            db.log_alert(None, None, "exec_alert_fills:suppressed", "成交回报", False)
            n = FakeNotifier(ok=True)
            retry_failed_alerts(db, n, fill_push=True)
            self.assertEqual(n.messages, [])


class TestEntryPartialFill(unittest.TestCase):
    def test_entry_message_shows_planned_vs_actual(self):
        with tempfile.TemporaryDirectory() as tmp:
            n = FakeNotifier(ok=True)
            leg, db, ex, info = _leg(tmp, notifier=n)
            info.now_ms = ENTRY_NOW_MS
            db.set_config("mr_enabled", "1")
            ex.fill_sz = 1.25                     # 计划 2.68，只成交 1.25
            leg.tick(now_ms=ENTRY_NOW_MS)
            msg = [m for m in n.messages if "入场成交" in m][0]
            self.assertIn("成交 1.25 / 计划 2.68", msg)
            self.assertIn("部分成交", msg)
            self.assertIn("成交时间", msg)


class TestMainRetryWiring(unittest.TestCase):
    """直接跑真实的 `main._retry_failed_alerts`（临时 DB + 假 notifier + 重启）。"""

    _ENV = ("DB_PATH", "ENABLE_SCHEDULER", "ADMIN_TOKEN", "TOKEN_SECRET",
            "BASE_URL", "DRY_RUN", "HL_EXEC_MODE")

    def test_real_retry_clears_same_event_and_survives_restart(self):
        import importlib
        import os
        saved = {k: os.environ.get(k) for k in self._ENV}
        try:
            import fastapi                                          # noqa: F401
        except ImportError:
            self.skipTest("fastapi 未安装（CI 会跑）")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                os.environ.update(
                    DB_PATH=f"{tmp}/main.db", ENABLE_SCHEDULER="false",
                    ADMIN_TOKEN="test-admin", TOKEN_SECRET="test-secret",
                    BASE_URL="https://test.example", DRY_RUN="true")
                main = importlib.reload(importlib.import_module("app.main"))
                main.db.log_alert("QQQ", None,
                                  "mr_action_required:exit_settle_gap:2026-04-17", "A", False)
                main.db.log_alert("QQQ", None,
                                  "mr_action_required:exit_settle_gap:2026-04-17", "A", False)
                main.db.log_alert("QQQ", None,
                                  "mr_action_required:exit_result_unknown:2026-04-17", "B", False)
                first = FakeNotifier(ok=True)
                main.notifier = first
                main._retry_failed_alerts(limit=1)      # 最新事件 = B
                self.assertEqual(first.messages, ["B"])

                # 进程重启：关连接、重新装配模块（同一 DB 文件）
                main.db.conn.close()
                main = importlib.reload(main)
                second = FakeNotifier(ok=True)
                main.notifier = second
                main._retry_failed_alerts(limit=1)      # 同事件两行 → 只补一次
                self.assertEqual(second.messages, ["A"])
                main._retry_failed_alerts(limit=1)      # 不能重放
                self.assertEqual(second.messages, ["A"])
                self.assertEqual(main.db.failed_alerts_since(0), [])
                main.db.conn.close()
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


if __name__ == "__main__":
    unittest.main()
