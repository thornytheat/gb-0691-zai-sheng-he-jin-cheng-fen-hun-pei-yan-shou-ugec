import threading
from decimal import Decimal
from pathlib import Path

import pytest

from app.errors import DomainError

DEMO_CSV = Path(__file__).resolve().parent.parent / 'sample_data' / 'reblend_demo.csv'
INV_CSV = Path(__file__).resolve().parent.parent / 'sample_data' / 'reblend_inventory.csv'


@pytest.fixture
def run_id(client):
    demo = DEMO_CSV.read_text(encoding='utf-8-sig')
    return client.post('/api/run', json={'csv': demo}).get_json()['manifest']['run_id']


def _plan(client, run_id):
    return client.post('/api/reblend/plan', json={'run_id': run_id}).get_json()


def test_plan_endpoint_shape(client, run_id):
    plan = _plan(client, run_id)
    assert plan['name'] == '复配补料方案' and plan['plan_hash'] and plan['confirm_key']
    assert len(plan['rows']) == 5 and len(plan['inventory']) == 3
    assert all('分配原因' in r and '补后区间' in r for r in plan['rows'])
    assert all('候选材料' in r for r in plan['rows'])


def test_confirm_deducts_shared_inventory_and_recomputes(client, run_id):
    plan = _plan(client, run_id)
    resp = client.post('/api/reblend/confirm',
                       json={'run_id': run_id, 'confirm_key': plan['confirm_key']})
    assert resp.status_code == 200 and resp.get_json()['idempotent'] is False
    view = client.get('/api/reblend/inventory').get_json()
    balance = {m['material_id']: Decimal(m['on_hand_kg']) for m in view['materials']}
    # 逐材料核对：原库存 - 方案占用 == 当前余量
    for item in plan['inventory']:
        expect = Decimal(item['现有库存_kg']) - Decimal(item['本方案占用_kg'])
        assert balance[item['材料']] == expect
    # 补后验收已在方案行内重算为通过
    for row in resp.get_json()['plan']['rows']:
        if row['分配状态'] == '已分配':
            assert all('通过' in part for part in row['补后验收'].split('／'))
    # 台账留下扣料证据
    ledger = view['ledger']
    assert any(l['ref_type'] == 'CONFIRM' and l['ref_key'] == plan['confirm_key']
               for l in ledger)


def test_retry_same_key_does_not_deduct_twice(client, run_id):
    plan = _plan(client, run_id)
    payload = {'run_id': run_id, 'confirm_key': plan['confirm_key']}
    first = client.post('/api/reblend/confirm', json=payload).get_json()
    before = client.get('/api/reblend/inventory').get_json()['materials']
    second = client.post('/api/reblend/confirm', json=payload).get_json()
    after = client.get('/api/reblend/inventory').get_json()['materials']
    assert first['idempotent'] is False and second['idempotent'] is True
    assert before == after
    confirmations = client.get('/api/reblend/confirmations').get_json()
    assert len(confirmations) == 1


def test_stale_key_rejected_with_409(client, run_id):
    plan = _plan(client, run_id)
    # 先手工调整库存，使方案键漂移
    client.post('/api/reblend/replenish',
                json={'material_id': 'MAT-CU99', 'change_kg': '10', 'note': '插单入库'})
    resp = client.post('/api/reblend/confirm',
                       json={'run_id': run_id, 'confirm_key': plan['confirm_key']})
    assert resp.status_code == 409 and resp.get_json()['code'] == 'VERSION'


def test_keyless_confirm_uses_freshest_plan(client, run_id):
    resp = client.post('/api/reblend/confirm', json={'run_id': run_id})
    assert resp.status_code == 200 and resp.get_json()['idempotent'] is False


def test_over_issue_rolls_back_and_keeps_stock(client):
    import app.reblend_store as store
    before = client.get('/api/reblend/inventory').get_json()['materials']
    with pytest.raises(DomainError) as exc:
        store.confirm_plan('overissue', 'run-x', 'h',
                           [['F-X', 'MAT-ZN99', '999999']], [])
    assert exc.value.code == 'STOCK'
    # 整单回滚：库存与确认单数量均不变
    assert client.get('/api/reblend/inventory').get_json()['materials'] == before
    assert all(c['confirm_key'] != 'overissue'
               for c in client.get('/api/reblend/confirmations').get_json())


