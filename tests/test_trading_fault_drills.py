"""Offline multi-tick drills. Only exchange I/O is simulated; SQLite and business code are real."""
import json
import socket
import unittest
from pathlib import Path
from unittest.mock import patch

from app.db import DB
from tests.test_hl_executor import Base as EntryBase, _snap as entry_snap, _pos as entry_pos
from app.hl import mr as M
from tests.test_review7_mr import (MrBase, Info, Ex, make_leg, mr_entry, fill,
                                  NOW, EXIT_SESSIONS, ENTRY_SESSIONS)
from tests.test_kr_cancel_filled_race import (Base as MainBase, BAR, QTY, STOP,
    STOP_OID, REF_PRICE, FILL_PX, _snap, _pos, _closing_fills, PURPOSE_STOP)


class OfflineGuard:
    def setUp(self):
        super().setUp()
        self.net_guard = patch.object(socket.socket, 'connect',
                                      side_effect=AssertionError('network forbidden in fault drills'))
        self.net_guard.start()
        self.addCleanup(self.net_guard.stop)


class MrLifecycleDrills(OfflineGuard, MrBase):
    def tick(self, info, exchange, now=NOW):
        leg = make_leg(self.db, info, exchange, clock_ms=now)
        # Candle acquisition/aggregation is outside this execution-focused drill.
        with patch.object(leg, '_candles', return_value=[{'c': str(info.mid)}]), \
             patch('app.hl.mr.mr_signal.aggregate_sessions_ex', return_value=(EXIT_SESSIONS, [])):
            return leg.tick(now_ms=now)

    def test_entry_then_exit_across_restarts(self):
        info = Info(szi=0, entry=100, mid=100)
        exchange = Ex(info)
        leg = make_leg(self.db, info, exchange, clock_ms=NOW)
        with patch.object(leg, '_candles', return_value=[{'c': '100'}]), \
             patch('app.hl.mr.mr_signal.aggregate_sessions_ex', return_value=(ENTRY_SESSIONS, [])):
            result = leg.tick(now_ms=NOW)
        self.assertEqual(result['state'], 'entered')
        original = info.szi
        self.assertGreater(original, 0)
        self.assertEqual(len(exchange.calls), 1)
        self.assertFalse(exchange.calls[0]['reduce_only'])
        self.db.conn.close()
        self.db = DB(str(Path(self.tmp.name) / 't.db'))
        info.mid = 100
        exit_now = NOW + 4 * 86400000
        from tests.test_review7_mr import sess
        future_sessions = EXIT_SESSIONS + [sess('2026-11-13', 110), sess('2026-11-16', 111)]
        exit_leg = make_leg(self.db, info, exchange, clock_ms=exit_now)
        with patch.object(exit_leg, '_candles', return_value=[{'c': '100'}]), \
             patch('app.hl.mr.mr_signal.aggregate_sessions_ex', return_value=(future_sessions, [])):
            result = exit_leg.tick(now_ms=exit_now)
        self.assertEqual(result['state'], 'exited')
        self.assertEqual(info.szi, 0)
        self.assertEqual(len(exchange.calls), 2)
        self.assertTrue(exchange.calls[1]['reduce_only'])
        self.assertAlmostEqual(exchange.calls[1]['sz'], original)
        self.tick(info, exchange, NOW + 600000)
        self.assertEqual(len(exchange.calls), 2)

    def test_known_boundary_lost_response_marks_estimated_close_incomplete(self):
        mr_entry(self.db, qty=10, entry=100)
        info = Info(szi=10, entry=100, mid=110)
        def lost(coin, is_buy, sz, px, **kw):
            info.szi = 0
            info.fills = [fill(t=NOW, px=110, sz=sz, cloid=kw['cloid'], tid='full')]
            return {'ok': False, 'status': 'unknown', 'error': 'response lost'}
        exchange = Ex(info, exit_=lost)
        info.fills_raises = True
        self.tick(info, exchange)
        self.assertEqual(len(exchange.calls), 1)
        self.assertIsNone(self.db.mr_open_trade())
        closed = self.db.conn.execute('SELECT * FROM mr_trades').fetchone()
        self.assertFalse(json.loads(closed['raw'])['net_pnl_complete'])
        self.assertEqual(closed['exit_reason'], 'sma5_adopted')

    def test_stale_position_after_partial_receipt_waits_for_evidence(self):
        mr_entry(self.db, qty=10, entry=100)
        info = Info(szi=10, entry=100, mid=110)
        def stale(coin, is_buy, sz, px, **kw):
            info.fills = [fill(t=NOW, px=110, sz=4, cloid=kw['cloid'], tid='partial')]
            return {'ok': True, 'status': 'partially_filled', 'filled_sz': 4,
                    'avg_px': 110, 'oid': 'partial'}
        exchange = Ex(info, exit_=stale)
        result = self.tick(info, exchange)
        self.assertEqual(result['state'], 'action_required')
        self.assertEqual(len(exchange.calls), 1)
        self.assertEqual(json.loads(self.db.mr_open_trade()['raw']).get('partial_exits', []), [])
        # Fresh snapshot arrives on next process/tick; evidence can now be committed.
        info.szi = 6
        leg = make_leg(self.db, info, exchange, clock_ms=NOW + 60000)
        pending = leg._mr_attempts(M.MR_PURPOSE_EXIT, '2026-11-05', ('intent', 'sent'))
        self.assertEqual(leg._resolve_exit_unknown(self.db.mr_open_trade(), {'szi': 6},
                                                 NOW + 60000, pending, {}), 'clear')
        self.assertEqual(len(exchange.calls), 1)
        self.assertEqual(sum(p['qty'] for p in json.loads(self.db.mr_open_trade()['raw'])['partial_exits']), 4)

    def test_partial_faults_restart_then_final_close_conserves_ledger(self):
        for fraction in (.1, .4, .9):
            for fault in ('timeout', 'commit_failure', 'process_death'):
                with self.subTest(fraction=fraction, fault=fault):
                    self._exercise_partial(fraction, fault)

    def _exercise_partial(self, fraction, fault):
        # Each trajectory uses a fresh persistent DB and a newly constructed leg per tick.
        self.db.conn.close()
        path = Path(self.tmp.name) / f'{fraction}-{fault}.db'
        self.db = DB(str(path))
        self.db.set_config('mr_enabled', '1')
        mr_entry(self.db, qty=10, entry=100)
        info = Info(szi=10, entry=100, mid=110)
        first_qty = 10 * fraction
        class ProcessDeath(BaseException):
            pass
        def first(coin, is_buy, sz, limit_px, **kw):
            self.assertFalse(is_buy)
            self.assertTrue(kw['reduce_only'])
            info.szi -= first_qty
            info.fills.append(fill(t=NOW, px=110, sz=first_qty,
                                   cloid=kw['cloid'], tid='first'))
            if fault == 'process_death':
                raise ProcessDeath()
            if fault == 'timeout':
                return {'ok': False, 'status': 'unknown', 'error': 'response lost'}
            return {'ok': True, 'status': 'partially_filled', 'filled_sz': first_qty,
                    'avg_px': 110, 'oid': 'first-order'}
        exchange = Ex(info, exit_=first)
        if fault == 'commit_failure':
            self.db.conn.execute("CREATE TRIGGER fail_shadow BEFORE UPDATE ON hl_orders "
                "WHEN NEW.status='filled' BEGIN SELECT RAISE(ABORT,'storage unavailable'); END")
            self.db.conn.commit()
        if fault == 'process_death':
            with self.assertRaises(ProcessDeath):
                self.tick(info, exchange)
        else:
            result = self.tick(info, exchange)
            self.assertEqual(result['state'], 'action_required')
        self.assertEqual(len(exchange.calls), 1)
        self.assertIsNotNone(self.db.mr_open_trade())
        self.assertEqual(json.loads(self.db.mr_open_trade()['raw']).get('partial_exits', []), [])
        if fault == 'commit_failure':
            self.db.conn.execute('DROP TRIGGER fail_shadow')
            self.db.conn.commit()
        # Reopen the connection as well as reconstructing the business object.
        self.db.conn.close()
        self.db = DB(str(path))
        # A failed history read must retain the unknown order and issue no second order.
        info.fills_raises = True
        blocked = self.tick(info, exchange, NOW + 60000)
        self.assertEqual(blocked['state'], 'action_required')
        self.assertEqual(len(exchange.calls), 1)
        info.fills_raises = False
        def final(coin, is_buy, sz, limit_px, **kw):
            self.assertAlmostEqual(sz, 10 - first_qty)
            self.assertTrue(kw['reduce_only'])
            info.szi = 0
            info.fills.append(fill(t=NOW + 300000, px=110, sz=sz,
                                   cloid=kw['cloid'], tid='last'))
            return {'ok': True, 'status': 'filled', 'filled_sz': sz,
                    'avg_px': 110, 'oid': 'last-order'}
        exchange.exit = final
        result = self.tick(info, exchange, NOW + 300000)
        self.assertEqual(result['state'], 'exited')
        self.assertIsNone(self.db.mr_open_trade())
        self.assertEqual(len(exchange.calls), 2)
        row = self.db.conn.execute('SELECT * FROM mr_trades').fetchone()
        self.assertAlmostEqual(row['net_pnl'], 100)
        parts = json.loads(row['raw'])['partial_exits']
        self.assertAlmostEqual(sum(p['qty'] for p in parts), first_qty)
        self.assertEqual(len(parts), 1)
        self.tick(info, exchange, NOW + 600000)
        self.assertEqual(len(exchange.calls), 2, 'closed trajectory must not replay exit')
        self.db.conn.close()
        self.db = DB(str(Path(self.tmp.name) / 't.db'))


