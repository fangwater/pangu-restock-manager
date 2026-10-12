import io
import json
import sqlite3
import tempfile
import time
import unittest
from datetime import date, timedelta
from pathlib import Path
from zipfile import ZipFile
from xml.etree import ElementTree as ET
import app
from reporting import date_window, percentage_parts
from excel_export import report_workbook


class DecisionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = app.Store(Path(self.tmp.name) / 'db.sqlite3')
        self.manager = app.Manager(self.store)
        self.today = app.datetime.now(app.TZ).date()
        points = [dict(date=str(self.today), shop='home', sku='PH+H-20Pcs-Black', name='衣架', units=30, warehouse_units=60, status='shipped'),
                  dict(date=str(self.today), shop='buy', sku='PH+H-10Pcs-White', name='衣架', units=10, warehouse_units=10, status='pending'),
                  dict(date=str(self.today), shop='shein.new-shop', sku='LB-2Pcs-Red', name='脏衣篓', units=20, warehouse_units=20, status='completed'),
                  dict(date=str(self.today-timedelta(days=1)), shop='shein', sku='UNKNOWN', name='待映射', units=5, warehouse_units=5, status='completed')]
        self.store.source('sales', data=dict(version=3, points=[dict(p, sales_sku=p['sku'], conversion_factor=p['warehouse_units']/p['units']) for p in points], start=str(self.today-timedelta(days=729)), end=str(self.today), sources=[]))
        self.store.source('inventory', data=[dict(sku=p['sku'], product_name=p['name'], available_amount=7, transport_amount=5, warehouses={}) for p in points[:3]])
    def tearDown(self):
        self.manager.exports.pool.shutdown(wait=True)
        self.tmp.cleanup()
    def row(self, sku='PH+H-20Pcs-Black', **query):
        return next(r for r in self.manager.dashboard(query)['rows'] if r['sku']==sku)
    def test_dates_quarters_and_inclusive_filters(self):
        self.assertEqual(date_window({'preset':'previous_quarter'}, date(2026,10,11)), (date(2026,7,1),date(2026,9,30)))
        self.assertEqual(date_window({'preset':'current_quarter'}, date(2026,10,11)), (date(2026,10,1),date(2026,10,11)))
        for n in (7,30,90):
            s,e=date_window({'preset':f'last{n}'},self.today);self.assertEqual((e-s).days+1,n)
        self.assertEqual(self.manager.dashboard({'start':str(self.today),'end':str(self.today)})['summary']['total'],60)
        for q in ({'start':'2026-10-12','end':'2026-10-11'},{'start':'invalid','end':'invalid'},{'days':367}):
            with self.assertRaises(app.APIError): self.manager.dashboard(q)
    def test_quantity_basis_category_denominator_dynamic_shops(self):
        d=self.manager.dashboard();self.assertEqual(d['summary']['total'],65)
        self.assertIn('shein.new-shop',[s['key'] for s in d['shops']])
        r=self.row();self.assertEqual(r['total'],30);self.assertEqual(r['warehouse_total'],60)
        self.assertEqual(r['category_share'],75);self.assertEqual(r['global_share'],46.15)
        filtered=self.manager.dashboard({'category':'hanger'})
        self.assertEqual(filtered['summary']['total'],40);self.assertEqual(filtered['summary']['global_total'],65)
        self.assertAlmostEqual(sum(c['share'] for c in d['categories']),100)
        self.assertEqual(sum(r['category_share'] for r in filtered['rows']),100)
        self.assertEqual(self.manager.dashboard({'shops':'home','statuses':'shipped'})['summary']['total'],30)
        self.assertEqual(self.manager.dashboard({'shops':'home','statuses':'completed'})['summary']['total'],0)
    def test_whole_box_conversion_rule_precedence_and_unknown_inventory(self):
        r=self.row();self.assertEqual(r['suggested'],65) # 60/30 * 30 * 1.2 - 7
        p=self.manager.validate_catalog({**r['product'],'version':0,'box_size':12})
        self.store.save_catalog(p)
        self.assertEqual(self.row()['suggested'],72)
        self.store.save_rule(dict(key='category',category='hanger',shop='',target_days=60,safety_factor=.2,box_size=1,include_in_transit=True,version=0))
        r=self.row();self.assertEqual(r['suggested'],132) # ceil((144 - 7 - 5)/12)*12
        self.assertIsNone(r['basis']['conversion_ratio']);self.assertEqual(r['demand_sources'][0]['conversion_factor'],2)
        self.assertIsNone(self.row('UNKNOWN')['suggested']);self.assertIsNone(self.row('UNKNOWN')['available'])
        with self.assertRaises(app.APIError):self.store.save_catalog(p)
    def test_locked_and_non_draft_plans_survive_recompute(self):
        d=self.manager.dashboard();changes=[]
        for r in d['rows'][:3]:
            changes.append(self.manager.validate_plan(dict(sku=r['sku'],quantity=100,version=0,locked=r['sku']=='PH+H-20Pcs-Black',status='confirmed' if r['sku']=='LB-2Pcs-Red' else 'draft'),d))
        self.store.save_plans(changes)
        before=self.store.read()[1]['PH+H-20Pcs-Black']
        result=self.manager.recompute({})
        self.assertEqual(result['updated'],1)
        plans=self.store.read()[1];self.assertEqual(plans['PH+H-20Pcs-Black'],before)
        self.assertEqual(plans['LB-2Pcs-Red']['quantity'],100)
        self.assertEqual(plans['PH+H-10Pcs-White']['quantity'],5)
        self.assertEqual(plans['PH+H-10Pcs-White']['version'],2)
    def test_xlsx_and_async_download_preserve_numeric_and_filter_basis(self):
        d=self.manager.dashboard();d['filters']['q']='';body=report_workbook(d,'purchase')
        with ZipFile(io.BytesIO(body)) as z:
            for name in z.namelist():
                if name.endswith(('.xml','.rels')):ET.fromstring(z.read(name))
            self.assertIn('类目占比',z.read('xl/workbook.xml').decode())
            sheet=z.read('xl/worksheets/sheet4.xml').decode();self.assertIn('日均仓库需求',sheet);self.assertIn('<v>65</v>',sheet)
            self.assertNotIn('<f>',sheet)
        job=self.manager.exports.create(d,'purchase')
        for _ in range(100):
            job=self.manager.exports.get(job['id'])
            if job['status'] not in ('queued','running'):break
            time.sleep(.01)
        self.assertEqual(job['status'],'ready')
        with ZipFile(io.BytesIO(self.manager.exports.download(job['id'])[0])) as actual, ZipFile(io.BytesIO(body)) as expected:
            self.assertEqual({n: actual.read(n) for n in actual.namelist()}, {n: expected.read(n) for n in expected.namelist()})
        self.assertIsNone(self.manager.exports.download('../db.sqlite3'))
    def test_legacy_quantity_cache_and_schema_migration_preserve_plan(self):
        snapshots,plans,settings,sources=self.store.read()
        payload=snapshots['sales']['data'];payload.pop('version', None)
        for p in payload['points']:p['platform_units']=p['units'];p['units']=p.pop('warehouse_units')
        self.store.source('sales',data=payload)
        self.assertEqual(self.row()['total'],30);self.assertEqual(self.row()['warehouse_total'],60)
        legacy=Path(self.tmp.name)/'old.sqlite3'
        db=sqlite3.connect(legacy)
        db.executescript("CREATE TABLE plans(sku TEXT PRIMARY KEY,quantity INTEGER,status TEXT,mode TEXT,weights TEXT,note TEXT,basis_days INTEGER,version INTEGER,created_at TEXT,updated_at TEXT);CREATE TABLE settings(id INTEGER PRIMARY KEY,payload TEXT);")
        db.execute('INSERT INTO plans VALUES(?,?,?,?,?,?,?,?,?,?)',('old',73,'draft','manual','{"home":100,"buy":0,"shein":0}','保留',30,4,app.now(),app.now()))
        db.execute('INSERT INTO settings VALUES(1,?)',(json.dumps(dict(target_days=30,safety_days=7,include_in_transit=False)),));db.commit();db.close()
        migrated=app.Store(legacy);p=migrated.read()[1]['old'];self.assertEqual(p['quantity'],73);self.assertEqual(p['version'],4);self.assertTrue(p['locked']);self.assertEqual(migrated.read()[2]['safety_factor'],.2)
    def test_percentages_sum_to_100_for_complete_dimensions(self):
        self.assertEqual(percentage_parts({'a':1,'b':1,'c':1}),{'a':33.34,'b':33.33,'c':33.33})
        self.assertEqual(sum(app.allocate(13,{'home':1,'buy':1,'shein':1,'new':1},['home','buy','shein','new']).values()),13)

    def test_sold_packs_and_procurement_are_independent_and_exported(self):
        small, large = 'VH+H-50Pcs-Pink-45cm', 'VH+H-100Pcs-Pink-45cm'
        points = [dict(date=str(self.today), shop=shop, sales_sku=sold, sku=warehouse, units=qty,
                       warehouse_units=qty*factor, conversion_factor=factor, status='shipped')
                  for shop, sold, warehouse, qty, factor in [
                      ('home', small, small, 561, 1), ('home', large, small, 64, 2),
                      ('buy', small, small, 100, 1), ('buy', large, large, 10, 1)]]
        points.append({**points[1], 'date': str(self.today-timedelta(days=30)), 'units': 20, 'warehouse_units': 40})
        self.store.source('sales', data=dict(version=3, points=points, start=str(self.today-timedelta(days=90)), end=str(self.today), sources=[]))
        self.store.source('inventory', data=[dict(sku=s, available_amount=0, transport_amount=0, warehouses={}) for s in (small, large)])
        d=self.manager.dashboard(); rows={r['sku']:r for r in d['rows']}
        a,b=rows[small],rows[large]
        self.assertEqual(a['sales']['home'],561);self.assertEqual(b['sales']['home'],64)
        self.assertEqual(a['warehouse_sales']['home'],689);self.assertEqual(b['warehouse_sales']['home'],0)
        self.assertEqual(b['prior_total'],20);self.assertEqual(a['prior_total'],0)
        self.assertEqual(d['summary']['total'],735)
        self.assertEqual(sum(sum(p[k] for k in ('home','buy','shein')) for p in d['series']),735)
        self.assertEqual(sum(r['warehouse_total'] for r in d['rows']),799)
        self.assertEqual(a['suggested'],947);self.assertEqual(b['suggested'],12)
        self.assertEqual(sum(x['warehouse_units'] for x in a['demand_sources']),789)
        self.assertIsNone(a['basis']['conversion_ratio'])
        plan=self.manager.validate_plan(dict(sku=small,quantity=789,version=0),d)
        self.assertEqual(plan['weights'],dict(home=689,buy=100,shein=0))
        self.store.save_plans([plan]);before=self.store.read()[1][small]
        self.assertEqual(self.row(small)['allocation'],dict(home=689,buy=100,shein=0))
        self.assertEqual(self.manager.recompute({})['updated'],0)
        self.assertEqual(self.store.read()[1][small],before)
        filtered=self.manager.dashboard({'shops':'home'}); fr={r['sku']:r for r in filtered['rows']}
        self.assertEqual(fr[small]['total'],561);self.assertEqual(fr[small]['warehouse_total'],689)
        self.assertEqual(fr[large]['total'],64);self.assertEqual(fr[large]['suggested'],0)
        self.assertEqual(self.manager.dashboard({'statuses':'completed'})['summary']['total'],0)
        with ZipFile(io.BytesIO(report_workbook(filtered,'purchase'))) as z:
            ns={'m':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
            def rows_at(index):
                root=ET.fromstring(z.read(f'xl/worksheets/sheet{index}.xml'))
                return [[ ''.join(c.itertext()) for c in r ] for r in root.findall('.//m:sheetData/m:row',ns)]
            detail=rows_at(3);header=detail[0];line=next(r for r in detail[1:] if r[1]==small)
            self.assertEqual(line[header.index('Panda Homes 销量')],'561')
            self.assertEqual(line[header.index('仓库换算需求')],'689')
            demand=rows_at(5)
            source=next(r for r in demand[1:] if r[0]=='当前统计' and r[3]==large and r[1]==small)
            self.assertEqual(source[4:7],['64','2','128'])

    def test_demand_without_direct_sales_and_legacy_cache_safety(self):
        self.store.source('sales',data=dict(version=3,points=[dict(date=str(self.today),shop='home',sales_sku='PACK-100',sku='PACK-50',units=3,warehouse_units=6,conversion_factor=2,status='pending')],start=str(self.today-timedelta(days=90)),end=str(self.today),sources=[]))
        self.store.source('inventory',data=[dict(sku='PACK-50',available_amount=0,transport_amount=0,warehouses={})])
        r=self.row('PACK-50');self.assertEqual(r['total'],0);self.assertEqual(r['warehouse_total'],6)
        self.assertEqual(r['stock_status'],'out');self.assertEqual(r['suggested'],8)
        plan=self.manager.validate_plan(dict(sku='PACK-50',quantity=8,version=0),self.manager.dashboard())
        self.assertEqual(plan['weights']['home'],6)
        snapshots,*_=self.store.read();data=snapshots['sales']['data'];data['version']=2
        self.store.source('sales',data=data)
        self.assertIsNone(self.row('PACK-50')['suggested'])
        self.assertTrue(any('旧销量快照' in w for w in self.manager.dashboard()['warnings']))
