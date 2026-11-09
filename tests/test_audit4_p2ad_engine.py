"""第四轮审核 P2-A / P2-D 的引擎定向回归（只覆盖 `app/engine.py` 的两处修复）。

P2-A **信号 bar 重放不得用入场前的 wick 判止损**
    崩溃重放时 `bar_state` 可能落后到信号 bar 或更早（进程死在入场 tick 里、
    `set_last_bar` 失败、其后二次异常），旧 `walk` 对任意根都判 low/high →
    拿**入场前**那根产出的 `exit('stop')` 会撤止损 + 市价平掉一个健康仓。
    修复后离场判定只从入场根（signal_bar_ms + 1 个周期）开始；
    **PR-14 的「入场根允许 MA20 离场」不能因此回退**。

P2-D **离场异常后不得丢信号**
    旧 `_tick_symbol` 捕获 `executor.on_exit` 异常后照样 `set_last_bar`，下一轮从下一根
    开始 → 这一根的离场信号永久不再评估。修复：不推进 bar_state + 落持久化恢复态
    （`configuration.exit_retry:<symbol>`），下一轮从同一根重评；重评前用执行器的
    exit 意图行确认「平仓单没发出」，防止重复下单。

零网络：`market.fetch` 全程打桩；bar 时间戳写死。DB 用临时文件。
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import engine, market                                       # noqa: E402
from app import strategy as S                                       # noqa: E402
from app.db import DB                                               # noqa: E402
from app.hl import PURPOSE_EXIT                                     # noqa: E402
from tests.test_engine import cfg, entry_setup, prep                 # noqa: E402
from tests.test_strategy import STEP, flat_bar, trend_then_flat      # noqa: E402

SYM = "BTC_USDT"


def _exits(events):
    return [e for e in events if e.kind == "exit"]


# ================================================================ P2-A（walk，纯函数）

class TestReplayDoesNotUsePreEntryWick(unittest.TestCase):
    """信号 bar 自身 / 更早的根都不得产出离场事件。"""

    def _setup(self):
        """入场信号 + 把**信号 bar 的 low** 打到止损下方（全历史 1.8% 的信号就是这样）。

        返回 (cfg, bars, sig_i, pos)：`pos` 是 `walk` 正常产出的仓位（entry_price =
        信号 bar 次根开盘价）。
        """
        bars, i = entry_setup()
        c = cfg()
        ind, sq = prep(bars)
        _, pos = engine.walk(c, SYM, bars, ind, sq, None, i, i, i)
        self.assertIsNotNone(pos)

        stop = pos.eff_stop
        self.assertGreater(bars[i].low, stop, "夹具前提：原始信号 bar 没碰止损")
        sig = S.Bar(open_ms=bars[i].open_ms, open=bars[i].open, high=bars[i].high,
                    low=stop * 0.995, close=bars[i].close, volume=bars[i].volume)
        self.assertLess(sig.low, stop, "夹具前提：信号 bar 的 wick 已穿过自己的止损")
        entry = flat_bar(sig, sig.close)          # 入场根：不碰止损、收盘不破均线
        bars2 = bars[:i] + [sig, entry, flat_bar(entry, entry.close)]
        ind2, sq2 = prep(bars2)
        self.assertEqual(sig.open_ms + STEP, entry.open_ms)
        return c, bars2, i, pos, ind2, sq2, stop

    def test_normal_progress_skips_signal_bar(self):
        """① bar_state = 信号 bar（正常）→ from_i = 入场根：不因信号 bar 的 wick 假止损。"""
        c, bars, i, pos, ind, sq, stop = self._setup()
        latest = i + 1                                   # 入场根
        events, pos2 = engine.walk(c, SYM, bars, ind, sq, pos, latest, latest, latest)
        self.assertEqual(_exits(events), [], "信号 bar 不在扫描窗口内，不得产出离场")
        self.assertIsNotNone(pos2)

    def test_replay_from_signal_bar_ignores_its_own_wick(self):
        """② bar_state = 信号 bar - 1（重放）→ from_i = 信号 bar：仍不得用它判止损。"""
        c, bars, i, pos, ind, sq, stop = self._setup()
        latest = i + 1
        events, pos2 = engine.walk(c, SYM, bars, ind, sq, pos, i, latest, latest)
        self.assertEqual(_exits(events), [],
                         "重放不得用入场前的 low 判止损（旧代码在这里 exit/stop）")
        self.assertIsNotNone(pos2, "信号 bar 上的假离场不得把仓位回收")

    def test_replay_from_earlier_bars_also_safe(self):
        """② 补：bar_state 再落后一根（from_i = 信号 bar - 2）同样不产出假离场。"""
        c, bars, i, pos, ind, sq, stop = self._setup()
        latest = i + 1
        events, pos2 = engine.walk(c, SYM, bars, ind, sq, pos, i - 2, latest, latest)
        self.assertEqual(_exits(events), [])
        self.assertIsNotNone(pos2)

    def test_signal_bar_ma20_break_is_not_an_exit(self):
        """② 补：信号 bar 自己的收盘也不得判 MA20 离场（那时仓位还不存在）。"""
        bars = trend_then_flat()
        last = bars[-1]
        sig = S.Bar(open_ms=last.open_ms, open=130.0, high=130.0, low=129.6, close=129.0)
        bars2 = bars[:-1] + [sig, flat_bar(sig, 129.0)]
        ind, sq = prep(bars2)
        latest = len(bars2) - 2                          # sig
        self.assertLess(sig.close, ind["sma20"][latest], "夹具前提：收盘确实破 SMA20")
        pos = engine.PositionState(symbol=SYM, entry_price=130.0, stop_price=120.0,
                                   contracts=1, signal_bar_ms=sig.open_ms, status="open")
        events, pos2 = engine.walk(cfg(), SYM, bars2, ind, sq, pos, latest, latest, latest)
        self.assertEqual(events, [], "信号 bar 上不得产出任何离场事件")
        self.assertIsNotNone(pos2)

    def test_entry_bar_stop_still_fires(self):
        """④ 入场根的止损照常生效（守卫不能把入场根一起压掉）。"""
        bars, i = entry_setup()
        ind, sq = prep(bars)
        c = cfg()
        _, pos = engine.walk(c, SYM, bars, ind, sq, None, i, i, i)
        stop = pos.eff_stop
        entry = S.Bar(open_ms=bars[i].open_ms + STEP, open=bars[i].close, high=bars[i].close,
                      low=stop * 0.99, close=bars[i].close)          # 盘中穿透止损，收盘仍高
        bars2 = bars[:i + 1] + [entry, flat_bar(entry, entry.close)]
        ind2, sq2 = prep(bars2)
        latest = i + 1
        events, _ = engine.walk(c, SYM, bars2, ind2, sq2, pos, latest, latest, latest)
        exit_evs = _exits(events)
        self.assertEqual([(e.kind, e.reason) for e in exit_evs], [("exit", "stop")])
        self.assertAlmostEqual(exit_evs[0].exit_price, stop)
        self.assertEqual(exit_evs[0].bars_held, 1, "入场根 = 持有 1 根")

    def test_entry_bar_ma20_exit_still_fires(self):
        """③ 入场根的 MA20 离场照常触发（PR-14 对齐，不能回退）。"""
        bars, i = entry_setup()
        ind, sq = prep(bars)
        c = cfg()
        _, pos = engine.walk(c, SYM, bars, ind, sq, None, i, i, i)
        pos.stop_price = pos.entry_price * 0.5            # 只可能由 MA20 触发
        px = bars[i].close
        entry = S.Bar(open_ms=bars[i].open_ms + STEP, open=px, high=px,
                      low=px * 0.90, close=px * 0.90)
        bars2 = bars[:i + 1] + [entry, flat_bar(entry, entry.close)]
        ind2, sq2 = prep(bars2)
        latest = i + 1
        self.assertEqual(entry.open_ms - pos.signal_bar_ms, engine.INTERVAL_MS)
        self.assertLess(entry.close, ind2["sma20"][latest])
        self.assertGreater(entry.low, pos.eff_stop)
        events, _ = engine.walk(c, SYM, bars2, ind2, sq2, pos, latest, latest, latest)
        self.assertEqual(events[0].kind, "exit")
        self.assertEqual(events[0].reason, "ma20")
        self.assertAlmostEqual(events[0].exit_price, entry.close)
        self.assertEqual(events[0].bars_held, 1)

    def test_later_bars_stop_detection_unaffected(self):
        """④ 入场根之后各根的止损判定不受影响。"""
        bars, i = entry_setup()
        ind, sq = prep(bars)
        c = cfg()
        _, pos = engine.walk(c, SYM, bars, ind, sq, None, i, i, i)
        stop = pos.eff_stop
        px = bars[i].close
        b1 = flat_bar(bars[i], px)                                  # 入场根（干净）
        b2 = S.Bar(open_ms=b1.open_ms + STEP, open=px, high=px, low=stop * 0.99, close=px)
        bars2 = bars[:i + 1] + [b1, b2, flat_bar(b2, px)]
        ind2, sq2 = prep(bars2)
        latest = i + 2
        events, _ = engine.walk(c, SYM, bars2, ind2, sq2, pos, i + 1, latest, latest)
        self.assertEqual([(e.kind, e.reason) for e in events], [("exit", "stop")])
        self.assertEqual(events[0].bars_held, 2)


# ================================================================ P2-D（tick 级）

class _Notifier:
    def __init__(self):
        self.msgs = []

    def send_message(self, m):
        self.msgs.append(m)
        return True


class _Exec:
    """执行器替身。``fail_times`` 次 on_exit 抛异常（模拟 specs/DB 抛）；成功后按真实
    执行器的语义**自己关账**（引擎不关），这样「重评是否收口」才测得出来。"""

    def __init__(self, db=None, fail_times=0, handled=True, exc=None):
        self.db = db
        self.fail_times = fail_times
        self.handled = handled
        self.exc = exc or RuntimeError("info.specs() 抛异常（P2-D 的典型窗口）")
        self.exit_calls = []
        self.entry_calls = []

    def run(self):
        pass

    def on_exit(self, pid, reason, ref_price):
        self.exit_calls.append((pid, reason, float(ref_price)))
        if self.fail_times > 0:
            self.fail_times -= 1
            raise self.exc
        if self.handled and self.db is not None:
            self.db.close_position(pid, float(ref_price), reason, auto=False)
            self.db.set_pos_hl(pid, hl_status="closed")
        return self.handled

    def on_entry(self, pid):
        self.entry_calls.append(pid)
        return False


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

    def _tick(self, executor=None):
        return engine.tick(self.db, self.c, self.notifier, now_ms=self.feed[-1].open_ms,
                           executor=executor)

    def _alerts(self, kind=None):
        return [r for r in self.db.recent_alerts(200) if kind is None or r["kind"] == kind]

    def _open_row(self, signal_bar_ms, entry=130.0, stop=120.0):
        pid = self.db.create_position(symbol=SYM, signal_bar_ms=signal_bar_ms, side=1,
                                      entry_price=entry, stop_price=stop, stop_dist=0.07,
                                      contracts=1, coin_qty=0.001, notional=130.0,
                                      risk_u=10.0, equity_at_entry=1000.0)
        self.db.confirm_position(pid, entry, 0.001, stop, True)
        return pid

    def _n_positions(self):
        return int(self.db.conn.execute("SELECT COUNT(*) FROM positions WHERE symbol=?",
                                        (SYM,)).fetchone()[0])

    def _retry_key(self):
        return engine.EXIT_RETRY_KEY + SYM

    def _retry_state(self):
        raw = self.db.get_config().get(self._retry_key())
        return json.loads(raw) if raw else None


class TestExitExceptionKeepsSignal(_TickBase):
    """P2-D：on_exit 异常后 bar 进度不推进，下一轮从同一根重评（且只重发一次）。"""

    def _ma20_exit(self):
        """末端一根收盘破 SMA20（未碰止损）→ walk 产出 exit(ma20)。"""
        base = trend_then_flat()
        last = base[-1]
        drop = S.Bar(open_ms=last.open_ms + STEP, open=130.0, high=130.0,
                     low=128.5, close=129.0)
        bars = base + [drop, flat_bar(drop, 129.0)]
        latest = len(bars) - 2                                   # drop
        pid = self._open_row(drop.open_ms - 3 * STEP)
        self.db.set_last_bar(SYM, bars[latest - 1].open_ms)      # from_i = drop
        self.feed = bars
        return bars, latest, pid

    def test_bar_not_advanced_and_same_exit_replayed(self):
        bars, latest, pid = self._ma20_exit()
        ex = _Exec(self.db, fail_times=1)
        self._tick(executor=ex)
        self.assertEqual(len(ex.exit_calls), 1, "本轮应尝试过一次离场")
        self.assertEqual(ex.exit_calls[0][0], pid)
        self.assertEqual(ex.exit_calls[0][1], "ma20")
        self.assertAlmostEqual(ex.exit_calls[0][2], 129.0)

        row = self.db.open_position(SYM)
        self.assertIsNotNone(row, "异常路径不得关账")
        self.assertEqual(row["status"], "open")
        self.assertEqual(self.db.get_last_bar(SYM), bars[latest - 1].open_ms,
                         "on_exit 异常时 bar_state 必须停在原地")
        mk = self._retry_state()
        self.assertIsNotNone(mk, "必须落持久化离场恢复态")
        self.assertEqual(int(mk["pid"]), pid)
        self.assertEqual(int(mk["bar_ms"]), bars[latest].open_ms)
        self.assertEqual(mk["reason"], "ma20")
        de = self._alerts("data_error")
        self.assertEqual(len(de), 1)
        self.assertIn("执行器离场时异常", de[0]["message"])
        self.assertEqual(self._alerts("exit"), [], "异常路径不发人工离场卡")

        ex2 = _Exec(self.db)
        self._tick(executor=ex2)
        self.assertEqual(len(ex2.exit_calls), 1, "下一轮必须重评**同一个**离场信号")
        self.assertEqual(ex2.exit_calls[0][0], pid)
        self.assertEqual(ex2.exit_calls[0][1], "ma20")
        self.assertAlmostEqual(ex2.exit_calls[0][2], 129.0)
        self.assertIsNone(self.db.open_position(SYM), "重评成功应让执行器关账")
        self.assertIsNone(self._retry_state(), "收口后恢复态必须消失")
        self.assertEqual(self.db.get_last_bar(SYM), bars[latest].open_ms,
                         "收口后 bar 进度推进到最新已收盘 bar")
        self.assertEqual(self._alerts("exit"), [], "执行器接管时不发人工离场卡")

    def test_still_failing_holds_bar_and_dedups_alerts(self):
        bars, latest, pid = self._ma20_exit()
        ex = _Exec(self.db, fail_times=9)
        self._tick(executor=ex)
        self._tick(executor=ex)
        self.assertEqual(len(ex.exit_calls), 2, "两轮各重评一次")
        self.assertEqual(self.db.get_last_bar(SYM), bars[latest - 1].open_ms)
        mk = self._retry_state()
        self.assertIsNotNone(mk)
        # 首轮：write-ahead 1 次 + 异常补记 1 次；第二轮重评再 +1
        self.assertEqual(int(mk["tries"]), 3)
        self.assertIn("RuntimeError", mk["last_error"])
        # 同一 bar 的重评告警只推一次（_emit 幂等），首轮那条是 NULL bar 的另一条
        bar_alerts = [r for r in self._alerts("data_error")
                      if r["bar_ms"] == bars[latest].open_ms]
        self.assertEqual(len(bar_alerts), 1)
        self.assertIsNotNone(self.db.open_position(SYM))

    def test_write_ahead_marker_covers_process_death(self):
        """进程死在 on_exit 内部（没有走到异常分支）：写前落库的恢复态同样挡住重复平仓。"""
        bars, latest, pid = self._ma20_exit()
        ev = engine.Event(kind="exit", symbol=SYM, bar_ms=bars[latest].open_ms,
                          is_latest=True, exit_price=129.0, reason="ma20", bars_held=1)
        engine._exit_retry_save(self.db, SYM, pid, ev)          # 调用前就落库
        # 死之前的动作：place_order 前的 exit 意图行已经在台账里（status=sent）
        self.db.hl_record_intent(SYM, "PF_XBTUSD", bars[latest].open_ms, PURPOSE_EXIT, 1,
                                 0.001, px=129.0, reduce_only=True, status="sent",
                                 cloid="c-dead-1")
        ex = _Exec(self.db)
        self._tick(executor=ex)
        self.assertEqual(ex.exit_calls, [], "不能凭重放再发一张平仓单")
        self.assertEqual(self.db.get_last_bar(SYM), bars[latest - 1].open_ms,
                         "未确认前 bar 进度不推进")
        self.assertIsNotNone(self.db.open_position(SYM))
        self.assertIsNotNone(self._retry_state())

    def test_exit_order_seen_blocks_resend(self):
        """平仓单可能已发出（exit 意图行在场）→ 绝不重发，只告警并等对账。"""
        bars, latest, pid = self._ma20_exit()
        self._tick(executor=_Exec(self.db, fail_times=1))
        self.assertEqual(self.db.get_last_bar(SYM), bars[latest - 1].open_ms)

        # 模拟 executor 在 place_order 前落库、随后抛异常：行停在 sent。
        self.db.hl_record_intent(SYM, "PF_XBTUSD", bars[latest].open_ms, PURPOSE_EXIT, 1,
                                 0.001, px=129.0, reduce_only=True, status="sent",
                                 cloid="c-exit-1")
        ex2 = _Exec(self.db)
        self._tick(executor=ex2)
        self.assertEqual(ex2.exit_calls, [], "exit 意图行在场时**不得**再发平仓单")
        self.assertEqual(self.db.get_last_bar(SYM), bars[latest - 1].open_ms,
                         "未确认前 bar 进度不推进")
        self.assertIsNotNone(self._retry_state(), "未确认前恢复态保留")
        self.assertIsNotNone(self.db.open_position(SYM))
        held = [r for r in self._alerts("data_error") if "不重发平仓单" in r["message"]]
        self.assertEqual(len(held), 1)

        # 意图行转 failed（证明没成交）→ 下一轮重新允许重评
        self.db.hl_update_order(SYM, bars[latest].open_ms, PURPOSE_EXIT, status="failed")
        ex3 = _Exec(self.db)
        self._tick(executor=ex3)
        self.assertEqual(len(ex3.exit_calls), 1)
        self.assertIsNone(self.db.open_position(SYM))
        self.assertIsNone(self._retry_state())
        self.assertEqual(self.db.get_last_bar(SYM), bars[latest].open_ms)

    def test_row_closed_elsewhere_clears_retry_without_exit_call(self):
        """异常之后执行器/对账自己关掉了行 → 只清恢复态、推进 bar，不再调 on_exit。"""
        bars, latest, pid = self._ma20_exit()
        self._tick(executor=_Exec(self.db, fail_times=1))
        self.db.close_position(pid, 129.5, "ma20", auto=False)
        self.db.set_pos_hl(pid, hl_status="closed")

        ex2 = _Exec(self.db)
        self._tick(executor=ex2)
        self.assertEqual(ex2.exit_calls, [], "行已关，不得再驱动一次离场")
        self.assertIsNone(self._retry_state())
        self.assertEqual(self.db.get_last_bar(SYM), bars[latest].open_ms)
        self.assertEqual(self._alerts("entry"), [], "恢复收口那一轮不得补发入场卡")

    def test_no_executor_falls_back_to_normal_flow(self):
        """提醒模式（executor=None）：恢复态交回普通流程，walk 重放同一根。"""
        bars, latest, pid = self._ma20_exit()
        self._tick(executor=_Exec(self.db, fail_times=1))
        self.assertIsNotNone(self._retry_state())
        # 第二轮回退成纯提醒流：同一根 bar 的离场信号必须被重新评估 → 本地关账 + 离场卡
        self._tick(executor=None)
        self.assertIsNone(self._retry_state())
        self.assertIsNone(self.db.open_position(SYM))
        self.assertEqual(len(self._alerts("exit")), 1, "普通流程照旧发离场卡")
        self.assertEqual(self.db.get_last_bar(SYM), bars[latest].open_ms)


class TestExitExceptionSameTickEntry(_TickBase):
    """异常 + 同窗口反手入场：重评只能驱动**出事那一笔**，不得重复入场/多建行。"""

    def _stop_then_entry(self):
        base = trend_then_flat()
        last = base[-1]
        k = S.Bar(open_ms=last.open_ms + STEP, open=130.0, high=130.0, low=118.0, close=130.0)
        bo = S.Bar(open_ms=k.open_ms + STEP, open=130.0, high=136.0, low=130.0, close=136.0)
        bars = base + [k, bo, flat_bar(bo, 136.0)]
        latest = len(bars) - 2                                     # bo
        pid = self._open_row(k.open_ms - 3 * STEP)
        self.db.set_last_bar(SYM, bars[-4].open_ms)                # from_i = k
        self.feed = bars
        return bars, latest, pid

    def test_retry_does_not_replay_entry(self):
        bars, latest, pid = self._stop_then_entry()
        ex = _Exec(self.db, fail_times=1)
        self._tick(executor=ex)
        self.assertEqual(len(ex.exit_calls), 1)
        self.assertEqual(ex.exit_calls[0][1], "stop")
        self.assertEqual(len(ex.entry_calls), 1, "同窗口的反手入场已经走过一次")
        self.assertEqual(len(self._alerts("entry")), 1)
        self.assertEqual(self._n_positions(), 2, "旧仓行 + 新仓行")
        old = self.db.position_by_id(pid)
        self.assertEqual(old["status"], "open", "异常路径不得关账")
        new = self.db.open_position(SYM)
        self.assertIsNotNone(new)
        self.assertEqual(new["status"], "pending")
        self.assertEqual(int(new["signal_bar_ms"]), bars[latest].open_ms)

        ex2 = _Exec(self.db)
        self._tick(executor=ex2)
        self.assertEqual(len(ex2.exit_calls), 1, "重评只驱动出事那一笔离场")
        self.assertEqual(ex2.exit_calls[0][0], pid)
        self.assertEqual(ex2.entry_calls, [], "重评不得再驱动一次入场")
        self.assertEqual(len(self._alerts("entry")), 1, "不得重复发入场卡")
        self.assertEqual(self._n_positions(), 2, "不得建第三行仓位")
        self.assertEqual(self.db.position_by_id(pid)["status"], "closed")
        still = self.db.open_position(SYM)
        self.assertIsNotNone(still, "新仓（pending）不得被恢复流程误动")
        self.assertEqual(still["status"], "pending")
        self.assertEqual(self.db.get_last_bar(SYM), bars[latest].open_ms)


if __name__ == "__main__":
    unittest.main()
