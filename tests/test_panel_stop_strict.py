"""面板止损列与「同源判据」的回归测试（app/hl/views.status_payload）。

背景（2026-11-13 审核 P2-1）：`status_payload` 里的止损检测停在第一版，只判
coin + reduce_only + is_trigger + trigger_px>0，而执行器那份已经补了方向/数量/
亏损侧/止盈排除。于是一张「买向 0.001 张的盈利侧止盈单」也能让面板显示「已有止损」，
与执行器/探针互相矛盾。现在面板直接调用 `app/hl/stops.covering_stop_px`。

覆盖点：
1. 正确反向止损 ⇒ has_stop=True 且 stop_px 就是触发价
2. 同向买单（买向、多仓）⇒ 未保护
3. 张数不足 ⇒ 未保护
4. take_profit ⇒ 未保护（即使方向/数量都对）
5. trigger_px=0 ⇒ 未保护
6. orders_trusted=False ⇒ has_stop=False 且 stop_unknown=True
7. orders_open 透传 order_type（缺失给 None）

全部复用 tests/test_hl_views.py 的 fake_snapshot/FakeInfo/BaseCase，临时 sqlite DB，不触网。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_hl_views import (FIXED_NOW, BaseCase, FakeInfo,  # noqa: E402
                                 fake_snapshot)

try:
    from app.hl.views import DashboardDeps, status_payload
except ImportError:                                   # pragma: no cover
    DashboardDeps = status_payload = None


def stop_order(**kw):
    """一张「看起来像止损」的挂单；用 kw 逐项破坏某一维判据。"""
    o = {"coin": "BTC", "oid": 7, "is_buy": False, "sz": 0.05, "limit_px": 0.0,
         "reduce_only": True, "trigger_px": 97_000.0, "is_trigger": True,
         "order_type": "stop_loss", "timestamp": int(FIXED_NOW * 1000)}
    o.update(kw)
    return o


def snap_with(orders, trusted=True, sz_ticks=None):
    """无止损的基线快照 + 指定挂单列表（BTC 多仓 0.05 @ 100000，标记 101000）。

    ``sz_ticks`` = 调度器随快照落库的数量精度（第七轮：面板渲染零网络，
    精度从快照读，不再调 specs()）。默认 None = 老快照/精度未知。
    """
    snap = fake_snapshot(with_stop=False)
    snap["open_orders"] = list(orders)
    snap["orders_trusted"] = trusted
    if sz_ticks is not None:
        snap["sz_ticks"] = dict(sz_ticks)
    return snap


class SpecInfo(FakeInfo):
    """带 `specs()` 的替身：模拟交易所数量精度（真实 KrInfo/HlInfo 都有）。"""

    def __init__(self, snapshot=None, sz_decimals=5):
        super().__init__(snapshot=snapshot)
        self.sz_decimals = sz_decimals
        self.spec_calls = 0

    def specs(self, coins=None):
        self.spec_calls += 1
        return {c: {"sz_decimals": self.sz_decimals, "szDecimals": self.sz_decimals}
                for c in (coins or ["BTC"])}


class NoSpecInfo(FakeInfo):
    """降级适配层替身：没有 specs()（精度查不到，判据应退化成相对容差）。"""


class PanelStopCase(BaseCase):
    def wire(self, snapshot, info=None):
        """把 deps 的 info 换成构造好的替身（db 沿用 BaseCase 的；app 要重建，
        否则路由闭包里抓的还是 setUp 里那个旧 deps）。"""
        self.info = info if info is not None else FakeInfo(snapshot)
        self.deps = DashboardDeps(
            db=self.db,
            config_fn=lambda: type("C", (), {"symbols": ["BTC_USDT", "ETH_USDT"]})(),
            info=self.info, address_fn=lambda: "0xTESTADDRESS123456",
            mode_fn=lambda: "live", now_fn=lambda: FIXED_NOW)
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.hl.views import build_router
        app = FastAPI()
        app.include_router(build_router(self.deps))
        self.client = TestClient(app)
        return self.deps

    def pos(self, payload=None):
        p = payload if payload is not None else status_payload(self.deps)
        btc = [x for x in p["positions"] if x["coin"] == "BTC"]
        self.assertEqual(len(btc), 1, "基线快照里应恰好有一条 BTC 持仓")
        return btc[0]

    def check(self, orders, trusted=True, sz_decimals=5):
        snap = snap_with(orders, trusted=trusted,
                         sz_ticks={"BTC": 10.0 ** (-sz_decimals)})
        self.wire(snap, SpecInfo(snap, sz_decimals=sz_decimals))
        return self.pos()


class TestCoveringStop(PanelStopCase):
    def test_correct_reverse_stop_is_protected(self):
        p = self.check([stop_order()])
        self.assertTrue(p["has_stop"])
        self.assertEqual(p["stop_px"], 97_000.0)
        self.assertFalse(p["stop_unknown"])

    def test_same_direction_buy_order_is_not_protected(self):
        # 多仓要卖单保护；买向触发单是加仓方向，不能算保护。
        p = self.check([stop_order(is_buy=True)])
        self.assertFalse(p["has_stop"])
        self.assertIsNone(p["stop_px"])
        self.assertFalse(p["stop_unknown"])       # 挂单可信 → 明确「无」，不是「无法确认」

    def test_size_too_small_is_not_protected(self):
        # 0.001 张远小于持仓 0.05 张：省下来的 0.049 张没有任何保护。
        p = self.check([stop_order(sz=0.001)])
        self.assertFalse(p["has_stop"])
        self.assertIsNone(p["stop_px"])

    def test_take_profit_is_not_protected(self):
        p = self.check([stop_order(order_type="take_profit")])
        self.assertFalse(p["has_stop"])
        self.assertIsNone(p["stop_px"])

    def test_take_profit_alias_case_insensitive_is_not_protected(self):
        p = self.check([stop_order(order_type="  TAKE_PROFIT ")])
        self.assertFalse(p["has_stop"])

    def test_trigger_px_zero_is_not_protected(self):
        p = self.check([stop_order(trigger_px=0)])
        self.assertFalse(p["has_stop"])
        self.assertIsNone(p["stop_px"])

    def test_untrusted_orders_reports_unknown(self):
        p = self.check([stop_order()], trusted=False)
        self.assertFalse(p["has_stop"])
        self.assertIsNone(p["stop_px"])
        self.assertTrue(p["stop_unknown"], "挂单不可信必须显示「无法确认」而非 ❌")

    def test_trigger_on_profit_side_is_not_protected(self):
        # 多仓的「止损」却挂在 entry 之上（盈利侧）：越过它就离场，挡不住下跌。
        p = self.check([stop_order(trigger_px=102_000.0)])
        self.assertFalse(p["has_stop"])

    def test_bad_order_among_good_ones_still_found(self):
        # 先来一张不顶用的止盈单，后面的真止损仍要认出来（不能被第一张骗停）。
        p = self.check([stop_order(sz=0.001, order_type="take_profit"),
                        stop_order(oid=9)])
        self.assertTrue(p["has_stop"])
        self.assertEqual(p["stop_px"], 97_000.0)

    def test_order_missing_keys_does_not_raise(self):
        # 老快照行缺 order_type/is_buy/sz 等键：面板不许 500，也不许当止损放行。
        bare = {"coin": "BTC", "oid": 7, "reduce_only": True, "is_trigger": True,
                "trigger_px": 97_000.0}
        p = self.check([bare])
        self.assertIn("has_stop", p)

    def test_sz_tick_comes_from_specs(self):
        # 交易所精度 1e-5：0.0499975 张与持仓 0.05 张差 2.5e-6（小于半格 5e-6）
        # → 算覆盖。整整差一个 tick（0.04999，差 1e-5）按第七轮 A1 属真实缺口，不算覆盖。
        p = self.check([stop_order(sz=0.0499975)], sz_decimals=5)
        self.assertTrue(p["has_stop"])
        p2 = self.check([stop_order(sz=0.04999)], sz_decimals=5)
        self.assertFalse(p2["has_stop"])

    def test_sz_tick_falls_back_when_specs_missing(self):
        # 同一个小于半格的张数、精度查不到 ⇒ 相对容差 5e-8，差 2.5e-6 就不算覆盖（判据更严，不 fail-open）。
        snap = snap_with([stop_order(sz=0.0499975)])
        self.wire(snap, NoSpecInfo(snap))
        p = self.pos()
        self.assertFalse(p["has_stop"])
        self.assertFalse(p["stop_unknown"])


class TestOrdersOpenOrderType(PanelStopCase):
    def test_order_type_passthrough(self):
        snap = snap_with([stop_order(order_type="take_profit"),
                          stop_order(oid=8, order_type="stop_loss")])
        self.wire(snap, SpecInfo(snap))
        p = status_payload(self.deps)
        self.assertEqual([o["order_type"] for o in p["orders_open"]],
                         ["take_profit", "stop_loss"])

    def test_order_type_missing_is_none(self):
        bare = {"coin": "BTC", "oid": 7, "is_buy": False, "sz": 0.05, "limit_px": 0.0,
                "reduce_only": True, "trigger_px": 97_000.0, "is_trigger": True,
                "timestamp": int(FIXED_NOW * 1000)}
        snap = snap_with([bare])
        self.wire(snap, SpecInfo(snap))
        p = status_payload(self.deps)
        self.assertIn("order_type", p["orders_open"][0])
        self.assertIsNone(p["orders_open"][0]["order_type"])

    def test_order_type_survives_json_round_trip(self):
        snap = snap_with([stop_order(order_type="take_profit")])
        self.wire(snap, SpecInfo(snap))
        body = self.client.get("/api/hl/status").json()
        self.assertEqual(body["orders_open"][0]["order_type"], "take_profit")


if __name__ == "__main__":
    unittest.main()
