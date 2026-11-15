"""Round 10: atomic MR partials, partial IOC semantics, contract-tick reconciliation."""
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from app.kr.exchange import parse_order_response
STATUS_PARTIAL = "partially_filled"
from tests.test_review7_mr import DB, Info, Ex, make_leg, mr_entry, NOW, EXIT_SESSIONS, M
from tests.test_kr_exchange import _exec_ok
from tests.test_reconcile_size import Base as SizeBase


class Review10MrFragment(unittest.TestCase):
    def run_partial(self, *, failure=False, flat=False, partial_status=False):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        db = DB(os.path.join(td.name, 'mr.db'))
        mr_entry(db, qty=10, entry=100)
        info = Info(szi=10, entry=100, mid=120)

        def partial(coin, is_buy, sz, px, **kw):
            info.szi = 0 if flat else 6
            return {'ok': True, 'status': STATUS_PARTIAL if partial_status else 'filled',
                    'oid': 'partial-oid', 'avg_px': 110, 'filled_sz': 4, 'error': '', 'raw': {}}

        ex = Ex(info, exit_=partial)
        leg = make_leg(db, info, ex, clock_ms=NOW)
        if failure:
            # Fail inside the transaction, after the fragment UPDATE but before shadow UPDATE.
            db.conn.execute("CREATE TRIGGER fail_shadow BEFORE UPDATE ON hl_orders "
                            "WHEN NEW.status='filled' BEGIN SELECT RAISE(ABORT,'disk failure'); END")
            db.conn.commit()
        status = leg._maybe_exit(NOW, EXIT_SESSIONS, [], db.mr_open_trade(),
                                 {'szi': 10, 'entryPx': 100, 'unrealized_pnl': 0}, {})
        return db, leg, ex, status

    def test_partial_transaction_failure_retains_unknown_without_fragment(self):
        db, leg, ex, status = self.run_partial(failure=True)
        parts = json.loads(db.mr_open_trade()['raw']).get('partial_exits', [])
        self.assertEqual(parts, [])
        attempts = leg._mr_attempts(M.MR_PURPOSE_EXIT, '2026-11-05', ('intent','sent','filled','failed'))
        self.assertEqual(attempts[-1][0]['status'], 'sent')
        self.assertEqual(status['state'], 'action_required')
        self.assertEqual(len(ex.calls), 1)

    def test_partial_success_commits_fragment_and_shadow_together(self):
        db, leg, ex, status = self.run_partial(partial_status=True)
        parts = json.loads(db.mr_open_trade()['raw'])['partial_exits']
        self.assertEqual(sum(p['qty'] for p in parts), 4)
        self.assertEqual(parts[0]['price'], 110)
        self.assertEqual(parts[0]['price_source'], 'avg_px')
        rows = leg._mr_attempts(M.MR_PURPOSE_EXIT, '2026-11-05', ('filled',))
        self.assertEqual(rows[-1][0]['filled_sz'], 4)
        self.assertEqual(len(ex.calls), 1)

    def test_short_receipt_with_flat_snapshot_does_not_close_full_ledger(self):
        db, leg, ex, status = self.run_partial(flat=True)
        self.assertIsNotNone(db.mr_open_trade())
        self.assertEqual(status['state'], 'action_required')
        rows = leg._mr_attempts(M.MR_PURPOSE_EXIT, '2026-11-05', ('sent',))
        self.assertEqual(len(rows), 1)

    def test_partial_execution_cancel_retains_partial_status(self):
        raw = _exec_ok(amount=4)
        raw['sendStatus']['status'] = 'partiallyFilled'
        raw['sendStatus']['orderEvents'].append({'type':'CANCEL','reason':'IOC remainder'})
        out = parse_order_response(raw, requested_sz=10)
        self.assertEqual(out['status'], STATUS_PARTIAL)
        self.assertEqual(out['filled_sz'], 4)

    def test_filled_status_with_short_execution_is_partial(self):
        out = parse_order_response(_exec_ok(amount=4), requested_sz=10)
        self.assertEqual(out['status'], STATUS_PARTIAL)
        self.assertEqual(out['filled_sz'], 4)


class Review10SizeTolerance(SizeBase):
    def test_exact_one_contract_tick_is_mismatch_on_first_check(self):
        pid = self.open_position(sz=.01)
        self.add_local_fill('open', sz=.01)
        executor = self.executor()
        executor._size_tick = lambda _: .0001
        result = executor._size_check(dict(self.db.position_by_id(pid)),
                                      {'size': .0099, 'side': 1}, kill_before=False)
        self.assertIsNotNone(result)
        self.assertFalse(result['kill'])
        self.assertIn('第 1 次', result['message'])
