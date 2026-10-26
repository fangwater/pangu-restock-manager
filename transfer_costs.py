#!/usr/bin/env python3
"""Import a read-only legacy export once, without replacing later restock edits."""
import argparse
import json
from decimal import Decimal
from pathlib import Path
from app import Store
from procurement import CostStore


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', required=True)
    parser.add_argument('--database', required=True)
    args = parser.parse_args()
    data = json.loads(Path(args.input).read_text(), parse_float=Decimal)
    # Decimal values become exact decimal strings rather than binary floats.
    data = json.loads(json.dumps(data, default=str, ensure_ascii=False))
    result = CostStore(Store(args.database)).import_legacy(data)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
