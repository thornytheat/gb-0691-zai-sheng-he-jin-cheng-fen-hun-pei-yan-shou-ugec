from decimal import Decimal
import pytest
from app.reblend import (
    element_dose_interval, build_plan, material_option, DOSE_STEP,
    post_dose_bounds, acceptance_status)
from app.reblend_store import parse_inventory, parse_orders
from app.errors import DomainError
from pathlib import Path

INV_CSV = Path(__file__).resolve().parent.parent / 'sample_data' / 'reblend_inventory.csv'
ORDER_CSV = Path(__file__).resolve().parent.parent / 'sample_data' / 'reblend_orders.csv'
DEMO_CSV = Path(__file__).resolve().parent.parent / 'sample_data' / 'reblend_demo.csv'


@pytest.fixture
def materials():
    return parse_inventory(INV_CSV.read_text(encoding='utf-8-sig'))


@pytest.fixture
def orders():
    return parse_orders(ORDER_CSV.read_text(encoding='utf-8-sig'))


@pytest.fixture
def acceptance():
    from app.pipeline import calculate
    run = calculate(DEMO_CSV.read_text(encoding='utf-8-sig'))
    return run['stages']['6']['rows']


def _row(batch, element, low_pct, high_pct, mass='1000'):
    m = Decimal(mass)
    return {'炉次': batch, '元素': element, '干重_kg': mass,
            '元素下界_kg': str(m * Decimal(low_pct) / 100),
            '元素上界_kg': str(m * Decimal(high_pct) / 100),
            '含量下界_pct': low_pct, '含量上界_pct': high_pct}


def _furnace(rows):
    return {'炉次': rows[0]['炉次'], '干重_kg': rows[0]['干重_kg'],
            'elements': {r['元素']: r for r in rows},
            '当前验收': '', '待救': True}


# ---------- 单元素剂量区间 ----------

def test_low_side_needs_positive_dose():
    # Cu 6%，补99.9%电解铜，目标下界7.5%：x >= (7.5*1000-60*100)/(99.9-7.5)
    interval = element_dose_interval(Decimal('60'), Decimal('60'), Decimal('1000'),
                                     Decimal('99.9'), Decimal('99.9'),
                                     (Decimal('7.5'), Decimal('12.5')))
    expect = (Decimal('7.5') * 1000 - 6000) / (Decimal('99.9') - Decimal('7.5'))
    assert interval[0] == pytest.approx(expect)
    assert interval[1] is not None and interval[1] > interval[0]


def test_high_side_cannot_be_fixed_by_same_element_material():
    # Cu 15% 已超上限12.5%，再补纯铜只会更高 => 不可行
    interval = element_dose_interval(Decimal('150'), Decimal('150'), Decimal('1000'),
                                     Decimal('99.9'), Decimal('99.9'),
                                     (Decimal('7.5'), Decimal('12.5')))
    assert interval is None


def test_dilution_material_has_upper_cap():
    # Cu 12.8% 略高于上限12.5%，补Cu含量确定为0的锌料可稀释：
    # 下界约束 x*(-7.5) >= -5300 给出上界 x <= 706.67；上界约束 x*(-12.5) >= 300 给出下界 x >= 24
    interval = element_dose_interval(Decimal('128'), Decimal('128'), Decimal('1000'),
                                     Decimal('0'), Decimal('0'),
                                     (Decimal('7.5'), Decimal('12.5')))
    assert interval is not None
    lower, upper = interval
    assert lower == pytest.approx(Decimal('24'))
    assert upper == pytest.approx(Decimal('706.6666666667'))


def test_unknown_assay_makes_mild_overflow_infeasible():
    # 补料未化验Cu按0~100保守：上界约束 87.5x <= -300 在x>=0无解，宁可判不可行也不冒险
    interval = element_dose_interval(Decimal('128'), Decimal('128'), Decimal('1000'),
                                     Decimal('0'), Decimal('100'),
                                     (Decimal('7.5'), Decimal('12.5')))
    assert interval is None


