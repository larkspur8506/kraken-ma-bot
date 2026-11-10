"""第四轮审核 P2-E 回归：MR 新仓必须受**组合总名义上限**约束。

背景（`/opt/data/weekend-run/phase3/kraken-r4-audit.md` P2-E）：

* 主策略 `executor._entry_gates`（`app/hl/executor.py:2352-2355`）用
  「全账户 `snap.notional` + `plan.notional`」对 `HL_MAX_TOTAL_NOTIONAL_MULT`
  （生产 2.0x）设限；
* MR 旧实现 `_maybe_enter` 只按 `base = equity_total - mr_margin_used`、
  `notional = base * cfg.exposure`（0.5x）定仓，完全不读全账户名义 ⇒ 主策略先到
  2.0x 后 MR 仍可再加 0.5x（组合 2.5x）。反向顺序被主策略闸门拦住，偏差单向「只超不收」。

修复口径：定仓完成、下单之前复用**同一个**上限倍数（同一个 env
`HL_MAX_TOTAL_NOTIONAL_MULT`，即 `ExecParams.max_total_notional_mult` 的来源），
读 `info.user_state()["marginSummary"]["totalNtlPos"]`（含 MR 与主策略全部持仓）；
超限写 SKIPPED 台账 + 告警；读不到 totalNtlPos / 权益则 fail-closed（本轮不下单）。

钉住的东西：
  ① 接近上限 → 跳过、写 SKIPPED、零下单；
  ② 空账户（total=0）→ 正常放行；
  ③ 读不到名义（抛错 / 缺键 / 负数 / NaN）→ 跳过且不下单，单次读失败不烧掉当天信号；
  ④ `HlMrConfig` 新字段在**末尾**，位置参数构造不错位；未配置上限时**不额外读账户**
     （老夹具没有 `user_state` 也不能回归）。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from contextlib import contextmanager
from dataclasses import fields

from app.db import DB
from app.hl.mr import (TOTAL_NOTIONAL_MULT_ENV, HlMrConfig, HlMrLeg,
                       parse_total_notional_mult)

# 复用既有的 MR 夹具（同一套合成行情 / 假交易所），避免两份行情漂移。
from tests.test_mr_hl import (ADDR, CANDLES, ENTRY_NOW_MS, FakeExchange, FakeInfo)

ADDR_ = ADDR
_MISSING = object()


@contextmanager
def _env(value=_MISSING):
    """临时设置/清除组合名义上限 env（与主策略共用的那个键）。"""
    old = os.environ.get(TOTAL_NOTIONAL_MULT_ENV)
    try:
        if value is _MISSING:
            os.environ.pop(TOTAL_NOTIONAL_MULT_ENV, None)
        else:
            os.environ[TOTAL_NOTIONAL_MULT_ENV] = str(value)
        yield
    finally:
        if old is None:
            os.environ.pop(TOTAL_NOTIONAL_MULT_ENV, None)
        else:
            os.environ[TOTAL_NOTIONAL_MULT_ENV] = old


class _StateInfo(FakeInfo):
    """在 FakeInfo 上加 `user_state()`（真实 KrInfo 的返回形状）。"""

    def __init__(self, *, total_ntl=0.0, state=None, raises=None, **kw):
        super().__init__(**kw)
        self.total_ntl = total_ntl
        self.state = state
        self.raises = raises
        self.user_state_calls = 0

    def user_state(self, address: str) -> dict:
        self.user_state_calls += 1
        if self.raises is not None:
            raise self.raises
        if self.state is not None:
            return self.state
        return {"marginSummary": {"accountValue": str(self.equity),
                                  "totalMarginUsed": "0",
                                  "totalNtlPos": str(self.total_ntl)},
                "assetPositions": list(
                    (self.user_state_dex(address, "kr") or {}).get("assetPositions") or [])}


def _leg(tmp: str, *, cap=_MISSING, config=None, plain_info=False, **info_kw):
    """构造 MR 腿。`cap=_MISSING` → 用 HlMrConfig 默认（None ⇒ 读 env）。"""
    db = DB(os.path.join(tmp, "audit4_p2_mr.db"))
    info = FakeInfo(**info_kw) if plain_info else _StateInfo(**info_kw)
    ex = FakeExchange()
    ex.info = info
    if config is None:
        kw = {} if cap is _MISSING else {"max_total_notional_mult": cap}
        config = HlMrConfig(fill_confirm_s=0.0, **kw)
    leg = HlMrLeg(db=db, info=info, exchange=ex, address_fn=lambda: ADDR_,
                  config=config, clock=lambda: ENTRY_NOW_MS / 1000.0)
    return leg, db, ex, info


def _enable(db: DB) -> None:
    db.set_config("mr_enabled", "1")


def _skipped_rows(db: DB):
    return [r for r in db.mr_recent_trades(5) if r["state"] == "SKIPPED"]


class TestGateBlocksNearCap(unittest.TestCase):
    """① 主策略已把组合名义推到接近 2.0x：MR 不许再按 0.5x 叠加。"""

    def test_skips_and_records_skipped_ledger(self):
        with tempfile.TemporaryDirectory() as tmp, _env():
            # 权益 500、上限 2.0x ⇒ cap 1000U；主策略已占 800U；MR 计划 250U。
            leg, db, ex, info = _leg(tmp, cap=2.0, equity=500.0, total_ntl=800.0)
            info.now_ms = ENTRY_NOW_MS
            _enable(db)
            status = leg.tick(now_ms=ENTRY_NOW_MS)

            self.assertEqual(ex.orders, [], "超总名义上限时不得发任何单")
            self.assertEqual(getattr(ex, "leverage_calls", []), [],
                             "被闸门拦下时不该再去设杠杆")
            self.assertEqual(status["state"], "skipped_total_notional")
            self.assertIn("组合名义将达", status["error"])
            self.assertAlmostEqual(status["total_notional"], 800.0, places=6)
            self.assertAlmostEqual(status["total_notional_cap"], 1000.0, places=6)

            rows = _skipped_rows(db)
            self.assertEqual(len(rows), 1, "必须落 SKIPPED 台账（可审计）")
            self.assertEqual(rows[0]["signal_date"], "2026-04-15")
            self.assertIn("组合名义", rows[0]["exit_reason"] or "")
            self.assertIsNone(db.mr_open_trade(), "跳过时不能留下 OPEN 台账")

            # 下单意图都不该写（否则下一轮认领会把没发的单当「未知成交」）
            self.assertEqual(db.hl_recent_events(5, kind="mr_entry_intent"), [])
            alerts = [r["message"] for r in db.hl_recent_events(10, kind="mr")]
            self.assertTrue(any("组合总名义上限" in (m or "") for m in alerts),
                            f"应推送告警，实际 mr 事件：{alerts}")

    def test_second_tick_same_signal_stays_skipped(self):
        with tempfile.TemporaryDirectory() as tmp, _env():
            leg, db, ex, info = _leg(tmp, cap=2.0, equity=500.0, total_ntl=900.0)
            info.now_ms = ENTRY_NOW_MS
            _enable(db)
            leg.tick(now_ms=ENTRY_NOW_MS)
            leg.tick(now_ms=ENTRY_NOW_MS + 300_000)
            self.assertEqual(ex.orders, [])
            self.assertEqual(len(_skipped_rows(db)), 1)

    def test_reverse_order_is_consistent_if_mr_already_holds(self):
        """MR 已持仓（total 含 MR 自身名义）时由主策略侧闸门兜住 —— 这里只挡**新仓**。"""
        with tempfile.TemporaryDirectory() as tmp, _env():
            leg, db, ex, info = _leg(tmp, cap=2.0, equity=500.0, total_ntl=1010.0)
            info.now_ms = ENTRY_NOW_MS
            _enable(db)
            status = leg.tick(now_ms=ENTRY_NOW_MS)
            self.assertEqual(ex.orders, [])
            self.assertEqual(status["state"], "skipped_total_notional")


class TestGateAllowsWhenRoom(unittest.TestCase):
    """② 空账户 / 恰好等于上限 → 放行（与主策略 `>` 同一边界）。"""

    def test_flat_account_places_order(self):
        with tempfile.TemporaryDirectory() as tmp, _env():
            leg, db, ex, info = _leg(tmp, cap=2.0, equity=500.0, total_ntl=0.0)
            info.now_ms = ENTRY_NOW_MS
            _enable(db)
            status = leg.tick(now_ms=ENTRY_NOW_MS)

            self.assertEqual(status["state"], "entered")
            self.assertEqual(len(ex.orders), 1)
            self.assertEqual(ex.orders[0]["sz"], 2.68)      # 250 / 93.0 → floor 2 位
            self.assertIsNotNone(db.mr_open_trade())
            self.assertEqual(_skipped_rows(db), [])

    def test_exactly_at_cap_is_allowed(self):
        """750 + 250 == 1000 == cap：主策略闸门是 `>`，这里必须一致（不早拦）。"""
        with tempfile.TemporaryDirectory() as tmp, _env():
            leg, db, ex, info = _leg(tmp, cap=2.0, equity=500.0, total_ntl=750.0)
            info.now_ms = ENTRY_NOW_MS
            _enable(db)
            status = leg.tick(now_ms=ENTRY_NOW_MS)
            self.assertEqual(status["state"], "entered")
            self.assertEqual(len(ex.orders), 1)

    def test_cap_comes_from_shared_env_when_config_untouched(self):
        """生产装配：main._build_mr_leg 不传该参数 ⇒ 读 HL_MAX_TOTAL_NOTIONAL_MULT。"""
        with tempfile.TemporaryDirectory() as tmp, _env("2.0"):
            leg, db, ex, info = _leg(tmp, equity=500.0, total_ntl=800.0)  # cap 默认 None
            info.now_ms = ENTRY_NOW_MS
            _enable(db)
            status = leg.tick(now_ms=ENTRY_NOW_MS)
            self.assertEqual(status["state"], "skipped_total_notional", status)
            self.assertEqual(ex.orders, [])
            self.assertEqual(info.user_state_calls, 1)

    def test_explicit_config_value_wins_over_env(self):
        with tempfile.TemporaryDirectory() as tmp, _env("2.0"):
            leg, db, ex, info = _leg(tmp, cap=0.0, equity=500.0, total_ntl=800.0)
            self.assertEqual(leg.total_notional_mult(), 0.0)
            info.now_ms = ENTRY_NOW_MS
            _enable(db)
            status = leg.tick(now_ms=ENTRY_NOW_MS)
            self.assertEqual(status["state"], "entered")
            self.assertEqual(len(ex.orders), 1)
            self.assertEqual(info.user_state_calls, 0, "上限关闭时不该多打一次账户接口")


class TestGateFailClosed(unittest.TestCase):
    """③ 读不到账户名义 → 不下单；单次失败不烧掉当天唯一信号。"""

    def test_user_state_error_blocks_but_does_not_burn_signal(self):
        with tempfile.TemporaryDirectory() as tmp, _env():
            leg, db, ex, info = _leg(tmp, cap=2.0, equity=500.0,
                                     raises=RuntimeError("account 503"))
            info.now_ms = ENTRY_NOW_MS
            _enable(db)
            status = leg.tick(now_ms=ENTRY_NOW_MS)

            self.assertEqual(ex.orders, [], "读不到名义必须 fail-closed")
            self.assertEqual(status["state"], "total_notional_unknown")
            self.assertIn("读不到全账户名义", status["error"])
            self.assertEqual(_skipped_rows(db), [],
                             "短暂读失败不该写成 SKIPPED：当天还有重试 tick")
            self.assertEqual(db.hl_recent_events(5, kind="mr_entry_intent"), [])
            alerts = [r["message"] for r in db.hl_recent_events(10, kind="mr")]
            self.assertTrue(any("本轮不下单" in (m or "") for m in alerts), alerts)

            # 下一轮读到真相（空仓）→ 同一信号仍能正常入场
            info.raises = None
            status2 = leg.tick(now_ms=ENTRY_NOW_MS + 300_000)
            self.assertEqual(status2["state"], "entered", status2)
            self.assertEqual(len(ex.orders), 1)

    def test_missing_totalntlpos_blocks(self):
        with tempfile.TemporaryDirectory() as tmp, _env():
            leg, db, ex, info = _leg(
                tmp, cap=2.0, equity=500.0,
                state={"marginSummary": {"marginUsed": "0"}, "assetPositions": []})
            info.now_ms = ENTRY_NOW_MS
            _enable(db)
            status = leg.tick(now_ms=ENTRY_NOW_MS)
            self.assertEqual(ex.orders, [])
            self.assertEqual(status["state"], "total_notional_unknown")
            self.assertIn("totalNtlPos", status["error"])

    def test_negative_and_nan_total_block(self):
        for bad in ("-1", "nan"):
            with self.subTest(total=bad), tempfile.TemporaryDirectory() as tmp, _env():
                leg, db, ex, info = _leg(tmp, cap=2.0, equity=500.0,
                                         state={"marginSummary": {"totalNtlPos": bad}})
                info.now_ms = ENTRY_NOW_MS
                _enable(db)
                status = leg.tick(now_ms=ENTRY_NOW_MS)
                self.assertEqual(ex.orders, [], f"totalNtlPos={bad!r} 必须拦")
                self.assertEqual(status["state"], "total_notional_unknown")

    def test_unreadable_state_shape_blocks(self):
        with tempfile.TemporaryDirectory() as tmp, _env():
            leg, db, ex, info = _leg(tmp, cap=2.0, equity=500.0, state="garbage")
            info.now_ms = ENTRY_NOW_MS
            _enable(db)
            status = leg.tick(now_ms=ENTRY_NOW_MS)
            self.assertEqual(ex.orders, [])
            self.assertEqual(status["state"], "total_notional_unknown")


class TestConfigCompatibility(unittest.TestCase):
    """④ 不回归：新字段在末尾、位置参数不错位、未配置上限时行为完全不变。"""

    def test_new_field_is_last(self):
        names = [f.name for f in fields(HlMrConfig)]
        self.assertEqual(names[-1], "max_total_notional_mult")
        self.assertEqual(names[-2], "taker_fee_rate")

    def test_positional_construction_still_maps_fields(self):
        cfg = HlMrConfig("QQQ", "kr", 0.5, 10.0, 15, 500, 0.0004, 10.0, 0.10,
                         6 * 3600.0, 20.0, 10.0, 5, True, 12.0, 1.0, 0.000275)
        self.assertEqual((cfg.coin, cfg.dex), ("QQQ", "kr"))
        self.assertEqual(cfg.exposure, 0.5)
        self.assertEqual(cfg.leverage, 5)
        self.assertTrue(cfg.cross_margin)
        self.assertAlmostEqual(cfg.taker_fee_rate, 0.000275)
        self.assertIsNone(cfg.max_total_notional_mult)

    def test_no_cap_configured_never_reads_user_state(self):
        """老夹具/老配置没有 user_state 能力：上限关闭时必须完全不碰它。"""
        with tempfile.TemporaryDirectory() as tmp, _env():
            leg, db, ex, info = _leg(tmp, plain_info=True)      # FakeInfo 无 user_state
            self.assertEqual(leg.total_notional_mult(), 0.0)
            info.now_ms = ENTRY_NOW_MS
            _enable(db)
            status = leg.tick(now_ms=ENTRY_NOW_MS)
            self.assertEqual(status["state"], "entered")
            self.assertEqual(len(ex.orders), 1)

    def test_env_garbage_disables_gate_like_main(self):
        """与 main.hl_params 同口径：读不懂的 env 退回 0（不设限），不擅自改行为。"""
        with tempfile.TemporaryDirectory() as tmp, _env("2,0"):
            leg, db, ex, info = _leg(tmp, plain_info=True)
            self.assertEqual(leg.total_notional_mult(), 0.0)
            info.now_ms = ENTRY_NOW_MS
            _enable(db)
            self.assertEqual(leg.tick(now_ms=ENTRY_NOW_MS)["state"], "entered")


class TestParseTotalNotionalMult(unittest.TestCase):
    def test_values(self):
        self.assertEqual(parse_total_notional_mult("2.0"), 2.0)
        self.assertEqual(parse_total_notional_mult(2), 2.0)
        self.assertEqual(parse_total_notional_mult(" 1.5 "), 1.5)
        for bad in (None, "", "abc", 0, 0.0, -1, "-2.0", "nan", float("nan")):
            with self.subTest(raw=bad):
                self.assertEqual(parse_total_notional_mult(bad), 0.0)


class TestSharedConstants(unittest.TestCase):
    def test_env_key_matches_main_assembly(self):
        """main.hl_params 里 `f("HL_MAX_TOTAL_NOTIONAL_MULT", 0.0)` 的同一个键。"""
        self.assertEqual(TOTAL_NOTIONAL_MULT_ENV, "HL_MAX_TOTAL_NOTIONAL_MULT")


if __name__ == "__main__":                                # pragma: no cover
    unittest.main()
