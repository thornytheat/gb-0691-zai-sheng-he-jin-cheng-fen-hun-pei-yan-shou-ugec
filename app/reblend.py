"""复配补料整组规划引擎（纯函数，不触碰数据库）。

输入：M6验收行、补料库存材料、急单交期表；输出：整组补料方案、
每炉分配原因、补料后成分上下界与补后验收，以及供确认防重的方案哈希。
"""
from decimal import Decimal, ROUND_CEILING
import hashlib
import json
from app.domain import TARGETS
from app.numeric import number, fmt
from app.errors import DomainError

DOSE_STEP = Decimal('0.001')          # 配料秤最小分度 1g
ELEMENTS = sorted(TARGETS)            # Cu / Zn
FAR_DATE = '9999-12-31'               # 未登记交期的炉次排在最后


def _ceil_step(value):
    if value <= 0:
        return Decimal(0)
    return (value / DOSE_STEP).to_integral_value(rounding=ROUND_CEILING) * DOSE_STEP


def element_dose_interval(low_kg, high_kg, mass_kg, c_low, c_high, target):
    """单元素约束下补料剂量 x(kg) 的可行区间。

    含量下界约束：(100*low + x*cL)/(M+x) >= lo  ⇔  x*(cL-lo) >= lo*M - 100*low
    含量上界约束：(100*high + x*cH)/(M+x) <= hi ⇔  x*(cH-hi) <= hi*M - 100*high
    系数为负时除号翻转不等号；返回 (下界, 上界)，上界 None 为正无穷；不可行返回 None。
    """
    lo, hi = target
    lower = Decimal(0)
    upper = None

    def tighten(coef, rhs):
        nonlocal lower, upper
        # 求解 coef * x [>=|<=] rhs
        if coef > 0:
            lower = max(lower, rhs / coef)
        elif coef == 0:
            if rhs > 0:                      # 0 >= 正数，永不成立
                return False
        else:
            cap = rhs / coef                 # 除以负数，>= 翻转为 <=
            if cap < 0:
                return False
            upper = cap if upper is None else min(upper, cap)
        return True

    if not tighten(c_low - lo, lo * mass_kg - 100 * low_kg):
        return None
    # 上界约束为 coef*x <= rhs：系数正时给上界，系数负时翻转为下界
    coef, rhs = c_high - hi, hi * mass_kg - 100 * high_kg
    if coef > 0:
        cap = rhs / coef
        if cap < 0:
            return None
        upper = cap if upper is None else min(upper, cap)
    elif coef == 0:
        if rhs < 0:                          # 0 <= 负数，永不成立
            return None
    else:
        lower = max(lower, rhs / coef)

    if upper is not None and lower > upper:
        return None
    return lower, upper


def _merge(intervals):
    """合并各元素的剂量区间，None 上界视为正无穷。"""
    lower = Decimal(0)
    upper = None
    for interval in intervals:
        if interval is None:
            return None
        lower = max(lower, interval[0])
        if interval[1] is not None:
            upper = interval[1] if upper is None else min(upper, interval[1])
    if upper is not None and lower > upper:
        return None
    return lower, upper


def post_dose_bounds(row, dose, c_low, c_high):
    """补料后该元素的含量上下界（质量分数%）。"""
    mass = number(row['干重_kg'])
    total = mass + dose
    low = (number(row['元素下界_kg']) + dose * c_low / 100) / total * 100
    high = (number(row['元素上界_kg']) + dose * c_high / 100) / total * 100
    return fmt(low), fmt(high)


def acceptance_status(low_pct, high_pct, element):
    lo, hi = TARGETS[element]
    if lo <= low_pct <= high_pct <= hi:
        return '通过'
    if high_pct < lo or low_pct > hi:
        return '不合格'
    return '待补测'


