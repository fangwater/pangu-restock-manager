"""Versioned purchase CSV patches; preview is read only and commits are atomic."""
import copy
import csv
import hashlib
import io
import json
import re
from decimal import Decimal, localcontext
from procurement import CostError, CostStore, currency, dumps, text, timestamp

FIELDS = [('ticket_no','库存单号'),('version','版本'),('template','成本模板'),('template_version','模板版本'),
          ('purchase_currency','采购币种'),('effective_date','成本生效日期'),('ticket_note','单据备注'),
          ('kind','明细类型'),('sku','仓库SKU'),('cost_type_id','费用类型编号'),('category','费用分类'),
          ('fee','费用名称'),('fee_currency','费用币种'),('quantity','数量'),('unit_price','单价'),('amount','金额'),('note','明细备注')]
META = [k for k,_ in FIELDS[:7]]
NUMBERS = ('quantity','unit_price','amount')
MAX_BYTES, MAX_ROWS, MAX_TICKETS = 700000, 5000, 200


def csv_text(value):
    value = '' if value is None else str(value)
    if value.startswith("'") or value.lstrip().startswith(('=', '+', '-', '@', '\t', '\r')):
        # Apostrophes are doubled so an exported literal apostrophe is reversible.
        if re.fullmatch(r'-?[0-9]+(?:\.[0-9]+)?',value):return value
        return "'"+value
    return value


def unescape(value):
    if value.startswith("''"):return value[1:]
    if value.startswith("'") and value[1:].lstrip().startswith(('=', '+', '-', '@', '\t', '\r')):return value[1:]
    return value


def encode_rows(headers, rows):
    out=io.StringIO(newline='');writer=csv.writer(out);writer.writerow(headers)
    writer.writerows([[csv_text(v) for v in row] for row in rows])
    return ('\ufeff'+out.getvalue()).encode('utf-8')


def export_csv(costs, filters=None, template=False, catalog=False):
    filters=filters or {};data=costs.snapshot(full=True);templates={t['id']:t for t in data['templates']}
    if catalog:
        return encode_rows(['成本模板','模板版本','费用类型编号','费用分类','费用名称','费用币种','计算方式'],
            [[t['name'],t['version'],k['id'],c['name'],k['name'],c['currency'],
              {'direct_amount':'直接金额','quantity_x_unit':'数量 × 单价'}[k['calculation_mode']]]
             for t in data['templates'] for c in t['categories'] for k in c['types']])
    rows=[]
    if not template:
        q=filters.get('q','').casefold();template_id=filters.get('template','');fee=filters.get('fee','')
        if fee not in ('','missing','recorded'):raise CostError('附加费用筛选无效')
        if template_id and not re.fullmatch('[1-9][0-9]*',template_id):raise CostError('成本模板筛选无效')
        for ticket in sorted(data['tickets'],key=lambda t:t['id']):
            if q and q not in ' '.join([ticket['ticket_no'],ticket.get('note') or '',*ticket['skus']]).casefold():continue
            if template_id and str(ticket.get('cost_template_id') or '')!=template_id:continue
            if (fee=='missing' and ticket['fee_entries']) or (fee=='recorded' and not ticket['fee_entries']):continue
            t=templates.get(ticket.get('cost_template_id'))
            meta=[ticket['ticket_no'],ticket['version'],t['name'] if t else '',t['version'] if t else '',
                  ticket.get('purchase_currency'),ticket.get('effective_date'),ticket.get('note')]
            for l in ticket['lines']:
                rows.append(meta+['SKU',l['warehouse_sku'],'','','','',*[l.get(k) for k in NUMBERS],l.get('note')])
            for c in ticket['costs']:
                rows.append(meta+['费用','',c['cost_type_id'],c['category_name'],c['cost_type_name'],c['currency'],
                                  *[c.get(k) for k in NUMBERS],c.get('note')])
    return encode_rows([label for _,label in FIELDS],rows)


