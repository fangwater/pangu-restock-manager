#!/usr/bin/env python3
"""Pangu restock: HTTP source snapshots and local SQLite planning, no dependencies."""
import csv
import io
import json
import logging
import math
import os
import re
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo
from reporting import DEFAULT_RULE, compute_report, infer_product
from excel_export import ExportQueue, MIME

ROOT = Path(__file__).resolve().parent
SHOPS = ('home', 'buy', 'shein')
LABELS = {'home': 'HOME', 'buy': 'BUY', 'shein': '希音'}
TZ = ZoneInfo('Asia/Shanghai')
DEFAULT_SETTINGS = dict(DEFAULT_RULE)
LOG = logging.getLogger('pangu-restock')


class APIError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def now():
    return datetime.now(TZ).isoformat(timespec='seconds')


def integer(value, name, minimum=0, maximum=10000000):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise APIError(f'{name}必须为 {minimum}–{maximum} 的整数')
    return value


def allocate(quantity, weights, keys=None):
    """Largest remainder: non-negative integers, conserved total, stable tie break."""
    keys = list(keys or dict.fromkeys([*SHOPS, *weights]))
    w = [Decimal(str(weights.get(k, 0))) for k in keys]
    if any(not x.is_finite() or x < 0 for x in w):
        raise APIError('店铺分配比例无效')
    total = sum(w)
    if not total:
        if quantity:
            raise APIError('该 SKU 没有销量，请手动设置店铺分配比例')
        return dict.fromkeys(keys, 0)
    exact = [Decimal(quantity) * x / total for x in w]
    floors = [int(x) for x in exact]
    remaining = quantity - sum(floors)
    order = sorted(range(len(keys)), key=lambda i: (-(exact[i] - floors[i]), i))
    for i in order[:remaining]:
        floors[i] += 1
    return dict(zip(keys, floors))