class MainEntryLifecycle(OfflineGuard, EntryBase):
    def test_entry_partial_exit_restart_and_final_close(self):
        pid = self.add_signal()
        self.p.max_signal_age_min = 0
        # Stateful exchange: each response changes the same venue snapshot.
        live = {'qty': 0.0, 'orders': []}
        def snapshot():
            return entry_snap(positions=[entry_pos(size=live['qty'])] if live['qty'] else [],
                              orders=live['orders'])
        def entry(coin, is_buy, sz, px):
            live['qty'] = sz
            return {'ok': True, 'status': 'filled', 'oid': 'entry', 'filled_sz': sz,
                    'avg_px': px, 'error': ''}
        def stop(coin, is_buy, sz, px):
            order = {'coin': coin, 'oid': 'protection', 'is_buy': is_buy, 'sz': sz,
                     'reduce_only': True, 'is_trigger': True, 'trigger_px': px,
                     'order_type': 'stop'}
            live['orders'] = [order]
            return {'ok': True, 'status': 'resting', 'oid': 'protection', 'error': ''}
        self.ex.entry = entry
        self.ex.stop = stop
        self.info.snapshot = lambda *a, **kw: snapshot()
        self.info.open_orders = lambda *a: live['orders']
        self.ex._open_orders_checked = lambda: (live['orders'], True)
        self.executor().on_entry(pid)
        original = live['qty']
        self.assertGreater(original, 0)
        self.assertAlmostEqual(live['orders'][0]['sz'], original)
        self.assertEqual(self.db.position_by_id(pid)['status'], 'open')
        original_cancel = self.ex.cancel
        def cancel(coin, oid):
            live['orders'] = []
            return original_cancel(coin, oid)
        self.ex.cancel = cancel
        def partial(coin, is_buy, sz, px):
            self.assertTrue(sz <= live['qty'] + 1e-12)
            qty = round(sz * .4, 5)
            live['qty'] -= qty
            return {'ok': True, 'status': 'partially_filled', 'oid': 'partial',
                    'filled_sz': qty, 'avg_px': px, 'error': ''}
        self.ex.exit = partial
        self.executor().on_exit(pid, 'ma20', 84000)
        row = self.db.position_by_id(pid)
        self.assertEqual(row['status'], 'open')
        self.assertAlmostEqual(row['hl_sz'], live['qty'])
        self.assertAlmostEqual(live['orders'][0]['sz'], live['qty'])
        self.db.conn.close()
        self.db = DB(str(Path(self.tmp.name) / 't.db'))
        def finish(coin, is_buy, sz, px):
            self.assertAlmostEqual(sz, live['qty'])
            live['qty'] = 0
            return {'ok': True, 'status': 'filled', 'oid': 'final',
                    'filled_sz': sz, 'avg_px': px, 'error': ''}
        self.ex.exit = finish
        self.executor().on_exit(pid, 'ma20', 84000)
        self.assertEqual(self.db.position_by_id(pid)['status'], 'closed')
        self.assertEqual(live['qty'], 0)
        self.assertEqual(live['orders'], [])


