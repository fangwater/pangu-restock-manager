"""Versioned landed costs, FX quotes, warehouse movements and return evidence."""
import csv
import hashlib
import io
import json
import sqlite3
from collections import defaultdict
from datetime import date
from decimal import Decimal, localcontext, ROUND_HALF_UP
from procurement import CostError, CostStore, currency, number, record_id, text, timestamp, dumps

D = Decimal
Q = D('0.0000000001')
COMPONENTS = ('purchase', 'head_leg', 'logistics')
METHODS = ('declared_value', 'quantity', 'purchase_value', 'manual')


def day(value, label='日期', optional=False):
    value = text(value, label, not optional, 10)
    if optional and not value:
        return None
    try:
        if date.fromisoformat(value).isoformat() != value:
            raise ValueError()
    except ValueError:
        raise CostError(label + '须为 YYYY-MM-DD')
    return value


def positive(value, label, zero=False):
    value = number(value, label, True)
    if value is None or D(value) < 0 or (not zero and D(value) == 0):
        raise CostError(label + ('须为非负数' if zero else '须大于零'))
    return value


def flag(value, label):
    if not isinstance(value, bool):
        raise CostError(label + '须明确选择是或否')
    return value


def round10(value):
    return value.quantize(Q, rounding=ROUND_HALF_UP)


