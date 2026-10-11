import importlib.util
import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

spec = importlib.util.spec_from_file_location('restock', Path(__file__).parents[1] / 'app.py')
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)


class AllocationTests(unittest.TestCase):
    def test_largest_remainder_conserves_units(self):
        for quantity in range(200):
            for weights in ({'home': 1, 'buy': 1, 'shein': 1}, {'home': 17.7, 'buy': 0, 'shein': 82.3}, {'home': .3333, 'buy': .3333, 'shein': .3334}):
                result = app.allocate(quantity, weights)
                self.assertEqual(sum(result.values()), quantity)
                self.assertTrue(all(isinstance(v, int) and v >= 0 for v in result.values()))

    def test_stable_tie_and_zero_sales(self):
        self.assertEqual(app.allocate(2, {'home': 1, 'buy': 1, 'shein': 1}), {'home': 1, 'buy': 1, 'shein': 0})
        self.assertEqual(app.allocate(0, {}), dict.fromkeys(app.SHOPS, 0))
        with self.assertRaises(app.APIError):
            app.allocate(1, {})
        with self.assertRaises(app.APIError):
            app.allocate(1, {'home': float('nan')})


class StoreAndAnalyticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = app.Store(Path(self.temp.name) / 'db.sqlite3')
        self.manager = app.Manager(self.store)
        today = app.datetime.now(app.TZ).date()
        self.today = today
        points = [{'date': str(today), 'shop': k, 'sku': 'SKU-A', 'name': '商品', 'units': n} for k, n in zip(app.SHOPS, [30, 15, 15])]
        # A 30-day window includes today and day -29, but excludes day -30.
        points += [{'date': str(today-timedelta(days=29)), 'shop': 'home', 'sku': 'SKU-A', 'name': '商品', 'units': 30},
                   {'date': str(today-timedelta(days=30)), 'shop': 'buy', 'sku': 'SKU-A', 'name': '商品', 'units': 300}]
        self.store.source('sales', data={'points': points, 'start': str(today-timedelta(days=89)), 'end': str(today), 'sources': []})
        self.store.source('inventory', data=[{'sku': 'SKU-A', 'product_name': '商品', 'available_amount': 10, 'transport_amount': 5, 'warehouses': {}}, {'sku': 'SKU-Z', 'product_name': '暂无销量', 'available_amount': 0, 'transport_amount': 0, 'warehouses': {}}])

    def tearDown(self):
        self.temp.cleanup()

    def change(self, sku='SKU-A', version=0, quantity=11, mode='sales', **extra):
        return self.manager.validate_plan({'sku': sku, 'quantity': quantity, 'mode': mode, 'version': version, 'status': 'draft', **extra}, self.manager.dashboard())

    def test_shop_totals_date_boundaries_and_restock(self):
        dashboard = self.manager.dashboard()
        row = next(r for r in dashboard['rows'] if r['sku'] == 'SKU-A')
        self.assertEqual(row['sales'], {'home': 60, 'buy': 15, 'shein': 15})
        self.assertEqual(row['total'], 90)
        self.assertEqual(row['prior_total'], 300)
        self.assertEqual(row['suggested'], 98)  # 90/30 * 30 * 1.2 - 10
        self.assertEqual(sum(row['shares'].values()), 100)
        self.assertEqual(sum(sum(p[k] for k in app.SHOPS) for p in dashboard['series']), 90)
        self.assertEqual(dashboard['range']['start'], str(self.today-timedelta(days=29)))

    def test_missing_inventory_does_not_invent_suggestion(self):
        with self.store.connection() as db:
            db.execute("DELETE FROM snapshots WHERE source='inventory'")
        row = self.manager.dashboard()['rows'][0]
        self.assertIsNone(row['available'])
        self.assertIsNone(row['suggested'])

    def test_stale_source_pauses_restock_suggestion(self):
        with self.store.connection() as db:
            old = (app.datetime.now(app.TZ)-timedelta(hours=1)).isoformat()
            db.execute("UPDATE snapshots SET fetched_at=? WHERE source='sales'", (old,))
        self.assertIsNone(self.manager.dashboard()['summary']['suggested'])

    def test_failed_sync_keeps_snapshot_and_success_time(self):
        old = self.store.read()[0]['sales']
        self.store.source('sales', error='连接失败')
        snapshots, _, _, sources = self.store.read()
        self.assertEqual(snapshots['sales'], old)
        source = next(s for s in sources if s['source'] == 'sales')
        self.assertEqual(source['status'], 'error')
        self.assertIsNotNone(source['success_at'])

    def test_plan_and_fixed_allocation_survive_sync_and_restart(self):
        self.store.save_plans([self.change()])
        before = self.manager.dashboard()['rows'][0]['allocation']
        self.store.source('sales', data={'points': [{'date': str(self.today), 'shop': 'buy', 'sku': 'SKU-A', 'name': '商品', 'units': 90}], 'sources': []})
        restarted = app.Manager(app.Store(Path(self.temp.name) / 'db.sqlite3'))
        row = next(r for r in restarted.dashboard()['rows'] if r['sku'] == 'SKU-A')
        self.assertEqual(row['plan']['quantity'], 11)
        self.assertEqual(row['allocation'], before)
        self.assertEqual(sum(row['allocation'].values()), 11)

    def test_optimistic_conflict_and_bulk_rollback(self):
        self.store.save_plans([self.change()])
        with self.assertRaises(app.APIError) as caught:
            self.store.save_plans([self.change(sku='SKU-Z', mode='manual', weights={'home': 100, 'buy': 0, 'shein': 0}), self.change(version=0)])
        self.assertEqual(caught.exception.status, 409)
        self.assertNotIn('SKU-Z', self.store.read()[1])
        self.assertEqual(self.store.read()[1]['SKU-A']['version'], 1)

    def test_manual_ratio_validation_and_no_sales(self):
        with self.assertRaises(app.APIError):
            self.change(mode='manual', weights={'home': 40, 'buy': 40, 'shein': 0})
        with self.assertRaises(app.APIError):
            self.change(sku='SKU-Z')
        with self.assertRaises(app.APIError):
            self.change(quantity=1.5)
        change = self.change(sku='SKU-Z', mode='manual', weights={'home': 60, 'buy': 20, 'shein': 20})
        self.store.save_plans([change])
        self.assertEqual(sum(app.allocate(11, change['weights']).values()), 11)

    def test_in_transit_opt_in_and_no_negative_suggestions(self):
        with self.store.connection() as db:
            db.execute('UPDATE settings SET payload=?', (json.dumps({**app.DEFAULT_SETTINGS, 'include_in_transit': True}),))
        self.assertEqual(self.manager.dashboard()['rows'][0]['suggested'], 93)
        with self.store.connection() as db:
            db.execute('UPDATE settings SET payload=?', (json.dumps({**app.DEFAULT_SETTINGS, 'target_days': 1, 'safety_factor': 0}),))
        self.assertEqual(self.manager.dashboard()['rows'][0]['suggested'], 0)

    def test_audit_tracks_updates_and_versions(self):
        self.store.save_plans([self.change(note='首批')])
        saved = self.store.save_plans([self.change(version=1, quantity=20, note='第二批')])[0]
        self.assertEqual(saved['version'], 2)
        with self.store.connection() as db:
            history = list(db.execute('SELECT * FROM audit ORDER BY id'))
        self.assertEqual(len(history), 2)
        self.assertEqual(json.loads(history[1]['before_json'])['note'], '首批')
        self.assertEqual(json.loads(history[1]['after_json'])['quantity'], 20)


if __name__ == '__main__':
    unittest.main()