class MainLifecycleDrills(OfflineGuard, MainBase):
    def test_stop_cancel_race_delayed_fills_restart_then_reconcile(self):
        pid = self.open_pos()
        self.db.hl_record_intent('BTC_USDT', 'BTC', BAR, PURPOSE_STOP, 1, QTY,
                                px=STOP, reduce_only=True, status='resting', oid=STOP_OID)
        self.info._snap = _snap(positions=[])
        self.ex.cancel_result = {'ok': True, 'status': 'filled'}
        self.executor().on_exit(pid, 'ma20', REF_PRICE)
        self.assertEqual(self.db.position_by_id(pid)['hl_status'], 'closing')
        self.assertEqual(self.ex.orders(), [])
        path = str(Path(self.tmp.name) / 't.db')
        self.db.conn.close()
        self.db = DB(path)
        self.info._fills = _closing_fills()
        self.executor().reconcile(self.info._snap)
        row = self.db.position_by_id(pid)
        self.assertEqual(row['status'], 'closed')
        self.assertAlmostEqual(row['exit_price'], FILL_PX)
        self.assertEqual(self.ex.orders(), [])
        self.executor().reconcile(self.info._snap)
        self.assertEqual(self.ex.orders(), [])
        self.assertEqual(self.db.hl_order('BTC_USDT', BAR, PURPOSE_STOP)['status'], 'filled')

    def test_untrusted_order_snapshot_blocks_recovery_across_restart(self):
        pid = self.open_pos()
        self.info._snap = _snap(positions=[_pos()], trusted=False)
        for _ in range(3):
            ex = self.executor()
            outcome = ex._recover_exit_unknown(pid, self.db.position_by_id(pid), 'BTC',
                'BTC_USDT', 1, QTY, BAR, 5, {'ok': False, 'status': 'unknown'})
            self.assertEqual(outcome, 'unknown')
            self.assertEqual(self.ex.orders(), [])
            self.assertEqual(self.db.position_by_id(pid)['hl_status'], 'closing')
            self.db.conn.close()
            self.db = DB(str(Path(self.tmp.name) / 't.db'))