class AccountingStore:
    KINDS = ('rate', 'rule', 'terms', 'movement', 'refund', 'coverage')
    def __init__(self, store):
        self.store = store
        with store.connection() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS cost_accounting_records(id INTEGER PRIMARY KEY AUTOINCREMENT,
              kind TEXT NOT NULL,record_key TEXT NOT NULL,payload TEXT NOT NULL,version INTEGER NOT NULL,
              created_at TEXT NOT NULL,updated_at TEXT NOT NULL,deleted_at TEXT,UNIQUE(kind,record_key));
            CREATE TABLE IF NOT EXISTS cost_accounting_policy(id INTEGER PRIMARY KEY CHECK(id=1),payload TEXT NOT NULL,
              version INTEGER NOT NULL,updated_at TEXT NOT NULL);
            ''')
            db.execute('INSERT OR IGNORE INTO cost_accounting_policy VALUES(1,?,1,?)',
                       (dumps({'report_currency':'CNY','demand_basis':'order','fx_date':'sale','warehouse_timezone':''}), timestamp()))

    def snapshot(self, db=None):
        if db is None:
            with self.store.connection() as conn:
                conn.execute('BEGIN')
                return self.snapshot(conn)
        row = db.execute('SELECT * FROM cost_accounting_policy WHERE id=1').fetchone()
        policy = {**json.loads(row['payload']), 'version':row['version'], 'updated_at':row['updated_at']}
        records = [CostStore.item(r) | {'kind':r['kind']} for r in db.execute('SELECT * FROM cost_accounting_records WHERE deleted_at IS NULL ORDER BY id')]
        data = {'policy':policy, 'records':records}
        return {**data, 'sha256':hashlib.sha256(dumps(data).encode()).hexdigest()}

    def save_policy(self, payload):
        after = {'report_currency':currency(payload.get('report_currency'),True),
                 'demand_basis':payload.get('demand_basis'), 'fx_date':payload.get('fx_date'), 'warehouse_timezone':text(payload.get('warehouse_timezone'),'仓储源时间时区',maximum=80)}
        if after['warehouse_timezone'] not in ('','Asia/Shanghai','UTC','America/New_York','America/Los_Angeles'):raise CostError('仓储源时间时区无效')
        if after['demand_basis'] not in ('order','outbound') or after['fx_date'] not in ('sale','receipt'):
            raise CostError('需求口径或成本汇率日期无效')
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM cost_accounting_policy WHERE id=1').fetchone()
            CostStore.check_version({'version':row['version'],'deleted_at':None},payload)
            db.execute('UPDATE cost_accounting_policy SET payload=?,version=version+1,updated_at=? WHERE id=1',(dumps(after),timestamp()))
            CostStore.audit(db,'accounting_policy',1,'update',json.loads(row['payload']),after)
        return self.snapshot()['policy']

    def normalize(self, db, kind, p):
        if kind == 'rate':
            a,b=currency(p.get('from_currency'),True),currency(p.get('to_currency'),True)
            if a==b:raise CostError('汇率两端币种须不同')
            after={'from_currency':a,'to_currency':b,'effective_date':day(p.get('effective_date')),
                   'rate':positive(p.get('rate'),'汇率'),'reference':text(p.get('reference'),'汇率来源',True,1000)}
            key=[a,b,after['effective_date']]
        elif kind == 'rule':
            type_id=text(str(p.get('cost_type_id') or ''),'费用类型',True)
            types={str(k['id']) for t in CostStore.rows(db,'cost_templates') for c in t['categories'] for k in c['types']}
            if type_id not in types:raise CostError('费用类型不存在')
            bucket=p.get('bucket');method=p.get('method','declared_value')
            if bucket not in ('head_leg','logistics','excluded') or method not in METHODS:raise CostError('费用归属或分摊方法无效')
            after={'cost_type_id':type_id,'bucket':bucket,'method':method,'note':text(p.get('note'),'备注',maximum=2000)}
            key=[type_id]
        elif kind == 'terms':
            tid=record_id(p.get('ticket_id'))
            row=db.execute('SELECT * FROM cost_tickets WHERE id=? AND deleted_at IS NULL',(tid,)).fetchone()
            if not row:raise CostError('采购单不存在')
            ticket=CostStore.item(row)
            if p.get('ticket_version')!=ticket['version']:raise CostError('采购单已更新，请刷新后重试',409)
            lines=p.get('lines',[])
            if not isinstance(lines,list) or len(lines)>2000:raise CostError('分摊依据明细无效')
            valid={l['warehouse_sku'] for l in ticket['lines']};seen=set();items=[]
            for l in lines:
                sku=text(l.get('warehouse_sku'),'仓库 SKU',True)
                if sku not in valid or sku in seen:raise CostError('SKU 不属于采购单或重复')
                seen.add(sku)
                items.append({'warehouse_sku':sku,'declared_value':number(l.get('declared_value'),'申报总金额',True),
                              'currency':currency(l.get('currency')),'weight':number(l.get('weight'),'分摊权重',True)})
            after={'ticket_id':tid,'ticket_version':ticket['version'],'head_leg_complete':flag(p.get('head_leg_complete'),'头程完整'),
                   'logistics_complete':flag(p.get('logistics_complete'),'物流完整'),'lines':items}
            key=[tid]
        elif kind == 'movement':
            after={k:text(p.get(k),label,True) for k,label in [('source_key','出库明细键'),('order_no','订单号'),('platform_sku','销售 SKU'),('warehouse_sku','仓库 SKU'),('warehouse_code','仓库'),('shop_key','店铺')]}
            platform=p.get('platform')
            if platform not in ('temu','shein'):raise CostError('平台无效')
            after.update(platform=platform,movement_date=day(p.get('movement_date')),quantity=positive(p.get('quantity'),'销售数量'),
                         warehouse_quantity=positive(p.get('warehouse_quantity'),'出库数量'),reference=text(p.get('reference'),'出库凭证',True,1000))
            key=[after['shop_key'],after['source_key']]
        elif kind == 'refund':
            mid=record_id(p.get('movement_id'))
            row=db.execute("SELECT * FROM cost_accounting_records WHERE id=? AND kind='movement' AND deleted_at IS NULL",(mid,)).fetchone()
            if not row:raise CostError('原出库明细不存在')
            movement=CostStore.item(row)
            source_key=text(p.get('source_key'),'退款商品明细键',True)
            reverse=p.get('cost_action')
            if reverse not in ('none','purchase','all'):raise CostError('成本回冲方式无效')
            received=flag(p.get('restocked'),'已退回库存')
            if not received and reverse!='none':raise CostError('未实际退回库存不能回冲商品成本')
            d=day(p.get('returned_date'))
            if d<movement['movement_date']:raise CostError('退款日期不能早于原出库日期')
            qty=positive(p.get('warehouse_quantity'),'退款仓库数量');sales_qty=positive(p.get('quantity'),'退款商品数量')
            with localcontext() as ctx:
                ctx.prec=60
                if D(sales_qty)*D(movement['warehouse_quantity'])!=D(qty)*D(movement['quantity']):raise CostError('退款销售数量与原出库换算比例不符')
                previous=[json.loads(r['payload']) for r in db.execute("SELECT payload FROM cost_accounting_records WHERE kind='refund' AND deleted_at IS NULL AND id<>?",(p.get('id') or 0,))]
                if sum((D(r['warehouse_quantity']) for r in previous if r['movement_id']==mid),D(0))+D(qty)>D(movement['warehouse_quantity']):raise CostError('累计退款数量超过原出库数量')
            after={'source_key':source_key,'movement_id':mid,'movement_version':movement['version'],'shop_key':movement['shop_key'],
                   'returned_date':d,'quantity':sales_qty,'warehouse_quantity':qty,'restocked':received,'cost_action':reverse,
                   'reference':text(p.get('reference'),'退款凭证',True,1000)}
            key=[movement['shop_key'],source_key]
        elif kind == 'coverage':
            a,b=day(p.get('start')),day(p.get('end'))
            if a>b:raise CostError('开始日期不能晚于结束日期')
            scope=p.get('scope')
            if scope not in ('outbound','refund'):raise CostError('完整性范围无效')
            after={'shop_key':text(p.get('shop_key'),'店铺',True),'scope':scope,'start':a,'end':b,
                   'confirmed':flag(p.get('confirmed'),'完整性确认'),'reference':text(p.get('reference'),'核对凭证',True,1000)}
            key=[after['shop_key'],scope,a,b]
        else:raise CostError('核算记录类型无效')
        return after,dumps(key)

    def save(self, kind, payload, db=None, idempotent=False):
        if kind not in self.KINDS:raise CostError('核算记录类型无效')
        if db is None:
            with self.store.connection() as conn:
                conn.execute('BEGIN IMMEDIATE')
                return self.save(kind,payload,conn,idempotent)
        identity=record_id(payload.get('id'),True)
        row=db.execute('SELECT * FROM cost_accounting_records WHERE id=? AND kind=?',(identity,kind)).fetchone() if identity else None
        if identity and not row:raise CostError('记录不存在',404)
        old=CostStore.item(row) if row else None
        CostStore.check_version(old,payload)
        after,key=self.normalize(db,kind,payload)
        existing=db.execute('SELECT * FROM cost_accounting_records WHERE kind=? AND record_key=?',(kind,key)).fetchone()
        if existing and existing['id']!=identity:
            if idempotent and not existing['deleted_at'] and json.loads(existing['payload'])==after:return {'unchanged':True,'id':existing['id']}
            if existing['deleted_at'] and not identity:identity=existing['id'];old=CostStore.item(existing)
            else:raise CostError('业务键重复，须编辑原记录',409)
        stamp=timestamp()
        if identity:db.execute('UPDATE cost_accounting_records SET record_key=?,payload=?,version=version+1,deleted_at=NULL,updated_at=? WHERE id=?',(key,dumps(after),stamp,identity))
        else:identity=db.execute('INSERT INTO cost_accounting_records(kind,record_key,payload,version,created_at,updated_at) VALUES(?,?,?,1,?,?)',(kind,key,dumps(after),stamp,stamp)).lastrowid
        CostStore.audit(db,'accounting_'+kind,identity,'update' if old else 'create',old,after)
        return CostStore.item(db.execute('SELECT * FROM cost_accounting_records WHERE id=?',(identity,)).fetchone())

    def remove(self,payload):
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM cost_accounting_records WHERE id=?',(record_id(payload.get('id')),)).fetchone()
            if not row:raise CostError('记录不存在',404)
            old=CostStore.item(row);CostStore.check_version(old,payload)
            if row['kind']=='movement' and any(json.loads(r['payload'])['movement_id']==row['id'] for r in db.execute("SELECT payload FROM cost_accounting_records WHERE kind='refund' AND deleted_at IS NULL")):
                raise CostError('出库明细已有退款引用，不能删除',409)
            db.execute('UPDATE cost_accounting_records SET deleted_at=?,updated_at=?,version=version+1 WHERE id=?',(timestamp(),timestamp(),row['id']))
            CostStore.audit(db,'accounting_'+row['kind'],row['id'],'delete',old,None)
        return {'id':row['id']}

    def import_rows(self,kind,rows):
        if not isinstance(rows,list) or not 1<=len(rows)<=5000 or any(not isinstance(r,dict) for r in rows):raise CostError('须提交 1–5000 条记录')
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            results=[self.save(kind,{**r,'version':0},db,True) for r in rows]
        return {'created':sum(not r.get('unchanged') for r in results),'unchanged':sum(bool(r.get('unchanged')) for r in results)}


def fx_convert(amount, source, target, when, records):
    if amount is None or not source:return None,None
    if source==target:return amount,{'from_currency':source,'to_currency':target,'rate':'1','effective_date':when,'id':None,'version':None}
    if amount==0:return D(0),{'from_currency':source,'to_currency':target,'zero_amount':True}
    rates=[r for r in records if r['kind']=='rate' and r['effective_date']<=when and {r['from_currency'],r['to_currency']}=={source,target}]
    if not rates:return None,None
    row=max(rates,key=lambda r:(r['effective_date'],r['from_currency']==source,r['id']))
    with localcontext() as ctx:
        ctx.prec=60
        rate=D(row['rate']) if row['from_currency']==source else D(1)/D(row['rate'])
        return round10(amount*rate),{k:row[k] for k in ('id','version','effective_date','reference')}|{'from_currency':source,'to_currency':target,'rate':format(rate,'f'),'inverse':row['from_currency']!=source}


def allocate_ticket(ticket, accounting):
    """Return per-SKU native components, completeness and every fee allocation."""
    with localcontext() as ctx:
        ctx.prec=60
        records=accounting['records'];rules={r['cost_type_id']:r for r in records if r['kind']=='rule'}
        terms=next((r for r in records if r['kind']=='terms' and r['ticket_id']==ticket['id']),None)
        valid_terms=terms and terms['ticket_version']==ticket['version']
        bases={l['warehouse_sku']:l for l in terms['lines']} if valid_terms else {}
        result={l['warehouse_sku']:{'amounts':{c:{} for c in COMPONENTS},'complete':{c:bool(valid_terms and terms.get(c+'_complete')) for c in COMPONENTS},'trace':[],'issues':[]} for l in ticket['lines']}
        for l in ticket['lines']:
            r=result[l['warehouse_sku']];known=l.get('amount') is not None and ticket.get('purchase_currency') and D(l['amount'])>=0
            r['complete']['purchase']=bool(known)
            if known:r['amounts']['purchase'][ticket['purchase_currency']]=D(l['amount'])
        for fee in ticket['costs']:
            rule=rules.get(str(fee['cost_type_id']))
            if rule and rule['bucket']=='excluded':continue
            bucket=rule['bucket'] if rule else None
            reason=None;weights={}
            if not rule:reason='费用类型未配置归属'
            elif fee.get('amount') is None or not fee.get('currency'):reason='费用金额或币种缺失'
            else:
                for l in ticket['lines']:
                    sku=l['warehouse_sku'];basis=bases.get(sku,{})
                    value={'quantity':l.get('quantity'),'purchase_value':l.get('amount'),'declared_value':basis.get('declared_value'),'manual':basis.get('weight')}[rule['method']]
                    if value is None or D(value)<0:reason='分摊依据不完整';break
                    weights[sku]=D(value)
                if not reason and rule['method']=='declared_value' and (not valid_terms or len({bases.get(s,{}).get('currency') for s in weights})!=1 or not all(bases.get(s,{}).get('currency') for s in weights)):reason='申报金额币种缺失或不一致'
                if not reason and sum(weights.values(),D(0))<=0:reason='分摊依据合计须大于零'
                if not reason and rule['method']=='purchase_value' and not ticket.get('purchase_currency'):reason='采购金额币种缺失'
            if reason:
                for r in result.values():
                    for c in ((bucket,) if bucket else ('head_leg','logistics')):r['complete'][c]=False
                    r['issues'].append(reason)
                continue
            amount=D(fee['amount']);total=sum(weights.values(),D(0));cumulative=D(0);previous=D(0)
            for sku in sorted(weights):
                cumulative+=weights[sku];allocated=round10(amount*cumulative/total)-previous;previous+=allocated
                r=result[sku];r['amounts'][bucket][fee['currency']]=r['amounts'][bucket].get(fee['currency'],D(0))+allocated
                r['trace'].append({'cost_type_id':str(fee['cost_type_id']),'fee_id':fee.get('id'),'fee_name':fee['cost_type_name'],
                                   'currency':fee['currency'],'original_amount':fee['amount'],'amount':format(allocated,'f'),'weight':format(weights[sku],'f'),
                                   'weight_total':format(total,'f'),'method':rule['method'],'bucket':bucket,'rule_id':rule['id'],'rule_version':rule['version']})
        return result


def covered(records,scope,shop,start,end):
    intervals=sorted((r['start'],r['end']) for r in records if r['kind']=='coverage' and r['scope']==scope and r['shop_key']==shop and r['confirmed'])
    cursor=date.fromisoformat(start);last=date.fromisoformat(end)
    from datetime import timedelta
    for a,b in intervals:
        a,b=date.fromisoformat(a),date.fromisoformat(b)
        if a>cursor:break
        if b>=cursor:cursor=b+timedelta(days=1)
        if cursor>last:return True
    return cursor>last

# Public CSV labels; identifiers are resolved against business document numbers.
CSV_FIELDS={
 'rate':[('原币','from_currency'),('目标币','to_currency'),('生效日期','effective_date'),('汇率','rate'),('来源凭证','reference')],
 'rule':[('费用类型','cost_type_id'),('费用归属','bucket'),('分摊方式','method'),('备注','note')],
 'movement':[('出库明细键','source_key'),('平台','platform'),('店铺','shop_key'),('订单号','order_no'),('销售SKU','platform_sku'),('仓库','warehouse_code'),('仓库SKU','warehouse_sku'),('出库日期','movement_date'),('销售数量','quantity'),('出库数量','warehouse_quantity'),('来源凭证','reference')],
 'refund':[('退款商品明细键','source_key'),('店铺','shop_key'),('原出库明细键','movement_id'),('退款日期','returned_date'),('退款商品数量','quantity'),('退款仓库数量','warehouse_quantity'),('已退回库存','restocked'),('成本回冲','cost_action'),('来源凭证','reference')],
 'coverage':[('店铺','shop_key'),('范围','scope'),('开始日期','start'),('截止日期','end'),('已确认完整','confirmed'),('来源凭证','reference')]
}
CSV_ENUMS={'bucket':{'头程':'head_leg','物流':'logistics','不计入商品成本':'excluded'},'method':{'申报金额':'declared_value','数量':'quantity','采购金额':'purchase_value','人工权重':'manual'},
           'scope':{'出库':'outbound','退款':'refund'},'cost_action':{'不回冲':'none','仅采购成本':'purchase','全部商品成本':'all'},'platform':{'TEMU':'temu','SHEIN':'shein'},'restocked':{'是':True,'否':False},'confirmed':{'是':True,'否':False}}


def import_csv(store,kind,content):
    if kind not in CSV_FIELDS:raise CostError('此类型不支持 CSV 导入')
    if not isinstance(content,str) or len(content.encode())>700000:raise CostError('CSV 超过 700 KB 或格式无效')
    try:rows=list(csv.DictReader(io.StringIO(content.lstrip('\ufeff'))))
    except csv.Error:raise CostError('CSV 格式无效')
    records=store.snapshot()['records'];movements={(r['shop_key'],r['source_key']):r['id'] for r in records if r['kind']=='movement'}
    with store.store.connection() as db:
        fee_types={f"{t['name']} / {c['name']} / {k['name']}":str(k['id']) for t in CostStore.rows(db,'cost_templates') for c in t['categories'] for k in c['types']}
    mapped=[]
    for row in rows:
        p={key:row.get(label) for label,key in CSV_FIELDS[kind]}
        for key,options in CSV_ENUMS.items():
            if key in p:p[key]=options.get(p[key],p[key])
        if kind=='rule':p['cost_type_id']=fee_types.get(p['cost_type_id'],p['cost_type_id'])
        if kind=='refund':p['movement_id']=movements.get((p['shop_key'],p['movement_id']))
        mapped.append(p)
    return store.import_rows(kind,mapped)
