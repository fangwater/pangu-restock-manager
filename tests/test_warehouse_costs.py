import copy,unittest
from app import Manager
import test_valuation as legacy
from warehouse_costs import accounting_date,evidence,import_evidence
from procurement import CostError

class WarehouseCostTests(unittest.TestCase):
    setUp=legacy.FIFOTests.setUp
    tearDown=legacy.FIFOTests.tearDown
    def manager(self):
        m=Manager(self.store)
        data={'version':1,'source_time_kind':'timestamp_without_timezone','sha256':'proof','coverage':[],
              'records':[{'source_key':'IN1','warehouse_code':'W','warehouse_sku':'S','document_no':'IB-1','kind':'入库单','change_quantity':'3','raw_time':'2026-09-01 12:00:00','references':[]},
                         {'source_key':'IN2','warehouse_code':'W','warehouse_sku':'S','document_no':'IB-1','kind':'入库单','change_quantity':'2','raw_time':'2026-09-01 13:00:00','references':[]}], 'candidates':[]}
        m.get=lambda url:{'success':True,'data':copy.deepcopy(data)}
        return m,data
    def test_unconfirmed_timezone_keeps_partial_receipt_as_draft_and_retry_is_idempotent(self):
        ticket=self.costs.save_ticket({'version':0,'ticket_no':'BUY','note':'IB-1','purchase_currency':'CNY','lines':[{'warehouse_sku':'S','quantity':'10','amount':'50'}]})
        m,_=self.manager();source=evidence(m,{})
        p={'kind':'receipt','keys':[source['inbound_groups'][0]['key']],'source_sha256':'proof'}
        self.assertEqual(import_evidence(m,p)['created'],1)
        record=m.valuation.inputs()['records'][0];self.assertIsNone(record['received_date']);self.assertEqual(record['lines'][0]['quantity'],'5');self.assertTrue(record['source_sha256'])
        self.assertEqual(import_evidence(m,p)['unchanged'],1)
        self.assertEqual(self.costs.detail(ticket['id'])['lines'][0]['quantity'],'10')
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM cost_warehouse_imports').fetchone()[0],1)
    def test_outbound_requires_timezone_and_unique_matching(self):
        m,data=self.manager();data['records']=[{'source_key':'OUT','warehouse_code':'W','warehouse_sku':'S','document_no':'OB-1','kind':'出库单','change_quantity':'-2','raw_time':'2026-08-31 20:00:00','references':['PO']}]
        data['candidates']=[{'source_key':'OUT','platform':'temu','shop_key':'A','order_no':'PO','platform_sku':'SALE','conversion_factor':'2'}]
        p={'kind':'movement','keys':['OUT'],'source_sha256':'proof'}
        with self.assertRaises(CostError):import_evidence(m,p)
        policy=m.valuation.accounting.snapshot()['policy'];m.valuation.accounting.save_policy({**policy,'warehouse_timezone':'UTC'})
        self.assertEqual(import_evidence(m,p)['created'],1)
        row=next(r for r in m.valuation.accounting.snapshot()['records'] if r['kind']=='movement')
        self.assertEqual(row['movement_date'],'2026-09-01');self.assertEqual(row['quantity'],'1');self.assertEqual(row['warehouse_quantity'],'2')
        p['source_sha256']='old'
        with self.assertRaises(CostError):import_evidence(m,p)
    def test_timezone_dst_ambiguity_is_not_guessed(self):
        self.assertEqual(accounting_date('2026-09-01 00:00:00','Asia/Shanghai'),'2026-09-01')
        for value in ('2026-11-01 01:30:00','2026-03-08 02:30:00'):
            with self.assertRaises(CostError):accounting_date(value,'America/New_York')

if __name__=='__main__':unittest.main()
