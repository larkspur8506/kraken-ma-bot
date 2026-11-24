"""第十二轮：资金费不可用时推送不出现资金费；SKIPPED 区分单槽占用（只改文案）。"""
import os
import tempfile
import unittest

os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "t.db"))

from app.hl import mr as M  # noqa: E402


def _txt(funding, complete=True):
    return M._mr_exit_text(
        coin="QQQ", filled=0.377, ledger_qty=0.377, partial_qty=0.0, gap_qty=0.0,
        entry_px=662.2, exit_px=652.4, entry_ts=1000.0, now_s=1000.0 + 6 * 86400,
        net_pnl=-3.83, fee_est=0.136, complete=complete, funding_paid=funding,
        price_source="avg_px", signal_date="2026-11-23")


class TestFundingHidden(unittest.TestCase):
    def test_unavailable_hides_line_and_tail(self):
        t = _txt(None)
        self.assertNotIn("资金费", t)
        self.assertIn("（估算：已扣估算手续费）", t)
        self.assertIn("信号日    2026-11-23", t)

    def test_unavailable_incomplete_hides(self):
        t = _txt(None, complete=False)
        self.assertNotIn("资金费", t)
        self.assertIn("未完成", t)

    def test_known_funding_still_shown(self):
        t = _txt(0.18)
        self.assertIn("资金费    净付 0.18 USD", t)
        self.assertIn("含资金费", t)

    def test_constant_removed(self):
        self.assertFalse(hasattr(M, "FUNDING_UNAVAILABLE"))


class TestSlotOccupied(unittest.TestCase):
    T = [{"state": "CLOSED", "entry_session": "2026-11-17", "exit_signal_date": "2026-11-23"},
         {"state": "SKIPPED", "entry_session": None, "exit_signal_date": None}]

    def test_inside_holding(self):
        for d in ("2026-11-17", "2026-11-19", "2026-11-20", "2026-11-22"):
            self.assertTrue(M._slot_occupied(self.T, d), d)

    def test_outside_holding(self):
        for d in ("2026-11-05", "2026-11-16", "2026-11-23", "2026-11-24"):
            self.assertFalse(M._slot_occupied(self.T, d), d)

    def test_open_row_right_open(self):
        t = [{"state": "OPEN", "entry_session": "2026-11-17", "exit_signal_date": None}]
        self.assertTrue(M._slot_occupied(t, "2026-12-01"))
        self.assertFalse(M._slot_occupied(t, "2026-11-16"))

    def test_closed_without_exit_date_not_attributed(self):
        t = [{"state": "CLOSED", "entry_session": "2026-11-17", "exit_signal_date": None}]
        self.assertFalse(M._slot_occupied(t, "2026-11-20"))

    def test_bad_rows_ignored(self):
        self.assertFalse(M._slot_occupied([None, {}, {"state": "CLOSED"}], "2026-11-20"))
        self.assertFalse(M._slot_occupied(None, "2026-11-20"))

    def test_reasons(self):
        self.assertEqual(M.SKIP_WINDOW_MISSED, "窗口错过（停机/延迟）")
        self.assertIn("单槽占用", M.SKIP_SLOT_OCCUPIED)


if __name__ == "__main__":
    unittest.main()
