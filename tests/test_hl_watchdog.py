"""app/hl/watchdog.py 的单元测试（离线）。

看守是「出事才发现」的最后一层，它自己必须可靠：
- 正常时**必须静默**（否则 cron 天天刷屏，人会开始忽略告警）
- 有问题时必须带 ⚠️ 前缀（Hermes cron 靠这个前缀决定推不推）
"""
from __future__ import annotations

import contextlib
import io
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from app.db import DB
from app.hl import MODE_DRY, MODE_LIVE, MODE_OFF
from app.hl import watchdog
from app.hl.info import HlInfoError

from tests.test_hl_executor import FakeInfo, _pos, _snap, _stop_order


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(str(Path(self.tmp.name) / "t.db"))
        self.info = FakeInfo(snap=_snap(positions=[], orders=[]))
        self.env = mock.patch.dict(os.environ, {"HL_ACCOUNT_ADDRESS": "0xabc"}, clear=False)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def checks(self, mode=MODE_LIVE, force=False):
        return watchdog.run_checks(self.db, info=self.info, mode=mode, force=force)

    def _heartbeat(self, age_s=0.0):
        self.db.hl_log_event("heartbeat", "ok")
        if age_s:
            ev_id = self.db.hl_recent_events(1, kind="heartbeat")[0]["id"]
            with self.db.conn:
                self.db.conn.execute("UPDATE hl_events SET created_at=? WHERE id=?",
                                     (time.time() - age_s, ev_id))

    def _set_hl_status(self, pid, status):
        with self.db.conn:
            self.db.conn.execute("UPDATE positions SET hl_status=? WHERE id=?", (status, pid))

    @staticmethod
    def bad(notes):
        return [n for n in notes if n.startswith("⚠️")]


class TestBasic(Base):
    def test_off_mode_short_circuits(self):
        notes = self.checks(mode=MODE_OFF, force=True)
        self.assertEqual(len(notes), 1)
        self.assertIn("off 模式", notes[0])
        self.assertEqual(self.bad(notes), [])

    def test_healthy_state_has_no_warnings(self):
        self._heartbeat()
        self.assertEqual(self.bad(self.checks()), [])

    def test_force_shows_ok_items_too(self):
        self._heartbeat()
        notes = self.checks(force=True)
        self.assertTrue(any(n.startswith("· ") for n in notes))
        self.assertEqual(self.bad(notes), [])