def material_option(furnace, material):
    """评估一种补料材料对该炉次的可行剂量与补后成分。"""
    intervals = []
    for element in ELEMENTS:
        row = furnace['elements'][element]
        c_low, c_high = material['assays'].get(element, (Decimal(0), Decimal(100)))
        intervals.append(element_dose_interval(
            number(row['元素下界_kg']), number(row['元素上界_kg']),
            number(row['干重_kg']), c_low, c_high, TARGETS[element]))
    merged = _merge(intervals)
    option = {
        '材料': material['material_id'], '材料名称': material['material_name'],
        '可行': False, '最小补料_kg': None, '最大补料_kg': None,
        '不可行原因': '', '补后成分': []}
    if merged is None:
        option['不可行原因'] = '现有材料含量无法把成分修入验收区间'
        return option
    lower, upper = merged
    dose = _ceil_step(lower)
    if upper is not None and dose > upper:
        option['不可行原因'] = '秤量分度下最小剂量仍超出可行上界'
        return option
    option['可行'] = True
    option['最小补料_kg'] = fmt(dose)
    option['最大补料_kg'] = '不限' if upper is None else fmt(upper)
    for element in ELEMENTS:
        row = furnace['elements'][element]
        c_low, c_high = material['assays'].get(element, (Decimal(0), Decimal(100)))
        low_pct, high_pct = post_dose_bounds(row, dose, c_low, c_high)
        lo, hi = TARGETS[element]
        option['补后成分'].append({
            '元素': element, '补后下界_pct': low_pct, '补后上界_pct': high_pct,
            '验收区间': f'{fmt(lo)}~{fmt(hi)}',
            '补后验收': acceptance_status(number(low_pct), number(high_pct), element)})
    return option


def _build_furnaces(acceptance_rows):
    furnaces = []
    for batch in sorted({r['炉次'] for r in acceptance_rows}):
        rows = [r for r in acceptance_rows if r['炉次'] == batch]
        elements = {r['元素']: r for r in rows}
        missing = [e for e in ELEMENTS if e not in elements]
        if missing:
            raise DomainError('DATA', f'炉次{batch}缺少元素化验记录：{"、".join(missing)}')
        current = {e: elements[e]['验收'] for e in ELEMENTS}
        furnaces.append({
            '炉次': batch, 'elements': elements,
            '干重_kg': elements[ELEMENTS[0]]['干重_kg'],
            '当前验收': '／'.join(f'{e}:{current[e]}' for e in ELEMENTS),
            '待救': any(current[e] != '通过' for e in ELEMENTS)})
    return furnaces


