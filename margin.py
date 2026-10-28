"""Order cohort gross margin, exact money, explicit evidence and missing inputs."""
from collections import defaultdict
from decimal import Decimal as D, localcontext, ROUND_FLOOR
from procurement import CostError,dumps
from landed import COMPONENTS, covered, fx_convert, round10
import csv,hashlib,io

LABELS={'purchase':'采购成本','head_leg':'头程费用','logistics':'物流运费'}
CATEGORIES={'tax_withheld':'税金代扣','tax_refund':'税金退回','non_order_fee':'非订单交易费','other_income':'其他收入','other_expense':'其他支出'}


def split_money(amount,weights):
    """Largest remainder, preserving original precision and signed source total."""
    with localcontext() as ctx:
        ctx.prec=60
        total=sum(weights.values(),D(0))
        if not weights or total<=0 or any(v<0 for v in weights.values()):return None
        quantum=D(1).scaleb(-max(2,-amount.as_tuple().exponent))
        units=abs(amount)/quantum;floors={k:(units*v/total).to_integral_value(rounding=ROUND_FLOOR) for k,v in weights.items()}
        remainder={k:units*v/total-floors[k] for k,v in weights.items()};left=int(units-sum(floors.values(),D(0)))
        for k in sorted(weights,key=lambda k:(-remainder[k],k))[:left]:floors[k]+=1
        sign=D(-1) if amount<0 else D(1)
        return {k:sign*quantum*v for k,v in floors.items()}


def coverage(records,scope,shop,start,end):
    rows=[{**r,'kind':'coverage'} for r in records if r.get('kind')=='coverage']
    return covered(rows,scope,shop,start,end)


def build_margin(source,costs,inputs,filters=None):
    with localcontext() as ctx:
        ctx.prec=60
        return _build(source,costs,inputs,filters or {})