def parse_csv(raw):
    if not isinstance(raw,str):raise CostError('CSV 须为 UTF-8 文本且不超过 700 KB')
    try:size=len(raw.encode('utf-8'))
    except UnicodeError:raise CostError('CSV 须为有效 UTF-8 文本')
    if size>MAX_BYTES:raise CostError('CSV 须为 UTF-8 文本且不超过 700 KB')
    raw=raw.removeprefix('\ufeff');rows=[]
    try:
        reader=csv.reader(io.StringIO(raw,newline=''),strict=True);headers=next(reader,[])
        if len(headers)!=len(set(headers)) or set(headers)!={label for _,label in FIELDS}:raise CostError('CSV 表头须与采购成本模板一致，不能缺列、重复或增加列')
        keys={label:key for key,label in FIELDS}
        for values in reader:
            if not any(v.strip() for v in values):continue
            if len(values)!=len(headers):raise CostError(f'第 {reader.line_num} 行列数与表头不一致')
            row={keys[k]:unescape(v) for k,v in zip(headers,values)};row['_line']=reader.line_num;rows.append(row)
            if len(rows)>MAX_ROWS:raise CostError('CSV 超过 5000 行，请分批导入')
    except (csv.Error,UnicodeError) as exc:raise CostError('CSV 格式无效：'+str(exc))
    if not rows:raise CostError('CSV 没有采购明细')
    return raw,rows


def integer(raw,label,zero=True):
    if not re.fullmatch(r'(?:0|[1-9][0-9]{0,9})',raw.strip()) or (not zero and raw.strip()=='0'):raise CostError(label+'须为'+('非负' if zero else '正')+'整数')
    return int(raw)


def signature(ticket):
    if ticket is None:return None
    def entry(value,fields):
        return tuple(Decimal(str(value[k])) if k in NUMBERS and value.get(k) not in (None,'') else
                     (None if k in NUMBERS else str(value.get(k) or '').strip()) for k in fields)
    return (ticket.get('cost_template_id'),ticket.get('purchase_currency'),ticket.get('effective_date') or '',str(ticket.get('note') or '').strip(),
            sorted((l['warehouse_sku'],entry(l,(*NUMBERS,'note'))) for l in ticket['lines']),
            sorted((str(c['cost_type_id']),entry(c,('currency','calculation_mode',*NUMBERS,'note'))) for c in ticket['costs']))


def changes(before,after):
    result=[]
    def compare(kind,key,old,new,fields):
        for field,label in fields:
            a=(old or {}).get(field);b=(new or {}).get(field)
            same=(Decimal(str(a))==Decimal(str(b))) if field in NUMBERS and a not in (None,'') and b not in (None,'') else (a or None)==(b or None)
            if not same:result.append({'kind':kind,'key':key,'field':label,'before':a,'after':b})
    compare('单据',after['ticket_no'],before,after,[('purchase_currency','采购币种'),('effective_date','成本生效日期'),('note','单据备注')])
    for kind,list_name,key in [('SKU','lines','warehouse_sku'),('费用','costs','cost_type_id')]:
        old={str(r[key]):r for r in (before or {}).get(list_name,[])};new={str(r[key]):r for r in after[list_name]}
        for identity in sorted(old.keys()|new.keys()):
            a,b=old.get(identity),new.get(identity);r=b or a
            label=identity if kind=='SKU' else r['category_name']+' / '+r['cost_type_name']
            if a is None or b is None:result.append({'kind':kind,'key':label,'field':'明细状态','before':'已录入' if a else None,'after':'已录入' if b else None})
            compare(kind,label,a,b,[(k,{'quantity':'数量','unit_price':'单价','amount':'金额','note':'明细备注','currency':'费用币种'}[k]) for k in ((*NUMBERS,'note') if kind=='SKU' else ('currency',*NUMBERS,'note'))])
    return result


def summary(ticket,templates):
    if not ticket:return None
    d=CostStore.decorate(ticket,templates)
    return {k:d[k] for k in ('purchase_currency','purchase_amount','missing_purchase_amounts','fee_totals','line_count','fee_entries','effective_date')}