def _allocate(furnaces, materials, orders, run_id):
    stock = {m['material_id']: number(m['on_hand_kg']) for m in materials}
    plan_rows = []
    allocations = []

    def order_of(batch):
        return orders.get(batch, {'due_date': None, 'priority': '普通'})

    def cheapest(furnace):
        """该炉次所有可行材料按最小剂量升序、材料号升序排列。"""
        opts = [material_option(furnace, m) for m in materials]
        opts.sort(key=lambda o: (number(o['最小补料_kg']) if o['可行'] else Decimal('Infinity'),
                                 o['材料']))
        return opts

    def take(furnace, options, reason_builder):
        for opt in options:
            if not opt['可行']:
                continue
            need = number(opt['最小补料_kg'])
            if stock[opt['材料']] >= need:
                stock[opt['材料']] -= need
                allocations.append([furnace['炉次'], opt['材料'], str(need)])
                plan_rows.append(_row(furnace, order_of(furnace['炉次']), opt,
                                      '已分配', reason_builder(opt, need), options))
                return
        reasons = '；'.join(sorted({o['不可行原因'] for o in options if not o['可行']}))
        if any(o['可行'] for o in options):
            detail = '、'.join(f"{o['材料']}需{o['最小补料_kg']}kg/余"
                               f"{fmt(stock[o['材料']])}kg" for o in options if o['可行'])
            reason = '可行材料库存均不足，按规则不超领：' + detail
        else:
            reason = reasons or '无可用补料材料'
        plan_rows.append(_row(
            furnace, order_of(furnace['炉次']), None, '未分配', reason, options))

    urgent = [f for f in furnaces if f['待救'] and order_of(f['炉次'])['priority'] == '急单']
    normal = [f for f in furnaces if f['待救'] and order_of(f['炉次'])['priority'] != '急单']
    urgent.sort(key=lambda f: (order_of(f['炉次'])['due_date'] or FAR_DATE, f['炉次']))
    # 普通炉次按“最小耗料”轮候：先救耗料少的，同等耗料按炉次号
    normal.sort(key=lambda f: (_cheapest_dose(f, materials), f['炉次']))

    for furnace in urgent:
        take(furnace, cheapest(furnace),
             lambda opt, need, f=furnace: (
                 f"急单（交期{order_of(f['炉次'])['due_date'] or '未登记'}）优先分配；"
                 f"{opt['材料']}在可行材料中补料量最少（{opt['最小补料_kg']}kg）"))
    for furnace in normal:
        take(furnace, cheapest(furnace),
             lambda opt, need, f=furnace: (
                 '非急单按最小耗料量轮候分配；'
                 f"{opt['材料']}在可行材料中补料量最少（{opt['最小补料_kg']}kg）"))
    for furnace in [f for f in furnaces if not f['待救']]:
        plan_rows.append(_row(furnace, order_of(furnace['炉次']), None, '免补料',
                              '当前成分已全部落入验收区间，不占用共用库存', []))

    plan_rows.sort(key=lambda r: (r['优先级'] != '急单', r['交期'] or FAR_DATE, r['炉次']))
    snapshot = {m['material_id']: str(number(m['on_hand_kg'])) for m in materials}
    digest = hashlib.sha256(json.dumps(
        {'run_id': run_id, 'stock': snapshot, 'allocations': allocations,
         'orders': orders}, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()[:16]
    return plan_rows, allocations, stock, digest


def _cheapest_dose(furnace, materials):
    doses = [number(o['最小补料_kg']) for m in materials
             for o in [material_option(furnace, m)] if o['可行']]
    return min(doses) if doses else Decimal('Infinity')


def _row(furnace, order, chosen, status, reason, options):
    row = {
        '炉次': furnace['炉次'], '交期': order['due_date'] or '未登记',
        '优先级': order['priority'], '干重_kg': furnace['干重_kg'],
        '当前验收': furnace['当前验收'], '分配状态': status, '分配原因': reason,
        '选用材料': chosen['材料'] if chosen else '—',
        '补料量_kg': chosen['最小补料_kg'] if chosen else '0',
        '补后验收': '／'.join(f"{c['元素']}:{c['补后验收']}" for c in chosen['补后成分'])
        if chosen else '—',
        '补后区间': '；'.join(
            f"{c['元素']} {c['补后下界_pct']}~{c['补后上界_pct']}%" for c in chosen['补后成分'])
        if chosen else '—',
        '候选材料': options}
    return row


def build_plan(acceptance_rows, materials, orders, run_id):
    """生成整组补料方案。orders: {batch: {due_date, priority}}。"""
    furnaces = _build_furnaces(acceptance_rows)
    rows, allocations, remaining, digest = _allocate(furnaces, materials, orders, run_id)
    rescued = sum(1 for r in rows if r['分配状态'] == '已分配')
    metrics = [
        ['炉次总数', len(rows), '炉'],
        ['待救炉次', sum(1 for r in rows if r['分配状态'] in ('已分配', '未分配')), '炉'],
        ['计划救成', rescued, '炉'],
        ['急单救成', sum(1 for r in rows if r['优先级'] == '急单' and r['分配状态'] == '已分配'), '炉'],
        ['未分配', sum(1 for r in rows if r['分配状态'] == '未分配'), '炉'],
        ['库存材料', len(materials), '种'],
        ['规则版本', 'REBLEND-V2', '']]
    inventory = [{
        '材料': m['material_id'], '材料名称': m['material_name'],
        '现有库存_kg': fmt(number(m['on_hand_kg'])),
        '本方案占用_kg': fmt(number(m['on_hand_kg']) - remaining[m['material_id']]),
        '方案后余量_kg': fmt(remaining[m['material_id']])} for m in materials]
    return {
        'stage': 7, 'name': '复配补料方案', 'plan_hash': digest,
        'rows': rows, 'inventory': inventory, 'metrics': metrics,
        'allocations': allocations}
