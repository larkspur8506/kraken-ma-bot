"""Round 9: offline failure boundaries, never exchange writes."""
import dataclasses
import json
import os
import tempfile
import unittest
from unittest.mock import Mock, patch
from app.kr.info import KrInfo, KrInfoError
from app.kr.exchange import parse_order_response
from app import market
from app.strategy import Bar, MIN_BARS_REQUIRED
from tests.test_review7_mr import (DB, Info, Ex, make_leg, mr_entry, exit_intent,
                                   fill, NOW, EXIT_SESSIONS, M)

class Review9(unittest.TestCase):
    def test_probe_quantity_matches_shared_stop(self):
        from scripts.probe_state import stop_coverage
        from app.hl.stops import has_covering_stop
        p=dict(coin='BTC',side=1,size=.0083,entry_px=83000)
        for missing in (0,1,2):
            o=dict(coin='BTC',is_buy=False,sz=.0083-missing*.0001,
                   trigger_px=80000,reduce_only=True,is_trigger=True,order_type='stp')
            bad,unknown=stop_coverage([p],[o],True)
            covered=has_covering_stop([o],'BTC',side=1,sz=.0083,entry_px=83000,sz_tick=.0001)
            self.assertEqual(bool(bad),not covered);self.assertFalse(unknown)

    def test_atomic_fragment_rollback_and_multiple_fills(self):
        with tempfile.TemporaryDirectory() as td:
            db=DB(os.path.join(td,'x.db'));mr_entry(db,qty=10,entry=100)
            leg=make_leg(db,Info(szi=4));pid=int(db.mr_open_trade()['id'])
            key=leg._shadow_begin(M.MR_PURPOSE_EXIT,side=-1,sz=10,px=110,reduce_only=True,cloid='c',signal_date='2026-11-05',now_ms=NOW)
            wrong={**key,'bar_ms':0}
            with self.assertRaises(ValueError):
                db.mr_resolve_exit_fragment(pid,wrong,qty=4,price=110,ts=NOW/1000,cloid='c')
            self.assertFalse(json.loads(db.mr_open_trade()['raw']).get('partial_exits'))
            for qty,px in ((4,110),(6,115),(6,115)):
                db.mr_resolve_exit_fragment(pid,key,qty=qty,price=px,ts=NOW/1000,cloid='c',fill_ids=['a','b'])
            parts=json.loads(db.mr_open_trade()['raw'])['partial_exits']
            self.assertEqual(len(parts),1);self.assertEqual(parts[0]['qty'],6)
            self.assertEqual(parts[0]['price'],115)

    def test_bad_position_sizes_fail_closed(self):
        info = KrInfo()
        info._initial_margin = lambda c: .01
        info._ep = Mock()
        for size in (None, 'bad', float('nan'), float('inf'), -.01, True):
            info._ep.openpositions.return_value = [dict(symbol='PF_XBTUSD', size=size, side='long', price=83000)]
            with self.subTest(size=size), self.assertRaises(KrInfoError):
                info._positions({'BTC':83000})

    def test_bad_order_fields_untrusted(self):
        info=KrInfo(); info._ep=Mock()
        for fields in ({'side':None,'reduceOnly':True},{'side':'sell','reduceOnly':'false'}):
            info._ep.openorders.return_value=[dict(symbol='PF_XBTUSD',order_id='o',unfilledSize=.01,orderType='stp',stopPrice=80000,**fields)]
            with self.subTest(fields=fields):
                self.assertFalse(info._orders()[1])

    def test_bad_execution_price_unknown(self):
        for px in (None,0,-1,'bad',float('nan'),float('inf')):
            result=parse_order_response({'sendStatus':{'status':'filled','order_id':'o','orderEvents':[{'type':'EXECUTION','amount':.01,'price':px}]}})
            with self.subTest(px=px): self.assertEqual(result['status'],'unknown')

    def test_bad_ohlc_rejected(self):
        iv=market.INTERVAL_MS; now=NOW; last=now//iv*iv
        bars=[Bar(last-(MIN_BARS_REQUIRED+1-i)*iv,100,101,99,100,1) for i in range(MIN_BARS_REQUIRED+2)]
        for key,value in [('close',float('nan')),('open',-1),('high',float('inf')),('close',200)]:
            bad=list(bars);bad[-2]=dataclasses.replace(bad[-2],**{key:value})
            with self.subTest(key=key,value=value),self.assertRaises(market.FeedError):market.validate('BTC_USDT',bad,now)

    def test_mr_confirmation_unreadable_stays_pending(self):
        with tempfile.TemporaryDirectory() as td:
            db=DB(os.path.join(td,'x.db'));mr_entry(db,qty=10,entry=100)
            info=Info(szi=10,entry=100,mid=120);leg=make_leg(db,info)
            with patch.object(leg,'_await_position',return_value=(None,False)):
                leg._maybe_exit(NOW,EXIT_SESSIONS,[],db.mr_open_trade(),{'szi':10,'entryPx':100,'unrealized_pnl':0},{})
            self.assertEqual(len(leg._mr_attempts(M.MR_PURPOSE_EXIT,'2026-11-05',('intent','sent'))),1)

    def test_mr_fragment_replay_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            db=DB(os.path.join(td,'x.db'));mr_entry(db,qty=10,entry=100)
            info=Info(szi=6,entry=100,mid=120,fills=[fill(t=NOW-1000,px=110,sz=4,cloid='c',tid='f')]);leg=make_leg(db,info)
            key=leg._shadow_begin(M.MR_PURPOSE_EXIT,side=-1,sz=10,px=110,reduce_only=True,cloid='c',signal_date='2026-11-05',now_ms=NOW-60000)
            leg._shadow_finish(key,status='sent')
            leg._append_fragment(int(db.mr_open_trade()['id']),qty=4,price=110,ts=NOW/1000,price_source='fills',cloid='c')
            pending=leg._mr_attempts(M.MR_PURPOSE_EXIT,'2026-11-05',('intent','sent'))
            leg._resolve_exit_unknown(db.mr_open_trade(),{'szi':6},NOW,pending,{})
            parts=json.loads(db.mr_open_trade()['raw'])['partial_exits']
            self.assertEqual(sum(p['qty'] for p in parts),4)

    def test_mr_unknown_full_fill_recovers_without_new_order(self):
        with tempfile.TemporaryDirectory() as td:
            db=DB(os.path.join(td,'x.db'));mr_entry(db,qty=10,entry=100)
            info=Info(szi=0,mid=110,fills=[fill(t=NOW-1000,px=110,sz=10,cloid='c',tid='f')])
            ex=Ex(info);leg=make_leg(db,info,ex,fee=.000275)
            exit_intent(db)
            key=leg._shadow_begin(M.MR_PURPOSE_EXIT,side=-1,sz=10,px=110,reduce_only=True,cloid='c',signal_date='2026-11-05',now_ms=NOW-60000)
            leg._shadow_finish(key,status='sent')
            pending=leg._mr_attempts(M.MR_PURPOSE_EXIT,'2026-11-05',('intent','sent'))
            self.assertEqual(leg._resolve_exit_unknown(db.mr_open_trade(),None,NOW,pending,{}),'closed')
            self.assertFalse(ex.calls)
            row=db.conn.execute('select * from mr_trades').fetchone()
            self.assertAlmostEqual(row['net_pnl'],99.4225)
            self.assertTrue(json.loads(row['raw'])['net_pnl_complete'])

    def test_partial_fragment_database_error_blocks_retry(self):
        with tempfile.TemporaryDirectory() as td:
            db=DB(os.path.join(td,'x.db'));mr_entry(db,qty=10,entry=100)
            info=Info(szi=6,mid=110,fills=[fill(t=NOW-1000,px=110,sz=4,cloid='c',tid='f')]);ex=Ex(info);leg=make_leg(db,info,ex)
            key=leg._shadow_begin(M.MR_PURPOSE_EXIT,side=-1,sz=10,px=110,reduce_only=True,cloid='c',signal_date='2026-11-05',now_ms=NOW-60000)
            leg._shadow_finish(key,status='sent')
            pending=leg._mr_attempts(M.MR_PURPOSE_EXIT,'2026-11-05',('intent','sent'))
            with patch.object(db,'mr_resolve_exit_fragment',side_effect=RuntimeError('disk failure')):
                self.assertEqual(leg._resolve_exit_unknown(db.mr_open_trade(),{'szi':6},NOW,pending,{}),'blocked')
            self.assertFalse(ex.calls)
            self.assertEqual(len(leg._mr_attempts(M.MR_PURPOSE_EXIT,'2026-11-05',('intent','sent'))),1)

    def test_mr_all_fragments_complete(self):
        for age in (60,7*3600):
            with self.subTest(age=age),tempfile.TemporaryDirectory() as td:
                db=DB(os.path.join(td,'x.db'));mr_entry(db,qty=10,entry=100,raw={'partial_exits':[{'qty':10,'price':110,'price_source':'fills','oid':'o1'}]})
                exit_intent(db,ts=NOW/1000-age)
                info=Info(szi=0,mid=110,fills=[fill(t=NOW-1000,px=110,sz=10,tid='a')]);leg=make_leg(db,info,fee=.000275)
                status={};self.assertTrue(leg._adopt_exit(db.mr_open_trade(),NOW,status))
                row=db.conn.execute('select * from mr_trades').fetchone();raw=json.loads(row['raw'])
                self.assertTrue(raw['net_pnl_complete']);self.assertNotIn('action_required',status)
                self.assertAlmostEqual(row['net_pnl'],99.4225)
