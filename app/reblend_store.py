"""复配补料库存与确认单持久化。

并发模型：确认、入库、重置全部在单条 BEGIN IMMEDIATE 事务内完成，
SQLite 写锁把并发请求串行化；库存不足直接回滚并 422，绝不超领。
重试幂等：同一确认键已存在时原样返回既有单据，不二次扣料。
"""
import csv
import io
import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from app.errors import DomainError
from app.numeric import number, fmt
from app.store import connection

SEED_INVENTORY = Path(__file__).resolve().parent.parent / 'sample_data' / 'reblend_inventory.csv'
SEED_ORDERS = Path(__file__).resolve().parent.parent / 'sample_data' / 'reblend_orders.csv'
VALID_PRIORITY = ('急单', '普通')


def _now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def parse_inventory(text):
    """解析库存CSV，同材料多行合并为各元素化验。仅支持干基确定值。"""
    reader = csv.DictReader(io.StringIO(text.lstrip('﻿'), newline=''))
    required = {'material_id', 'material_name', 'element', 'assay_pct', 'on_hand_kg'}
    if reader.fieldnames is None or set(reader.fieldnames) != required:
        raise DomainError('HEADER', '库存CSV字段缺失或重复')
    materials = {}
    order = []
    for cells in reader:
        mid = (cells.get('material_id') or '').strip()
        element = (cells.get('element') or '').strip()
        if not mid:
            raise DomainError('MISSING', '材料编号为空')
        if element not in ('Cu', 'Zn'):
            raise DomainError('ELEMENT', '库存材料仅支持Cu/Zn化验')
        assay = number(cells['assay_pct'])
        if not 0 <= assay <= 100:
            raise DomainError('RANGE', '材料化验含量超出0~100%')
        on_hand = number(cells['on_hand_kg'])
        if on_hand < 0:
            raise DomainError('RANGE', '库存不能为负')
        if mid not in materials:
            materials[mid] = {'material_id': mid,
                              'material_name': (cells.get('material_name') or '').strip() or mid,
                              'assays': {}, 'on_hand_kg': on_hand}
            order.append(mid)
        if materials[mid]['on_hand_kg'] != on_hand:
            raise DomainError('CONFLICT', f'材料{mid}库存数量行不一致')
        if element in materials[mid]['assays']:
            raise DomainError('CONFLICT', f'材料{mid}元素{element}化验重复')
        materials[mid]['assays'][element] = (assay, assay)
    if not materials:
        raise DomainError('EMPTY', '库存CSV没有材料记录')
    return [materials[mid] for mid in order]


def parse_orders(text):
    reader = csv.DictReader(io.StringIO(text.lstrip('﻿'), newline=''))
    required = {'batch', 'due_date', 'priority'}
    if reader.fieldnames is None or set(reader.fieldnames) != required:
        raise DomainError('HEADER', '交期CSV字段缺失或重复')
    orders = {}
    for cells in reader:
        batch = (cells.get('batch') or '').strip()
        due = (cells.get('due_date') or '').strip() or None
        priority = (cells.get('priority') or '').strip()
        if not batch:
            raise DomainError('MISSING', '炉次号为空')
        if priority not in VALID_PRIORITY:
            raise DomainError('DECISION', '优先级必须为急单或普通')
        if due:
            try:
                datetime.strptime(due, '%Y-%m-%d')
            except ValueError:
                raise DomainError('RANGE', '交期必须为YYYY-MM-DD')
        if batch in orders:
            raise DomainError('CONFLICT', f'炉次{batch}交期重复登记')
        orders[batch] = {'due_date': due, 'priority': priority}
    return orders


def ensure_tables(db):
    db.execute('CREATE TABLE IF NOT EXISTS reblend_inventory('
               'material_id TEXT PRIMARY KEY, material_name TEXT NOT NULL, '
               'on_hand_kg TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS reblend_assays('
               'material_id TEXT NOT NULL, element TEXT NOT NULL, assay_pct TEXT NOT NULL, '
               'PRIMARY KEY(material_id, element))')
    db.execute('CREATE TABLE IF NOT EXISTS reblend_orders('
               'batch TEXT PRIMARY KEY, due_date TEXT, priority TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS reblend_ledger('
               'id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, '
               'material_id TEXT NOT NULL, change_kg TEXT NOT NULL, balance_kg TEXT NOT NULL, '
               'ref_type TEXT NOT NULL, ref_key TEXT NOT NULL, note TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS reblend_confirmations('
               'confirm_key TEXT PRIMARY KEY, run_id TEXT NOT NULL, plan_hash TEXT NOT NULL, '
               'created_at TEXT NOT NULL, payload TEXT NOT NULL)')


