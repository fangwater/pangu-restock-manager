"""Sales reports and purchase decisions over ERP daily aggregates."""
import math
import re
from datetime import date, datetime, timedelta
from decimal import Decimal

SHOP_LABELS = {'home': 'Panda Homes', 'buy': 'Panda Buy', 'shein': 'Shein'}
DEFAULT_RULE = {'target_days': 30, 'safety_factor': 0.2, 'box_size': 1, 'include_in_transit': False}
STATUSES = {'pending': '待发货', 'shipped': '已发货', 'completed': '已完成'}


def percentage_parts(values):
    """Keep displayed hundredths summing to 100% despite rounding."""
    total = sum(values.values())
    if total <= 0:
        return dict.fromkeys(values, 0)
    exact = {k: Decimal(str(v)) * 10000 / Decimal(str(total)) for k, v in values.items()}
    result = {k: int(v) for k, v in exact.items()}
    for key in sorted(values, key=lambda k: (-(exact[k] - result[k]), str(k)))[:10000 - sum(result.values())]:
        result[key] += 1
    return {k: v / 100 for k, v in result.items()}


def date_window(query, today):
    preset = query.get('preset', '')
    if query.get('start') or query.get('end'):
        start = date.fromisoformat(query.get('start', ''))
        end = date.fromisoformat(query.get('end', ''))
    elif preset in ('current_quarter', 'previous_quarter'):
        quarter = date(today.year, ((today.month - 1) // 3) * 3 + 1, 1)
        if preset == 'current_quarter':
            start, end = quarter, today
        else:
            end = quarter - timedelta(days=1)
            start = date(end.year, ((end.month - 1) // 3) * 3 + 1, 1)
    else:
        days = int(query.get('days', preset.removeprefix('last') if preset.startswith('last') else 30))
        if not 1 <= days <= 366:
            raise ValueError('统计天数必须为 1–366 天')
        end, start = today, today - timedelta(days=days - 1)
    if start > end:
        raise ValueError('开始日期不能晚于结束日期')
    if end > today:
        raise ValueError('结束日期不能晚于今天')
    if (end - start).days >= 366:
        raise ValueError('单次查询最多支持 366 天')
    return start, end


def infer_product(sku, name='', variant='', categories=None):
    text = (sku + ' ' + name).lower()
    category = 'uncategorized'
    ordered = sorted(categories or [], key=lambda c: (c.get('sort_order', 0), c['code']))
    matched = next((c for c in ordered if c['code'] != 'uncategorized' and any(
        sku.lower().startswith(token.lower()) for token in c.get('match_rules', {}).get('sku_prefixes', []))), None)
    if matched is None:
        # Specific product phrases must outrank incidental generic words in titles.
        # For example, "Laundry Basket ... High Hanger" is a laundry basket.
        candidates = [(len(token), position, c) for position, c in enumerate(ordered)
                      if c['code'] != 'uncategorized'
                      for token in c.get('match_rules', {}).get('keywords', [])
                      if token and token.lower() in text]
        if candidates:
            matched = min(candidates, key=lambda item: (-item[0], item[1]))[2]
    if matched:
        category = matched['code']
    # Structured warehouse SKU tokens are more reliable than generic product titles.
    colors = ('Navy Blue', 'Ivory', 'Beige', 'Black', 'White', 'Pink', 'Gray', 'Grey', 'Red', 'Blue', 'Green', 'Brown', 'Purple')
    tokens = sku.split('-')
    color = next((token for token in tokens if any(c.lower() in token.lower() for c in colors)), '')
    if not color:
        color = next((c for c in colors if re.search(r'\b' + re.escape(c) + r'\b', variant, re.I)), '')
    specs = [token for token in tokens if re.search(r'\d+(?:\.\d+)?\s*(?:pcs?|pack|cm|inch|in|l)\b', token, re.I)]
    spec = ' / '.join(specs) or variant[:120]
    return {'sku': sku, 'name': name, 'category': category, 'color': color, 'spec': spec,
            'box_size': None, 'target_days': None, 'safety_factor': None, 'confirmed': False, 'version': 0}


def resolve_rule(settings, rules, product, shops):
    result = {**DEFAULT_RULE, **{k: settings[k] for k in DEFAULT_RULE if k in settings}}
    applied = ['全局规则']
    shop = shops[0] if len(shops) == 1 else ''
    matches = [r for r in rules if (not r['category'] or r['category'] == product['category'])
               and (not r['shop'] or r['shop'] == shop)]
    for rule in sorted(matches, key=lambda r: (bool(r['category']) * 2 + bool(r['shop']), r['key'])):
        result.update({k: rule[k] for k in DEFAULT_RULE if k in rule})
        applied.append(rule.get('label', rule['key']))
    for key in ('target_days', 'safety_factor'):
        if product.get(key) is not None:
            result[key] = product[key]
            applied.append('SKU 覆盖' if key == 'target_days' else 'SKU 安全系数')
    result['box_size'] = product.get('box_size') or result['box_size']
    result['applied'] = applied
    return result


def compute_report(snapshots, plans, settings, sources, catalog, categories, rules, query, today, timestamp, allocate):
    start, end = date_window(query, today)
    days = (end - start).days + 1
    previous = start - timedelta(days=days)
    sales = snapshots.get('sales', {}).get('data', {})
    inventory = snapshots.get('inventory', {}).get('data', [])
    snapshot_version = sales.get('version', 1 if any('platform_units' in p for p in sales.get('points', [])) else 2)
    shop_info = {k: {'key': k, 'name': v} for k, v in SHOP_LABELS.items()}
    for s in sales.get('shops', []):
        shop_info[s['key']] = s
    for p in sales.get('points', []):
        shop_info.setdefault(p['shop'], {'key': p['shop'], 'name': p['shop']})
    all_shops = list(shop_info)
    raw_shops = query.get('shops', query.get('shop', 'all'))
    selected_shops = raw_shops if isinstance(raw_shops, list) else raw_shops.split(',')
    selected_shops = all_shops if 'all' in selected_shops or not selected_shops else list(dict.fromkeys(selected_shops))
    if any(s not in shop_info for s in selected_shops):
        raise ValueError('所选店铺不存在')
    raw_status = query.get('statuses', 'pending,shipped,completed')
    selected_statuses = raw_status if isinstance(raw_status, list) else raw_status.split(',')
    if not selected_statuses or any(s not in STATUSES for s in selected_statuses):
        raise ValueError('请选择有效的订单状态')
    category = query.get('category', 'all')
    category_names = {r['code']: r['name'] for r in categories}
    if category != 'all' and category not in category_names:
        raise ValueError('所选类目不存在')
    items = {}
    series = {str(start + timedelta(days=i)): dict.fromkeys(all_shops, 0) for i in range(days)}

    def item(sku, name='', variant=''):
        if sku not in items:
            product = {**infer_product(sku, name, variant, categories), **catalog.get(sku, {})}
            items[sku] = {'sku': sku, 'name': product['name'] or name, 'product': product,
                          'category': product['category'], 'color': product['color'], 'spec': product['spec'],
                          'sales': dict.fromkeys(all_shops, 0), 'warehouse_sales': dict.fromkeys(all_shops, 0),
                          'prior_sales': dict.fromkeys(all_shops, 0), 'available': None, 'in_transit': None,
                          'warehouses': {}, 'regions': {}, 'has_inventory': False}
        return items[sku]

    for p in sales.get('points', []):
        if p['shop'] not in selected_shops or p.get('status', 'pending') not in selected_statuses:
            continue
        when = date.fromisoformat(p['date'])
        if previous <= when <= end:
            row = item(p['sku'], p.get('name', ''), p.get('variant', ''))
            if when >= start:
                order_units = p.get('platform_units', p['units']) if snapshot_version < 2 else p['units']
                row['sales'][p['shop']] += order_units
                row['warehouse_sales'][p['shop']] += p.get('warehouse_units', p['units'])
                if category == 'all' or row['category'] == category:
                    series[str(when)][p['shop']] += order_units
                if p.get('region'):
                    row['regions'][p['region']] = row['regions'].get(p['region'], 0) + p['units']
            else:
                row['prior_sales'][p['shop']] += p.get('platform_units', p['units']) if snapshot_version < 2 else p['units']
    for r in inventory:
        row = item(r['sku'], r.get('product_name', ''))
        row.update(available=r['available_amount'], in_transit=r['transport_amount'],
                   warehouses=r.get('warehouses', {}), has_inventory=True)
    for sku, product in catalog.items():
        item(sku, product.get('name', ''))
    for sku in plans:
        item(sku)
    sales_ready, inventory_ready = 'sales' in snapshots, 'inventory' in snapshots
    current = datetime.fromisoformat(timestamp)
    sales_stale = not sales_ready or (current - datetime.fromisoformat(snapshots['sales']['fetched_at'])).total_seconds() > 1800
    inventory_stale = not inventory_ready or (current - datetime.fromisoformat(snapshots['inventory']['fetched_at'])).total_seconds() > 1800
    history_complete = bool(sales.get('start') and sales['start'] <= str(previous))
    window_complete = bool(sales.get('start') and sales['start'] <= str(start) and sales.get('end', str(today)) >= str(end))
    category_totals = dict.fromkeys(category_names, 0)
    for row in items.values():
        row['total'] = sum(row['sales'].values())
        row['warehouse_total'] = sum(row['warehouse_sales'].values())
        row['prior_total'] = sum(row['prior_sales'].values())
        category_totals.setdefault(row['category'], 0)
        category_totals[row['category']] += row['total']
    global_total = sum(category_totals.values())
    category_shares = percentage_parts(category_totals)
    sku_global_shares = percentage_parts({sku: r['total'] for sku, r in items.items()})
    sku_category_shares, ranks = {}, {}
    for code in category_totals:
        group = sorted((r for r in items.values() if r['category'] == code), key=lambda r: (-r['total'], r['sku']))
        sku_category_shares.update(percentage_parts({r['sku']: r['total'] for r in group}))
        ranks.update({r['sku']: i + 1 for i, r in enumerate(group)})
    filters = {'start': str(start), 'end': str(end), 'days': days, 'shops': selected_shops,
               'statuses': selected_statuses, 'category': category}
    for row in items.values():
        total, prior = row['total'], row['prior_total']
        row['shares'] = percentage_parts(row['sales'])
        row['daily_rate'] = total / days
        row['warehouse_daily_rate'] = row['warehouse_total'] / days
        row['category_share'] = sku_category_shares[row['sku']]
        row['global_share'] = sku_global_shares[row['sku']]
        row['rank'] = ranks[row['sku']]
        row['category_name'] = category_names.get(row['category'], '待分类')
        row['growth'] = (total / prior - 1) * 100 if prior and history_complete else None
        rule = resolve_rule(settings, rules, row['product'], selected_shops)
        stock = row['available']
        row['coverage'] = stock / row['warehouse_daily_rate'] if stock is not None and row['warehouse_daily_rate'] else None
        row['stock_status'] = 'unknown' if stock is None else ('out' if total and stock <= 0 else 'low' if row['coverage'] is not None and row['coverage'] < rule['target_days'] * rule['safety_factor'] else 'normal' if total else 'inactive')
        incoming = row['in_transit'] if rule['include_in_transit'] and row['in_transit'] is not None else 0
        target = row['warehouse_daily_rate'] * rule['target_days'] * (1 + rule['safety_factor'])
        raw = max(0, target - (stock or 0) - incoming)
        row['suggested'] = None
        row['suggestion_reason'] = ''
        if not window_complete:
            row['suggestion_reason'] = '所选日期超出已同步的销量覆盖范围'
        elif sales_stale or inventory_stale:
            row['suggestion_reason'] = '销量或库存尚未同步，或快照超过 30 分钟'
        elif not row['has_inventory']:
            row['suggestion_reason'] = '缺少对应仓库 SKU 库存记录，请核对产品映射'
        else:
            row['suggested'] = math.ceil(round(raw / rule['box_size'], 10)) * rule['box_size']
        row['basis'] = {**rule, 'window_days': days, 'order_sales': total, 'warehouse_sales': row['warehouse_total'],
                        'order_daily_rate': row['daily_rate'], 'daily_rate': row['warehouse_daily_rate'],
                        'conversion_ratio': row['warehouse_total'] / total if total else 1,
                        'available': stock, 'in_transit_deducted': incoming, 'target_quantity': target,
                        'raw_quantity': raw if stock is not None else None, 'suggested': row['suggested'],
                        'quantity_unit': '仓库 SKU 单位', 'calculated_at': timestamp, 'filters': filters}
        row['plan'] = plans.get(row['sku'])
        row['allocation'] = allocate(row['plan']['quantity'], row['plan']['weights'], all_shops) if row['plan'] else dict.fromkeys(all_shops, 0)
    rows = sorted((r for r in items.values() if category == 'all' or r['category'] == category), key=lambda r: (-r['total'], r['sku']))
    totals = {k: sum(r['sales'][k] for r in rows) for k in all_shops}
    planned = [r for r in rows if r['plan'] and r['plan']['status'] != 'completed']
    warnings = []
    if sales_stale:
        warnings.append('销量尚未同步或超过 30 分钟，备货建议暂停计算')
    if inventory_stale:
        warnings.append('库存尚未同步或超过 30 分钟，备货建议暂停计算')
    if not window_complete and sales_ready:
        warnings.append(f'当前销量快照覆盖 {sales.get("start")} 至 {sales.get("end")}，所选日期不完整')
    fallback = sum(s.get('first_seen_orders', 0) for s in sales.get('sources', []))
    if fallback:
        warnings.append('部分订单缺少平台下单时间，沿用中台首次采集日期；历史回补可能影响日期归属')
    missing = sum(s.get('orders_without_lines', 0) for s in sales.get('sources', []))
    if missing:
        warnings.append(f'{missing} 笔订单缺少 SKU 明细，未计入销量')
    return {'range': {'start': str(start), 'end': str(end), 'days': days}, 'filters': filters, 'rows': rows,
            'shops': list(shop_info.values()), 'categories': [{**next((c for c in categories if c['code'] == code), {'code': code, 'name': category_names.get(code, code)}),
            'total': qty, 'share': category_shares[code], 'sku_count': sum(r['category'] == code and r['total'] > 0 for r in items.values())}
            for code, qty in category_totals.items()], 'statuses': STATUSES,
            'summary': {'total': sum(totals.values()), 'global_total': global_total, 'shops': totals, 'shares': percentage_parts(totals),
                        'active_skus': sum(r['total'] > 0 for r in rows), 'sku_count': len(rows),
                        'urgent_skus': sum(r['stock_status'] in ('out', 'low') for r in rows),
                        'suggested': sum(r['suggested'] or 0 for r in rows) if window_complete and not sales_stale and not inventory_stale else None,
                        'planned': sum(r['plan']['quantity'] for r in planned), 'locked': sum(bool(r['plan'].get('locked')) for r in planned),
                        'planned_shops': {k: sum(r['allocation'][k] for r in planned) for k in all_shops},
                        'unclassified': sum(r['category'] == 'uncategorized' and r['total'] > 0 for r in items.values())},
            'series': [{'date': k, **v} for k, v in series.items()], 'sources': sources, 'settings': settings, 'rules': rules,
            'warnings': warnings, 'quality': {'inferred_mappings': sales.get('inferred_mappings', 0),
            'unmapped_mappings': sales.get('unmapped_mappings', 0), 'orders_without_lines': missing},
            'ready': sales_ready, 'inventory_ready': inventory_ready, 'history_complete': history_complete,
            'window_complete': window_complete, 'snapshots': {k: v['fetched_at'] for k, v in snapshots.items()},
            'generated_at': timestamp, 'unit_basis': '订单 quantity；采购量使用仓库 SKU 单位'}
