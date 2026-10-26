import copy,unittest
from decimal import Decimal as D
import test_landed as landed
from margin import build_margin,split_money,margin_csv

class MarginTests(unittest.TestCase):
    setUp=landed.LandedTests.setUp
    tearDown=landed.LandedTests.tearDown
    opening=landed.LandedTests.opening
    settings=landed.LandedTests.settings
    policy=landed.LandedTests.policy
    move=landed.LandedTests.move
    refund=landed.LandedTests.refund
    rate=landed.LandedTests.rate
    report=landed.LandedTests.report
    def prepare(self,currency='CNY'):
        r=self.opening(3,'9',currency)
        self.v.save_record({**r,'lines':[{'warehouse_sku':'S','quantity':'3','amount':'9','head_leg_amount':'3','logistics_amount':'6'}]})
        self.settings();self.policy();m=self.move(qty='2')
        for scope in ('outbound','refund'):
            self.v.accounting.save('coverage',{'version':0,'shop_key':'A','scope':scope,'start':'2026-09-01','end':'2026-09-30','confirmed':True,'reference':'Complete source export'})
        return m
    def source(self):
        return {'version':1,'timezone':'Asia/Shanghai','start':'2026-09-01','end':'2026-09-30','as_of':'2026-09-30','sha256':'cash',
                'items':[{'shop_key':'A','order_no':'PO1','platform_sku':'S','quantity':'2','order_date':'2026-09-01','order_time_source':'platform_order_time','sales_eligible':True,'site':'','product_id':'','normalized_line_count':1}],
                'cash':[self.cash('sale','100')], 'records':[{'kind':'coverage','shop_key':'A','scope':s,'start':'2026-09-01','end':'2026-09-30','confirmed':True} for s in ('statements','taxes')], 'shops':['A']}
    def cash(self,key,amount,fee='销售回款',currency='CNY'):
        return {'entry_key':key,'source':'settlement','source_reference':key,'shop_key':'A','order_no':'PO1','platform_sku':'S','fee_name':fee,'amount':amount,'currency':currency,'date':'2026-09-20','paid_at':None,'order_status':'matched','sku_status':'matched','allocation_status':'matched','allocations':[]}
    def calculate(self,source=None):return build_margin(source or self.source(),self.report([]),self.v.inputs())
    def test_multiple_cash_receipts_deduct_order_cost_once(self):
        self.prepare();s=self.source();s['cash']=[self.cash('first','60'),self.cash('second','40')]
        r=self.calculate(s);self.assertEqual(r['status'],'complete');row=r['rows'][0]
        self.assertEqual(row['costs'],{'purchase':'6.0000000000','head_leg':'2.0000000000','logistics':'4.0000000000'});self.assertEqual(D(row['gross_profit']),D(88));self.assertEqual(D(row['gross_margin']),D('.88'))
        self.assertEqual(r['sku_totals'][0]['sales_quantity'],'2')
    def test_missing_tax_source_or_refunds_cannot_become_zero(self):
        self.prepare();s=self.source();s['records']=s['records'][:1]
        r=self.calculate(s);self.assertIsNone(r['rows'][0]['gross_profit']);self.assertIn('税费及非订单费用未确认完整',r['rows'][0]['reasons']);self.assertIsNone(r['summary']['non_order_net'])
        for record in self.v.accounting.snapshot()['records']:
            if record['kind']=='coverage' and record['scope']=='refund':self.v.accounting.remove({'id':record['id'],'version':record['version']})
        r=self.calculate();self.assertIsNone(r['rows'][0]['refund_quantity'])
    def test_refund_count_and_book_cost_reversal_are_separate_from_cash_reversal(self):
        m=self.prepare();self.refund(m,qty='1')
        s=self.source();s['cash'].append(self.cash('reverse','-50','销售冲回'))
        r=self.calculate(s);row=r['rows'][0]
        self.assertEqual(row['refund_quantity'],'1');self.assertEqual(D(row['costs']['purchase']),D(3));self.assertEqual(D(row['gross_profit']),D(44));self.assertEqual(D(row['gross_margin']),D('.88'))
    def test_refund_uses_original_book_fx_not_return_day_market_quote(self):
        m=self.prepare('USD');self.rate();self.rate('2026-09-03','8');self.refund(m)
        r=self.calculate();self.assertEqual(D(r['rows'][0]['costs']['purchase']),D(21));self.assertEqual(D(r['rows'][0]['costs']['head_leg']),D(7));self.assertEqual(D(r['rows'][0]['costs']['logistics']),D(14))
        self.assertEqual(D(r['rows'][0]['gross_profit']),D(58))
    def test_non_order_fees_stay_out_of_sku_margin(self):
        self.prepare();s=self.source();s['records'].append({'kind':'adjustment','id':1,'version':1,'shop_key':'A','source_key':'MONTHLY','category':'non_order_fee','currency':'CNY','amount':'5','date':'2026-09-20','reference':'invoice'})
        r=self.calculate(s);self.assertEqual(D(r['rows'][0]['gross_profit']),D(88));self.assertEqual(D(r['summary']['operating_balance']),D(83));self.assertEqual(len(r['overhead']),1)
    def test_unassigned_cash_never_appears_as_complete_overall_profit(self):
        self.prepare();s=self.source();s['cash'][0]['allocation_status']='unattributed';s['cash'][0]['sku_status']='unattributed'
        r=self.calculate(s);self.assertIsNone(r['summary']['gross_profit']);self.assertEqual(len(r['unassigned']),1)
    def test_missing_fx_keeps_native_cash_and_null_profit(self):
        self.prepare();s=self.source();s['cash'][0]['currency']='EUR'
        r=self.calculate(s);self.assertEqual(r['rows'][0]['native_platform_net'],{'EUR':'100'});self.assertIsNone(r['rows'][0]['platform_net']);self.assertIsNone(r['rows'][0]['gross_profit'])
    def test_metadata_and_csv_trace_remain_present(self):
        self.prepare();s=self.source();s['records'].append({'kind':'metadata','id':2,'version':3,'entry_key':'sale','paid_date':'2026-09-21','site':'US','product_id':'PRODUCT-123'})
        r=self.calculate(s);row=r['rows'][0];self.assertEqual(row['site'],'US');self.assertEqual(row['product_id'],'PRODUCT-123');self.assertEqual(row['transactions'][0]['metadata_version'],3)
        self.assertIn('PRODUCT-123',margin_csv(r).decode('utf-8-sig'))
    def test_largest_remainder_positive_negative_and_high_precision(self):
        for amount in ('0.01','-0.01','1.2345','-1.2345','0.0000000001'):
            split=split_money(D(amount),{'A':D(1),'B':D(1),'C':D(1)})
            self.assertEqual(sum(split.values()),D(amount))
        self.assertEqual(split_money(D(1),{'A':D(0)}),None)
    def test_zero_or_negative_sales_receipts_have_no_margin_percentage(self):
        self.prepare();s=self.source();s['cash'][0]['amount']='0'
        r=self.calculate(s);self.assertIsNone(r['rows'][0]['gross_margin'])

if __name__=='__main__':unittest.main()
