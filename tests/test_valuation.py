import copy
import hashlib
import tempfile
import unittest
from pathlib import Path
from app import Store, Manager
from procurement import CostStore, CostError
from valuation import ValuationStore, fifo_report, fifo_csv


def demand(identity, quantity, day='2026-09-01', shop='A', sku='S', warehouse='W'):
    return {'id':identity,'order_id':identity,'platform':'temu','shop_key':shop,'order_no':f'PO{identity}','line_no':str(identity),'occurred_at':day+'T01:00:00Z','date':day,'platform_sku':sku,'warehouse_sku':sku,'warehouse_code':warehouse,'quantity':str(quantity),'warehouse_quantity':str(quantity),'conversion_factor':'1'}


class FIFOTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=Store(Path(self.tmp.name)/'test.db')
        self.costs=CostStore(self.store)
        self.v=ValuationStore(self.store)
    def tearDown(self):
        self.tmp.cleanup()
    def settings(self):
        return self.v.save_settings({'version':self.v.inputs()['settings']['version'],'start_date':'2026-09-01','opening_confirmed':True})
    def opening(self,q,amount,code='CNY',name='OPEN'):
        return self.v.save_record({'version':0,'kind':'opening','record_no':name,'warehouse_code':'W','received_date':'2026-09-01','currency':code,'lines':[{'warehouse_sku':'S','quantity':str(q),'amount':amount}]})
    def ticket(self,q=3,amount='1',name='BUY'):
        return self.costs.save_ticket({'version':0,'ticket_no':name,'purchase_currency':'CNY','lines':[{'warehouse_sku':'S','quantity':str(q),'amount':amount}], 'costs':[]})
    def receipt(self,t,q,day='2026-09-02',name='IN'):
        return self.v.save_record({'version':0,'kind':'receipt','record_no':name,'warehouse_code':'W','received_date':day,'ticket_id':t['id'],'ticket_version':t['version'],'lines':[{'warehouse_sku':'S','quantity':str(q)}]})
    def report(self,rows,filters=None):
        src={'version':1,'timezone':'Asia/Shanghai','start':'2026-09-01','end':'2026-09-30','sha256':'source','demands':rows}
        return fifo_report(self.v.inputs(),src,{'end':'2026-09-30',**(filters or {})})
    def test_missing_configuration_does_not_fetch_source(self):
        manager=Manager(self.store)
        manager.get=lambda *a: self.fail('must not fetch without confirmed opening')
        result=manager.fifo({'end':'2026-09-30'})
        self.assertEqual(result['status'],'blocked')
        self.assertEqual(len(result['issues']),2)
    def test_replay_all_shops_and_earlier_days_before_filters(self):
        self.opening(2,'6');t=self.ticket(3,'12');self.receipt(t,3);self.settings()
        rows=[demand(3,2,'2026-09-03','A'),demand(1,1,shop='B'),demand(2,2,'2026-09-02','B')]
        r=self.report(rows,{'shop':'A','start':'2026-09-03'})
        self.assertEqual(r['summary']['replayed_lines'],3)
        self.assertEqual(len(r['rows']),1)
        self.assertEqual(r['rows'][0]['cost_amounts'],{'CNY':'8.0000000000'})
        self.assertEqual(r['remaining'],[])
    def test_partial_receipts_and_consumption_conserve_amount(self):
        t=self.ticket()
        for n in range(3):self.receipt(t,1,'2026-09-02',f'IN{n}')
        self.settings()
        r=self.report([demand(n,1,'2026-09-02') for n in range(3)])
        self.assertEqual([v['cost_amounts']['CNY'] for v in r['rows']],['0.3333333333','0.3333333334','0.3333333333'])
        self.assertEqual(r['summary']['complete_cost_totals'],{'CNY':'1.0000000000'})
        t2=self.ticket(1,'2',name='T2')
        self.receipt(t2,1,'2026-09-30',name='IN4')
        r=self.report([demand(n,'0.1','2026-09-02') for n in range(10)])
        self.assertEqual(sum(__import__('decimal').Decimal(v['known_amounts']['CNY']) for v in r['rows']),__import__('decimal').Decimal('0.3333333333'))
    def test_future_receipts_not_borrowed_and_shortage_taints_later(self):
        t=self.ticket(10,'20');self.receipt(t,10,'2026-09-02');self.settings()
        r=self.report([demand(1,1),demand(2,1,'2026-09-02')])
        self.assertEqual(r['rows'][0]['shortage_quantity'],'1')
        self.assertEqual(r['rows'][0]['allocations'],[])
        self.assertIsNone(r['rows'][1]['cost_amounts'])
        self.assertEqual(r['rows'][1]['known_amounts'],{'CNY':'2.0000000000'})
    def test_null_cost_distinct_from_zero_and_currency_separation(self):
        self.opening(1,None);self.opening(1,'0','USD','OPEN2');t=self.ticket(1,'4');self.receipt(t,1);self.settings()
        r=self.report([demand(1,1),demand(2,1),demand(3,1,'2026-09-02')])
        self.assertIsNone(r['rows'][0]['cost_amounts'])
        self.assertEqual(r['summary']['complete_cost_totals'],{'CNY':'4.0000000000','USD':'0.0000000000'})
    def test_opening_mutation_invalidates_confirmation_and_version(self):
        record=self.opening(1,'0');self.settings();config=self.v.inputs()['settings']
        self.v.save_record({**record,'lines':[{'warehouse_sku':'S','quantity':'2','amount':'1'}]})
        self.assertFalse(self.v.inputs()['settings']['opening_confirmed'])
        with self.assertRaises(CostError):self.v.save_settings(config)
        with self.assertRaises(CostError):self.v.save_record(record)
    def test_purchase_mutation_invalidates_receipt_and_overreceipt_rejected(self):
        t=self.ticket();self.receipt(t,2)
        with self.assertRaises(CostError):self.receipt(t,2,name='OVER')
        self.costs.save_ticket({**t,'lines':[{'warehouse_sku':'S','quantity':'3','amount':'2'}]})
        self.settings();r=self.report([demand(1,1,'2026-09-03')])
        self.assertIsNone(r['rows'][0]['cost_amounts'])
        self.assertTrue(any('版本变化' in i['message'] for i in r['issues']))
    def test_csv_atomic_idempotent_and_conflicting_records(self):
        header='类型,单号,仓库,入库日期,币种,采购单号,SKU,数量,金额,备注\n'
        valid='期初,OPEN,W,2026-09-01,CNY,,S,1,2,\n'
        self.assertEqual(self.v.import_csv(header+valid),{'created':1,'unchanged':0})
        self.assertEqual(self.v.import_csv(header+valid),{'created':0,'unchanged':1})
        with self.assertRaises(CostError):self.v.import_csv(header+valid.replace('OPEN','NEW')+'入库,BAD,W,2026-09-01,,MISSING,S,2,,\n')
        self.assertEqual(len(self.v.inputs()['records']),1)
        with self.assertRaises(CostError):self.v.import_csv(header+valid.replace(',1,2,',',2,2,'))
    def test_undated_records_block_only_affected_keys_and_csv_all_rows(self):
        t=self.ticket(100,'100');self.receipt(t,100,None);self.settings()
        r=self.report([demand(n,1,'2026-09-02') for n in range(70)])
        self.assertTrue(all(v['cost_amounts'] is None for v in r['rows']))
        self.assertEqual(len(fifo_csv(r).decode('utf-8-sig').splitlines()),71)
    def test_source_failure_never_becomes_zero(self):
        self.settings();m=Manager(self.store)
        def fail(*args):raise ValueError('source failure')
        m.get=fail
        with self.assertRaises(CostError) as err:m.fifo({'end':'2026-09-30'})
        self.assertEqual(err.exception.status,502)
    def test_unknown_warehouse_demand_prevents_false_cost_for_same_sku(self):
        self.opening(10,'10');self.settings()
        r=self.report([demand(1,1,warehouse=''),demand(2,1)])
        self.assertTrue(all(v['cost_amounts'] is None for v in r['rows']))
        self.assertTrue(any('仓库不明' in reason for reason in r['rows'][1]['reasons']))

    def test_incomplete_source_is_not_complete_cost(self):
        self.opening(10,'10');self.settings()
        src={'version':1,'timezone':'Asia/Shanghai','start':'2026-09-01','end':'2026-09-30','sha256':'x','missing_line_orders':1,'demands':[demand(1,1)]}
        r=fifo_report(self.v.inputs(),src,{'end':'2026-09-30'})
        self.assertIsNone(r['rows'][0]['cost_amounts'])
        src['demands']*=2
        with self.assertRaises(CostError):fifo_report(self.v.inputs(),src,{'end':'2026-09-30'})

if __name__=='__main__':unittest.main()
