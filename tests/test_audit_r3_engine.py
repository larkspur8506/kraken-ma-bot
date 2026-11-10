"""第三轮审核（fix/audit-r3）的策略/引擎类回归测试。

四条缺陷，每条一组用例：

R3-1 **入场根不得压制 MA20 离场**：回测 `harness.py` 在 t 用 `exit_signal_at(t-1)` 判离场，
     t-1 就是入场根，没有任何 `u > ei` 守卫；旧版 engine.walk 用 is_entry_bar 把入场根的
     MA20 离场压掉 → 与回测差 6% 量级的交易（方向是「亏损单多拿一根 4h」）。
R3-2 **closing/mismatch 行不得按参考价本地关账并发可执行卡**：执行器对这两类行故意
     return False（让出接管权），旧引擎随即 close_position + format_exit 出人工离场卡 →
     用户照卡去平一个交易所侧可能已不存在的仓（反手开新仓）。
R3-3 **on_entry 异常不得回落成手工开仓卡**：下单可能已成交、只是落库那步抛异常，
     旧代码随后照常发「开仓」卡 → 用户再开一次 = 双开。
R3-4 **MR 会话必须认 NYSE 假日与半日市**：假日 6 根「合法」bar 不得产出会话；
     半日市（13:00 收盘）不得把 13:00 的盘后 bar 当收盘。

零网络：`market.fetch` 全程打桩；时间一律用写死的 bar 时间戳。
"""
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import engine, market, mr_signal as M                       # noqa: E402
from app import strategy as S                                       # noqa: E402
from app.db import DB                                               # noqa: E402
from tests.test_engine import cfg, entry_setup, prep                # noqa: E402
from tests.test_strategy import STEP, flat_bar, trend_then_flat     # noqa: E402

SYM = "BTC_USDT"


class _Notifier:
    def __init__(self):
        self.msgs = []

    def send_message(self, m):
        self.msgs.append(m)
        return True


class _Exec:
    """执行器替身（只覆盖引擎会调的那几个入口）。"""

    def __init__(self, on_exit_ret=False, on_entry_exc=None):
        self.on_exit_ret = on_exit_ret
        self.on_entry_exc = on_entry_exc
        self.exit_calls = []
        self.entry_calls = []

    def run(self):
        pass

    def on_exit(self, pid, reason, ref_price):
        self.exit_calls.append((pid, reason, ref_price))
        return self.on_exit_ret

    def on_entry(self, pid):
        self.entry_calls.append(pid)
        if self.on_entry_exc is not None:
            raise self.on_entry_exc
        return False


# ---------------------------------------------------------------- R3-1

