"""charts_history 翻页 + market.py 主源单测（PR-1）。"""
import unittest

from app.kr.client import KrEndpoints


class _FakeCharts(KrEndpoints):
    """模拟：每页若干根，more_candles 控制；记录请求的 from/to（秒）。"""

    def __init__(self, pages):
        self.pages = list(pages)          # [(candles, more), ...]
        self.calls = []
        super().__init__()

    def charts(self, trade_symbol, res, from_s, to_s, tick="trade"):
        self.calls.append((from_s, to_s))
        if not self.pages:
            return [], False
        return self.pages.pop(0)


class TestChartsHistory(unittest.TestCase):
    def test_pagination_seconds(self):
        ep = _FakeCharts.__new__(_FakeCharts)
        # candle.time 为毫秒（与真接口一致）；1000000ms = 1000s
        ep.pages = [([{"time": 1000000}, {"time": 2000000}], True),
                    ([{"time": 3000000}, {"time": 4000000}], False)]
        ep.calls = []
        out = ep.charts_history("PF_XBTUSD", "4h", 1, 100000)
        self.assertEqual([c["time"] for c in out], [1000000, 2000000, 3000000, 4000000])
        self.assertEqual(len(ep.calls), 2)
        # 翻页推进 = 上一页最后一根 time(ms)//1000 + 1（秒）
        self.assertEqual(ep.calls[1][0], 2001)

    def test_empty_first_page(self):
        ep = _FakeCharts.__new__(_FakeCharts)
        ep.pages = [([], False)]
        ep.calls = []
        self.assertEqual(ep.charts_history("PF_XBTUSD", "4h", 1, 100), [])
        self.assertEqual(len(ep.calls), 1)

    def test_market_primary_source_is_kr(self):
        from app.market import SOURCES
        self.assertEqual(SOURCES[0][0], "kr-chart")

    def test_kraken_charts_maps_registry(self):
        from app import symbol_registry as registry
        self.assertEqual(registry.kr_symbol("BTC"), "PF_XBTUSD")
        self.assertEqual(registry.kr_symbol("QQQ"), "PF_QQQXUSD")
        self.assertEqual(registry.kr_coin("PF_SOLUSD"), "SOL")
        self.assertIsNone(registry.kr_symbol("DOGE"))


if __name__ == "__main__":
    unittest.main()