class TestProblems(Base):
    def test_missing_heartbeat(self):
        self.assertIn("从未写过心跳", " ".join(self.bad(self.checks())))

    def test_stale_heartbeat(self):
        self._heartbeat()
        ev_id = self.db.hl_recent_events(1, kind="heartbeat")[0]["id"]
        with self.db.conn:
            self.db.conn.execute("UPDATE hl_events SET created_at=? WHERE id=?",
                                 (time.time() - 20 * 60, ev_id))
        self.assertIn("心跳超期", " ".join(self.bad(self.checks())))

    def test_kill_switch_reported_with_reason(self):
        self.db.hl_set_kill(True, "当日亏损熔断")
        joined = " ".join(self.bad(self.checks()))
        self.assertIn("kill switch 处于触发状态", joined)
        self.assertIn("当日亏损熔断", joined)            # 中文必须是可读的，不是 \uXXXX

    def test_stuck_intent_order(self):
        self.db.hl_record_intent("BTC_USDT", "BTC", 1791900000000, "entry", 1, sz=0.01)
        with self.db.conn:
            self.db.conn.execute("UPDATE hl_orders SET created_at=?", (time.time() - 600,))
        self.assertIn("卡住的意图单", " ".join(self.bad(self.checks())))

    def test_naked_position_without_stop(self):
        self.info._snap = _snap(positions=[_pos("BTC")], orders=[])
        self.assertIn("没有止损挂单", " ".join(self.bad(self.checks())))

    def test_position_with_stop_is_fine(self):
        self._heartbeat()
        self.info._snap = _snap(positions=[_pos("BTC")], orders=[_stop_order()])
        self.assertEqual(self.bad(self.checks()), [])

    def test_local_open_missing_on_exchange(self):
        pid = self.db.create_position(symbol="BTC_USDT", signal_bar_ms=1, side=1, entry_price=1.0,
                                      stop_price=0.9, stop_dist=0.1, contracts=1, coin_qty=1.0,
                                      notional=1.0, risk_u=1.0, equity_at_entry=100.0)
        self.db.confirm_position(pid, 1.0, 1, 0.9, True)
        self._set_hl_status(pid, "open")
        self.assertIn("交易所查不到该仓位", " ".join(self.bad(self.checks())))

    def test_mismatch_reported(self):
        pid = self.db.create_position(symbol="BTC_USDT", signal_bar_ms=1, side=1, entry_price=1.0,
                                      stop_price=0.9, stop_dist=0.1, contracts=1, coin_qty=1.0,
                                      notional=1.0, risk_u=1.0, equity_at_entry=100.0)
        self._set_hl_status(pid, "mismatch")
        self.assertIn("mismatch", " ".join(self.bad(self.checks())))

    def test_closing_row_listed_but_not_shouted(self):
        """L4（对抗审查）：closing（平仓成交待核对）必须出现在报告里，但**不当异常喊**。

        以前它两头都不报：mismatch 分行不收它、「本地有仓但交易所查不到」只认
        hl_status='open' —— 宽限期里的仓在看守报告里彻底消失。
        """
        self._heartbeat()
        pid = self.db.create_position(symbol="BTC_USDT", signal_bar_ms=1, side=1, entry_price=1.0,
                                      stop_price=0.9, stop_dist=0.1, contracts=1, coin_qty=1.0,
                                      notional=1.0, risk_u=1.0, equity_at_entry=100.0)
        self.db.confirm_position(pid, 1.0, 1, 0.9, True)
        self._set_hl_status(pid, "closing")

        notes = self.checks(force=True)
        joined = " ".join(notes)
        self.assertIn("平仓待核对", joined)
        self.assertEqual(self.bad(notes), [], "closing 是已知状态，不许当异常喊")
        self.assertIn("· ", [n[:2] for n in notes if "平仓待核对" in n][0])

    def test_recent_failures(self):
        self.db.hl_record_intent("BTC_USDT", "BTC", 1791900000000, "entry", 1, sz=0.01)
        self.db.hl_record_intent("ETH_USDT", "ETH", 1791900000000, "entry", 1, sz=0.1)
        rows = self.db.hl_recent_orders(10)
        self.assertTrue(rows)
        self.db.hl_update_order(rows[0]["symbol"], rows[0]["bar_ms"], rows[0]["purpose"],
                                status="failed", error="insufficient margin")
        self.assertIn("下单失败", " ".join(self.bad(self.checks())))

    def test_exchange_unreadable(self):
        self.info = FakeInfo(raise_on_snapshot="network down")
        self.assertIn("读不到 Kraken 账户", " ".join(self.bad(self.checks())))

    def test_missing_address_skips_exchange_checks(self):
        with mock.patch.dict(os.environ, {"HL_ACCOUNT_ADDRESS": ""}, clear=False):
            notes = self.checks(force=True)
        self.assertIn("未配置 HL_ACCOUNT_ADDRESS", " ".join(notes))


class TestMain(Base):
    """main() 的输出契约：正常静默、异常带 ⚠️ 前缀。"""

    def _run(self, argv=None):
        buf = io.StringIO()
        with mock.patch("app.db.DB", return_value=self.db), \
             mock.patch("app.kr.info.KrInfo", return_value=self.info), \
             contextlib.redirect_stdout(buf):
            rc = watchdog.main(argv or ["--mode", MODE_LIVE])
        return rc, buf.getvalue()

    def test_silent_when_healthy(self):
        self._heartbeat()
        rc, out = self._run()
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")

    def test_prints_when_problem(self):
        rc, out = self._run()                     # 没有心跳 → 有问题
        self.assertEqual(rc, 0)
        self.assertIn("Kraken 执行器看守告警", out)
        self.assertIn("从未写过心跳", out)

    def test_dry_run_lists_everything(self):
        rc, out = self._run(["--dry-run", "--mode", MODE_LIVE])
        self.assertEqual(rc, 0)
        self.assertIn("[dry-run]", out)
        self.assertIn("kill switch", out)


