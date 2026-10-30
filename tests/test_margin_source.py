import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

from app import Manager, Store
from procurement import CostError


class MarginSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.manager = Manager(Store(Path(self.temp.name) / 'restock.sqlite3'))

    @staticmethod
    def financial_body(padding=0):
        return json.dumps({'success': True, 'data': {
            'version': 1, 'timezone': 'Asia/Shanghai', 'sha256': 'source-evidence',
            'start': '2026-08-01', 'end': '2026-09-30', 'as_of': '2026-09-30',
            'items': [{'shop_key': 'A', 'order_no': 'PO1', 'platform_sku': 'SKU1',
                       'order_date': '2026-08-01', 'quantity': '1', 'sales_eligible': True}],
            'cash': [{'entry_key': 'final-cash', 'shop_key': 'A', 'order_no': 'PO1',
                      'platform_sku': 'SKU1', 'fee_name': '销售回款', 'amount': '123.45',
                      'currency': 'USD', 'date': '2026-09-20',
                      'order_status': 'matched', 'sku_status': 'matched',
                      'allocation_status': 'matched'}],
            'records': [], 'shops': ['A'], 'source_evidence': 'x' * padding,
        }}).encode()

    def test_cross_month_evidence_above_32_mib_reaches_margin_without_losing_cash(self):
        body = self.financial_body(padding=32 * 1024 * 1024)
        with patch('app.urlopen', return_value=io.BytesIO(body)):
            report = self.manager.margin({'start': '2026-08-01', 'end': '2026-09-30'})
        self.assertEqual(report['cash_sha256'], 'source-evidence')
        self.assertEqual(report['summary']['native_platform_net'], {'USD': '123.45'})
        self.assertEqual(report['rows'][0]['transactions'][0]['entry_key'], 'final-cash')
        self.assertEqual(report['status'], 'review')
        self.assertIsNone(report['summary']['gross_profit'])

    def test_other_sources_keep_the_32_mib_limit(self):
        body = self.financial_body(padding=32 * 1024 * 1024)
        with patch('app.urlopen', return_value=io.BytesIO(body)):
            with self.assertRaisesRegex(ValueError, '接口响应超过大小限制'):
                self.manager.get('http://source.example/snapshot')

    def test_response_limit_accepts_exact_boundary_and_rejects_extra_byte(self):
        body = self.financial_body()
        with patch('app.urlopen', return_value=io.BytesIO(body)):
            result = self.manager.get('http://source.example', max_bytes=len(body))
        self.assertTrue(result['success'])
        with patch('app.urlopen', return_value=io.BytesIO(body)):
            with self.assertRaisesRegex(ValueError, '接口响应超过大小限制'):
                self.manager.get('http://source.example', max_bytes=len(body) - 1)

    def test_source_failure_still_blocks_margin_without_zero_fallback(self):
        with patch('app.urlopen', side_effect=URLError('source unavailable')):
            with self.assertLogs('pangu-restock', level='ERROR'):
                with self.assertRaises(CostError) as caught:
                    self.manager.margin({'start': '2026-08-01', 'end': '2026-09-30'})
        self.assertEqual(caught.exception.status, 502)
        self.assertIn('财务收入源读取失败', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
