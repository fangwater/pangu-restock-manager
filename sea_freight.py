"""Excel-owned ocean freight bills and versioned original evidence."""
import base64
import copy
import hashlib
import io
import json
import posixpath
import re
from collections import Counter
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile

from excel_export import Formula, workbook
from procurement import CostError, dumps, text, timestamp

MAX_FILE = 6 * 1024 * 1024
NS = {'m': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
REL = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
FIELDS = list(zip(
    ('sequence departure_date arrival_date forwarder bill_no destination converted_usd sea_usd tax_usd delivery_usd ams_usd other_usd usd_total trucking_cny port_cny customs_cny other_cny cny_total converted_cny vessel voyage order_no container_no container_type origin packages gross_weight volume goods_value payment_rule due_date payment_status amount_basis source_reference notes').split(),
    ('序号|开航日期|到港日期|货代|提单号|目的港|折合美元合计|海运费(USD)|税金(USD)|派送/清关(USD)|舱单/AMS(USD)|其他项(USD)|美元合计(USD)|拖车费(RMB)|港杂费(RMB)|报关费(RMB)|其他项(RMB)|人民币合计(RMB)|折合人民币合计|船名|航次|托单号/订单号|柜号|箱型|起运港|件数|毛重(KG)|体积(CBM)|货值(USD)|付款规则|付款到期日|付款状态|金额口径|来源文件|备注').split('|')))
FEES = [('sea_usd', '海运费', 'USD'), ('tax_usd', '税金', 'USD'), ('delivery_usd', '派送/清关', 'USD'), ('ams_usd', '舱单/AMS', 'USD'), ('other_usd', '其他项', 'USD'), ('trucking_cny', '拖车费', 'CNY'), ('port_cny', '港杂费', 'CNY'), ('customs_cny', '报关费', 'CNY'), ('other_cny', '其他项', 'CNY')]
NUMBER_FIELDS = {k for k, _, _ in FEES} | {'usd_total', 'cny_total', 'packages', 'gross_weight', 'volume', 'goods_value'}
DATE_FIELDS = {'departure_date', 'arrival_date', 'due_date'}


def decimal_text(value):
    if value in (None, ''):
        return None
    try:
        number = Decimal(str(value).replace(',', '').strip())
        if not number.is_finite() or abs(number) > Decimal('1e15'):
            raise ValueError()
        # XLSX numeric storage can contain binary floating-point tails.
        cents = number.quantize(Decimal('.01'))
        if abs(number - cents) < Decimal('0.00000001'):
            number = cents
        if number.as_tuple().exponent < -10:
            raise ValueError()
        return format(number, 'f')
    except (InvalidOperation, ValueError):
        raise CostError('金额或数量无效，最多保留十位小数')


def excel_date(value, epoch1904=False):
    if value in (None, ''):
        return None
    try:
        if re.fullmatch(r'\d+(?:\.0+)?', str(value)):
            n = int(Decimal(str(value)))
            result = date(1904, 1, 1) + timedelta(days=n) if epoch1904 else date(1899, 12, 30) + timedelta(days=n + (0 < n < 60))
        else:
            result = date.fromisoformat(str(value).strip()[:10])
        if not 1900 <= result.year <= 2200:
            raise ValueError()
        return result.isoformat()
    except (ValueError, OverflowError):
        raise CostError('日期无效，须为 Excel 日期或 YYYY-MM-DD')


def payment_state(value):
    value = str(value or '').replace('✅', '').strip()
    return {'已付': 'paid', '已付清': 'paid', '未付': 'unpaid', '部分已付': 'partial'}.get(value, 'unconfirmed')


def bill_checks(data):
    checks = [currency + ' 账单合计未提供' for currency, key in [('USD', 'usd_total'), ('CNY', 'cny_total')] if data[key] is None]
    if data.get('warehouse_date') and data.get('warehouse_date_basis') != '实际':
        checks.append('入仓日期未确认为实际日期')
    if '一口价' in data['amount_basis']:
        checks.append('人民币一口价；辅助拆分未作为额外费用累计')
    if payment_state(data['payment_status']) == 'unconfirmed':
        checks.append('付款状态待确认，不作为已确认欠款')
    if data['notes']:
        checks.append('原表含备注，详见账单明细')
    return checks


def read_xlsx(blob, with_metadata=False):
    if not isinstance(blob, bytes) or not 0 < len(blob) <= MAX_FILE:
        raise CostError('Excel 文件须为 1 字节至 6 MiB')
    try:
        with ZipFile(io.BytesIO(blob)) as archive:
            files = archive.infolist()
            if len(files) > 2000 or sum(f.file_size for f in files) > 64 * 1024 * 1024:
                raise CostError('Excel 解压后超过 64 MiB 或文件条目过多')
            if len({f.filename for f in files}) != len(files):
                raise CostError('Excel 包含重复文件条目')

            def xml(path):
                raw = archive.read(path)
                if b'<!DOCTYPE' in raw or b'<!ENTITY' in raw:
                    raise CostError('Excel XML 包含不支持的声明')
                return ET.fromstring(raw)

            strings = [''.join(n.itertext()) for n in xml('xl/sharedStrings.xml').findall('m:si', NS)] if 'xl/sharedStrings.xml' in archive.namelist() else []
            book = xml('xl/workbook.xml')
            properties = book.find('m:workbookPr', NS)
            epoch1904 = properties is not None and properties.get('date1904') in ('1', 'true')
            relations = {r.get('Id'): r for r in xml('xl/_rels/workbook.xml.rels')}
            sheets = {}
            for sheet in book.findall('m:sheets/m:sheet', NS):
                relation = relations[sheet.get('{' + REL + '}id')]
                if relation.get('TargetMode') == 'External':
                    raise CostError('不支持外部工作表')
                target = relation.get('Target', '')
                path = posixpath.normpath(target.lstrip('/') if target.startswith('/') else 'xl/' + target)
                if not path.startswith('xl/worksheets/'):
                    raise CostError('工作表路径无效')
                rows = {}
                for row in xml(path).findall('m:sheetData/m:row', NS):
                    index = int(row.get('r'))
                    if index > 10000:
                        raise CostError('工作表行号超过 10000')
                    cells = {}
                    for cell in row.findall('m:c', NS):
                        ref = cell.get('r', '')
                        col = re.sub(r'\d', '', ref)
                        raw = cell.find('m:v', NS)
                        value = raw.text if raw is not None else None
                        if cell.get('t') == 's':
                            value = strings[int(value)]
                        elif cell.get('t') == 'inlineStr':
                            value = ''.join(cell.find('m:is', NS).itertext())
                        if cell.get('t') == 'e':
                            value = {'error': value}
                        cells[col] = {'value': value, 'formula': cell.find('m:f', NS) is not None}
                    rows[index] = cells
                sheets[sheet.get('name')] = rows
            metadata = {}
            if with_metadata and 'docProps/custom.xml' in archive.namelist():
                parts = sorted((p.get('name'), ''.join(p.itertext())) for p in xml('docProps/custom.xml') if (p.get('name') or '').startswith('PanguSeaSnapshot.'))
                if parts:
                    try:
                        metadata = json.loads(''.join(value for _, value in parts))
                        if not isinstance(metadata, dict) or not isinstance(metadata.get('bills'), dict):
                            raise ValueError()
                    except (ValueError, TypeError):
                        raise CostError('Excel 导出版本信息损坏，请重新导出')
            return (sheets, epoch1904, metadata) if with_metadata else (sheets, epoch1904)
    except CostError:
        raise
    except (BadZipFile, KeyError, ValueError, TypeError, IndexError, ET.ParseError, RuntimeError):
        raise CostError('Excel 文件损坏或不是受支持的 XLSX 工作簿')


def header(rows, required):
    for index, cells in list(rows.items())[:20]:
        fields = {str(c['value']).strip(): col for col, c in cells.items() if c['value'] is not None and not isinstance(c['value'], dict)}
        if required <= fields.keys():
            return index, fields
    return None, {}


def parse_workbook(blob, include_issues=False):
    sheets, epoch, metadata = read_xlsx(blob, with_metadata=True)
    bills, warnings, keys, issues = [], [], set(), []
    plan = {}
    rate = None
    for name, rows in sheets.items():
        issue_start, issue_fields = header(rows, {'异常项目', '提单号/单号', '货代', '说明与建议'})
        if issue_start:
            for index, cells in rows.items():
                if index <= issue_start:
                    continue
                values = {label: cells.get(col, {}).get('value') for label, col in issue_fields.items()}
                if values.get('异常项目'):
                    issues.append({'sheet': name, 'row': index, 'identifier': str(values.get('提单号/单号') or ''), 'forwarder': str(values.get('货代') or ''), 'title': str(values['异常项目']), 'amount': decimal_text(values.get('金额')), 'currency': str(values.get('币种') or ''), 'notes': str(values.get('说明与建议') or '')})
        for index, cells in rows.items():
            for col, cell in cells.items():
                if name == '汇总总览' and cell['value'] in ('美元折人民币汇率（可修改）', '参考汇率 USD/CNY'):
                    next_col = chr(ord(col) + 1) if len(col) == 1 else ''
                    candidate = cells.get(next_col, {}).get('value')
                    if candidate not in (None, ''):
                        rate = decimal_text(candidate)
        start, fields = header(rows, {'托单号/提单号', '开航日期', '入仓日期'})
        if start:
            for index, cells in rows.items():
                if index <= start:
                    continue
                identifier = cells.get(fields['托单号/提单号'], {}).get('value')
                if identifier and identifier != '托单号/提单号':
                    plan[str(identifier).strip()] = {label: cells.get(fields.get(label), {}).get('value') for label in ('入仓日期', '付款到期日', '待付人民币', '待付美元', '待付折RMB')}

    for name, rows in sheets.items():
        start, fields = header(rows, {'货代', '提单号', '开航日期', '美元合计(USD)', '人民币合计(RMB)', '海运费(USD)'})
        if start is None:
            continue
        missing = [label for _, label in FIELDS if label not in fields]
        if missing:
            raise CostError(name + ' 缺少列：' + '、'.join(missing))
        for index, cells in rows.items():
            if index <= start:
                continue
            def get(label):
                cell = cells.get(fields.get(label), {})
                value = cell.get('value')
                if isinstance(value, dict):
                    raise CostError(f'{name} 第 {index} 行 {label} 为 Excel 错误值')
                if cell.get('formula') and value is None:
                    raise CostError(f'{name} 第 {index} 行 {label} 公式未保存计算值，请在 Excel 中重算并保存')
                return value

            if not any(get(label) for label in ('货代', '提单号', '托单号/订单号')):
                continue
            try:
                data = {}
                for key, label in FIELDS:
                    if key in ('sequence', 'converted_usd', 'converted_cny'):
                        continue
                    value = get(label)
                    data[key] = decimal_text(value) if key in NUMBER_FIELDS else excel_date(value, epoch) if key in DATE_FIELDS else str(value or '').strip()
                if not data['forwarder'] or not (data['order_no'] or data['bill_no']):
                    raise CostError('货代及托单号/提单号必填')
                if not data['departure_date']:
                    raise CostError('开航日期必填')
                identity = data['forwarder'] + '\0' + (data['order_no'] or data['bill_no'])
                if identity in keys:
                    raise CostError('同一货代及单号在文件内重复')
                keys.add(identity)
                for currency, total_key in [('USD', 'usd_total'), ('CNY', 'cny_total')]:
                    parts = [Decimal(data[k]) for k, _, code in FEES if code == currency and data[k] is not None]
                    if data[total_key] is not None and sum(parts, Decimal(0)) != Decimal(data[total_key]):
                        raise CostError(currency + ' 分项金额与账单合计不一致')
                p = plan.get(data['order_no']) or plan.get(data['bill_no']) or {}
                snapshot = metadata.get('bills', {}).get(identity, {})
                if not isinstance(snapshot, dict):
                    raise CostError('Excel 导出版本信息损坏，请重新导出')
                warehouse = get('入仓日期') if '入仓日期' in fields else p.get('入仓日期')
                warehouse_date = excel_date(warehouse, epoch)
                basis = snapshot.get('warehouse_date_basis', '未确认') if snapshot.get('warehouse_date') == warehouse_date else '未确认'
                data.update(warehouse_date=warehouse_date, warehouse_date_basis=basis,
                            _warehouse_basis_supplied=bool(snapshot))
                if data['warehouse_date_basis'] not in ('实际', '估算', '未确认'):
                    raise CostError('入仓日期口径须为实际、估算或未确认')
                data.update(source_sheet=name, source_row=index, source_rate=rate,
                            mode=name.rsplit('-', 1)[0] if '-' in name else name, country=name.rsplit('-', 1)[-1] if '-' in name else '',
                            raw_cells=cells,
                            expected_version=str(get('版本') or '').strip() if '版本' in fields else str(snapshot.get('version') or ''))
                if '参考汇率 USD/CNY' in fields:
                    data['source_rate'] = decimal_text(get('参考汇率 USD/CNY'))
                elif rate is None and snapshot.get('source_rate') is not None:
                    data['source_rate'] = decimal_text(snapshot['source_rate'])
                if data['source_rate'] is not None and Decimal(data['source_rate']) <= 0:
                    raise CostError('参考汇率须大于零')
                data['checks'] = bill_checks(data)
                bills.append(data)
            except CostError as error:
                raise CostError(f'{name} 第 {index} 行：{error}')
    if not bills or len(bills) > 2000:
        raise CostError('账单须为 1 至 2000 票；仅导入海运明细表，不导入汇总或报价表')
    if rate is not None and Decimal(rate) <= 0:
        raise CostError('参考汇率须大于零')
    bad_fx = 0
    if rate:
        for p in plan.values():
            if p.get('待付折RMB') is not None and p.get('待付人民币') is not None and p.get('待付美元') is not None:
                expected = Decimal(decimal_text(p['待付人民币'])) + Decimal(decimal_text(p['待付美元'])) * Decimal(rate)
                if abs(Decimal(str(p['待付折RMB'])) - expected) > Decimal('.005'):
                    bad_fx += 1
    if bad_fx:
        warnings.append(f'付款计划 {bad_fx} 行折算额与参考汇率不一致，ERP 使用原币合计并重新计算参考折算额')
    if '传美一口价补全' in sheets:
        warnings.append('一口价补全页含估算拆分，仅保留为原始附件，不叠加到海运账单')
    result = (bills, warnings, rate)
    return (*result, issues) if include_issues else result


def decode_file(payload):
    value = payload.get('xlsx_base64')
    if not isinstance(value, str) or len(value) > (MAX_FILE * 4 // 3 + 8):
        raise CostError('Excel 文件超过 6 MiB 或内容缺失')
    try:
        blob = base64.b64decode(value, validate=True)
    except ValueError:
        raise CostError('Excel 上传编码无效')
    filename = text(payload.get('filename'), '文件名', True, 200)
    if not filename.lower().endswith('.xlsx'):
        raise CostError('文件须为 .xlsx 格式')
    reference = text(payload.get('reference') or filename, '来源凭证', True, 1000)
    return blob, filename, reference


class SeaFreightStore:
    def __init__(self, store):
        self.store = store
        with store.connection() as db:
            db.executescript('''
              CREATE TABLE IF NOT EXISTS sea_files(id INTEGER PRIMARY KEY AUTOINCREMENT,sha256 TEXT NOT NULL UNIQUE,filename TEXT NOT NULL,reference TEXT NOT NULL,content BLOB NOT NULL,manifest TEXT NOT NULL,created_at TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS sea_bills(id INTEGER PRIMARY KEY AUTOINCREMENT,business_key TEXT NOT NULL UNIQUE,payload TEXT NOT NULL,version INTEGER NOT NULL,source_file_id INTEGER NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS sea_history(id INTEGER PRIMARY KEY AUTOINCREMENT,bill_id INTEGER NOT NULL,action TEXT NOT NULL,before_json TEXT,after_json TEXT NOT NULL,file_id INTEGER,reference TEXT NOT NULL,created_at TEXT NOT NULL);
              CREATE INDEX IF NOT EXISTS sea_history_bill_idx ON sea_history(bill_id,id);
            ''')

    @staticmethod
    def unpack(row):
        result = {**json.loads(row['payload']), **{key: row[key] for key in ('id', 'version', 'source_file_id', 'created_at', 'updated_at')}}
        result['checks'] = bill_checks(result)
        return result

    def rows(self, db):
        return [self.unpack(row) for row in db.execute('SELECT * FROM sea_bills ORDER BY id')]

    def plan(self, db, blob, filename, reference):
        sha = hashlib.sha256(blob).hexdigest()
        previous = db.execute('SELECT id,manifest FROM sea_files WHERE sha256=?', (sha,)).fetchone()
        if previous:
            manifest = json.loads(previous['manifest'])
            return {'filename': filename, 'sha256': sha, 'reference': reference, 'duplicate_file_id': previous['id'], 'rows': [], 'counts': {'new': 0, 'updated': 0, 'unchanged': manifest['bills']}, 'warnings': ['该文件已导入，本次不覆盖后续修改'], 'bills': manifest['bills']}
        parsed, warnings, rate, issues = parse_workbook(blob, include_issues=True)
        current = self.rows(db)
        plan, counts, matched_ids = [], Counter(), set()
        for incoming in parsed:
            data = copy.deepcopy(incoming)
            candidates = [old for old in current if old['forwarder'] == data['forwarder'] and ((data['order_no'] and old['order_no'] == data['order_no']) or (data['bill_no'] and old['bill_no'] == data['bill_no']))]
            if len(candidates) > 1:
                raise CostError((data['order_no'] or data['bill_no']) + ' 匹配到多条历史账单，请核对单号')
            old = candidates[0] if candidates else None
            if old and old['id'] in matched_ids:
                raise CostError('文件内多条账单指向同一历史记录')
            if old:
                matched_ids.add(old['id'])
            expected = data.pop('expected_version')
            if expected not in (None, '') and (not expected.isdigit() or int(expected) != (old['version'] if old else 0)):
                raise CostError('账单版本已变化，请重新导出后修改', 409)
            basis_supplied = data.pop('_warehouse_basis_supplied', False)
            if old and not basis_supplied and data['warehouse_date'] == old.get('warehouse_date'):
                data['warehouse_date_basis'] = old.get('warehouse_date_basis', '未确认')
            # Preserve historic annotations without exposing or extending them.
            for key in ('purchase_numbers', 'purchase_ticket_ids', 'link_note'):
                if old and key in old:
                    data[key] = old[key]
            def comparable(value):
                return {k: v for k, v in value.items() if k not in ('id', 'version', 'created_at', 'updated_at', 'source_file_id', 'source_sheet', 'source_row', 'raw_cells', 'checks')}
            action = 'new' if not old else 'unchanged' if comparable(old) == comparable(data) else 'updated'
            counts[action] += 1
            changed = [key for key in data if comparable(data).get(key) != comparable(old or {}).get(key) and key in comparable(data)]
            plan.append({'id': old['id'] if old else None, 'version': old['version'] if old else 0, 'action': action, 'before': old, 'after': data, 'changed_fields': changed})
        return {'filename': filename, 'sha256': sha, 'reference': reference, 'rows': plan, 'counts': {k: counts[k] for k in ('new', 'updated', 'unchanged')}, 'warnings': warnings, 'bills': len(parsed), 'reference_rate': rate, 'issues': issues}

    @staticmethod
    def revision(plan):
        return hashlib.sha256(dumps(plan).encode()).hexdigest()

    def preview(self, payload):
        blob, filename, reference = decode_file(payload)
        with self.store.connection() as db:
            db.execute('BEGIN')
            plan = self.plan(db, blob, filename, reference)
        return {**plan, 'revision': self.revision(plan)}

    def import_file(self, payload):
        blob, filename, reference = decode_file(payload)
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            plan = self.plan(db, blob, filename, reference)
            if payload.get('revision') != self.revision(plan):
                raise CostError('预览依据已变化，请重新预览', 409)
            if plan.get('duplicate_file_id'):
                return {'file_id': plan['duplicate_file_id'], **plan['counts'], 'duplicate': True}
            moment = timestamp()
            manifest = {k: plan.get(k) for k in ('bills', 'counts', 'warnings', 'reference_rate', 'issues')}
            file_id = db.execute('INSERT INTO sea_files(sha256,filename,reference,content,manifest,created_at) VALUES(?,?,?,?,?,?)', (plan['sha256'], filename, reference, blob, dumps(manifest), moment)).lastrowid
            for row in plan['rows']:
                if row['action'] == 'unchanged':
                    continue
                data = row['after']
                if row['action'] == 'new':
                    key = data['forwarder'] + '\0' + (data['order_no'] or data['bill_no'])
                    identity = db.execute('INSERT INTO sea_bills(business_key,payload,version,source_file_id,created_at,updated_at) VALUES(?,?,1,?,?,?)', (key, dumps(data), file_id, moment, moment)).lastrowid
                else:
                    identity = row['id']
                    db.execute('UPDATE sea_bills SET payload=?,version=version+1,source_file_id=?,updated_at=? WHERE id=?', (dumps(data), file_id, moment, identity))
                db.execute('INSERT INTO sea_history(bill_id,action,before_json,after_json,file_id,reference,created_at) VALUES(?,?,?,?,?,?,?)', (identity, row['action'], dumps(row['before']) if row['before'] else None, dumps(data), file_id, reference, moment))
        return {'file_id': file_id, **plan['counts'], 'duplicate': False}

    def list(self, query=None, *, internal=False):
        query = query or {}
        with self.store.connection() as db:
            db.execute('BEGIN')
            all_rows = self.rows(db)
            if not internal:
                all_rows = [self.public_bill(r) for r in all_rows]
            files = [dict(r) for r in db.execute('SELECT id,sha256,filename,reference,manifest,created_at FROM sea_files ORDER BY id DESC LIMIT 30')]
            issue_files = [dict(r) for r in db.execute('SELECT id,manifest FROM sea_files ORDER BY id DESC')]
        rows = all_rows
        for key in ('forwarder', 'mode', 'country'):
            if query.get(key):
                rows = [r for r in rows if r[key] == query[key]]
        for key, operator in [('start', lambda a, b: a >= b), ('end', lambda a, b: a <= b)]:
            if query.get(key):
                selected = excel_date(query[key])
                rows = [r for r in rows if r['departure_date'] and operator(r['departure_date'], selected)]
        if query.get('payment'):
            if query['payment'] not in ('paid', 'unpaid', 'partial', 'unconfirmed'):
                raise CostError('付款状态筛选无效')
            rows = [r for r in rows if payment_state(r['payment_status']) == query['payment']]
        if query.get('q'):
            q = str(query['q']).casefold()
            rows = [r for r in rows if q in ' '.join(str(r.get(k) or '') for k in ('bill_no', 'order_no', 'container_no', 'destination', 'notes', 'source_reference')).casefold()]
        rows.sort(key=lambda r: (r['departure_date'] or '', r['id']), reverse=True)
        summary = {'bills': len(rows), 'paid': sum(payment_state(r['payment_status']) == 'paid' for r in rows), 'unconfirmed': sum(payment_state(r['payment_status']) == 'unconfirmed' for r in rows)}
        for code, key in [('USD', 'usd_total'), ('CNY', 'cny_total')]:
            summary[code] = format(sum((Decimal(r[key]) for r in rows if r[key] is not None), Decimal(0)), 'f')
            summary[code + '_missing'] = sum(r[key] is None for r in rows)
        options = {key: sorted({r[key] for r in all_rows if r[key]}) for key in ('forwarder', 'mode', 'country')}
        unique_issues = {}
        for f in issue_files:
            for issue in json.loads(f['manifest']).get('issues') or []:
                key = dumps({k: issue[k] for k in ('identifier', 'forwarder', 'title', 'amount', 'currency', 'notes')})
                unique_issues.setdefault(key, {**issue, 'file_id': f['id']})
        return {'rows': rows, 'summary': summary, 'options': options, 'files': [{**f, 'manifest': json.loads(f['manifest'])} for f in files], 'issues': list(unique_issues.values())}

    def detail(self, identity):
        with self.store.connection() as db:
            row = db.execute('SELECT * FROM sea_bills WHERE id=?', (identity,)).fetchone()
            if not row:
                raise CostError('账单不存在', 404)
            db.execute('BEGIN')
            result = self.public_bill(self.unpack(row))
            history = [dict(r) for r in db.execute('SELECT id,action,reference,file_id,created_at FROM sea_history WHERE bill_id=? ORDER BY id DESC LIMIT 50', (identity,))]
        return {**result, 'history': history, 'fees': [{'key': k, 'name': name, 'currency': code, 'amount': result[k]} for k, name, code in FEES]}

    @staticmethod
    def public_bill(row):
        hidden = {'purchase_numbers', 'purchase_ticket_ids', 'link_note', 'warehouse_date_basis'}
        return {key: value for key, value in row.items() if key not in hidden}

    def update(self, identity, payload):
        if set(payload) - {'version', 'reference', 'payment_status', 'warehouse_date'}:
            raise CostError('仅支持更新原表付款状态与入仓日期；其余字段通过 Excel 导入')
        if any(key in payload for key in NUMBER_FIELDS):
            raise CostError('金额与数量须通过 Excel 导入更新')
        reference = text(payload.get('reference'), '来源凭证', True, 1000)
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            raw = db.execute('SELECT * FROM sea_bills WHERE id=?', (identity,)).fetchone()
            if not raw:
                raise CostError('账单不存在', 404)
            old = self.unpack(raw)
            if payload.get('version') != old['version']:
                raise CostError('账单已更新，请重新读取', 409)
            data = json.loads(raw['payload'])
            status = text(payload.get('payment_status', data['payment_status']), '付款状态', True, 100)
            if status not in ('已付', '未付', '部分已付', '待确认', '待确认（账单无付款记录）'):
                raise CostError('付款状态无效')
            data['payment_status'] = status
            data['warehouse_date'] = excel_date(payload.get('warehouse_date', data['warehouse_date']))
            if data['warehouse_date'] != old.get('warehouse_date'):
                data['warehouse_date_basis'] = '未确认'
            db.execute('UPDATE sea_bills SET payload=?,version=version+1,updated_at=? WHERE id=?', (dumps(data), timestamp(), identity))
            db.execute('INSERT INTO sea_history(bill_id,action,before_json,after_json,reference,created_at) VALUES(?,?,?,?,?,?)', (identity, 'review', dumps(old), dumps(data), reference, timestamp()))
        return self.detail(identity)

    def original(self, identity):
        with self.store.connection() as db:
            row = db.execute('SELECT content,filename FROM sea_files WHERE id=?', (identity,)).fetchone()
        if not row:
            raise CostError('原始文件不存在', 404)
        return row['content']

    def export(self, query=None):
        data = self.list(query, internal=True)
        groups = {name: [] for name in ('整柜-美国', '整柜-加拿大', '散货-美国', '散货-加拿大')}
        for row in data['rows']:
            groups.setdefault(row['source_sheet'], []).append(row)
        rate_values = {r['source_rate'] for r in data['rows'] if r.get('source_rate')}
        rate = next(iter(rate_values)) if len(rate_values) == 1 else None
        sheets = [('汇总总览', [['项目', '金额'], ['参考汇率 USD/CNY', Decimal(rate) if rate else None], ['账单票数', len(data['rows'])], ['人民币合计', Decimal(data['summary']['CNY'])], ['美元合计', Decimal(data['summary']['USD'])]])]
        payment_rows = [['货代', '托单号/提单号', '开航日期', '入仓日期', '付款到期日', '付款状态', '账单人民币', '账单美元', '参考折人民币', '金额口径']]
        for bill in data['rows']:
            usd, cny = (Decimal(bill[k]) if bill[k] is not None else None for k in ('usd_total', 'cny_total'))
            bill_rate = Decimal(bill['source_rate']) if bill.get('source_rate') else None
            payment_rows.append([bill['forwarder'], bill['order_no'] or bill['bill_no'], bill['departure_date'], bill['warehouse_date'], bill['due_date'], bill['payment_status'], cny, usd, cny + usd * bill_rate if cny is not None and usd is not None and bill_rate else None, bill['amount_basis']])
        sheets.append(('付款计划', payment_rows))
        for name, bills in groups.items():
            rows = [[label for _, label in FIELDS]]
            for i, bill in enumerate(sorted(bills, key=lambda r: (r['departure_date'], r['id'])), 2):
                values = []
                for key, label in FIELDS:
                    value = bill.get(key)
                    if key == 'sequence':
                        value = i - 1
                    elif key in NUMBER_FIELDS and value is not None:
                        value = Decimal(value)
                    elif key in ('converted_usd', 'converted_cny'):
                        own_rate = bill.get('source_rate')
                        valid = own_rate and bill['usd_total'] is not None and bill['cny_total'] is not None
                        rate_ref = "'汇总总览'!$B$2" if rate else own_rate
                        if valid:
                            usd, cny, fx = Decimal(bill['usd_total']), Decimal(bill['cny_total']), Decimal(own_rate)
                            expression = f'M{i}+R{i}/{rate_ref}' if key == 'converted_usd' else f'R{i}+M{i}*{rate_ref}'
                            cached = usd + cny / fx if key == 'converted_usd' else cny + usd * fx
                            value = Formula(f'IF(OR(M{i}="",R{i}=""),"",{expression})', cached)
                        else:
                            value = None
                    if key == 'usd_total' and value is not None:
                        value = Formula(f'SUM(H{i}:L{i})', value)
                    elif key == 'cny_total' and value is not None:
                        value = Formula(f'SUM(N{i}:Q{i})', value)
                    values.append(value)
                rows.append(values)
            sheets.append((name, rows))
        sheets.append(('异常费用清单', [['提单号/单号', '货代', '异常项目', '金额', '币种', '说明与建议']] + [[r['identifier'],r['forwarder'],r['title'],Decimal(r['amount']) if r['amount'] is not None else None,r['currency'],r['notes']] for r in data['issues']]))
        sheets.append(('数据说明与差异提醒', [['项目', '口径'], ['金额', '按原币保存；空白表示未提供'], ['参考折算', '人民币合计 + 美元合计 × 参考汇率'], ['付款状态', '待确认表示原表未确认付款'], ['一口价', '辅助估算拆分不追加到费用金额']]))
        snapshot = {'bills': {r['forwarder'] + '\0' + (r['order_no'] or r['bill_no']): {k: r.get(k) for k in ('version', 'source_rate', 'warehouse_date', 'warehouse_date_basis')} for r in data['rows']}}
        raw = dumps(snapshot)
        properties = {f'PanguSeaSnapshot.{i // 30000:04d}': raw[i:i + 30000] for i in range(0, len(raw), 30000)}
        return workbook(sheets, properties=properties)
