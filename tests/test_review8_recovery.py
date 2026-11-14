"""Review 8: cross-module recovery regressions, offline only."""
import json
import time
import types
import unittest
from unittest.mock import Mock, patch

from tests.test_hl_executor import Base, _snap, _pos
from tests.test_review7_mr import (MrBase, Info, Ex, make_leg, mr_entry, exit_intent,
                                  fill, NOW, EXIT_SESSIONS, row_of)
from tests.test_review7_panel import base_snapshot, qqq_pos
from app import engine, market, strategy
from app.config import Config
from app.kr.client import KrEndpoints, KrApiError
from app.hl.views import DashboardDeps, status_payload, _mr_view
from app.hl import mr as M


class PositionsContract(unittest.TestCase):
    def test_requires_explicit_list(self):
        c = Mock()
        ep = KrEndpoints(c)
        for payload in ({'result':'success'}, {'openPositions':None}, {'openPositions':{}}):
            c.get_private.return_value = (payload, 200)
            with self.assertRaises(KrApiError):
                ep.openpositions()
        c.get_private.return_value = ({'openPositions':[]}, 200)
        self.assertEqual(ep.openpositions(), [])


class Recovery(Base):
    def test_untrusted_orders_unknown_never_writes(self):
        pid = self.add_signal()
        self.db.confirm_position(pid, 83000, .01, 80600, True)
        self.db.set_pos_hl(pid, hl_status='open', hl_sz=.01)
        for trusted in (False, None, 'true'):
            self.info._snap = _snap(positions=[_pos()])
            self.info._snap['orders_trusted'] = trusted
            ex = self.executor()
            result = ex._recover_exit_unknown(pid, self.db.position_by_id(pid), 'BTC',
                        'BTC_USDT', 1, .01, 123456, 4, {'status':'unknown','ok':False})
            self.assertEqual(result, 'unknown')
            self.assertEqual(self.ex.calls, [])
            self.assertEqual(self.db.position_by_id(pid)['hl_status'], 'closing')

    def test_machine_loss_kill_allows_protection_manual_does_not(self):
        pid = self.add_signal()
        self.db.confirm_position(pid, 83000, .01, 80600, True)
        self.db.set_pos_hl(pid, hl_status='open', hl_sz=.01)
        self.ex.stop = {'ok':True,'status':'resting','oid':'stop','not_executed':False}
        ex = self.executor()
        for origin in ('realized_dd', 'daily_loss'):
            self.ex.calls.clear()
            self.db.hl_set_kill(True, 'loss', origin=origin)
            ex._repair_missing_stop(dict(self.db.position_by_id(pid)),
                                    _snap(positions=[_pos()]), kill_before=True)
            self.assertEqual(len([c for c in self.ex.calls if c[0]=='order']), 1)
            self.assertTrue(self.db.hl_kill())
        self.ex.calls.clear()
        self.db.hl_set_kill(True, 'manual', origin='manual')
        ex._repair_missing_stop(dict(self.db.position_by_id(pid)),
                                _snap(positions=[_pos()]), kill_before=True)
        self.assertEqual(self.ex.calls, [])

    def test_repaired_pending_survives_engine_expiry(self):
        ms = int(time.time()*1000)
        base = ms//market.INTERVAL_MS*market.INTERVAL_MS
        bars = [strategy.Bar(base+(i-400)*market.INTERVAL_MS,83000,83100,82900,83000)
                for i in range(401)]
        pid = self.add_signal(bar_ms=base-5*market.INTERVAL_MS)
        bar = self.db.position_by_id(pid)['signal_bar_ms']
        self.db.hl_record_intent('BTC_USDT','BTC',bar,'entry',1,.01,status='filled',oid='entry')
        self.db.hl_update_order('BTC_USDT',bar,'entry',filled_sz=.01,avg_px=83000)
        self.info._snap = _snap(positions=[_pos()])
        self.ex.stop = {'ok':True,'status':'resting','oid':'stop','not_executed':False}
        ex = self.executor()
        rec = ex.reconcile(self.info._snap, kill_before=False)
        for row in rec['missing_stop']:
            ex._repair_missing_stop(row,self.info._snap,kill_before=False)
        row = self.db.position_by_id(pid)
        self.assertEqual(row['status'],'open')
        self.assertEqual(row['hl_status'],'open')
        self.assertEqual(row['hl_sz'],.01)
        with patch('app.engine.market.fetch',return_value=market.Feed('BTC_USDT','fake',bars,ms)), \
             patch('app.engine.strategy.evaluate_entry',return_value=None):
            engine._tick_symbol(self.db,Config(),self.notifier,'BTC_USDT',ms,ex,[])
        self.assertEqual(self.db.position_by_id(pid)['status'],'open')

    def test_lifetime_close_proof_after_partial(self):
        pid=self.add_signal()
        self.db.confirm_position(pid,83000,.01,80600,True)
        self.db.set_pos_hl(pid,hl_status='closing',hl_sz=.006,hl_stop_oid='stop-last')
        row=self.db.position_by_id(pid)
        ex=self.executor()
        ex._size_tick=lambda c:.0001
        self.db.hl_fills_window=Mock(return_value=[
            {'symbol':'BTC_USDT','sz':.004,'px':84000,'oid':'first','tid':'1',
             'reduce_only':1,'dir':'Close Long','closed_pnl':4},
            {'symbol':'BTC_USDT','sz':.006,'px':85000,'oid':'stop-last','tid':'2',
             'reduce_only':1,'dir':'Close Long','closed_pnl':12}])
        proof=ex._fill_close_proof(row,_snap(),allow_refetch=False)
        self.assertIsNotNone(proof)
        self.assertAlmostEqual(proof['sz'],.01)
        self.assertAlmostEqual(proof['px'],84600)
        self.assertEqual(set(proof['tids']),{'1','2'})

    def test_mr_upnl_survives_presentation_dedup(self):
        info=Mock()
        info.venue_address_free=True
        info.flows_supported=False
        info.snapshot.return_value=base_snapshot([qqq_pos()])
        info.all_mids.return_value={}
        deps=DashboardDeps(db=self.db,info=info,mode_fn=lambda:'live',
            address_fn=lambda:'kraken',config_fn=lambda:types.SimpleNamespace(
                symbols=['BTC_USDT'],hl_pnl_from_fills=False),
            mr_fn=lambda:{'enabled':True,'state':'holding','recon':'ok',
                         'pos':{'qty':1.,'entry_price':610.,'upnl':10.,'mark':620.}})
        p=status_payload(deps,include_curve_points=False)
        self.assertEqual(len(p['positions']),0)
        self.assertEqual(p['account']['unrealized_pnl'],10.)


