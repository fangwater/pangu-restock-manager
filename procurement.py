"""Purchase batches and configurable freight costs owned by the restock service."""
import hashlib
import json
import sqlite3
import uuid
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, localcontext
from zoneinfo import ZoneInfo

CURRENCIES = ('CNY', 'USD', 'EUR', 'HKD')
DEFAULT_PURCHASE_CURRENCY = 'CNY'


class CostError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def timestamp():
    return datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(timespec='microseconds')


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def text(value, label, required=False, maximum=200):
    if value is None:
        value = ''
    if not isinstance(value, str) or len(value.strip()) > maximum or (required and not value.strip()):
        raise CostError(f'{label}须为{1 if required else 0}–{maximum}字')
    return value.strip()


def number(value, label, nonnegative=False):
    if value is None or value == '':
        return None
    try:
        if isinstance(value, bool) or len(str(value)) > 60:
            raise ValueError()
        result = Decimal(str(value))
        if not result.is_finite() or abs(result) >= Decimal('1e18') or result.as_tuple().exponent < -10:
            raise ValueError()
        if nonnegative and result < 0:
            raise ValueError()
        return format(result, 'f')
    except (InvalidOperation, ValueError):
        raise CostError(f'{label}数值无效')


def currency(value, required=False):
    value = text(value, '币种', required, 8).upper()
    if value == 'RMB':
        value = 'CNY'
    if value and value not in CURRENCIES:
        raise CostError('币种须选择 CNY、USD、EUR 或 HKD')
    return value


def total(values):
    known = [Decimal(str(v)) for v in values if v is not None and v != '']
    with localcontext() as ctx:
        ctx.prec = 60
        return format(sum(known, Decimal(0)), 'f') if known else None


def record_id(value, optional=False):
    if optional and value in (None, ''):
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise CostError('记录编号无效')
    return value