class TestEntryBarCanExitOnMA20(unittest.TestCase):
    """入场根收盘跌破 SMA20 → walk 必须产出 exit(ma20)（与回测一致）。"""

    def test_entry_bar_ma20_exit(self):
        bars, i = entry_setup()
        ind, sq = prep(bars)
        c = cfg()
        _, pos = engine.walk(c, SYM, bars, ind, sq, None, i, i, i)
        self.assertIsNotNone(pos)

        # 止损甩到很远：只可能由 MA20 触发（保证测的是被删掉的那条压制逻辑）
        pos.stop_price = pos.entry_price * 0.5

        entry_px = bars[i].close
        entry_bar = S.Bar(open_ms=bars[i].open_ms + STEP, open=entry_px, high=entry_px,
                          low=entry_px * 0.90, close=entry_px * 0.90)   # 收盘大跌 → 破 SMA20
        bars2 = bars[:i + 1] + [entry_bar, flat_bar(entry_bar, entry_bar.close)]
        ind2, sq2 = prep(bars2)
        latest = i + 1

        # 前提：这根 bar **正是入场根**（信号 bar 的下一根，相距一个 4h）
        self.assertEqual(entry_bar.open_ms - pos.signal_bar_ms, engine.INTERVAL_MS)
        self.assertLess(entry_bar.close, ind2["sma20"][latest])          # 确实跌破均线
        self.assertGreater(entry_bar.low, pos.eff_stop)                  # 且没碰止损

        events, pos2 = engine.walk(c, SYM, bars2, ind2, sq2, pos, latest, latest, latest)
        self.assertEqual(events[0].kind, "exit")
        self.assertEqual(events[0].reason, "ma20")
        self.assertAlmostEqual(events[0].exit_price, entry_bar.close)
        self.assertEqual(events[0].bars_held, 1, "入场根 = 持有 1 根")
        # 同一根上策略可能立刻给出反向信号（本例确实出现空单信号）—— 那是 walk 的正常
        # 行为；这里只断言**旧的多单仓位**已被回收，没有再被算作持仓。
        self.assertTrue(pos2 is None or pos2.signal_bar_ms == entry_bar.open_ms)

    def test_next_bar_ma20_exit_still_works(self):
        """（对照组）入场根之后的根照旧能离场 —— 删守卫不能顺手改坏常路径。"""
        bars, i = entry_setup()
        ind, sq = prep(bars)
        c = cfg()
        _, pos = engine.walk(c, SYM, bars, ind, sq, None, i, i, i)
        pos.stop_price = pos.entry_price * 0.5
        px = bars[i].close
        b1 = flat_bar(bars[i], px)                                      # 入场根（不破均线）
        b2 = S.Bar(open_ms=b1.open_ms + STEP, open=px, high=px,
                   low=px * 0.90, close=px * 0.90)                      # 第二根才破均线
        bars2 = bars[:i + 1] + [b1, b2, flat_bar(b2, b2.close)]
        ind2, sq2 = prep(bars2)
        latest = i + 2
        events, _ = engine.walk(c, SYM, bars2, ind2, sq2, pos, latest, latest, latest)
        self.assertEqual([(e.kind, e.reason) for e in events], [("exit", "ma20")])
        self.assertEqual(events[0].bars_held, 2)


# ---------------------------------------------------------------- R3-2 / R3-3（tick 级）

class _TickBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(str(Path(self.tmp.name) / "t.db"))
        self.c = cfg()
        self.c.symbols = [SYM]
        self.c.dry_run = False
        self.notifier = _Notifier()
        self.feed = []
        self._orig_fetch = market.fetch
        market.fetch = self._fetch

    def tearDown(self):
        market.fetch = self._orig_fetch
        self.tmp.cleanup()

    def _fetch(self, symbol, now_ms=None, allow_fallback=False):
        return market.Feed(symbol=symbol, source="test", bars=self.feed,
                           fetched_at_ms=now_ms if now_ms is not None else self.feed[-1].open_ms)

    def _tick(self, now_ms, executor=None, bars=None):
        if bars is not None:
            self.feed = bars
        return engine.tick(self.db, self.c, self.notifier, now_ms=now_ms, executor=executor)

    def _alerts(self, kind=None):
        rows = self.db.recent_alerts(200)
        return [r for r in rows if kind is None or r["kind"] == kind]