class TestKrakenAdaptation(Base):
    """PR-12 去 HL 残留：Kraken key 无到期 → 跳过 agent 检查。

    时钟偏差检查已整段删除（2026-11-15 第十一轮）：Kraken v3 签名不含时间戳/nonce
    （见 app/kr/auth.py），「>2s 会让签名被拒」在 Kraken 上不成立，留着只会推假告警。
    """

    def test_agent_check_skipped_for_kraken_key_shape(self):
        """Kraken 下 KrInfo **实现了** agent_status，但返回
        {"kind":"kraken-api","expires":None,"days_left":None}（无 agents / max_valid_until）。
        这是「不适用」，不是读失败、更不是「没有任何 agent」故障，不许报成告警。
        用真实 KrInfo 的形状做 fixture（不用自造「没有该方法」的假件——那会掩盖真实契约）。
        """
        from app.kr.info import KrInfo
        self._heartbeat()

        class _KrakenShape:
            """复用真实 KrInfo.agent_status 的返回，避免 fixture 复制错假设。"""
            def snapshot(self, address):
                return _snap(positions=[], orders=[])

            def agent_status(self, address):
                return KrInfo.agent_status(self, address)   # 真实实现：kind=kraken-api，无 agents

        notes = watchdog.run_checks(self.db, info=_KrakenShape(), mode=MODE_LIVE, force=True)
        joined = " ".join(notes)
        self.assertIn("无到期概念", joined)
        self.assertNotIn("没有任何 agent", joined)
        self.assertEqual(self.bad(notes), [])

    def test_agent_check_skipped_without_agent_status_method(self):
        """没有 agent_status 方法的桩（老 HL 迁移期假件）：同样按「不适用」跳过。"""
        self._heartbeat()

        class _NoAgent:
            def snapshot(self, address):
                return _snap(positions=[], orders=[])

        notes = watchdog.run_checks(self.db, info=_NoAgent(), mode=MODE_LIVE, force=True)
        joined = " ".join(notes)
        self.assertIn("无到期概念", joined)
        self.assertEqual(self.bad(notes), [])


class TestVenueAddressFree(Base):
    """2026-11-08 审核 C4：Kraken 没有钱包地址概念，不许拿「env 没填」当配置缺失。

    以前 `not address` 就跳过整段交易所侧检查 —— 生产 compose 里 HL_ACCOUNT_ADDRESS
    只是占位常量（可能为空），一旦为空，实盘的仓位/止损检查就整块静默消失。
    """

    def _kraken_info(self, positions=None, orders=None):
        snap = _snap(positions=positions or [], orders=orders or [])

        class _Kr:
            venue_address_free = True

            def snapshot(self, address):
                return snap

            def agent_status(self, address):
                return {"kind": "kraken-api", "expires": None, "days_left": None}

        return _Kr()

    def test_no_env_address_still_checks_positions(self):
        self._heartbeat()
        naked = _pos(coin="BTC", size=0.01, entry=100_000.0)
        with mock.patch.dict(os.environ, {"HL_ACCOUNT_ADDRESS": ""}, clear=False):
            notes = self.checks()          # 先用普通 info 拿基线（会警告未配置）
            self.assertIn("未配置 HL_ACCOUNT_ADDRESS", " ".join(notes))
            notes = watchdog.run_checks(self.db, info=self._kraken_info(positions=[naked]),
                                        mode=MODE_LIVE, force=True)
        joined = " ".join(notes)
        self.assertNotIn("未配置 HL_ACCOUNT_ADDRESS", joined, "venue 无地址概念，不许报未配置")
        self.assertIn("没有止损挂单", joined, "仓位检查必须照常跑（否则实盘无看守）")
        self.assertIn("仓位", joined)

    def test_equity_text_is_kraken_derivatives_account(self):
        self._heartbeat()
        snap = _snap(positions=[], orders=[])
        snap.update({"unified": True, "equity": 1234.5, "equity_total": 1234.5,
                     "withdrawable": 1000.0, "margin_used": 0.0, "spot_usdc": 1000.0})
        info = self._kraken_info()
        info.snapshot = lambda address: snap
        notes = watchdog.run_checks(self.db, info=info, mode=MODE_LIVE, force=True)
        joined = " ".join(notes)
        self.assertIn("Kraken 衍生品账户", joined)
        self.assertNotIn("现货", joined, "Kraken 不拆现货/合约，别写不存在的结构")
        self.assertNotIn("浮动盈亏", joined)