def _build(source,costs,inputs,filters):
    if source.get('version')!=1 or source.get('timezone')!='Asia/Shanghai':raise CostError('财务收入快照格式或时区无效',502)
    start,end,asof=source['start'],source['end'],source['as_of'];accounting=inputs['accounting'];policy=accounting['policy'];target=policy['report_currency']
    records=source['records'];rates=accounting['records'];base={};issues=[]
    def add_issue(code,message):
        value={'code':code,'message':message}
        if value not in issues:issues.append(value)
    for item in source['items']:
        if not item.get('platform_sku'):
            add_issue('order_line_missing',item['shop_key']+' / '+item['order_no']+' 缺少 SKU 明细');continue
        key=(item['shop_key'],item['order_no'],item['platform_sku'])
        if key not in base:
            base[key]={**item,'order_quantity':D(0),'cost_rows':[],'refund_rows':[],'transactions':[],'reasons':[]}
        row=base[key]
        row['order_quantity']+=D(item.get('quantity') or '0')
        if item.get('order_time_source')=='first_seen':row['reasons'].append('缺少平台订单日期')
        if item.get('source_item_count') and item['source_item_count']>item.get('normalized_line_count',0):row['reasons'].append('订单商品明细不完整')
    by_order=defaultdict(list)
    for key in base:by_order[key[:2]].append(key)
    by_movement={}
    for row in costs.get('rows',[]):
        key=(row['shop_key'],row['order_no'],row['platform_sku'])
        if row['platform']=='temu' and key in base:base[key]['cost_rows'].append(row);by_movement[row['id']]=key
    for row in costs.get('refunds',[]):
        key=by_movement.get(row['movement_id'])
        if key in base:base[key]['refund_rows'].append(row)
    metadata={r['entry_key']:r for r in records if r['kind']=='metadata'}
    unassigned=[];overhead=[];all_cash=[]
    for entry in source['cash']:
        item={**entry};meta=metadata.get(item['entry_key'])
        if meta:
            item.update(original_date=item.get('date'),date=meta.get('date') or item.get('date'),metadata_id=meta['id'],metadata_version=meta['version'],paid_at=meta.get('paid_at') or meta.get('paid_date') or item.get('paid_at'),site=meta.get('site'),product_id=meta.get('product_id'))
        if item['allocation_status']=='non_order':
            if not item.get('date'):
                unassigned.append(item|{'allocation_reason':'已确认非订单款项缺少费用日期'})
            elif start<=item['date']<=end:overhead.append(item)
            continue
        allocations=[]
        if item['allocation_status']=='allocated':
            allocations=[{'key':(item['shop_key'],a['po_no'],a['platform_sku']),'amount':D(a['amount']),'allocation':a} for a in item['allocations']]
        elif item['allocation_status']=='matched' and item.get('sku_status')=='matched' and item.get('order_status')=='matched':allocations=[{'key':(item['shop_key'],item['order_no'],item['platform_sku']),'amount':D(item['amount'])}]
        if not allocations:
            unassigned.append(item);continue
        for a in allocations:
            if a['key'] not in base:continue
            t={**item,'amount':format(a['amount'],'f'),'allocation':a.get('allocation')}
            base[a['key']]['transactions'].append(t);all_cash.append(t)
    allocation=source.get('allocation',{});method=allocation.get('policy',{}).get('method','declared_value')
    bases={(b['shop_key'],b['po_no']):b for b in allocation.get('basis',[])}
    for r in records:
        if r['kind']!='adjustment' or r['date']>asof:continue
        entry={'entry_key':'supplement:'+str(r['id']),'source':'supplement','source_reference':r['source_key'],'shop_key':r['shop_key'],
               'order_no':r.get('order_no',''),'platform_sku':r.get('platform_sku',''),'fee_name':CATEGORIES[r['category']],'category':r['category'],
               'currency':r['currency'],'date':r['date'],'paid_at':r.get('paid_at') or r.get('paid_date'),'reference':r['reference'],'record_id':r['id'],'record_version':r['version']}
        amount=D(r['amount'])*(-1 if r['category'] in ('tax_withheld','non_order_fee','other_expense') else 1);entry['amount']=format(amount,'f')
        if not r.get('order_no'):
            if start<=r['date']<=end:overhead.append(entry)
            continue
        candidates=by_order.get((r['shop_key'],r['order_no']),[])
        if not candidates:continue
        key=(r['shop_key'],r['order_no'],r.get('platform_sku'))
        shares=None
        if r.get('platform_sku') and key in base:shares={key:amount}
        elif not r.get('platform_sku') and len(candidates)==1:shares={candidates[0]:amount}
        elif not r.get('platform_sku') and method!='manual':
            b=bases.get((r['shop_key'],r['order_no']));items={i['platform_sku']:i for i in (b or {}).get('items',[])}
            if all(k[2] in items for k in candidates):
                weights={k:D(str(items[k[2]]['quantity' if method=='quantity' else 'declared_value'])) for k in candidates};shares=split_money(amount,weights)
        if shares is None:unassigned.append(entry|{'allocation_reason':'补录款项未找到完整的订单 SKU 分摊依据'});continue
        for key,value in shares.items():
            t=entry|{'amount':format(value,'f'),'allocation':{'method':method,'policy_version':allocation.get('policy',{}).get('version')}}
            base[key]['transactions'].append(t);all_cash.append(t)
    if unassigned:add_issue('unattributed_cash',f'{len(unassigned)} 笔平台收支或补录款项尚未归因，未分配到订单毛利')
    for issue in costs.get('issues',[]):add_issue('cost_'+issue['code'],issue['message'])
    rows=[]
    for key,row in sorted(base.items()):
        reasons=row['reasons'];shop=key[0];native=defaultdict(lambda:D(0));breakdown=defaultdict(lambda:D(0));cash_total=D(0);net_sales=D(0);cash_ok=True;cash_trace=[]
        for t in row['transactions']:
            native[t['currency']]+=D(t['amount'])
            converted,evidence=fx_convert(D(t['amount']),t['currency'],target,t.get('date') or '',rates)
            if converted is None or not t.get('date'):cash_ok=False;reasons.append('平台款项缺少费用日期或有效汇率')
            else:
                cash_total+=converted;breakdown[t['fee_name']]+=converted
                if t['fee_name'] in ('销售回款','销售冲回'):net_sales+=converted
            cash_trace.append(t|{'converted_amount':format(converted,'f') if converted is not None else None,'fx':evidence})
        statement_complete=coverage(records,'statements',shop,start,asof)
        taxes_complete=coverage(records,'taxes',shop,start,asof)
        if any(t['shop_key']==shop and (not t.get('order_no') or t['order_no']==row['order_no']) for t in unassigned):reasons.append('本订单或店铺存在未归因款项')
        if not statement_complete:reasons.append('财务账单未确认完整')
        if not taxes_complete:reasons.append('税费及非订单费用未确认完整')
        if policy['demand_basis']!='outbound':reasons.append('当前为订单需求测算，尚未采用实际出库')
        if costs.get('status')=='blocked':reasons.append('期初或核算起始日未完成')
        if not inputs['settings'].get('start_date') or inputs['settings']['start_date']>row['order_date']:reasons.append('订单早于核算起始日或缺少起始日')
        outbound_covered=covered(rates,'outbound',shop,start,asof);refund_covered=covered(rates,'refund',shop,start,asof)
        if not outbound_covered:reasons.append('出库明细未确认完整')
        if not refund_covered:reasons.append('退款商品明细未确认完整')
        if not row['cost_rows'] and (row.get('sales_eligible') or cash_total!=0):reasons.append('订单尚无实际出库成本')
        component={c:D(0) for c in COMPONENTS};component_ok={c:True for c in COMPONENTS};cost_ok=True
        for c in row['cost_rows']:
            if c['landed_status']!='complete':cost_ok=False;reasons.extend(c['landed_reasons'] or ['商品成本或汇率不完整'])
            for bucket in COMPONENTS:
                if c['converted'][bucket] is None:cost_ok=False;component_ok[bucket]=False
                else:component[bucket]+=D(c['converted'][bucket])
        refund_quantity=D(0);refund_trace=[]
        for r in row['refund_rows']:
            refund_quantity+=D(r['quantity'])
            if r['status']!='complete':cost_ok=False;reasons.extend(r['reasons'] or ['退款成本待核对'])
            for bucket in COMPONENTS:
                value=r.get('converted',{}).get(bucket)
                if value is None:cost_ok=False;component_ok[bucket]=False;reasons.append('退款原账面成本或汇率不完整')
                else:component[bucket]+=D(value)
            refund_trace.append(r)
        base_cost_known=bool(row['cost_rows']) and costs.get('status')!='blocked' and outbound_covered and policy['demand_basis']=='outbound'
        reasons=list(dict.fromkeys(reasons))
        fully_known=not reasons and cash_ok and cost_ok
        gross=cash_total-sum(component.values(),D(0)) if fully_known else None
        quantity=sum((D(c['quantity']) for c in row['cost_rows']),D(0))
        sites={t.get('site') for t in cash_trace if t.get('site')};products={t.get('product_id') for t in cash_trace if t.get('product_id')}
        sites.update([row['site']] if row.get('site') else []);products.update([row['product_id']] if row.get('product_id') else [])
        rows.append({k:v for k,v in row.items() if k not in ('reasons','transactions','cost_rows','refund_rows','order_quantity')}|
                    {'order_quantity':format(row['order_quantity'],'f'),'sales_quantity':format(quantity,'f') if outbound_covered and policy['demand_basis']=='outbound' and costs.get('status')!='blocked' else None,
                     'refund_quantity':format(refund_quantity,'f') if refund_covered and costs.get('status')!='blocked' else None,'site':' / '.join(sorted(sites)),'product_id':' / '.join(sorted(products)),
                     'currency':target,'native_platform_net':{c:format(v,'f') for c,v in sorted(native.items())},'platform_net':format(cash_total,'f') if cash_ok and statement_complete and taxes_complete else None,'observed_platform_net':format(cash_total,'f') if cash_ok else None,
                     'net_sales_receipts':format(net_sales,'f') if cash_ok and statement_complete else None,'fee_breakdown':{k:format(v,'f') for k,v in sorted(breakdown.items())},
                     'costs':{c:format(v,'f') if component_ok[c] and base_cost_known else None for c,v in component.items()},'gross_profit':format(gross,'f') if gross is not None else None,
                     'gross_margin':format(gross/net_sales,'f') if gross is not None and net_sales>0 else None,'status':'complete' if fully_known else 'review','reasons':reasons,
                     'transactions':cash_trace,'cost_details':row['cost_rows'],'refund_details':refund_trace})
    overhead_total=D(0);overhead_ok=True
    for t in overhead:
        converted,evidence=fx_convert(D(t['amount']),t['currency'],target,t['date'],rates);t.update(converted_amount=format(converted,'f') if converted is not None else None,fx=evidence)
        if converted is None:overhead_ok=False;add_issue('overhead_fx_missing','期间非订单款项缺少有效汇率')
        else:overhead_total+=converted
    observed_overhead=overhead_total
    active_shops={r['shop_key'] for r in rows}|{r['shop_key'] for r in overhead}|{r['shop_key'] for r in unassigned}
    if not active_shops or any(not coverage(records,scope,shop,start,asof) for shop in active_shops for scope in ('statements','taxes')):
        overhead_ok=False;add_issue('non_order_coverage_missing','期间非订单费用未确认完整')
    q=filters.get('q','').lower();selected=[r for r in rows if (not q or q in ' '.join(str(r.get(k) or '') for k in ('order_no','platform_sku','product_name','product_id')).lower()) and (not filters.get('status') or r['status']==filters['status'])]
    known_gross=sum((D(r['gross_profit']) for r in selected if r['gross_profit'] is not None),D(0));all_complete=bool(rows) and all(r['status']=='complete' for r in rows) and not issues and overhead_ok
    native_totals=defaultdict(lambda:D(0))
    for r in selected:
        for code,value in r['native_platform_net'].items():native_totals[code]+=D(value)
    gap_counts=defaultdict(int)
    for r in selected:
        for reason in r['reasons']:gap_counts[reason]+=1
    sku_groups=defaultdict(list)
    for r in selected:sku_groups[(r['shop_key'],r['platform_sku'])].append(r)
    sku_totals=[]
    for (shop,sku),group in sorted(sku_groups.items()):
        known=all(r['status']=='complete' for r in group)
        gross=sum((D(r['gross_profit']) for r in group if r['gross_profit'] is not None),D(0))
        sales=sum((D(r['net_sales_receipts']) for r in group if r['net_sales_receipts'] is not None),D(0))
        total={'shop_key':shop,'platform_sku':sku,'orders':len({r['order_no'] for r in group}),'status':'complete' if known else 'review',
               'gross_profit':format(gross,'f') if known else None,'gross_margin':format(gross/sales,'f') if known and sales>0 else None}
        for field in ('sales_quantity','refund_quantity','platform_net','net_sales_receipts'):
            total[field]=format(sum((D(r[field]) for r in group if r[field] is not None),D(0)),'f') if all(r[field] is not None for r in group) else None
        total['costs']={c:format(sum((D(r['costs'][c]) for r in group if r['costs'][c] is not None),D(0)),'f') if all(r['costs'][c] is not None for r in group) else None for c in COMPONENTS}
        sku_totals.append(total)
    return {'version':1,'gap_counts':dict(sorted(gap_counts.items())),'sku_totals':sku_totals,'basis':'订单日期范围；收入及费用累计至到账截止日；成本按实际出库并计入已确认的退回回冲。毛利率 = 毛利 ÷（销售回款 + 销售冲回），分母不大于零时不计算。非订单费用单列，不强行分配到 SKU。',
            'range':{'start':start,'end':end,'as_of':asof},'currency':target,'rows':selected,'issues':issues,'unassigned':unassigned,'overhead':overhead,
            'status':'complete' if all_complete else 'review','summary':{'orders':len({(r['shop_key'],r['order_no']) for r in rows}),'sku_rows':len(rows),'selected_rows':len(selected),
            'complete_rows':sum(r['status']=='complete' for r in selected),'review_rows':sum(r['status']!='complete' for r in selected),
            'native_platform_net':{c:format(v,'f') for c,v in sorted(native_totals.items())},'complete_rows_gross_profit':format(known_gross,'f'),
            'gross_profit':format(known_gross,'f') if all_complete else None,
            'observed_non_order_net':format(observed_overhead,'f'),'non_order_net':format(overhead_total,'f') if overhead_ok else None,'operating_balance':format(known_gross+overhead_total,'f') if all_complete and not filters.get('q') and not filters.get('status') else None},
            'shops':source.get('shops',[]),'input_sha256':inputs['sha256'],'cash_sha256':source['sha256'],'cost_source_sha256':costs.get('source_sha256'),
            'policy':policy,'allocation_policy':allocation.get('policy'),'cost_settings':inputs['settings']}


