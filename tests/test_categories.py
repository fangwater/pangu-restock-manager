import tempfile
import unittest
from pathlib import Path
import app
from reporting import infer_product
import test_reporting

class CategoryConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.fixture=test_reporting.DecisionTests();self.fixture.setUp()
        self.store,self.manager=self.fixture.store,self.fixture.manager
        self.store.seed_catalog([r['product'] for r in self.manager.dashboard()['rows']])
    def tearDown(self):
        self.fixture.tearDown()
    def category(self, code):
        return next(c for c in self.store.master_data()[1] if c['code']==code)
    def save(self, **change):
        return self.store.save_category(self.manager.validate_category(change))
    def test_config_rules_reclassify_unconfirmed_and_preserve_manual(self):
        r=next(r for r in self.manager.dashboard()['rows'] if r['sku']=='PH+H-20Pcs-Black')
        self.store.save_catalog(self.manager.validate_catalog({**r['product'],'category':'hamper'}))
        self.save(code='storage',name='收纳',icon='box',sort_order=0,version=0,match_rules={'sku_prefixes':['UNKNOWN','PH+H-'],'keywords':[]})
        catalog=self.store.master_data()[0]
        self.assertEqual(catalog['UNKNOWN']['category'],'storage')
        self.assertEqual(catalog['PH+H-10Pcs-White']['category'],'storage')
        self.assertEqual(catalog['PH+H-20Pcs-Black']['category'],'hamper')
        d=self.manager.dashboard();self.assertEqual(d['summary']['total'],65)
        self.assertEqual(next(c for c in d['categories'] if c['code']=='storage')['icon'],'box')
    def test_rename_icon_and_match_rules_are_persistent(self):
        old=self.category('hanger')
        saved=self.save(**{**old,'name':'服饰配件','icon':'layers','match_rules':{'sku_prefixes':['TEST-'],'keywords':[]}})
        self.assertEqual(saved['version'],old['version']+1)
        categories=self.store.master_data()[1]
        self.assertEqual(infer_product('PH+H-20Pcs-Black','衣架',categories=categories)['category'],'uncategorized')
        self.assertEqual(infer_product('TEST-new','衣架',categories=categories)['category'],'hanger')
        restarted=app.Store(self.store.path)
        c=next(c for c in restarted.master_data()[1] if c['code']=='hanger')
        self.assertEqual((c['name'],c['icon'],c['match_rules']),('服饰配件','layers',{'sku_prefixes':['TEST-'],'keywords':[]}))
        with self.assertRaises(app.APIError):self.save(**old)
    def test_bulk_classification_conflict_rolls_back(self):
        catalog=self.store.master_data()[0]
        changes=[self.manager.validate_catalog({**catalog[sku],'category':'hamper'}) for sku in ['PH+H-10Pcs-White','PH+H-20Pcs-Black']]
        changes[1]['version']=0
        with self.assertRaises(app.APIError):self.store.save_catalogs(changes)
        self.assertEqual(self.store.master_data()[0]['PH+H-10Pcs-White']['category'],'hanger')
        with self.store.connection() as db:self.assertEqual(db.execute('SELECT count(*) FROM audit').fetchone()[0],0)
    def test_delete_empty_category_does_not_reappear_and_references_are_protected(self):
        c=self.save(code='empty',name='空类目',icon='package',sort_order=100,version=0,match_rules={})
        self.store.remove_category('empty',c['version'])
        self.assertNotIn('empty',[c['code'] for c in app.Store(self.store.path).master_data()[1]])
        with self.assertRaises(app.APIError):self.store.remove_category('hanger',self.category('hanger')['version'])
        with self.assertRaises(app.APIError):self.store.remove_category('uncategorized',self.category('uncategorized')['version'])
        with tempfile.TemporaryDirectory() as tmp:
            empty=app.Store(Path(tmp)/'empty.db')
            h=next(c for c in empty.master_data()[1] if c['code']=='hanger')
            empty.remove_category('hanger',h['version'])
            self.assertNotIn('hanger',[c['code'] for c in app.Store(empty.path).master_data()[1]])
    def test_legacy_category_metadata_migration_does_not_rename_existing_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            import sqlite3
            p=Path(tmp)/'legacy.db'
            with sqlite3.connect(p) as db:
                db.executescript('CREATE TABLE categories(code TEXT PRIMARY KEY,name TEXT,sort_order INTEGER,version INTEGER);')
                db.execute('INSERT INTO categories VALUES(?,?,?,?)',('hanger','用户类目名',8,7))
                db.execute('INSERT INTO categories VALUES(?,?,?,?)',('uncategorized','其他',999,1))
            c=next(c for c in app.Store(p).master_data()[1] if c['code']=='hanger')
            self.assertEqual((c['name'],c['version']),('用户类目名',7));self.assertEqual(c['icon'],'hanger')
            self.assertIn('hanger',c['match_rules']['keywords'])
