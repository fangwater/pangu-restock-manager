#!/usr/bin/env python3
"""Read-only reconciliation of a Temu Excel export and an ERP order-line CSV.

Requires openpyxl. Inputs are never imported into the ERP or used to alter orders.
CSV columns: po, sales_sku, quantity, warehouse_quantity, normalized_status,
             sales_eligible, statistic_time (business timezone).
"""
import argparse
import csv
import json
import os
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill


def reconcile(excel, csv_path, start, end):
    source = load_workbook(excel, read_only=True, data_only=True)
    values = iter(source.active.values)
    headers = next(values)
    expected, actual = Counter(), Counter()
    details = defaultdict(list)
    input_rows = 0
    for row in values:
        record = dict(zip(headers, row))
        if not record.get('订单号'):
            continue
        stamp = str(record['订单创建时间'])[:10]
        if not start <= stamp <= end:
            raise ValueError('Excel 含范围外订单；请使用与筛选范围一致的文件')
        expected[record['订单号'], record['SKU货号']] += Decimal(str(record['应履约件数']))
        input_rows += 1
    source.close()
    with open(csv_path, newline='') as f:
        for row in csv.DictReader(f):
            key = row['po'], row['sales_sku']
            details[key].append(row)
            eligible = row['sales_eligible'].lower() in ('t', 'true', '1')
            cancelled = row['normalized_status'].lower() in ('cancelled', 'canceled', 'cancel', 'refunded', 'refund')
            if eligible and not cancelled and start <= row['statistic_time'][:10] <= end:
                actual[key] += Decimal(row['quantity'])
    keys = sorted(set(expected) | {k for k, v in actual.items() if v})
    differences = [k for k in keys if expected[k] != actual[k]]
    return expected, actual, details, keys, differences, input_rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--excel', required=True)
    parser.add_argument('--orders-csv', required=True)
    parser.add_argument('--start', required=True)
    parser.add_argument('--end', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    expected, actual, details, keys, differences, input_rows = reconcile(args.excel, args.orders_csv, args.start, args.end)
    report = Workbook()
    summary = report.active
    summary.title = '验收汇总'
    summary.append(['项目', '结果'])
    stats = {'原始文件行数': input_rows, 'Excel订单与SKU组合': len(expected), '核对组合数': len(keys),
             '一致组合数': len(keys)-len(differences), '差异组合数': len(differences),
             'Excel销量': float(sum(expected.values())), '系统销量': float(sum(actual.values()))}
    for row in [('验证文件', Path(args.excel).name), ('日期范围', args.start+' 至 '+args.end),
                ('验证时间UTC', datetime.now(timezone.utc).isoformat()), *stats.items(),
                ('用途', 'Excel 仅用于逐订单/SKU校验，不导入或修改系统数据'),
                ('统计口径', '平台下单时间；quantity 求和；取消/退款状态不计销量')]:
        summary.append(row)
    totals_expected, totals_actual = Counter(), Counter()
    for (_, sku), qty in expected.items(): totals_expected[sku] += qty
    for (_, sku), qty in actual.items(): totals_actual[sku] += qty
    skus = report.create_sheet('SKU汇总')
    skus.append(['销售SKU', 'Excel销量', '系统销量', '差额'])
    for sku in sorted(set(totals_expected) | set(totals_actual)):
        skus.append([sku, float(totals_expected[sku]), float(totals_actual[sku]), float(totals_actual[sku]-totals_expected[sku])])
    for title, selected in [('逐单核验', keys), ('差异明细', differences)]:
        sheet = report.create_sheet(title)
        sheet.append(['PO订单号', '销售SKU', 'Excel销量', '系统销量', '差额', '系统订单状态', '系统平台下单时间'])
        for po, sku in selected:
            rows = details[po, sku]
            sheet.append([po, sku, float(expected[po, sku]), float(actual[po, sku]), float(actual[po, sku]-expected[po, sku]),
                          ', '.join(sorted({r['normalized_status'] for r in rows})),
                          ', '.join(sorted({r['statistic_time'] for r in rows}))])
    for sheet in report:
        sheet.freeze_panes = 'C2'
        sheet.auto_filter.ref = sheet.dimensions
        for cell in sheet[1]:
            cell.font = Font(bold=True, color='FFFFFF')
            cell.fill = PatternFill('solid', fgColor='315DCC')
        for row in sheet:
            for cell in row:
                cell.alignment = Alignment(vertical='top', wrap_text=True)
                if isinstance(cell.value, str) and cell.value.startswith(('=', '+', '-', '@')):
                    cell.data_type = 's'
        for col in sheet.columns:
            sheet.column_dimensions[col[0].column_letter].width = min(60, max(16, max(len(str(c.value or '')) for c in col)+2))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    # Set private permissions before writing order identifiers.
    with os.fdopen(os.open(output, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600), 'wb') as target:
        report.save(target)
    print(json.dumps({'report': str(output), **stats}, ensure_ascii=False))


if __name__ == '__main__':
    main()