class Store:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS snapshots(source TEXT PRIMARY KEY, payload TEXT NOT NULL, fetched_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sources(source TEXT PRIMARY KEY, status TEXT NOT NULL, checked_at TEXT, success_at TEXT, error TEXT NOT NULL DEFAULT '', details TEXT NOT NULL DEFAULT '{}');
            CREATE TABLE IF NOT EXISTS plans(sku TEXT PRIMARY KEY, quantity INTEGER NOT NULL CHECK(quantity>=0), status TEXT NOT NULL,
              mode TEXT NOT NULL, weights TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', basis_days INTEGER NOT NULL DEFAULT 30,
              version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, sku TEXT NOT NULL, action TEXT NOT NULL, before_json TEXT, after_json TEXT, created_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS audit_sku ON audit(sku,id DESC);
            CREATE TABLE IF NOT EXISTS settings(id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS categories(code TEXT PRIMARY KEY,name TEXT NOT NULL,sort_order INTEGER NOT NULL DEFAULT 0,version INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS sku_catalog(sku TEXT PRIMARY KEY,name TEXT NOT NULL DEFAULT '',category TEXT NOT NULL DEFAULT 'uncategorized',
              color TEXT NOT NULL DEFAULT '',spec TEXT NOT NULL DEFAULT '',box_size INTEGER,target_days INTEGER,safety_factor REAL,
              confirmed INTEGER NOT NULL DEFAULT 0,version INTEGER NOT NULL DEFAULT 1,updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS planning_rules(key TEXT PRIMARY KEY,category TEXT NOT NULL DEFAULT '',shop TEXT NOT NULL DEFAULT '',
              target_days INTEGER NOT NULL,safety_factor REAL NOT NULL,box_size INTEGER NOT NULL,include_in_transit INTEGER NOT NULL,
              version INTEGER NOT NULL DEFAULT 1,updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS region_rules(region TEXT PRIMARY KEY,name TEXT NOT NULL,warehouse TEXT NOT NULL,states TEXT NOT NULL,confirmed INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS export_jobs(id TEXT PRIMARY KEY,kind TEXT NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL,
              completed_at TEXT,error TEXT NOT NULL DEFAULT '',filename TEXT NOT NULL DEFAULT '',filters TEXT NOT NULL);
            ''')
            columns = {r['name'] for r in db.execute('PRAGMA table_info(plans)')}
            for name, definition in [('locked', 'INTEGER NOT NULL DEFAULT 1'), ('basis_filter', "TEXT NOT NULL DEFAULT '{}'"), ('basis', "TEXT NOT NULL DEFAULT '{}'"), ('region', "TEXT NOT NULL DEFAULT ''"), ('warehouse', "TEXT NOT NULL DEFAULT ''")]:
                if name not in columns:
                    db.execute(f'ALTER TABLE plans ADD COLUMN {name} {definition}')
            db.execute('INSERT OR IGNORE INTO settings VALUES(1,?)', (json.dumps(DEFAULT_SETTINGS),))
            settings = json.loads(db.execute('SELECT payload FROM settings WHERE id=1').fetchone()[0])
            if 'safety_factor' not in settings:
                factor = 0.2 if settings.get('target_days') == 30 and settings.get('safety_days') == 7 else settings.get('safety_days', 6) / max(1, settings.get('target_days', 30))
                settings = {**DEFAULT_SETTINGS, **settings, 'safety_factor': factor}
                settings.pop('safety_days', None)
                db.execute('UPDATE settings SET payload=? WHERE id=1', (json.dumps(settings),))
            category_columns = {r['name'] for r in db.execute('PRAGMA table_info(categories)')}
            migrated_categories = 'match_rules' not in category_columns
            for name, definition in [('icon', "TEXT NOT NULL DEFAULT 'package'"), ('match_rules', "TEXT NOT NULL DEFAULT '{}'" )]:
                if name not in category_columns:
                    db.execute(f'ALTER TABLE categories ADD COLUMN {name} {definition}')
            presets = json.loads((ROOT / 'config' / 'category-presets.json').read_text())
            if not db.execute('SELECT count(*) FROM categories').fetchone()[0]:
                for c in presets:
                    db.execute('INSERT INTO categories(code,name,sort_order,icon,match_rules) VALUES(?,?,?,?,?)',
                               (c['code'], c['name'], c['sort_order'], c['icon'], json.dumps(c['match_rules'], ensure_ascii=False)))
            elif migrated_categories:
                for c in presets:
                    db.execute('UPDATE categories SET icon=?,match_rules=? WHERE code=?',
                               (c['icon'], json.dumps(c['match_rules'], ensure_ascii=False), c['code']))
            for code, label, warehouse in [('NY', '美东北', 'NY'), ('ATL', '美东南', 'ATL'), ('HOU', '美南', 'HOU'), ('LA', '美西', 'LA')]:
                db.execute('INSERT OR IGNORE INTO region_rules VALUES(?,?,?,?,0)', (code, label, warehouse, '[]'))
            db.execute("UPDATE export_jobs SET status='failed',error='服务重启，请重新导出',completed_at=? WHERE status IN ('queued','running')", (now(),))
        os.chmod(path, 0o600)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def read(self):
        with self.connection() as db:
            db.execute('BEGIN')
            snapshots = {r['source']: {'data': json.loads(r['payload']), 'fetched_at': r['fetched_at']} for r in db.execute('SELECT * FROM snapshots')}
            plans = {r['sku']: self.plan_dict(r) for r in db.execute('SELECT * FROM plans')}
            settings = json.loads(db.execute('SELECT payload FROM settings WHERE id=1').fetchone()[0])
            sources = [{**dict(r), 'details': json.loads(r['details'])} for r in db.execute('SELECT * FROM sources ORDER BY source')]
        return snapshots, plans, settings, sources

    @staticmethod
    def plan_dict(row):
        return {**dict(row), 'weights': json.loads(row['weights']), 'locked': bool(row['locked']),
                'basis_filter': json.loads(row['basis_filter']), 'basis': json.loads(row['basis'])}

    def master_data(self):
        with self.connection() as db:
            categories = [{**dict(r), 'match_rules': json.loads(r['match_rules'])} for r in db.execute('SELECT * FROM categories ORDER BY sort_order,code')]
            catalog = {r['sku']: {**dict(r), 'confirmed': bool(r['confirmed'])} for r in db.execute('SELECT * FROM sku_catalog')}
            rules = [{**dict(r), 'include_in_transit': bool(r['include_in_transit'])} for r in db.execute('SELECT * FROM planning_rules ORDER BY key')]
            regions = [{**dict(r), 'states': json.loads(r['states']), 'confirmed': bool(r['confirmed'])} for r in db.execute('SELECT * FROM region_rules')]
        return catalog, categories, rules, regions

    def seed_catalog(self, products):
        with self.connection() as db:
            for p in products:
                db.execute('''INSERT OR IGNORE INTO sku_catalog(sku,name,category,color,spec,box_size,target_days,safety_factor,confirmed,updated_at)
                VALUES(?,?,?,?,?,NULL,NULL,NULL,0,?)''', (p['sku'], p.get('name', ''), p['category'], p['color'], p['spec'], now()))

    def save_catalog(self, change):
        return self.save_catalogs([change])[0]

    def save_catalogs(self, changes):
        results = []
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            for change in changes:
                old = db.execute('SELECT * FROM sku_catalog WHERE sku=?', (change['sku'],)).fetchone()
                if change['version'] != (old['version'] if old else 0):
                    raise APIError('产品资料已更新，请刷新后重试', 409)
                if not db.execute('SELECT 1 FROM categories WHERE code=?', (change['category'],)).fetchone():
                    raise APIError('类目不存在')
                after = {**change, 'version': change['version'] + 1, 'confirmed': True, 'updated_at': now()}
                db.execute('''INSERT INTO sku_catalog VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(sku) DO UPDATE SET
                    name=excluded.name,category=excluded.category,color=excluded.color,spec=excluded.spec,box_size=excluded.box_size,
                    target_days=excluded.target_days,safety_factor=excluded.safety_factor,confirmed=1,version=excluded.version,updated_at=excluded.updated_at''',
                    (after['sku'], after['name'], after['category'], after['color'], after['spec'], after['box_size'], after['target_days'], after['safety_factor'], 1, after['version'], after['updated_at']))
                db.execute('INSERT INTO audit(sku,action,before_json,after_json,created_at) VALUES(?,?,?,?,?)',
                           (after['sku'], 'catalog', json.dumps(dict(old)) if old else None, json.dumps(after, ensure_ascii=False), now()))
                results.append(after)
        return results

    @staticmethod
    def refresh_classification(db):
        categories = [{**dict(r), 'match_rules': json.loads(r['match_rules'])} for r in db.execute('SELECT * FROM categories')]
        for row in list(db.execute('SELECT sku,name,category,version FROM sku_catalog WHERE confirmed=0')):
            category = infer_product(row['sku'], row['name'], categories=categories)['category']
            if category != row['category']:
                db.execute('UPDATE sku_catalog SET category=?,version=version+1,updated_at=? WHERE sku=?', (category, now(), row['sku']))
                db.execute('INSERT INTO audit(sku,action,before_json,after_json,created_at) VALUES(?,?,?,?,?)',
                           (row['sku'], 'category_rule', json.dumps(dict(row)), json.dumps({'category': category, 'version': row['version'] + 1}), now()))

    def save_category(self, change):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM categories WHERE code=?', (change['code'],)).fetchone()
            if change['version'] != (old['version'] if old else 0):
                raise APIError('类目已更新，请刷新', 409)
            after = {**change, 'version': change['version'] + 1}
            db.execute('''INSERT INTO categories(code,name,sort_order,version,icon,match_rules) VALUES(?,?,?,?,?,?)
                ON CONFLICT(code) DO UPDATE SET name=excluded.name,sort_order=excluded.sort_order,version=excluded.version,
                icon=excluded.icon,match_rules=excluded.match_rules''',
                (after['code'], after['name'], after['sort_order'], after['version'], after['icon'], json.dumps(after['match_rules'], ensure_ascii=False)))
            db.execute('INSERT INTO audit(sku,action,before_json,after_json,created_at) VALUES(?,?,?,?,?)',
                       ('__categories__', 'category', json.dumps(dict(old)) if old else None, json.dumps(after, ensure_ascii=False), now()))
            self.refresh_classification(db)
        return after

    def remove_category(self, code, version):
        if code == 'uncategorized':
            raise APIError('默认待分类类目不能删除')
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM categories WHERE code=?', (code,)).fetchone()
            if not old or old['version'] != version:
                raise APIError('类目已更新，请刷新', 409)
            if db.execute('SELECT count(*) FROM sku_catalog WHERE category=?', (code,)).fetchone()[0]:
                raise APIError('类目下存在 SKU，无法删除', 409)
            if db.execute('SELECT count(*) FROM planning_rules WHERE category=?', (code,)).fetchone()[0]:
                raise APIError('类目存在备货规则，无法删除', 409)
            db.execute('DELETE FROM categories WHERE code=?', (code,))
            db.execute('INSERT INTO audit(sku,action,before_json,created_at) VALUES(?,?,?,?)', ('__categories__', 'remove_category', json.dumps(dict(old)), now()))
        return {'removed': True}

    def save_rule(self, change):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM planning_rules WHERE key=?', (change['key'],)).fetchone()
            if change['version'] != (old['version'] if old else 0):
                raise APIError('备货规则已更新，请刷新后重试', 409)
            after = {**change, 'version': change['version'] + 1, 'updated_at': now()}
            db.execute('''INSERT INTO planning_rules VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET
                target_days=excluded.target_days,safety_factor=excluded.safety_factor,box_size=excluded.box_size,
                include_in_transit=excluded.include_in_transit,version=excluded.version,updated_at=excluded.updated_at''',
                (after['key'], after['category'], after['shop'], after['target_days'], after['safety_factor'], after['box_size'], int(after['include_in_transit']), after['version'], after['updated_at']))
            db.execute('INSERT INTO audit(sku,action,before_json,after_json,created_at) VALUES(?,?,?,?,?)',
                       ('__rules__', 'rule', json.dumps(dict(old)) if old else None, json.dumps(after, ensure_ascii=False), now()))
        return after

    def source(self, source, data=None, error=None, details=None):
        stamp = now()
        with self.connection() as db:
            if data is not None:
                db.execute('INSERT INTO snapshots VALUES(?,?,?) ON CONFLICT(source) DO UPDATE SET payload=excluded.payload,fetched_at=excluded.fetched_at', (source, json.dumps(data, ensure_ascii=False, allow_nan=False), stamp))
            db.execute('''INSERT INTO sources VALUES(?,?,?,?,?,?) ON CONFLICT(source) DO UPDATE SET
               status=excluded.status,checked_at=excluded.checked_at,success_at=CASE WHEN excluded.status='ok' THEN excluded.success_at ELSE sources.success_at END,
               error=excluded.error,details=CASE WHEN excluded.status='ok' THEN excluded.details ELSE sources.details END''',
               (source, 'error' if error else 'ok', stamp, None if error else stamp, error or '', json.dumps(details or {}, ensure_ascii=False)))

    def save_plans(self, changes):
        results = []
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            for change in changes:
                sku, version = change['sku'], change['version']
                old = db.execute('SELECT * FROM plans WHERE sku=?', (sku,)).fetchone()
                before = self.plan_dict(old) if old else None
                if version != (old['version'] if old else 0):
                    raise APIError('备货计划已被其他操作更新，请刷新后重试', 409)
                stamp = now()
                after = {**change, 'version': version + 1, 'updated_at': stamp, 'created_at': old['created_at'] if old else stamp}
                db.execute('''INSERT INTO plans(sku,quantity,status,mode,weights,note,basis_days,version,created_at,updated_at,locked,basis_filter,basis,region,warehouse)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(sku) DO UPDATE SET
                  quantity=excluded.quantity,status=excluded.status,mode=excluded.mode,weights=excluded.weights,note=excluded.note,
                  basis_days=excluded.basis_days,version=excluded.version,updated_at=excluded.updated_at,locked=excluded.locked,
                  basis_filter=excluded.basis_filter,basis=excluded.basis''',
                  (sku, after['quantity'], after['status'], after['mode'], json.dumps(after['weights']), after['note'], after['basis_days'], after['version'], after['created_at'], stamp,
                   int(after.get('locked', True)), json.dumps(after.get('basis_filter', {})), json.dumps(after.get('basis', {})), '', ''))
                db.execute('INSERT INTO audit(sku,action,before_json,after_json,created_at) VALUES(?,?,?,?,?)',
                           (sku, 'update' if old else 'create', json.dumps(before, ensure_ascii=False), json.dumps(after, ensure_ascii=False), stamp))
                results.append(after)
        return results


class Manager:
    def __init__(self, store):
        self.store = store
        self.exports = ExportQueue(store, now)
        self.lock = threading.Lock()
        self.sync_state = {'running': False, 'started_at': None, 'completed_at': None}
        self.state_lock = threading.Lock()
        self.sales_url = os.environ.get('SALES_API_BASE_URL', 'http://127.0.0.1:18100').rstrip('/')
        self.warehouse_url = os.environ.get('WAREHOUSE_API_BASE_URL', 'https://pangutech.online/warehouse-console/api').rstrip('/')
        self.temu_url = os.environ.get('TEMU_API_BASE_URL', 'https://pangutech.online/temu/api').rstrip('/')
        self.shein_url = os.environ.get('SHEIN_API_BASE_URL', 'https://pangutech.online/shein/api').rstrip('/')

    @staticmethod
    def get(url, headers=None):
        request = Request(url, headers={'Accept': 'application/json', 'User-Agent': 'Pangu-Restock/1.0', **(headers or {})})
        with urlopen(request, timeout=40) as response:
            body = response.read(32 * 1024 * 1024 + 1)
        if len(body) > 32 * 1024 * 1024:
            raise ValueError('接口响应超过大小限制')
        envelope = json.loads(body)
        if envelope.get('success') is not True:
            raise ValueError('数据接口返回失败')
        return envelope

    def sales(self):
        data = self.get(self.sales_url + '/api/restock/snapshot?days=730&version=3')['data']
        if data.get('version') != 3 or not isinstance(data.get('points'), list) or data.get('timezone') != 'Asia/Shanghai':
            raise ValueError('销量快照格式或时区不正确')
        for p in data['points']:
            if not isinstance(p.get('shop'), str) or not p['shop'] or not isinstance(p.get('sku'), str) or not p['sku']:
                raise ValueError('销量快照包含无效 SKU/店铺')
            if not isinstance(p.get('sales_sku'), str) or not p['sales_sku']:
                raise ValueError('销量快照缺少销售 SKU')
            datetime.strptime(p['date'], '%Y-%m-%d')
            if any(not isinstance(p.get(k), (int, float)) or not math.isfinite(p[k]) or p[k] < 0 for k in ('units', 'warehouse_units')):
                raise ValueError('销量快照包含无效数量')
            factor = p.get('conversion_factor')
            if not isinstance(factor, (int, float)) or not math.isfinite(factor) or factor <= 0 or not math.isclose(p['units'] * factor, p['warehouse_units'], rel_tol=1e-8, abs_tol=1e-4):
                raise ValueError('销量快照换算数量不一致')
        return data, {'rows': len(data['points']), 'generated_at': data['generated_at'], 'sources': data['sources']}

    def inventory(self):
        records = []
        page = 1
        while page <= 100:
            data = self.get(self.warehouse_url + '/inventory/sku-levels?' + urlencode({'page': page, 'page_size': 100}))['data']
            if not isinstance(data.get('records'), list):
                raise ValueError('库存快照格式不正确')
            records.extend(data['records'])
            if page >= data['pages']:
                break
            page += 1
        else:
            raise ValueError('库存分页超出限制')
        if len(records) != data['total'] or len({r['sku'] for r in records}) != len(records):
            raise ValueError('库存分页不完整或存在重复 SKU，请重试')
        for r in records:
            for field in ('available_amount', 'transport_amount'):
                if not isinstance(r.get(field), (int, float)) or not math.isfinite(r[field]):
                    raise ValueError('库存数量格式不正确')
        return records, {'skus': len(records), 'source_updated_at': max((r.get('last_seen_at', '') for r in records), default=None)}

    def source_meta(self, shop):
        if shop == 'shein':
            url = self.shein_url + '/orders?shop_key=beauty-hangers-home&page=1&page_size=1'
            headers = {}
        else:
            headers = {'X-Temu-Shop': 'panda-homes' if shop == 'home' else 'panda-buy'}
            state = self.get(self.temu_url + '/sales-sync/status', headers=headers).get('data')
            if not isinstance(state, dict):
                raise ValueError('缺少销量同步状态')
            return None, {'sales_sync': state}
        meta = self.get(url, headers=headers).get('meta', {})
        return None, {'sync': meta.get('sync'), 'open_orders': meta.get('total')}

    def sync(self):
        if not self.lock.acquire(blocking=False):
            return False
        with self.state_lock:
            self.sync_state = {'running': True, 'started_at': now(), 'completed_at': None}
        def run():
            try:
                tasks = {'sales': self.sales, 'inventory': self.inventory}
                for shop in SHOPS:
                    tasks[shop] = lambda shop=shop: self.source_meta(shop)
                with ThreadPoolExecutor(max_workers=5) as pool:
                    futures = {pool.submit(fn): name for name, fn in tasks.items()}
                    for future in as_completed(futures):
                        name = futures[future]
                        try:
                            data, details = future.result()
                            self.store.source(name, data=data, details=details)
                            if name in ('sales', 'inventory'):
                                products = data['points'] if name == 'sales' else data
                                unique = {p['sku']: p for p in products}
                                if name == 'sales':
                                    unique.update({p['sales_sku']: {**p, 'sku': p['sales_sku']} for p in products})
                                categories = self.store.master_data()[1]
                                self.store.seed_catalog([infer_product(p['sku'], p.get('name', p.get('product_name', '')), p.get('variant', ''), categories) for p in unique.values()])
                        except Exception as error:
                            # Never log response payloads or credentials.
                            LOG.warning('Source %s failed: %s', name, type(error).__name__)
                            self.store.source(name, error=f'拉取失败（{type(error).__name__}），保留上次成功快照')
            finally:
                with self.state_lock:
                    self.sync_state['running'] = False
                    self.sync_state['completed_at'] = now()
                self.lock.release()
        threading.Thread(target=run, name='source-sync', daemon=True).start()
        return True

    def status(self):
        with self.state_lock:
            return dict(self.sync_state)

    def dashboard(self, query=None):
        query = {'days': query} if isinstance(query, int) else (query or {})
        if not isinstance(query, dict):
            raise APIError('筛选条件格式无效')
        snapshots, plans, settings, sources = self.store.read()
        catalog, categories, rules, regions = self.store.master_data()
        try:
            report = compute_report(snapshots, plans, settings, sources, catalog, categories, rules,
                                    query, datetime.now(TZ).date(), now(), allocate)
        except (ValueError, TypeError) as error:
            raise APIError(str(error))
        report['sync'] = self.status()
        report['regions'] = regions
        for category in report['categories']:
            category['assigned_skus'] = sum(p['category'] == category['code'] for p in catalog.values())
        report['filters']['q'] = str(query.get('q', ''))[:200]
        return report

    def validate_rule(self, payload):
        factor = payload.get('safety_factor')
        if isinstance(factor, bool) or not isinstance(factor, (int, float)) or not math.isfinite(factor) or not 0 <= factor <= 3:
            raise APIError('安全系数必须为 0–3 的数值')
        incoming = payload.get('include_in_transit')
        if not isinstance(incoming, bool):
            raise APIError('在途库存选项无效')
        return {'target_days': integer(payload.get('target_days'), '覆盖天数', 1, 365),
                'safety_factor': factor, 'box_size': integer(payload.get('box_size'), '箱规', 1, 100000),
                'include_in_transit': incoming}

    def validate_category(self, payload):
        code, name = payload.get('code', ''), payload.get('name', '')
        if not isinstance(code, str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,39}', code) or not isinstance(name, str) or not 1 <= len(name.strip()) <= 60:
            raise APIError('类目编码须为英文/数字，名称为 1–60 字')
        icon = payload.get('icon', 'package')
        if icon not in ('hanger', 'hamper', 'tray', 'package', 'box', 'layers'):
            raise APIError('类目图标无效')
        rules = payload.get('match_rules', {})
        if not isinstance(rules, dict):
            raise APIError('分类匹配规则无效')
        cleaned = {}
        for field in ('sku_prefixes', 'keywords'):
            tokens = rules.get(field, [])
            if not isinstance(tokens, list) or len(tokens) > 100 or any(not isinstance(t, str) or not 1 <= len(t.strip()) <= 100 for t in tokens):
                raise APIError('匹配项须为 1–100 字，最多 100 项')
            cleaned[field] = list(dict.fromkeys(t.strip() for t in tokens))
        if code == 'uncategorized' and any(cleaned.values()):
            raise APIError('默认待分类类目不设置匹配规则')
        return {'code': code, 'name': name.strip(), 'icon': icon, 'match_rules': cleaned,
                'sort_order': integer(payload.get('sort_order', 100), '排序', 0, 10000),
                'version': integer(payload.get('version'), '版本号')}

    def validate_catalog(self, payload):
        if not isinstance(payload.get('sku'), str) or not 1 <= len(payload['sku']) <= 200:
            raise APIError('SKU 无效')
        change = {k: payload.get(k, '') for k in ('sku', 'name', 'category', 'color', 'spec')}
        if any(not isinstance(v, str) or len(v) > 500 for v in change.values()):
            raise APIError('产品资料格式无效')
        for k, upper in [('box_size', 100000), ('target_days', 365)]:
            change[k] = None if payload.get(k) in (None, '') else integer(payload[k], k, 1, upper)
        change['safety_factor'] = payload.get('safety_factor')
        if change['safety_factor'] not in (None, ''):
            factor = change['safety_factor']
            if isinstance(factor, bool) or not isinstance(factor, (float, int)) or not math.isfinite(factor) or not 0 <= factor <= 3:
                raise APIError('SKU 安全系数无效')
        else:
            change['safety_factor'] = None
        change['version'] = integer(payload.get('version'), '版本号')
        return change

    def recompute(self, query):
        report = self.dashboard(query)
        changes = []
        for row in report['rows']:
            plan = row['plan']
            if not plan or plan['locked'] or plan['status'] != 'draft' or row['suggested'] is None:
                continue
            change = self.validate_plan({**plan, 'quantity': row['suggested'], 'locked': False}, report)
            changes.append(change)
        return {'items': self.store.save_plans(changes), 'updated': len(changes),
                'skipped_locked': sum(bool(r['plan'] and r['plan']['locked']) for r in report['rows'])}

    def validate_plan(self, payload, dashboard):
        sku = payload.get('sku')
        row = next((r for r in dashboard['rows'] if r['sku'] == sku), None)
        if row is None:
            raise APIError('SKU 不存在', 404)
        quantity = integer(payload.get('quantity'), '备货数量')
        version = integer(payload.get('version'), '版本号')
        mode = payload.get('mode', 'sales')
        status = payload.get('status', 'draft')
        if mode not in ('sales', 'manual') or status not in ('draft', 'confirmed', 'purchasing', 'completed'):
            raise APIError('计划状态或分配方式无效')
        note = payload.get('note', '')
        if not isinstance(note, str) or len(note) > 1000:
            raise APIError('备注最多 1000 字')
        if mode == 'sales':
            weights = row['warehouse_sales']
        else:
            raw = payload.get('weights')
            keys = [s['key'] for s in dashboard['shops']]
            if not isinstance(raw, dict) or set(raw) != set(keys):
                raise APIError('请设置全部店铺的分配比例')
            try:
                weights = {k: Decimal(str(raw[k])) for k in keys}
            except (InvalidOperation, ValueError):
                raise APIError('分配比例无效')
            if any(not v.is_finite() or v < 0 or v > 100 for v in weights.values()) or abs(sum(weights.values()) - 100) > Decimal('0.0001'):
                raise APIError('店铺分配比例之和必须为 100%')
            weights = {k: float(v) for k, v in weights.items()}
        locked = payload.get('locked', True)
        if not isinstance(locked, bool):
            raise APIError('锁定选项无效')
        allocate(quantity, weights, [s['key'] for s in dashboard['shops']])
        return {'locked': locked, 'basis_filter': dashboard['filters'], 'basis': row['basis'], 'sku': sku, 'quantity': quantity, 'version': version, 'status': status, 'mode': mode, 'weights': weights, 'note': note.strip(), 'basis_days': dashboard['range']['days']}


class Handler(BaseHTTPRequestHandler):
    manager = None
    server_version = 'PanguRestock'

    def log_message(self, fmt, *args):
        LOG.info('%s %s', self.command, urlparse(self.path).path)

    def reply(self, data, status=200):
        body = json.dumps({'success': status < 400, 'data': data} if status < 400 else {'success': False, 'error': data}, ensure_ascii=False, allow_nan=False).encode()
        self.send_bytes(body, 'application/json; charset=utf-8', status)

    def send_bytes(self, body, mime, status=200, filename=None):
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'SAMEORIGIN')
        self.send_header('Referrer-Policy', 'same-origin')
        if filename:
            self.send_header('Content-Disposition', f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(body)

    def query(self):
        return {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}

    def body(self):
        # Custom header requires a same-origin JS request; no permissive CORS.
        if self.headers.get('X-Pangu-Request') != 'restock':
            raise APIError('请从备货管理页面提交', 403)
        origin = self.headers.get('Origin')
        if origin and urlparse(origin).netloc != self.headers.get('Host'):
            raise APIError('请求来源不匹配', 403)
        if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
            raise APIError('请提交 JSON 数据')
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 1024 * 1024:
                raise APIError('请求大小无效', 413)
            data = json.loads(self.rfile.read(size))
            if not isinstance(data, dict):
                raise ValueError()
            return data
        except (ValueError, UnicodeDecodeError):
            raise APIError('JSON 数据无效')

    def do_GET(self):
        try:
            path = urlparse(self.path).path
            if path == '/api/health':
                self.reply({'service': 'pangu-restock-manager', 'time': now()})
            elif path == '/api/dashboard':
                self.reply(self.manager.dashboard(self.query()))
            elif path == '/api/catalog':
                report = self.manager.dashboard()
                self.reply({'items': [r['product'] for r in report['rows']], 'categories': report['categories']})
            elif path == '/api/status':
                snapshots, _, _, sources = self.manager.store.read()
                self.reply({'sync': self.manager.status(), 'sources': sources, 'snapshots': {k: v['fetched_at'] for k, v in snapshots.items()}})
            elif path == '/api/sku':
                sku = self.query().get('sku', '')
                dashboard = self.manager.dashboard(self.query())
                row = next((r for r in dashboard['rows'] if r['sku'] == sku), None)
                if not row:
                    raise APIError('SKU 不存在', 404)
                snapshots, _, _, _ = self.manager.store.read()
                points = [p for p in snapshots.get('sales', {}).get('data', {}).get('points', [])
                          if (p['sku'] == sku or p.get('sales_sku') == sku)
                          and dashboard['range']['start'] <= p['date'] <= dashboard['range']['end']
                          and p['shop'] in dashboard['filters']['shops']
                          and p.get('status', 'pending') in dashboard['filters']['statuses']]
                with self.manager.store.connection() as db:
                    history = [{**dict(r), 'after': json.loads(r['after_json'])} for r in db.execute('SELECT id,action,after_json,created_at FROM audit WHERE sku=? ORDER BY id DESC LIMIT 20', (sku,))]
                self.reply({'row': row, 'points': points, 'history': history})
            elif path.startswith('/api/exports/'):
                parts = path.strip('/').split('/')
                if len(parts) == 4 and parts[3] == 'download':
                    result = self.manager.exports.download(parts[2])
                    if result is None:
                        raise APIError('导出尚未完成或已过期', 404)
                    self.send_bytes(result[0], MIME, filename=result[1])
                elif len(parts) == 3:
                    job = self.manager.exports.get(parts[2])
                    if not job:
                        raise APIError('导出任务不存在', 404)
                    self.reply(job)
                else:
                    raise APIError('接口不存在', 404)
            elif path == '/api/export.csv':
                self.export()
            elif path.startswith('/api/'):
                raise APIError('接口不存在', 404)
            else:
                files = {'/': ('index.html', 'text/html'), '/app.js': ('app.js', 'application/javascript'), '/app.css': ('app.css', 'text/css'), '/vendor/lucide.min.js': ('vendor/lucide.min.js', 'application/javascript'), '/vendor/echarts.min.js': ('vendor/echarts.min.js', 'application/javascript'), '/favicon.svg': ('favicon.svg', 'image/svg+xml')}
                if path not in files:
                    raise APIError('页面不存在', 404)
                filename, mime = files[path]
                self.send_bytes((ROOT / 'web' / filename).read_bytes(), mime + '; charset=utf-8')
        except APIError as e:
            self.reply(str(e), e.status)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            LOG.exception('GET failed')
            self.reply('服务暂时不可用，请稍后重试', 500)

    def do_POST(self):
        self.mutate()

    def do_PUT(self):
        self.mutate()

    def mutate(self):
        try:
            payload = self.body()
            path = urlparse(self.path).path
            if path == '/api/sync' and self.command == 'POST':
                started = self.manager.sync()
                self.reply({'started': started, 'sync': self.manager.status()}, 202)
            elif path == '/api/plans' and self.command == 'PUT':
                dashboard = self.manager.dashboard(payload.get('filters') or {'days': integer(payload.get('basis_days', 30), '统计天数', 1, 366)})
                change = self.manager.validate_plan(payload, dashboard)
                self.reply(self.manager.store.save_plans([change])[0])
            elif path == '/api/plans/bulk' and self.command == 'POST':
                dashboard = self.manager.dashboard(payload.get('filters') or {'days': integer(payload.get('basis_days', 30), '统计天数', 1, 366)})
                entries = payload.get('items')
                if not isinstance(entries, list) or not 1 <= len(entries) <= 500 or any(not isinstance(e, dict) for e in entries):
                    raise APIError('批量提交需要 1–500 个 SKU')
                if len({e.get('sku') for e in entries}) != len(entries):
                    raise APIError('批量提交中包含重复 SKU')
                changes = []
                by_sku = {r['sku']: r for r in dashboard['rows']}
                for entry in entries:
                    row = by_sku.get(entry.get('sku'))
                    if not row or row['suggested'] is None:
                        raise APIError('销量和库存完整同步后才能采用备货建议')
                    if row['plan'] is not None:
                        raise APIError('批量采用建议仅适用于尚未创建计划的 SKU', 409)
                    change = self.manager.validate_plan({'sku': row['sku'], 'quantity': row['suggested'], 'version': entry.get('version'), 'status': 'draft', 'mode': 'sales'}, dashboard)
                    changes.append(change)
                self.reply({'items': self.manager.store.save_plans(changes)})
            elif path == '/api/plans/recompute' and self.command == 'POST':
                self.reply(self.manager.recompute(payload.get('filters', {})))
            elif path == '/api/exports' and self.command == 'POST':
                report = self.manager.dashboard(payload.get('filters', {}))
                try:
                    job = self.manager.exports.create(report, payload.get('kind', 'report'))
                except ValueError as error:
                    raise APIError(str(error))
                self.reply(job, 202)
            elif path == '/api/catalog' and self.command == 'PUT':
                self.reply(self.manager.store.save_catalog(self.manager.validate_catalog(payload)))
            elif path == '/api/catalog/classify' and self.command == 'POST':
                entries = payload.get('items')
                if not isinstance(entries, list) or not 1 <= len(entries) <= 500 or any(not isinstance(e, dict) for e in entries):
                    raise APIError('批量分类需要 1–500 个 SKU')
                if len({e.get('sku') for e in entries}) != len(entries):
                    raise APIError('包含重复 SKU')
                by_sku = {r['sku']: r for r in self.manager.dashboard()['rows']}
                changes = []
                for e in entries:
                    row = by_sku.get(e.get('sku'))
                    if not row:
                        raise APIError('SKU 不存在', 404)
                    changes.append(self.manager.validate_catalog({**row['product'], 'category': payload.get('category'), 'version': e.get('version')}))
                self.reply({'items': self.manager.store.save_catalogs(changes)})
            elif path == '/api/categories' and self.command == 'PUT':
                self.reply(self.manager.store.save_category(self.manager.validate_category(payload)))
            elif path == '/api/categories/remove' and self.command == 'POST':
                self.reply(self.manager.store.remove_category(payload.get('code'), integer(payload.get('version'), '版本号')))
            elif path == '/api/rules' and self.command == 'PUT':
                category, shop = payload.get('category', ''), payload.get('shop', '')
                report = self.manager.dashboard()
                if category and category not in [c['code'] for c in report['categories']]:
                    raise APIError('类目不存在')
                if shop and shop not in [s['key'] for s in report['shops']]:
                    raise APIError('店铺不存在')
                if not category and not shop:
                    raise APIError('请选择类目或店铺；全局规则请在全局设置中修改')
                key = json.dumps([category, shop], separators=(',', ':'))
                change = {**self.manager.validate_rule(payload), 'category': category, 'shop': shop, 'key': key, 'version': integer(payload.get('version'), '版本号')}
                self.reply(self.manager.store.save_rule(change))
            elif path == '/api/rules/remove' and self.command == 'POST':
                with self.manager.store.connection() as db:
                    db.execute('BEGIN IMMEDIATE')
                    old = db.execute('SELECT * FROM planning_rules WHERE key=?', (payload.get('key'),)).fetchone()
                    if not old or old['version'] != payload.get('version'):
                        raise APIError('规则已更新，请刷新', 409)
                    db.execute('DELETE FROM planning_rules WHERE key=?', (old['key'],))
                    db.execute('INSERT INTO audit(sku,action,before_json,created_at) VALUES(?,?,?,?)', ('__rules__', 'remove_rule', json.dumps(dict(old)), now()))
                self.reply({'removed': True})
            elif path == '/api/settings' and self.command == 'PUT':
                settings = self.manager.validate_rule(payload)
                with self.manager.store.connection() as db:
                    before = db.execute('SELECT payload FROM settings WHERE id=1').fetchone()[0]
                    db.execute('UPDATE settings SET payload=? WHERE id=1', (json.dumps(settings),))
                    db.execute('INSERT INTO audit(sku,action,before_json,after_json,created_at) VALUES(?,?,?,?,?)', ('__settings__', 'settings', before, json.dumps(settings), now()))
                self.reply(settings)
            else:
                raise APIError('接口不存在', 404)
        except APIError as e:
            self.reply(str(e), e.status)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            LOG.exception('Mutation failed')
            self.reply('保存失败，请稍后重试', 500)

    def export(self):
        report = self.manager.dashboard(self.query())
        shops = report['filters']['shops']
        stream = io.StringIO()
        writer = csv.writer(stream)
        writer.writerow(['SKU', '类目', '颜色', '规格', '开始日期', '结束日期', *shops, '订单销量', '类目内占比%', '全局占比%', '仓库换算需求', '可售库存', '在途库存', '建议备货', '箱规', '计划数量', '锁定', '备注'])
        q = report['filters'].get('q', '').lower()
        for row in report['rows']:
            if q and q not in ' '.join(str(row[k]) for k in ('sku', 'name', 'color', 'spec')).lower():
                continue
            plan = row['plan'] or {}
            cells = [row['sku'], row['category_name'], row['color'], row['spec'], report['range']['start'], report['range']['end'],
                     *[row['sales'][k] for k in shops], row['total'], row['category_share'], row['global_share'], row['warehouse_total'],
                     row['available'], row['in_transit'], row['suggested'], row['basis']['box_size'], plan.get('quantity'), plan.get('locked'), plan.get('note', '')]
            writer.writerow([("'" + cell) if isinstance(cell, str) and cell.lstrip().startswith(('=', '+', '-', '@')) else cell for cell in cells])
        self.send_bytes(('\ufeff' + stream.getvalue()).encode('utf-8'), 'text/csv; charset=utf-8', filename=f'pangu-restock-{report["range"]["end"]}.csv')


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    store = Store(os.environ.get('RESTOCK_DATABASE', str(ROOT / 'data' / 'restock.sqlite3')))
    Handler.manager = Manager(store)
    interval = max(60, int(os.environ.get('SYNC_INTERVAL_SECONDS', '300')))
    def scheduler():
        while True:
            Handler.manager.sync()
            time.sleep(interval)
    threading.Thread(target=scheduler, name='scheduler', daemon=True).start()
    server = ThreadingHTTPServer((os.environ.get('LISTEN_HOST', '127.0.0.1'), int(os.environ.get('LISTEN_PORT', '18110'))), Handler)
    LOG.info('Pangu restock listening on %s:%s', *server.server_address)
    server.serve_forever()


if __name__ == '__main__':
    main()
