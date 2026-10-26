"""FIFO purchase-cost inputs, exact decimal replay, and source traceability."""
import csv
import hashlib
import io
import json
import sqlite3
from collections import defaultdict, deque
from datetime import date, datetime
from decimal import Decimal, localcontext, ROUND_HALF_UP
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
            history = [dict(r) for r in db.execute("SELECT id,entity,entity_id,action,created_at FROM cost_history WHERE entity IN ('stock_record','valuation_settings') ORDER BY id DESC LIMIT 30")]
        data = {'version': 1, 'method': 'fifo', 'settings': settings, 'records': records, 'tickets': tickets}
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

    def _save_record(self, db, payload, idempotent=False):
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
            lines.append({'warehouse_sku': sku, 'quantity': quantity, 'amount': amount})
        after = {'kind': kind, 'record_no': name, 'warehouse_code': warehouse, 'received_date': received,
                 'ticket_id': ticket_id, 'ticket_version': ticket['version'] if ticket else None,
                 'currency': currency(payload.get('currency'), True) if kind == 'opening' else None,
                 'note': text(payload.get('note'), '备注', maximum=4000), 'lines': lines,
                 'source_sha256': (old or {}).get('source_sha256')}
        key = dumps([kind, warehouse, name])
        existing = db.execute('SELECT * FROM cost_stock_records WHERE record_key=?', (key,)).fetchone()
        if existing and (not identity or existing['id'] != identity):
            existing_payload = json.loads(existing['payload'])
            if idempotent and not existing['deleted_at'] and existing_payload == after:
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
                group['lines'].append({'warehouse_sku': row.get('SKU'), 'quantity': row.get('数量'), 'amount': row.get('金额')})
            results = [self._save_record(db, value, True) for value in groups.values()]
        return {'created': sum(not r.get('already_imported') for r in results), 'unchanged': sum(bool(r.get('already_imported')) for r in results)}


def fifo_report(inputs, source, filters=None):
    """Replay the complete shared inventory before applying any display filter."""
    with localcontext() as ctx:
        ctx.prec = 60
        return _fifo_report(inputs, source, filters or {})