class MrRecovery(MrBase):
    def test_partial_timeout_preserves_unknown_then_recovers_once(self):
        tid=mr_entry(self.db,qty=10,entry=100)
        info=Info(szi=10,entry=100,mid=110)
        def lost(*a,**kw):
            info.szi=6
            return {'ok':False,'status':'unknown','error':'timeout'}
        ex=Ex(info,exit_=lost)
        leg=make_leg(self.db,info,ex)
        status=leg._maybe_exit(NOW,EXIT_SESSIONS,[],self.db.mr_open_trade(),
                              leg._read_account('kraken')['mr_pos'],{})
        pending=leg._mr_attempts(M.MR_PURPOSE_EXIT,'2026-11-05',('intent','sent'))
        self.assertEqual(len(pending),1)
        self.assertEqual(status['state'],'action_required')
        cloid=pending[0][1]['cloid']
        info.fills=[fill(t=NOW-1,px=109.9,sz=4,cloid=cloid,tid='f-first')]
        result=leg._resolve_exit_unknown(self.db.mr_open_trade(),None,NOW+300000,pending,{})
        self.assertEqual(result,'clear')
        self.assertEqual(len(ex.calls),1)
        raw=json.loads(row_of(self.db,tid)['raw'])
        self.assertEqual(len(raw['partial_exits']),1)
        self.assertEqual(raw['partial_exits'][0]['qty'],4)
        self.assertEqual(leg._mr_attempts(M.MR_PURPOSE_EXIT,'2026-11-05',('intent','sent')),[])

    def test_adopt_partial_conserves_quantity_and_notional(self):
        tid=mr_entry(self.db,qty=10,entry=100,raw={'partial_exits':[
            {'qty':4.,'price':110.,'price_source':'avg_px','oid':'first'}]})
        exit_intent(self.db)
        info=Info(szi=0,mid=120,fills=[fill(t=NOW-60000,px=110,sz=4,tid='a'),
                                    fill(t=NOW-1000,px=120,sz=6,tid='b')])
        leg=make_leg(self.db,info,fee=.000275)
        self.assertTrue(leg._adopt_exit(self.db.mr_open_trade(),NOW,{}))
        row=row_of(self.db,tid)
        self.assertAlmostEqual(row['net_pnl'],159.406)
        self.assertAlmostEqual(row['exit_price'],120.)
        self.assertTrue(json.loads(row['raw'])['net_pnl_complete'])

    def test_estimated_partial_cannot_become_complete(self):
        mr_entry(self.db,raw={'partial_exits':[
            {'qty':4,'price':110,'price_source':'limit_px'}]})
        leg=make_leg(self.db,Info())
        st=leg._settlement(self.db.mr_open_trade(),covered_qty=6,covered_px=120,
                           price_source='avg_px',estimated=False)
        self.assertFalse(st['net_pnl_complete'])

    def test_disabled_existing_position_retains_exit_policy(self):
        view=_mr_view({'mr':{'enabled':False,'state':'disabled','ledger_pos':
                           {'qty':1,'entry_price':100}}})
        self.assertTrue(view['auto_exit'])
        self.assertIn('继续离场',view['status'])
