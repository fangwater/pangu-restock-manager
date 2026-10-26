#!/usr/bin/env python3
"""Compare every legacy business field with the imported restock snapshot."""
import argparse
import json
import sqlite3
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import Store
from procurement import CostStore


def verify(source, actual):
    numeric = {'quantity', 'unit_price', 'amount'}
    tables = {'templates': actual['templates'], 'tickets': actual['tickets'],
              'categories': [c for t in actual['templates'] for c in t['categories']],
              'types': [k for t in actual['templates'] for c in t['categories'] for k in c['types']],
              'lines': [l for t in actual['tickets'] for l in t['lines']],
              'costs': [c for t in actual['tickets'] for c in t['costs']]}
    errors = []
    fields = 0
    for name, expected in source.items():
        if name not in tables:
            continue
        imported = {r['id']: r for r in tables[name]}
        if len(imported) != len(expected):
            errors.append(f'{name}: row count differs')
        for row in expected:
            found = imported.get(row['id'], {})
            for key, value in row.items():
                fields += 1
                result = found.get(key)
                equal = result == value
                if key == 'currency' and value == 'RMB' and result == 'CNY':
                    equal = True
                if key in numeric and value is not None and result is not None:
                    equal = Decimal(str(result)) == Decimal(str(value))
                if not equal:
                    errors.append(f'{name}/{row["id"]}/{key}: value differs')
    return {'checked_fields': fields, 'counts': {k: len(v) for k, v in tables.items()}, 'errors': errors}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', required=True)
    parser.add_argument('--database', required=True)
    parser.add_argument('--before')
    args = parser.parse_args()
    source = json.loads(Path(args.source).read_text(), parse_float=Decimal)
    result = verify(source, CostStore(Store(args.database)).snapshot(full=True))
    result['protected_tables'] = []
    if args.before:
        with sqlite3.connect('file:' + args.before + '?mode=ro', uri=True) as before, sqlite3.connect('file:' + args.database + '?mode=ro', uri=True) as after:
            for name in ('plans', 'settings', 'categories', 'sku_catalog', 'planning_rules', 'region_rules', 'audit'):
                expected = list(before.execute('SELECT * FROM ' + name))
                current = list(after.execute('SELECT * FROM ' + name))
                if sorted(expected) != sorted(current):
                    result['errors'].append(f'protected table changed: {name}')
                result['protected_tables'].append({'name': name, 'before': len(expected), 'after': len(current)})
    print(json.dumps(result, ensure_ascii=False))
    if result['errors']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