def plan(costs,db,raw,reference):
    reference=text(reference,'来源凭证',True,1000);raw,rows=parse_csv(raw)
    templates=CostStore.rows(db,'cost_templates');by_name={t['name']:t for t in templates};by_id={t['id']:t for t in templates}
    all_tickets={r['ticket_no']:r for r in db.execute('SELECT * FROM cost_tickets')};groups={}
    for row in rows:
        name=text(row['ticket_no'],'库存单号',True);groups.setdefault(name,[]).append(row)
    if len(groups)>MAX_TICKETS:raise CostError('CSV 超过 200 张库存单，请分批导入')
    receipt_rows=[]
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cost_stock_records'").fetchone():
        receipt_rows=[dict(r) for r in db.execute('SELECT id,version,payload FROM cost_stock_records WHERE deleted_at IS NULL ORDER BY id') if json.loads(r['payload']).get('ticket_id') in {all_tickets[n]['id'] for n in groups if n in all_tickets}]
    details=[];prepared=[];state=[];template_state={}
    for name,items in groups.items():
        first=items[0];line=first['_line']
        try:
            if any(any(r[k]!=first[k] for k in META) for r in items):raise CostError('同一库存单的版本、模板、采购币种、日期和单据备注须一致')
            version=integer(first['version'],'版本');row=all_tickets.get(name);old=CostStore.item(row) if row else None
            if old and old['deleted_at']:raise CostError('库存单已删除，不能重新导入',409)
            t=by_name.get(first['template'].strip())
            if first['template'].strip() and not t:raise CostError('成本模板不存在')
            if t:
                if integer(first['template_version'],'模板版本',False)!=t['version']:raise CostError('成本模板已更新，请重新导出费用类型',409)
                template_state[t['id']]=t
            elif first['template_version'].strip() not in ('','0'):raise CostError('空模板不能填写模板版本')
            if old and old.get('cost_template_id')!=(t['id'] if t else None):raise CostError('导入不能切换已有库存单的成本模板，请单独编辑',409)
            payload={'id':old['id'] if old else None,'version':old['version'] if old else 0,'ticket_no':name,
                     'cost_template_id':t['id'] if t else None,'purchase_currency':currency(first['purchase_currency'],True),
                     'effective_date':first['effective_date'].strip(),'note':first['ticket_note'],
                     'lines':copy.deepcopy((old or {}).get('lines',[])),'costs':copy.deepcopy((old or {}).get('costs',[]))}
            known={str(k['id']):k for k in CostStore.types(t or {}).values()}
            touched={'lines':set(),'costs':set()}
            for item in items:
                line=item['_line'];kind=item['kind'].strip()
                if kind=='SKU':
                    if any(item[k].strip() for k in ('cost_type_id','category','fee','fee_currency')):raise CostError('SKU 行不能填写费用字段')
                    key=text(item['sku'],'仓库 SKU',True);target='lines';identity='warehouse_sku';extra={'warehouse_sku':key}
                elif kind=='费用':
                    if item['sku'].strip():raise CostError('费用行不能填写仓库 SKU')
                    matches=[k for k in known.values() if (not item['cost_type_id'].strip() or str(k['id'])==item['cost_type_id'].strip()) and k['category_name']==item['category'].strip() and k['name']==item['fee'].strip()]
                    if len(matches)!=1:raise CostError('费用类型不存在、名称不符或不唯一，请核对费用类型表')
                    k=matches[0];key=str(k['id']);target='costs';identity='cost_type_id';extra={'cost_type_id':k['id']}
                    existing=next((c for c in payload['costs'] if str(c['cost_type_id'])==key),None)
                    if currency(item['fee_currency'],True)!=(existing or k)['currency']:raise CostError('费用币种与该费用类型的原币种不符')
                    if k['calculation_mode']=='direct_amount' and any(item[v].strip() for v in ('quantity','unit_price')):raise CostError('直接金额费用不能填写数量或单价')
                else:raise CostError('明细类型须为 SKU 或费用')
                if key in touched[target]:raise CostError('同单号的 SKU 或费用类型重复')
                touched[target].add(key);entry=next((e for e in payload[target] if str(e[identity])==key),None)
                replacement={**(entry or {}),**extra,**{k:item[k].strip() for k in NUMBERS},'note':item['note']}
                if entry is None:payload[target].append(replacement)
                else:payload[target][payload[target].index(entry)]=replacement
            line=f"{first['_line']}–{items[-1]['_line']}" if len(items)>1 else first['_line']
            _,after=costs.prepare_ticket(db,payload)
            if old:
                # Retain source IDs and untouched entries from the handover archive.
                after={**json.loads(row['payload']),**after}
                for target,identity in [('lines','warehouse_sku'),('costs','cost_type_id')]:
                    original={str(e[identity]):e for e in old[target]};normalized={str(e[identity]):e for e in after[target]}
                    after[target]=[{**e,**normalized[str(e[identity])]} if str(e[identity]) in touched[target] else e
                                   for e in old[target] if str(e[identity]) not in touched[target] or str(e[identity]) in normalized]
                    after[target].extend(e for key,e in normalized.items() if key not in original)
            action='unchanged' if old and signature(old)==signature(after) else ('updated' if old else 'created')
            if action!='unchanged' and version!=(old['version'] if old else 0):raise CostError('库存单已更新，请重新导出当前版本',409)
            if action=='unchanged':after=json.loads(row['payload'])
            receipts=sum(json.loads(r['payload']).get('ticket_id')==(old or {}).get('id') for r in receipt_rows)
            details.append({'ticket_no':name,'id':(old or {}).get('id'),'version':(old or {}).get('version',0),
                            'action':action,'template':t['name'] if t else '', 'linked_receipts':receipts,
                            'before':summary(old,by_id),'after':summary(after,by_id),'changes':[] if action=='unchanged' else changes(old,after)})
            prepared.append((old,after,action));state.append(dict(row) if row else {'ticket_no':name,'new':True})
        except CostError as exc:raise CostError(f'第 {line} 行，库存单 {name}：{exc}',exc.status)
    file_hash=hashlib.sha256(raw.encode()).hexdigest()
    revision=hashlib.sha256(dumps({'file':file_hash,'reference':reference,'tickets':state,'templates':template_state,'receipts':receipt_rows}).encode()).hexdigest()
    result={'file_sha256':file_hash,'revision':revision,'reference':reference,'summary':{action:sum(d['action']==action for d in details) for action in ('created','updated','unchanged')},'rows':details}
    result['summary'].update(tickets=len(groups),csv_rows=len(rows))
    return raw,prepared,result