def test_dose_rounds_up_to_scale_step():
    from app.reblend import _ceil_step
    assert _ceil_step(Decimal('16.2334')) == Decimal('16.234')
    assert _ceil_step(Decimal('0')) == Decimal(0)
    assert DOSE_STEP == Decimal('0.001')


# ---------- 补后成分上下界与验收 ----------

def test_post_dose_bounds_land_on_target(materials):
    rows = [_row('F-X', 'Cu', '6', '6'), _row('F-X', 'Zn', '5', '5')]
    cu99 = next(m for m in materials if m['material_id'] == 'MAT-CU99')
    opt = material_option(_furnace(rows), cu99)
    assert opt['可行'] and opt['最小补料_kg'] == '16.2340'
    cu = next(c for c in opt['补后成分'] if c['元素'] == 'Cu')
    # 最小剂量把Cu下界恰好顶到验收下限7.5%
    assert cu['补后下界_pct'] == '7.5000' and cu['补后验收'] == '通过'
    zn = next(c for c in opt['补后成分'] if c['元素'] == 'Zn')
    # 电解铜不含Zn：缺测元素按0~100给上下界，下界稀释、上界放宽
    assert Decimal(zn['补后下界_pct']) < Decimal('5')
    assert Decimal(zn['补后上界_pct']) > Decimal('5')
    assert zn['补后验收'] == '通过'


def test_unmeasured_element_never_treated_as_zero(materials):
    # 库存CSV只登记已化验元素；材料未化验的元素在评估时按0~100保守区间，绝不当零
    cu99 = next(m for m in materials if m['material_id'] == 'MAT-CU99')
    assert 'Zn' not in cu99['assays']
    # Cu 6% 待救，必须补电解铜；该材料未化验Zn，补后Zn必须保留宽区间而非定值
    rows = [_row('F-X', 'Cu', '6', '6'), _row('F-X', 'Zn', '5', '5')]
    opt = material_option(_furnace(rows), cu99)
    assert opt['可行'] and Decimal(opt['最小补料_kg']) > 0
    zn = next(c for c in opt['补后成分'] if c['元素'] == 'Zn')
    low, high = post_dose_bounds(rows[1], Decimal(opt['最小补料_kg']), Decimal(0), Decimal(100))
    assert (zn['补后下界_pct'], zn['补后上界_pct']) == (low, high)
    # 未化验按0~100：下界被稀释到5以下，上界远高于5，绝不当作确定值5
    assert Decimal(low) < Decimal('5')
    assert Decimal(high) > Decimal('5')


def test_acceptance_status_matches_m6_rules():
    assert acceptance_status(Decimal('7.5'), Decimal('12.5'), 'Cu') == '通过'
    assert acceptance_status(Decimal('13'), Decimal('14'), 'Cu') == '不合格'
    assert acceptance_status(Decimal('7'), Decimal('8'), 'Cu') == '待补测'


def test_over_copper_furnace_infeasible_for_all_materials(materials, acceptance):
    f3105 = [r for r in acceptance if r['炉次'] == 'F-3105']
    furnace = _furnace(f3105)
    for material in materials:
        opt = material_option(furnace, material)
        assert opt['可行'] is False and opt['不可行原因']


# ---------- 整组分配 ----------

def test_plan_rescues_demo_furnaces_with_reasons(acceptance, materials, orders):
    plan = build_plan(acceptance, materials, orders, 'run-1')
    by_batch = {r['炉次']: r for r in plan['rows']}
    assert by_batch['F-3101']['分配状态'] == '已分配'
    assert by_batch['F-3101']['选用材料'] == 'MAT-CU99'
    assert '急单' in by_batch['F-3101']['分配原因']
    assert by_batch['F-3102']['选用材料'] == 'MAT-ZN99'
    assert by_batch['F-3103']['选用材料'] == 'MAT-BRASS'
    assert by_batch['F-3104']['分配状态'] == '免补料'
    assert by_batch['F-3105']['分配状态'] == '未分配'
    # 每一行都必须给出可复核的分配原因
    assert all(r['分配原因'] for r in plan['rows'])