def margin_csv(report):
    stream=io.StringIO();writer=csv.writer(stream)
    writer.writerow(['订单日期','店铺','订单号','产品ID','站点','销售SKU','属性','产品名称','订单数量','出库销量','退款件数','报告币种','平台收支净额','采购成本','头程费用','物流运费','毛利润','毛利率','状态','缺失数据','平台款项明细','批次成本明细','退款明细','输入SHA256','财务SHA256'])
    for r in report['rows']:
        values=[r.get(k) for k in ('order_date','shop_key','order_no','product_id','site','platform_sku','variant_name','product_name','order_quantity','sales_quantity','refund_quantity','currency','platform_net')]+[r['costs'][c] for c in COMPONENTS]+[r['gross_profit'],r['gross_margin'],r['status'],'；'.join(r['reasons']),dumps(r['transactions']),dumps(r['cost_details']),dumps(r['refund_details']),report['input_sha256'],report['cash_sha256']]
        writer.writerow(["'"+v if isinstance(v,str) and (v.lstrip().startswith(('=','+','@')) or (v.lstrip().startswith('-') and not __import__('re').fullmatch(r'-[0-9]+(?:\.[0-9]+)?',v))) else v for v in values])
    return ('\ufeff'+stream.getvalue()).encode('utf-8')


def cash_csv(report,part):
    stream=io.StringIO();writer=csv.writer(stream)
    keys=('entry_key','shop_key','source','source_reference','fee_name','order_no','platform_sku','currency','amount','date','paid_at','package_no','tracking_no','allocation_reason')
    writer.writerow(['流水键','店铺','来源','来源业务键','费用名','订单号','销售SKU','币种','原币金额','费用日期','付款时间','包裹','运单','归因原因','完整明细','财务SHA256'])
    for r in report[part]:
        values=[r.get(k,'') for k in keys]+[dumps(r),report['cash_sha256']]
        writer.writerow(["'"+v if isinstance(v,str) and (v.lstrip().startswith(('=','+','@')) or (v.lstrip().startswith('-') and not __import__('re').fullmatch(r'-[0-9]+(?:\.[0-9]+)?',v))) else v for v in values])
    return ('\ufeff'+stream.getvalue()).encode('utf-8')
