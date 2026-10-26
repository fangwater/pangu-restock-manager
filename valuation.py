"""FIFO purchase-cost inputs, exact decimal replay, and source traceability."""
import csv
import hashlib
import io
import json
import sqlite3
from collections import defaultdict, deque
from datetime import date, datetime
from decimal import Decimal, localcontext, ROUND_HALF_UP
from landed import AccountingStore
from procurement import CostError, CostStore, currency, number, record_id, text, timestamp, dumps

ZERO = Decimal(0)
QUANTUM = Decimal('0.0000000001')


def day(value, label, optional=False):
    value = text(value, label, not optional, 10)
    if not value and optional:
        return None
    try:
        if date.fromisoformat(value).isoformat() != value:
            raise ValueError()
    except ValueError:
        raise CostError(f'{label}须为 YYYY-MM-DD')
    return value


def dec(value):
    return Decimal(str(value))


def fmt(value):
    return format(value, 'f')


def rounded(value):
    return value.quantize(QUANTUM, rounding=ROUND_HALF_UP)


class ValuationStore:
    def __init__(self, store):
        self.store = store
        self.accounting = AccountingStore(store)
        with store.connection() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS cost_valuation_settings(id INTEGER PRIMARY KEY CHECK(id=1),
              payload TEXT NOT NULL,version INTEGER NOT NULL,updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS cost_stock_records(id INTEGER PRIMARY KEY AUTOINCREMENT,
              record_key TEXT NOT NULL UNIQUE,payload TEXT NOT NULL,version INTEGER NOT NULL,
              created_at TEXT NOT NULL,updated_at TEXT NOT NULL,deleted_at TEXT);
            ''')
            db.execute('INSERT OR IGNORE INTO cost_valuation_settings VALUES(1,?,1,?)',
                       (dumps({'start_date': None, 'opening_confirmed': False}), timestamp()))

    def inputs(self):
        with self.store.connection() as db:
            db.execute('BEGIN')
            row = db.execute('SELECT * FROM cost_valuation_settings WHERE id=1').fetchone()
            settings = {**json.loads(row['payload']), 'version': row['version'], 'updated_at': row['updated_at']}
            records = [CostStore.item(r) for r in db.execute('SELECT * FROM cost_stock_records WHERE deleted_at IS NULL ORDER BY id')]
            tickets = CostStore.rows(db, 'cost_tickets')
            accounting = self.accounting.snapshot(db)
            history = [dict(r) for r in db.execute("SELECT id,entity,entity_id,action,created_at FROM cost_history WHERE entity IN ('stock_record','valuation_settings') ORDER BY id DESC LIMIT 30")]
        data = {'version': 1, 'method': 'fifo', 'settings': settings, 'records': records, 'tickets': tickets, 'accounting': accounting}
        digest = hashlib.sha256(dumps(data).encode()).hexdigest()
        return {**data, 'sha256': digest, 'history': history, 'currencies': ['CNY', 'USD', 'EUR', 'HKD'],
                'summary': {'receipts': sum(r['kind'] == 'receipt' for r in records),
                            'opening_records': sum(r['kind'] == 'opening' for r in records),
                            'missing_dates': sum(not r.get('received_date') for r in records),
                            'purchase_tickets_without_receipts': len({t['id'] for t in tickets} - {r.get('ticket_id') for r in records if r['kind'] == 'receipt'})}}

    def save_settings(self, payload):
        start = day(payload.get('start_date'), '核算起始日', True)
        confirmed = payload.get('opening_confirmed', False)
        if not isinstance(confirmed, bool) or (confirmed and not start):
            raise CostError('确认期初库存前须填写核算起始日')
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM cost_valuation_settings WHERE id=1').fetchone()
            if isinstance(payload.get('version'), bool) or not isinstance(payload.get('version'), int) or payload.get('version') != row['version']:
                raise CostError('核算配置已更新，请刷新后重试', 409)
            before = json.loads(row['payload'])
            if confirmed:
                openings = [json.loads(r['payload']) for r in db.execute('SELECT payload FROM cost_stock_records WHERE deleted_at IS NULL')]
                if any(r['kind'] == 'opening' and r.get('received_date') != start for r in openings):
                    raise CostError('期初记录日期须与核算起始日一致')
            after = {'start_date': start, 'opening_confirmed': confirmed}
            db.execute('UPDATE cost_valuation_settings SET payload=?,version=version+1,updated_at=? WHERE id=1', (dumps(after), timestamp()))
            CostStore.audit(db, 'valuation_settings', 1, 'update', before, after)
        return self.inputs()['settings']

    def save_record(self, payload):
        with localcontext() as ctx, self.store.connection() as db:
            ctx.prec = 60
            db.execute('BEGIN IMMEDIATE')
            return self._save_record(db, payload)

    def _save_record(self, db, payload, idempotent=False, source_sha256=None):
        identity = record_id(payload.get('id'), True)
        row = db.execute('SELECT * FROM cost_stock_records WHERE id=?', (identity,)).fetchone() if identity else None
        if identity and not row:
            raise CostError('库存成本记录不存在', 404)
        old = CostStore.item(row) if row else None
        CostStore.check_version(old, payload)
        kind = payload.get('kind')
        if kind not in ('opening', 'receipt'):
            raise CostError('记录类型无效')
        warehouse = text(payload.get('warehouse_code'), '仓库代码', True, 100)
        name = text(payload.get('record_no'), '单号', True)
        received = day(payload.get('received_date'), '入库日期', True)
        ticket_id = record_id(payload.get('ticket_id'), True) if kind == 'receipt' else None
        ticket = None
        if kind == 'receipt':
            ticket_row = db.execute('SELECT * FROM cost_tickets WHERE id=? AND deleted_at IS NULL', (ticket_id,)).fetchone()
            if not ticket_row:
                raise CostError('采购库存单不存在')
            ticket = CostStore.item(ticket_row)
            if payload.get('ticket_version') is not None and payload['ticket_version'] != ticket['version']:
                raise CostError('采购库存单已更新，请刷新后重试', 409)
        raw_lines = payload.get('lines')
        if not isinstance(raw_lines, list) or not 1 <= len(raw_lines) <= 2000:
            raise CostError('须填写 1–2000 条 SKU 明细')
        lines, seen = [], set()
        ticket_lines = {l['warehouse_sku']: l for l in (ticket or {}).get('lines', [])}
        for raw in raw_lines:
            if not isinstance(raw, dict):
                raise CostError('SKU 明细格式无效')
            sku = text(raw.get('warehouse_sku'), '仓库 SKU', True)
            if sku in seen:
                raise CostError(f'SKU 重复：{sku}')
            seen.add(sku)
            quantity = number(raw.get('quantity'), '数量', True)
            if quantity is None or dec(quantity) <= 0:
                raise CostError('库存数量须大于零')
            amount = number(raw.get('amount'), '期初采购金额', True) if kind == 'opening' else None
            if ticket and sku not in ticket_lines:
                raise CostError(f'采购库存单没有 SKU：{sku}')
            lines.append({'warehouse_sku': sku, 'quantity': quantity, 'amount': amount, **({k: number(raw.get(k), '期初费用金额', True) for k in ('head_leg_amount','logistics_amount')} | {k: currency(raw.get(k)) for k in ('head_leg_currency','logistics_currency')} if kind == 'opening' else {})})
        after = {'kind': kind, 'record_no': name, 'warehouse_code': warehouse, 'received_date': received,
                 'ticket_id': ticket_id, 'ticket_version': ticket['version'] if ticket else None,
                 'currency': currency(payload.get('currency'), True) if kind == 'opening' else None,
                 'note': text(payload.get('note'), '备注', maximum=4000), 'lines': lines,
                 'source_sha256': (old or {}).get('source_sha256') or source_sha256}
        key = dumps([kind, warehouse, name])
        existing = db.execute('SELECT * FROM cost_stock_records WHERE record_key=?', (key,)).fetchone()
        if existing and (not identity or existing['id'] != identity):
            existing_payload = json.loads(existing['payload'])
            if idempotent and not existing['deleted_at'] and {k:v for k,v in existing_payload.items() if k!='source_sha256'} == {k:v for k,v in after.items() if k!='source_sha256'}:
                return {**CostStore.item(existing), 'already_imported': True}
            raise CostError('同仓库、同类型的单号已存在', 409)
        if ticket:
            totals = defaultdict(lambda: ZERO)
            for item in db.execute('SELECT id,payload FROM cost_stock_records WHERE deleted_at IS NULL'):
                other = json.loads(item['payload'])
                if item['id'] != identity and other['kind'] == 'receipt' and other['ticket_id'] == ticket_id:
                    for l in other['lines']:
                        totals[l['warehouse_sku']] += dec(l['quantity'])
            for l in lines:
                if totals[l['warehouse_sku']] + dec(l['quantity']) > dec(ticket_lines[l['warehouse_sku']]['quantity']):
                    raise CostError(f"累计入库数量超过采购数量：{l['warehouse_sku']}")
        stamp = timestamp()
        if old:
            db.execute('UPDATE cost_stock_records SET record_key=?,payload=?,version=version+1,updated_at=? WHERE id=?', (key, dumps(after), stamp, identity))
        else:
            identity = db.execute('INSERT INTO cost_stock_records(record_key,payload,version,created_at,updated_at) VALUES(?,?,1,?,?)', (key, dumps(after), stamp, stamp)).lastrowid
        CostStore.audit(db, 'stock_record', identity, 'update' if old else 'create', old, after)
        # Any opening change requires the global completeness assertion to be renewed.
        if kind == 'opening' or (old and old['kind'] == 'opening'):
            self._unconfirm_opening(db)
        return CostStore.item(db.execute('SELECT * FROM cost_stock_records WHERE id=?', (identity,)).fetchone())

    @staticmethod
    def _unconfirm_opening(db):
        row = db.execute('SELECT * FROM cost_valuation_settings WHERE id=1').fetchone()
        data = json.loads(row['payload'])
        if data['opening_confirmed']:
            after = {**data, 'opening_confirmed': False}
            db.execute('UPDATE cost_valuation_settings SET payload=?,version=version+1,updated_at=? WHERE id=1', (dumps(after), timestamp()))
            CostStore.audit(db, 'valuation_settings', 1, 'opening_changed', data, after)

    def remove_record(self, payload):
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            identity = record_id(payload.get('id'))
            row = db.execute('SELECT * FROM cost_stock_records WHERE id=?', (identity,)).fetchone()
            if not row:
                raise CostError('库存成本记录不存在', 404)
            old = CostStore.item(row)
            CostStore.check_version(old, payload)
            db.execute('UPDATE cost_stock_records SET deleted_at=?,version=version+1 WHERE id=?', (timestamp(), identity))
            CostStore.audit(db, 'stock_record', identity, 'delete', old, None)
            if old['kind'] == 'opening':
                self._unconfirm_opening(db)
        return {'id': identity}

    def import_csv(self, content):
        if not isinstance(content, str) or len(content.encode()) > 700_000:
            raise CostError('CSV 超过 700 KB 或格式无效')
        try:
            rows = list(csv.DictReader(io.StringIO(content.lstrip('\ufeff'))))
        except csv.Error:
            raise CostError('CSV 格式无效')
        if not 1 <= len(rows) <= 10000:
            raise CostError('CSV 须包含 1–10000 条明细')
        groups = {}
        with localcontext() as ctx, self.store.connection() as db:
            ctx.prec = 60
            db.execute('BEGIN IMMEDIATE')
            tickets = {r['ticket_no']: r['id'] for r in db.execute('SELECT id,ticket_no FROM cost_tickets WHERE deleted_at IS NULL')}
            for row in rows:
                kind = {'期初': 'opening', '入库': 'receipt'}.get(row.get('类型'))
                if not kind:
                    raise CostError('类型须为“期初”或“入库”')
                key = (kind, row.get('仓库'), row.get('单号'))
                header = {'kind': kind, 'record_no': row.get('单号'), 'warehouse_code': row.get('仓库'), 'received_date': row.get('入库日期'),
                          'currency': row.get('币种') if kind == 'opening' else None, 'ticket_id': tickets.get(row.get('采购单号')) if kind == 'receipt' else None,
                          'note': row.get('备注', ''), 'version': 0}
                if key in groups and any(groups[key].get(k) != v for k, v in header.items()):
                    raise CostError('同单号的日期、币种、采购单或备注不一致')
                group = groups.setdefault(key, {**header, 'lines': []})
                group['lines'].append({'warehouse_sku': row.get('SKU'), 'quantity': row.get('数量'), 'amount': row.get('金额'), 'head_leg_amount':row.get('头程金额'), 'head_leg_currency':row.get('头程币种'), 'logistics_amount':row.get('物流金额'), 'logistics_currency':row.get('物流币种')})
            results = [self._save_record(db, value, True) for value in groups.values()]
        return {'created': sum(not r.get('already_imported') for r in results), 'unchanged': sum(bool(r.get('already_imported')) for r in results)}


def fifo_report(inputs, source, filters=None):
    from costing import report
    return report(inputs, source, filters or {})


def fifo_csv(report):
    stream=io.StringIO();writer=csv.writer(stream)
    writer.writerow(['事件','日期','平台','店铺','订单号','销售SKU','仓库','仓库SKU','仓库数量','状态','缺口数量','完整采购成本','已知采购金额','头程原币','物流原币','报告币成本','异常','批次明细','输入SHA256','数量源SHA256','配置版本'])
    events=report['rows']+[{**r,'date':r['returned_date'],'event':'refund','shortage_quantity':'0','cost_amounts':r['components']['purchase'] if r['complete']['purchase'] and not r['reasons'] else None,'known_amounts':r['components']['purchase'],'landed_reasons':r['reasons'],'landed_status':r['status']} for r in report.get('refunds',[])]
    for r in events:
        cells=[r.get('event','order'),r['date'],r['platform'],r['shop_key'],r['order_no'],r['platform_sku'],r['warehouse_code'],r['warehouse_sku'],r['warehouse_quantity'],r.get('landed_status',r['status']),r['shortage_quantity'],dumps(r['cost_amounts']),dumps(r['known_amounts']),dumps(r['components']['head_leg']),dumps(r['components']['logistics']),dumps(r.get('converted',{})),'；'.join(r['landed_reasons']),dumps(r['allocations']),report['input_sha256'],report.get('source_sha256'),report['settings_version']]
        writer.writerow(["'"+v if isinstance(v,str) and (v.lstrip().startswith(('=','+','@')) or (v.lstrip().startswith('-') and not __import__('re').fullmatch(r'-[0-9]+(?:\.[0-9]+)?',v))) else v for v in cells])
    return ('\ufeff'+stream.getvalue()).encode('utf-8')