class TestLedgerSuspectExitNotClosed(_TickBase):
    """closing / mismatch 行的离场事件：不关账、不发操作卡，只发 data_error。"""

    def _setup_ledger_row(self, hl_status):
        base = trend_then_flat()                                        # 末端横盘在 130
        last = base[-1]
        drop = S.Bar(open_ms=last.open_ms + STEP, open=130.0, high=130.0,
                     low=129.0, close=125.0)                            # 收盘破 SMA20，未碰止损
        bars = base + [drop, flat_bar(drop, 125.0)]
        pid = self.db.create_position(symbol=SYM, signal_bar_ms=last.open_ms - 5 * STEP,
                                      side=1, entry_price=130.0, stop_price=110.0,
                                      stop_dist=0.15, contracts=1, coin_qty=0.001,
                                      notional=130.0, risk_u=20.0, equity_at_entry=1000.0)
        self.db.confirm_position(pid, 130.0, 0.001, 110.0, True)
        self.db.set_pos_hl(pid, hl_status=hl_status)
        self.pid = pid
        self.bars = bars
        self.latest = len(bars) - 2                                     # drop bar
        return pid

    def _assert_untouched_and_warned(self):
        row = self.db.open_position(SYM)
        self.assertIsNotNone(row, "账本待核对的行不得被本地关账")
        self.assertEqual(row["status"], "open")
        self.assertEqual(self._alerts("exit"), [], "不得发可执行离场卡")
        de = self._alerts("data_error")
        self.assertEqual(len(de), 1, "只发一条 data_error")
        self.assertIn("账本待核对", de[0]["message"])
        self.assertEqual(len(self.notifier.msgs), 1)
        self.assertIn("账本待核对", self.notifier.msgs[0])
        self.assertNotIn("信号（4h 收盘", self.notifier.msgs[0])         # 不是开仓卡
        self.assertNotIn("市价平仓", self.notifier.msgs[0])              # 不是离场操作卡

    def test_closing_row_not_closed(self):
        self._setup_ledger_row("closing")
        self._tick(self.bars[-1].open_ms, executor=_Exec(on_exit_ret=False), bars=self.bars)
        self._assert_untouched_and_warned()

    def test_mismatch_row_not_closed(self):
        self._setup_ledger_row("mismatch")
        self._tick(self.bars[-1].open_ms, executor=_Exec(on_exit_ret=False), bars=self.bars)
        self._assert_untouched_and_warned()

    def test_warning_is_idempotent_per_bar(self):
        """同一 symbol + 同一 bar 只提醒一次（回放/重试不得刷屏）。"""
        self._setup_ledger_row("closing")
        self._tick(self.bars[-1].open_ms, executor=_Exec(on_exit_ret=False), bars=self.bars)
        self.db.set_last_bar(SYM, self.bars[self.latest - 1].open_ms)    # 退回去重放同一根
        self._tick(self.bars[-1].open_ms, executor=_Exec(on_exit_ret=False), bars=self.bars)
        self.assertEqual(len(self.notifier.msgs), 1, "同一 bar 的账本告警必须只推一次")
        self.assertEqual(len(self._alerts("data_error")), 1)
        self.assertIsNotNone(self.db.open_position(SYM))

    def test_open_row_still_closes_locally(self):
        """（对照组）hl_status='open' 且执行器让出接管权 → 仍走本地关账 + 离场卡。"""
        self._setup_ledger_row("open")
        self._tick(self.bars[-1].open_ms, executor=_Exec(on_exit_ret=False), bars=self.bars)
        closed = {int(r["id"]): r for r in self.db.closed_positions(0.0)}
        self.assertIn(int(self.pid), closed, "open 行必须照旧本地关账")
        self.assertEqual(len(self._alerts("exit")), 1)


class TestEntryExceptionDoesNotSendCard(_TickBase):
    """on_entry 抛异常：只发 data_error，不发开仓卡；仓位行保持不动。"""

    def test_no_entry_card_on_executor_exception(self):
        bars, i = entry_setup()
        self.bars = bars
        ex = _Exec(on_entry_exc=RuntimeError("db write failed after fill"))
        self._tick(bars[-1].open_ms, executor=ex, bars=bars)

        self.assertEqual(len(ex.entry_calls), 1)
        self.assertEqual(self._alerts("entry"), [], "执行器开仓异常时不得发手工开仓卡")
        de = self._alerts("data_error")
        self.assertEqual(len(de), 1)
        self.assertIn("执行器开仓时异常", de[0]["message"])
        self.assertEqual(len(self.notifier.msgs), 1)
        self.assertNotIn("信号（4h 收盘", self.notifier.msgs[0], "不得出现开仓卡正文")

        row = self.db.open_position(SYM)
        self.assertIsNotNone(row, "仓位行交给对账接手，不得删/关")
        self.assertEqual(row["status"], "pending")
        self.assertEqual(int(row["signal_bar_ms"]), bars[i].open_ms)

    def test_handled_entry_sends_no_card(self):
        """（对照组）执行器正常接管时不发卡、也不发 data_error。"""
        bars, _ = entry_setup()

        class _Taken(_Exec):
            def on_entry(self, pid):
                self.entry_calls.append(pid)
                return True

        self._tick(bars[-1].open_ms, executor=_Taken(), bars=bars)
        self.assertEqual(self.notifier.msgs, [])
        self.assertEqual(self._alerts("entry"), [])