def test_concurrent_confirms_never_over_issue(client, run_id, monkeypatch, tmp_path):
    import app.reblend_store as store
    # 直接对库存层做并发：20kg库存，两个不同确认单各领15kg
    store.replenish('MAT-CU99', Decimal('-20'), '压测调账')
    outcomes = []

    def worker(key):
        try:
            store.confirm_plan(key, 'run-c', 'h', [['F-C', 'MAT-CU99', '15']], [])
            outcomes.append('OK')
        except DomainError:
            outcomes.append('REJECT')

    t1 = threading.Thread(target=worker, args=('ck-1',))
    t2 = threading.Thread(target=worker, args=('ck-2',))
    t1.start(); t2.start(); t1.join(); t2.join()
    assert sorted(outcomes) == ['OK', 'REJECT']
    balance = next(m for m in client.get('/api/reblend/inventory').get_json()['materials']
                   if m['material_id'] == 'MAT-CU99')['on_hand_kg']
    assert Decimal(balance) == Decimal('5')


def test_concurrent_same_key_deducts_once(client):
    import app.reblend_store as store
    outcomes = []

    def worker():
        out = store.confirm_plan('same-ck', 'run-d', 'h', [['F-D', 'MAT-ZN99', '3']], [])
        outcomes.append(out['idempotent'])

    t1 = threading.Thread(target=worker); t2 = threading.Thread(target=worker)
    t1.start(); t2.start(); t1.join(); t2.join()
    assert sorted(outcomes) == [False, True]
    confirmations = client.get('/api/reblend/confirmations').get_json()
    assert sum(1 for c in confirmations if c['confirm_key'] == 'same-ck') == 1


def test_replenish_rejects_negative_balance(client):
    ok = client.post('/api/reblend/replenish',
                     json={'material_id': 'MAT-CU99', 'change_kg': '1', 'note': '入库'})
    assert ok.status_code == 200
    bad = client.post('/api/reblend/replenish',
                      json={'material_id': 'MAT-CU99', 'change_kg': '-999999', 'note': '退库'})
    assert bad.status_code == 422 and bad.get_json()['code'] == 'STOCK'
    missing = client.post('/api/reblend/replenish',
                          json={'material_id': 'NOPE', 'change_kg': '1'})
    assert missing.status_code == 404


def test_reset_inventory_and_orders(client):
    inv = INV_CSV.read_text(encoding='utf-8-sig')
    orders = Path(__file__).resolve().parent.parent.joinpath(
        'sample_data/reblend_orders.csv').read_text(encoding='utf-8-sig')
    resp = client.post('/api/reblend/reset',
                       json={'inventory_csv': inv, 'orders_csv': orders})
    assert resp.status_code == 200
    assert {m['material_id'] for m in resp.get_json()['materials']} == {
        'MAT-CU99', 'MAT-ZN99', 'MAT-BRASS'}
    assert len(resp.get_json()['orders']) == 5


def test_m7_page_renders_reblend_panels(client, run_id):
    page = client.get(f'/work/7?run_id={run_id}').data.decode()
    assert 'reblend.js' in page
    assert '确认方案并扣减共用库存' in page
    assert 'data-run-id="' + run_id + '"' in page
    # 方案表格由JS异步拉取，首屏不内嵌方案行
    assert 'plan-confirm' in page


def test_plan_requires_run_id(client):
    assert client.post('/api/reblend/plan', json={}).status_code == 422
    assert client.post('/api/reblend/plan', json={'run_id': 'absent'}).status_code == 404


def test_legacy_withdrawal_trial_unchanged(client):
    resp = client.post('/api/m7', json={})
    assert resp.status_code == 200 and resp.get_json()['stage'] == 7
    assert len(resp.get_json()['rows']) == 24