def _seed_if_empty(db):
    if db.execute('SELECT COUNT(*) FROM reblend_inventory').fetchone()[0] == 0:
        for m in parse_inventory(SEED_INVENTORY.read_text(encoding='utf-8-sig')):
            db.execute('INSERT INTO reblend_inventory VALUES(?,?,?)',
                       (m['material_id'], m['material_name'], str(m['on_hand_kg'])))
            for element, (low, high) in m['assays'].items():
                db.execute('INSERT INTO reblend_assays VALUES(?,?,?)',
                           (m['material_id'], element, str(low)))
            db.execute('INSERT INTO reblend_ledger(created_at,material_id,change_kg,balance_kg,'
                       'ref_type,ref_key,note) VALUES(?,?,?,?,?,?,?)',
                       (_now(), m['material_id'], str(m['on_hand_kg']), str(m['on_hand_kg']),
                        'SEED', 'seed', '初始补料库存'))
    if db.execute('SELECT COUNT(*) FROM reblend_orders').fetchone()[0] == 0:
        for batch, order in parse_orders(SEED_ORDERS.read_text(encoding='utf-8-sig')).items():
            db.execute('INSERT INTO reblend_orders VALUES(?,?,?)',
                       (batch, order['due_date'], order['priority']))


def load_materials():
    with connection() as db:
        ensure_tables(db)
        _seed_if_empty(db)
        rows = db.execute('SELECT material_id,material_name,on_hand_kg FROM reblend_inventory '
                          'ORDER BY material_id').fetchall()
        materials = []
        for row in rows:
            assays = {r['element']: (number(r['assay_pct']), number(r['assay_pct']))
                      for r in db.execute('SELECT element,assay_pct FROM reblend_assays '
                                          'WHERE material_id=?', (row['material_id'],))}
            materials.append({'material_id': row['material_id'],
                              'material_name': row['material_name'],
                              'on_hand_kg': row['on_hand_kg'], 'assays': assays})
        return materials


def load_orders():
    with connection() as db:
        ensure_tables(db)
        _seed_if_empty(db)
        return {r['batch']: {'due_date': r['due_date'], 'priority': r['priority']}
                for r in db.execute('SELECT batch,due_date,priority FROM reblend_orders')}


def inventory_view():
    with connection() as db:
        ensure_tables(db)
        _seed_if_empty(db)
        materials = db.execute('SELECT material_id,material_name,on_hand_kg FROM '
                               'reblend_inventory ORDER BY material_id').fetchall()
        ledger = db.execute('SELECT created_at,material_id,change_kg,balance_kg,ref_type,'
                            'ref_key,note FROM reblend_ledger ORDER BY id').fetchall()
    return {'materials': [dict(r) for r in materials], 'ledger': [dict(r) for r in ledger],
            'orders': _orders_json()}


def _orders_json():
    with connection() as db:
        ensure_tables(db)
        _seed_if_empty(db)
        return [dict(r) for r in db.execute(
            'SELECT batch,due_date,priority FROM reblend_orders ORDER BY due_date,batch')]


def reset_inventory(text, order_text=None):
    """用上传CSV重置库存与交期（带审计重置单）。"""
    materials = parse_inventory(text)
    orders = parse_orders(order_text) if order_text else None
    db = connection()
    try:
        db.execute('BEGIN IMMEDIATE')
        ensure_tables(db)
        db.execute('DELETE FROM reblend_inventory')
        db.execute('DELETE FROM reblend_assays')
        for m in materials:
            db.execute('INSERT INTO reblend_inventory VALUES(?,?,?)',
                       (m['material_id'], m['material_name'], str(m['on_hand_kg'])))
            for element, (low, high) in m['assays'].items():
                db.execute('INSERT INTO reblend_assays VALUES(?,?,?)',
                           (m['material_id'], element, str(low)))
            db.execute('INSERT INTO reblend_ledger(created_at,material_id,change_kg,balance_kg,'
                       'ref_type,ref_key,note) VALUES(?,?,?,?,?,?,?)',
                       (_now(), m['material_id'], str(m['on_hand_kg']), str(m['on_hand_kg']),
                        'RESET', 'reset', '库存表重置'))
        if orders is not None:
            db.execute('DELETE FROM reblend_orders')
            for batch, order in orders.items():
                db.execute('INSERT INTO reblend_orders VALUES(?,?,?)',
                           (batch, order['due_date'], order['priority']))
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    return inventory_view()


