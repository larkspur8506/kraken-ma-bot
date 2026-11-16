"""关账后补算净额（no_exit_fills → fills）的回归。

背景（2026-11-16 全流程真钱演练实测）：entry→exit 只隔 101 秒，`_close_local` 调
`_record_fill_pnl` 时 Kraken 的平仓成交还没同步进 hl_fills，于是只能标 `no_exit_fills`；
而它只在关账那一刻被调一次 —— 该笔 net_pnl 永久 NULL、逐笔统计与 R 值缺失。

修法：执行器每轮 run() 末尾用 `db.positions_missing_pnl()` 取出「已关账但净额没算出来」
的行补算。本文件钉住四件事：
  1. 出场腿补齐后能算出流水口径净额（net_pnl 写对、pnl_source 变 fills）；
  2. 仍然缺出场腿 → 保持 no_exit_fills、net_pnl 仍为 NULL（绝不写半截数字）；
  3. 未关账（open/pending）的行不受影响；
  4. 补算真的挂在 run() 上（不是只写了个没人调的方法）。
"""
from __future__ import annotations

import time
import unittest

from app.hl.executor import PNL_SRC_FILLS, PNL_SRC_NO_EXIT_FILLS
from tests.test_hl_executor import Base, FakeInfo


def _fill(tid, coin, direction, sz, px, ts, closed_pnl=0.0, fee=0.05, oid="o1"):
    return {"tid": tid, "coin": coin, "dir": direction, "sz": sz, "px": px,
            "time": int(ts * 1000), "closedPnl": closed_pnl, "fee": fee,
            "feeToken": "USD", "oid": oid, "hash": tid}


class PnlBackfill(Base):
    def _closed_position(self, hl_status="closed"):
        """造一笔已关账、净额没算出来的仓位（模拟关账时平仓腿还没落库）。"""
        pid = self.add_signal()                       # pending 行
        self.db.set_pos_hl(pid, hl_status="open", hl_sz=0.001, hl_entry_oid="oid-entry")
        self.db.confirm_position(pid, 83000.0, 0.001, 80600.0, True, equity_at_entry=1000.0)
        self.db.close_position(pid, 84000.0, "stop")
        self.db.set_pos_hl(pid, hl_status=hl_status)
        self.db.set_pos_pnl(pid, None, PNL_SRC_NO_EXIT_FILLS)
        row = self.db.position_by_id(pid)
        return pid, float(row["opened_at"]), float(row["closed_at"])

    def test_backfill_writes_net_pnl_when_exit_fill_arrives(self):
        pid, opened, closed = self._closed_position()
        self.info._fills = [
            _fill("t-open", "BTC", "Open Long", 0.001, 83000.0, opened - 1, closed_pnl=0.0),
            _fill("t-close", "BTC", "Close Long", 0.001, 84000.0, closed + 1,
                  closed_pnl=1.0, oid="oid-stop"),
        ]
        filled = self.executor()._backfill_missing_pnl()
        row = self.db.position_by_id(pid)
        self.assertEqual(filled, 1)
        self.assertEqual(row["pnl_source"], PNL_SRC_FILLS)
        # net = Σclosed_pnl − 双边手续费 = 1.0 − 0.10
        self.assertAlmostEqual(float(row["net_pnl"]), 0.90, places=6)

    def test_still_missing_exit_leg_stays_null(self):
        pid, opened, _closed = self._closed_position()
        self.info._fills = [
            _fill("t-open", "BTC", "Open Long", 0.001, 83000.0, opened - 1, closed_pnl=0.0),
        ]
        self.executor()._backfill_missing_pnl()
        row = self.db.position_by_id(pid)
        self.assertEqual(row["pnl_source"], PNL_SRC_NO_EXIT_FILLS)
        self.assertIsNone(row["net_pnl"])

    def test_open_position_is_not_touched(self):
        pid = self.add_signal()
        self.db.set_pos_hl(pid, hl_status="open", hl_sz=0.001)
        self.db.confirm_position(pid, 83000.0, 0.001, 80600.0, True, equity_at_entry=1000.0)
        self.info._fills = [
            _fill("t-open", "BTC", "Open Long", 0.001, 83000.0, time.time() - 1),
        ]
        self.executor()._backfill_missing_pnl()
        row = self.db.position_by_id(pid)
        self.assertEqual(row["status"], "open")
        self.assertIsNone(row["pnl_source"])

    def test_old_positions_outside_window_are_skipped(self):
        pid, _opened, _closed = self._closed_position()
        self.db.conn.execute("UPDATE positions SET closed_at=? WHERE id=?",
                             (time.time() - 30 * 86400, pid))
        self.db.conn.commit()
        rows = self.db.positions_missing_pnl(time.time() - 7 * 86400)
        self.assertEqual([int(r["id"]) for r in rows], [])

    def test_run_calls_backfill(self):
        pid, opened, closed = self._closed_position()
        self.info = FakeInfo(fills=[
            _fill("t-open", "BTC", "Open Long", 0.001, 83000.0, opened - 1, closed_pnl=0.0),
            _fill("t-close", "BTC", "Close Long", 0.001, 84000.0, closed + 1,
                  closed_pnl=1.0, oid="oid-stop"),
        ])
        self.executor().run()
        row = self.db.position_by_id(pid)
        self.assertEqual(row["pnl_source"], PNL_SRC_FILLS)
        self.assertAlmostEqual(float(row["net_pnl"]), 0.90, places=6)


if __name__ == "__main__":
    unittest.main()