class CostStore:
    def __init__(self, store):
        self.store = store
        with store.connection() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS cost_templates(id INTEGER PRIMARY KEY AUTOINCREMENT,
              name TEXT NOT NULL UNIQUE,payload TEXT NOT NULL,version INTEGER NOT NULL,
              created_at TEXT NOT NULL,updated_at TEXT NOT NULL,deleted_at TEXT);
            CREATE TABLE IF NOT EXISTS cost_tickets(id INTEGER PRIMARY KEY AUTOINCREMENT,
              ticket_no TEXT NOT NULL UNIQUE,payload TEXT NOT NULL,version INTEGER NOT NULL,
              created_at TEXT NOT NULL,updated_at TEXT NOT NULL,deleted_at TEXT);
            CREATE TABLE IF NOT EXISTS cost_history(id INTEGER PRIMARY KEY,entity TEXT NOT NULL,
              entity_id INTEGER NOT NULL,action TEXT NOT NULL,before_json TEXT,after_json TEXT,created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS cost_migrations(source TEXT PRIMARY KEY,sha256 TEXT NOT NULL,
              imported_at TEXT NOT NULL,counts TEXT NOT NULL,source_payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS cost_ticket_imports(id INTEGER PRIMARY KEY AUTOINCREMENT,
              reference TEXT NOT NULL,file_sha256 TEXT NOT NULL,revision_sha256 TEXT NOT NULL,
              result_json TEXT NOT NULL,csv_text TEXT NOT NULL,created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS cost_data_migrations(name TEXT PRIMARY KEY,applied_at TEXT NOT NULL);
            ''')
            db.execute('BEGIN IMMEDIATE')
            if not db.execute("SELECT 1 FROM cost_data_migrations WHERE name='currency-cny-v1'").fetchone():
                for table, entity in [('cost_templates', 'template'), ('cost_tickets', 'ticket')]:
                    for row in list(db.execute(f'SELECT * FROM {table}')):
                        before = json.loads(row['payload'])
                        after = json.loads(row['payload'])
                        if entity == 'template':
                            for category in after.get('categories', []):
                                if str(category.get('currency', '')).strip().upper() == 'RMB':
                                    category['currency'] = 'CNY'
                        else:
                            if str(after.get('purchase_currency', '')).strip().upper() == 'RMB':
                                after['purchase_currency'] = 'CNY'
                            for fee in after.get('costs', []):
                                if str(fee.get('currency', '')).strip().upper() == 'RMB':
                                    fee['currency'] = 'CNY'
                        if before != after:
                            db.execute(f'UPDATE {table} SET payload=?,version=version+1 WHERE id=?', (dumps(after), row['id']))
                            self.audit(db, entity, row['id'], 'normalize_currency', before, after)
                db.execute('INSERT INTO cost_data_migrations VALUES(?,?)', ('currency-cny-v1', timestamp()))
            # Purchase amounts were confirmed as CNY by the business owner.
            # Freight categories and their historical currencies are independent.
            if not db.execute("SELECT 1 FROM cost_data_migrations WHERE name='purchase-cny-confirmed-20261026'").fetchone():
                for row in list(db.execute('SELECT * FROM cost_tickets')):
                    before = json.loads(row['payload'])
                    if not str(before.get('purchase_currency') or '').strip():
                        after = {**before, 'purchase_currency': DEFAULT_PURCHASE_CURRENCY}
                        db.execute('UPDATE cost_tickets SET payload=?,version=version+1 WHERE id=?', (dumps(after), row['id']))
                        self.audit(db, 'ticket', row['id'], 'confirm_purchase_currency', before, after)
                db.execute('INSERT INTO cost_data_migrations VALUES(?,?)', ('purchase-cny-confirmed-20261026', timestamp()))

    @staticmethod
    def item(row):
        return {**json.loads(row['payload']), 'id': row['id'], 'version': row['version'],
                'created_at': row['created_at'], 'updated_at': row['updated_at'], 'deleted_at': row['deleted_at']}

    @staticmethod
    def rows(db, table, deleted=False):
        # Callers pass only these two table names.
        assert table in ('cost_templates', 'cost_tickets')
        return [CostStore.item(r) for r in db.execute(f'SELECT * FROM {table}' + ('' if deleted else ' WHERE deleted_at IS NULL') + ' ORDER BY id')]

    @staticmethod
    def audit(db, entity, identity, action, before, after):
        db.execute('INSERT INTO cost_history(entity,entity_id,action,before_json,after_json,created_at) VALUES(?,?,?,?,?,?)',
                   (entity, identity, action, dumps(before) if before else None, dumps(after) if after else None, timestamp()))

    @staticmethod
    def check_version(old, incoming):
        version = incoming.get('version')
        if isinstance(version, bool) or not isinstance(version, int) or version != (old['version'] if old else 0):
            raise CostError('记录已更新，请刷新后重试', 409)
        if old and old['deleted_at']:
            raise CostError('记录已删除', 409)

    @staticmethod
    def types(template):
        return {str(t['id']): {**t, 'category_id': c['id'], 'category_name': c['name'], 'currency': c['currency']}
                for c in template.get('categories', []) for t in c.get('types', [])}

    @staticmethod
    def decorate(ticket, templates):
        template = templates.get(ticket.get('cost_template_id'))
        fees = {}
        for entry in ticket.get('costs', []):
            if entry.get('amount') is not None:
                code = entry.get('currency') or ''
                fees[code] = total([fees.get(code), entry['amount']])
        lines = ticket.get('lines', [])
        return {**ticket, 'cost_template_name': template['name'] if template else '', 'line_count': len(lines),
                'skus': [l['warehouse_sku'] for l in lines],
                'quantity': total([l.get('quantity') for l in lines]),
                'purchase_amount': total([l.get('amount') for l in lines]),
                'missing_purchase_amounts': sum(l.get('amount') is None for l in lines),
                'fee_totals': fees, 'fee_entries': len(ticket.get('costs', [])),
                'fee_amounts_recorded': sum(c.get('amount') is not None for c in ticket.get('costs', []))}

    def snapshot(self, full=False):
        with self.store.connection() as db:
            db.execute('BEGIN')
            templates = self.rows(db, 'cost_templates')
            tickets = self.rows(db, 'cost_tickets')
            migration = db.execute('SELECT source,sha256,imported_at,counts FROM cost_migrations ORDER BY imported_at DESC LIMIT 1').fetchone()
        by_id = {t['id']: t for t in templates}
        tickets = [self.decorate(t, by_id) for t in tickets]
        lines = [l for t in tickets for l in t.get('lines', [])]
        fees = [c for t in tickets for c in t.get('costs', [])]
        fee_totals = {}
        for c in fees:
            if c.get('amount') is not None:
                code = c.get('currency') or ''
                fee_totals[code] = total([fee_totals.get(code), c['amount']])
        summary = {'tickets': len(tickets), 'sku_lines': len(lines), 'skus': len({l['warehouse_sku'] for l in lines}),
                   'fee_entries': len(fees), 'with_fees': sum(t['fee_entries'] > 0 for t in tickets),
                   'without_fees': sum(t['fee_entries'] == 0 for t in tickets), 'fee_totals': fee_totals}
        purchase = {}
        for t in tickets:
            if t['purchase_amount'] is not None:
                code = t.get('purchase_currency') or ''
                purchase[code] = total([purchase.get(code), t['purchase_amount']])
        summary['purchase_totals'] = purchase
        if not full:
            tickets = [{k: v for k, v in t.items() if k not in ('lines', 'costs')} for t in tickets]
        return {'version': 1, 'owner': 'pangu-restock-manager', 'generated_at': timestamp(), 'summary': summary,
                'currencies': list(CURRENCIES), 'defaults': {'purchase_currency': DEFAULT_PURCHASE_CURRENCY},
                'templates': templates, 'tickets': sorted(tickets, key=lambda t: t['updated_at'], reverse=True),
                'migration': {**dict(migration), 'counts': json.loads(migration['counts'])} if migration else None}

    def detail(self, identity):
        with self.store.connection() as db:
            row = db.execute('SELECT * FROM cost_tickets WHERE id=? AND deleted_at IS NULL', (identity,)).fetchone()
            if not row:
                raise CostError('库存单不存在', 404)
            templates = {t['id']: t for t in self.rows(db, 'cost_templates')}
            history = [dict(r) for r in db.execute('SELECT id,action,created_at FROM cost_history WHERE entity=? AND entity_id=? ORDER BY id DESC LIMIT 20', ('ticket', identity))]
            return {**self.decorate(self.item(row), templates), 'history': history}

    def save_ticket(self, payload):
        with localcontext() as ctx, self.store.connection() as db:
            ctx.prec = 60
            db.execute('BEGIN IMMEDIATE')
            old, after = self.prepare_ticket(db, payload)
            identity = self.write_ticket(db, old, after)
        return self.detail(identity)

    def prepare_ticket(self, db, payload):
        identity = record_id(payload.get('id'), optional=True)
        row = db.execute('SELECT * FROM cost_tickets WHERE id=?', (identity,)).fetchone() if identity else None
        if identity and not row:
            raise CostError('库存单不存在', 404)
        old = self.item(row) if row else None
        self.check_version(old, payload)
        name = text(payload.get('ticket_no'), '库存单号', True)
        template_id = record_id(payload.get('cost_template_id'), optional=True)
        template_row = db.execute('SELECT * FROM cost_templates WHERE id=? AND deleted_at IS NULL', (template_id,)).fetchone() if template_id else None
        if template_id and not template_row:
            raise CostError('成本模板不存在')
        template_id = template_row['id'] if template_row else None
        types = self.types(self.item(template_row)) if template_row else {}
        raw_lines = payload.get('lines')
        if not isinstance(raw_lines, list) or not 1 <= len(raw_lines) <= 2000:
            raise CostError('库存单须包含 1–2000 条 SKU 明细')
        lines, skus = [], set()
        for line in raw_lines:
            if not isinstance(line, dict):
                raise CostError('SKU 明细格式无效')
            sku = text(line.get('warehouse_sku'), '仓库 SKU', True)
            if sku in skus:
                raise CostError(f'SKU 重复：{sku}')
            skus.add(sku)
            quantity = number(line.get('quantity'), '数量', True)
            if quantity is None:
                raise CostError('请填写 SKU 数量')
            price = number(line.get('unit_price'), '采购单价')
            amount = number(line.get('amount'), '采购金额')
            if amount is None and price is not None:
                amount = number(Decimal(quantity) * Decimal(price), '采购金额')
            lines.append({'id': line.get('id'), 'warehouse_sku': sku, 'quantity': quantity,
                          'unit_price': price, 'amount': amount, 'note': text(line.get('note'), 'SKU 备注', maximum=2000)})
        raw_costs = payload.get('costs', [])
        if not isinstance(raw_costs, list) or len(raw_costs) > 2000:
            raise CostError('费用明细格式无效')
        costs, seen = [], set()
        old_costs = {str(c['cost_type_id']): c for c in (old or {}).get('costs', [])}
        for entry in raw_costs:
            if not isinstance(entry, dict):
                raise CostError('费用明细格式无效')
            key = str(entry.get('cost_type_id'))
            if key not in types or key in seen:
                raise CostError('费用类型不存在或重复')
            seen.add(key)
            kind = types[key]
            quantity = number(entry.get('quantity'), '费用数量', True)
            price = number(entry.get('unit_price'), '费用单价')
            amount = number(entry.get('amount'), '费用金额')
            note = text(entry.get('note'), '费用备注', maximum=2000)
            if kind['calculation_mode'] == 'quantity_x_unit':
                if (quantity is None) != (price is None):
                    raise CostError('费用数量和单价须同时填写')
                if quantity is not None:
                    amount = number(Decimal(quantity) * Decimal(price), '费用金额')
            else:
                quantity = price = None
            if quantity is None and price is None and amount is None and not note:
                continue
            previous = old_costs.get(key) if old and old.get('cost_template_id') == template_id else None
            costs.append({'id': entry.get('id'), 'cost_type_id': kind['id'],
                          'category_id': kind['category_id'], 'category_name': kind['category_name'],
                          'cost_type_name': kind['name'], 'calculation_mode': kind['calculation_mode'],
                          'currency': previous['currency'] if previous else kind['currency'],
                          'quantity': quantity, 'unit_price': price, 'amount': amount, 'note': note})
        effective = text(payload.get('effective_date'), '成本生效日期', maximum=10)
        if effective:
            try:
                if date.fromisoformat(effective).isoformat() != effective:
                    raise ValueError()
            except ValueError:
                raise CostError('成本生效日期须为 YYYY-MM-DD')
        after = {'ticket_no': name, 'cost_template_id': template_id,
                 'purchase_currency': currency(payload.get('purchase_currency', DEFAULT_PURCHASE_CURRENCY), True),
                 'effective_date': effective or None, 'note': text(payload.get('note'), '备注', maximum=4000),
                 'source_shop_key': (old or {}).get('source_shop_key', ''), 'lines': lines, 'costs': costs}
        return old, after

    def write_ticket(self, db, old, after, action=None, import_context=None):
        identity = old['id'] if old else None
        name = after['ticket_no']
        stamp = timestamp()
        try:
            if old:
                db.execute('UPDATE cost_tickets SET ticket_no=?,payload=?,version=version+1,updated_at=? WHERE id=?',
                           (name, dumps(after), stamp, identity))
            else:
                identity = db.execute('INSERT INTO cost_tickets(ticket_no,payload,version,created_at,updated_at) VALUES(?,?,1,?,?)',
                                      (name, dumps(after), stamp, stamp)).lastrowid
        except sqlite3.IntegrityError:
            raise CostError('库存单号已存在', 409)
        self.audit(db, 'ticket', identity, action or ('update' if old else 'create'), old, {**after, **({'import': import_context} if import_context else {})})
        return identity

    def save_template(self, payload):
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            identity = record_id(payload.get('id'), optional=True)
            row = db.execute('SELECT * FROM cost_templates WHERE id=?', (identity,)).fetchone() if identity else None
            if identity and not row:
                raise CostError('成本模板不存在', 404)
            old = self.item(row) if row else None
            self.check_version(old, payload)
            name = text(payload.get('name'), '模板名称', True)
            raw_categories = payload.get('categories', [])
            if not isinstance(raw_categories, list) or len(raw_categories) > 100:
                raise CostError('费用分类格式无效')
            categories, category_ids, type_ids, category_names = [], set(), set(), set()
            old_categories = {str(c['id']) for c in (old or {}).get('categories', [])}
            old_types = self.types(old or {})
            for index, category in enumerate(raw_categories):
                if not isinstance(category, dict):
                    raise CostError('费用分类格式无效')
                category_id = category.get('id') or uuid.uuid4().hex
                category_name = text(category.get('name'), '分类名称', True)
                if str(category_id) in category_ids or category_name in category_names:
                    raise CostError('费用分类重复')
                if category.get('id') and str(category_id) not in old_categories:
                    raise CostError('费用分类不属于该模板')
                category_ids.add(str(category_id))
                category_names.add(category_name)
                code = currency(category.get('currency'), True)
                raw_types = category.get('types', [])
                if not isinstance(raw_types, list) or len(raw_types) > 200:
                    raise CostError('费用类型格式无效')
                kinds, names = [], set()
                for pos, kind in enumerate(raw_types):
                    if not isinstance(kind, dict):
                        raise CostError('费用类型格式无效')
                    key = kind.get('id') or uuid.uuid4().hex
                    label = text(kind.get('name'), '费用名称', True)
                    mode = kind.get('calculation_mode')
                    if mode not in ('direct_amount', 'quantity_x_unit'):
                        raise CostError('费用计算方式无效')
                    if str(key) in type_ids or label in names:
                        raise CostError('费用类型重复')
                    if kind.get('id') and (str(key) not in old_types or str(old_types[str(key)]['category_id']) != str(category_id)):
                        raise CostError('费用类型不属于该分类')
                    type_ids.add(str(key))
                    names.add(label)
                    kinds.append({'id': key, 'name': label, 'calculation_mode': mode, 'sort_order': pos})
                categories.append({'id': category_id, 'name': category_name, 'currency': code, 'sort_order': index, 'types': kinds})
            after = {'name': name, 'categories': categories, 'source_shop_key': (old or {}).get('source_shop_key', '')}
            new_types = self.types(after)
            # Historical fee currency and calculation rules cannot be silently reinterpreted.
            used = {str(c['cost_type_id']) for ticket in self.rows(db, 'cost_tickets', deleted=True)
                    if ticket.get('cost_template_id') == identity for c in ticket.get('costs', [])}
            for key in used:
                if key not in new_types:
                    raise CostError('费用类型已有历史记录，不能删除', 409)
                if key in old_types and any(old_types[key][k] != new_types[key][k] for k in ('currency', 'calculation_mode')):
                    raise CostError('费用类型已有历史记录，不能修改币种或计算方式', 409)
            stamp = timestamp()
            try:
                if old:
                    db.execute('UPDATE cost_templates SET name=?,payload=?,version=version+1,updated_at=? WHERE id=?', (name, dumps(after), stamp, identity))
                else:
                    identity = db.execute('INSERT INTO cost_templates(name,payload,version,created_at,updated_at) VALUES(?,?,1,?,?)', (name, dumps(after), stamp, stamp)).lastrowid
            except sqlite3.IntegrityError:
                raise CostError('模板名称已存在', 409)
            self.audit(db, 'template', identity, 'update' if old else 'create', old, after)
            return self.item(db.execute('SELECT * FROM cost_templates WHERE id=?', (identity,)).fetchone())

    def remove(self, entity, payload):
        if entity not in ('ticket', 'template'):
            raise CostError('记录类型无效')
        table = 'cost_tickets' if entity == 'ticket' else 'cost_templates'
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute(f'SELECT * FROM {table} WHERE id=?', (record_id(payload.get('id')),)).fetchone()
            if not row:
                raise CostError('记录不存在', 404)
            old = self.item(row)
            self.check_version(old, payload)
            if entity == 'template' and any(t.get('cost_template_id') == row['id'] for t in self.rows(db, 'cost_tickets', deleted=True)):
                raise CostError('模板已有库存单，不能删除', 409)
            db.execute(f'UPDATE {table} SET deleted_at=?,updated_at=?,version=version+1 WHERE id=?', (timestamp(), timestamp(), row['id']))
            self.audit(db, entity, row['id'], 'delete', old, None)
        return {'removed': True}

    def import_legacy(self, source):
        """One-time, atomic, idempotent transfer; never overwrites edits after handover."""
        raw = dumps(source)
        digest = hashlib.sha256(raw.encode()).hexdigest()
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT * FROM cost_migrations WHERE source=?', ('legacy-inventory',)).fetchone()
            if previous:
                if previous['sha256'] != digest:
                    raise CostError('旧系统数据已移交，禁止覆盖备货系统的后续修改', 409)
                return {**json.loads(previous['counts']), 'sha256': digest, 'already_imported': True}
            if db.execute('SELECT count(*) FROM cost_tickets').fetchone()[0] or db.execute('SELECT count(*) FROM cost_templates').fetchone()[0]:
                raise CostError('成本库非空，不能执行首次移交', 409)
            for key in ('templates', 'categories', 'types', 'tickets', 'lines', 'costs'):
                if not isinstance(source.get(key), list):
                    raise CostError('旧系统导出缺少完整成本表')
                if len({r['id'] for r in source[key]}) != len(source[key]):
                    raise CostError('旧系统导出存在重复记录')
            template_ids = {t['id'] for t in source['templates']}
            categories = {c['id']: c for c in source['categories']}
            kinds = {t['id']: t for t in source['types']}
            ticket_ids = {t['id'] for t in source['tickets']}
            if any(c['template_id'] not in template_ids for c in categories.values()) or any(k['category_id'] not in categories for k in kinds.values()):
                raise CostError('旧系统模板关联不完整')
            if any(r['inventory_ticket_id'] not in ticket_ids for r in [*source['lines'], *source['costs']]):
                raise CostError('旧系统库存单关联不完整')
            for template in source['templates']:
                nested = [{**c, 'currency': currency(c['currency'], True), 'types': [k for k in source['types'] if k['category_id'] == c['id']]}
                          for c in source['categories'] if c['template_id'] == template['id']]
                payload = {**template, 'categories': nested, 'source_shop_key': template.get('shop_key', '')}
                db.execute('INSERT INTO cost_templates(id,name,payload,version,created_at,updated_at) VALUES(?,?,?,1,?,?)',
                           (template['id'], template['name'], dumps(payload), template['created_at'], template['updated_at']))
            for ticket in source['tickets']:
                template_id = ticket.get('cost_template_id')
                if template_id is not None and template_id not in template_ids:
                    raise CostError('旧系统库存单模板不存在')
                lines = [dict(l) for l in source['lines'] if l['inventory_ticket_id'] == ticket['id']]
                if len({l['warehouse_sku'] for l in lines}) != len(lines):
                    raise CostError('旧系统库存单 SKU 重复')
                for line in lines:
                    for field in ('quantity', 'unit_price', 'amount'):
                        line[field] = number(line.get(field), field, field == 'quantity')
                costs = []
                for entry in source['costs']:
                    if entry['inventory_ticket_id'] != ticket['id']:
                        continue
                    kind = kinds.get(entry['cost_type_id'])
                    if not kind or categories[kind['category_id']]['template_id'] != template_id:
                        raise CostError('旧系统费用模板关联无效')
                    category = categories[kind['category_id']]
                    costs.append({**entry, 'category_id': category['id'], 'category_name': category['name'],
                                  'cost_type_name': kind['name'], 'calculation_mode': kind['calculation_mode'], 'currency': currency(category['currency'], True),
                                  **{field: number(entry.get(field), field) for field in ('quantity', 'unit_price', 'amount')}})
                payload = {**ticket, 'source_shop_key': ticket.get('shop_key', ''), 'purchase_currency': DEFAULT_PURCHASE_CURRENCY,
                           'effective_date': None, 'lines': lines, 'costs': costs}
                db.execute('INSERT INTO cost_tickets(id,ticket_no,payload,version,created_at,updated_at) VALUES(?,?,?,1,?,?)',
                           (ticket['id'], ticket['ticket_no'], dumps(payload), ticket['created_at'], ticket['updated_at']))
            counts = {k: len(source[k]) for k in ('templates', 'categories', 'types', 'tickets', 'lines', 'costs')}
            db.execute('INSERT INTO cost_migrations VALUES(?,?,?,?,?)', ('legacy-inventory', digest, timestamp(), dumps(counts), raw))
            self.audit(db, 'migration', 0, 'import', None, {'counts': counts, 'sha256': digest})
        return {**counts, 'sha256': digest, 'already_imported': False}