def replenish(material_id, change_kg, note):
    """补料入库（正数）；退库用负数但不得使库存为负。"""
    if change_kg == 0:
        raise DomainError('RANGE', '变动量不能为零')
    db = connection()
    try:
        db.execute('BEGIN IMMEDIATE')
        ensure_tables(db)
        _seed_if_empty(db)
        row = db.execute('SELECT on_hand_kg FROM reblend_inventory WHERE material_id=?',
                         (material_id,)).fetchone()
        if row is None:
            raise DomainError('NOT_FOUND', '材料不存在', 404)
        balance = number(row['on_hand_kg']) + change_kg
        if balance < 0:
            raise DomainError('STOCK', '退库后库存为负，拒绝操作')
        db.execute('UPDATE reblend_inventory SET on_hand_kg=? WHERE material_id=?',
                   (str(balance), material_id))
        db.execute('INSERT INTO reblend_ledger(created_at,material_id,change_kg,balance_kg,'
                   'ref_type,ref_key,note) VALUES(?,?,?,?,?,?,?)',
                   (_now(), material_id, str(change_kg), str(balance),
                    'ADJUST', material_id, note or '手工调整'))
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    return inventory_view()


def confirm_plan(confirm_key, run_id, plan_hash, allocations, plan_rows):
    """按方案扣减共用库存并落确认单。

    allocations: [[炉次, 材料, 剂量kg], ...]，由服务端重算产生，客户端只传确认键。
    """
    db = connection()
    try:
        db.execute('BEGIN IMMEDIATE')
        ensure_tables(db)
        _seed_if_empty(db)
        # 幂等：同确认键重试直接回放原单据，不重复扣料
        existing = db.execute('SELECT payload FROM reblend_confirmations WHERE confirm_key=?',
                              (confirm_key,)).fetchone()
        if existing is not None:
            db.commit()
            return {**json.loads(existing['payload']), 'idempotent': True}
        balances = {r['material_id']: number(r['on_hand_kg'])
                    for r in db.execute('SELECT material_id,on_hand_kg FROM reblend_inventory')}
        for batch, material_id, dose_text in allocations:
            if material_id not in balances:
                raise DomainError('NOT_FOUND', f'补料材料{material_id}不存在', 404)
            dose = number(dose_text)
            if balances[material_id] < dose:
                raise DomainError(
                    'STOCK',
                    f'材料{material_id}库存不足：需{fmt(dose)}kg，余{fmt(balances[material_id])}kg')
        for batch, material_id, dose_text in allocations:
            dose = number(dose_text)
            balances[material_id] -= dose
            db.execute('UPDATE reblend_inventory SET on_hand_kg=? WHERE material_id=?',
                       (str(balances[material_id]), material_id))
            db.execute('INSERT INTO reblend_ledger(created_at,material_id,change_kg,balance_kg,'
                       'ref_type,ref_key,note) VALUES(?,?,?,?,?,?,?)',
                       (_now(), material_id, str(-dose), str(balances[material_id]),
                        'CONFIRM', confirm_key, f'复配确认 炉次{batch}'))
        payload = {'confirm_key': confirm_key, 'run_id': run_id, 'plan_hash': plan_hash,
                   'created_at': _now(), 'allocations': allocations,
                   'rows': plan_rows, 'idempotent': False}
        db.execute('INSERT INTO reblend_confirmations(confirm_key,run_id,plan_hash,created_at,'
                   'payload) VALUES(?,?,?,?,?)',
                   (confirm_key, run_id, plan_hash, payload['created_at'],
                    json.dumps(payload, ensure_ascii=False)))
        db.commit()
        return payload
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def list_confirmations():
    with connection() as db:
        ensure_tables(db)
        rows = db.execute('SELECT confirm_key,run_id,plan_hash,created_at,payload '
                          'FROM reblend_confirmations ORDER BY created_at').fetchall()
    return [{'confirm_key': r['confirm_key'], 'run_id': r['run_id'],
             'plan_hash': r['plan_hash'], 'created_at': r['created_at'],
             'allocations': json.loads(r['payload'])['allocations']} for r in rows]


def get_confirmation(confirm_key):
    with connection() as db:
        ensure_tables(db)
        row = db.execute('SELECT payload FROM reblend_confirmations WHERE confirm_key=?',
                         (confirm_key,)).fetchone()
    if row is None:
        raise DomainError('NOT_FOUND', '确认单不存在', 404)
    return json.loads(row['payload'])
