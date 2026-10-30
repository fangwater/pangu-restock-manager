import copy
import csv
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from app import Store, Manager
from procurement import CostStore, CostError
from purchase_import import FIELDS, export_csv, parse_csv, preview, apply, history, encode_rows
from test_procurement import fixture

class PurchaseImportTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.store=Store(Path(self.temp.name)/'restock.sqlite3');self.costs=CostStore(self.store);self.costs.import_legacy(fixture())
    def tearDown(self):self.temp.cleanup()
    def source(self,**filters):return export_csv(self.costs,filters).decode('utf-8-sig')
    def rewrite(self,raw,fn):
        rows=parse_csv(raw)[1];rows=fn(rows) or rows
        return encode_rows([v for _,v in FIELDS],[[r[k] for k,_ in FIELDS] for r in rows]).decode('utf-8-sig')
    def request(self,raw):return {'csv':raw,'reference':'采购凭证-测试'}
    def execute(self,payload):return apply(self.costs,{**payload,'revision':preview(self.costs,payload)['revision']})
    def all_rows(self):
        with self.store.connection() as db:
            return {table:[dict(r) for r in db.execute('SELECT * FROM '+table+' ORDER BY 1')] for table in ('cost_tickets','cost_templates','cost_history','cost_migrations','cost_data_migrations','cost_ticket_imports')}
    def test_round_trip_and_preview_have_no_side_effects_or_revision_churn(self):
        raw=self.source();before=self.all_rows();p=self.request(raw)
        plan=preview(self.costs,p);self.assertEqual(plan['summary'],{'created':0,'updated':0,'unchanged':2,'tickets':2,'csv_rows':4})
        result=apply(self.costs,{**p,'revision':plan['revision']});self.assertIsNone(result['batch_id']);self.assertEqual(before,self.all_rows())
    def test_patch_preserves_omitted_rows_ids_currencies_and_records_audited_changes(self):
        original_fees=copy.deepcopy(self.costs.detail(61)['costs'])
        raw=self.rewrite(self.source(q='PO-09'),lambda rows:[dict(r,amount='12.1234567891') for r in rows if r['kind']=='SKU'])
        p=self.request(raw);plan=preview(self.costs,p);self.assertEqual(plan['summary']['updated'],1)
        self.assertEqual(plan['rows'][0]['after']['fee_totals'],{'CNY':'2000.25','USD':'200.02'})
        result=apply(self.costs,{**p,'revision':plan['revision']});ticket=self.costs.detail(61)
        self.assertEqual(ticket['lines'][0]['id'],100);self.assertEqual(ticket['lines'][0]['amount'],'12.1234567891');self.assertEqual(ticket['costs'],original_fees)
        self.assertEqual([c['id'] for c in ticket['costs']],[31,32]);self.assertEqual(ticket['purchase_currency'],'CNY')
        batch=history(self.costs,result['batch_id']);self.assertEqual(batch['result']['rows'][0]['changes'][0]['before'],'0.3000000003')
        with self.store.connection() as db:
            audit=json.loads(db.execute("SELECT after_json FROM cost_history WHERE action='import_update'").fetchone()[0]);self.assertEqual(audit['import']['reference'],p['reference']);self.assertEqual(audit['import']['file_sha256'],plan['file_sha256'])
        before=self.all_rows();self.assertEqual(self.execute(p)['summary']['unchanged'],1);self.assertEqual(before,self.all_rows())
    def test_creates_multiple_tickets_and_uses_existing_cost_type_rules(self):
        raw=self.rewrite(self.source(q='PO-09'),lambda rows:[dict(r,ticket_no='NEW-1',version='0') for r in rows]+[dict(rows[0],ticket_no='NEW-2',version='0',template='',template_version='',amount='',unit_price='0.1000000001')])
        result=self.execute(self.request(raw));self.assertEqual(result['summary']['created'],2)
        tickets={t['ticket_no']:t for t in self.costs.snapshot(full=True)['tickets']}
        self.assertEqual(tickets['NEW-2']['lines'][0]['amount'],'0.3000000003');self.assertEqual(tickets['NEW-1']['fee_totals'],{'CNY':'2000.25','USD':'200.02'})
    def test_stale_or_deleted_records_template_changes_and_preview_edit_are_rejected(self):
        p=self.request(self.rewrite(self.source(q='PO-09'),lambda rows:[dict(r,amount='9') for r in rows if r['kind']=='SKU']));plan=preview(self.costs,p)
        ticket=self.costs.detail(61);ticket['note']='另一位操作人';self.costs.save_ticket(ticket);before=self.all_rows()
        with self.assertRaises(CostError) as e:apply(self.costs,{**p,'revision':plan['revision']})
        self.assertEqual(e.exception.status,409);self.assertEqual(before,self.all_rows())
        p=self.request(self.source());plan=preview(self.costs,p)
        with self.assertRaises(CostError):apply(self.costs,{**p,'reference':'changed','revision':plan['revision']})
        t=self.costs.snapshot()['templates'][0];t['name']='改名后模板';self.costs.save_template(t)
        with self.assertRaises(CostError):preview(self.costs,p)
        self.costs.remove('ticket',{'id':61,'version':self.costs.detail(61)['version']})
        with self.assertRaises(CostError):preview(self.costs,p)
    def test_currency_mismatch_duplicate_headers_rows_and_inconsistent_metadata_do_not_write(self):
        before=self.all_rows();raw=self.source(q='PO-09')
        variants=[raw.replace('费用币种','币种拼错'),raw.replace('单价,金额','金额,金额'),
                  self.rewrite(raw,lambda rows:rows+[rows[0]]),
                  self.rewrite(raw,lambda rows:[dict(r,fee_currency='USD') if r['fee_currency']=='CNY' else r for r in rows]),
                  self.rewrite(raw,lambda rows:[dict(r,purchase_currency='EUR') if i==0 else r for i,r in enumerate(rows)]),
                  self.rewrite(raw,lambda rows:[dict(r,template_version='0') for r in rows])]
        for variant in variants:
            with self.subTest(variant=variant),self.assertRaises(CostError):preview(self.costs,self.request(variant))
        self.assertEqual(before,self.all_rows())
    def test_blank_is_unknown_and_zero_is_known_and_new_unknown_total_is_not_zero(self):
        raw=self.rewrite(self.source(q='PO-10'),lambda rows:[dict(rows[0],amount='',unit_price='')]);result=self.execute(self.request(raw))
        self.assertIsNone(self.costs.detail(62)['lines'][0]['amount']);self.assertIsNone(result['rows'][0]['after']['purchase_amount'])
        raw=self.rewrite(self.source(q='PO-10'),lambda rows:[dict(rows[0],amount='0')]);self.execute(self.request(raw));self.assertEqual(self.costs.detail(62)['lines'][0]['amount'],'0')
    def test_csv_export_filters_all_rows_and_escapes_spreadsheet_formulas_reversibly(self):
        for name in ['=SUM(A1:A2)',"'=LITERAL",'-NEW','@NEW','+NEW']:
            self.costs.save_ticket({'version':0,'ticket_no':name,'purchase_currency':'CNY','lines':[{'warehouse_sku':name,'quantity':'1','amount':'0','note':name}]})
        raw=self.source();self.assertIn("'=SUM",raw);self.assertIn("''=LITERAL",raw);self.assertEqual(preview(self.costs,self.request(raw))['summary']['unchanged'],7)
        self.assertEqual(len(parse_csv(self.source(fee='recorded'))[1]),3)
        self.assertEqual(len(parse_csv(self.source(template='4'))[1]),3)
    def test_batch_is_rolled_back_if_a_late_write_fails(self):
        raw=self.rewrite(self.source(),lambda rows:[dict(r,ticket_no='NEW-'+r['ticket_no'],version='0') for r in rows]);p=self.request(raw);plan=preview(self.costs,p);before=self.all_rows();write=self.costs.write_ticket;count=0
        def fail(*args,**kwargs):
            nonlocal count
            count+=1
            if count==2:raise CostError('第二张单保存失败')
            return write(*args,**kwargs)
        with patch.object(self.costs,'write_ticket',side_effect=fail),self.assertRaises(CostError):apply(self.costs,{**p,'revision':plan['revision']})
        self.assertEqual(before,self.all_rows())
    def test_receipt_change_after_preview_invalidates_commit_and_warns_about_affected_batches(self):
        manager=Manager(self.store);raw=self.rewrite(self.source(q='PO-09'),lambda rows:[dict(rows[0],amount='9')]);p=self.request(raw);plan=preview(self.costs,p)
        manager.valuation.save_record({'version':0,'kind':'receipt','record_no':'RECEIPT','warehouse_code':'W','ticket_id':61,'lines':[{'warehouse_sku':'SKU-A','quantity':'1'}]})
        with self.assertRaises(CostError) as e:apply(self.costs,{**p,'revision':plan['revision']})
        self.assertEqual(e.exception.status,409);self.assertEqual(preview(self.costs,p)['rows'][0]['linked_receipts'],1)
    def test_independent_cny_usd_eur_hkd_fee_currencies_and_zero_are_preserved(self):
        template=self.costs.save_template({'version':0,'name':'多币种费用配置','categories':[{'name':code+'分类','currency':code,'types':[{'name':code+'费用','calculation_mode':'direct_amount'}]} for code in ('CNY','USD','EUR','HKD')]})
        meta={'ticket_no':'FOUR-CURRENCY','version':'0','template':template['name'],'template_version':str(template['version']),'purchase_currency':'CNY','effective_date':'','ticket_note':''}
        row={k:'' for k,_ in FIELDS};row.update(meta,kind='SKU',sku='S',quantity='1',amount='0')
        rows=[row]
        for category in template['categories']:
            fee={k:'' for k,_ in FIELDS};fee.update(meta,kind='费用',cost_type_id=category['types'][0]['id'],category=category['name'],fee=category['types'][0]['name'],fee_currency=category['currency'],amount='0' if category['currency']=='CNY' else '12.1234567891');rows.append(fee)
        raw=encode_rows([v for _,v in FIELDS],[[r[k] for k,_ in FIELDS] for r in rows]).decode('utf-8-sig')
        result=self.execute(self.request(raw));ticket=self.costs.detail(result['rows'][0]['id']);self.assertEqual(ticket['purchase_currency'],'CNY');self.assertEqual(ticket['fee_totals'],{'CNY':'0','USD':'12.1234567891','EUR':'12.1234567891','HKD':'12.1234567891'})
    def test_fee_only_patch_leaves_sku_rows_and_unknown_fee_is_never_zero(self):
        original=self.costs.detail(61)['lines']
        raw=self.rewrite(self.source(q='PO-09'),lambda rows:[dict(r,amount='0') for r in rows if r['fee_currency']=='CNY']);self.execute(self.request(raw));self.assertEqual(self.costs.detail(61)['fee_totals']['CNY'],'0')
        raw=self.rewrite(self.source(q='PO-09'),lambda rows:[dict(r,amount='') for r in rows if r['fee_currency']=='CNY']);self.execute(self.request(raw));t=self.costs.detail(61)
        self.assertNotIn('CNY',t['fee_totals']);self.assertEqual(t['lines'],original);self.assertEqual(t['fee_totals']['USD'],'200.02')
    def test_product_beyond_ten_decimals_is_rejected_instead_of_silently_rounded(self):
        raw=self.rewrite(self.source(q='PO-10'),lambda rows:[dict(rows[0],quantity='9999999999.1234567891',unit_price='99999999.1234567891',amount='')])
        with self.assertRaises(CostError):preview(self.costs,self.request(raw))
    def test_http_write_origin_guards_and_preview_import_history_routes(self):
        import threading,urllib.request,urllib.error
        from app import Handler,ThreadingHTTPServer
        class TestHandler(Handler):
            manager=Manager(self.store)
            def log_message(self,*args):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),TestHandler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();base='http://127.0.0.1:'+str(server.server_port)
        def request(path,payload=None,headers=None):
            req=urllib.request.Request(base+path,json.dumps(payload).encode() if payload is not None else None,headers or {'Content-Type':'application/json','X-Pangu-Request':'restock'})
            with urllib.request.urlopen(req,timeout=5) as response:return response.read(),response.headers
        try:
            script,headers=request('/purchase-import.js');self.assertIn(b'openPurchaseImport',script);self.assertIn('javascript',headers['Content-Type'])
            for headers in [{'Content-Type':'application/json'},{'Content-Type':'application/json','X-Pangu-Request':'restock','Origin':'https://foreign.test'}]:
                with self.assertRaises(urllib.error.HTTPError) as error:request('/api/costs/tickets/import/preview',{},headers)
                self.assertEqual(error.exception.code,403);error.exception.close()
            raw=self.rewrite(self.source(q='PO-10'),lambda rows:[dict(rows[0],amount='9')]);p=self.request(raw)
            response,_=request('/api/costs/tickets/import/preview',p);plan=json.loads(response)['data']
            response,_=request('/api/costs/tickets/import',{**p,'revision':plan['revision']});result=json.loads(response)['data'];identity=result['batch_id']
            response,_=request('/api/costs/tickets/imports/'+str(identity));batch=json.loads(response)['data'];self.assertNotIn('csv_text',batch);self.assertEqual(batch['result']['summary']['updated'],1)
            response,headers=request('/api/costs/tickets/imports/'+str(identity)+'?export=1');self.assertIn('text/csv',headers['Content-Type']);self.assertEqual(parse_csv(response.decode('utf-8-sig'))[1][0]['amount'],'9')
            response,_=request('/api/costs/tickets/export.csv?q=PO-10');self.assertEqual(len(parse_csv(response.decode('utf-8-sig'))[1]),1)
        finally:server.shutdown();server.server_close();thread.join(2)
    def test_limits_and_bad_shapes_are_explicit(self):
        for raw in ['','库存单号\nA\n','x'*700001,'\ud800',export_csv(self.costs,template=True).decode('utf-8-sig')]:
            with self.assertRaises(CostError):preview(self.costs,self.request(raw))
        with self.assertRaises(CostError):apply(self.costs,self.request(self.source()))
        with self.assertRaises(CostError):preview(self.costs,{'csv':self.source(),'reference':''})

    def test_shipping_identifiers_export_patch_and_legacy_headers_preserve_existing_identifiers(self):
        ticket=self.costs.detail(61)
        self.costs.save_ticket({**ticket,'shipping_order_no':'SEA-1','bill_of_lading_no':'BL-1'})
        raw=self.source(q='SEA-1');rows=parse_csv(raw)[1]
        self.assertTrue(rows);self.assertTrue(all(r['shipping_order_no']=='SEA-1' and r['bill_of_lading_no']=='BL-1' for r in rows))
        changed=self.rewrite(raw,lambda rows:[dict(r,bill_of_lading_no='BL-2') for r in rows])
        plan=preview(self.costs,self.request(changed));self.assertEqual(plan['summary']['updated'],1)
        self.assertIn('提单号',[c['field'] for c in plan['rows'][0]['changes']])
        self.execute(self.request(changed));self.assertEqual(self.costs.detail(61)['bill_of_lading_no'],'BL-2')
        current=parse_csv(self.source(q='PO-09'))[1]
        legacy=encode_rows([v for _,v in FIELDS[:-2]],[[r[k] for k,_ in FIELDS[:-2]] for r in current]).decode('utf-8-sig')
        self.assertEqual(self.execute(self.request(legacy))['summary']['unchanged'],1)
        self.assertEqual(self.costs.detail(61)['shipping_order_no'],'SEA-1')

if __name__=='__main__':unittest.main()
