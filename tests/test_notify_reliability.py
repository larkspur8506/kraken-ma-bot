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


def _leg(tmpdir: str, *, notifier=None, **kw):
    db = DB(f"{tmpdir}/notify_{abs(hash(tmpdir)) % 99999}.db")
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

            # 进程重启：内存去重全清，但 DB 里事件行还在 —— 旧实现会在这里 return，
            # 推送永远补不回来。
            n = FakeNotifier(ok=True)
            leg2 = _rebuild(db, tmp, notifier=n)
            leg2._action_required({}, kind="exit_fragment_uncommitted",
                                  reason="片段未能原子落库", signal_date="2026-04-15")
            self.assertEqual(len(n.messages), 1)
            self.assertEqual(_failed(db, "mr_action_required"), [])
            self.assertEqual(len(db.hl_recent_events(10, kind=ACTION_REQUIRED_KIND)), 1)

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
            for want in ("已做", "自动", "人工", "缺口 0.5 张"):
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
                         "1 天 6 小时 30 分钟", "净盈亏", "费用", "信号日"):
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

    def test_stop_exit_title_is_main_leg(self):
        txt = self._exit("stop", -1)
        self.assertIn("【Kraken·主腿】🛑 止损离场", txt)


if __name__ == "__main__":
    unittest.main()