class TestAgentExpiry(Base):
    """agent（API 钱包）授权到期必须提前告警 —— 到期后所有下单会被拒。"""

    def test_warns_when_expiring_soon(self):
        self.db.hl_log_event("heartbeat", "ok")
        self.info.agent_status = lambda addr: {"agents": [{"name": "Hermes", "address": "0xc1ee", "valid_until": (time.time() + 3600) * 1000}], "max_valid_until": (time.time() + 3600) * 1000}
        joined = " ".join(self.bad(self.checks()))
        self.assertIn("agent 授权只剩", joined)

    def test_quiet_when_far_from_expiry(self):
        self._heartbeat()
        self.info.agent_status = lambda addr: {"agents": [{"name": "Hermes", "address": "0xc1ee", "valid_until": (time.time() + 30 * 86400) * 1000}], "max_valid_until": (time.time() + 30 * 86400) * 1000}
        self.assertEqual(self.bad(self.checks()), [])

    def test_warns_when_no_agent_at_all(self):
        self.db.hl_log_event("heartbeat", "ok")
        self.info.agent_status = lambda addr: {"agents": [], "max_valid_until": None}
        self.assertIn("没有任何 agent", " ".join(self.bad(self.checks())))

    def test_no_expiry_is_fine(self):
        self._heartbeat()
        self.info.agent_status = lambda addr: {"agents": [{"name": "Hermes", "address": "0xc1ee", "valid_until": None}], "max_valid_until": None}
        self.assertEqual(self.bad(self.checks()), [])


if __name__ == "__main__":
    unittest.main()


class TestStopCoverageSameSource(Base):
    """止损判据必须与执行器/面板/探针**同源**（app/hl/stops.py::covering_stop_px）。

    2026-11-15 第十一轮复审的实测缺陷：旧版只判 coin + reduce_only + is_trigger，
    于是「盈利侧止盈单」和「只有 1 tick 的触发单」都被算成「止损在场」——
    真裸仓被漏报；同时不排除 QQQ，MR 持仓每次都被报成裸奔。

    这组用例把判据钉死在同一份实现上：**改判据只许改 app/hl/stops.py**。
    """

    def _notes(self, positions, orders, info=None):
        self._heartbeat()
        snap = _snap(positions=positions, orders=orders)
        self.info = info or FakeInfo(snap=snap, specs={
            "BTC": {"sz_decimals": 5}, "ETH": {"sz_decimals": 4}})
        self.info._snap = snap
        return watchdog.run_checks(self.db, info=self.info, mode=MODE_LIVE, force=True)

    def test_covering_stop_is_quiet(self):
        notes = self._notes([_pos(size=0.01, entry=100_000.0)],
                            [dict(_stop_order(trigger=97_000.0), sz=0.01)])
        self.assertEqual(self.bad(notes), [])
        self.assertIn("止损单在场（覆盖量足够）", " ".join(notes))

    def test_profit_side_take_profit_is_not_a_stop(self):
        tp = dict(_stop_order(trigger=105_000.0), order_type="take_profit")
        notes = self._notes([_pos(size=0.01, entry=100_000.0)], [tp])
        self.assertIn("裸奔仓位", " ".join(self.bad(notes)))
        self.assertNotIn("止损单在场", " ".join(notes))

    def test_one_tick_stop_does_not_cover(self):
        notes = self._notes([_pos(size=0.01, entry=100_000.0)],
                            [dict(_stop_order(trigger=97_000.0), sz=0.0001)])
        self.assertIn("裸奔仓位", " ".join(self.bad(notes)))

    def test_wrong_side_trigger_does_not_cover(self):
        # 多仓的止损必须在入场价**下方**；触发价在上方是移动止盈，不是止损
        notes = self._notes([_pos(size=0.01, entry=100_000.0)],
                            [dict(_stop_order(trigger=103_000.0), sz=0.01)])
        self.assertIn("裸奔仓位", " ".join(self.bad(notes)))

    def test_mr_leg_is_skipped_not_reported_naked(self):
        qqq = _pos(coin="QQQ", size=0.05, entry=600.0)
        notes = self._notes([qqq], [])
        self.assertEqual(self.bad(notes), [], "MR 腿设计上不挂交易所止损，不许报裸奔")
        self.assertIn("MR 腿持仓", " ".join(notes))

    def test_coin_mapping_follows_registry(self):
        self.assertEqual(watchdog._coin_of("BTC_USDT"), "BTC")
        self.assertEqual(watchdog._coin_of("QQQ_USDT"), "QQQ")
        self.assertIsNone(watchdog._coin_of("NOPE_USDT"),
                          "未登记标的必须判 None（不许 split('_')[0] 猜一个合约名）")
