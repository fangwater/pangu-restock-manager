import unittest
from decimal import Decimal as D
import test_valuation as legacy
from test_valuation import demand
from landed import allocate_ticket,fx_convert,covered,import_csv
from procurement import CostError
from valuation import fifo_report

class LandedTests(unittest.TestCase):
    setUp=legacy.FIFOTests.setUp
    tearDown=legacy.FIFOTests.tearDown
    opening=legacy.FIFOTests.opening
    ticket=legacy.FIFOTests.ticket
    receipt=legacy.FIFOTests.receipt
    settings=legacy.FIFOTests.settings
    report=legacy.FIFOTests.report
    def rate(self,when='2026-09-01',rate='7',a='USD',b='CNY'):
        return self.v.accounting.save('rate',{'version':0,'from_currency':a,'to_currency':b,'effective_date':when,'rate':rate,'reference':'FX document'})
    def policy(self,basis='outbound'):
        p=self.v.accounting.snapshot()['policy']
        return self.v.accounting.save_policy({**p,'demand_basis':basis})
    def move(self,key='OUT1',qty='1',when='2026-09-02'):
        return self.v.accounting.save('movement',{'version':0,'source_key':key,'platform':'temu','shop_key':'A','order_no':'PO1','platform_sku':'S','warehouse_sku':'S','warehouse_code':'W','movement_date':when,'quantity':qty,'warehouse_quantity':qty,'reference':'Warehouse document'})
    def refund(self,m,key='R1',qty='1',when='2026-09-03',restocked=True,action='all'):
        return self.v.accounting.save('refund',{'version':0,'source_key':key,'movement_id':m['id'],'quantity':qty,'warehouse_quantity':qty,'returned_date':when,'restocked':restocked,'cost_action':action,'reference':'Return item document'})
    def test_fx_uses_latest_past_quote_and_inverse_and_versions(self):
        a=self.rate();self.rate('2026-10-01','8')
        amount,e=fx_convert(D('1.25'),'USD','CNY','2026-09-20',self.v.accounting.snapshot()['records'])
        self.assertEqual(amount,D('8.75'));self.assertEqual(e['id'],a['id'])
        self.assertEqual(fx_convert(D('7'),'CNY','USD','2026-09-20',self.v.accounting.snapshot()['records'])[0],D('1'))
        self.assertEqual(fx_convert(D(1),'USD','EUR','2026-09-20',self.v.accounting.snapshot()['records']),(None,None))
        self.assertEqual(fx_convert(D(0),'USD','EUR','2026-09-20',[])[0],D(0))
        self.assertEqual(fx_convert(D(1),'USD','CNY','2026-08-31',self.v.accounting.snapshot()['records']),(None,None))
        self.v.accounting.save('rate',{**a,'rate':'7.1'})
        with self.assertRaises(CostError):self.v.accounting.save('rate',a)
    def test_declared_fee_conserved_and_missing_rules_not_zero(self):
        template=self.costs.save_template({'version':0,'name':'Template','categories':[{'name':'Category','currency':'USD','types':[{'name':'Fee','calculation_mode':'direct_amount'}]}]})
        kind=template['categories'][0]['types'][0]
        t=self.costs.save_ticket({'version':0,'ticket_no':'T','cost_template_id':template['id'],'purchase_currency':'CNY','lines':[{'warehouse_sku':s,'quantity':'3','amount':'9'} for s in ('A','B','C')],'costs':[{'cost_type_id':kind['id'],'amount':'0.01'}]})
        before=allocate_ticket(t,self.v.accounting.snapshot());self.assertTrue(all(not r['complete']['head_leg'] for r in before.values()))
        self.v.accounting.save('rule',{'version':0,'cost_type_id':kind['id'],'bucket':'head_leg','method':'declared_value'})
        self.v.accounting.save('terms',{'version':0,'ticket_id':t['id'],'ticket_version':t['version'],'head_leg_complete':True,'logistics_complete':True,'lines':[{'warehouse_sku':s,'declared_value':'1','currency':'USD'} for s in ('A','B','C')]})
        after=allocate_ticket(t,self.v.accounting.snapshot())
        self.assertEqual(sum(r['amounts']['head_leg']['USD'] for r in after.values()),D('0.01'))
        self.assertTrue(all(all(r['complete'].values()) for r in after.values()))
    def test_actual_return_restocks_original_cost_once_and_is_saleable_next_day(self):
        opening=self.opening(2,'6');self.v.save_record({**opening,'lines':[{'warehouse_sku':'S','quantity':'2','amount':'6','head_leg_amount':'2','logistics_amount':'0'}]});self.settings();self.policy()
        m=self.move(qty='2');self.refund(m);self.move('OUT2','1','2026-09-03');self.move('OUT3','1','2026-09-04')
        r=self.report([])
        self.assertEqual(r['rows'][0]['converted'],{'purchase':'6.0000000000','head_leg':'2.0000000000','logistics':'0.0000000000'})
        self.assertEqual(r['rows'][1]['shortage_quantity'],'1')
        self.assertEqual(r['rows'][2]['known_amounts'],{'CNY':'3.0000000000'})
        self.assertEqual(r['refunds'][0]['components']['purchase'],{'CNY':'-3.0000000000'})
        with self.assertRaises(CostError):self.refund(m,'TOO_MUCH','2')
        with self.assertRaises(CostError):self.v.accounting.remove({'id':m['id'],'version':m['version']})
    def test_refund_without_physical_return_does_not_reverse_cost(self):
        self.opening(1,'3');self.settings();self.policy();m=self.move();self.refund(m,restocked=False,action='none')
        r=self.report([]);self.assertEqual(r['refunds'][0]['components']['purchase'],{});self.assertEqual(r['remaining'],[])
    def test_receipt_fee_allocation_and_fx_trace(self):
        t=self.ticket(3,'9');self.v.accounting.save('terms',{'version':0,'ticket_id':t['id'],'ticket_version':t['version'],'head_leg_complete':True,'logistics_complete':True,'lines':[]});self.receipt(t,1);self.receipt(t,2,name='IN2');self.settings()
        r=self.report([demand(1,3,'2026-09-02')]);self.assertEqual(r['rows'][0]['landed_status'],'complete');self.assertEqual(r['rows'][0]['converted']['purchase'],'9.0000000000')
    def test_unknown_opening_freight_remains_unknown(self):
        self.opening(1,'0');self.settings();r=self.report([demand(1,1)])
        self.assertEqual(r['rows'][0]['status'],'complete');self.assertEqual(r['rows'][0]['landed_status'],'review');self.assertIsNone(r['rows'][0]['converted']['head_leg'])
    def test_csv_retry_is_idempotent_and_error_rolls_back(self):
        header='原币,目标币,生效日期,汇率,来源凭证\n';line='USD,CNY,2026-09-01,7,FX\n'
        self.assertEqual(import_csv(self.v.accounting,'rate',header+line),{'created':1,'unchanged':0})
        self.assertEqual(import_csv(self.v.accounting,'rate',header+line),{'created':0,'unchanged':1})
        with self.assertRaises(CostError):import_csv(self.v.accounting,'rate',header+line.replace('09-01','09-02')+'CNY,USD,2026-09-01,0,BAD\n')
        self.assertEqual(len(self.v.accounting.snapshot()['records']),1)
    def test_deleted_business_key_can_be_restored_with_new_version(self):
        r=self.rate();self.v.accounting.remove({'id':r['id'],'version':r['version']})
        restored=self.rate();self.assertEqual(restored['id'],r['id']);self.assertEqual(restored['version'],3)
        with self.assertRaises(CostError):self.v.accounting.save('rate',r)
    def test_coverage_requires_contiguous_confirmed_dates(self):
        rows=[{'kind':'coverage','shop_key':'A','scope':'refund','start':'2026-09-01','end':'2026-09-14','confirmed':True},{'kind':'coverage','shop_key':'A','scope':'refund','start':'2026-09-15','end':'2026-09-30','confirmed':True}]
        self.assertTrue(covered(rows,'refund','A','2026-09-01','2026-09-30'));rows[1]['start']='2026-09-16';self.assertFalse(covered(rows,'refund','A','2026-09-01','2026-09-30'))

if __name__=='__main__':unittest.main()
