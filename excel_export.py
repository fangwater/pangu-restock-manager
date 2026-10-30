"""Dependency-free XLSX generation and bounded asynchronous export jobs."""
import io
import json
import logging
import math
import re
import threading
import uuid
from dataclasses import dataclass
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile

LOG = logging.getLogger('pangu-restock')
MIME = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'


class Percent(float):
    pass


@dataclass(frozen=True)
class Formula:
    expression: str
    cached: object = None


def xml_text(value):
    return escape(re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', str(value))[:32767], {'"': '&quot;'})


def column_name(index):
    result = ''
    while index:
        index, digit = divmod(index - 1, 26)
        result = chr(65 + digit) + result
    return result


def worksheet(rows):
    content = []
    for i, row in enumerate(rows, 1):
        cells = []
        for j, value in enumerate(row, 1):
            ref = column_name(j) + str(i)
            numeric = isinstance(value, (float, int, Decimal)) and not isinstance(value, bool)
            style = 1 if i == 1 else 3 if isinstance(value, Percent) else 2 if numeric or isinstance(value, Formula) else 0
            if isinstance(value, Formula):
                cached = '' if value.cached is None else str(value.cached)
                kind = ' t="str"' if value.cached is None or isinstance(value.cached, str) else ''
                cells.append(f'<c r="{ref}" s="{style}"{kind}><f>{xml_text(value.expression)}</f><v>{xml_text(cached)}</v></c>')
            elif numeric and (value.is_finite() if isinstance(value, Decimal) else math.isfinite(value)):
                cells.append(f'<c r="{ref}" s="{style}"><v>{float(value) if isinstance(value, Percent) else value}</v></c>')
            else:
                text = '' if value is None else '是' if value is True else '否' if value is False else value
                cells.append(f'<c r="{ref}" s="{style}" t="inlineStr"><is><t xml:space="preserve">{xml_text(text)}</t></is></c>')
        content.append(f'<row r="{i}">{"".join(cells)}</row>')
    count = max(map(len, rows), default=1)
    return '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' + f'''<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
    <sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>
    <cols><col min="1" max="1" width="8" customWidth="1"/><col min="2" max="2" width="35" customWidth="1"/><col min="3" max="{max(3, count)}" width="19" customWidth="1"/></cols>
    <sheetData>{''.join(content)}</sheetData><autoFilter ref="A1:{column_name(count)}{max(1,len(rows))}"/></worksheet>'''


def workbook(sheets, properties=None):
    output = io.BytesIO()
    with ZipFile(output, 'w', compression=ZIP_DEFLATED) as archive:
        types = ['<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>', '<Default Extension="xml" ContentType="application/xml"/>', '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>', '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>']
        for i, (_, rows) in enumerate(sheets, 1):
            types.append(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>')
            archive.writestr(f'xl/worksheets/sheet{i}.xml', worksheet(rows))
        property_relationship = ''
        if properties:
            types.append('<Override PartName="/docProps/custom.xml" ContentType="application/vnd.openxmlformats-officedocument.custom-properties+xml"/>')
            property_relationship = '<Relationship Id="customProperties" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/custom-properties" Target="docProps/custom.xml"/>'
            values = ''.join(f'<property fmtid="{{D5CDD505-2E9C-101B-9397-08002B2CF9AE}}" pid="{i}" name="{xml_text(name)}"><vt:lpwstr>{xml_text(value)}</vt:lpwstr></property>' for i, (name, value) in enumerate(properties.items(), 2))
            archive.writestr('docProps/custom.xml', '<?xml version="1.0" encoding="UTF-8"?><Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/custom-properties" xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">' + values + '</Properties>')
        archive.writestr('[Content_Types].xml', '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">' + ''.join(types) + '</Types>')
        archive.writestr('_rels/.rels', '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>' + property_relationship + '</Relationships>')
        archive.writestr('xl/workbook.xml', '<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>' + ''.join(f'<sheet name="{xml_text(name)}" sheetId="{i}" r:id="rId{i}"/>' for i, (name, _) in enumerate(sheets, 1)) + '</sheets></workbook>')
        archive.writestr('xl/_rels/workbook.xml.rels', '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' + ''.join(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>' for i in range(1, len(sheets) + 1)) + '<Relationship Id="styles" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>')
        archive.writestr('xl/styles.xml', '''<?xml version="1.0" encoding="UTF-8"?><styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
        <numFmts count="2"><numFmt numFmtId="164" formatCode="#,##0.##"/><numFmt numFmtId="165" formatCode="0.00%"/></numFmts>
        <fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/><color rgb="FFFFFFFF"/></font></fonts>
        <fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF6B5CE7"/><bgColor indexed="64"/></patternFill></fill></fills>
        <borders count="1"><border/></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
        <cellXfs count="4"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/><xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/><xf numFmtId="165" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/></cellXfs>
        <cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>''')
    return output.getvalue()


def report_workbook(report, kind):
    filters = report['filters']
    shop_names = {s['key']: s['name'] for s in report['shops']}
    category_names = {c['code']: c['name'] for c in report['categories']}
    metadata = [['项目', '值'], ['报表', '采购建议单' if kind == 'purchase' else 'SKU 销量占比'],
                ['导出时间（北京时间）', report['generated_at']], ['开始日期', filters['start']], ['结束日期', filters['end']],
                ['窗口天数', filters['days']], ['店铺', '、'.join(shop_names[k] for k in filters['shops'])],
                ['类目', category_names.get(filters['category'], '全部类目')], ['订单状态', '、'.join(report['statuses'][s] for s in filters['statuses'])],
                ['搜索', filters.get('q', '')], ['占比基数', '订单实际售出 quantity，取消/退款订单剔除'],
                ['销量归属', '按销售 SKU 汇总原始订单件数；不同包装规格不并入仓库 SKU 销量'],
                ['备货单位', '按仓库 SKU 汇总各销售 SKU 数量 × 对应换算系数'],
                ['备货公式', '日均仓库需求 × 覆盖天数 × (1 + 安全系数) − 可售库存 − 计入在途，再向上取整至箱规倍数'],
                ['全局占比分母', '当前日期、店铺及订单状态下全部类目的销量；类目与搜索筛选不改变分母'],
                ['百分比舍入', '采用最大余数法保留两位小数，确保同一完整维度合计为 100%'],
                ['计划锁定', '锁定计划的数量及已保存计算依据保留；不会被未锁定计划重算覆盖']]
    metadata.extend(['数据提示', warning] for warning in report['warnings'])
    categories = [['一级类目', '销量（订单件数）', '全局占比', '有销量 SKU 数']] + [[c['name'], c['total'], Percent(c['share'] / 100), c['sku_count']] for c in report['categories']]
    categories.append(['合计', report['summary']['global_total'], Percent(1 if report['summary']['global_total'] else 0), sum(c['sku_count'] for c in report['categories'])])
    shops = filters['shops']
    detail = [['类目内排名', 'SKU', '一级类目', '颜色', '规格', *[shop_names[k] + ' 销量' for k in shops], '销量（订单件数）', '类目内占比', '全局占比', '仓库换算需求', '可售库存', '在途库存', '建议备货', '箱规', '计划数量', '计划锁定']]
    purchase = [['SKU', '一级类目', '颜色', '规格', '最新建议量', '采购数量', '数量来源', '日均本 SKU 销量', '日均仓库需求', '覆盖天数', '安全系数', '可售库存', '抵扣在途', '箱规', '箱数', '计算日期范围', '依据保存时间', '备注']]
    demand = [['依据', '仓库 SKU', '店铺', '销售 SKU', '订单销量', '换算系数', '仓库需求', '开始日期', '结束日期']]
    rows = report['rows']
    if filters.get('q'):
        q = filters['q'].lower()
        rows = [r for r in rows if q in (r['sku'] + ' ' + r['name'] + ' ' + r['color'] + ' ' + r['spec']).lower()]
    for row in rows:
        p = row['plan']
        for source in row['demand_sources']:
            demand.append(['当前统计', row['sku'], shop_names[source['shop']], source['sales_sku'], source['units'], source['conversion_factor'], source['warehouse_units'], filters['start'], filters['end']])
        detail.append([row['rank'], row['sku'], row['category_name'], row['color'], row['spec'], *[row['sales'][k] for k in shops], row['total'], Percent(row['category_share'] / 100), Percent(row['global_share'] / 100), row['warehouse_total'], row['available'], row['in_transit'], row['suggested'], row['basis']['box_size'], p['quantity'] if p else None, bool(p and p.get('locked'))])
        if (p and p['status'] != 'completed' and p['quantity'] > 0) or (not p and row['suggested'] and row['suggested'] > 0):
            basis = (p.get('basis') or {}) if p else row['basis']
            qty = p['quantity'] if p else row['suggested']
            window = (p.get('basis_filter') or {}) if p else filters
            box = basis.get('box_size')
            purchase.append([row['sku'], row['category_name'], row['color'], row['spec'], row['suggested'], qty,
                             '锁定计划' if p and p.get('locked') else '已保存计划' if p else '自动建议',
                             basis.get('order_daily_rate'), basis.get('daily_rate'), basis.get('target_days'),
                             Percent(basis.get('safety_factor', 0)), basis.get('available'), basis.get('in_transit_deducted'), box,
                             qty / box if box else None, f"{window.get('start', '旧计划未记录')} — {window.get('end', '')}", basis.get('calculated_at'), p.get('note', '') if p else row['suggestion_reason']])
            if p:
                for source in basis.get('demand_sources', []):
                    demand.append(['已保存计划', row['sku'], shop_names.get(source['shop'], source['shop']), source['sales_sku'], source['units'], source['conversion_factor'], source['warehouse_units'], window.get('start'), window.get('end')])
    return workbook([('筛选与口径', metadata), ('类目占比', categories), ('SKU销量', detail), ('采购建议', purchase), ('仓库需求明细', demand)])


class ExportQueue:
    def __init__(self, store, clock):
        self.store, self.clock = store, clock
        self.root = Path(store.path).parent / 'exports'
        self.root.mkdir(mode=0o700, exist_ok=True)
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='excel-export')
        self.lock = threading.Lock()

    def create(self, report, kind):
        if kind not in ('report', 'purchase'):
            raise ValueError('导出类型无效')
        with self.lock, self.store.connection() as db:
            if db.execute("SELECT count(*) FROM export_jobs WHERE status IN ('queued','running')").fetchone()[0] >= 6:
                raise ValueError('导出任务较多，请稍后重试')
            cutoff = (datetime.fromisoformat(self.clock()) - timedelta(days=1)).isoformat()
            for row in db.execute('SELECT id FROM export_jobs WHERE created_at<?', (cutoff,)):
                (self.root / (row['id'] + '.xlsx')).unlink(missing_ok=True)
            db.execute('DELETE FROM export_jobs WHERE created_at<?', (cutoff,))
            job = uuid.uuid4().hex
            db.execute('INSERT INTO export_jobs(id,kind,status,created_at,filters) VALUES(?,?,?,?,?)', (job, kind, 'queued', self.clock(), json.dumps(report['filters'])))
        self.pool.submit(self.run, job, report, kind)
        return self.get(job)

    def run(self, job, report, kind):
        try:
            with self.store.connection() as db:
                db.execute("UPDATE export_jobs SET status='running' WHERE id=?", (job,))
            body = report_workbook(report, kind)
            target = self.root / (job + '.xlsx')
            target.write_bytes(body)
            target.chmod(0o600)
            filename = f'pangu-{kind}-{report["range"]["start"]}-{report["range"]["end"]}.xlsx'
            with self.store.connection() as db:
                db.execute("UPDATE export_jobs SET status='ready',filename=?,completed_at=? WHERE id=?", (filename, self.clock(), job))
        except Exception as error:
            LOG.warning('Excel export failed: %s', type(error).__name__)
            with self.store.connection() as db:
                db.execute("UPDATE export_jobs SET status='failed',error='导出失败，请重试',completed_at=? WHERE id=?", (self.clock(), job))

    def get(self, job):
        if not re.fullmatch(r'[a-f0-9]{32}', job):
            return None
        with self.store.connection() as db:
            row = db.execute('SELECT * FROM export_jobs WHERE id=?', (job,)).fetchone()
        return {**dict(row), 'filters': json.loads(row['filters'])} if row else None

    def download(self, job):
        info = self.get(job)
        if not info or info['status'] != 'ready':
            return None
        path = self.root / (job + '.xlsx')
        if not path.is_file():
            return None
        return path.read_bytes(), info['filename']