def test_urgent_order_allocated_before_normal(acceptance, materials, orders):
    # 把电解铜库存压到只够急单F-3101（16.234），普通炉F-3103的纯铜路线被堵
    scarce = parse_inventory(INV_CSV.read_text(encoding='utf-8-sig').replace(
        'MAT-CU99,电解铜99,Cu,99.9,40', 'MAT-CU99,电解铜99,Cu,99.9,16.5'))
    plan = build_plan(acceptance, scarce, orders, 'run-1')
    by_batch = {r['炉次']: r for r in plan['rows']}
    assert by_batch['F-3101']['分配状态'] == '已分配'
    assert by_batch['F-3101']['选用材料'] == 'MAT-CU99'


def test_stock_shortage_never_over_issues(acceptance, orders):
    scarce = parse_inventory(INV_CSV.read_text(encoding='utf-8-sig')
                             .replace('MAT-CU99,电解铜99,Cu,99.9,40',
                                      'MAT-CU99,电解铜99,Cu,99.9,15')
                             .replace('MAT-BRASS,黄铜回炉料,Cu,65,80',
                                      'MAT-BRASS,黄铜回炉料,Cu,65,20')
                             .replace('MAT-BRASS,黄铜回炉料,Zn,33,80',
                                      'MAT-BRASS,黄铜回炉料,Zn,33,20'))
    plan = build_plan(acceptance, scarce, orders, 'run-1')
    by_batch = {r['炉次']: r for r in plan['rows']}
    assert by_batch['F-3101']['分配状态'] == '未分配'
    assert '不超领' in by_batch['F-3101']['分配原因']
    # 库存视图占用量不得超过现存量
    for item in plan['inventory']:
        assert Decimal(item['本方案占用_kg']) <= Decimal(item['现有库存_kg'])
        assert Decimal(item['方案后余量_kg']) >= 0


def test_normal_furnaces_sorted_by_min_dose(acceptance, materials, orders):
    plan = build_plan(acceptance, materials, orders, 'run-1')
    normal = [r for r in plan['rows'] if r['优先级'] != '急单' and r['分配状态'] == '已分配']
    doses = [Decimal(r['补料量_kg']) for r in normal]
    assert doses == sorted(doses)


def test_plan_hash_changes_with_stock(acceptance, materials, orders):
    first = build_plan(acceptance, materials, orders, 'run-1')
    changed = parse_inventory(INV_CSV.read_text(encoding='utf-8-sig').replace(
        'MAT-CU99,电解铜99,Cu,99.9,40', 'MAT-CU99,电解铜99,Cu,99.9,25'))
    second = build_plan(acceptance, changed, orders, 'run-1')
    assert first['plan_hash'] != second['plan_hash']


def test_plan_deterministic(acceptance, materials, orders):
    a = build_plan(acceptance, materials, orders, 'run-1')
    b = build_plan(acceptance, materials, orders, 'run-1')
    assert a['rows'] == b['rows'] and a['plan_hash'] == b['plan_hash']


def test_missing_element_record_rejected():
    bad = [_row('F-X', 'Cu', '6', '6')]
    with pytest.raises(DomainError) as exc:
        build_plan(bad, [], {}, 'run-1')
    assert exc.value.code == 'DATA'


# ---------- CSV 解析 ----------

def test_parse_inventory_merges_element_rows():
    materials = parse_inventory(INV_CSV.read_text(encoding='utf-8-sig'))
    brass = next(m for m in materials if m['material_id'] == 'MAT-BRASS')
    assert brass['assays']['Cu'] == (Decimal('65'), Decimal('65'))
    assert brass['assays']['Zn'] == (Decimal('33'), Decimal('33'))
    assert brass['on_hand_kg'] == Decimal('80')


def test_parse_inventory_rejects_bad_header():
    with pytest.raises(DomainError):
        parse_inventory('a,b\n1,2')


def test_parse_orders_validates_priority_and_date():
    with pytest.raises(DomainError):
        parse_orders('batch,due_date,priority\nF1,2026-13-01,急单')
    with pytest.raises(DomainError):
        parse_orders('batch,due_date,priority\nF1,2026-11-21,特急')
    orders = parse_orders('batch,due_date,priority\nF1,,普通\n')
    assert orders['F1'] == {'due_date': None, 'priority': '普通'}
