"""Read-only WMS evidence, explicit timezone conversion and atomic import."""
import hashlib,re,json
from collections import defaultdict
from datetime import datetime
from decimal import Decimal as D,localcontext
from urllib.parse import urlencode
from zoneinfo import ZoneInfo
from procurement import CostStore,CostError,dumps,timestamp
from landed import day

TIMEZONES=('Asia/Shanghai','UTC','America/New_York','America/Los_Angeles')

def accounting_date(raw,timezone):
    if timezone not in TIMEZONES:raise CostError('导入实际出库前须确认仓储源时间时区')
    try:
        dt=datetime.fromisoformat(raw)
        if dt.tzinfo:raise ValueError()
        zone=ZoneInfo(timezone);a=dt.replace(tzinfo=zone,fold=0);b=dt.replace(tzinfo=zone,fold=1)
        if a.utcoffset()!=b.utcoffset():raise CostError('源时间处于夏令时切换区间，须人工核对入库 / 出库日期')
        if a.astimezone(ZoneInfo('UTC')).astimezone(zone).replace(tzinfo=None)!=dt:raise CostError('源时间在所选时区不存在，须人工核对日期')
        return a.astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat()
    except (ValueError,TypeError):raise CostError('仓储源时间格式无效')


def evidence(manager,query):
    start=day(query.get('start') or datetime.now().date().replace(month=1,day=1).isoformat());end=day(query.get('end') or datetime.now().date().isoformat())
    if start>end:raise CostError('开始日期不能晚于截止日期')
    try:data=manager.get(manager.sales_url+'/api/restock/warehouse-evidence?'+urlencode({'start':start,'end':end,'kind':query.get('kind','receipt')}))['data']
    except Exception:raise CostError('仓储流水源读取失败，未覆盖本地成本记录',502)
    if data.get('version')!=1 or data.get('source_time_kind')!='timestamp_without_timezone':raise CostError('仓储流水快照格式无效',502)
    inputs=manager.valuation.inputs();candidates=defaultdict(list)
    for r in data['candidates']:candidates[r['source_key']].append(r)
    for r in data['records']:r['candidates']=candidates[r['source_key']]
    groups={}
    with localcontext() as ctx:
        ctx.prec=60
        for r in data['records']:
            if r['kind']!='入库单' or D(r['change_quantity'])<=0:continue
            key=(r['warehouse_code'],r['document_no'],r['raw_time'][:10]);g=groups.setdefault(key,{'key':dumps(key),'warehouse_code':key[0],'document_no':key[1],'raw_date':key[2],'records':[],'quantities':defaultdict(lambda:D(0))})
            g['records'].append(r);g['quantities'][r['warehouse_sku']]+=D(r['change_quantity'])
        for g in groups.values():
            g['quantities']={s:format(q,'f') for s,q in g['quantities'].items()};g['tickets']=[]
            for ticket in inputs['tickets']:
                if not re.search(r'(?<![A-Za-z0-9])'+re.escape(g['document_no'])+r'(?![A-Za-z0-9])',ticket.get('note','')):continue
                lines={l['warehouse_sku']:D(l['quantity']) for l in ticket['lines']}
                if all(s in lines and D(q)<=lines[s] for s,q in g['quantities'].items()):g['tickets'].append({'id':ticket['id'],'ticket_no':ticket['ticket_no'],'version':ticket['version']})
    return data|{'inbound_groups':list(groups.values()),'policy':inputs['accounting']['policy']}


def import_evidence(manager,payload):
    kind=payload.get('kind');selected=payload.get('keys')
    if kind not in ('movement','receipt') or not isinstance(selected,list) or not 1<=len(selected)<=5000 or any(not isinstance(k,str) for k in selected):raise CostError('须选择 1–5000 条仓储记录')
    data=evidence(manager,payload)
    if payload.get('source_sha256')!=data['sha256']:raise CostError('仓储源快照已变化，请重新读取后重试',409)
    policy=data['policy'];timezone=policy.get('warehouse_timezone','');wanted=set(selected)
    values=[r for r in (data['records'] if kind=='movement' else data['inbound_groups']) if (r['source_key'] if kind=='movement' else r['key']) in wanted]
    if len(values)!=len(wanted):raise CostError('源记录已变化，请重新读取后选择',409)
    archive_payload={'source_sha256':data['sha256'],'timezone':timezone,'kind':kind,'records':values};archive=dumps(archive_payload);sha=hashlib.sha256(archive.encode()).hexdigest()
    created=unchanged=0
    with manager.store.connection() as db,localcontext() as ctx:
        ctx.prec=60;db.execute('BEGIN IMMEDIATE')
        db.execute('''CREATE TABLE IF NOT EXISTS cost_warehouse_imports(sha256 TEXT PRIMARY KEY,payload TEXT NOT NULL,created_at TEXT NOT NULL)''')
        db.execute('INSERT OR IGNORE INTO cost_warehouse_imports VALUES(?,?,?)',(sha,archive,timestamp()))
        for r in values:
            if kind=='movement':
                if r['kind']!='出库单' or D(r['change_quantity'])>=0 or len(r['candidates'])!=1:raise CostError('仅可导入数量为负且唯一关联订单商品的出库流水')
                c=r['candidates'][0];qty=-D(r['change_quantity']);date=accounting_date(r['raw_time'],timezone)
                p={**c,'source_key':'xlwms:'+r['source_key'],'warehouse_code':r['warehouse_code'],'warehouse_sku':r['warehouse_sku'],'movement_date':date,
                   'quantity':format(qty/D(c['conversion_factor']),'f'),'warehouse_quantity':format(qty,'f'),'reference':'XLWMS stock_flow / '+r['source_key'],'version':0}
                result=manager.valuation.accounting.save('movement',p,db,True)
                same=result.get('unchanged')
            else:
                if len(r['tickets'])!=1:raise CostError('入库单未找到唯一采购单，须人工核对')
                dates={accounting_date(v['raw_time'],timezone) for v in r['records']} if timezone else {None}
                if len(dates)!=1:raise CostError('同组源流水转换后跨会计日期，须人工分批入库')
                t=r['tickets'][0];p={'version':0,'kind':'receipt','record_no':r['document_no']+'/'+r['raw_date'],'warehouse_code':r['warehouse_code'],
                    'received_date':next(iter(dates)),'ticket_id':t['id'],'ticket_version':t['version'],'note':'XLWMS 来源日期 '+r['raw_date']+'；'+('源时区 '+timezone if timezone else '源时区未确认，日期待核对'),
                    'lines':[{'warehouse_sku':s,'quantity':q} for s,q in r['quantities'].items()]}
                result=manager.valuation._save_record(db,p,True,sha);same=result.get('already_imported')
            unchanged+=int(bool(same));created+=int(not same)
    return {'created':created,'unchanged':unchanged,'source_sha256':sha}