def _fifo_report(inputs, source, filters):
    settings = inputs['settings']
    start = settings.get('start_date')
    end = day(filters.get('end') or datetime.now().date().isoformat(), '截止日期')
    display_start = day(filters.get('start') or start or end, '开始日期')
    if display_start > end or (start and (display_start < start or end < start)):
        raise CostError('查询日期须位于核算起始日及其之后，且开始不晚于截止')
    issues = []
    def issue(code, message, record=None):
        issues.append({'code': code, 'message': message, 'record_id': record})
    if not start:
        issue('start_missing', '未设置核算起始日')
    if not settings.get('opening_confirmed'):
        issue('opening_unconfirmed', '期初库存尚未确认完整（无期初库存也须确认）')
    base = {'method': 'fifo', 'range': {'start': display_start, 'end': end, 'replay_start': start},
            'input_sha256': inputs['sha256'], 'settings_version': settings['version'], 'issues': issues,
            'basis': '按订单时间测算，含待发货；同日入库先于订单；未做售后成本回冲；仅采购成本，未含头程和平台费用'}
    if not start or not settings.get('opening_confirmed'):
        return {**base, 'status': 'blocked', 'rows': [], 'remaining': [], 'summary': {}, 'shops': [], 'warehouses': []}
    if not isinstance(source, dict) or source.get('version') != 1 or source.get('timezone') != 'Asia/Shanghai' or source.get('start') != start or source.get('end') != end or not isinstance(source.get('demands'), list):
        raise CostError('订单数量快照的日期、版本或时区无效', 502)
    base.update(source_sha256=source.get('sha256'), source_updated_at=source.get('source_updated_at'))
    if source.get('missing_line_orders'):
        issue('missing_order_lines', f"{source['missing_line_orders']} 个订单缺少完整 SKU 明细")
    if source.get('unknown_order_time_orders'):
        issue('unknown_order_time', f"{source['unknown_order_time_orders']} 个订单缺少业务时间，使用首次采集时间")
    tickets = {t['id']: t for t in inputs['tickets']}
    records = sorted(inputs['records'], key=lambda r: (r.get('received_date') or '9999', r['id']))
    lots, receipt_cumulative = [], defaultdict(lambda: ZERO)
    uncertain = set()
    for r in records:
        d = r.get('received_date')
        problem = None
        if not d:
            problem = '缺少入库日期'
        elif r['kind'] == 'opening' and d != start:
            problem = '期初日期与核算起始日不一致'
        elif r['kind'] == 'receipt' and d < start:
            problem = '起始日前入库须计入期初库存，不得重复入库'
        ticket = tickets.get(r.get('ticket_id')) if r['kind'] == 'receipt' else None
        if r['kind'] == 'receipt' and (not ticket or ticket['version'] != r.get('ticket_version')):
            problem = '采购库存单已删除或版本变化，须重新核对入库记录'
        if problem:
            issue('invalid_stock_record', f"{r['record_no']}：{problem}", r['id'])
            uncertain.update((r['warehouse_code'], l['warehouse_sku']) for l in r['lines'])
            continue
        if d > end:
            continue
        purchase_lines = {l['warehouse_sku']: l for l in (ticket or {}).get('lines', [])}
        for index, line in enumerate(r['lines']):
            q = dec(line['quantity'])
            amount = dec(line['amount']) if line.get('amount') is not None else None
            code = r['currency']
            if ticket:
                p = purchase_lines.get(line['warehouse_sku'])
                code = ticket.get('purchase_currency')
                key = (ticket['id'], line['warehouse_sku'])
                previous = receipt_cumulative[key]
                receipt_cumulative[key] += q
                if not p or dec(p['quantity']) <= 0 or receipt_cumulative[key] > dec(p['quantity']):
                    issue('receipt_quantity', f"{r['record_no']}：SKU {line['warehouse_sku']} 入库数量与采购单不一致", r['id'])
                    uncertain.add((r['warehouse_code'], line['warehouse_sku']))
                    continue
                amount = None if p.get('amount') is None else rounded(dec(p['amount']) * receipt_cumulative[key] / dec(p['quantity'])) - rounded(dec(p['amount']) * previous / dec(p['quantity']))
            if amount is not None and amount < 0:
                amount = None
            if not code or amount is None:
                issue('purchase_cost_missing', f"{r['record_no']}：SKU {line['warehouse_sku']} 缺少采购金额或币种", r['id'])
            lots.append({'record_id': r['id'], 'record_version': r['version'], 'record_no': r['record_no'],
                         'ticket_id': ticket['id'] if ticket else None, 'ticket_version': ticket['version'] if ticket else None,
                         'ticket_no': ticket['ticket_no'] if ticket else None, 'date': d, 'kind': r['kind'],
                         'warehouse_code': r['warehouse_code'], 'warehouse_sku': line['warehouse_sku'],
                         'currency': code, 'quantity': q, 'amount': amount, 'used': ZERO, 'used_amount': ZERO, 'index': index})
    lots.sort(key=lambda l: (l['date'], l['kind'] != 'opening', l['record_id'], l['index']))
    queues = defaultdict(deque)
    position, rows, tainted = 0, [], set()
    demands = source['demands']
    seen = set()
    unknown_warehouse_skus = {d['warehouse_sku'] for d in demands if not d['warehouse_code']}
    for demand in sorted(demands, key=lambda d: (d['occurred_at'], d['order_id'], d['id'])):
        if demand['id'] in seen:
            raise CostError('订单快照包含重复明细，计算已中止', 502)
        seen.add(demand['id'])
        when = day(demand['date'], '订单日期')
        q = number(demand.get('warehouse_quantity'), '仓库需求数量', True)
        if not q or dec(q) <= 0 or not start <= when <= end:
            raise CostError('订单快照包含无效数量或日期', 502)
        q = dec(q)
        while position < len(lots) and lots[position]['date'] <= when:
            l = lots[position]
            queues[(l['warehouse_code'], l['warehouse_sku'])].append(l)
            position += 1
        key = (demand['warehouse_code'], demand['warehouse_sku'])
        reasons, allocations, known = [], [], defaultdict(lambda: ZERO)
        if not all(key):
            reasons.append('缺少仓库或仓库 SKU')
        if key[0] and key[1] in unknown_warehouse_skus:
            reasons.append('该 SKU 存在仓库不明的订单需求')
        if key in uncertain:
            reasons.append('该仓库 SKU 存在未核对的入库记录')
        if key in tainted:
            reasons.append('此前订单库存不足，后续批次归属待核对')
        remaining = q
        queue = queues[key]
        while remaining > 0 and queue:
            lot = queue[0]
            take = min(remaining, lot['quantity'] - lot['used'])
            lot['used'] += take
            amount = None
            if lot['amount'] is not None and lot['currency']:
                cumulative = rounded(lot['amount'] * lot['used'] / lot['quantity'])
                amount = cumulative - lot['used_amount']
                lot['used_amount'] = cumulative
                known[lot['currency']] += amount
            else:
                reasons.append('批次采购成本缺失')
            allocations.append({k: lot[k] for k in ('record_id', 'record_version', 'record_no', 'ticket_id', 'ticket_version', 'ticket_no', 'date', 'currency')} | {'quantity': fmt(take), 'amount': fmt(amount) if amount is not None else None})
            remaining -= take
            if lot['used'] == lot['quantity']:
                queue.popleft()
        if remaining > 0:
            reasons.append('入库库存不足')
            tainted.add(key)
        if source.get('missing_line_orders') or source.get('unknown_order_time_orders'):
            reasons.append('订单源数据不完整，批次归属待核对')
        reasons = list(dict.fromkeys(reasons))
        rows.append({**demand, 'status': 'review' if reasons else 'complete', 'reasons': reasons,
                     'shortage_quantity': fmt(remaining), 'known_amounts': {c: fmt(a) for c, a in sorted(known.items())},
                     'cost_amounts': None if reasons else {c: fmt(a) for c, a in sorted(known.items())}, 'allocations': allocations})
    def matches(r):
        return (not filters.get('shop') or r.get('shop_key') == filters['shop']) and (not filters.get('warehouse') or r['warehouse_code'] == filters['warehouse']) and (not filters.get('q') or filters['q'].lower() in ' '.join(str(r.get(k) or '') for k in ('warehouse_sku', 'platform_sku', 'order_no', 'record_no')).lower())
    selected = [r for r in rows if display_start <= r['date'] <= end and matches(r) and (not filters.get('status') or r['status'] == filters['status'])]
    totals = defaultdict(lambda: ZERO)
    for row in selected:
        for code, amount in (row['cost_amounts'] or {}).items():
            totals[code] += dec(amount)
    remaining_lots = [{k: l[k] for k in ('record_id', 'record_no', 'date', 'warehouse_code', 'warehouse_sku', 'currency')}
                      | {'quantity': fmt(l['quantity'] - l['used']), 'amount': fmt(l['amount'] - l['used_amount']) if l['amount'] is not None else None}
                      for l in lots if l['quantity'] > l['used']]
    return {**base, 'status': 'review' if issues or any(r['status'] != 'complete' for r in rows) else 'complete', 'rows': selected,
            'remaining': remaining_lots, 'shops': sorted({r['shop_key'] for r in rows}),
            'warehouses': sorted({r['warehouse_code'] for r in rows} | {l['warehouse_code'] for l in lots}),
            'summary': {'replayed_lines': len(rows), 'selected_lines': len(selected),
                        'complete_lines': sum(r['status'] == 'complete' for r in selected),
                        'review_lines': sum(r['status'] != 'complete' for r in selected),
                        'complete_cost_totals': {c: fmt(a) for c, a in sorted(totals.items())}}}


def fifo_csv(report):
    stream = io.StringIO()
    writer = csv.writer(stream)
    writer.writerow(['订单日期','平台','店铺','订单号','销售SKU','仓库','仓库SKU','需求数量','状态','缺口数量','完整采购成本','已知部分金额','异常','批次明细','输入SHA256','订单SHA256','配置版本'])
    for r in report['rows']:
        cells = [r['date'], r['platform'], r['shop_key'], r['order_no'], r['platform_sku'], r['warehouse_code'], r['warehouse_sku'], r['warehouse_quantity'],
                 r['status'], r['shortage_quantity'], dumps(r['cost_amounts']), dumps(r['known_amounts']), '；'.join(r['reasons']), dumps(r['allocations']), report['input_sha256'], report.get('source_sha256'), report['settings_version']]
        writer.writerow(["'" + c if isinstance(c, str) and c.lstrip().startswith(('=', '+', '-', '@')) else c for c in cells])
    return ('\ufeff' + stream.getvalue()).encode('utf-8')