def preview(costs,payload):
    with localcontext() as ctx,costs.store.connection() as db:
        ctx.prec=60;db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
        return plan(costs,db,payload.get('csv'),payload.get('reference'))[2]


def apply(costs,payload):
    if not isinstance(payload.get('revision'),str) or not re.fullmatch('[a-f0-9]{64}',payload['revision']):raise CostError('须先预览导入内容')
    with localcontext() as ctx,costs.store.connection() as db:
        ctx.prec=60;db.execute('BEGIN IMMEDIATE');raw,prepared,result=plan(costs,db,payload.get('csv'),payload.get('reference'))
        if payload['revision']!=result['revision']:raise CostError('文件、凭证或相关数据已变化，请重新预览',409)
        if not result['summary']['created'] and not result['summary']['updated']:return {**result,'batch_id':None}
        context={k:result[k] for k in ('file_sha256','revision','reference')}
        for (old,after,action),detail in zip(prepared,result['rows']):
            if action=='unchanged':continue
            detail['id']=costs.write_ticket(db,old,after,'import_update' if old else 'import_create',context)
            detail['new_version']=(old['version'] if old else 0)+1
        batch=db.execute('INSERT INTO cost_ticket_imports(reference,file_sha256,revision_sha256,result_json,csv_text,created_at) VALUES(?,?,?,?,?,?)',
                        (result['reference'],result['file_sha256'],result['revision'],dumps(result),raw,timestamp())).lastrowid
        return {**result,'batch_id':batch}


def history(costs,identity=None):
    with costs.store.connection() as db:
        if identity is not None:
            row=db.execute('SELECT * FROM cost_ticket_imports WHERE id=?',(identity,)).fetchone()
            if not row:raise CostError('导入记录不存在',404)
            return {**dict(row),'result':json.loads(row['result_json'])}
        return {'rows':[{'id':r['id'],'reference':r['reference'],'file_sha256':r['file_sha256'],'created_at':r['created_at'],'summary':json.loads(r['result_json'])['summary']}
                        for r in db.execute('SELECT id,reference,file_sha256,created_at,result_json FROM cost_ticket_imports ORDER BY id DESC LIMIT 20')]}
