"""Exact FIFO replay with landed components and identified inventory returns."""
from collections import defaultdict, deque
from datetime import datetime
from decimal import Decimal, localcontext
import hashlib
from procurement import CostError, dumps, number
from landed import COMPONENTS, allocate_ticket, covered, day, fx_convert, round10

D=Decimal

def strings(values):return {k:format(v,'f') for k,v in sorted(values.items())}
def empty():return {c:{} for c in COMPONENTS}
def complete():return {c:True for c in COMPONENTS}


def report(inputs,source,filters):
    with localcontext() as ctx:
        ctx.prec=60
        return replay(inputs,source,filters)


def replay(inputs,source,filters):
    settings=inputs['settings'];start=settings.get('start_date')
    end=day(filters.get('end') or datetime.now().date().isoformat(),'截止日期')
    display_start=day(filters.get('start') or start or end,'开始日期')
    if display_start>end or (start and display_start<start):raise CostError('查询日期须位于核算起始日及其之后，且开始不晚于截止')
    accounting=inputs.get('accounting',{'records':[],'policy':{'report_currency':'CNY','demand_basis':'order','fx_date':'sale','version':1}})
    policy=accounting['policy'];actual=policy['demand_basis']=='outbound';records=accounting['records']
    issues=[]
    def issue(code,message,record=None):issues.append({'code':code,'message':message,'record_id':record})
    if not start:issue('start_missing','未设置核算起始日')
    if not settings.get('opening_confirmed'):issue('opening_unconfirmed','期初库存尚未确认完整（无期初库存也须确认）')
    base={'method':'fifo','range':{'start':display_start,'end':end,'replay_start':start},'input_sha256':inputs['sha256'],'settings_version':settings['version'],
          'accounting_policy':policy,'issues':issues,'basis':('按实际出库日期核算，退回库存须关联原出库明细' if actual else '按订单时间测算，含待发货；不作为实际出库记账')+'；同日先入库、再出库、最后退回；成本汇率采用'+('出库/订单日期' if policy['fx_date']=='sale' else '原入库日期')}
    if not start or not settings.get('opening_confirmed'):
        return base|{'status':'blocked','rows':[],'refunds':[],'remaining':[],'summary':{},'shops':[],'warehouses':[]}
    if actual:
        movements=[r for r in records if r['kind']=='movement' and start<=r['movement_date']<=end]
        demands=[{'id':r['id'],'order_id':r['id'],'platform':r['platform'],'shop_key':r['shop_key'],'order_no':r['order_no'],'line_no':r['source_key'],
                  'occurred_at':r['movement_date']+'T12:00:00+08:00','date':r['movement_date'],'platform_sku':r['platform_sku'],
                  'warehouse_sku':r['warehouse_sku'],'warehouse_code':r['warehouse_code'],'quantity':r['quantity'],'warehouse_quantity':r['warehouse_quantity'],
                  'conversion_factor':format(D(r['warehouse_quantity'])/D(r['quantity']),'f'),'movement_version':r['version'],'source_reference':r['reference']} for r in movements]
        source={'version':1,'timezone':'Asia/Shanghai','start':start,'end':end,'demands':demands,'sha256':hashlib.sha256(dumps(movements).encode()).hexdigest()}
    if not isinstance(source,dict) or source.get('version')!=1 or source.get('timezone')!='Asia/Shanghai' or source.get('start')!=start or source.get('end')!=end or not isinstance(source.get('demands'),list):raise CostError('订单数量快照的日期、版本或时区无效',502)
    base.update(source_sha256=source.get('sha256'),source_updated_at=source.get('source_updated_at'))
    source_incomplete=bool(source.get('missing_line_orders') or source.get('unknown_order_time_orders'))
    if source.get('missing_line_orders'):issue('missing_order_lines',f"{source['missing_line_orders']} 个订单缺少完整 SKU 明细")
    if source.get('unknown_order_time_orders'):issue('unknown_order_time',f"{source['unknown_order_time_orders']} 个订单缺少业务时间")
    tickets={t['id']:t for t in inputs['tickets']};allocated={tid:allocate_ticket(t,accounting) for tid,t in tickets.items()}
    lots=[];uncertain=set();receipt_cumulative=defaultdict(lambda:D(0))
    for r in sorted(inputs['records'],key=lambda r:(r.get('received_date') or '9999',r['id'])):
        when=r.get('received_date');ticket=tickets.get(r.get('ticket_id')) if r['kind']=='receipt' else None
        problem=None
        if not when:problem='缺少入库日期'
        elif r['kind']=='opening' and when!=start:problem='期初日期与核算起始日不一致'
        elif r['kind']=='receipt' and when<start:problem='起始日前入库须计入期初库存'
        if r['kind']=='receipt' and (not ticket or ticket['version']!=r.get('ticket_version')):problem='采购库存单已删除或版本变化，须重新核对入库记录'
        if problem:
            issue('invalid_stock_record',r['record_no']+'：'+problem,r['id']);uncertain.update((r['warehouse_code'],l['warehouse_sku']) for l in r['lines']);continue
        if when>end:continue
        purchase_lines={l['warehouse_sku']:l for l in (ticket or {}).get('lines',[])}
        for index,l in enumerate(r['lines']):
            qty=D(l['quantity']);sku=l['warehouse_sku'];components=empty();flags=complete();trace=[];reasons=[]
            if ticket:
                p=purchase_lines.get(sku);key=(ticket['id'],sku);previous=receipt_cumulative[key];receipt_cumulative[key]+=qty
                if not p or D(p['quantity'])<=0 or receipt_cumulative[key]>D(p['quantity']):
                    issue('receipt_quantity',r['record_no']+'：SKU '+sku+' 入库数量与采购单不一致',r['id']);uncertain.add((r['warehouse_code'],sku));continue
                a=allocated[ticket['id']][sku];flags=a['complete'].copy();trace=a['trace'];reasons=a['issues']
                for c in COMPONENTS:
                    for code,amount in a['amounts'][c].items():components[c][code]=round10(amount*receipt_cumulative[key]/D(p['quantity']))-round10(amount*previous/D(p['quantity']))
            else:
                for c,field in [('purchase','amount'),('head_leg','head_leg_amount'),('logistics','logistics_amount')]:
                    amount=l.get(field);code=r['currency'] if c=='purchase' else l.get(c+'_currency') or r['currency']
                    flags[c]=amount is not None and bool(code) and D(amount)>=0
                    if flags[c]:components[c][code]=D(amount)
            if not flags['purchase']:issue('purchase_cost_missing',r['record_no']+'：SKU '+sku+' 缺少采购金额或币种',r['id'])
            lots.append({'record_id':r['id'],'record_version':r['version'],'record_no':r['record_no'],'ticket_id':ticket['id'] if ticket else None,
                         'ticket_version':ticket['version'] if ticket else None,'ticket_no':ticket['ticket_no'] if ticket else None,'date':when,'kind':r['kind'],
                         'warehouse_code':r['warehouse_code'],'warehouse_sku':sku,'quantity':qty,'used':D(0),'components':components,'complete':flags,
                         'used_components':empty(),'trace':trace,'issues':reasons,'index':index})
    lots.sort(key=lambda l:(l['date'],l['kind']!='opening',l['record_id'],l['index']))
    demands=source['demands'];events=[(d['date'],1,d['occurred_at'],d['id'],d) for d in demands]
    if actual:events.extend((r['returned_date'],2,'',r['id'],r) for r in records if r['kind']=='refund' and start<=r['returned_date']<=end)
    queues=defaultdict(deque);position=0;rows=[];refund_rows=[];returned_lots=[];tainted=set();seen=set();by_movement={};refund_used=defaultdict(lambda:D(0))
    unknown_warehouse_skus={d['warehouse_sku'] for d in demands if not d['warehouse_code']}
    for when,event_type,_,identity,demand in sorted(events,key=lambda e:e[:4]):
        while position<len(lots) and lots[position]['date']<=when:
            l=lots[position];queues[(l['warehouse_code'],l['warehouse_sku'])].append(l);position+=1
        if event_type==2:
            original=by_movement.get(demand['movement_id']);reason=[];known=empty();flags=complete();allocations=[];book={c:D(0) for c in COMPONENTS}
            if not original or original.get('movement_version')!=demand['movement_version']:
                reason.append('原出库未进入核算范围或版本变化');flags={c:False for c in COMPONENTS}
            else:
                available=D(demand['warehouse_quantity']);reverse=COMPONENTS if demand['cost_action']=='all' else ('purchase',) if demand['cost_action']=='purchase' else ()
                if original['status']!='complete':reason.append('原出库批次归属待核对');flags={c:False for c in COMPONENTS}
                for index,a in enumerate(original['allocations']):
                    key=(original['id'],index);previous=refund_used[key];qty=min(available,D(a['quantity'])-previous)
                    if qty<=0:continue
                    refund_used[key]+=qty;available-=qty;component=empty();cf=complete()
                    for c in COMPONENTS:
                        if c not in reverse:continue
                        cf[c]=a['complete'][c];flags[c]&=cf[c]
                        if a['converted'][c] is None:flags[c]=False
                        else:book[c]-=round10(D(a['converted'][c])*refund_used[key]/D(a['quantity']))-round10(D(a['converted'][c])*previous/D(a['quantity']))
                        for code,value in a['components'][c].items():
                            value=D(value);amount=round10(value*refund_used[key]/D(a['quantity']))-round10(value*previous/D(a['quantity']))
                            component[c][code]=amount;known[c][code]=known[c].get(code,D(0))-amount
                    allocations.append({'original_record_id':a['record_id'],'quantity':format(qty,'f'),'components':{c:strings(component[c]) for c in COMPONENTS},'original_fx':a['fx']})
                    if demand['restocked']:
                        l={'record_id':-demand['id'],'record_version':demand['version'],'record_no':'退回 '+demand['source_key'],'ticket_id':a['ticket_id'],'ticket_version':a['ticket_version'],'ticket_no':a['ticket_no'],
                           'date':when,'kind':'return','warehouse_code':original['warehouse_code'],'warehouse_sku':original['warehouse_sku'],'quantity':qty,'used':D(0),
                           'components':component,'complete':cf,'used_components':empty(),'trace':[],'issues':[],'index':index}
                        if reason:uncertain.add((original['warehouse_code'],original['warehouse_sku']))
                        returned_lots.append(l);queues[(l['warehouse_code'],l['warehouse_sku'])].append(l)
                if available>0:reason.append('原出库缺少可核对的批次数量');flags={c:False for c in COMPONENTS}
            rr={**demand,'event':'refund','order_no':original['order_no'] if original else '', 'platform_sku':original['platform_sku'] if original else '',
                'platform':original['platform'] if original else '', 'warehouse_code':original['warehouse_code'] if original else '', 'warehouse_sku':original['warehouse_sku'] if original else '', 'components':{c:strings(known[c]) for c in COMPONENTS},'complete':flags,'reasons':reason,'allocations':allocations,
                'status':'review' if reason or not all(flags.values()) else 'complete','converted':{c:format(book[c],'f') if flags[c] else None for c in COMPONENTS}}
            if rr['status']=='review':issue('refund_review',demand['source_key']+'：退款成本待核对',demand['id'])
            refund_rows.append(rr);continue
        if identity in seen:raise CostError('订单快照包含重复明细，计算已中止',502)
        seen.add(identity);day(when,'订单日期');qty=number(demand.get('warehouse_quantity'),'仓库需求数量',True)
        if not qty or D(qty)<=0 or not start<=when<=end:raise CostError('订单快照包含无效数量或日期',502)
        remaining=D(qty);key=(demand['warehouse_code'],demand['warehouse_sku']);reasons=[];landed_reasons=[];allocations=[];known=empty();flags=complete()
        if not all(key):reasons.append('缺少仓库或仓库 SKU')
        if key[0] and key[1] in unknown_warehouse_skus:reasons.append('该 SKU 存在仓库不明的订单需求')
        if key in uncertain:reasons.append('该仓库 SKU 存在未核对的入库记录')
        if key in tainted:reasons.append('此前订单库存不足，后续批次归属待核对')
        queue=queues[key]
        while remaining>0 and queue:
            l=queue[0];take=min(remaining,l['quantity']-l['used']);l['used']+=take;remaining-=take;component=empty();fx={};converted={}
            for c in COMPONENTS:
                flags[c]&=l['complete'][c]
                for code,total in l['components'][c].items():
                    cumulative=round10(total*l['used']/l['quantity']);amount=cumulative-l['used_components'][c].get(code,D(0));l['used_components'][c][code]=cumulative
                    component[c][code]=amount;known[c][code]=known[c].get(code,D(0))+amount
                converted[c],fx[c]=convert_components(component[c],policy['report_currency'],when if policy['fx_date']=='sale' else l['date'],records)
            allocations.append({k:l[k] for k in ('record_id','record_version','record_no','ticket_id','ticket_version','ticket_no','date')}|
                               {'quantity':format(take,'f'),'currency':next(iter(component['purchase']),None),'amount':format(sum(component['purchase'].values(),D(0)),'f') if l['complete']['purchase'] else None,
                                'components':{c:strings(component[c]) for c in COMPONENTS},'complete':l['complete'].copy(),'fee_trace':l['trace'],'fx':fx,'converted':converted})
            landed_reasons.extend(l['issues'])
            if l['used']==l['quantity']:queue.popleft()
        if remaining>0:reasons.append('入库库存不足');tainted.add(key)
        if source_incomplete:reasons.append('订单源数据不完整，批次归属待核对')
        if not flags['purchase']:reasons.append('批次采购成本缺失')
        for c,label in [('head_leg','头程'),('logistics','物流')]:
            if not flags[c]:landed_reasons.append(label+'费用未确认完整或缺少分摊依据')
        if reasons:flags={c:False for c in COMPONENTS}
        converted={}
        for c in COMPONENTS:
            values=[a['converted'][c] for a in allocations]
            converted[c]=format(sum((D(v) for v in values),D(0)),'f') if flags[c] and all(v is not None for v in values) else None
            if flags[c] and any(v is None for v in values):landed_reasons.append('缺少有效汇率')
        landed_reasons=list(dict.fromkeys(reasons+landed_reasons))
        row={**demand,'event':'outbound' if actual else 'order','status':'review' if reasons else 'complete','reasons':list(dict.fromkeys(reasons)),
             'shortage_quantity':format(remaining,'f'),'known_amounts':strings(known['purchase']),'cost_amounts':None if reasons else strings(known['purchase']),
             'components':{c:strings(known[c]) for c in COMPONENTS},'complete':flags,'converted':converted,'landed_status':'complete' if not landed_reasons and all(v is not None for v in converted.values()) else 'review',
             'landed_reasons':landed_reasons,'allocations':allocations}
        rows.append(row);by_movement[identity]=row
    def matches(r):return (not filters.get('shop') or r['shop_key']==filters['shop']) and (not filters.get('warehouse') or r['warehouse_code']==filters['warehouse']) and (not filters.get('q') or filters['q'].lower() in ' '.join(str(r.get(k) or '') for k in ('warehouse_sku','platform_sku','order_no')).lower())
    selected=[r for r in rows if display_start<=r['date']<=end and matches(r) and (not filters.get('status') or r['status']==filters['status']) and (not filters.get('landed_status') or r['landed_status']==filters['landed_status'])]
    totals=defaultdict(lambda:D(0));landed_totals={c:D(0) for c in COMPONENTS}
    for r in selected:
        for code,value in (r['cost_amounts'] or {}).items():totals[code]+=D(value)
        if r['landed_status']=='complete':
            for c in COMPONENTS:landed_totals[c]+=D(r['converted'][c])
    remaining=[]
    for l in lots+returned_lots:
        if l['quantity']<=l['used']:continue
        component={c:{code:total-l['used_components'][c].get(code,D(0)) for code,total in l['components'][c].items()} for c in COMPONENTS}
        remaining.append({k:l[k] for k in ('record_id','record_no','date','warehouse_code','warehouse_sku')}|{'currency':next(iter(component['purchase']),None),'quantity':format(l['quantity']-l['used'],'f'),
                         'amount':format(sum(component['purchase'].values(),D(0)),'f') if l['complete']['purchase'] else None,'components':{c:strings(component[c]) for c in COMPONENTS},'complete':l['complete']})
    if actual:
        for shop in sorted({r['shop_key'] for r in rows}):
            for scope,label in [('outbound','出库'),('refund','退款')]:
                if not covered(records,scope,shop,start,end):issue('coverage_missing',shop+'：'+label+'明细未确认完整')
    selected_refunds=[r for r in refund_rows if display_start<=r['returned_date']<=end and matches(r)]
    return base|{'status':'review' if issues or any(r['landed_status']!='complete' for r in rows) else 'complete','rows':selected,'refunds':selected_refunds,'remaining':remaining,
                 'shops':sorted({r['shop_key'] for r in rows}),'warehouses':sorted({r['warehouse_code'] for r in rows}|{l['warehouse_code'] for l in lots}),
                 'summary':{'replayed_lines':len(rows),'selected_lines':len(selected),'complete_lines':sum(r['status']=='complete' for r in selected),'review_lines':sum(r['status']!='complete' for r in selected),
                            'complete_cost_totals':strings(totals),'landed_complete_lines':sum(r['landed_status']=='complete' for r in selected),'landed_complete_totals':strings(landed_totals),'refund_lines':len(refund_rows)}}


def convert_components(native,target,when,records):
    values=[];trace=[]
    for code,amount in native.items():
        converted,evidence=fx_convert(amount,code,target,when,records)
        if converted is None:return None,trace
        values.append(converted);trace.append(evidence)
    return format(sum(values,D(0)),'f'),trace
