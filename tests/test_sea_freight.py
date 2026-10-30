import base64
import io
import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from decimal import Decimal
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

from app import Store, Manager, Handler, ThreadingHTTPServer
from excel_export import Formula, workbook
from procurement import CostStore, CostError
from sea_freight import FIELDS, FEES, SeaFreightStore, parse_workbook, read_xlsx
from test_procurement import fixture


def bill(**changes):
    row = {key: '' for key, _ in FIELDS}
    row.update({key: 0 for key, _, _ in FEES})
    row.update(forwarder='测试货代', order_no='SEA-1', bill_no='BL-1', departure_date='2026-08-29', sea_usd=Decimal('123.45'), usd_total=Decimal('123.45'), trucking_cny=Decimal('66.78'), cny_total=Decimal('66.78'), payment_status='待确认（账单无付款记录）', notes='日期存在差异')
    row.update(changes)
    return row


def source(rows=None, extras=(), other_sheets=()):
    rows = rows if rows is not None else [bill()]
    fields = FIELDS + list(extras)
    return workbook([('汇总总览', [['项目','金额'], ['参考汇率 USD/CNY',Decimal('6.75')]]), ('整柜-美国', [[label for _,label in fields]] + [[r.get(k) for k,_ in fields] for r in rows])] + list(other_sheets))


class SeaFreightTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / 'db.sqlite3')
        self.costs = CostStore(self.store)
        self.costs.import_legacy(fixture())
        self.sea = SeaFreightStore(self.store)

    def tearDown(self):
        self.tmp.cleanup()

    def payload(self, blob=None):
        return {'filename':'海运.xlsx', 'reference':'账单凭证', 'xlsx_base64':base64.b64encode(blob if blob is not None else source()).decode()}

    def commit(self, payload):
        return self.sea.import_file({**payload,'revision':self.sea.preview(payload)['revision']})

    def snapshot(self, tables):
        with self.store.connection() as db:
            return {t:[dict(r) for r in db.execute('SELECT * FROM '+t+' ORDER BY 1')] for t in tables}

    def test_preview_import_original_and_duplicate_preserve_existing_costs(self):
        tables = ['cost_tickets','cost_history','cost_templates','cost_migrations']
        before = self.snapshot(tables)
        blob = source(); payload = self.payload(blob)
        preview = self.sea.preview(payload)
        self.assertEqual(preview['counts'], {'new':1,'updated':0,'unchanged':0})
        self.assertEqual(self.sea.list()['rows'], [])
        result = self.commit(payload)
        self.assertEqual(self.sea.original(result['file_id']), blob)
        self.assertEqual(before, self.snapshot(tables))
        prior = self.snapshot(['sea_files','sea_bills','sea_history'])
        self.assertEqual(self.commit(payload)['unchanged'],1)
        self.assertEqual(prior,self.snapshot(prior))
        summary = self.sea.list()['summary']
        self.assertEqual((summary['USD'],summary['CNY'],summary['unconfirmed'],summary['unlinked']),('123.45','66.78',1,1))

    def test_export_reimport_unchanged_and_controlled_formulas_are_cached(self):
        self.commit(self.payload())
        exported = self.sea.export()
        parsed, warnings, rate = parse_workbook(exported)
        self.assertEqual((rate,len(parsed),warnings),('6.75',1,[]))
        plan = self.sea.preview(self.payload(exported))
        self.assertEqual(plan['counts'],{'new':0,'updated':0,'unchanged':1})
        sheets,_ = read_xlsx(exported)
        self.assertEqual(sheets['整柜-美国'][2]['M']['value'],'123.45')
        self.assertTrue(sheets['整柜-美国'][2]['S']['formula'])
        self.assertIn('散货-加拿大',sheets)
        self.assertEqual(Decimal(sheets['整柜-美国'][2]['S']['value']),Decimal('900.0675'))

    def test_review_is_versioned_and_duplicate_original_never_reverts_it(self):
        p=self.payload();self.commit(p)
        row=self.sea.list()['rows'][0]
        updated=self.sea.update(row['id'],{'version':1,'reference':'银行回单','payment_status':'已付','warehouse_date':'2026-09-12','warehouse_date_basis':'实际','purchase_numbers':['PO-09']})
        self.assertEqual(updated['version'],2)
        self.assertEqual(updated['purchase_ticket_ids'],[61])
        self.assertNotIn('付款状态待确认，不作为已确认欠款',updated['checks'])
        self.commit(p)
        self.assertEqual(self.sea.detail(row['id'])['payment_status'],'已付')
        with self.assertRaises(CostError):self.sea.update(row['id'],{'version':1,'reference':'过期修改'})
        for extra in [{'purchase_numbers':['INVALID']},{'warehouse_date':'','warehouse_date_basis':'实际'},{'payment_status':'自动已付'}]:
            with self.assertRaises(CostError):self.sea.update(row['id'],{'version':2,'reference':'核对',**extra})
        self.assertEqual(self.sea.preview(self.payload(self.sea.export()))['counts']['unchanged'],1)

    def test_preview_revision_rejects_concurrent_changes(self):
        self.commit(self.payload())
        p=self.payload(source([bill(sea_usd=200,usd_total=200)]));preview=self.sea.preview(p)
        row=self.sea.list()['rows'][0]
        self.sea.update(row['id'],{'version':1,'reference':'付款凭证','payment_status':'已付'})
        with self.assertRaises(CostError) as e:self.sea.import_file({**p,'revision':preview['revision']})
        self.assertEqual(e.exception.status,409)
        self.assertEqual(self.sea.list()['summary']['USD'],'123.45')

    def test_changed_amounts_and_originals_have_audit_history(self):
        self.commit(self.payload())
        result=self.commit(self.payload(source([bill(sea_usd=Decimal('12.1234567891'),usd_total=Decimal('12.1234567891'))])))
        self.assertEqual(result['updated'],1)
        row=self.sea.list()['rows'][0];self.assertEqual(row['version'],2)
        self.assertEqual(row['usd_total'],'12.1234567891')
        with self.store.connection() as db:
            h=db.execute("SELECT * FROM sea_history WHERE action='updated'").fetchone()
            self.assertEqual(json.loads(h['before_json'])['usd_total'],'123.45')
            self.assertEqual(json.loads(h['after_json'])['usd_total'],'12.1234567891')

    def set_identifiers(self, identity, order='', bl=''):
        ticket=self.costs.detail(identity)
        return self.costs.save_ticket({**ticket,'shipping_order_no':order,'bill_of_lading_no':bl})

    def test_both_identifiers_link_multiple_purchases_without_double_counting_existing_freight(self):
        self.commit(self.payload())
        before={i:self.costs.detail(i) for i in (61,62)}
        self.set_identifiers(61,'sea-1','BL-1');self.set_identifiers(62,'SEA-1')
        row=self.sea.list()['rows'][0]
        self.assertEqual(row['effective_purchase_numbers'],['PO-09','PO-10'])
        self.assertEqual(row['purchase_totals'],{'CNY':'0.3000000003'})
        self.assertEqual(row['container_totals'],{'CNY':'67.0800000003','USD':'123.45'})
        self.assertEqual(row['purchase_extra_fee_totals'],{'CNY':'2000.25','USD':'200.02'})
        self.assertEqual(self.sea.list({'link':'unlinked'})['rows'],[])
        self.assertEqual(self.sea.list()['summary']['unlinked'],0)
        for identity in (61,62):
            self.assertEqual(before[identity]['purchase_amount'],self.costs.detail(identity)['purchase_amount'])
            self.assertEqual(before[identity]['fee_totals'],self.costs.detail(identity)['fee_totals'])
        self.assertEqual(self.sea.preview(self.payload(self.sea.export()))['counts']['unchanged'],1)

    def test_identifier_conflicts_and_ambiguous_matches_do_not_create_container_totals(self):
        self.commit(self.payload(source([bill(),bill(order_no='SEA-2',bill_no='BL-2')])))
        self.set_identifiers(61,'SEA-1','BL-2')
        rows=self.sea.list()['rows']
        self.assertTrue(all(r['container_totals'] is None for r in rows))
        self.assertTrue(all(r['link_issues'] for r in rows))
        self.assertTrue(all(not r['effective_purchase_numbers'] for r in rows))
        self.set_identifiers(61,'SEA-1','DIFFERENT')
        r=next(r for r in self.sea.list()['rows'] if r['order_no']=='SEA-1')
        self.assertIn('对应关系不一致',' '.join(r['link_issues']))
        self.assertIsNone(r['container_totals'])

    def test_identifier_edits_recompute_links_and_missing_purchase_amount_never_becomes_zero(self):
        self.commit(self.payload())
        self.set_identifiers(62,'SEA-1')
        r=self.sea.list()['rows'][0];self.assertEqual(r['container_totals']['CNY'],'66.78')
        ticket=self.costs.detail(62);ticket['lines'][0].update(amount=None,unit_price=None)
        self.costs.save_ticket(ticket)
        r=self.sea.detail(r['id']);self.assertEqual(r['purchase_missing_amounts'],1);self.assertIsNone(r['container_totals'])
        self.set_identifiers(62,'OTHER')
        self.assertEqual(self.sea.list()['summary']['unlinked'],1)

    def test_cross_container_manual_association_requires_purchase_allocation(self):
        self.commit(self.payload(source([bill(),bill(order_no='SEA-2',bill_no='BL-2')])))
        for row in self.sea.list()['rows']:
            self.sea.update(row['id'],{'version':1,'reference':'采购对应凭证','purchase_numbers':['PO-09']})
        for row in self.sea.list()['rows']:
            self.assertIsNone(row['container_totals']);self.assertIn('尚未分摊',' '.join(row['link_issues']))

    def test_invalid_sum_duplicate_business_rows_missing_cache_and_unknown_purchase_are_atomic(self):
        cases=[source([bill(usd_total=1)]),source([bill(),bill()]),source([bill(sea_usd=Formula('1+1'))]),source([bill(purchase_numbers='UNKNOWN')],[('purchase_numbers','采购单号')])]
        for blob in cases:
            with self.subTest(length=len(blob)):
                with self.assertRaises(CostError):self.sea.preview(self.payload(blob))
                self.assertEqual(self.sea.list()['rows'],[])
        p=self.payload();p['revision']='invalid'
        with self.assertRaises(CostError):self.sea.import_file(p)
        self.assertEqual(self.snapshot(['sea_files'])['sea_files'],[])

    def test_transaction_rolls_back_file_and_first_bill_if_later_write_fails(self):
        with self.store.connection() as db:
            db.execute("CREATE TRIGGER fail_second BEFORE INSERT ON sea_bills WHEN (SELECT COUNT(*) FROM sea_bills)=1 BEGIN SELECT RAISE(ABORT,'forced failure'); END")
        p=self.payload(source([bill(),bill(order_no='SEA-2',bill_no='BL-2')]))
        with self.assertRaises(sqlite3.IntegrityError):self.commit(p)
        self.assertEqual(self.snapshot(['sea_files','sea_bills','sea_history']),{'sea_files':[],'sea_bills':[],'sea_history':[]})

    def test_missing_amount_stays_unknown_and_explicit_zero_stays_zero(self):
        self.commit(self.payload(source([bill(usd_total=None,sea_usd=None,cny_total=0,trucking_cny=0)])))
        r=self.sea.list()['rows'][0]
        self.assertIsNone(r['usd_total']);self.assertEqual(Decimal(r['cny_total']),0)
        self.assertEqual(self.sea.list()['summary']['USD_missing'],1)
        exported=self.sea.export();parsed,_,_=parse_workbook(exported)
        self.assertIsNone(parsed[0]['usd_total'])

    def test_quotes_and_estimates_are_not_double_counted_and_status_is_not_assumed_unpaid(self):
        blob=source([bill(sea_usd=0,usd_total=0,trucking_cny=200,cny_total=200,amount_basis='人民币一口价')],other_sheets=[('传美一口价补全',[['海运估算',999999]])])
        self.commit(self.payload(blob))
        d=self.sea.list();self.assertEqual((d['summary']['CNY'],d['summary']['USD'],d['summary']['unconfirmed']),('200.00','0.00',1))
        self.assertEqual(self.sea.list({'payment':'unpaid'})['rows'],[])
        self.assertEqual(len(self.sea.list({'payment':'unconfirmed','end':'2026-08-29','start':'2026-08-29','link':'unlinked','q':'SEA-1'})['rows']),1)
        self.assertEqual(len(self.sea.list({'start':'2026-08-30'})['rows']),0)

    def test_xlsx_limits_invalid_zip_and_literal_formula_text(self):
        for blob in [b'not zip',b'',b'x'*(6*1024*1024+1)]:
            with self.assertRaises(CostError):read_xlsx(blob)
        self.commit(self.payload(source([bill(notes='=HYPERLINK("http://example.test")')])) )
        parsed,_,_=parse_workbook(self.sea.export());self.assertEqual(parsed[0]['notes'],'=HYPERLINK("http://example.test")')
        sheets,_=read_xlsx(self.sea.export());self.assertFalse(sheets['整柜-美国'][2]['AI']['formula'])

    def test_http_guards_preview_import_review_export_and_source(self):
        class TestHandler(Handler):
            manager=Manager(self.store)
            def log_message(self,*args):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),TestHandler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        base='http://127.0.0.1:'+str(server.server_port)
        def request(path,payload=None,headers=None,method=None):
            req=urllib.request.Request(base+path,json.dumps(payload).encode() if payload is not None else None,headers or {'Content-Type':'application/json','X-Pangu-Request':'restock'},method=method)
            with urllib.request.urlopen(req,timeout=5) as r:return r.read(),r.headers
        try:
            for headers in [{'Content-Type':'application/json'},{'Content-Type':'application/json','X-Pangu-Request':'restock','Origin':'https://other.test'}]:
                with self.assertRaises(urllib.error.HTTPError) as e:request('/api/sea-freight/import/preview',{},headers)
                self.assertEqual(e.exception.code,403);e.exception.close()
            p=self.payload();raw,_=request('/api/sea-freight/import/preview',p);preview=json.loads(raw)['data']
            raw,_=request('/api/sea-freight/import',{**p,'revision':preview['revision']});result=json.loads(raw)['data']
            raw,_=request('/api/sea-freight/files/'+str(result['file_id']));self.assertEqual(raw,source())
            raw,_=request('/api/sea-freight');row=json.loads(raw)['data']['rows'][0]
            raw,_=request('/api/sea-freight/bills/'+str(row['id']),{'version':1,'reference':'核对凭证','payment_status':'已付'},method='PUT')
            self.assertEqual(json.loads(raw)['data']['payment_status'],'已付')
            raw,headers=request('/api/sea-freight/export.xlsx');self.assertIn('spreadsheetml',headers['Content-Type']);self.assertEqual(len(parse_workbook(raw)[0]),1)
            raw,headers=request('/sea-freight.js');self.assertIn(b'openSeaImport',raw)
        finally:
            server.shutdown();server.server_close();thread.join(2);TestHandler.manager.exports.pool.shutdown(wait=True)


if __name__=='__main__':unittest.main()