# ---------------------------------------------------------------- R3-4（MR 会话）

def _hourly(date_str, times, px=100.0):
    """ET 日期 + [(h, m)] → 升序 1h candle 行（OHLC 自洽、每根 +1 元）。"""
    tz = ZoneInfo(M.SESSION_TZ)
    out = []
    for (h, m) in times:
        d = datetime.strptime(date_str, "%Y-%m-%d").replace(hour=h, minute=m, tzinfo=tz)
        out.append({"t": int(d.timestamp() * 1000), "o": px, "h": px * 1.001,
                    "l": px * 0.999, "c": px})
        px += 1.0
    return out


FULL_DAY = [(10, 0), (11, 0), (12, 0), (13, 0), (14, 0), (15, 0)]


class TestSessionCalendar(unittest.TestCase):
    def test_holiday_produces_no_session(self):
        """2026-09-07 劳动节：6 根「完整」小时 bar 也不得产出会话。"""
        self.assertTrue(M.is_market_holiday("2026-09-07"), "夹具前提：劳动节是 NYSE 休市日")
        sessions, dropped = M.aggregate_sessions_ex(_hourly("2026-09-07", FULL_DAY))
        self.assertEqual(sessions, [], "假日不是交易日 → 一个会话都不该有")
        self.assertEqual(dropped, [], "假日 bar 直接不入桶，不算「被丢弃的会话」")

    def test_early_close_drops_post_close_bar(self):
        """2026-11-27 半日市（13:00 收盘）：13:00 盘后 bar 不得当收盘。"""
        rows = _hourly("2026-11-27", [(10, 0), (11, 0), (12, 0), (13, 0)])
        sessions, dropped = M.aggregate_sessions_ex(rows)
        self.assertEqual(sessions, [])
        self.assertEqual(len(dropped), 1)
        self.assertEqual(dropped[0]["date"], "2026-11-27")
        self.assertEqual(dropped[0]["bars"], 3, "只算 09:30–13:00 内的 3 根")
        self.assertEqual(dropped[0]["last_bar_et"], "12:00", "末根是 12:00，不是 13:00")
        self.assertEqual(dropped[0]["reason"], "too_few_bars")

    def test_early_close_min_last_bar_is_noon(self):
        """半日市末根门槛是 12:00（不是 15:00）。

        注意：真实半日市只有 3 根整点 bar（10/11/12），必然 < MIN_SESSION_BARS 而弃用。
        这里合成 5 根（含 09:30/11:30 非整点）**只为越过根数门槛**，单独验证末根判据：
        末根 12:00 应被认作合法收盘，11:00 才算 missing_close_bar。
        """
        ok_rows = _hourly("2026-12-24", [(9, 30), (10, 0), (11, 0), (11, 30), (12, 0), (13, 0)])
        sessions, dropped = M.aggregate_sessions_ex(ok_rows)
        self.assertEqual(dropped, [])
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].last_bar_et, "12:00")
        self.assertEqual(sessions[0].bars, 5, "13:00 盘后 bar 不计入")

        late_rows = _hourly("2026-12-24", [(9, 30), (10, 0), (11, 0), (11, 30), (11, 45),
                                          (13, 0)])
        sessions, dropped = M.aggregate_sessions_ex(late_rows)
        self.assertEqual(sessions, [])
        self.assertEqual([d["reason"] for d in dropped], ["missing_close_bar"])
        self.assertEqual(dropped[0]["last_bar_et"], "11:45")

    def test_regular_day_unchanged(self):
        """（对照组）普通交易日：6 根 → 一个合法会话，末根 15:00。"""
        self.assertFalse(M.is_market_holiday("2026-11-25"))
        sessions, dropped = M.aggregate_sessions_ex(_hourly("2026-11-25", FULL_DAY))
        self.assertEqual(dropped, [])
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].last_bar_et, "15:00")
        self.assertEqual(sessions[0].bars, 6)


if __name__ == "__main__":
    unittest.main(verbosity=2)
