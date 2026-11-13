"""app/hl/stops.py —— 止损覆盖判据的唯一口径回归（执行器/面板共用）。"""
from __future__ import annotations

import unittest

from app.hl.stops import covering_stop_px, has_covering_stop


def _o(**kw):
    base = {"coin": "BTC", "oid": 1, "is_buy": False, "sz": 1.0, "reduce_only": True,
            "trigger_px": 90.0, "is_trigger": True, "order_type": "stop_loss"}
    base.update(kw)
    return base


class TestCoveringStop(unittest.TestCase):
    def test_good_stop_covers(self):
        self.assertTrue(has_covering_stop([_o()], "BTC", side=1, sz=1.0, entry_px=100.0))

    def test_wrong_direction_does_not_cover(self):
        bad = _o(is_buy=True)              # 多仓挂买单：只会加仓，不是止损
        self.assertFalse(has_covering_stop([bad], "BTC", side=1, sz=1.0, entry_px=100.0))

    def test_undersized_does_not_cover(self):
        self.assertFalse(has_covering_stop([_o(sz=0.5)], "BTC", side=1, sz=1.0,
                                           entry_px=100.0))
        # 一个完整 tick 是真实未覆盖的余量，不再当噪声吞掉（第七轮 A1）。
        self.assertFalse(has_covering_stop([_o(sz=0.9999)], "BTC", side=1, sz=1.0,
                                           entry_px=100.0, sz_tick=0.0001))
        # 仅浮点噪声（远小于半格）仍算覆盖。
        self.assertTrue(has_covering_stop([_o(sz=0.99999999)], "BTC", side=1, sz=1.0,
                                          entry_px=100.0, sz_tick=0.0001))

    def test_take_profit_is_not_a_stop(self):
        tp = _o(order_type="take_profit", trigger_px=120.0)
        self.assertFalse(has_covering_stop([tp], "BTC", side=1, sz=1.0, entry_px=100.0))

    def test_profit_side_stop_is_not_protection(self):
        # 触发价在入场价上方（盈利侧），即使 order_type 写 stop 也保护不了下行
        self.assertFalse(has_covering_stop([_o(trigger_px=110.0)], "BTC", side=1, sz=1.0,
                                           entry_px=100.0))

    def test_zero_trigger_is_not_a_stop(self):
        self.assertFalse(has_covering_stop([_o(trigger_px=0)], "BTC", side=1, sz=1.0,
                                           entry_px=100.0))
        self.assertFalse(has_covering_stop([_o(trigger_px=None)], "BTC"))

    def test_short_side_requires_buy_and_above_entry(self):
        ok = _o(is_buy=True, trigger_px=110.0)
        self.assertTrue(has_covering_stop([ok], "BTC", side=-1, sz=1.0, entry_px=100.0))
        self.assertFalse(has_covering_stop([_o(is_buy=False, trigger_px=110.0)], "BTC",
                                           side=-1, sz=1.0, entry_px=100.0))

    def test_missing_order_type_does_not_fail_open(self):
        o = _o()
        del o["order_type"]
        self.assertTrue(has_covering_stop([o], "BTC", side=1, sz=1.0, entry_px=100.0),
                        "类型缺失时仍按方向/数量/亏损侧判，不该凭空放行或凭空否决")
        o2 = _o(trigger_px=120.0)
        del o2["order_type"]
        self.assertFalse(has_covering_stop([o2], "BTC", side=1, sz=1.0, entry_px=100.0))

    def test_old_style_no_extra_args(self):
        self.assertTrue(has_covering_stop([_o()], "BTC"))
        self.assertFalse(has_covering_stop([], "BTC"))
        self.assertFalse(has_covering_stop([_o(coin="ETH")], "BTC"))

    def test_covers_across_other_orders(self):
        orders = [_o(coin="ETH"), _o(order_type="take_profit", trigger_px=130.0), _o()]
        self.assertAlmostEqual(covering_stop_px(orders, "BTC", side=1, sz=1.0,
                                                entry_px=100.0), 90.0)


if __name__ == "__main__":
    unittest.main()
