import copy
import json
import tempfile
import unittest
from pathlib import Path
from app import Store
from procurement import CostStore, CostError, dumps


def fixture():
    stamp = '2026-09-30T10:00:00+08:00'
    return {
        'templates': [{'id': 4, 'name': '业务成本模板', 'shop_key': 'original-shop', 'created_at': stamp, 'updated_at': stamp}],
        'categories': [{'id': 8, 'template_id': 4, 'name': '配置费用A', 'currency': 'RMB', 'sort_order': 0},
                       {'id': 9, 'template_id': 4, 'name': '配置费用B', 'currency': 'USD', 'sort_order': 1}],
        'types': [{'id': 15, 'category_id': 8, 'name': '费用一', 'calculation_mode': 'direct_amount'},
                  {'id': 16, 'category_id': 9, 'name': '费用二', 'calculation_mode': 'quantity_x_unit'}],
        'tickets': [{'id': 61, 'ticket_no': 'PO-09', 'cost_template_id': 4, 'shop_key': 'original-shop', 'note': '', 'created_at': stamp, 'updated_at': stamp},
                    {'id': 62, 'ticket_no': 'PO-10', 'cost_template_id': None, 'shop_key': 'original-shop', 'note': '', 'created_at': stamp, 'updated_at': stamp}],
        'lines': [{'id': 100, 'inventory_ticket_id': 61, 'warehouse_sku': 'SKU-A', 'quantity': '3', 'unit_price': '0.1000000001', 'amount': '0.3000000003', 'note': ''},
                  {'id': 101, 'inventory_ticket_id': 62, 'warehouse_sku': 'SKU-B', 'quantity': '7', 'unit_price': None, 'amount': '0', 'note': ''}],
        'costs': [{'id': 31, 'inventory_ticket_id': 61, 'cost_type_id': 15, 'quantity': None, 'unit_price': None, 'amount': '2000.25', 'note': ''},
                  {'id': 32, 'inventory_ticket_id': 61, 'cost_type_id': 16, 'quantity': '2', 'unit_price': '100.01', 'amount': '200.02', 'note': ''}]
    }


class ProcurementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'restock.sqlite3')
        self.costs = CostStore(self.store)

    def tearDown(self):
        self.temp.cleanup()

    def test_transfer_preserves_ids_amount_precision_nulls_and_currency_separation(self):
        self.costs.import_legacy(fixture())
        snapshot = self.costs.snapshot(full=True)
        self.assertEqual(snapshot['summary']['fee_totals'], {'CNY': '2000.25', 'USD': '200.02'})
        self.assertEqual(snapshot['summary']['without_fees'], 1)
        detail = self.costs.detail(61)
        self.assertEqual(detail['lines'][0]['id'], 100)
        self.assertEqual(detail['lines'][0]['amount'], '0.3000000003')
        self.assertEqual(detail['purchase_currency'], '')
        self.assertIsNone(detail['effective_date'])
        self.assertEqual(self.costs.detail(62)['lines'][0]['unit_price'], None)
        self.assertEqual(self.costs.detail(62)['fee_totals'], {})
        self.assertEqual(snapshot['summary']['purchase_totals'], {'': '0.3000000003'})

    def test_transfer_is_atomic_and_reimport_cannot_overwrite_subsequent_changes(self):
        broken = fixture()
        broken['costs'][0]['cost_type_id'] = 9999
        with self.assertRaises(CostError):
            self.costs.import_legacy(broken)
        self.assertEqual(self.costs.snapshot()['summary']['tickets'], 0)
        self.assertEqual(self.costs.snapshot()['templates'], [])
        self.costs.import_legacy(fixture())
        ticket = self.costs.detail(61)
        ticket['note'] = '备货系统后续修改'
        self.costs.save_ticket(ticket)
        self.assertTrue(self.costs.import_legacy(fixture())['already_imported'])
        self.assertEqual(self.costs.detail(61)['note'], '备货系统后续修改')
        with self.assertRaises(CostError):
            self.costs.import_legacy(broken)

    def test_existing_rmb_normalization_is_once_only_and_preserves_amounts_and_source_archive(self):
        self.costs.import_legacy(fixture())
        with self.store.connection() as db:
            archive = db.execute('SELECT source_payload FROM cost_migrations').fetchone()[0]
            db.execute('DELETE FROM cost_data_migrations')
            for table in ('cost_templates', 'cost_tickets'):
                for row in list(db.execute('SELECT id,payload FROM ' + table)):
                    payload = json.loads(row['payload'])
                    for entry in payload.get('categories', []) + payload.get('costs', []):
                        if entry.get('currency') == 'CNY':
                            entry['currency'] = 'RMB'
                    db.execute('UPDATE ' + table + ' SET payload=? WHERE id=?', (dumps(payload), row['id']))
        migrated = CostStore(self.store)
        first = migrated.detail(61)
        self.assertEqual(first['fee_totals'], {'CNY': '2000.25', 'USD': '200.02'})
        self.assertEqual(first['purchase_currency'], '')
        self.assertEqual(CostStore(self.store).detail(61)['version'], first['version'])
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT source_payload FROM cost_migrations').fetchone()[0], archive)

    def test_purchase_and_quantity_fee_arithmetic_versions_and_missing_values(self):
        self.costs.import_legacy(fixture())
        ticket = self.costs.detail(61)
        ticket['lines'][0]['amount'] = None
        ticket['costs'][1]['unit_price'] = '0.1000000001'
        ticket['costs'][1]['amount'] = '999'
        ticket['purchase_currency'] = 'cny'
        ticket['effective_date'] = '2026-09-01'
        saved = self.costs.save_ticket(ticket)
        self.assertEqual(saved['lines'][0]['amount'], '0.3000000003')
        self.assertEqual(saved['costs'][1]['amount'], '0.2000000002')
        self.assertEqual(saved['purchase_currency'], 'CNY')
        with self.assertRaises(CostError) as conflict:
            self.costs.save_ticket(ticket)
        self.assertEqual(conflict.exception.status, 409)
        saved['lines'].append(copy.deepcopy(saved['lines'][0]))
        with self.assertRaises(CostError):
            self.costs.save_ticket(saved)
        self.assertEqual(len(self.costs.detail(61)['lines']), 1)
        new = self.costs.save_ticket({'version': 0, 'ticket_no': 'NO-TEMPLATE', 'lines': [{'warehouse_sku': 'NEW', 'quantity': '0', 'amount': None}]})
        self.assertIsNone(new['purchase_amount'])
        self.assertEqual(new['costs'], [])
        self.assertGreater(new['id'], 62)

    def test_template_management_preserves_history_and_supports_dynamic_configuration(self):
        self.costs.import_legacy(fixture())
        template = self.costs.snapshot()['templates'][0]
        original = copy.deepcopy(template)
        template['categories'][0]['currency'] = 'EUR'
        with self.assertRaises(CostError):
            self.costs.save_template(template)
        template = original
        template['categories'].append({'name': '新增配置类', 'currency': 'EUR', 'types': [{'name': '新增费用', 'calculation_mode': 'direct_amount'}]})
        saved = self.costs.save_template(template)
        self.assertEqual(saved['categories'][-1]['currency'], 'EUR')
        self.assertTrue(saved['categories'][-1]['types'][0]['id'])
        saved['categories'][0]['types'] = []
        with self.assertRaises(CostError):
            self.costs.save_template(saved)
        with self.assertRaises(CostError):
            self.costs.remove('template', {'id': original['id'], 'version': original['version'] + 1})

    def test_deletion_retains_audit_and_does_not_reappear_in_finance_snapshot(self):
        self.costs.import_legacy(fixture())
        ticket = self.costs.detail(61)
        self.costs.remove('ticket', {'id': 61, 'version': ticket['version']})
        self.assertEqual(self.costs.snapshot(full=True)['summary']['tickets'], 1)
        with self.assertRaises(CostError):
            self.costs.detail(61)
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM cost_tickets').fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT count(*) FROM cost_history WHERE action='delete'").fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main()
